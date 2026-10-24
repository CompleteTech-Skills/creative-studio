"""Immutable, versioned Scene3D prefab expansion and conflict-aware upgrades.

Source definitions never receive instance edits. Expansion returns separate scene
and derived animation documents; callers persist them through the project owner.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from rig_errors import decode_json, finite_json
from scene3d_contracts import (MAX_DEPTH, MAX_NODES, MAX_FILES, MAX_TOTAL_BYTES, all_refs, canonical_bytes,
                               portable_path, validate_document, definitions, validator)
from scene3d_example import TRANSFORM, node as make_node
from scene3d_graph import SceneGraph, _inventories, _relink, _subtree, fail, require


def scoped_id(instance, source, namespace='node'):
    digest = hashlib.sha256((namespace+'\0'+instance+'\0'+source).encode('utf-8')).hexdigest()
    return 'p-' + digest[:48]


def _reference(document, payload, path):
    portable_path(path)
    return dict(id=document['id'], version=document['version'], kind=document['kind'],
                path=path, sha256=hashlib.sha256(payload).hexdigest())


def _refs(values):
    output = {}
    for value in values:
        for r in all_refs(value):
            if 'kind' not in r:
                continue
            key = (r['kind'], r['id'], r['version'])
            require(key not in output or output[key] == r, 'Conflicting immutable dependency identity.',
                    'SCENE3D_IMMUTABLE')
            output[key] = deepcopy(r)
    return sorted(output.values(), key=lambda r: (r['kind'], r['id'], r['version']))


class PrefabLibrary:
    """In-memory exact-byte catalog, without a second asset blob store."""

    def __init__(self):
        self._documents = {}
        self._payloads = {}
        self._references = {}

    @classmethod
    def from_bundle(cls, bundle):
        result = cls()
        for path, document in bundle['documents'].items():
            if document['kind'] in ('scene3d-prefab', 'scene3d-animation'):
                # Preserve the exact manifest's hash if callers supply bytes;
                # load_project only returns parsed JSON, so canonical admission
                # must match its recorded inventory, or request original bytes.
                payload = canonical_bytes(document)
                require(hashlib.sha256(payload).hexdigest() == bundle['inventory'][path]['sha256'],
                        'Noncanonical source manifests require from_files for exact-byte preservation.',
                        'SCENE3D_SOURCE_BYTES')
                result.register_bytes(payload, path)
        return result

    @classmethod
    def from_files(cls, root, bundle):
        from asset_library import read_source_bytes
        result = cls()
        for path, document in bundle['documents'].items():
            if document['kind'] in ('scene3d-prefab', 'scene3d-animation'):
                payload = read_source_bytes(Path(root)/portable_path(path), 16*1024*1024)
                require(hashlib.sha256(payload).hexdigest() == bundle['inventory'][path]['sha256'],
                        'Source changed after project admission.', 'SCENE3D_SOURCE_CHANGED')
                result.register_bytes(payload, path)
        return result

    def register_bytes(self, payload, path):
        require(isinstance(payload, bytes) and len(payload) <= 16*1024*1024,
                'Prefab/animation payload must be bounded immutable bytes.')
        document = validate_document(decode_json(payload))
        require(len(self._documents) < MAX_FILES and sum(map(len, self._payloads.values()))+len(payload) <= MAX_TOTAL_BYTES,
                'Prefab catalog file/byte budget exceeded.', 'SCENE3D_PREFAB_LIMIT')
        require(document['kind'] in ('scene3d-prefab', 'scene3d-animation'),
                'This catalog accepts Scene3D prefab and animation documents only.')
        identity = (document['kind'], document['id'], document['version'])
        reference = _reference(document, payload, path)
        require(identity not in self._references, 'This immutable version is already registered.', 'SCENE3D_IMMUTABLE')
        require(path not in self._payloads, 'A different manifest already occupies this path.', 'SCENE3D_IMMUTABLE')
        require(document['provenance']['fixture'] or not any(d['id'] == document['id'] and
                d['kind'] == document['kind'] and d['provenance']['fixture'] for d in self._documents.values()),
                'A fixture identity cannot be relabeled as production source.', 'SCENE3D_FIXTURE')
        self._documents[identity] = deepcopy(document)
        self._payloads[path] = payload
        self._references[identity] = reference
        try:
            self._check_cycles()
        except Exception:
            del self._documents[identity], self._references[identity], self._payloads[path]
            raise
        return deepcopy(reference)

    def register(self, document, path=None):
        payload = canonical_bytes(document)
        path = path or ('prefabs/' if document['kind'] == 'scene3d-prefab' else 'animations/') + document['id'] + '/' + document['version'] + '.json'
        return self.register_bytes(payload, path)

    def resolve(self, reference):
        finite_json(reference)
        require(next(validator('reference').iter_errors(reference), None) is None, 'Invalid immutable prefab reference.')
        identity = (reference.get('kind'), reference.get('id'), reference.get('version'))
        require(identity in self._documents and self._references[identity] == reference,
                'Exact prefab/animation version and source bytes are unavailable.', 'SCENE3D_PREFAB_MISSING')
        return deepcopy(self._documents[identity])

    def payloads(self):
        return dict(self._payloads)

    def _check_cycles(self):
        states, heights = {}, {}
        def visit(identity, depth):
            require(depth <= MAX_DEPTH, 'Prefab/animation closure exceeds depth limit.', 'SCENE3D_PREFAB_DEPTH')
            require(states.get(identity) != 1, 'Recursive prefab/animation reference.', 'SCENE3D_PREFAB_CYCLE')
            if states.get(identity) == 2:
                return heights[identity]
            states[identity] = 1
            children = []
            for r in all_refs(self._documents[identity]):
                child = (r.get('kind'), r.get('id'), r.get('version'))
                if child in self._documents:
                    require(self._references[child] == r, 'Conflicting dependency version/hash.', 'SCENE3D_IMMUTABLE')
                    children.append(visit(child, depth+1))
            height = 1 + max(children, default=0)
            require(height <= MAX_DEPTH, 'Prefab/animation closure exceeds depth limit.', 'SCENE3D_PREFAB_DEPTH')
            states[identity], heights[identity] = 2, height
            return height
        for identity in self._documents:
            visit(identity, 0)

    def _has_fixture(self, reference, seen=None):
        seen = set() if seen is None else seen
        identity = (reference['kind'], reference['id'], reference['version'], reference['sha256'])
        if identity in seen:
            return False
        seen.add(identity)
        source = self.resolve(reference)
        fixture = source['provenance']['fixture']
        for nested in all_refs(source):
            if nested.get('kind') in ('scene3d-prefab', 'scene3d-animation'):
                fixture = self._has_fixture(nested, seen) or fixture
        return fixture

    def create(self, graph, root_id, identifier, version, *, name=None,
               parameters=None, parameter_bindings=None):
        scene = graph.document()
        nodes = {n['id']: n for n in scene['nodes']}
        selected = _subtree(nodes, root_id)
        copied = [deepcopy(nodes[i]) for i in selected]
        copied[0]['parent'] = None
        copied[0]['transform'] = deepcopy(TRANSFORM)
        # A source-local clip/constraint dependency outside the selected subtree
        # cannot be converted into an unversioned reference to another project.
        used_collections = {i for n in copied for i in n['collections']}
        used_layers = {i for n in copied for i in n['layers']}
        document = dict(schema_version=1, kind='scene3d-prefab', id=identifier, version=version,
            name=name or nodes[root_id]['name'], roots=[root_id], nodes=copied,
            parameters=deepcopy(parameters or {}), parameter_bindings=deepcopy(parameter_bindings or []),
            material_slots=sorted({o['slot'] for n in copied for o in n['material_overrides']}),
            sockets=[n['id'] for n in copied if n['type'] == 'socket'], overrides=[],
            dependencies=_refs(copied), provenance=deepcopy(scene['provenance']),
            collections=[deepcopy(c) for c in scene['collections'] if c['id'] in used_collections],
            layers=[deepcopy(c) for c in scene['layers'] if c['id'] in used_layers])
        document['provenance']['origin'] = 'derived'
        document['provenance']['tool'] = 'scene3d-prefab-extraction-v1'
        validate_document(document, 'prefab')
        reference = self.register(document)
        return dict(reference=reference, prefab=document,
                    placement=graph.evaluate_base(root_id)['world_matrix'])

    def instantiate(self, graph, reference, identifier, *, parent=None, transform=None, overrides=None):
        finite_json(overrides)
        source = self.resolve(reference)
        require(source['kind'] == 'scene3d-prefab', 'Instantiate a prefab, not an animation.')
        instance = make_node(identifier, 'prefab_instance', parent)
        instance['prefab'] = deepcopy(reference)
        instance['transform'] = deepcopy(transform or TRANSFORM)
        instance['prefab_overrides'] = deepcopy(overrides or dict(parameters={}, materials={}, nodes=[]))
        self._effective(source, instance['prefab_overrides'])
        scene = graph.document()
        scene['nodes'].append(instance)
        scene['dependencies'] = _refs([scene['dependencies'], reference])
        if self._has_fixture(reference):
            scene['provenance']['fixture'] = True
        return graph.replace_document(scene, expected_revision=graph.revision)

    @staticmethod
    def _effective(source, overrides):
        from scene3d_contracts import _prefab_overrides
        from jsonschema import Draft202012Validator
        finite_json(overrides)
        errors = Draft202012Validator({'$defs': definitions(),
            '$ref': '#/$defs/node/properties/prefab_overrides'}).iter_errors(overrides)
        require(next(errors, None) is None, 'Invalid typed prefab override.', 'SCENE3D_PREFAB_OVERRIDE')
        _prefab_overrides(overrides, source)
        result = deepcopy(source)
        nodes = {n['id']: n for n in result['nodes']}
        for override in result['overrides']:
            for key, value in override.items():
                if key != 'node':
                    nodes[override['node']][key] = deepcopy(value)
        parameters = {**result['parameters'], **overrides['parameters']}
        for binding in result.get('parameter_bindings', []):
            value = parameters[binding['parameter']]
            target = nodes[binding['node']]
            if binding['property'] == 'visible':
                target['visible'] = value
            else:
                target['transform'][binding['property']][binding['component']] = value
        covered = set()
        for target in nodes.values():
            for material in target['material_overrides']:
                if material['slot'] in overrides['materials']:
                    material['material'] = deepcopy(overrides['materials'][material['slot']])
                    covered.add(material['slot'])
        require(covered == set(overrides['materials']), 'An exposed material slot has no authored node binding.',
                'SCENE3D_PREFAB_OVERRIDE')
        for override in overrides['nodes']:
            for key, value in override.items():
                if key != 'node':
                    nodes[override['node']][key] = deepcopy(value)
        result['nodes'].extend(deepcopy(overrides.get('children', [])))
        result['roots'] = [n['id'] for n in result['nodes'] if n['parent'] is None]
        result['parameters'] = parameters
        # All simultaneous quaternion/scale bindings are validated as a whole.
        validate_document(result, 'prefab')
        return result

    def set_overrides(self, graph, identifier, overrides):
        scene = graph.document()
        target = next((n for n in scene['nodes'] if n['id'] == identifier), None)
        require(target is not None and target['type'] == 'prefab_instance', 'Choose a prefab instance.')
        graph._editable(identifier)
        self._effective(self.resolve(target['prefab']), overrides)
        target['prefab_overrides'] = deepcopy(overrides)
        return graph.replace_document(scene, expected_revision=graph.revision)

    def reset(self, graph, identifier):
        return self.set_overrides(graph, identifier, dict(parameters={}, materials={}, nodes=[]))

    def compare(self, old_reference, new_reference):
        old, new = self.resolve(old_reference), self.resolve(new_reference)
        require(old['kind'] == new['kind'] == 'scene3d-prefab' and old['id'] == new['id'],
                'Compare versions of the same prefab identity.')
        a, b = {n['id']: n for n in old['nodes']}, {n['id']: n for n in new['nodes']}
        return dict(added=sorted(b.keys()-a.keys()), removed=sorted(a.keys()-b.keys()),
                    changed=sorted(i for i in a.keys() & b.keys() if a[i] != b[i]),
                    parameters_changed=old['parameters'] != new['parameters'],
                    materials_changed=old['material_slots'] != new['material_slots'])

    def upgrade(self, graph, identifier, new_reference, *, resolutions=None):
        scene = graph.document()
        target = next((n for n in scene['nodes'] if n['id'] == identifier), None)
        require(target is not None and target['type'] == 'prefab_instance', 'Choose a prefab instance.')
        graph._editable(identifier)
        old, new = self.resolve(target['prefab']), self.resolve(new_reference)
        comparison = self.compare(target['prefab'], new_reference)
        overrides = deepcopy(target.get('prefab_overrides', dict(parameters={}, materials={}, nodes=[])))
        a, b = {n['id']: n for n in old['nodes']}, {n['id']: n for n in new['nodes']}
        conflicts = []
        for override in overrides['nodes']:
            for field in set(override)-{'node'}:
                if override['node'] not in b or a[override['node']].get(field) != b[override['node']].get(field):
                    conflicts.append('node:'+override['node']+':'+field)
        conflicts += ['parameter:'+key for key in overrides['parameters']
                      if key not in new['parameters'] or old['parameters'].get(key) != new['parameters'][key]]
        conflicts += ['material:'+key for key in overrides['materials'] if key not in new['material_slots']]
        new_ids = set(b)
        conflicts += ['child:'+n['id'] for n in overrides.get('children', [])
                      if n['id'] in new_ids or n['parent'] is not None and n['parent'] not in new_ids |
                      {c['id'] for c in overrides.get('children', [])}]
        conflicts = sorted(set(conflicts))
        resolutions = {} if resolutions is None else resolutions
        require(isinstance(resolutions, dict) and set(resolutions) <= set(conflicts) and
                all(v in ('keep', 'source') for v in resolutions.values()), 'Invalid conflict resolutions.')
        unresolved = [c for c in conflicts if c not in resolutions]
        if unresolved:
            return dict(applied=False, conflicts=conflicts, unresolved=unresolved, comparison=comparison)
        for conflict, choice in resolutions.items():
            parts = conflict.split(':')
            if parts[0] == 'node':
                require(choice == 'source' or parts[1] in b, 'Cannot retain an override of a removed node.')
                if choice == 'source':
                    for override in overrides['nodes']:
                        if override['node'] == parts[1]:
                            override.pop(parts[2], None)
            elif parts[0] == 'parameter':
                require(choice == 'source' or parts[1] in new['parameters'], 'Cannot retain a removed parameter.')
                if choice == 'source': overrides['parameters'].pop(parts[1], None)
            elif parts[0] == 'material':
                require(choice == 'source', 'Cannot retain a removed material slot.')
                overrides['materials'].pop(parts[1], None)
            else:
                require(choice == 'source', 'Conflicting optional children need an explicit separate edit.')
                overrides['children'] = [n for n in overrides.get('children', []) if n['id'] != parts[1]]
        overrides['nodes'] = [o for o in overrides['nodes'] if len(o) > 1 and o['node'] in b]
        self._effective(new, overrides)
        target['prefab'] = deepcopy(new_reference)
        target['prefab_overrides'] = overrides
        if self._has_fixture(new_reference):
            scene['provenance']['fixture'] = True
        result = graph.replace_document(scene, expected_revision=graph.revision)
        return dict(applied=True, conflicts=conflicts, comparison=comparison, **result)

    def expand(self, scene):
        validate_document(scene, 'scene')
        result = deepcopy(scene)
        output, derived, mapping_report = [], {}, {}
        collections = {c['id']: deepcopy(c) for c in scene['collections']}
        layers = {c['id']: deepcopy(c) for c in scene['layers']}
        active = set()
        clip_cache = {}
        work = 0
        def derive_clip(reference, mapping, context, depth):
            if reference['kind'] != 'scene3d-animation':
                return deepcopy(reference)
            require(depth <= MAX_DEPTH, 'Animation derivation exceeds depth bound.', 'SCENE3D_PREFAB_DEPTH')
            key = (reference['sha256'], context)
            if key in clip_cache:
                return deepcopy(clip_cache[key])
            clip = self.resolve(reference)
            require(clip['kind'] == 'scene3d-animation', 'Wrong clip kind.')
            clip['id'] = scoped_id(context, clip['id'], 'clip')
            for track in clip['tracks']:
                require(track['target'] in mapping, 'A prefab clip targets outside its source subtree.',
                        'SCENE3D_PREFAB_EXTERNAL')
                track['target'] = mapping[track['target']]
            for binding in clip['bindings']:
                require(binding['target'] in mapping, 'A nested prefab clip targets outside its source subtree.',
                        'SCENE3D_PREFAB_EXTERNAL')
                binding['target'] = mapping[binding['target']]
                binding['clip'] = derive_clip(binding['clip'], mapping, context, depth+1)
            clip['dependencies'] = _refs([clip['dependencies'], clip['bindings'], reference])
            clip['provenance']['origin'] = 'derived'
            clip['provenance']['tool'] = 'scene3d-prefab-animation-target-map-v1'
            clip['provenance']['inputs'].append(dict(path=reference['path'], sha256=reference['sha256']))
            payload = canonical_bytes(clip)
            path = 'derived-animations/'+clip['id']+'/'+clip['version']+'.json'
            r = _reference(clip, payload, path)
            derived[path] = clip
            clip_cache[key] = r
            return deepcopy(r)
        def append_node(node, context, depth):
            nonlocal work
            require(depth <= MAX_DEPTH, 'Prefab expansion exceeds depth bound.', 'SCENE3D_PREFAB_DEPTH')
            require(len(output) < MAX_NODES, 'Expanded scene exceeds its node budget.', 'SCENE3D_PREFAB_LIMIT')
            if node['type'] != 'prefab_instance':
                output.append(node)
                return
            reference = node['prefab']
            identity = (reference['id'], reference['version'], reference['sha256'])
            require(identity not in active, 'Recursive prefab instance.', 'SCENE3D_PREFAB_CYCLE')
            active.add(identity)
            source = self._effective(self.resolve(reference), node.get('prefab_overrides', dict(parameters={}, materials={}, nodes=[])))
            if source['provenance']['fixture']:
                result['provenance']['fixture'] = True
            work += len(source['nodes'])
            require(work <= MAX_NODES*4, 'Prefab expansion operation budget exceeded.', 'SCENE3D_PREFAB_LIMIT')
            anchor = deepcopy(node)
            anchor['type'] = 'group'
            anchor.pop('prefab')
            anchor.pop('prefab_overrides', None)
            output.append(anchor)
            instance_context = context+'/'+node['id']
            mapping = {n['id']: scoped_id(instance_context, n['id']) for n in source['nodes']}
            require(len(set(mapping.values())) == len(mapping), 'Scoped prefab identifier collision.')
            mapping_report[node['id']] = dict(mapping)
            collection_map, layer_map = {}, {}
            for field, table, remap in (('collections', collections, collection_map), ('layers', layers, layer_map)):
                declared = {c['id']: c for c in source.get(field, [])}
                for n in source['nodes']:
                    for membership in n[field]:
                        require(membership in declared or membership in table,
                                'Prefab membership needs an explicit source or destination collection.')
                for old, value in declared.items():
                    identifier = scoped_id(instance_context, old, field)
                    remap[old] = identifier
                    replacement = {**deepcopy(value), 'id': identifier}
                    require(identifier not in table, 'Scoped collection collision.')
                    table[identifier] = replacement
            for source_node in source['nodes']:
                child = _relink(source_node, mapping)
                if source_node['parent'] is None:
                    child['parent'] = anchor['id']
                child['collections'] = [collection_map.get(c, c) for c in child['collections']]
                child['layers'] = [layer_map.get(c, c) for c in child['layers']]
                for binding in child['animation_bindings']:
                    binding['clip'] = derive_clip(binding['clip'], mapping, instance_context, 0)
                append_node(child, instance_context, depth+1)
            active.remove(identity)
        for original in scene['nodes']:
            append_node(deepcopy(original), '', 0)
        require(len({n['id'] for n in output}) == len(output), 'Expanded node identity collision.')
        result['nodes'] = output
        result['collections'], result['layers'] = list(collections.values()), list(layers.values())
        result['dependencies'] = _refs([scene['dependencies'], output, list(derived.values())])
        _inventories(result)
        validate_document(result, 'scene')
        SceneGraph(result)  # Supported constraint/socket semantics too.
        return dict(scene=result, derived_documents=derived, mappings=mapping_report,
                    original_prefab_references=_refs([n.get('prefab') for n in scene['nodes'] if 'prefab' in n]))

    def detach(self, graph, identifier):
        graph._editable(identifier)
        original = graph.document()
        require(any(n['id'] == identifier and n['type'] == 'prefab_instance' for n in original['nodes']),
                'Choose a prefab instance.')
        # Only the selected instance is flattened; unrelated instances stay linked.
        selected = deepcopy(original)
        nodes = {n['id']: n for n in selected['nodes']}
        selected_ids = set(_subtree(nodes, identifier))
        selected['nodes'] = [n for n in selected['nodes'] if n['id'] in selected_ids]
        selected['nodes'][next(i for i, n in enumerate(selected['nodes']) if n['id'] == identifier)]['parent'] = None
        _inventories(selected)
        expanded = self.expand(selected)
        for n in expanded['scene']['nodes']:
            if n['id'] == identifier:
                n['parent'] = nodes[identifier]['parent']
        replacement = deepcopy(original)
        replacement['provenance']['fixture'] |= expanded['scene']['provenance']['fixture']
        replacement['nodes'] = [n for n in original['nodes'] if n['id'] not in selected_ids] + expanded['scene']['nodes']
        for field in ('collections', 'layers'):
            present = {c['id'] for c in replacement[field]}
            replacement[field].extend(c for c in expanded['scene'][field] if c['id'] not in present)
        replacement['dependencies'] = _refs([replacement['dependencies'], expanded['scene']['dependencies']])
        result = graph.replace_document(replacement, expected_revision=graph.revision)
        return dict(**result, derived_documents=expanded['derived_documents'], mappings=expanded['mappings'])
