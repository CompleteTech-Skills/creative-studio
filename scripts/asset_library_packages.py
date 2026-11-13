"""Exact asset closure packages using shared source transport and archive guards.

Packaging preserves existing manifests and approvals. It certifies complete bytes,
not technical/artistic approval or permission to distribute them publicly.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile

from asset_library import AssetLibrary, read_source_bytes
from asset_library_contracts import canonical_bytes, load_manifest, portable_path, validate
from asset_library_dependencies import DependencyGraph, location_key, dependency_edges
from rig_errors import RigError, decode_json, object_hash
from production_sources import (_new_destination, _publish_stage, _portable_inventory,
    create_source_bundle, restore_source_bundle, verify_source_bundle, SOURCE_INDEX)
from portable_runs import verify_archive

INDEX = 'asset-package.json'
MAX_FILES = 4096
MAX_BYTES = 256 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
PACKAGE_TYPES = ('asset', 'collection', 'prefab', 'scene', 'project', 'library')


def fail(code, message):
    raise RigError('LIBRARY_PACKAGE_' + code, message)


def _keys(value, names, label):
    if not isinstance(value, dict) or set(value) != set(names):
        fail('SCHEMA', label + ' has missing or unsupported fields.')


def _read(root, relative, limit=MAX_FILE_BYTES):
    portable_path(relative)
    return read_source_bytes(Path(root) / relative, limit)


def _record(payload):
    return dict(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())


def _inventory(root):
    """Enumerate private ordinary files without following any directory links."""
    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        fail('PATH', 'Package root must be an ordinary existing directory.')
    result = set()
    for current, directories, files in os.walk(root, followlinks=False):
        for name in [*directories, *files]:
            path = Path(current) / name
            if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                fail('PATH', 'Package links and reparse points are forbidden.')
            portable_path(path.relative_to(root).as_posix())
        for name in files:
            info = (Path(current) / name).lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                fail('PATH', 'Package payloads must be ordinary files without hardlinks.')
            result.add((Path(current) / name).relative_to(root).as_posix())
            if len(result) > MAX_FILES:
                fail('LIMIT', 'Package has too many files.')
    _portable_inventory(result)
    return result


def _policy(value):
    _keys(value, ('require_sources', 'require_license_evidence', 'intended_use'), 'Policy')
    if any(type(value[k]) is not bool for k in ('require_sources', 'require_license_evidence')):
        fail('SCHEMA', 'Policy flags must be booleans.')
    if not isinstance(value['intended_use'], str) or not 1 <= len(value['intended_use']) <= 256:
        fail('SCHEMA', 'An explicit bounded intended use is required.')
    return deepcopy(value)


def _gate(manifest, policy):
    if policy['require_sources'] and not manifest['provenance']['sources']:
        fail('SOURCE', 'Policy requires retained source evidence.')
    if policy['require_license_evidence'] and not manifest['provenance']['license_evidence']:
        fail('LICENSE', 'Policy requires retained license evidence.')
    # Every evidence reference must remain an inventoried exact record.
    inventory = {f['path']: f for f in manifest['files']}
    for record in [*manifest['provenance']['sources'], *manifest['provenance']['license_evidence']]:
        if inventory.get(record['path']) != record:
            fail('EVIDENCE', 'Source/license evidence differs from actual inventory.')
    return dict(asset=dict(id=manifest['identity']['id'], version=manifest['version'],
                          sha256=manifest['manifest_sha256']),
                declared_use_permitted=policy['intended_use'] in manifest['metadata']['allowed_uses'],
                license=manifest['metadata']['license'],
                license_evidence_retained=bool(manifest['provenance']['license_evidence']),
                visual_review=manifest['metadata']['review_status'],
                distribution=manifest['publication']['distribution'])


def _restorable_identities(manifests):
    """One canonical target cannot merge conflicting original identity spaces."""
    versions = {}; identities = {}
    for manifest in manifests:
        identity = manifest['identity']; name = identity['id']
        stable = tuple(identity[k] for k in ('namespace', 'library', 'kind', 'created_at'))
        if identities.setdefault(name, stable) != stable:
            fail('IDENTITY', 'Scoped assets disagree on stable identity: ' + name)
        key = (name, manifest['version'])
        if versions.setdefault(key, manifest['manifest_sha256']) != manifest['manifest_sha256']:
            fail('IDENTITY', 'Scoped assets conflict on one immutable ID/version: ' + name)


def create_package(graph: DependencyGraph, roots: list, destination, *, package_id: str,
                   package_type='collection', policy=None, context_files=None, scopes=None) -> dict:
    """Publish a complete directory with unchanged manifests and verified blobs.

    Scene/project context files are explicit consumer documents; callers retain
    their existing document validators. Every source file is included, including
    optional own files. Required transitive assets use the dependency authority.
    """
    if package_type not in PACKAGE_TYPES:
        fail('TYPE', 'Unsupported package type.')
    if not isinstance(package_id, str) or not 1 <= len(package_id) <= 256:
        fail('IDENTITY', 'Package needs a bounded explicit identity.')
    policy = _policy(policy or dict(require_sources=True, require_license_evidence=True,
                                    intended_use='private-reuse'))
    context_files = {} if context_files is None else context_files
    if not isinstance(context_files, dict) or len(context_files) > 256:
        fail('CONTEXT', 'Context files must be an explicit bounded path mapping.')
    if package_type in ('scene', 'project') and not context_files:
        fail('CONTEXT', 'Scene/project packages require their consumer documents.')
    destination = _new_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if scopes is None:
        scopes = {'default': roots}
    if not isinstance(scopes, dict) or not 1 <= len(scopes) <= 256:
        fail('SCOPE', 'Supply one to 256 explicitly independent scopes.')
    locks = {}
    for name, scope_roots in sorted(scopes.items()):
        portable_path(name)
        if '/' in name or len(name) > 64:
            fail('SCOPE', 'Scope names must be bounded single path components.')
        locks[name] = graph.lock(scope_roots, include_optional=True)
        if locks[name]['graph']['diagnostics']:
            fail('DEPENDENCY', 'Canonical restoration requires every declared dependency, including optional links.')
    nodes = {location_key(node): node for lock in locks.values() for node in lock['graph']['nodes']}
    if len(nodes) > 4096:
        fail('LIMIT', 'Scoped union exceeds 4096 assets.')
    if package_type == 'asset' and len(roots) != 1:
        fail('TYPE', 'An individual asset package needs exactly one root.')
    stage = Path(tempfile.mkdtemp(prefix='.asset-package-', dir=destination.parent))
    try:
        members = {}; assets = []; fixture = True; manifests = []
        def write(relative, payload):
            portable_path(relative)
            if len(payload) > MAX_FILE_BYTES:
                fail('LIMIT', 'Package member exceeds 64 MiB.')
            record = _record(payload)
            if relative in members:
                if members[relative] != record:
                    fail('COLLISION', 'Package path has conflicting content.')
                return
            if len(members) >= MAX_FILES - 1 or sum(x['bytes'] for x in members.values()) + len(payload) > MAX_BYTES:
                fail('LIMIT', 'Package exceeds file or byte limits.')
            members[relative] = record
            path = stage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        for node in sorted(nodes.values(), key=location_key):
            location = dict(library=node['library'], reference=node['reference'])
            manifest = graph.inspect(location)
            _gate(manifest, policy)
            manifests.append(manifest)
            _restorable_identities(manifests)
            fixture = fixture and manifest['provenance']['fixture']
            relative = 'manifests/' + manifest['manifest_sha256'] + '.json'
            write(relative, canonical_bytes(manifest))
            assets.append(dict(location=location, manifest=relative))
            for record in manifest['files']:
                payload = graph.owner(location).read_own_file(location['reference'], record['path'])
                if _record(payload) != {k: record[k] for k in ('bytes', 'sha256')}:
                    fail('HASH', 'Library changed while packaging.')
                write('blobs/' + record['sha256'], payload)
        lock_paths = {}
        for name, lock in locks.items():
            lock_paths[name] = 'locks/' + name + '.json'
            write(lock_paths[name], canonical_bytes(lock))
        context = []
        for relative, source in sorted(context_files.items()):
            portable_path(relative)
            path = 'context/' + relative
            write(path, read_source_bytes(source, MAX_FILE_BYTES))
            context.append(path)
        index = dict(schema_version=1, kind='asset-library-package', package_id=package_id,
                     package_type=package_type, fixture=fixture, policy=policy,
                     locks=lock_paths, assets=assets, context=context, members=members)
        index['sha256'] = object_hash(index)
        raw = canonical_bytes(index)
        if len(raw) > MAX_INDEX_BYTES:
            fail('LIMIT', 'Package index exceeds 4 MiB.')
        (stage / INDEX).write_bytes(raw)
        report = verify_package(stage)
        for lock in locks.values():
            graph.verify_lock(lock)
        _publish_stage(stage, destination)
        return {**report, 'package': str(destination), 'created': True}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


class _PackageLibrary:
    def __init__(self, manifests):
        self.manifests = manifests

    def inspect(self, reference, *, verify_payloads=True):
        validate(reference, 'AssetRef')
        token = (reference['id'], reference['version'], reference['sha256'])
        if token not in self.manifests:
            fail('MISSING', 'Exact package asset version is absent.')
        return self.manifests[token]


def _verify(root):
    root = Path(root).absolute()
    index = decode_json(_read(root, INDEX, MAX_INDEX_BYTES))
    _keys(index, ('schema_version', 'kind', 'package_id', 'package_type', 'fixture', 'policy',
                  'locks', 'assets', 'context', 'members', 'sha256'), 'Package index')
    if type(index['schema_version']) is not int or index['schema_version'] != 1 or index['kind'] != 'asset-library-package':
        fail('SCHEMA', 'Unsupported package schema.')
    if (index['package_type'] not in PACKAGE_TYPES or type(index['fixture']) is not bool or
        not isinstance(index['package_id'], str) or not 1 <= len(index['package_id']) <= 256):
        fail('SCHEMA', 'Invalid package identity/type/fixture.')
    if object_hash({k: v for k, v in index.items() if k != 'sha256'}) != index['sha256']:
        fail('HASH', 'Package index hash differs.')
    policy = _policy(index['policy'])
    if not isinstance(index['members'], dict) or not 1 <= len(index['members']) < MAX_FILES:
        fail('LIMIT', 'Package needs a bounded nonempty inventory.')
    if _inventory(root) != set(index['members']) | {INDEX}:
        fail('INCOMPLETE', 'Missing or extra package payload members.')
    total = 0
    for path, record in index['members'].items():
        _keys(record, ('bytes', 'sha256'), 'Member')
        if type(record['bytes']) is not int or not 1 <= record['bytes'] <= MAX_FILE_BYTES:
            fail('LIMIT', 'Member byte count exceeds its bounds.')
        total += record['bytes']
        if total > MAX_BYTES:
            fail('LIMIT', 'Package expanded bytes exceed 256 MiB.')
        if _record(_read(root, path, record['bytes'])) != record:
            fail('HASH', 'Package payload hash/bytes differ: ' + path)
    if not isinstance(index['locks'], dict) or not 1 <= len(index['locks']) <= 256:
        fail('SCHEMA', 'Invalid scoped lock inventory.')
    locks = {}
    for name, path in index['locks'].items():
        portable_path(name)
        if '/' in name or len(name) > 64 or path != 'locks/' + name + '.json':
            fail('SCHEMA', 'Noncanonical scope/lock path.')
        locks[name] = decode_json(_read(root, path, MAX_INDEX_BYTES))
    if not isinstance(index['assets'], list) or not 1 <= len(index['assets']) <= 4096:
        fail('LIMIT', 'Package asset inventory exceeds bounds.')
    expected = set(index['locks'].values()); libraries = {}; manifests = []; gates = []; fixture = True
    locations = []
    for asset in index['assets']:
        _keys(asset, ('location', 'manifest'), 'Package asset')
        node = asset['location']; token = location_key(node)
        if token in locations:
            fail('DUPLICATE', 'Duplicate asset locations.')
        locations.append(token)
        if asset['manifest'] != 'manifests/' + node['reference']['sha256'] + '.json':
            fail('SCHEMA', 'Noncanonical manifest path.')
        manifest = load_manifest(_read(root, asset['manifest'], MAX_INDEX_BYTES))
        if (manifest['identity']['id'], manifest['version'], manifest['manifest_sha256']) != token[1:]:
            fail('IDENTITY', 'Package asset reference differs from its manifest.')
        libraries.setdefault(node['library'], {})[token[1:]] = manifest
        manifests.append(manifest); gates.append(_gate(manifest, policy))
        fixture = fixture and manifest['provenance']['fixture']
        expected.add(asset['manifest'])
        for record in manifest['files']:
            path = 'blobs/' + record['sha256']; expected.add(path)
            if index['members'].get(path) != {k: record[k] for k in ('bytes', 'sha256')}:
                fail('INCOMPLETE', 'Required asset file is not in package inventory: ' + record['path'])
    if not isinstance(index['context'], list) or len(index['context']) > 256:
        fail('CONTEXT', 'Invalid context inventory.')
    if len(set(index['context'])) != len(index['context']):
        fail('CONTEXT', 'Duplicate context file.')
    for path in index['context']:
        portable_path(path)
        if not path.startswith('context/'):
            fail('CONTEXT', 'Context path outside its namespace.')
        expected.add(path)
    if index['package_type'] in ('scene', 'project') and not index['context']:
        fail('CONTEXT', 'Scene/project consumer documents are absent.')
    if expected != set(index['members']) or fixture != index['fixture']:
        fail('INVENTORY', 'Index contains unreferenced payloads or altered fixture status.')
    _restorable_identities(manifests)
    graph = DependencyGraph({name: _PackageLibrary(items) for name, items in libraries.items()})
    for lock in locks.values():
        graph.verify_lock(lock)
        if lock['graph']['diagnostics']:
            fail('DEPENDENCY', 'Canonical restoration requires every declared dependency.')
    if set(locations) != {location_key(n) for lock in locks.values() for n in lock['graph']['nodes']}:
        fail('INVENTORY', 'Asset inventory differs from exact dependency closure.')
    if index['package_type'] == 'asset' and (len(locks) != 1 or len(next(iter(locks.values()))['graph']['roots']) != 1):
        fail('TYPE', 'Individual asset package has multiple roots.')
    report = dict(verified=True, completeness='verified', state='verified', package_id=index['package_id'],
                  package_sha256=index['sha256'], fixture=fixture, asset_count=len(manifests),
                  file_count=len(index['members']), bytes=total, gates=gates, scopes=list(locks),
                  technical_readiness='payload-verified-only', production_ready=False,
                  public_distribution_authorized=False, visual_review_performed=False)
    return index, manifests, report


def verify_package(root) -> dict:
    """Strict complete-byte gate; incomplete data raises a structured error."""
    return _verify(root)[2]


def inspect_package(root) -> dict:
    """Read-only diagnostic; never labels a manifest-only package ready."""
    try:
        return verify_package(root)
    except (RigError, OSError, ValueError, KeyError, TypeError) as exc:
        return dict(verified=False, state='incomplete', completeness='incomplete',
                    production_ready=False, public_distribution_authorized=False, error=str(exc))


def restore_package(root, destination) -> dict:
    """Restore into a new isolated canonical library, preserving exact manifests."""
    destination = _new_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.asset-library-restore-', dir=destination.parent))
    try:
        index, manifests, report = _verify(root)
        library = AssetLibrary(stage / 'library', create=True)
        pending = {m['manifest_sha256']: m for m in manifests}; published = set()
        while pending:
            eligible = [m for m in pending.values() if all(d['target']['sha256'] in published
                        for d in dependency_edges(m))]
            if not eligible:
                fail('DEPENDENCY', 'Package cannot be restored in dependency order.')
            for manifest in eligible:
                sources = {}
                for record in manifest['files']:
                    path = stage / 'payloads' / record['sha256']
                    payload = _read(root, 'blobs/' + record['sha256'], record['bytes'])
                    if _record(payload) != {k: record[k] for k in ('bytes', 'sha256')}:
                        fail('HASH', 'Package changed during restoration.')
                    if not path.exists():
                        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(payload)
                    sources[record['path']] = path
                library.register(manifest, sources)
                published.add(manifest['manifest_sha256']); pending.pop(manifest['manifest_sha256'])
        shutil.rmtree(stage / 'payloads')
        # Retain exact original alias resolution/consumer documents as evidence.
        for path in [INDEX, *index['locks'].values(), *index['context']]:
            payload = _read(root, path, MAX_INDEX_BYTES if path == INDEX or path in index['locks'].values() else MAX_FILE_BYTES)
            if path == INDEX and decode_json(payload) != index:
                fail('HASH', 'Package index changed during restoration.')
            if path != INDEX and _record(payload) != index['members'][path]:
                fail('HASH', 'Package context changed during restoration.')
            target = stage / path; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(payload)
        for manifest in manifests:
            ref = dict(id=manifest['identity']['id'], version=manifest['version'], sha256=manifest['manifest_sha256'])
            if library.resolve(ref) != manifest:
                fail('RESTORE', 'Restored version differs.')
        _publish_stage(stage, destination)
        return {**report, 'restored': str(destination), 'library': str(destination / 'library')}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def create_transport(root, destination, *, part_size=65535) -> dict:
    """Reuse the existing ordered, hash-verified source-parts transport."""
    index, _, report = _verify(root)
    result = create_source_bundle(root, sorted(index['members']) + [INDEX], destination,
        fixture=index['fixture'], origin='Exact asset package ' + index['package_id'],
        provenance_paths=[INDEX, *index['locks'].values()], part_size=part_size)
    return {**result, 'package_sha256': report['package_sha256']}


@contextmanager
def _unpack_transport(root):
    # Existing source transport validates order, part/whole hashes and safe paths.
    root = Path(root)
    base = root if root.is_dir() else root.parent
    index_path = root / SOURCE_INDEX if root.is_dir() else root
    index = decode_json(read_source_bytes(index_path, MAX_INDEX_BYTES))
    expected = {SOURCE_INDEX} | {p['path'] for p in index.get('parts', [])}
    if _inventory(base) != expected:
        fail('INCOMPLETE', 'Transport has missing or extra members.')
    with tempfile.TemporaryDirectory(prefix='asset-parts-') as temporary:
        target = Path(temporary) / 'package'
        restore_source_bundle(index_path, target)
        yield target


def verify_transport(root) -> dict:
    with _unpack_transport(root) as package:
        return verify_package(package)


def restore_transport(root, destination) -> dict:
    with _unpack_transport(root) as package:
        return restore_package(package, destination)


def create_archive(root, destination) -> dict:
    """ZIP transport reuses the existing strict generic archive guard."""
    index, _, report = _verify(root)
    destination = _new_destination(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.asset-zip-', dir=destination.parent))
    try:
        archive = stage / 'package.zip'
        receipt = dict(schema_version=2, kind='asset-library-package-transport',
                       files={path: record['sha256'] for path, record in index['members'].items()})
        raw = _read(root, INDEX, MAX_INDEX_BYTES)
        receipt['files'][INDEX] = hashlib.sha256(raw).hexdigest()
        with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_STORED) as output:
            output.writestr(INDEX, raw)
            for path, record in index['members'].items():
                payload = _read(root, path, record['bytes'])
                if _record(payload) != record:
                    fail('HASH', 'Package changed while archiving.')
                output.writestr(path, payload)
            output.writestr('archive-receipt.json', canonical_bytes(receipt))
        if archive.stat().st_size > MAX_FILE_BYTES:
            fail('LIMIT', 'ZIP transport exceeds 64 MiB; use ordered source parts.')
        verify_archive(archive)
        from atomic_publish import _rename_no_replace
        import hatch
        with hatch.ownership(destination, resolve_leaf=False):
            _new_destination(destination); _rename_no_replace(archive, destination)
        return {**report, 'archive': str(destination)}
    finally:
        shutil.rmtree(stage)


@contextmanager
def _unpack_archive(path):
    # Bound source before ZIP metadata parsing; shared guard rejects traversal,
    # duplicate members, symlinks, noncanonical names and inventory mismatch.
    raw = read_source_bytes(path, MAX_FILE_BYTES)
    import io
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if any((e.external_attr >> 16) & 0o170000 not in (0, 0o100000) for e in archive.infolist()):
            fail('PATH', 'Archive special-file members are forbidden.')
        if len(archive.infolist()) > MAX_FILES + 1 or any(e.file_size > MAX_FILE_BYTES for e in archive.infolist()):
            fail('LIMIT', 'Archive member/count exceeds asset package limits.')
        if sum(e.file_size for e in archive.infolist()) > MAX_BYTES + MAX_INDEX_BYTES:
            fail('LIMIT', 'Archive expansion exceeds asset package limit.')
        # Verify the exact same bounded snapshot, never a changed path.
        with tempfile.TemporaryDirectory(prefix='asset-zip-read-') as temporary:
            snapshot = Path(temporary) / 'input.zip'; snapshot.write_bytes(raw)
            receipt = verify_archive(snapshot)
            if receipt.get('kind') != 'asset-library-package-transport':
                fail('TYPE', 'Archive is not an asset package transport.')
            target = Path(temporary) / 'package'; target.mkdir()
            for name in receipt['files']:
                portable_path(name)
                output = target / name; output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(archive.read(name))
            yield target


def verify_package_archive(path) -> dict:
    with _unpack_archive(path) as root:
        return verify_package(root)


def restore_package_archive(path, destination) -> dict:
    with _unpack_archive(path) as root:
        return restore_package(root, destination)
