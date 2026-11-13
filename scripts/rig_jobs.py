"""Transactional local rig runs, immutable revisions and bounded resumable steps."""
from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
from pathlib import Path
import shutil
import tempfile
import hashlib
import json
import os
import uuid

from atomic_publish import _rename_no_replace

from rig_contracts import COORDINATES, DEFAULT_LIMITS, validate_contract
from rig_errors import (VERSION, RigError, atomic_bytes, atomic_json, job_lock,
                        object_hash, read_json, decode_json, MAX_JSON_BYTES, require, safe_path, sha256, slug, timestamp)
from rig_models import RigAsset, load_rig, rig_signature, save_geometry, save_weights


_ACTIVE_TRANSACTION = ContextVar('rig_active_transaction', default=None)


def _manifest_target(root):
    root = Path(root).resolve()
    transaction = _ACTIVE_TRANSACTION.get()
    if transaction and transaction['root'] == root:
        return transaction['candidate']
    return root / 'rig.json'


def write_rig_manifest(root, manifest):
    """Stage a step's active pointer until its entire result is verified."""
    atomic_json(_manifest_target(root), validate_contract(manifest, 'Rig'))



def retain_artifact(root, label, value):
    """Store exact immutable JSON evidence and return its relative reference."""
    name = 'rig/artifacts/' + slug(label) + '-' + object_hash(value) + '.json'
    target = safe_path(root, name)
    payload = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    digest = hashlib.sha256(payload).hexdigest()
    if target.exists():
        require(sha256(target) == digest, 'RIG_ARTIFACT_COLLISION',
                'Immutable artifact content was modified.', asset=name)
    else:
        atomic_bytes(target, payload)
    return name


def provenance(operation, inputs=None, ancestry=None):
    return dict(schema_version=1, tool_version=VERSION, operation=operation,
                created_at=timestamp(), input_hashes=inputs or {}, ancestry=ancestry or [])


def next_version(version):
    a, b, c = map(int, version.split('.'));return f'{a}.{b}.{c+1}'


def save_rig(rig: RigAsset, *, bump=True, operation='save') -> dict:
    """Write immutable blobs first and atomically promote the one active manifest last."""
    require(rig.root is not None, 'RIG_RUN', 'The rig needs a run directory.')
    root = Path(rig.root);target = _manifest_target(root)
    old = read_json(target) if target.exists() else None
    manifest = deepcopy(rig.manifest)
    if old and bump:
        manifest['version'] = next_version(old['version']);manifest['revision'] = old['revision']+1
        manifest['review'] = dict(status='pending', receipts=[]);manifest['validation'] = []
        manifest['provenance'] = provenance(operation, {'previous': rig_signature(old)},
                                            [*old['provenance']['ancestry'], rig_signature(old)][-1000:])
    manifest['geometry'] = save_geometry(root, rig.geometry, manifest['limits'])
    if old:
        manifest['geometry']['version']=(next_version(old['geometry']['version']) if
            manifest['geometry']['data']['sha256']!=old['geometry']['data']['sha256'] else old['geometry']['version'])
    manifest['skins'] = [save_weights(root, w, manifest['version']) for _, w in sorted(rig.weights.items())]
    manifest['animations'] = []
    for key, clip in sorted(rig.clips.items()):
        require(key == clip['id'], 'RIG_ANIMATION_ID', 'Clip dictionary key differs from its identifier.')
        validate_contract(clip, 'AnimationClip')
        digest = object_hash(clip);name = f'animations/{slug(key)}-{digest}.json'
        target = safe_path(root, name)
        if not target.exists():atomic_json(target, clip)
        manifest['animations'].append(dict(id=key, version=clip['version'], path=name, sha256=sha256(target)))
    validate_contract(manifest, 'Rig')
    if old:
        snapshot = f'rig/revisions/{old["revision"]:06d}-{rig_signature(old)}.json'
        target = safe_path(root, snapshot)
        if target.exists():require(read_json(target) == old, 'RIG_REVISION_COLLISION', 'Immutable revision was modified.')
        else:atomic_json(target, old)
    write_rig_manifest(root, manifest)
    rig.manifest = manifest
    if (root/'job.json').exists():
        job=read_json(root/'job.json')
        if not any(s['status']=='running' for s in job['steps'].values()):
            job['rig_sha256']=rig_signature(manifest);job['limits']=manifest['limits']
            job['status']=('animation-ready' if rig.clips and rig.weights else 'weights-generated' if rig.weights
                           else 'needs-correction' if any(u['resolution']=='pending' for u in manifest['uncertainties'])
                           else 'skeleton-generated' if manifest['skeleton'] else 'imported')
            for step in job['steps'].values():
                if step['output_sha256']!=job['rig_sha256'] and step['status']=='succeeded':step['status']='stale'
            job['history'].append(dict(operation=operation,at=timestamp(),signature=job['rig_sha256']))
            job['history']=job['history'][-10000:];atomic_json(root/'job.json',validate_contract(job,'RigJob'))
    return manifest


def create_run(model, root, *, identifier=None, scale=1., limits=None, fixture=False,
               origin='import', attribution='User-supplied model; see original source files.', blender=None) -> RigAsset:
    """Publish a validated run whose geometry and provenance share frozen bytes."""
    from rig_source_snapshot import import_snapshot
    root = Path(root).absolute();model = Path(model).absolute()
    require(not root.is_symlink(), 'RIG_RUN_PATH', 'Run directories cannot be symbolic links.')
    limits = {**DEFAULT_LIMITS, **(limits or {})};validate_contract(limits, 'Limits')
    require(limits['influences'] in (4, 8), 'RIG_INFLUENCES', 'Choose four or eight influences.')
    with job_lock(root):
        require(not root.exists() and not root.is_symlink(), 'RIG_RUN_EXISTS',
                'Import into a new destination; existing directories are preserved.', preserved=str(root))
        with import_snapshot(model, scale=scale, limits=limits, blender=blender) as snapshot:
            imported = snapshot.model
            fixture = bool(fixture or imported.report.get('fixture'))
            if fixture:origin = 'procedural-fixture'
            identifier = slug(identifier or model.stem)
            records = {record['path']: record for record in snapshot.source_records}
            entry = snapshot.provenance['source_entry']
            require(entry in records, 'RIG_SOURCE_INVENTORY', 'Import did not retain the original model.')
            source_hash = records[entry]['sha256']
            # The original source can change now without altering the captured
            # model or the provenance published for it.
            with tempfile.TemporaryDirectory(prefix='.rig-import-', dir=root.parent) as temporary:
                stage = Path(temporary)/'run';stage.mkdir()
                dependencies = []
                for relative, record in sorted(records.items()):
                    name = 'source/'+relative
                    source = safe_path(snapshot.root, relative, exists=True)
                    target = safe_path(stage, name);target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target)
                    require(target.stat().st_size == record['bytes'] and sha256(target) == record['sha256'],
                            'RIG_SOURCE_COPY', 'Captured source copy failed its byte hash.', asset=relative)
                    dependencies.append(dict(path=name, sha256=record['sha256']))
                source_name = 'source/'+entry
                identity = dict(schema_version=1, id=identifier, source_sha256=source_hash, fixture=fixture,
                                origin=origin, attribution=attribution, coordinate_system=COORDINATES,
                                import_scale=scale, approved_artwork=False)
                atomic_json(stage/'identity.json', identity)
                facial = dict(schema_version=1, eyes=[], morph_targets={m.id:list(m.morphs) for m in imported.geometry.meshes if m.morphs}, expressions={})
                manifest = dict(schema_version=1, kind='hatch-3d-rig', id=identifier, version='1.0.0', revision=0,
                    character=dict(schema_version=1, id=identifier, version='1.0.0', identity_sha256=sha256(stage/'identity.json')),
                    source=dict(schema_version=1, path=source_name, sha256=source_hash, origin=origin,
                                fixture=fixture, attribution=attribution, dependencies=dependencies),
                    coordinate_system=deepcopy(COORDINATES), archetype='custom', geometry={}, skeleton=imported.skeleton,
                    skins=[], controllers=dict(ik=[], constraints=[]), facial=facial, attachments=[], animations=[],
                    state_mapping={}, uncertainties=[], provenance=provenance('import', {'source':source_hash}),
                    limits=limits, review=dict(status='pending', receipts=[]), validation=[])
                rig = RigAsset(manifest, imported.geometry, imported.weights, imported.clips, stage)
                for clip in rig.clips.values():
                    clip['rig_id']=identifier
                    if rig.manifest['skeleton']:clip['skeleton_version']=rig.skeleton['version']
                save_rig(rig, bump=False)
                atomic_json(stage/'validation/import.json', imported.report)
                job = dict(schema_version=1, kind='hatch-3d-rig-job', id=identifier, status='imported',
                           rig_sha256=rig_signature(rig.manifest), steps={}, limits=limits,
                           history=[dict(operation='import', at=timestamp(), signature=rig_signature(rig.manifest))])
                atomic_json(stage/'job.json', validate_contract(job, 'RigJob'))
                # Reopen the complete staged run before the only publication.
                load_rig(stage)
                try:
                    _rename_no_replace(stage, root)
                except FileExistsError as exc:
                    raise RigError('RIG_RUN_EXISTS', 'Another writer created the destination; it was preserved.',
                                   preserved=str(root)) from exc
                return load_rig(root)


def code_signature():
    return object_hash({p.name:sha256(p) for p in sorted(Path(__file__).parent.glob('rig_*.py'))})


def _finish_transaction(root, name, step, job, directory, record):
    """Finish a prepared pointer/journal commit, including after process death."""
    require(record.get('phase') == 'prepared' and record.get('token') == step['token'] and
            record.get('input_sha256') == step['input_sha256'],
            'RIG_TRANSACTION', 'The step transaction does not match its running journal.')
    candidate = safe_path(directory, 'manifest.json', exists=True)
    with candidate.open('rb') as source:
        candidate_payload = source.read(MAX_JSON_BYTES + 1)
    manifest = decode_json(candidate_payload)
    require(hashlib.sha256(candidate_payload).hexdigest() == record.get('manifest_sha256'), 'RIG_TRANSACTION',
            'Prepared transaction manifest was modified.')
    active = load_rig(root, manifest_override=manifest)
    prepared = record.get('step')
    require(isinstance(prepared, dict) and prepared.get('token') == step['token'] and
            prepared.get('input_sha256') == step['input_sha256'] and
            prepared.get('parameters_sha256') == step['parameters_sha256'] and
            prepared.get('code_sha256') == step['code_sha256'] and
            prepared.get('status') == 'succeeded' and
            prepared.get('parameters') == step['parameters'] and
            prepared.get('attempts') == step['attempts'] and
            prepared.get('started_at') == step['started_at'] and
            prepared.get('output_sha256') == rig_signature(manifest),
            'RIG_TRANSACTION', 'Prepared transaction step differs from its running journal.')
    trial = deepcopy(job)
    trial['steps'][name] = prepared
    trial['status'] = record.get('job_status')
    validate_contract(trial, 'RigJob')
    for artifact in prepared['artifacts']:
        require(sha256(safe_path(root, artifact['path'], exists=True)) == artifact['sha256'],
                'RIG_TRANSACTION', 'Prepared transaction artifact changed.')
    observed = sha256(root / 'rig.json')
    require(observed in (record.get('active_sha256'), record['manifest_sha256']),
            'RIG_TRANSACTION_CONFLICT', 'Another active revision superseded this transaction; it is preserved.')
    finished = deepcopy(job)
    finished['steps'][name] = prepared
    finished['status'] = record['job_status']
    finished['rig_sha256'] = prepared['output_sha256']
    finished['limits'] = active.manifest['limits']
    if prepared['output_sha256'] != step['input_sha256']:
        for key, other in finished['steps'].items():
            if key != name and other['status'] == 'succeeded':
                other['status'] = 'stale'
    finished['history'].append(dict(operation=name, status='succeeded',
                                    at=prepared['finished_at'], token=step['token']))
    finished['history'] = finished['history'][-10000:]
    validate_contract(finished, 'RigJob')
    if observed != record['manifest_sha256']:
        atomic_bytes(root / 'rig.json', candidate_payload)
    atomic_json(root / 'job.json', finished)
    atomic_json(directory / 'record.json', dict(record, phase='committed'))
    return finished


def _recover_transactions(root, job):
    """Recover only journal-owned attempts; unrelated files are never removed."""
    changed = False
    for name, step in list(job['steps'].items()):
        if step['status'] != 'running':
            continue
        try:
            token = str(uuid.UUID(step['token']))
        except (ValueError, AttributeError) as exc:
            raise RigError('RIG_TRANSACTION', 'Invalid running attempt token.') from exc
        require(token == step['token'], 'RIG_TRANSACTION', 'Attempt tokens must be canonical UUIDs.')
        directory = safe_path(root, 'rig/transactions/' + token)
        record_path = safe_path(directory, 'record.json')
        if record_path.exists():
            record = read_json(record_path)
            require(isinstance(record, dict) and record.get('token') == token and
                    record.get('input_sha256') == step['input_sha256'],
                    'RIG_TRANSACTION', 'Transaction does not match its journal owner.')
            if record.get('phase') == 'prepared':
                job = _finish_transaction(root, name, step, job, directory, record)
                continue
            require(record.get('phase') in ('started', 'failed'), 'RIG_TRANSACTION',
                    'Running journal has an unsupported transaction phase.')
            require(sha256(root / 'rig.json') == record.get('active_sha256'),
                    'RIG_TRANSACTION_CONFLICT', 'Active revision changed during an interrupted step; it is preserved.')
        # Old-version running journals have no staged transaction. Never assume
        # they can be rolled back; record interruption and preserve actual state.
        step['status'] = 'interrupted'
        step['finished_at'] = timestamp()
        job['history'].append(dict(operation=name, status='interrupted',
                                   at=step['finished_at'], token=step['token']))
        changed = True
    if changed:
        job['history'] = job['history'][-10000:]
        atomic_json(root / 'job.json', validate_contract(job, 'RigJob'))
    return job


def run_step(root, name, parameters, action, *, status, retry=False):
    """Single writer with staged active revisions and recoverable commit records."""
    root = Path(root).resolve()
    with job_lock(root):
        job = validate_contract(read_json(root / 'job.json'), 'RigJob')
        job = _recover_transactions(root, job)
        rig = load_rig(root)
        current = rig_signature(rig.manifest)
        param_hash, code_hash = object_hash(parameters), code_signature()
        previous = job['steps'].get(name)
        same = previous and previous['parameters_sha256'] == param_hash and previous['code_sha256'] == code_hash
        if same and previous['status'] == 'succeeded' and previous['output_sha256'] == current:
            intact = all(sha256(safe_path(root, d['path'], exists=True)) == d['sha256'] for d in previous['artifacts'])
            if intact:
                return dict(status='cached', step=name, rig_sha256=current, artifacts=previous['artifacts'])
        attempts = previous['attempts'] if same and previous['input_sha256'] == current else 0
        require(attempts < rig.manifest['limits']['attempts'], 'RIG_ATTEMPTS',
                'Step attempt budget is exhausted; correct the input or parameters.')
        step = dict(status='running', input_sha256=current, output_sha256=None, parameters=deepcopy(parameters),
                    parameters_sha256=param_hash, code_sha256=code_hash, attempts=attempts + 1,
                    token=str(uuid.uuid4()), started_at=timestamp(), finished_at=None, error=None, artifacts=[])
        transaction_root = safe_path(root, 'rig/transactions')
        if transaction_root.exists():
            with os.scandir(transaction_root) as retained:
                for count, _ in enumerate(retained, 1):
                    require(count < rig.manifest['limits']['archive_files'], 'RIG_TRANSACTION_LIMIT',
                            'Retained transaction budget is exhausted; archive this run before further work.')
        directory = safe_path(root, 'rig/transactions/' + step['token'])
        directory.mkdir(parents=True, exist_ok=False)
        candidate = directory / 'manifest.json'
        atomic_bytes(candidate, (root / 'rig.json').read_bytes())
        require(read_json(candidate) == rig.manifest and sha256(candidate) == sha256(root / 'rig.json'),
                'RIG_TRANSACTION_CONFLICT', 'The input revision changed while preparing the step; it is preserved.')
        record = dict(schema_version=1, token=step['token'], phase='started',
                      active_sha256=sha256(root / 'rig.json'), input_sha256=current)
        atomic_json(directory / 'record.json', record)
        job['steps'][name] = step
        atomic_json(root / 'job.json', validate_contract(job, 'RigJob'))
        context = _ACTIVE_TRANSACTION.set(dict(root=root, candidate=candidate))
        try:
            result = action(rig) or {}
            active = load_rig(root, manifest_override=read_json(candidate))
            step['output_sha256'] = rig_signature(active.manifest)
            for artifact_name in result.get('artifact_paths', []):
                path = safe_path(root, artifact_name, exists=True)
                step['artifacts'].append(dict(path=artifact_name, sha256=sha256(path)))
            step['status'] = 'succeeded'
            step['finished_at'] = timestamp()
            record.update(phase='prepared', manifest_sha256=sha256(candidate), step=deepcopy(step),
                          job_status=result.get('job_status', status))
            # Validate the complete prospective job before entering commit.
            prospective = deepcopy(job)
            prospective.update(status=record['job_status'], rig_sha256=step['output_sha256'], limits=active.manifest['limits'])
            validate_contract(prospective, 'RigJob')
        except Exception as exc:
            error = exc.report if isinstance(exc, RigError) else RigError('RIG_STEP_FAILED', str(exc), stage=name,
                           retryable=True, preserved=str(root)).report
            step.update(status='failed', error=error, finished_at=timestamp(), output_sha256=None)
            job['status'] = 'failed'
            job['history'].append(dict(operation=name, status='failed', at=step['finished_at'], token=step['token']))
            job['history'] = job['history'][-10000:]
            failed_record = dict(record, phase='failed', error=error)
            atomic_json(directory / 'record.json', failed_record)
            atomic_json(root / 'job.json', validate_contract(job, 'RigJob'))
            # An admission rejection with an unchanged candidate has no derived
            # revision to retain. Its diagnostic remains in the job journal.
            if sha256(candidate) == record['active_sha256']:
                owned = [(path, path.stat(), sha256(path)) for path in
                         (candidate, directory / 'record.json')]
                for path, identity, digest in owned:
                    if (not path.is_symlink() and path.exists() and
                            path.stat().st_ino == identity.st_ino and
                            path.stat().st_dev == identity.st_dev and sha256(path) == digest):
                        path.unlink()
                try:
                    directory.rmdir()
                except OSError:
                    pass
            if isinstance(exc, RigError):
                raise
            raise RigError(**{k:v for k,v in error.items() if k not in ('expected','observed')}) from exc
        finally:
            _ACTIVE_TRANSACTION.reset(context)
        # A crash after this durable preparation can finish idempotently without
        # running action again. Until then the active manifest is untouched.
        atomic_json(directory / 'record.json', record)
        running = read_json(root / 'job.json')['steps'][name]
        _finish_transaction(root, name, running, job, directory, record)
        return dict(status='succeeded', step=name, rig_sha256=step['output_sha256'], **result)


def configure_limits(rig, changes):
    value={**rig.manifest['limits'], **changes};validate_contract(value, 'Limits')
    require(value['influences'] in (4,8), 'RIG_INFLUENCES', 'Choose four or eight influences.')
    require(rig.geometry.vertex_count <= value['vertices'] and rig.geometry.triangle_count <= value['triangles'],
            'RIG_LIMIT_GEOMETRY', 'Existing geometry exceeds the proposed limits.')
    if rig.manifest['skeleton']:require(len(rig.skeleton['bones']) <= value['bones'], 'RIG_LIMIT_BONES', 'Existing skeleton exceeds the proposed limits.')
    rig.manifest['limits']=value;save_rig(rig, operation='configure-limits')


def status(root):
    rig=load_rig(root);job=validate_contract(read_json(Path(root)/'job.json'), 'RigJob')
    return dict(id=rig.manifest['id'], version=rig.manifest['version'], rig_sha256=rig_signature(rig.manifest),
                status=job['status'], review=rig.manifest['review']['status'], fixture=rig.manifest['source']['fixture'],
                meshes=len(rig.geometry.meshes), vertices=rig.geometry.vertex_count,
                bones=len(rig.skeleton['bones']) if rig.manifest['skeleton'] else 0,
                skins=len(rig.weights), clips=list(rig.clips), steps=job['steps'])
