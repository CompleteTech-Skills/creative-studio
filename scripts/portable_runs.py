"""Verified local snapshots, explicit schema migration, and portable restoration."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile
from asset_contracts import versions
from atomic_publish import _rename_no_replace

MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key in archive receipt: ' + key)
        result[key] = value
    return result


def migrate(root):
    import hatch
    data = hatch.read(root / 'imagegen-jobs.json')
    if data.get('schema_version') == 3:
        return {'migrated': False, 'schema_version': 3}
    if data.get('schema_version') != 2:
        raise ValueError('only controller run schema 2 can migrate to 3; preserve other runs and prepare separately')
    # Retain exact pre-migration evidence before any in-place metadata change.
    backup = root / ('migration-v2-' + hatch.uuid.uuid4().hex + '.zip')
    with zipfile.ZipFile(backup, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in ('imagegen-jobs.json', 'pet_request.json', 'profile.json', 'frames/frames-manifest.json', 'qa/visual-review.json'):
            path = root / name
            if path.is_file():
                archive.write(path, name)
    with zipfile.ZipFile(backup) as archive:
        if archive.testzip():
            raise ValueError('migration backup verification failed')
    data['original_root'] = data.get('run_dir')
    data['schema_version'] = 3
    data['versions'] = {'historical': None, 'migrated_with': versions(), 'unavailable_reason': 'Old run did not record tool/environment versions.'}
    for job in data['jobs']:
        try:
            valid = all(hatch.digest(hatch.inside(root, key)) == value for key, value in job.get('input_hashes', {}).items())
            if job['status'] == 'generated':
                valid = valid and hatch.digest(root / job['output_path']) == job['output_hash']
        except OSError:
            valid = False
        job['depends_on'] = [] if job['id'] == 'base' else ['base']
        job['input_images'] = [r for r in job['input_images'] if not r['path'].replace('\\', '/').startswith('decoded/')]
        if job['status'] == 'running':
            job['stale'] = True  # Original active token/receipt remains for reconciliation.
        elif job['status'] == 'generated' and valid:
            job['input_hashes'] = hatch.inputs(root, job)
        elif job['status'] == 'generated':
            job['status'] = 'pending'
        job['history'].append({'event': 'schema-migrated', 'old_evidence_valid': valid, 'time': hatch.time.time()})
    hatch.invalidate(data, 'schema migration requires extraction v4 and fresh visual review')
    hatch.event(data, 'schema-migrated', source=2, destination=3, backup=backup.name)
    hatch.save(root, data)
    return {'migrated': True, 'schema_version': 3, 'backup': str(backup),
            'revalidation_required': 'Run validate, inspect review UI and record a fresh visual review. Running claims must be reconciled first.'}


def archive_run(root, data, destination):
    import hatch
    destination = Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(root):
        raise ValueError('archive needs a new file outside the run')
    if any(j['status'] == 'running' for j in data['jobs']):
        raise ValueError('reconcile active claims before a portable archive; preserve the existing run identity')
    files = {name: value for name, value in hatch.artifact_hashes(root).items() if not name.startswith('@')}
    files['imagegen-jobs.json'] = hatch.digest(root / 'imagegen-jobs.json')
    if (root / 'qa/visual-review.json').exists():
        files['qa/visual-review.json'] = hatch.digest(root / 'qa/visual-review.json')
    if sum((root / name).stat().st_size for name in files) > MAX_ARCHIVE_BYTES:
        raise ValueError('archive exceeds 1 GiB supported snapshot bound')
    receipt = {'schema_version': 2, 'mode': 'full', 'files': files, 'run_id': data['run_id'],
               'fixture': data['fixture_mode'], 'versions': versions()}
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + '.' + hatch.uuid.uuid4().hex + '.tmp')
    try:
        with zipfile.ZipFile(temp, 'w', zipfile.ZIP_DEFLATED) as out:
            for name in files:
                out.write(root / name, name)
            out.writestr('archive-receipt.json', json.dumps(receipt))
        verify_archive(temp)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return {'archive': str(destination), 'sha256': hatch.digest(destination), 'run_id': data['run_id']}


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        names = [p.filename for p in entries]
        if len(names) != len(set(names)) or len(names) > 10000 or sum(p.file_size for p in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError('archive duplicate, entry count or decompressed size bound exceeded')
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (entry.orig_filename != entry.filename or not entry.filename or path.as_posix() != entry.filename or entry.filename == '.' or
                    path.is_absolute() or '..' in path.parts or '\\' in entry.filename or ':' in entry.filename or
                    (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('unsafe archive member: ' + entry.filename)
        receipt = json.loads(archive.read('archive-receipt.json'), object_pairs_hook=_unique_json_object)
        if not isinstance(receipt, dict):
            raise ValueError('archive receipt must be an object')
        if type(receipt.get('schema_version', 1)) is not int or receipt.get('schema_version', 1) not in (1, 2):
            raise ValueError('unsupported archive receipt version')
        files = receipt.get('files')
        if not isinstance(files, dict) or not files:
            raise ValueError('archive requires a nonempty hashed file inventory')
        if set(names) != set(files) | {'archive-receipt.json'}:
            raise ValueError('archive members differ from receipt')
        for name, expected in files.items():
            if hashlib.sha256(archive.read(name)).hexdigest() != expected:
                raise ValueError('archive hash mismatch: ' + name)
        return receipt


def restore_run(archive_path, destination):
    import hatch
    receipt = verify_archive(archive_path)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError('restore destination must not exist; use a new directory')
    # Resolve parent aliases without following a destination link created after the check.
    destination = destination.parent.resolve() / destination.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.hatch-restore-', dir=destination.parent))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for name in receipt['files']:
                path = hatch.inside(stage, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(name))
        data = hatch.read(stage / 'imagegen-jobs.json')
        if data.get('schema_version') == 2:
            migrate(stage)
            data = hatch.manifest(stage)
        elif data.get('schema_version') != 3:
            raise ValueError('archive needs unsupported run migration')
        prior_versions = receipt.get('versions')
        revalidate = prior_versions != versions() or receipt.get('mode') != 'full'
        if revalidate:
            hatch.invalidate(data, 'restoration tool versions or compact snapshot require revalidation')
        # Never act on external installations/package locations from the archived host.
        for key in ('installed', 'packaged'):
            previous = data.get('gates', {}).pop(key, None)
            if previous:
                hatch.event(data, 'external-receipt-retained', kind=key, previous=previous)
        data.pop('pending_install', None)
        hatch.event(data, 'restored', archive_sha256=hatch.digest(archive_path), revalidation_required=revalidate)
        hatch.save(stage, data)
        hatch.manifest(stage)
        if not revalidate and not hatch._review_ok(stage, data):
            revalidate = True
            hatch.invalidate(data, 'restored artifacts do not satisfy current validation and review')
            hatch.save(stage, data)
        _rename_no_replace(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {'restored': str(destination), 'run_id': data['run_id'], 'revalidation_required': revalidate,
            'external_integration_verified': False}
