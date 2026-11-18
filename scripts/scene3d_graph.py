"""Editable Scene3D hierarchy with exact matrices and selective derived caches.

This is an in-memory document service. It does not replace project persistence,
animation evaluation, renderer visibility, or a native rig controller. Evaluated
matrices describe the authored base pose; animation bindings remain immutable
native references for the animation owner to evaluate.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import product
import hashlib
import json

import numpy as np

from rig_errors import RigError, decode_json, finite_json
from rig_math import compose, decompose, matrix_quaternion, slerp
from scene3d_contracts import MAX_DEPTH, MAX_FILES, MAX_TOTAL_BYTES, all_refs, canonical_bytes, portable_path, validate_document, validator
from scene3d_math import local_matrix

KEEP_PARENT = object()

def fail(message, code='SCENE3D_EDIT'):
    raise RigError(code, message, stage='scene3d-graph')


def require(condition, message, code='SCENE3D_EDIT'):
    if not condition:
        fail(message, code)


def _affine(value):
    try:
        result = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        fail('Expected a finite affine matrix: ' + str(exc))
    require(result.shape == (4, 4) and np.isfinite(result).all() and
            np.allclose(result[3], [0, 0, 0, 1], atol=1e-10, rtol=0),
            'Expected a finite affine 4 by 4 matrix.')
    return result


def transform_for_matrix(matrix, *, pivot=None):
    """Represent an affine matrix without silently introducing shear/reflection."""
    matrix = _affine(matrix)
    try:
        transform = decompose(matrix)
    except RigError as exc:
        raise RigError('SCENE3D_UNREPRESENTABLE',
                       'The requested edit needs unsupported shear, reflection or a singular transform.',
                       stage='scene3d-graph', correction='Choose a representable parent or bake the geometry explicitly.') from exc
    if pivot is not None:
        p = np.asarray(pivot, dtype=float)
        require(p.shape == (3,) and np.isfinite(p).all(), 'Pivot must be a finite three-vector.')
        transform['pivot'] = p.tolist()
        transform['translation'] = (matrix[:3, 3] - p + matrix[:3, :3] @ p).tolist()
    require(np.allclose(local_matrix(transform), matrix, atol=1e-7, rtol=1e-7),
            'The matrix cannot be represented without distortion.', 'SCENE3D_UNREPRESENTABLE')
    return transform


def _inverse(matrix):
    matrix = _affine(matrix)
    try:
        require(np.linalg.cond(matrix[:3, :3]) <= 1e8, 'Parent transform is ill-conditioned.',
                'SCENE3D_UNREPRESENTABLE')
        result = np.linalg.inv(matrix)
    except np.linalg.LinAlgError as exc:
        raise RigError('SCENE3D_UNREPRESENTABLE', 'Parent transform is singular.', stage='scene3d-graph') from exc
    require(np.isfinite(result).all(), 'Inverse transform is nonfinite.', 'SCENE3D_UNREPRESENTABLE')
    return result


def _blend(left, right, weight):
    if weight == 0:
        return left
    if weight == 1:
        return right
    a, b = transform_for_matrix(left), transform_for_matrix(right)
    return compose((1-weight)*np.asarray(a['translation']) + weight*np.asarray(b['translation']),
                   slerp(a['rotation'], b['rotation'], weight),
                   (1-weight)*np.asarray(a['scale']) + weight*np.asarray(b['scale']))


def _aim(matrix, target, camera=False):
    old = transform_for_matrix(matrix)
    direction = np.asarray(target) - matrix[:3, 3]
    require(np.linalg.norm(direction) > 1e-10, 'Look-at target coincides with its owner.', 'SCENE3D_CONSTRAINT')
    direction /= np.linalg.norm(direction)
    z = -direction if camera else direction
    up = np.array([0., 1., 0.])
    if abs(float(np.dot(up, z))) > .999999:
        up = np.array([1., 0., 0.])
    x = np.cross(up, z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return compose(old['translation'], matrix_quaternion(np.column_stack([x, y, z])), old['scale'])


def _world_bounds(bounds, world):
    if bounds is None:
        return None
    points = np.array(list(product(*zip(bounds['minimum'], bounds['maximum']))))
    points = points @ world[:3, :3].T + world[:3, 3]
    require(np.isfinite(points).all(), 'World bounds overflow.')
    return dict(minimum=points.min(axis=0).tolist(), maximum=points.max(axis=0).tolist())


def _dependencies(node):
    return ([node['parent']] if node['parent'] is not None else []) + [c['target'] for c in node['constraints']] + (
        [node['attachment']['socket']] if 'attachment' in node else [])


def _invalidate_closure(nodes, seeds):
    reverse = {identifier: set() for identifier in nodes}
    for identifier, node in nodes.items():
        for dependency in _dependencies(node):
            reverse.setdefault(dependency, set()).add(identifier)
    affected, pending = set(seeds), list(seeds)
    while pending:
        for identifier in reverse.get(pending.pop(), ()):
            if identifier not in affected:
                affected.add(identifier)
                pending.append(identifier)
    return affected


def _subtree(nodes, identifier):
    require(isinstance(identifier,str) and identifier in nodes, 'Unknown node: ' + str(identifier))
    children = {i: [] for i in nodes}
    for n in nodes.values():
        if n['parent'] in children:
            children[n['parent']].append(n['id'])
    result, pending = [], [identifier]
    while pending:
        current = pending.pop()
        result.append(current)
        pending.extend(reversed(children[current]))
    return result


def _relink(value, mapping):
    """Rewrite only node-owned reference fields, never asset/material identities."""
    value = deepcopy(value)
    value['id'] = mapping.get(value['id'], value['id'])
    value['parent'] = mapping.get(value['parent'], value['parent'])
    for c in value['constraints']:
        c['target'] = mapping.get(c['target'], c['target'])
    for b in value['animation_bindings']:
        b['target'] = mapping.get(b['target'], b['target'])
    if 'attachment' in value:
        for field in ('owner', 'socket'):
            value['attachment'][field] = mapping.get(value['attachment'][field], value['attachment'][field])
    return value


def _inventories(scene):
    scene['roots'] = [n['id'] for n in scene['nodes'] if n['parent'] is None]
    scene['cameras'] = [n['id'] for n in scene['nodes'] if n['type'] == 'camera']
    scene['lights'] = [n['id'] for n in scene['nodes'] if n['type'] == 'light']


def _derive_signature(node):
    fields = ('parent', 'transform', 'visible', 'render', 'collections', 'layers',
              'bounds', 'asset', 'constraints', 'attachment', 'locked', 'selectable')
    return json.dumps({key: node.get(key) for key in fields}, sort_keys=True, allow_nan=False)


class SceneGraph:
    """Single-owner base-pose editing; every operation validates before promotion.

    Pass an explicit complete ``external_references`` inventory before destructive
    edits, or construct through ``from_bundle``. Unknown external references fail
    closed rather than silently deleting a shot's referenced node.
    """

    def __init__(self, scene, *, external_references=None, asset_bounds=None, animation_documents=None, animation_payloads=None):
        finite_json(scene)
        finite_json(external_references)
        finite_json(asset_bounds)
        scene = validate_document(scene, 'scene')
        require(len(canonical_bytes(scene)) <= 16*1024*1024, 'Working scene exceeds the document byte bound.')
        require(animation_payloads is None or isinstance(animation_payloads,dict), 'Invalid animation byte mapping.')
        self._animation_payloads = dict(animation_payloads or {})
        self._animation_documents = {}
        require((animation_documents is None or isinstance(animation_documents, dict)) and
                len(self._animation_payloads) <= MAX_FILES and
                all(isinstance(p,str) and isinstance(raw,bytes) for p,raw in self._animation_payloads.items()) and
                sum(len(raw) for raw in self._animation_payloads.values()) <= MAX_TOTAL_BYTES,
                'Invalid or excessive exact animation byte context.')
        for path, payload in self._animation_payloads.items():
            portable_path(path)
            self._animation_documents[path] = validate_document(decode_json(payload), 'animation')
        require(animation_documents is None or set(animation_documents) == set(self._animation_documents) and
                all(canonical_bytes(animation_documents[p]) == canonical_bytes(d) for p,d in self._animation_documents.items()),
                'Parsed animation context must match its exact verified source bytes.', 'SCENE3D_ANIMATION_CONTEXT')
        self._derived_documents = {}
        self._scene = deepcopy(scene)
        self._references = deepcopy(external_references)
        require(asset_bounds is None or isinstance(asset_bounds,dict), 'Invalid asset bounds context.')
        self._asset_bounds = deepcopy({} if asset_bounds is None else asset_bounds)
        require(isinstance(self._asset_bounds, dict) and all(isinstance(k, str) and
                (v is None or next(validator('bounds').iter_errors(v), None) is None and
                 all(a <= b for a, b in zip(v['minimum'], v['maximum'])))
                for k, v in self._asset_bounds.items()), 'Invalid asset bounds context.')
        require(external_references is None or isinstance(external_references, dict) and
                all(isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v)
                    for k, v in external_references.items()), 'Invalid external-reference context.')
        self._cache, self._evaluations, self._bounds_cache = {}, {}, {}
        self._isolate = None
        self._check_service_semantics(self._scene)

    @classmethod
    def from_bundle(cls, bundle, scene_path, *, source_root=None):
        require(scene_path in bundle['documents'], 'Scene is absent from verified bundle.')
        scene = bundle['documents'][scene_path]
        require(scene['kind'] == 'scene3d-scene', 'Choose a scene manifest.')
        references = {}
        relevant = []
        for path, document in bundle['documents'].items():
            if document['kind'] == 'scene3d-shot' and document['scene']['path'] == scene_path:
                relevant.append((path, document))
        pending = list(relevant)  # Scene-owned bindings are recomputed from each candidate.
        seen = set()
        while pending:
            path, document = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            if document['kind'] != 'scene3d-scene':
                targets = [t['target'] for t in document.get('tracks', []) if t['target'] != 'world']
                targets += [b['target'] for b in document.get('bindings', [])]
                targets += [o['target'] for o in document.get('material_overrides', [])]
                targets += [t for c in document.get('temporal_constraints', [])
                            for t in (c['target'], c['constraint']['target'])]
                if document['kind'] == 'scene3d-shot':
                    targets.append(document['camera'])
                for target in targets:
                    references.setdefault(target, []).append(path)
            bindings = document.get('bindings', []) + [b for n in document.get('nodes', []) for b in n['animation_bindings']]
            for binding in bindings:
                if binding['clip']['kind'] == 'scene3d-animation':
                    p = binding['clip']['path']
                    require(p in bundle['documents'], 'Missing animation dependency context.')
                    pending.append((p, bundle['documents'][p]))
        bounds = {d['id'] + '@' + d['version']: d['bounds'] for d in bundle['documents'].values()
                  if d['kind'] == 'scene3d-asset'}
        payloads, total = {}, 0
        if source_root is not None:
            from asset_library import read_source_bytes
            from pathlib import Path
            for path, document in bundle['documents'].items():
                if document['kind'] == 'scene3d-animation':
                    portable_path(path)
                    require(len(payloads) < MAX_FILES, 'Animation context count exceeds bound.')
                    raw = read_source_bytes(Path(source_root)/path, 16*1024*1024)
                    require(hashlib.sha256(raw).hexdigest() == bundle['inventory'][path]['sha256'],
                            'Animation source changed after verified load.', 'SCENE3D_ANIMATION_CONTEXT')
                    require(canonical_bytes(validate_document(decode_json(raw),'animation')) == canonical_bytes(document),
                            'Parsed animation context differs from verified bytes.', 'SCENE3D_ANIMATION_CONTEXT')
                    total += len(raw)
                    require(total <= MAX_TOTAL_BYTES, 'Animation context bytes exceed bound.')
                    payloads[path] = raw
        return cls(scene, external_references=references, asset_bounds=bounds, animation_payloads=payloads)

    @staticmethod
    def _check_service_semantics(scene):
        nodes = {n['id']: n for n in scene['nodes']}
        require(sum(len(_dependencies(n)) for n in nodes.values()) <= 100000,
                'Graph dependency operation budget exceeded.', 'SCENE3D_GRAPH_LIMIT')
        for node in nodes.values():
            for c in node['constraints']:
                require(c['type'] != 'socket' or nodes[c['target']]['type'] == 'socket',
                        'A socket constraint must target a socket node.', 'SCENE3D_CONSTRAINT')

    def document(self):
        return deepcopy(self._scene)

    @property
    def revision(self):
        return self._scene['revision']

    @property
    def digest(self):
        return hashlib.sha256(canonical_bytes(self._scene)).hexdigest()

    def _nodes(self):
        return {n['id']: n for n in self._scene['nodes']}

    def _editable(self, identifier):
        nodes = self._nodes()
        require(isinstance(identifier, str) and identifier in nodes, 'Unknown node: ' + str(identifier))
        node = nodes[identifier]
        collections = {c['id']: c for c in self._scene['collections']}
        layers = {c['id']: c for c in self._scene['layers']}
        cursor = node
        membership_locked = False
        while cursor is not None:
            membership_locked |= any(collections[c].get('locked', False) for c in cursor['collections'])
            membership_locked |= any(layers[c].get('locked', False) for c in cursor['layers'])
            cursor = nodes[cursor['parent']] if cursor['parent'] is not None else None
        require(not node['locked'] and not membership_locked, 'Node or inherited membership is locked.', 'SCENE3D_LOCKED')
        return node

    def _check_animation_bindings(self, candidate, documents, payloads):
        from scene3d_contracts import _binding_context
        checked = set()
        def verify(reference, depth=0):
            if reference['kind'] != 'scene3d-animation':
                return
            require(depth <= MAX_DEPTH, 'Animation binding closure exceeds depth bound.')
            path = reference['path']
            key = (path,reference['sha256'])
            if key in checked:
                return
            require(path in documents and path in payloads and
                    hashlib.sha256(payloads[path]).hexdigest() == reference['sha256'] and
                    (documents[path]['id'],documents[path]['version']) == (reference['id'],reference['version']),
                    'Editing a bound Scene3D clip requires complete exact byte context.', 'SCENE3D_ANIMATION_CONTEXT')
            checked.add(key)
            for nested in documents[path]['bindings']:
                verify(nested['clip'],depth+1)
        context = {'seen':set(),'checks':0}
        for node in candidate['nodes']:
            for binding in node['animation_bindings']:
                verify(binding['clip'])
                _binding_context(binding,candidate,documents,context)

    def _commit(self, candidate, *, expected_revision=None, derived_documents=None):
        require(expected_revision is None or type(expected_revision) is int and expected_revision == self.revision,
                'Scene revision conflict.', 'SCENE3D_CONFLICT')
        _inventories(candidate)
        candidate['revision'] = self.revision + 1
        candidate = validate_document(candidate, 'scene')
        require(len(canonical_bytes(candidate)) <= 16*1024*1024, 'Working scene exceeds the document byte bound.')
        self._check_service_semantics(candidate)
        require(derived_documents is None or isinstance(derived_documents,dict), 'Invalid derived animation mapping.')
        additions = {p:validate_document(d,'animation') for p,d in (derived_documents or {}).items()}
        require(all(p not in self._animation_payloads or canonical_bytes(d) == self._animation_payloads[p]
                    for p,d in additions.items()), 'Derived animation cannot replace immutable source bytes.')
        payloads = {**self._animation_payloads, **{p:canonical_bytes(d) for p,d in additions.items()}}
        documents = {**self._animation_documents, **additions}
        require(len(payloads) <= MAX_FILES and sum(len(raw) for raw in payloads.values()) <= MAX_TOTAL_BYTES,
                'Combined original and derived animation context exceeds its resource bound.', 'SCENE3D_GRAPH_LIMIT')
        for path in additions:
            portable_path(path)
        self._check_animation_bindings(candidate, documents, payloads)
        old, new = self._nodes(), {n['id']: n for n in candidate['nodes']}
        seeds = {identifier for identifier in old.keys() | new.keys()
                 if identifier not in old or identifier not in new or
                 _derive_signature(old[identifier]) != _derive_signature(new[identifier])}
        if candidate['collections'] != self._scene['collections'] or candidate['layers'] != self._scene['layers']:
            seeds.update(new)
        if seeds:
            candidate['bounds'] = None  # Explicitly invalidate derived scene bounds.
        dirty = _invalidate_closure(old, seeds) | _invalidate_closure(new, seeds)
        self._scene = candidate
        self._animation_documents, self._animation_payloads = documents, payloads
        self._derived_documents.update(additions)
        for identifier in dirty:
            self._cache.pop(identifier, None)
        bounds_dirty = set(dirty)
        for table in (old, new):
            for identifier in dirty:
                cursor = table.get(identifier)
                while cursor is not None and cursor['parent'] is not None:
                    bounds_dirty.add(cursor['parent'])
                    cursor = table.get(cursor['parent'])
        for identifier in bounds_dirty:
            self._bounds_cache.pop(identifier, None)
        if self._isolate is not None:
            self._isolate.intersection_update(new)
        return dict(revision=self.revision, dirty=sorted(dirty), removed=sorted(old.keys()-new.keys()))

    def replace_document(self, scene, *, expected_revision, derived_documents=None):
        """Atomic in-memory replacement, also used by prefab/import services."""
        finite_json(scene)
        return self._commit(deepcopy(scene), expected_revision=expected_revision, derived_documents=derived_documents)

    def create(self, node, *, expected_revision=None):
        finite_json(node)
        candidate = self.document()
        candidate['nodes'].append(deepcopy(node))
        return self._commit(candidate, expected_revision=expected_revision)

    def set_transform(self, identifier, transform, *, space='local', expected_revision=None):
        node = self._editable(identifier)
        candidate = self.document()
        target = next(n for n in candidate['nodes'] if n['id'] == identifier)
        if space == 'world':
            require(not node['constraints'] and 'attachment' not in node,
                    'Driven transforms must be detached before direct world editing.', 'SCENE3D_DRIVEN')
            parent = np.eye(4) if node['parent'] is None else np.asarray(self.evaluate_base(node['parent'])['world_matrix'])
            target['transform'] = transform_for_matrix(_inverse(parent) @ _affine(transform),
                                                       pivot=node['transform'].get('pivot'))
        else:
            require(space == 'local', 'Transform space must be local or world.')
            finite_json(transform)
            target['transform'] = deepcopy(transform)
        return self._commit(candidate, expected_revision=expected_revision)

    def transform(self, identifier, *, space='local'):
        require(isinstance(identifier,str) and identifier in self._nodes(), 'Unknown transform node.')
        require(space in ('local', 'world'), 'Transform space must be local or world.')
        return (deepcopy(self._nodes()[identifier]['transform']) if space == 'local'
                else self.evaluate_base(identifier)['world_matrix'])

    def rename(self, identifier, name, *, expected_revision=None):
        """Change a display label while retaining the stable node identity."""
        return self.configure_node(identifier, {'name': name}, expected_revision=expected_revision)

    def configure_node(self, identifier, properties, *, expected_revision=None):
        """Replace explicit authored properties through the canonical validator.

        Identity, parenting, transforms and visibility have dedicated operations;
        this operation cannot smuggle replacements for those authorities.
        """
        self._editable(identifier)
        require(isinstance(properties, dict) and properties and
                set(properties) <= {'name', 'camera', 'light', 'material_overrides', 'metadata'},
                'Unsupported node property update.', 'SCENE3D_COMMAND')
        finite_json(properties)
        candidate = self.document()
        target = next(n for n in candidate['nodes'] if n['id'] == identifier)
        target.update(deepcopy(properties))
        return self._commit(candidate, expected_revision=expected_revision)

    def configure_world(self, properties, *, expected_revision=None):
        """Replace bounded world properties without changing coordinates."""
        require(isinstance(properties, dict) and properties and
                set(properties) <= {'background', 'exposure', 'environment', 'metadata'},
                'Unsupported world property update.', 'SCENE3D_COMMAND')
        finite_json(properties)
        candidate = self.document()
        candidate['world'].update(deepcopy(properties))
        return self._commit(candidate, expected_revision=expected_revision)

    def set_tags(self, identifier, tags):
        require(isinstance(tags, list) and all(isinstance(t, str) and 0 < len(t) <= 60 for t in tags),
                'Tags must be a bounded string list.')
        self._editable(identifier)
        candidate = self.document()
        n = next(n for n in candidate['nodes'] if n['id'] == identifier)
        n['metadata'] = {k: v for k, v in n['metadata'].items() if not k.startswith('tag_')}
        n['metadata'].update({'tag_'+t: True for t in tags})
        return self._commit(candidate)

    def set_node_flags(self, identifier, **flags):
        allowed = {'visible', 'selectable', 'locked', 'render_visible', 'cast_shadow', 'receive_shadow', 'camera_visible'}
        require(set(flags) <= allowed and all(type(v) is bool for v in flags.values()), 'Invalid node visibility/lock flags.')
        candidate = self.document()
        target = next((n for n in candidate['nodes'] if n['id'] == identifier), None)
        require(target is not None, 'Unknown node.')
        for key, value in flags.items():
            if key in ('visible', 'selectable', 'locked'):
                target[key] = value
            else:
                target['render']['visible' if key == 'render_visible' else key] = value
        return self._commit(candidate)

    def set_memberships(self, identifier, *, collections=None, layers=None):
        self._editable(identifier)
        candidate = self.document()
        target = next(n for n in candidate['nodes'] if n['id'] == identifier)
        if collections is not None:
            target['collections'] = deepcopy(collections)
        if layers is not None:
            target['layers'] = deepcopy(layers)
        return self._commit(candidate)

    def collection(self, identifier, *, name=None, layer=False, **flags):
        require(type(layer) is bool, 'layer must be a boolean.')
        require(set(flags) <= {'visible', 'editor_visible', 'render_visible', 'locked'} and
                all(type(value) is bool for value in flags.values()), 'Invalid collection flags.')
        candidate = self.document()
        target = next((c for c in candidate['layers' if layer else 'collections'] if c['id'] == identifier), None)
        if target is None:
            require(name is not None, 'A new collection needs a display name.')
            target = dict(id=identifier, name=name, visible=True)
            candidate['layers' if layer else 'collections'].append(target)
        elif name is not None:
            target['name'] = name
        target.update(flags)
        return self._commit(candidate)

    def isolate(self, identifiers=None):
        """Transient editor isolation; never changes render flags or source bytes."""
        if identifiers is None:
            self._isolate = None
        else:
            require(isinstance(identifiers,(list,tuple,set)) and all(isinstance(i,str) for i in identifiers), 'Invalid isolation node list.')
            nodes = self._nodes()
            selected = set()
            for identifier in identifiers:
                selected.update(_subtree(nodes, identifier))
                cursor = nodes[identifier]['parent']
                while cursor is not None:
                    selected.add(cursor)
                    cursor = nodes[cursor]['parent']
            self._isolate = selected
        return sorted(self._isolate) if self._isolate is not None else None

    def reparent(self, identifier, parent, *, preserve_world=True, expected_revision=None):
        node = self._editable(identifier)
        nodes = self._nodes()
        require(type(preserve_world) is bool, 'preserve_world must be a boolean.')
        require(parent is None or isinstance(parent, str) and parent in nodes, 'Unknown new parent.')
        require(parent not in _subtree(nodes, identifier), 'Cannot reparent beneath a descendant.')
        candidate = self.document()
        target = next(n for n in candidate['nodes'] if n['id'] == identifier)
        target['parent'] = parent
        if preserve_world:
            require(not node['constraints'] and 'attachment' not in node,
                    'Driven transforms must be detached before preserve-world reparenting.', 'SCENE3D_DRIVEN')
            previous = np.asarray(self.evaluate_base(identifier)['world_matrix'])
            new_parent = np.eye(4) if parent is None else np.asarray(self.evaluate_base(parent)['world_matrix'])
            target['transform'] = transform_for_matrix(_inverse(new_parent) @ previous,
                                                       pivot=node['transform'].get('pivot'))
        return self._commit(candidate, expected_revision=expected_revision)

    def reorder(self, identifier, index):
        self._editable(identifier)
        nodes = self._nodes()
        siblings = [n['id'] for n in self._scene['nodes'] if n['parent'] == nodes[identifier]['parent']]
        require(type(index) is int and 0 <= index < len(siblings), 'Sibling index is out of range.')
        siblings.remove(identifier)
        siblings.insert(index, identifier)
        iterator = iter(siblings)
        candidate = self.document()
        ordered = []
        for n in candidate['nodes']:
            ordered.append(deepcopy(nodes[next(iterator)]) if n['parent'] == nodes[identifier]['parent'] else n)
        candidate['nodes'] = ordered
        return self._commit(candidate)

    def derived_documents(self):
        return deepcopy(self._derived_documents)

    def animation_payloads(self):
        """Return exact immutable animation bytes for downstream owner admission."""
        return dict(self._animation_payloads)

    def _map_clip(self, reference, mapping, context, derived, depth=0, state=None):
        if reference['kind'] != 'scene3d-animation':
            return deepcopy(reference)
        state = {'calls':0,'bytes':0,'memo':{}} if state is None else state
        state['calls'] += 1
        require(state['calls'] <= 100000, 'Animation derivation operation budget exceeded.')
        key = (reference['path'], reference['sha256'])
        if key in state['memo']:
            return deepcopy(state['memo'][key])
        require(depth <= MAX_DEPTH, 'Animation derivation exceeds depth bound.')
        source = self._animation_documents.get(reference['path'])
        require(source is not None, 'Duplicating a bound Scene3D clip requires complete animation context.',
                'SCENE3D_ANIMATION_CONTEXT')
        require(hashlib.sha256(self._animation_payloads[reference['path']]).hexdigest() == reference['sha256'] and
                (source['id'], source['version']) == (reference['id'], reference['version']),
                'Animation context identity mismatch.', 'SCENE3D_ANIMATION_CONTEXT')
        clip = deepcopy(source)
        clip['id'] = 'a-'+hashlib.sha256((context+'\0'+reference['path']+'\0'+reference['sha256']).encode()).hexdigest()[:48]
        derived_path = 'derived-animations/'+clip['id']+'/'+clip['version']+'.json'
        require(len(derived) < MAX_FILES, 'Derived animation count exceeds bound.')
        for track in clip['tracks']:
            require(track['target'] in mapping, 'Duplicated clip targets outside the copied subtree.')
            track['target'] = mapping[track['target']]
        for binding in clip['bindings']:
            require(binding['target'] in mapping, 'Duplicated nested clip targets outside the copied subtree.')
            binding['target'] = mapping[binding['target']]
            binding['clip'] = self._map_clip(binding['clip'], mapping, context, derived, depth+1, state)
        dependencies = {r['path']:deepcopy(r) for r in clip['dependencies']}
        dependencies[reference['path']] = deepcopy(reference)
        for binding in clip['bindings']:
            dependencies[binding['clip']['path']] = deepcopy(binding['clip'])
        clip['dependencies'] = list(dependencies.values())
        clip['provenance']['origin'] = 'derived'
        clip['provenance']['tool'] = 'scene3d-duplicate-animation-target-map-v1'
        clip['provenance']['inputs'].append(dict(path=reference['path'], sha256=reference['sha256']))
        validate_document(clip, 'animation')
        path = 'derived-animations/'+clip['id']+'/'+clip['version']+'.json'
        payload = canonical_bytes(clip)
        require(path not in derived or derived[path] == clip, 'Conflicting derived animation identity.')
        state['bytes'] += len(payload)
        require(state['bytes'] <= MAX_TOTAL_BYTES,
                'Derived animation bytes exceed bound.')
        derived[path] = clip
        result = dict(id=clip['id'],version=clip['version'],kind=clip['kind'],path=path,
                      sha256=hashlib.sha256(payload).hexdigest())
        state['memo'][key] = result
        return deepcopy(result)

    def duplicate(self, identifier, new_id, *, parent=KEEP_PARENT, preserve_world=True):
        self._editable(identifier)
        nodes = self._nodes()
        require(isinstance(new_id, str) and next(validator('node').iter_errors({**nodes[identifier], 'id': new_id}), None) is None and new_id not in nodes, 'Invalid or existing duplicate destination ID.')
        require(type(preserve_world) is bool, 'preserve_world must be a boolean.')
        selected = _subtree(nodes, identifier)
        mapping = {identifier: new_id}
        for old in selected[1:]:
            mapping[old] = 'n-' + hashlib.sha256((new_id+'\0'+old).encode()).hexdigest()[:48]
        require(len(set(mapping.values())) == len(mapping) and not set(mapping.values()) & nodes.keys(),
                'Duplicate identifier collision.')
        target_parent = nodes[identifier]['parent'] if parent is KEEP_PARENT else parent
        require(target_parent is None or isinstance(target_parent, str) and target_parent in nodes, 'Unknown duplicate parent.')
        candidate = self.document()
        copies = [_relink(nodes[old], mapping) for old in selected]
        copies[0]['parent'] = target_parent
        if preserve_world and target_parent != nodes[identifier]['parent']:
            require(not nodes[identifier]['constraints'] and 'attachment' not in nodes[identifier],
                    'Driven subtree needs an explicit detach before moving its duplicate.', 'SCENE3D_DRIVEN')
            p = np.eye(4) if target_parent is None else np.asarray(self.evaluate_base(target_parent)['world_matrix'])
            copies[0]['transform'] = transform_for_matrix(_inverse(p) @ np.asarray(self.evaluate_base(identifier)['world_matrix']),
                                                         pivot=nodes[identifier]['transform'].get('pivot'))
        derived = {}
        state = {'calls':0,'bytes':0,'memo':{}}
        for copied in copies:
            for binding in copied['animation_bindings']:
                binding['clip'] = self._map_clip(binding['clip'], mapping, new_id, derived, state=state)
        candidate['nodes'].extend(copies)
        result = self._commit(candidate, derived_documents=derived)
        self._derived_documents.update(derived)
        self._animation_documents.update(derived)
        self._animation_payloads.update({p:canonical_bytes(d) for p,d in derived.items()})
        result['derived_documents'] = deepcopy(derived)
        result['mapping'] = mapping
        return result

    def delete(self, identifier, *, policy='reject', expected_revision=None):
        self._editable(identifier)
        require(policy in ('reject', 'subtree', 'promote'), 'Choose reject, subtree or promote deletion.')
        nodes = self._nodes()
        selected = _subtree(nodes, identifier)
        require(policy != 'reject' or len(selected) == 1, 'Node has children; choose an explicit deletion policy.')
        removed = set(selected if policy == 'subtree' else [identifier])
        require(self._references is not None, 'Destructive editing needs complete external-reference context.',
                'SCENE3D_REFERENCE_CONTEXT')
        require(not any(self._references.get(n) for n in removed), 'A deleted node is referenced outside this scene.',
                'SCENE3D_REFERENCED')
        for old in removed:
            self._editable(old)
        candidate = self.document()
        if policy == 'promote':
            for n in candidate['nodes']:
                if n['parent'] == identifier:
                    self._editable(n['id'])
                    require(not n['constraints'] and 'attachment' not in n,
                            'Driven children cannot be silently promoted.', 'SCENE3D_DRIVEN')
                    p = np.eye(4) if nodes[identifier]['parent'] is None else np.asarray(
                        self.evaluate_base(nodes[identifier]['parent'])['world_matrix'])
                    n['transform'] = transform_for_matrix(_inverse(p) @ np.asarray(self.evaluate_base(n['id'])['world_matrix']),
                                                          pivot=n['transform'].get('pivot'))
                    n['parent'] = nodes[identifier]['parent']
        candidate['nodes'] = [n for n in candidate['nodes'] if n['id'] not in removed]
        require(candidate['nodes'], 'A scene cannot be empty; create another root before deletion.')
        return self._commit(candidate, expected_revision=expected_revision)

    def evaluate_base(self, identifier=None):
        """Evaluate exact authored matrices, supported constraints and visibility.

        Source animations remain referenced, not sampled. Each result explicitly
        says ``base_pose_only``; this is not a render/animation evaluation receipt.
        """
        nodes = self._nodes()
        require(identifier is None or isinstance(identifier,str) and identifier in nodes, 'Unknown evaluation node.')
        children = {i: [] for i in nodes}
        for n in nodes.values():
            if n['parent'] is not None:
                children[n['parent']].append(n['id'])
        require(not any('prefab' in n for n in nodes.values()),
                'Expand prefab instances through PrefabLibrary before graph evaluation.', 'SCENE3D_PREFAB')
        groups = ({c['id']: c for c in self._scene['collections']}, {c['id']: c for c in self._scene['layers']})
        def evaluate(current):
            if current in self._cache:
                return self._cache[current]
            node = nodes[current]
            for dependency in _dependencies(node):
                evaluate(dependency)
            parent = self._cache.get(node['parent'])
            world = (parent['_matrix'] if parent else np.eye(4)) @ local_matrix(node['transform'])
            if 'attachment' in node:
                a = node['attachment']
                world = self._cache[a['socket']]['_matrix'] @ local_matrix(a['offset']) @ local_matrix(node['transform'])
            for constraint in node['constraints']:
                if constraint['influence'] == 0:
                    continue
                target = self._cache[constraint['target']]['_matrix']
                desired = _aim(world, target[:3, 3], node['type'] == 'camera') if constraint['type'] == 'look-at' else target
                world = _blend(world, desired, constraint['influence'])
            _affine(world)
            editor, render = node['visible'], node['render']['visible']
            membership_locked = parent['_membership_locked'] if parent else False
            for table, field in zip(groups, ('collections', 'layers')):
                for membership in node[field]:
                    c = table[membership]
                    editor = editor and c['visible'] and c.get('editor_visible', True)
                    render = render and c['visible'] and c.get('render_visible', True)
                    membership_locked = membership_locked or c.get('locked', False)
            locked = node['locked'] or membership_locked
            editor = bool(editor and (parent['editor_visible'] if parent else True))
            render = bool(render and (parent['render_visible'] if parent else True))
            bounds = node['bounds']
            if bounds is None and 'asset' in node:
                bounds = self._asset_bounds.get(node['asset']['id']+'@'+node['asset']['version'])
            result = dict(world_matrix=world.tolist(), local_matrix=local_matrix(node['transform']).tolist(),
                          world_bounds=_world_bounds(bounds, world), editor_visible=editor,
                          render_visible=render, camera_visible=render and node['render'].get('camera_visible', True) and
                              (parent['camera_visible'] if parent else True),
                          cast_shadow=render and node['render']['cast_shadow'] and (parent['cast_shadow'] if parent else True),
                          receive_shadow=render and node['render']['receive_shadow'] and (parent['receive_shadow'] if parent else True),
                          selectable=editor and node['selectable'] and not locked,
                          locked=bool(locked), base_pose_only=True, animation_evaluated=False, _matrix=world,
                          _membership_locked=membership_locked)
            self._cache[current] = result
            self._evaluations[current] = self._evaluations.get(current, 0) + 1
            return result
        chosen = [identifier] if identifier is not None else list(nodes)
        require(all(i in nodes for i in chosen), 'Unknown evaluated node.')
        output = {}
        def aggregate(current):
            if current in self._bounds_cache:
                return self._bounds_cache[current]
            boxes = [evaluate(current)['world_bounds']]
            boxes += [aggregate(child) for child in children[current]]
            boxes = [b for b in boxes if b is not None]
            result = (dict(minimum=np.min([b['minimum'] for b in boxes], axis=0).tolist(),
                           maximum=np.max([b['maximum'] for b in boxes], axis=0).tolist()) if boxes else None)
            self._bounds_cache[current] = result
            return result
        for current in chosen:
            result = deepcopy({k: v for k, v in evaluate(current).items() if not k.startswith('_')})
            result['world_bounds'] = deepcopy(aggregate(current))
            if self._isolate is not None:
                result['editor_visible'] = result['editor_visible'] and current in self._isolate
                result['selectable'] = result['selectable'] and current in self._isolate
            output[current] = result
        return output[identifier] if identifier is not None else output

    def evaluation_counts(self):
        return dict(self._evaluations)

    def bounds(self, identifiers=None):
        values = self.evaluate_base()
        require(identifiers is None or isinstance(identifiers,(list,tuple,set)) and all(isinstance(i,str) for i in identifiers),
                'Invalid bounds node list.')
        identifiers = list(values) if identifiers is None else list(identifiers)
        require(all(i in values for i in identifiers), 'Unknown bounds node.')
        boxes = [values[i]['world_bounds'] for i in identifiers if values[i]['world_bounds'] is not None]
        if not boxes:
            return None
        return dict(minimum=np.min([b['minimum'] for b in boxes], axis=0).tolist(),
                    maximum=np.max([b['maximum'] for b in boxes], axis=0).tolist())

    def query(self, *, name=None, node_type=None, tag=None, collection=None, region=None):
        require(all(v is None or isinstance(v, str) for v in (name, node_type, tag, collection)), 'Query filters must be strings.')
        if region is not None:
            require(isinstance(region, dict) and set(region) == {'minimum', 'maximum'}, 'Region must be an axis-aligned box.')
            try: lo, hi = np.asarray(region['minimum'], dtype=float), np.asarray(region['maximum'], dtype=float)
            except (ValueError, TypeError, OverflowError) as exc: fail('Invalid region coordinates: '+str(exc))
            require(lo.shape == hi.shape == (3,) and np.isfinite(lo).all() and np.isfinite(hi).all()
                    and (lo <= hi).all(), 'Region bounds must be finite and ordered.')
        values = self.evaluate_base() if region is not None else None
        result = []
        for node in self._scene['nodes']:
            if name is not None and node['name'] != name:
                continue
            if node_type is not None and node['type'] != node_type:
                continue
            if tag is not None and node['metadata'].get('tag') != tag and node['metadata'].get('tag_'+tag) is not True:
                continue
            if collection is not None and collection not in node['collections']:
                continue
            if region is not None:
                b = values[node['id']]['world_bounds']
                if b is None or not ((np.asarray(b['maximum']) >= lo).all() and (np.asarray(b['minimum']) <= hi).all()):
                    continue
            result.append(node['id'])
        return result
