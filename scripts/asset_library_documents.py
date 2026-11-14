"""Thin Director ArtifactRef adapter over the canonical AssetLibrary records.

No logical project validation, selection, activation, or second registry lives here.
The common artifact root is the canonical library root. Returned paths name its
already published immutable blobs, never a separately promoted copy.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile

from asset_library_contracts import MAX_FILE_BYTES, canonical_bytes, portable_path, seal
from rig_errors import RigError, decode_json


def _fail(code, message):
    raise RigError('LIBRARY_DOCUMENT_' + code, message)


def _identity(artifact_id):
    # Director IDs have a broader alphabet than library IDs. This deterministic
    # encoding is a canonical asset identity, not a mutable lookup registry.
    return 'document-' + base64.b32encode(hashlib.sha256(artifact_id.encode()).digest()).decode().lower().rstrip('=')


def _validate_dependencies(dependencies):
    try:
        encoded = canonical_bytes(dependencies)
    except (RecursionError, TypeError, ValueError) as exc:
        raise RigError('LIBRARY_DOCUMENT_REQUEST', 'Malformed dependencies.') from exc
    if len(encoded) > 1024 * 1024:
        _fail('LIMIT', 'Dependency metadata exceeds 1 MiB.')
    for item in dependencies:
        _validate_ref(item)


def _validate_ref(reference):
    from director_schema import definitions
    from jsonschema import Draft202012Validator
    # Bound recursion/serialization before recursive schema validation.
    try:
        encoded = canonical_bytes(reference)
    except (RecursionError, TypeError, ValueError) as exc:
        raise RigError('LIBRARY_DOCUMENT_REQUEST', 'Malformed artifact reference.') from exc
    if len(encoded) > 1024 * 1024:
        _fail('LIMIT', 'Artifact reference exceeds 1 MiB.')
    error = next(Draft202012Validator({'$defs': definitions(), '$ref': '#/$defs/artifact-ref'}).iter_errors(reference), None)
    if error:
        _fail('REQUEST', 'Invalid ArtifactRef: ' + error.message)
    # Reuse Director's exact ArtifactRef traversal authority; schema admission
    # alone does not enforce cross-reference path/identity consistency.
    from director_contracts import artifact_references, MAX_FILES, MAX_TOTAL_BYTES
    from rig_errors import portable_name
    import unicodedata
    paths, identities, aliases = {}, {}, {}
    for item in artifact_references(reference):
        path = portable_name(item['path'])
        if path != unicodedata.normalize('NFC', path):
            _fail('PATH', 'Artifact paths must use Unicode NFC.')
        alias = path.casefold()
        identity = item['artifact_id'], item['semantic_version']
        if ((path in paths and paths[path] != item) or
                (identity in identities and identities[identity] != item)):
            _fail('CONFLICT', 'Artifact closure contains conflicting exact path or identity references.')
        if alias in aliases and aliases[alias] != path:
            _fail('PATH', 'Artifact closure contains case-aliased paths.')
        paths[path], identities[identity], aliases[alias] = item, item, path
    if len(paths) > MAX_FILES or sum(item['byte_size'] for item in paths.values()) > MAX_TOTAL_BYTES:
        _fail('LIMIT', 'Artifact reference closure exceeds Director bounds.')
    return reference


class DocumentPublisher:
    """Publish immutable source bytes with ArtifactRef paths under library.root.

    ``artifact_root`` is the canonical library root or an explicitly configured
    ancestor, including when a caller reopens or relocates it. Native and logical document validation remain with
    their existing owners. Fixture is a boolean and schema_version an integer.
    """
    def __init__(self, library, *, artifact_root=None, native_dependency_resolver=None):
        self.library = library
        if native_dependency_resolver is not None and not callable(native_dependency_resolver):
            _fail('REQUEST', 'Native dependency resolver must be an explicit trusted callable.')
        self.native_dependency_resolver = native_dependency_resolver
        self.artifact_root = Path(artifact_root).absolute() if artifact_root is not None else library.root
        try:
            relative = library.root.relative_to(self.artifact_root).as_posix()
        except ValueError:
            _fail('ROOT', 'Canonical library must be beneath the configured common artifact_root.')
        self.prefix = '' if relative == '.' else portable_path(relative) + '/'
        self._root_identity = self.artifact_root.stat().st_dev, self.artifact_root.stat().st_ino
        if library.storage is None:
            _fail('STORAGE', 'Document publication requires explicit canonical storage upgrade.')
        library.storage.validate_ready()

    def _ref(self, manifest, *, verify_dependencies=True):
        meta = decode_json(self.library.read_file(self._asset_ref(manifest), 'document-metadata.json'))
        if (not isinstance(meta, dict) or set(meta) != {'adapter', 'artifact'} or
                meta['adapter'] != 'asset-library-document-v1'):
            _fail('METADATA', 'Expected the closed canonical document metadata envelope.')
        reference = _validate_ref(meta['artifact'])
        payload = next((file for file in manifest['files'] if file['path'] == 'document.payload'), None)
        if (payload is None or not payload['required'] or manifest['identity']['kind'] != 'document' or
                manifest['identity']['id'] != _identity(reference['artifact_id']) or
                manifest['version'] != reference['semantic_version'] or
                manifest['identity']['subtype'] != reference['kind'] or
                manifest['provenance']['fixture'] != reference['fixture'] or
                payload['sha256'] != reference['sha256'] or payload['bytes'] != reference['byte_size'] or
                payload['mime_type'] != reference['media_type'] or
                reference['path'] != self.prefix + 'records/blobs/' + payload['sha256'] or
                len(manifest['dependencies']) != len(reference['dependencies'])):
            _fail('METADATA', 'Document metadata differs from its canonical identity/source binding.')
        if verify_dependencies:
            for declared, retained in zip(reference['dependencies'], manifest['dependencies']):
                target = retained['target']
                dependency = self.library.resolve(target)
                if dependency['identity']['kind'] == 'document':
                    if self._ref(dependency, verify_dependencies=False) != declared:
                        _fail('METADATA', 'Document dependency differs from its exact canonical publication.')
                elif self._native_dependency(declared, check_alias=False)[0] != target:
                    _fail('METADATA', 'Native dependency differs from its owning canonical version.')
        return reference

    def _check_path_alias(self, reference):
        # The canonical registry is the only inventory. Check again after
        # registration: different IDs have independent identity-history CAS and
        # can race. A conflicting publication remains unselected on failure.
        for target in self.library.list_references():
            manifest = self.library.resolve(target)
            if manifest['identity']['kind'] == 'document':
                payload = next(file for file in manifest['files'] if file['path'] == 'document.payload')
                candidate_path = self.prefix + 'records/blobs/' + payload['sha256']
                # Unrelated publications need no metadata/native capability
                # admission to read this exact immutable source. In particular,
                # an unrelated malformed raw document must not poison it.
                if candidate_path != reference['path']:
                    continue
                other = self._ref(manifest)
                if other != reference:
                    _fail('CONFLICT', 'One canonical source path cannot have conflicting ArtifactRefs.')

    @staticmethod
    def _asset_ref(manifest):
        return dict(id=manifest['identity']['id'], version=manifest['version'], sha256=manifest['manifest_sha256'])

    def _dependency(self, reference):
        # All dependencies must already be backed by this same canonical owner.
        candidates = [r for r in self.library.list_references()
                      if r['id'] == _identity(reference['artifact_id']) and r['version'] == reference['semantic_version']]
        if len(candidates) != 1:
            return self._native_dependency(reference)[0]
        manifest = self.library.resolve(candidates[0])
        if manifest['identity']['kind'] != 'document' or self._ref(manifest) != reference:
            _fail('DEPENDENCY', 'Dependency does not match its exact canonical ArtifactRef.')
        if self.library.read_file(candidates[0], 'document.payload') != self.read_ref(reference):
            _fail('DEPENDENCY', 'Dependency source bytes differ from its exact reference.')
        return candidates[0]

    def _native_dependency(self, reference, *, check_alias=True):
        """The native owner binds full ArtifactRef metadata to an existing AssetRef.

        This explicit capability never admits a free filesystem-only dependency.
        The returned canonical version/closure and an inventoried exact payload
        are independently verified here; schema/identity ownership stays native.
        """
        if self.native_dependency_resolver is None:
            _fail('DEPENDENCY', 'Dependency needs an explicit native owner resolver or a registered document version.')
        from asset_library_contracts import validate
        target = deepcopy(self.native_dependency_resolver(deepcopy(reference)))
        validate(target, 'AssetRef')
        manifest = self.library.resolve(target)
        matches = [record for record in manifest['files']
                   if record['sha256'] == reference['sha256'] and record['bytes'] == reference['byte_size']
                   and record['mime_type'] == reference['media_type']]
        if not matches:
            _fail('DEPENDENCY', 'Native dependency is not an exact inventoried canonical payload.')
        payload = self.library.read_file(target, matches[0]['path'])
        self._verify_path(reference, payload, check_alias=check_alias)
        return target, payload

    def _verify_path(self, reference, payload, *, check_alias=True):
        if check_alias:
            self._check_path_alias(reference)
        try:
            from asset_library import read_source_bytes
            info = self.artifact_root.lstat()
            if (info.st_dev, info.st_ino) != self._root_identity:
                _fail('ROOT', 'Configured artifact root was replaced.')
            if read_source_bytes(self.artifact_root / reference['path']) != payload:
                _fail('HASH', 'Actual canonical source path differs from registry bytes.')
        except OSError as exc:
            raise RigError('LIBRARY_DOCUMENT_PATH', 'Canonical source path is absent or unsafe.') from exc

    def read_ref(self, reference):
        """Read canonical source bytes; this never interprets a caller's path."""
        self.library.storage.validate_ready()
        reference = deepcopy(_validate_ref(reference))
        candidates = [r for r in self.library.list_references()
                      if r['id'] == _identity(reference['artifact_id']) and r['version'] == reference['semantic_version']]
        if len(candidates) != 1:
            return self._native_dependency(reference)[1]
        manifest = self.library.resolve(candidates[0])
        if self._ref(manifest) != reference:
            _fail('HASH', 'ArtifactRef differs from its canonical publication.')
        payload = self.library.read_file(candidates[0], 'document.payload')
        self._verify_path(reference, payload)
        if len(payload) != reference['byte_size'] or hashlib.sha256(payload).hexdigest() != reference['sha256']:
            _fail('HASH', 'Canonical source payload differs from ArtifactRef.')
        return payload

    def publish_document(self, payload, artifact_id, kind, schema_version,
                         semantic_version, dependencies, fixture):
        from asset_library import detect_format
        self.library.storage.validate_ready()
        if type(payload) is not bytes or not 0 < len(payload) <= MAX_FILE_BYTES:
            _fail('LIMIT', 'Document payload must be nonempty bytes, at most 64 MiB; payload is never truncated.')
        if type(dependencies) is not list or len(dependencies) > 128:
            _fail('REQUEST', 'Dependencies must be a list of at most 128 exact ArtifactRefs.')
        # Snapshot caller-owned records before validation/publication.
        _validate_dependencies(dependencies)
        dependencies = deepcopy(dependencies)
        digest = hashlib.sha256(payload).hexdigest()
        fmt, mime = detect_format(payload)
        reference = _validate_ref(dict(artifact_id=artifact_id, kind=kind,
            schema_version=schema_version, semantic_version=semantic_version,
            path=self.prefix + 'records/blobs/' + digest, sha256=digest, byte_size=len(payload),
            media_type=mime, provenance_ref=None, fixture=fixture, dependencies=dependencies))
        self._check_path_alias(reference)
        identity = _identity(artifact_id)
        dependency_refs = [self._dependency(item) for item in dependencies]
        metadata = canonical_bytes(dict(adapter='asset-library-document-v1', artifact=reference))
        if len(metadata) > MAX_FILE_BYTES:
            _fail('LIMIT', 'Mandatory document provenance exceeds the file budget.')
        def file(path, data, role, format, media):
            return dict(path=path, role=role, sha256=hashlib.sha256(data).hexdigest(),
                        bytes=len(data), format=format, mime_type=media, required=True)
        files = [file('document.payload', payload, 'document', fmt, mime),
                 file('document-metadata.json', metadata, 'provenance', 'json', 'application/json')]
        manifest = seal(dict(schema_version=1, kind='asset-library-version',
            identity=dict(id=identity, namespace='documents', library='canonical', slug=identity,
                name=artifact_id, kind='document', subtype=kind, created_at='1970-01-01T00:00:00Z',
                attribution='Caller supplied source document', owner='Local project owner'),
            version=semantic_version, files=files,
            dependencies=[dict(target=r, purpose='Exact document dependency', required=True,
                origin='authored', relationship='direct', capabilities=[]) for r in dependency_refs],
            metadata=dict(description='Immutable source document; native validation belongs to its owner.',
                category='document', author='Local project owner', publisher='Local project owner',
                source_url='local:document-publication', license='not-evaluated', allowed_uses=[],
                keywords=[], style_refs=[], capabilities=[], review_status='unreviewed'),
            compatibility=dict(coordinate_system='legacy-2d', unit_scale=1, rig_signature=[],
                skeleton_signature=[], material_capabilities=[], texture_channels=[], animation_channels=[],
                renderers=[], formats=[fmt], extensions=[], consumer_capabilities=[]), previews=[],
            provenance=dict(method='fixture' if fixture else 'import', origin='Caller supplied immutable document bytes',
                fixture=fixture, sources=[files[0]], provider=[], tools=[], parents=[], edits=[],
                license_evidence=[files[1]], permissions=[], uncertainty=['License and native validity not evaluated']),
            publication=dict(visibility='project-local', distribution='not-evaluated',
                license_evaluation='not-evaluated', inventory=[f['path'] for f in files],
                verification='payload-verified', integration='not-exercised')))
        # Compare-and-swap on all versions of this deterministic canonical identity.
        prior = [r for r in self.library.list_references() if r['id'] == identity]
        existing = next((r for r in prior if r['version'] == semantic_version), None)
        if existing:
            if self.library.resolve(existing) != manifest:
                _fail('CONFLICT', 'Document identity/version already has different bytes or metadata.')
            self.read_ref(reference)
            return deepcopy(reference)
        if self.library.storage is None:
            _fail('STORAGE', 'Document publication requires explicit canonical storage upgrade.')
        try:
            with tempfile.TemporaryDirectory(prefix='asset-document-') as temporary:
                sources = {}
                for record, data in zip(files, (payload, metadata)):
                    path = Path(temporary) / record['path']
                    path.write_bytes(data)
                    sources[record['path']] = path
                self.library.register(manifest, sources, expected_identity_refs=prior)
        except RigError as exc:
            if exc.report['code'] in ('LIBRARY_CONFLICT', 'LIBRARY_IMMUTABLE'):
                # A racing identical publication is idempotent, differing input conflicts.
                current = next((r for r in self.library.list_references()
                    if r['id'] == identity and r['version'] == semantic_version), None)
                if current and self.library.resolve(current) == manifest:
                    self.read_ref(reference)
                    return deepcopy(reference)
                _fail('CONFLICT', 'Document identity changed during publication; reload before retrying.')
            raise
        self.read_ref(reference)
        return deepcopy(reference)
