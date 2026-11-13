"""Exact asset version selection and immutable lifecycle records.

The AssetLibrary owns files and persistence. This module owns only the meaning
of lifecycle events; promotion is a local selection, never a visual, technical,
licensing or public-distribution approval.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re
import tempfile
from pathlib import Path
from typing import Protocol

from asset_library import legacy_roundtrip, read_source_bytes
from asset_library_contracts import canonical_bytes, seal, validate, ID_PATTERN
from rig_errors import RigError, decode_json, object_hash

MAX_EVENTS = 4096
MAX_BINDINGS = 1024


class LifecycleStore(Protocol):
    def resolve(self, reference: dict) -> dict: ...
    def read_file(self, reference: dict, path: str) -> bytes: ...
    def register(self, manifest: dict, sources: dict, *, expected_identity_refs: list[dict] | None = None) -> dict: ...
    def list_references(self, *, limit: int = 4096) -> list[dict]: ...
    def append_event(self, stream: str, *, expected_revision: int, document: dict) -> dict: ...
    def events(self, stream: str) -> list[dict]: ...


def fail(code, message):
    raise RigError('LIBRARY_VERSION_' + code, message)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(ID_PATTERN, value):
        fail('IDENTIFIER', 'Expected a canonical library identifier.')
    return value


def revision(value):
    if type(value) is not int or not 0 <= value < 2**53:
        fail('REVISION', 'Expected a bounded integer revision.')
    return value


def semver(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value) or len(value) > 32:
        fail('SEMVER', 'Expected an exact major.minor.patch version.')
    return tuple(map(int, value.split('.')))


def exact(reference):
    validate(reference, 'AssetRef')
    return deepcopy(reference)


def _payload(manifest):
    return {item['path']: (item['sha256'], item['bytes'], item['role'], item['required'])
            for item in manifest['files']}


def compare_manifests(previous, candidate):
    """Compare exact content and actual declared interfaces, not version labels."""
    validate(previous, 'AssetVersion'); validate(candidate, 'AssetVersion')
    if previous['identity'] != candidate['identity']:
        fail('IDENTITY', 'New versions must preserve the complete stable identity.')
    left, right = _payload(previous), _payload(candidate)
    changed = sorted(path for path in left.keys() | right.keys() if left.get(path) != right.get(path))
    compatibility = {key: {'before': previous['compatibility'][key], 'after': candidate['compatibility'][key]}
                     for key in previous['compatibility']
                     if previous['compatibility'][key] != candidate['compatibility'][key]}
    breaking = []
    for key, difference in compatibility.items():
        if key in ('coordinate_system', 'unit_scale', 'rig_signature', 'skeleton_signature'):
            breaking.append(key)
        elif set(difference['before']) - set(difference['after']):
            breaking.append(key)
    if any(item['required'] and item['path'] not in right for item in previous['files']):
        breaking.append('required-file-removal')
    required = 'major' if breaking else 'minor' if compatibility else 'patch'
    before, after = semver(previous['version']), semver(candidate['version'])
    actual = ('major' if after[0] > before[0] else 'minor' if after[:1] == before[:1] and after[1] > before[1]
              else 'patch' if after[:2] == before[:2] and after[2] > before[2] else None)
    return dict(files=changed, compatibility=compatibility, breaking=sorted(breaking),
                minimum_bump=required, actual_bump=actual,
                allowed=actual is not None and {'patch': 0, 'minor': 1, 'major': 2}[actual] >= {'patch': 0, 'minor': 1, 'major': 2}[required])


def consumer_compatibility(manifest, consumers):
    """Match explicit required native signatures/capabilities for each consumer.

    Signatures come from native inspectors. Empty declarations cannot satisfy a
    required signature. This admission does not independently inspect geometry.
    """
    if not isinstance(consumers, list) or len(consumers) > MAX_BINDINGS:
        fail('CONSUMER', 'Consumers must be a bounded list.')
    validate(manifest, 'AssetVersion')
    results = []
    names = set()
    for item in consumers:
        if not isinstance(item, dict) or set(item) != {'id', 'requirements'}:
            fail('CONSUMER', 'Consumer needs only id and explicit requirements.')
        name = identifier(item['id'])
        if name in names: fail('CONSUMER', 'Duplicate consumer identity.')
        names.add(name)
        requirements = item['requirements']
        if not isinstance(requirements, dict) or set(requirements) - set(manifest['compatibility']):
            fail('CONSUMER', 'Unknown compatibility requirement.')
        mismatches = []
        for key, required in requirements.items():
            actual = manifest['compatibility'][key]
            if isinstance(actual, list):
                if not isinstance(required, list) or any(not isinstance(v, str) for v in required) or len(required) > 128:
                    fail('CONSUMER', 'Required capabilities must be bounded string lists.')
                okay = set(required) <= set(actual)
            else:
                if type(required) is not type(actual) and not (type(required) in (int, float) and type(actual) in (int, float)):
                    fail('CONSUMER', 'Required scalar has the wrong type.')
                okay = required == actual
            if not okay: mismatches.append(key)
        results.append(dict(id=name, compatible=not mismatches, mismatches=mismatches))
    return results


def _event_stream(store, stream):
    events = store.events(stream)
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        fail('HISTORY', 'Metadata history exceeds its bound.')
    for index, item in enumerate(events, 1):
        if (not isinstance(item, dict) or set(item) != {'stream', 'revision', 'document'}
                or item['stream'] != stream or type(item['revision']) is not int
                or item['revision'] != index):
            fail('HISTORY', 'Malformed or noncontiguous metadata history.')
    return events


class AssetVersions:
    def __init__(self, library: LifecycleStore):
        self.library = library

    def _stream(self, asset_id):
        return 'asset-version.' + hashlib.sha256(identifier(asset_id).encode('utf-8')).hexdigest()

    def state(self, asset_id):
        asset_id = identifier(asset_id)
        events = _event_stream(self.library, self._stream(asset_id))
        if not isinstance(events, list) or len(events) > MAX_EVENTS:
            fail('HISTORY', 'Lifecycle history exceeds its bound.')
        result = dict(revision=0, promoted=None, deprecated=[], history=[])
        for item in events:
            if not isinstance(item, dict) or set(item) != {'stream', 'revision', 'document'}:
                fail('HISTORY', 'Malformed lifecycle event envelope.')
            if item['stream'] != self._stream(asset_id) or item['revision'] != result['revision'] + 1:
                fail('HISTORY', 'Lifecycle revisions must be contiguous.')
            document = item['document']
            if not isinstance(document, dict) or set(document) != {'schema_version', 'operation', 'reference', 'previous', 'reason', 'consumers'} or document['schema_version'] != 1:
                fail('HISTORY', 'Unknown lifecycle event fields/version.')
            reference = exact(document['reference'])
            if reference['id'] != asset_id or document['operation'] not in ('promote', 'rollback', 'deprecate'):
                fail('HISTORY', 'Lifecycle event has an invalid identity or operation.')
            if document['previous'] != result['promoted']:
                fail('HISTORY', 'Lifecycle event has a stale previous selection.')
            _reason(document['reason'])
            if not isinstance(document['consumers'], list): fail('HISTORY', 'Invalid consumer evidence.')
            if document['operation'] == 'deprecate':
                if reference == result['promoted'] or reference in result['deprecated']:
                    fail('HISTORY', 'Invalid deprecation of current/already deprecated version.')
                result['deprecated'].append(reference)
            else:
                if reference in result['deprecated']: fail('HISTORY', 'Deprecated versions cannot be selected.')
                result['promoted'] = reference
            result['revision'] = item['revision']; result['history'].append(deepcopy(document))
        return result

    def history(self, asset_id):
        references = self.library.list_references(limit=4096)
        return sorted((exact(ref) for ref in references if ref['id'] == identifier(asset_id)), key=lambda ref: semver(ref['version']))

    def compare(self, previous, candidate):
        return compare_manifests(self.library.resolve(exact(previous)), self.library.resolve(exact(candidate)))

    def create(self, manifest, sources, *, previous=None):
        """Register a new immutable version; originals and prior versions stay live."""
        validate(manifest, 'AssetVersion')
        known = self.history(manifest['identity']['id'])
        if previous is None and known:
            fail('PREVIOUS', 'Existing identities require an explicit previous version.')
        if previous is not None:
            before = self.library.resolve(exact(previous))
            comparison = compare_manifests(before, manifest)
            if not comparison['allowed']: fail('SEMVER', 'Version increment does not cover the changed native interface.')
            if exact(previous) not in manifest['provenance']['parents']:
                fail('PROVENANCE', 'A new version must retain its exact parent reference.')
            if before['metadata']['license'] != manifest['metadata']['license'] or before['metadata']['author'] != manifest['metadata']['author']:
                fail('ATTRIBUTION', 'Changing source attribution/license requires a separate audited rights migration.')
            if not set(manifest['metadata']['allowed_uses']) <= set(before['metadata']['allowed_uses']):
                fail('LICENSE', 'Version creation cannot broaden allowed uses.')
            required_evidence = {f['sha256'] for f in before['provenance']['license_evidence']}
            if not required_evidence <= {f['sha256'] for f in manifest['provenance']['license_evidence']}:
                fail('LICENSE', 'New versions must retain original license evidence.')
        return self.library.register(deepcopy(manifest), sources, expected_identity_refs=known)

    def duplicate(self, reference, *, identity, version='1.0.0'):
        """Copy exact registered bytes to a new identity in this same library.

        Immutable dependency/parent/style references retain their complete
        verified closure in the canonical store. No native IDs inside payloads
        are rewritten, and no selection, preview or approval is inherited.
        """
        reference = exact(reference)
        source = self.library.resolve(reference)
        validate(identity, 'AssetIdentity')
        semver(version)
        if identity['id'] == reference['id']:
            fail('IDENTITY', 'Duplication requires an explicit different asset identity.')
        for field in ('kind', 'subtype', 'attribution'):
            if identity[field] != source['identity'][field]:
                fail('IDENTITY', 'Duplication must preserve source kind, subtype and attribution.')
        candidate = deepcopy(source)
        candidate['identity'] = deepcopy(identity)
        candidate['version'] = version
        candidate['metadata']['review_status'] = 'unreviewed'
        candidate['previews'] = []
        provenance = candidate['provenance']
        if reference not in provenance['parents']:
            provenance['parents'].append(reference)
        provenance['method'] = 'fixture' if provenance['fixture'] else 'derived'
        provenance['edits'].append('Duplicated exact registered payload into a new library identity; native source IDs unchanged.')
        candidate['publication']['integration'] = 'not-exercised'
        candidate['publication']['visibility'] = 'private'
        candidate = seal(candidate)
        # register remains the only publication authority. It verifies the full
        # retained closure and performs expected-identity CAS inside its writer
        # transaction. Staging contains exact bytes and is never a second store.
        with tempfile.TemporaryDirectory(prefix='asset-duplicate-') as directory:
            staging = Path(directory).resolve(strict=True)
            sources = {}
            for index, record in enumerate(source['files']):
                path = staging / str(index)
                path.write_bytes(self.library.read_file(reference, record['path']))
                sources[record['path']] = path
            return self.create(candidate, sources)

    def promotion_plan(self, reference, *, consumers=None):
        reference = exact(reference); candidate = self.library.resolve(reference)
        state = self.state(reference['id'])
        if reference in state['deprecated']: fail('DEPRECATED', 'Deprecated versions cannot be promoted.')
        comparison = self.compare(state['promoted'], reference) if state['promoted'] else None
        checked = consumer_compatibility(candidate, [] if consumers is None else consumers)
        return dict(reference=reference, previous=state['promoted'], expected_revision=state['revision'],
                    comparison=comparison, consumers=checked,
                    compatible=all(x['compatible'] for x in checked),
                    affected_consumers=[x['id'] for x in checked],
                    invalidates=['binding', 'preview', 'render'] if state['promoted'] != reference else [],
                    grants_approval=False)

    def promote(self, reference, *, expected_revision, consumers=None, reason='Selected verified local version'):
        plan = self.promotion_plan(reference, consumers=consumers)
        if revision(expected_revision) != plan['expected_revision']: fail('CONFLICT', 'Reload stale lifecycle state before promotion.')
        if not plan['compatible']: fail('COMPATIBILITY', 'Candidate does not satisfy consumer requirements.')
        return self._append(plan, 'promote', reason)

    def rollback(self, asset_id, *, expected_revision, consumers=None, reason='Restore previous exact selection'):
        state = self.state(asset_id)
        if revision(expected_revision) != state['revision']: fail('CONFLICT', 'Reload stale lifecycle state before rollback.')
        prior = next((item['previous'] for item in reversed(state['history'])
                      if item['operation'] in ('promote', 'rollback') and item['previous'] is not None
                      and item['previous'] != state['promoted'] and item['previous'] not in state['deprecated']), None)
        if prior is None: fail('ROLLBACK', 'No retained previous selected version is available.')
        plan = self.promotion_plan(prior, consumers=consumers)
        if plan['expected_revision'] != expected_revision:
            fail('CONFLICT', 'Lifecycle changed while preparing rollback.')
        if not plan['compatible']: fail('COMPATIBILITY', 'Previous version no longer satisfies consumer requirements.')
        return self._append(plan, 'rollback', reason)

    def deprecate(self, reference, *, expected_revision, reason):
        reference = exact(reference); self.library.resolve(reference)
        state = self.state(reference['id'])
        if revision(expected_revision) != state['revision']: fail('CONFLICT', 'Reload stale lifecycle state before deprecation.')
        if state['promoted'] == reference or reference in state['deprecated']:
            fail('DEPRECATED', 'Select a replacement before deprecating the current version.')
        return self._append(dict(reference=reference, previous=state['promoted'], expected_revision=expected_revision, consumers=[]), 'deprecate', reason)

    def _append(self, plan, operation, reason):
        _reason(reason)
        if plan['expected_revision'] >= MAX_EVENTS:
            fail('LIMIT', 'Lifecycle history has reached its event budget.')
        return self.library.append_event(self._stream(plan['reference']['id']),
            expected_revision=plan['expected_revision'], document=dict(schema_version=1,
                operation=operation, reference=plan['reference'], previous=plan['previous'], reason=reason,
                consumers=plan['consumers']))

    def collections(self, reference):
        reference = exact(reference); self.library.resolve(reference)
        stream = 'asset-collections.' + reference['sha256']
        events = _event_stream(self.library, stream)
        current = dict(revision=0, collections=[])
        for item in events:
            doc = item['document']
            if item['stream'] != stream or item['revision'] != current['revision'] + 1 or not isinstance(doc, dict) or set(doc) != {'schema_version', 'reference', 'collections'} or doc['schema_version'] != 1 or doc['reference'] != reference:
                fail('HISTORY', 'Invalid collection history.')
            current = dict(revision=item['revision'], collections=_collections(doc['collections']))
        return current

    def set_collections(self, reference, *, expected_revision, collections):
        reference = exact(reference); self.library.resolve(reference)
        state = self.collections(reference)
        if revision(expected_revision) != state['revision']: fail('CONFLICT', 'Reload stale collection metadata.')
        if state['revision'] >= MAX_EVENTS: fail('LIMIT', 'Collection history has reached its event budget.')
        return self.library.append_event('asset-collections.' + reference['sha256'], expected_revision=expected_revision,
            document=dict(schema_version=1, reference=reference, collections=_collections(collections)))


def _collections(value):
    if not isinstance(value, list) or len(value) > 128 or any(not isinstance(item, str) for item in value) or len(set(value)) != len(value):
        fail('COLLECTION', 'Collections must be a unique bounded list.')
    return sorted(identifier(item) for item in value)


def _reason(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        fail('REASON', 'An explicit bounded reason is required.')


class ScopedResolver:
    """Ordered explicit roots; persisted bindings contain names and exact refs."""
    def __init__(self, libraries, *, project_library=None):
        if not isinstance(libraries, list) or not 1 <= len(libraries) <= 32:
            fail('SCOPE', 'Configure one to 32 explicitly ordered library roots.')
        self.libraries = {}
        for entry in libraries:
            if not isinstance(entry, (tuple, list)) or len(entry) != 2:
                fail('SCOPE', 'Each library scope requires a name and store.')
            name, library = entry
            name = identifier(name)
            if name in self.libraries: fail('SCOPE', 'Duplicate library scope name.')
            self.libraries[name] = library
        if project_library is not None:
            identifier(project_library)
        if project_library is not None and project_library not in self.libraries:
            fail('SCOPE', 'Project library must be an explicitly configured root.')
        self.project_library = project_library

    def select(self, asset_id, *, reference=None):
        identifier(asset_id)
        if reference is not None and exact(reference)['id'] != asset_id:
            fail('IDENTITY', 'Exact reference has a different asset identity.')
        for name, library in self.libraries.items():
            candidates = AssetVersions(library).history(asset_id)
            if not candidates: continue
            chosen = exact(reference) if reference is not None else AssetVersions(library).state(asset_id)['promoted']
            if chosen is None: fail('UNSELECTED', 'Highest precedence library has no explicit promoted version.')
            # A broken shadowing root fails closed, never falls through to a
            # same-named object in another library.
            library.resolve(chosen)
            return dict(library=name, reference=chosen)
        fail('MISSING', 'Asset is absent from all explicitly configured roots.')

    def load(self, binding):
        if not isinstance(binding, dict) or set(binding) != {'library', 'reference'}:
            fail('BINDING', 'Binding requires an exact reference and library name.')
        library = self.libraries.get(identifier(binding['library']))
        if library is None: fail('SCOPE', 'Pinned library is unavailable; implicit fallback is forbidden.')
        return library.resolve(exact(binding['reference']))

    def bindings(self, project_id):
        if self.project_library is None: fail('SCOPE', 'Configure a project-local store for durable pins.')
        owner = self.libraries[self.project_library]
        stream = 'asset-bindings.' + hashlib.sha256(identifier(project_id).encode('utf-8')).hexdigest()
        state = dict(revision=0, bindings={})
        for item in _event_stream(owner, stream):
            doc = item['document']
            if item['stream'] != stream or item['revision'] != state['revision'] + 1 or not isinstance(doc, dict) or set(doc) != {'schema_version', 'bindings'} or doc['schema_version'] != 1:
                fail('BINDING', 'Invalid project binding history.')
            if not isinstance(doc['bindings'], dict) or len(doc['bindings']) > MAX_BINDINGS: fail('BINDING', 'Invalid project binding collection.')
            for name, binding in doc['bindings'].items():
                identifier(name)
                if not isinstance(binding, dict) or set(binding) != {'library', 'reference'}: fail('BINDING', 'Malformed exact binding.')
                identifier(binding['library']); exact(binding['reference'])
            state = dict(revision=item['revision'], bindings=deepcopy(doc['bindings']))
        return state

    def bind(self, project_id, slot, binding, *, expected_revision):
        identifier(slot); self.load(binding)
        state = self.bindings(project_id)
        if revision(expected_revision) != state['revision']: fail('CONFLICT', 'Project binding snapshot is stale.')
        if state['revision'] >= MAX_EVENTS: fail('LIMIT', 'Binding history has reached its event budget.')
        values = {**state['bindings'], slot: deepcopy(binding)}
        if len(values) > MAX_BINDINGS: fail('LIMIT', 'Project binding count exceeds admission.')
        return self.libraries[self.project_library].append_event('asset-bindings.' + hashlib.sha256(identifier(project_id).encode('utf-8')).hexdigest(),
            expected_revision=expected_revision, document=dict(schema_version=1, bindings=values))


class LegacyMigration:
    """Non-destructive native adapters. Unsupported owners remain explicit.

    Migration retains exact originals in the new immutable library version. It
    never rewrites a source package, and rollback changes only a local selected
    version. Existing native validators continue to own native document meaning.
    """
    def __init__(self, library):
        self.library = library

    def preview(self, manifest, sources, *, native_path):
        validate(manifest, 'AssetVersion')
        kind = manifest['identity']['kind']
        if kind not in ('legacy-pet', 'legacy-cast', 'legacy-clip', 'rig'):
            fail('MIGRATION', 'No verified native migration adapter for this kind.')
        if native_path not in sources: fail('MIGRATION', 'Native source is absent from supplied inventory.')
        payload = read_source_bytes(sources[native_path])
        legacy_roundtrip(kind, payload)
        records = {item['path']: item for item in manifest['files']}
        if set(sources) != set(records): fail('MIGRATION', 'Migration requires the complete exact inventory.')
        source_hashes = {}
        for name, path in sources.items():
            content = read_source_bytes(path)
            digest = hashlib.sha256(content).hexdigest()
            if records[name]['sha256'] != digest or records[name]['bytes'] != len(content):
                fail('MIGRATION', 'Source differs from declared migration bytes.')
            source_hashes[name] = dict(sha256=digest, bytes=len(content))
            if records[name]['format'] == 'json':
                _native_reference_closure(decode_json(content), records)
        if records[native_path] not in manifest['provenance']['sources']:
            fail('MIGRATION', 'Original native source must remain provenance evidence.')
        plan = dict(schema_version=1, native_kind=kind, native_path=native_path,
                    manifest=deepcopy(manifest), sources=source_hashes,
                    source_mutation=False, native_id_preserved=True,
                    readiness='payload-verification-only')
        plan['sha256'] = object_hash(plan)
        return plan

    def apply(self, plan, sources):
        if not isinstance(plan, dict) or set(plan) != {'schema_version', 'native_kind', 'native_path', 'manifest', 'sources', 'source_mutation', 'native_id_preserved', 'readiness', 'sha256'}:
            fail('MIGRATION', 'Unknown migration plan fields.')
        saved = deepcopy(plan); expected = saved.pop('sha256')
        if object_hash(saved) != expected: fail('MIGRATION', 'Migration preview hash changed.')
        current = self.preview(plan['manifest'], sources, native_path=plan['native_path'])
        if current != plan: fail('MIGRATION', 'Migration source or plan changed after preview.')
        # register independently rereads and verifies all originals in its
        # transaction. Missing content cannot become ready through this adapter.
        reference = AssetVersions(self.library).create(plan['manifest'], sources)
        self.library.resolve(reference)
        return dict(reference=reference, native_kind=plan['native_kind'],
                    original_files=plan['sources'], source_mutation=False,
                    rollback='Select a retained previous version; immutable migrated bytes are never deleted.',
                    readiness='payload-verified', production_approved=False)


def _native_reference_closure(document, records):
    """Conservatively require every retained hash-bound native file reference.

    Native documents retain their original identifiers and path spellings. We do
    not rewrite a missing file into a plausible replacement or omit a reference
    because its role is not understood by the migration adapter.
    """
    pending = [document]; visited = 0
    while pending:
        item = pending.pop(); visited += 1
        if visited > 100000: fail('MIGRATION', 'Native reference graph exceeds migration budget.')
        if isinstance(item, dict):
            for path_key, hash_key in [('path', 'sha256'), ('receipt_path', 'receipt_sha256')]:
                if path_key in item and hash_key in item:
                    path = item[path_key]
                    if not isinstance(path, str) or path not in records or records[path]['sha256'] != item[hash_key]:
                        fail('MIGRATION', 'A hash-bound native dependency is absent or mismatched.')
            pending.extend(item.values())
        elif isinstance(item, list): pending.extend(item)
