"""Exact asset version selection and immutable lifecycle records.

AssetLibrary owns files and persistence. Lifecycle events record local
selection. Existing owners retain authority for review, licensing and release.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import re
from typing import Protocol

from asset_library import MAX_VERSIONS
from asset_library_contracts import ID_PATTERN, VERSION_PATTERN, validate
from asset_library_types import AssetRef, AssetVersion
from rig_errors import RigError

MAX_EVENTS = 4096
MAX_CONSUMERS = 1024
_EVENT_FIELDS = {'schema_version', 'operation', 'reference', 'previous', 'reason', 'consumers'}
_SELECTION_OPERATIONS = ('promote', 'rollback')


class LifecycleStore(Protocol):
    def resolve(self, reference: AssetRef) -> AssetVersion: ...
    def inspect(self, reference: AssetRef, *, verify_payloads: bool = True) -> AssetVersion: ...
    def list_references(self, *, limit: int = MAX_VERSIONS) -> list[AssetRef]: ...
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
    if (not isinstance(value, str) or len(value) > 32
            or not re.fullmatch(VERSION_PATTERN, value)):
        fail('SEMVER', 'Expected an exact major.minor.patch version.')
    return tuple(map(int, value.split('.')))


def exact(reference):
    validate(reference, 'AssetRef')
    return deepcopy(reference)


def _payload(manifest):
    return {item['path']: (item['sha256'], item['bytes'], item['role'], item['required'])
            for item in manifest['files']}


def compare_manifests(previous, candidate):
    """Compare exact inventories and declared compatibility for one identity."""
    validate(previous, 'AssetVersion')
    validate(candidate, 'AssetVersion')
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
    if any(file['required'] and (file['path'] not in right or not right[file['path']][3])
           for file in previous['files']):
        breaking.append('required-file-removal')
    required = 'major' if breaking else 'minor' if compatibility else 'patch'
    before, after = semver(previous['version']), semver(candidate['version'])
    bumps = ('major', 'minor', 'patch')
    actual = next((bump for bump, old, new in zip(bumps, before, after) if old != new), None)
    if after < before:
        actual = None
    return dict(files=changed, compatibility=compatibility, breaking=sorted(breaking),
                minimum_bump=required, actual_bump=actual,
                allowed=actual is not None and bumps.index(actual) <= bumps.index(required))


def consumer_compatibility(manifest, consumers):
    """Match explicit required native signatures/capabilities for each consumer.

    Signatures come from native inspectors. Empty declarations cannot satisfy a
    required signature. This admission does not independently inspect geometry.
    """
    if not isinstance(consumers, list) or len(consumers) > MAX_CONSUMERS:
        fail('CONSUMER', 'Consumers must be a bounded list.')
    validate(manifest, 'AssetVersion')
    results = []
    names = set()
    for item in consumers:
        if not isinstance(item, dict) or set(item) != {'id', 'requirements'}:
            fail('CONSUMER', 'Consumer needs only id and explicit requirements.')
        name = identifier(item['id'])
        if name in names:
            fail('CONSUMER', 'Duplicate consumer identity.')
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
            if not okay:
                mismatches.append(key)
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


def _validate_consumer_evidence(consumers, operation):
    if (not isinstance(consumers, list) or len(consumers) > MAX_CONSUMERS
            or (operation == 'deprecate' and consumers)):
        fail('HISTORY', 'Invalid consumer evidence.')
    names = set()
    for item in consumers:
        if (not isinstance(item, dict) or set(item) != {'id', 'compatible', 'mismatches'}
                or item['compatible'] is not True or item['mismatches'] != []):
            fail('HISTORY', 'Invalid consumer evidence.')
        name = identifier(item['id'])
        if name in names:
            fail('HISTORY', 'Duplicate consumer evidence.')
        names.add(name)


def _rollback_target(state):
    return next((item['previous'] for item in reversed(state['history'])
                 if item['operation'] in _SELECTION_OPERATIONS and item['previous'] is not None
                 and item['previous'] != state['promoted'] and item['previous'] not in state['deprecated']), None)


class AssetVersions:
    def __init__(self, library: LifecycleStore):
        self.library = library

    def _stream(self, asset_id):
        return 'asset-version.' + hashlib.sha256(identifier(asset_id).encode('utf-8')).hexdigest()

    def state(self, asset_id):
        """Replay this asset's selection and retirement decisions."""
        asset_id = identifier(asset_id)
        events = _event_stream(self.library, self._stream(asset_id))
        result = dict(revision=0, promoted=None, deprecated=[], history=[])
        checked_references = set()
        for item in events:
            document = item['document']
            if (not isinstance(document, dict) or set(document) != _EVENT_FIELDS
                    or type(document['schema_version']) is not int or document['schema_version'] != 1):
                fail('HISTORY', 'Unknown lifecycle event fields/version.')
            reference = exact(document['reference'])
            operation = document['operation']
            if reference['id'] != asset_id or operation not in (*_SELECTION_OPERATIONS, 'deprecate'):
                fail('HISTORY', 'Lifecycle event has an invalid identity or operation.')
            if document['previous'] != result['promoted']:
                fail('HISTORY', 'Lifecycle event has a stale previous selection.')
            _reason(document['reason'])
            _validate_consumer_evidence(document['consumers'], operation)
            key = (reference['version'], reference['sha256'])
            if key not in checked_references:
                self.library.inspect(reference, verify_payloads=False)
                checked_references.add(key)
            if operation == 'rollback' and reference != _rollback_target(result):
                fail('HISTORY', 'Rollback must restore a retained previous selection.')
            if operation == 'deprecate':
                if reference == result['promoted'] or reference in result['deprecated']:
                    fail('HISTORY', 'Invalid deprecation of current/already deprecated version.')
                result['deprecated'].append(reference)
            else:
                if reference in result['deprecated']:
                    fail('HISTORY', 'Deprecated versions cannot be selected.')
                result['promoted'] = reference
            result['revision'] = item['revision']
            result['history'].append(deepcopy(document))
        return result

    def history(self, asset_id):
        """List registered exact references in numeric semantic-version order."""
        asset_id = identifier(asset_id)
        references = self.library.list_references()
        matching = (exact(ref) for ref in references if ref['id'] == asset_id)
        return sorted(matching, key=lambda ref: semver(ref['version']))

    def compare(self, previous, candidate):
        return compare_manifests(self.library.resolve(exact(previous)), self.library.resolve(exact(candidate)))

    def promotion_plan(self, reference, *, consumers=None):
        """Inspect an exact candidate and report the revision used for selection."""
        reference = exact(reference)
        candidate = self.library.resolve(reference)
        state = self.state(reference['id'])
        if reference in state['deprecated']:
            fail('DEPRECATED', 'Deprecated versions cannot be promoted.')
        comparison = (compare_manifests(self.library.resolve(state['promoted']), candidate)
                      if state['promoted'] else None)
        checked = consumer_compatibility(candidate, [] if consumers is None else consumers)
        return dict(reference=reference, previous=state['promoted'], expected_revision=state['revision'],
                    comparison=comparison, consumers=checked,
                    compatible=all(x['compatible'] for x in checked),
                    affected_consumers=[x['id'] for x in checked],
                    invalidates=['binding', 'preview', 'render'] if state['promoted'] != reference else [],
                    grants_approval=False)

    def promote(self, reference, *, expected_revision, consumers=None, reason='Selected verified local version'):
        plan = self.promotion_plan(reference, consumers=consumers)
        if revision(expected_revision) != plan['expected_revision']:
            fail('CONFLICT', 'Reload stale lifecycle state before promotion.')
        if not plan['compatible']:
            fail('COMPATIBILITY', 'Candidate does not satisfy consumer requirements.')
        return self._append(plan, 'promote', reason)

    def rollback(self, asset_id, *, expected_revision, consumers=None, reason='Restore previous exact selection'):
        state = self.state(asset_id)
        if revision(expected_revision) != state['revision']:
            fail('CONFLICT', 'Reload stale lifecycle state before rollback.')
        prior = _rollback_target(state)
        if prior is None:
            fail('ROLLBACK', 'No retained previous selected version is available.')
        plan = self.promotion_plan(prior, consumers=consumers)
        if plan['expected_revision'] != expected_revision:
            fail('CONFLICT', 'Lifecycle changed while preparing rollback.')
        if not plan['compatible']:
            fail('COMPATIBILITY', 'Previous version no longer satisfies consumer requirements.')
        return self._append(plan, 'rollback', reason)

    def deprecate(self, reference, *, expected_revision, reason):
        reference = exact(reference)
        self.library.resolve(reference)
        state = self.state(reference['id'])
        if revision(expected_revision) != state['revision']:
            fail('CONFLICT', 'Reload stale lifecycle state before deprecation.')
        if state['promoted'] == reference or reference in state['deprecated']:
            fail('DEPRECATED', 'Select a replacement before deprecating the current version.')
        plan = dict(reference=reference, previous=state['promoted'],
                    expected_revision=expected_revision, consumers=[])
        return self._append(plan, 'deprecate', reason)

    def _append(self, plan, operation, reason):
        _reason(reason)
        if plan['expected_revision'] >= MAX_EVENTS:
            fail('LIMIT', 'Lifecycle history has reached its event budget.')
        document = dict(schema_version=1, operation=operation, reference=plan['reference'],
                        previous=plan['previous'], reason=reason, consumers=plan['consumers'])
        return self.library.append_event(
            self._stream(plan['reference']['id']),
            expected_revision=plan['expected_revision'], document=document,
        )


    def collections(self, reference):
        """Replay revision-checked collections for one verified exact version."""
        reference = exact(reference)
        self.library.resolve(reference)
        stream = 'asset-collections.' + reference['sha256']
        current = dict(revision=0, collections=[])
        for item in _event_stream(self.library, stream):
            document = item['document']
            if (not isinstance(document, dict)
                    or set(document) != {'schema_version', 'reference', 'collections'}
                    or type(document['schema_version']) is not int or document['schema_version'] != 1
                    or document['reference'] != reference):
                fail('HISTORY', 'Invalid collection history.')
            current = dict(revision=item['revision'], collections=_collections(document['collections']))
        return current

    def set_collections(self, reference, *, expected_revision, collections):
        reference = exact(reference)
        state = self.collections(reference)
        if revision(expected_revision) != state['revision']:
            fail('CONFLICT', 'Reload stale collection metadata.')
        if state['revision'] >= MAX_EVENTS:
            fail('LIMIT', 'Collection history has reached its event budget.')
        return self.library.append_event(
            'asset-collections.' + reference['sha256'], expected_revision=expected_revision,
            document=dict(schema_version=1, reference=reference, collections=_collections(collections)),
        )


def _collections(value):
    if (not isinstance(value, list) or len(value) > 128
            or any(not isinstance(item, str) for item in value) or len(set(value)) != len(value)):
        fail('COLLECTION', 'Collections must be a unique bounded list.')
    return sorted(identifier(item) for item in value)


def _reason(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        fail('REASON', 'An explicit bounded reason is required.')
