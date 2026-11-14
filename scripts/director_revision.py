"""Bounded Director revisions over the existing native editor transaction owner.

This module owns interpretation and impact analysis, not project persistence,
material publication, animation evaluation, job claims, or review approvals.
Plans do not execute providers or regenerate frames. Native adapters own edits;
the execution service must separately admit the reported stale work.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import re
import time
from pathlib import Path

from rig_errors import RigError, finite_json

MAX_PATCH_BYTES = 256 * 1024
MAX_INSTRUCTION = 16 * 1024
MAX_AFFECTED_FRAMES = 10000
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z')


def _need(ok, code, message):
    if not ok:
        raise RigError('DIRECTOR_REVISION_' + code, message, stage='director-revision')


def _bytes(value):
    finite_json(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _keys(value, required, optional=()):
    _need(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional),
          'SCHEMA', 'Revision object has unknown or missing fields.')


def _resolve(items, query, kind):
    _need(isinstance(query, str) and 0 < len(query) <= 256, 'TARGET', 'Use one bounded target identifier or exact name.')
    exact = [item for item in items if item['id'] == query]
    matches = exact or [item for item in items if item.get('name', '').casefold() == query.casefold()]
    _need(len(matches) == 1, 'AMBIGUOUS', 'Choose one exact '+kind+' ID; the supplied name is missing or ambiguous.')
    return matches[0]


def _shots(snapshot):
    """Only shots selecting the active exact scene, excluding retained history."""
    scene = snapshot['scene']
    return [(ref['path'], snapshot['documents'][ref['path']]) for ref in snapshot['project']['shots']
            if snapshot['documents'][ref['path']]['scene']['path'] == snapshot['scene_path']
            and snapshot['documents'][ref['path']]['scene']['id'] == scene['id']
            and snapshot['documents'][ref['path']]['scene']['version'] == scene['version']]


def _constraints(snapshot, additional):
    source = snapshot['project'].get('metadata', {}).get('director_locked_constraints', [])
    if isinstance(source, str):
        from rig_errors import decode_json
        _need(len(source.encode('utf-8')) <= 4096, 'POLICY', 'Native locked-constraint metadata exceeds its bound.')
        source = decode_json(source, max_bytes=4096)
    _need(isinstance(source, list) and isinstance(additional, (list, tuple)) and
          len(source) + len(additional) <= 256, 'POLICY', 'Locked constraints must be bounded lists.')
    result = []
    for value in [*source, *additional]:
        _need(isinstance(value, str) and 0 < len(value) <= 1024, 'POLICY', 'Invalid locked constraint.')
        if value.casefold().rstrip('.') == 'keep both robot designs exactly the same':
            value = 'identity:*'
        _need(value == 'world' or re.fullmatch(r'(?:identity|camera|material|duration|ending):(?:\*|[A-Za-z0-9][A-Za-z0-9._:-]{0,127})', value),
              'POLICY', 'Unsupported locked constraint; resolve it through the planning owner before editing.')
        result.append(value)
    return sorted(set(result))


def _instruction(instruction):
    """A deliberate local grammar; broader language belongs to the planning owner."""
    _need(isinstance(instruction, str) and 0 < len(instruction) <= MAX_INSTRUCTION,
          'INSTRUCTION', 'Use a nonempty bounded revision instruction.')
    pattern = r'(?:set|change) camera (\S+) (near|far|vertical_fov|orthographic_height) to ([0-9.eE+-]+)\.?'
    match = re.fullmatch(pattern, instruction.strip(), re.I)
    if match:
        try:
            scalar = float(match[3])
        except ValueError as error:
            raise RigError('DIRECTOR_REVISION_INSTRUCTION', 'Invalid camera scalar.') from error
        return dict(operation='camera', target=match[1], properties={match[2].lower(): scalar})
    match = re.fullmatch(r'(?:set|change) camera (\S+) (translation|rotation) to (.+)', instruction.strip(), re.I)
    if match:
        from rig_errors import decode_json
        return dict(operation='camera-pose', target=match[1], transform={match[2].lower(): decode_json(match[3])})
    match = re.fullmatch(r'(?:extend|lengthen) (?:the )?(.+?) shot by ([0-9]+(?:\.[0-9]+)?) seconds?\.?', instruction.strip(), re.I)
    if match:
        return dict(operation='duration', target=match[1], additional_seconds=match[2])
    match = re.fullmatch(r'(?:set|change) (\S+) material (\S+) (base_color|roughness|metallic|opacity) to (.+)', instruction.strip(), re.I)
    if match:
        from rig_errors import decode_json
        return dict(operation='material', target=match[1], slot=match[2],
                    surface={match[3].lower(): decode_json(match[4])})
    match = re.fullmatch(r'(?:set|change) world exposure to ([0-9.eE+-]+)\.?', instruction.strip(), re.I)
    if match:
        try:
            scalar = float(match[1])
        except ValueError as error:
            raise RigError('DIRECTOR_REVISION_INSTRUCTION', 'Invalid world exposure.') from error
        return dict(operation='world', target='world', properties={'exposure': scalar})
    _need(False, 'AMBIGUOUS', 'This instruction needs an explicit typed revision from the planning owner; no target, amount, identity change, or high-impact ending is guessed.')


def _prepare(snapshot, change):
    _need(isinstance(change, dict), 'SCHEMA', 'A typed change must be an object.')
    op = change.get('operation')
    if op in ('camera', 'camera-pose', 'material'):
        required = ({'operation', 'target', 'properties'} if op == 'camera' else
                    {'operation', 'target', 'transform'} if op == 'camera-pose' else
                    {'operation', 'target', 'slot', 'surface'})
        _keys(change, required)
        node = _resolve(snapshot['scene']['nodes'], change['target'], 'node')
        from scene3d_graph import SceneGraph
        graph = SceneGraph.from_bundle(snapshot, snapshot['scene_path'])
        _need(not graph.evaluate_base(node['id'])['locked'], 'LOCKED', 'The native node or its collection is locked.')
        if op == 'camera-pose':
            _need(node['type'] == 'camera' and isinstance(change['transform'], dict) and
                  bool(change['transform']) and not set(change['transform'])-{'translation', 'rotation'},
                  'TARGET', 'Static camera pose revisions accept explicit local translation or XYZW rotation.')
            _need(not node['constraints'] and 'attachment' not in node and 'path_binding' not in node and
                  not node['animation_bindings'], 'UNSUPPORTED', 'Use the native camera/timeline owner for a driven camera pose.')
            for _, shot in _shots(snapshot):
                if shot['camera'] != node['id']: continue
                _need(not any(track['target'] == node['id'] and track['property'] in ('translation', 'rotation', 'scale') for track in shot['tracks'])
                      and not any(binding['target'] == node['id'] for binding in shot['bindings'])
                      and not shot['camera_overrides'].get('transform'),
                      'UNSUPPORTED', 'The selected shot overrides this camera pose; revise its native timeline explicitly.')
            transform = {**deepcopy(node['transform']), **deepcopy(change['transform'])}
            graph.set_transform(node['id'], transform)
            return {**deepcopy(change), 'target': node['id'],
                    'transform': {key: deepcopy(transform[key]) for key in change['transform']}}
        if op == 'camera':
            _need(node['type'] == 'camera' and isinstance(change['properties'], dict) and bool(change['properties']),
                  'TARGET', 'Camera revisions require a camera node and explicit properties.')
            camera = {**deepcopy(node['camera']), **deepcopy(change['properties'])}
            graph.configure_node(node['id'], {'camera': camera})
            return {**deepcopy(change), 'target': node['id'], 'properties': camera}
        else:
            from editor3d_inspectors import material_slot_projection
            _, _, slots = material_slot_projection(snapshot, node['id'])
            _need(change['slot'] in {slot['slot'] for slot in slots}, 'TARGET', 'Choose a canonical material slot belonging to the selected geometry.')
            _need(isinstance(change['surface'], dict) and bool(change['surface']), 'SCHEMA', 'Material changes require explicit surface properties.')
            # The actual native material adapter validates the complete resource;
            # validate scalar values here without creating or publishing one.
            permitted = {'base_color', 'roughness', 'metallic', 'opacity', 'alpha_mode', 'alpha_cutoff', 'double_sided'}
            _need(not set(change['surface']) - permitted, 'UNSUPPORTED', 'Use supported scalar native PBR fields.')
            for name, value in change['surface'].items():
                if name == 'base_color':
                    _need(isinstance(value, list) and len(value) == 4 and
                          all(type(v) in (int, float) and 0 <= v <= 1 for v in value), 'SCHEMA', 'Use four bounded linear RGB/alpha values.')
                elif name == 'alpha_mode':
                    _need(value in ('OPAQUE', 'MASK', 'BLEND'), 'SCHEMA', 'Invalid native alpha mode.')
                elif name == 'double_sided':
                    _need(type(value) is bool, 'SCHEMA', 'Double sided must be an explicit boolean.')
                else:
                    _need(type(value) in (int, float) and 0 <= value <= 1, 'SCHEMA', 'PBR scalar is outside its native bound.')
        return {**deepcopy(change), 'target': node['id']}
    if op in ('duration', 'ending'):
        _keys(change, {'operation', 'target', 'additional_seconds'} if op == 'duration' else
              {'operation', 'target', 'light', 'frame', 'intensity'})
        selected = _resolve([dict(shot, path=path) for path, shot in _shots(snapshot)], change['target'], 'active shot')
        if op == 'duration':
            seconds = change['additional_seconds']
            _need((type(seconds) is str and 0 < len(seconds) <= 128) or
                  (type(seconds) in (int, float) and 0 < seconds <= 1e9),
                  'TIME', 'Additional seconds must be one bounded exact decimal or rational.')
            if type(seconds) is str:
                _need(re.fullmatch(r'(?:[0-9]{1,10}(?:\.[0-9]{1,9})?|[0-9]{1,18}/[1-9][0-9]{0,9})', seconds),
                      'TIME', 'Use a bounded decimal or integer ratio, without an unbounded exponent.')
            try:
                frames = Fraction(str(seconds)) * Fraction(selected['fps']['numerator'], selected['fps']['denominator'])
            except (ValueError, ZeroDivisionError) as error:
                raise RigError('DIRECTOR_REVISION_TIME', 'Invalid exact duration.') from error
            _need(frames > 0 and frames.denominator == 1, 'TIME', 'Duration must add a positive whole number of frames at the exact shot rate.')
            end = selected['duration_frames'] + int(frames)
            _need(end <= snapshot['project']['budgets']['max_frames'], 'BUDGET', 'Extended duration exceeds the native frame budget.')
            return dict(operation=op, target=selected['id'], document=selected['path'],
                        duration_frames=end, additional_frames=int(frames))
        light = _resolve(snapshot['scene']['nodes'], change['light'], 'light')
        _need(light['type'] == 'light' and 'light' in light, 'UNSUPPORTED', 'Ending changes currently require an existing inline native light; bound lighting is delegated to its LookDev owner.')
        from scene3d_graph import SceneGraph
        _need(not SceneGraph.from_bundle(snapshot, snapshot['scene_path']).evaluate_base(light['id'])['locked'],
              'LOCKED', 'The ending light or its collection is locked.')
        _need(type(change['frame']) is int and 0 <= change['frame'] < selected['duration_frames'],
              'TIME', 'Ending frame must lie inside the exact half-open shot range.')
        _need(type(change['intensity']) in (int, float) and 0 <= change['intensity'] <= 1e6,
              'SCHEMA', 'Use a bounded native light intensity.')
        return {**deepcopy(change), 'target': selected['id'], 'light': light['id'], 'document': selected['path']}
    if op == 'world':
        _keys(change, {'operation', 'target', 'properties'})
        _need(change['target'] == 'world' and isinstance(change['properties'], dict), 'TARGET', 'World changes target the native world.')
        from scene3d_graph import SceneGraph
        graph = SceneGraph.from_bundle(snapshot, snapshot['scene_path'])
        graph.configure_world(deepcopy(change['properties']))
        return deepcopy(change)
    _need(False, 'UNSUPPORTED', 'No native revision adapter is registered for this change; original geometry and identity are preserved.')


class RevisionService:
    """Trusted in-process revision compiler over one existing EditorRepository.

    ``authorizer`` is an application-installed authority resolver, never a
    callable or approval boolean deserialized from an HTTP request. It must
    verify the exact patch and immutable authorization evidence it receives.
    """
    def __init__(self, repository, *, authorizer=None, max_affected_frames=MAX_AFFECTED_FRAMES,
                 locked_constraints=(), brief_reference=None, artifact_root=None):
        _need(type(max_affected_frames) is int and 1 <= max_affected_frames <= MAX_AFFECTED_FRAMES,
              'BUDGET', 'Invalid revision analysis frame budget.')
        _need(authorizer is None or callable(authorizer), 'POLICY', 'Install a trusted authority resolver.')
        self.repository = repository
        self.authorizer = authorizer
        self.max_affected_frames = max_affected_frames
        _need(isinstance(locked_constraints, (list, tuple)), 'POLICY', 'Install original constraints through a trusted adapter.')
        self.locked_constraints = deepcopy(list(locked_constraints))
        _need((brief_reference is None) == (artifact_root is None), 'POLICY', 'A pinned original brief needs its configured artifact root.')
        self.brief_reference = deepcopy(brief_reference)
        self.artifact_root = artifact_root

    def _original_locks(self, snapshot):
        locks = list(self.locked_constraints)
        if self.brief_reference is not None:
            import director_contracts as contracts
            from rig_errors import decode_json
            from jsonschema import Draft202012Validator
            schema = contracts.schemas()['director.schema.json']
            error = next(Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/artifact-ref'}).iter_errors(self.brief_reference), None)
            _need(error is None and self.brief_reference['byte_size'] <= 16*1024*1024,
                  'POLICY', 'Use an exact bounded original brief ArtifactRef.')
            brief = contracts.validate_document(decode_json(contracts._read_artifact(self.artifact_root, self.brief_reference)))
            project_id = snapshot['project'].get('metadata', {}).get('director_project_id', snapshot['project']['id'])
            _need(brief['kind'] == 'director-creative-brief' and brief['project_id'] == project_id and
                  (brief['id'], brief['semantic_version'], brief['schema_version'], brief['kind']) ==
                  (self.brief_reference['artifact_id'], self.brief_reference['semantic_version'],
                   self.brief_reference['schema_version'], self.brief_reference['kind']),
                  'POLICY', 'Original brief identity or native project mapping differs.')
            locks.extend(brief['requirements']['locked'])
        return _constraints(snapshot, locks)

    def plan(self, instruction, *, expected_revision, expected_sha256, change=None, locked_constraints=()):
        snapshot = self.repository.snapshot()
        _need(snapshot['revision'] == expected_revision and snapshot['sha256'] == expected_sha256,
              'STALE', 'Revision planning requires the current exact native snapshot.')
        _need(isinstance(instruction, str) and 0 < len(instruction) <= MAX_INSTRUCTION,
              'INSTRUCTION', 'Retain a bounded nonempty original revision instruction.')
        _need(isinstance(locked_constraints, (list, tuple)) and len(locked_constraints) <= 256,
              'POLICY', 'Locked constraints must be a bounded list.')
        prepared = _prepare(snapshot, deepcopy(change) if change is not None else _instruction(instruction))
        locks = _constraints(snapshot, [*self._original_locks(snapshot), *locked_constraints])
        patch = dict(schema_version=1, kind='director-revision-patch', instruction=instruction,
                     parent=dict(revision=snapshot['revision'], sha256=snapshot['sha256'],
                                 project_sha256=snapshot['inventory']['project.json']['sha256'],
                                 scene_path=snapshot['scene_path']),
                     change=prepared, locked_constraints=locks)
        patch['sha256'] = _hash(patch)
        impact = self.impact(patch)
        return dict(patch=patch, impact=impact, assumptions=[],
                    summary=prepared['operation']+' revision of '+prepared['target'],
                    execution='planned-only', visual_review='not-performed')

    def _snapshot_for(self, patch):
        _keys(patch, {'schema_version', 'kind', 'instruction', 'parent', 'change', 'locked_constraints', 'sha256'})
        _need(len(_bytes(patch)) <= MAX_PATCH_BYTES and type(patch['schema_version']) is int and
              patch['schema_version'] == 1 and patch['kind'] == 'director-revision-patch', 'SCHEMA', 'Invalid bounded revision patch.')
        _need(patch['sha256'] == _hash({k: v for k, v in patch.items() if k != 'sha256'}),
              'HASH', 'Revision patch bytes changed.')
        _keys(patch['parent'], {'revision', 'sha256', 'project_sha256', 'scene_path'}, {'artifact'})
        parent = patch['parent']
        snapshot = self.repository.snapshot(parent['revision'])
        _need(snapshot['sha256'] == parent['sha256'] and snapshot['scene_path'] == parent['scene_path'] and
              snapshot['inventory']['project.json']['sha256'] == parent['project_sha256'],
              'STALE', 'Patch parent no longer identifies the exact retained native revision.')
        _constraints(snapshot, patch['locked_constraints'])
        _need(set(self._original_locks(snapshot)) <= set(patch['locked_constraints']),
              'POLICY', 'Patch omitted authoritative original project constraints.')
        change = patch['change']
        if isinstance(change, dict) and change.get('operation') == 'duration':
            _keys(change, {'operation', 'target', 'document', 'duration_frames', 'additional_frames'})
            _need(type(change['duration_frames']) is int and type(change['additional_frames']) is int and
                  0 < change['additional_frames'] <= 1_000_000, 'TIME', 'Invalid normalized duration change.')
            selected = next((shot for path, shot in _shots(snapshot) if path == change['document']), None)
            _need(selected is not None, 'TARGET', 'Duration change must select an active exact shot.')
            seconds = Fraction(change['additional_frames'] * selected['fps']['denominator'], selected['fps']['numerator'])
            raw = dict(operation='duration', target=change['target'], additional_seconds=str(seconds))
        elif isinstance(change, dict) and change.get('operation') == 'ending':
            _keys(change, {'operation', 'target', 'light', 'frame', 'intensity', 'document'})
            raw = {key: value for key, value in change.items() if key != 'document'}
        else:
            raw = change
        _need(_prepare(snapshot, raw) == change, 'SCHEMA', 'Patch differs from its validated exact native change.')
        return snapshot

    def impact(self, patch):
        snapshot = self._snapshot_for(patch)
        change = patch['change']; op = change.get('operation')
        active = _shots(snapshot)
        if op in ('camera', 'camera-pose'):
            affected = [(path, shot) for path, shot in active if shot['camera'] == change['target']]
        elif op in ('duration', 'ending'):
            affected = [(path, shot) for path, shot in active if path == change.get('document')]
        elif op in ('material', 'world'):
            affected = active
        else:
            _need(False, 'UNSUPPORTED', 'Unknown revision operation.')
        _need(bool(affected), 'TARGET', 'The revision has no active exact shot dependency.')
        ranges = []
        for path, shot in affected:
            start = change['frame'] if op == 'ending' else 0
            end = change['duration_frames'] if op == 'duration' else shot['duration_frames']
            ranges.append(dict(path=path, id=shot['id'], start_frame=start, end_frame=end,
                               fps=deepcopy(shot['fps'])))
        frames = sum(item['end_frame'] - item['start_frame'] for item in ranges)
        _need(frames <= self.max_affected_frames, 'BUDGET', 'Revision exceeds the bounded affected-frame budget.')
        protected = []
        target_node = next((n for n in snapshot['scene']['nodes'] if n['id'] == change['target']), None)
        identity_ids = {change['target']}
        is_character = False
        if target_node is not None:
            asset_id = target_node.get('asset', {}).get('id')
            identity_ids.add(asset_id)
            is_character = (target_node['type'] == 'skinned_character' or
                            any(ref['id'] == asset_id for ref in snapshot['project']['characters']))
        for lock in patch['locked_constraints']:
            family, _, target = lock.partition(':')
            identity_change = op == 'material'
            applies = ((family == 'identity' and identity_change and
                        (target in identity_ids or target == '*' and is_character)) or
                       (family == ('camera' if op == 'camera-pose' else op) and
                        (not target or target in ('*', change['target']))))
            if applies: protected.append(lock)
        changed_ids = [change['target']]
        if op == 'ending': changed_ids = sorted(set(changed_ids + [change['light']]))
        affected_paths = {path for path, _ in affected}
        stale = []
        preserved = []
        for path, doc in snapshot['documents'].items():
            if doc.get('kind') == 'scene3d-render' and doc['shot']['path'] in affected_paths:
                stale.append(path)
            elif doc.get('kind') == 'scene3d-asset':
                preserved.append(path)
        return dict(changed_ids=changed_ids, affected_shots=ranges,
                    stale_documents=sorted(affected_paths | set(stale)),
                    preserved_asset_documents=sorted(preserved),
                    stale_operations=['render-affected-frames', 'review-affected-shots', 'assemble-video'],
                    timeline_reflow=(op == 'duration'),
                    requires_authorization=protected,
                    estimate=dict(render_frames=frames, provider_calls=0, cpu_seconds=None,
                                  gpu_seconds=None, amount=None, classification='partial-estimate'),
                    regeneration='pending-native-execution-admission', visual_review='not-performed')

    def apply(self, patch, *, transaction_id, authorization=None):
        snapshot = self._snapshot_for(patch)
        impact = self.impact(patch)
        _need(isinstance(transaction_id, str) and _ID.fullmatch(transaction_id), 'TRANSACTION', 'Use a bounded transaction ID.')
        if impact['requires_authorization']:
            _need(self.authorizer is not None and self.authorizer(deepcopy(patch), authorization) is True,
                  'AUTHORIZATION', 'This exact revision changes locked requirements and needs applicable verified authority.')
        change = patch['change']; op = change['operation']; parent = patch['parent']
        if op in ('camera', 'camera-pose', 'world'):
            parameters = dict(properties=deepcopy(change['properties'])) if op != 'camera-pose' else {}
            operation = 'configure-world'
            if op == 'camera':
                operation = 'configure-node'; parameters = dict(identifier=change['target'],
                    properties={'camera': deepcopy(change['properties'])})
            elif op == 'camera-pose':
                node = next(node for node in snapshot['scene']['nodes'] if node['id'] == change['target'])
                operation = 'transform'; parameters = dict(identifier=change['target'],
                    transform={**deepcopy(node['transform']), **deepcopy(change['transform'])}, space='local')
            after = self.repository.apply(parent['revision'], dict(schema_version=1,
                commands=[dict(operation=operation, parameters=parameters)]), transaction_id,
                expected_sha256=parent['sha256'], label='Director '+op+' revision')
        elif op == 'material':
            from editor3d_inspectors import apply_material_edit, assign_authored_material, material_slot_projection
            node, _, slots = material_slot_projection(snapshot, change['target'])
            selected = next(slot for slot in slots if slot['slot'] == change['slot'])
            request = dict(node_id=change['target'], slot=change['slot'], surface=deepcopy(change['surface']),
                expected_revision=parent['revision'], expected_sha256=parent['sha256'], transaction_id=transaction_id)
            adapter = apply_material_edit if selected.get('override') is not None else assign_authored_material
            after = adapter(self.repository, request)
        else:
            try:
                from editor3d_timeline import apply_timeline
            except ImportError as error:
                raise RigError('DIRECTOR_REVISION_DEPENDENCY', 'Install the canonical native timeline adapter before duration or ending edits.', stage='director-revision') from error
            command = (dict(operation='shot-settings', document=change['document'],
                            duration_frames=change['duration_frames']) if op == 'duration' else
                       dict(operation='key-upsert', document=change['document'], target=change['light'],
                            property='light_intensity', frame=change['frame'], value=change['intensity'], interpolation='step'))
            after = apply_timeline(self.repository, dict(expected_revision=parent['revision'],
                expected_sha256=parent['sha256'], transaction_id=transaction_id, commands=[command]))
        reopened = self.repository.snapshot(after['revision'])
        _need(reopened['sha256'] == after['sha256'], 'VERIFY', 'Committed native revision failed reopening.')
        return dict(schema_version=1, kind='director-revision-receipt', patch_sha256=patch['sha256'],
                    instruction=patch['instruction'], before=deepcopy(parent),
                    after=dict(revision=after['revision'], sha256=after['sha256'],
                               project_sha256=after['inventory']['project.json']['sha256'],
                               scene_path=after['scene_path']),
                    impact=impact, comparison=self.compare(parent['revision'], after['revision']),
                    regeneration='pending-native-execution-admission', visual_review='not-performed')

    def compare(self, before_revision, after_revision):
        before, after = self.repository.snapshot(before_revision), self.repository.snapshot(after_revision)
        a, b = before['inventory'], after['inventory']
        return dict(before=before['sha256'], after=after['sha256'],
                    changed_files=sorted(path for path in set(a) & set(b) if a[path] != b[path]),
                    added_files=sorted(set(b) - set(a)), removed_files=sorted(set(a) - set(b)),
                    preserved_files=sorted(path for path in set(a) & set(b) if a[path] == b[path]))

    def undo(self, expected_revision, transaction_id, *, expected_sha256):
        return self.repository.undo(expected_revision, transaction_id, expected_sha256=expected_sha256)

    def redo(self, expected_revision, transaction_id, *, expected_sha256):
        return self.repository.redo(expected_revision, transaction_id, expected_sha256=expected_sha256)

    def artifact(self, snapshot, path, *, artifact_root):
        """Reference the existing immutable native closure, without copying it.

        The configured artifact root must contain this repository. Relative
        native resource paths therefore keep the exact owner-provided scope.
        No bare project manifest is republished without its native dependencies.
        """
        _need(path in snapshot['inventory'], 'REFERENCE', 'Artifact is absent from the exact native inventory.')
        native = self.repository.native_snapshot(snapshot['revision'], snapshot['sha256'])
        root = Path(artifact_root).resolve(strict=True)
        try:
            relative = (Path(native['root'])/path).relative_to(root).as_posix()
        except ValueError as error:
            raise RigError('DIRECTOR_REVISION_REFERENCE', 'Artifact root must contain the immutable native repository.') from error
        doc = snapshot['documents'].get(path)
        kind = doc['kind'] if doc else 'native-evidence'
        identifier = doc['id'] if doc else 'file-'+hashlib.sha256(path.encode()).hexdigest()[:32]
        version = doc['version'] if doc else '1.0.0'
        record = snapshot['inventory'][path]
        reference = dict(artifact_id=identifier, kind=kind, schema_version=doc['schema_version'] if doc else 1,
                         semantic_version=version, path=relative, sha256=record['sha256'],
                         byte_size=record['bytes'], media_type='application/json' if path.endswith('.json') else 'application/octet-stream',
                         provenance_ref=None, fixture=bool(snapshot['project']['provenance']['fixture']), dependencies=[])
        from director_contracts import _read_artifact
        _read_artifact(root, reference)
        return reference

    def execute(self, request, *, artifact_root, publish_artifact=None):
        """Canonical method-envelope adapter using the configured asset owner.

        ``publish_artifact`` is a trusted application adapter, not a new store:
        it must register bytes through the canonical immutable asset authority
        and return its real ArtifactRef under artifact_root. Its output is read
        back and hash-verified. Without that integration, planning fails before
        any publication or native project change.
        """
        import director_contracts as contracts
        from director_capabilities import _date
        from datetime import datetime, timezone
        from rig_errors import decode_json, object_hash
        method = request.get('method') if isinstance(request, dict) else None
        _need(method in ('RevisionService.plan', 'RevisionService.impact', 'RevisionService.apply'),
              'METHOD', 'Use a canonical RevisionService method.')
        contracts.validate_method_request(method, request)
        _need(_date(request['context']['deadline']) > datetime.now(timezone.utc), 'DEADLINE', 'Revision operation deadline expired.')
        data = request['input']; started = time.process_time()

        def read(reference, maximum=16*1024*1024):
            _need(reference['byte_size'] <= maximum, 'LIMIT', 'Revision input exceeds its bounded document profile.')
            raw = contracts._read_artifact(artifact_root, reference)
            return decode_json(raw, max_bytes=maximum)

        def context(snapshot, source):
            project_id = snapshot['project'].get('metadata', {}).get('director_project_id', snapshot['project']['id'])
            _need(request['context']['project_id'] == project_id and
                  request['context']['project_revision_sha256'] == source['sha256'],
                  'CONTEXT', 'Operation context must identify this exact native project revision.')

        if method == 'RevisionService.plan':
            _need(callable(publish_artifact), 'DEPENDENCY', 'Configure the canonical immutable asset publisher before planning.')
            snapshot = self.repository.snapshot()
            source = self.artifact(snapshot, 'project.json', artifact_root=artifact_root)
            context(snapshot, source)
            _need(data['current_project']['sha256'] == source['sha256'] and
                  read(data['current_project']) == snapshot['project'], 'STALE', 'Planning source is not the current exact native project.')
            # These are pinned planning inputs, not evidence that downstream
            # regeneration is admitted or that a provider has executed.
            read(data['budget']); read(data['capabilities'])
            planned = self.plan(data['instruction'], expected_revision=snapshot['revision'],
                                expected_sha256=snapshot['sha256'], locked_constraints=data['locked_constraints'])
            patch = planned['patch']; patch['parent']['artifact'] = deepcopy(data['current_project'])
            patch['sha256'] = _hash({key: value for key, value in patch.items() if key != 'sha256'})
            payload = _bytes(patch)
            dependencies = [data['current_project'], data['budget'], data['capabilities']]
            if self.brief_reference is not None:
                dependencies.append(deepcopy(self.brief_reference))
            reference = publish_artifact(payload, kind='director-revision-patch', semantic_version='1.0.0',
                                         dependencies=deepcopy(dependencies), fixture=source['fixture'])
            from jsonschema import Draft202012Validator
            schema = contracts.schemas()['director.schema.json']
            error = next(Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/artifact-ref'}).iter_errors(reference), None)
            _need(error is None, 'PUBLICATION', 'The canonical publisher returned an invalid ArtifactRef.')
            _need(reference['kind'] == 'director-revision-patch' and reference['dependencies'] == dependencies and
                  contracts._read_artifact(artifact_root, reference) == payload,
                  'PUBLICATION', 'The canonical publisher did not preserve exact patch bytes and dependencies.')
            value = dict(patch=reference, changed_ids=planned['impact']['changed_ids'], assumptions=[], diagnostics=[])
            artifacts = [reference]
        else:
            _need(data['patch']['kind'] == 'director-revision-patch', 'REFERENCE', 'Use a typed revision patch artifact.')
            patch = read(data['patch'], MAX_PATCH_BYTES)
            snapshot = self._snapshot_for(patch)
            source = self.artifact(snapshot, 'project.json', artifact_root=artifact_root)
            context(snapshot, source)
            impact = self.impact(patch)
            _need(len(impact['stale_documents']) <= 128 and len(impact['preserved_asset_documents']) <= 128,
                  'LIMIT', 'Partition this revision to fit the canonical artifact result limits.')
            stale = [self.artifact(snapshot, path, artifact_root=artifact_root) for path in impact['stale_documents']]
            preserved = [self.artifact(snapshot, path, artifact_root=artifact_root) for path in impact['preserved_asset_documents']]
            if method == 'RevisionService.impact':
                graph = read(data['graph'])
                _need(isinstance(graph, dict), 'SCHEMA', 'Execution graph must be a JSON object.')
                if graph.get('kind') == 'director-result':
                    contracts.validate_method_result('GraphCompiler.compile', graph)
                    graph = graph['value']
                from director_graph import impact as graph_impact
                from jsonschema import Draft202012Validator
                schema = contracts.schemas()['director.schema.json']
                error = next(Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/output-execution-graph'}).iter_errors(graph), None)
                _need(error is None, 'SCHEMA', 'Use a canonical validated execution graph.')
                shot_hashes = {snapshot['inventory'][path]['sha256'] for path in impact['stale_documents']}
                all_shot_hashes = {snapshot['inventory'][path]['sha256'] for path, shot in _shots(snapshot)}
                scene_hash = snapshot['inventory'][snapshot['scene_path']]['sha256']
                seeds = []
                for node in graph['nodes']:
                    hashes = {ref['sha256'] for ref in node['inputs']}
                    if hashes & shot_hashes or scene_hash in hashes and not hashes & all_shot_hashes:
                        seeds.append(node['id'])
                propagated = graph_impact(graph, graph, uncertain=seeds)
                changed_ids = sorted(set(impact['changed_ids']) | set(propagated['stale']))
                _need(len(changed_ids) <= 128, 'LIMIT', 'Affected graph identifiers exceed canonical result bounds.')
                value = dict(changed_ids=changed_ids, stale_descendants=stale, preserved_outputs=preserved, diagnostics=[])
                artifacts = []
            else:
                _need(data['expected_revision'] == source['sha256'], 'STALE', 'Apply expected project hash differs from the exact patch parent.')
                authorization = read(data['authorization'], MAX_PATCH_BYTES)
                _need(_date(request['context']['deadline']) > datetime.now(timezone.utc), 'DEADLINE', 'Revision deadline expired before commit.')
                receipt = self.apply(patch, transaction_id=request['context']['idempotency_key'],
                                     authorization=dict(reference=data['authorization'], document=authorization))
                after = self.repository.snapshot(receipt['after']['revision'])
                project = self.artifact(after, 'project.json', artifact_root=artifact_root)
                value = dict(project=project, parent=source, changed_ids=impact['changed_ids'],
                             stale_descendants=stale, preserved_outputs=preserved, diagnostics=[])
                artifacts = [project]
        measurement = lambda value, explanation: dict(value=value, explanation=explanation)
        usage = dict(classification='actual', provider_calls=measurement(0, 'No provider is invoked by this adapter.'),
                     input_tokens=measurement(0, 'No planning model call.'), output_tokens=measurement(0, 'No planning model call.'),
                     cpu_seconds=measurement(time.process_time()-started, 'Current process CPU time; no render workers are started.'),
                     gpu_seconds=measurement(0, 'No GPU work.'), storage_bytes=measurement(None, 'Filesystem allocation not measured.'),
                     currency=None, amount=measurement(None, 'No monetary cost measurement.'), measurement_source='director-revision-current-process')
        result = dict(schema_version=1, kind='director-result', method=method, status='succeeded',
                      operation_id=request['context']['request_id'], input_digest=object_hash(request), value=value,
                      artifacts=artifacts, diagnostics=[], usage=usage, provenance_receipts=[])
        contracts.validate_method_result(method, result, request)
        return result
