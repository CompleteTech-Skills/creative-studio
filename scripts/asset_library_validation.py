"""Exact-byte technical inspection of registered assets using native owners.

This module never repairs assets, publishes versions, or supplies visual approval.
The library owns byte identity; native rig and Scene3D owners own their semantics.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import hashlib
import io
from pathlib import Path
import tempfile
import uuid

from asset_library_contracts import portable_path
from rig_errors import RigError, decode_json, object_hash

VERSION = '1.0.0'
MAX_CLOSURE = 256


def fail(code, message, path=None):
    raise RigError('LIBRARY_TECHNICAL_' + code, message, asset=path)


def _limits(requested):
    from rig_contracts import DEFAULT_LIMITS
    value = dict(DEFAULT_LIMITS)
    value['influences'] = 8
    if requested is not None:
        if not isinstance(requested, dict) or set(requested) - set(value):
            fail('LIMIT', 'Only native resource limits may be tightened.')
        for key, limit in requested.items():
            if type(limit) is not int or not 1 <= limit <= value[key]:
                fail('LIMIT', 'Resource limits must be positive integers no larger than the native profile.')
            value[key] = limit
    return value


def _ratio(value):
    number = Fraction.from_float(float(value))
    return {'numerator': number.numerator, 'denominator': number.denominator}


def inspect_geometry(path, *, limits=None):
    """Capture untrusted source bytes through the importer before inspection."""
    from asset_library_import import SourceSnapshot, dependency_errors
    with tempfile.TemporaryDirectory(prefix='hatch-technical-source-') as directory:
        profile = _limits(limits)
        from rig_load_limits import LoadBudget
        budget = LoadBudget(profile)
        snapshot = SourceSnapshot(path, Path(directory) / 'source',
                                  maximum_bytes=min(96 * 1024**2, profile['dependency_bytes'],
                                                    profile['memory_bytes'] // 4), budget=budget)
        result = dependency_errors(_inspect_geometry)(snapshot.root / snapshot.entry, limits=profile, budget=budget)
        result['source_files'] = {name: {'sha256': hashlib.sha256(payload).hexdigest(),
                                         'bytes': len(payload)}
                                  for name, payload in sorted(snapshot.files.items())}
        return result


def _inspect_geometry(path, *, limits=None, budget=None):
    from rig_load_limits import LoadBudget
    limits = _limits(limits)
    parent = budget if budget is not None else LoadBudget(limits)
    child = parent.fork(limits)
    result = _inspect_geometry_admitted(path, limits=child.limits, budget=child)
    parent.commit(child)
    return result


def _inspect_geometry_admitted(path, *, limits, budget):
    """Inspect a private verified snapshot; native owners retain decoding."""
    import numpy as np
    from rig_animation import validate_animation
    from rig_geometry import analyze_geometry
    from rig_import import load_character_model
    from rig_models import RigAsset, validate_geometry
    from rig_skinning import validate_skin
    from rig_skeleton import validate_skeleton
    from asset_library_import import _check_gltf

    limits = _limits(limits)
    path = Path(path)
    document = None
    independent = {'status': 'not-applicable', 'reason': 'OBJ geometry has no glTF validator or animation contract.'}
    if path.suffix.lower() in ('.glb', '.gltf'):
        # Bound accessors and graph depth before the native model loader allocates
        # decoded meshes or recursively composes the transform hierarchy.
        document, independent = _check_gltf(path, limits, budget=budget)
    model = load_character_model(path, limits=limits, budget=budget)
    geometry = model.geometry
    validate_geometry(geometry, limits)
    if any(np.max(np.abs(mesh.positions)) > 1e9 for mesh in geometry.meshes):
        fail('COORDINATE', 'Coordinates exceed the supported billion-meter domain.')
    analysis = analyze_geometry(geometry, limits)
    warnings = []
    meshes = []
    allocated = 0
    for mesh, metrics in zip(geometry.meshes, analysis['meshes']):
        if np.max(np.abs(mesh.positions)) > 1e9:
            fail('COORDINATE', 'Coordinates exceed the supported billion-meter domain.', mesh.id)
        attributes = {}
        for name, values in sorted(mesh.attributes.items()):
            attributes[name] = {'components': int(values.shape[1]), 'vertices': len(values),
                                'sha256': hashlib.sha256(values.tobytes()).hexdigest()}
            allocated += values.nbytes
            if name.startswith('TEXCOORD_'):
                attributes[name]['minimum'] = values.min(axis=0).tolist()
                attributes[name]['maximum'] = values.max(axis=0).tolist()
            if name in ('NORMAL', 'TANGENT'):
                error = np.abs(np.linalg.norm(values[:, :3], axis=1) - 1)
                attributes[name]['max_unit_error'] = float(error.max())
                if np.any(error > 1e-4):
                    warnings.append({'code': 'DIRECTION_LENGTH', 'asset': mesh.id, 'attribute': name,
                                     'message': 'Direction vectors are not unit length; no repair was performed.'})
        duplicates = len(mesh.positions) - len(np.unique(mesh.positions, axis=0))
        for code, count in [('DUPLICATE_VERTICES', duplicates),
                            ('DEGENERATE_TRIANGLES', metrics['degenerate_triangles']),
                            ('NONMANIFOLD_EDGES', metrics['nonmanifold_edges'])]:
            if count:
                warnings.append({'code': code, 'asset': mesh.id, 'count': int(count),
                                 'message': 'Repairable topology observation; source remains unchanged.'})
        material = geometry.materials[mesh.material]
        if 'normalTexture' in material and 'TANGENT' not in mesh.attributes:
            warnings.append({'code': 'TANGENTS_UNAVAILABLE', 'asset': mesh.id,
                             'message': 'Normal-mapped material requires renderer tangent generation.'})
        morphs = []
        for name, channels in sorted(mesh.morphs.items()):
            morphs.append({'name': name, 'channels': sorted(channels),
                           'vertices': len(mesh.positions),
                           'sha256': object_hash({k: hashlib.sha256(v.tobytes()).hexdigest()
                                                  for k, v in sorted(channels.items())})})
            allocated += sum(v.nbytes for v in channels.values())
        allocated += mesh.positions.nbytes + mesh.triangles.nbytes
        meshes.append({**metrics, 'material_slot': int(mesh.material), 'source_node': mesh.source_node,
                       'attributes': attributes, 'morphs': morphs, 'duplicate_vertices': duplicates})
    textures = []
    from PIL import Image
    for image in geometry.images:
        with Image.open(io.BytesIO(image.data)) as decoded:
            decoded.load()
            textures.append({'name': image.name, 'mime_type': image.mime_type,
                             'width': decoded.width, 'height': decoded.height,
                             'sha256': hashlib.sha256(image.data).hexdigest()})
        allocated += len(image.data)
    skeleton = None
    skins = []
    clips = []
    if model.skeleton:
        validate_skeleton(model.skeleton, limits)
        skeleton = {'bones': len(model.skeleton['bones']),
                    'hierarchy_sha256': object_hash([{'id': b['id'], 'parent': b['parent'],
                                                       'deform': b['deform']} for b in model.skeleton['bones']]),
                    'rest_sha256': object_hash([{'id': b['id'], 'rest': b['rest'],
                                                 'head': b['head'], 'tail': b['tail']}
                                                for b in model.skeleton['bones']]),
                    'bone_ids': [b['id'] for b in model.skeleton['bones']]}
        for key, skin in sorted(model.weights.items()):
            measured = validate_skin(skin, geometry.by_id()[key], model.skeleton, limits)
            skins.append({'mesh': key, 'joints': list(skin.joints), 'metrics': measured,
                          'inverse_bind_sha256': hashlib.sha256(skin.inverse_bind.tobytes()).hexdigest(),
                          'binding_sha256': object_hash({'joints': skin.joints,
                              'indices': hashlib.sha256(skin.indices.tobytes()).hexdigest(),
                              'weights': hashlib.sha256(skin.weights.tobytes()).hexdigest()})})
    if model.weights and not model.skeleton:
        fail('ANIMATION', 'Skins have no validated native skeleton.')
    rig = RigAsset({'id': 'imported', 'skeleton': model.skeleton, 'limits': limits,
                    'controllers': {'ik': [], 'constraints': []}}, geometry, model.weights, model.clips)
    budget.animation_report(key=uuid.uuid4().hex + ':inspection report',
                            keys=sum(len(track['times']) for clip in model.clips.values()
                                     for track in clip['tracks']),
                            tracks=sum(len(clip['tracks']) for clip in model.clips.values()))
    for name, clip in sorted(model.clips.items()):
        measured = validate_animation(rig, clip)
        clips.append({'id': name, 'duration_seconds': clip['duration'],
                      'duration_exact': _ratio(clip['duration']), 'metrics': measured,
                      'root_motion': clip['root_motion'], 'loop': clip['loop'],
                      'fps': clip['fps'], 'fps_origin': 'native import default; not source sampling rate',
                      'tracks': [{'target': t['target'], 'path': t['path'],
                                  'interpolation': t['interpolation'],
                                  'key_times_exact': [_ratio(v) for v in t['times']]}
                                 for t in clip['tracks']]})
    hierarchy = None
    if document is not None:
        from rig_gltf import scene_graph
        parents, worlds, order = scene_graph(document)
        hierarchy = {'nodes': len(document.json.get('nodes', [])), 'selected_nodes': len(order),
                     'parents': {str(k): v for k, v in sorted(parents.items())},
                     'world_matrices': {str(k): worlds[k].tolist() for k in order},
                     'scenes': deepcopy(document.json.get('scenes', [])),
                     'cameras': len(document.json.get('cameras', [])),
                     'extensions_used': list(document.json.get('extensionsUsed', []))}
    return {'kind': 'geometry', 'format': path.suffix[1:].lower(),
            'geometry': {k: v for k, v in analysis.items() if k != 'meshes'}, 'meshes': meshes,
            'materials': deepcopy(geometry.materials),
            'authored_materials': (len(document.json.get('materials', [])) if document is not None
                                   else max(0, len(geometry.materials) - 1)),
            'textures': textures,
            'skeleton': skeleton, 'skins': skins, 'clips': clips, 'hierarchy': hierarchy,
            'estimated_geometry_bytes': allocated, 'estimated_working_bytes': allocated * 4,
            'independent_validation': independent, 'warnings': warnings,
            'limitations': ['No visual approval or renderer execution is inferred.',
                            'Open/nonmanifold/duplicate topology is reported without destructive repair.',
                            'OBJ carries geometry/material data only; no rig or animation preservation claim.']}


def _scene_documents(root, manifest, payloads, geometry, limits):
    """Verify typed Scene3D references against exact registered content and owners."""
    from scene3d_contracts import all_refs, validate_document, _binding_context, _prefab_overrides
    from scene3d_math import local_matrix
    import numpy as np
    documents = {}
    for name, payload in payloads.items():
        if not name.lower().endswith('.json'):
            continue
        value = decode_json(payload)
        if isinstance(value, dict) and str(value.get('kind', '')).startswith('scene3d-'):
            validate_document(value)
            if value['kind'] not in ('scene3d-asset', 'scene3d-scene', 'scene3d-prefab', 'scene3d-animation'):
                fail('UNSUPPORTED', 'No technical consumption adapter for document kind: ' + value['kind'], name)
            documents[name] = value
    edges = {}
    reports = []
    identities = {}
    for name, document in documents.items():
        identity = (document['kind'], document['id'], document['version'])
        if identity in identities and identities[identity] != name:
            fail('IDENTITY', 'Multiple files declare the same native identity.', name)
        identities[identity] = name
    binding_context = dict(seen=set(), checks=0)
    for name, document in documents.items():
        edges[name] = []
        for ref in all_refs(document):
            target = ref['path']
            portable_path(target)
            if target not in payloads or hashlib.sha256(payloads[target]).hexdigest() != ref['sha256']:
                fail('CLOSURE', 'Scene3D dependency is absent or differs from its exact digest.', target)
            if 'kind' in ref and ref['kind'].startswith('scene3d-'):
                child = documents.get(target)
                if child is None or any(child.get(k) != ref[k] for k in ('kind', 'id', 'version')):
                    fail('REFERENCE', 'Typed Scene3D reference differs from its actual document.', target)
                edges[name].append(target)
            elif ref.get('kind') == 'hatch-3d-rig':
                from rig_models import load_rig
                from rig_skeleton import validate_skeleton
                from rig_ik import validate_controllers
                from rig_skinning import validate_skin
                from rig_animation import validate_animation
                from rig_models import validate_geometry
                if Path(target).name != 'rig.json':
                    fail('UNSUPPORTED', 'Native rig owner requires its canonical rig.json closure.', target)
                rig = load_rig(root / Path(target).parent)
                if rig.manifest['id'] != ref['id'] or rig.manifest['version'] != ref['version']:
                    fail('REFERENCE', 'Native rig identity differs from declared scene binding.', target)
                validate_geometry(rig.geometry, limits)
                validate_skeleton(rig.skeleton, limits)
                validate_controllers(rig.skeleton, rig.manifest['controllers'])
                for mesh, skin in rig.weights.items():
                    validate_skin(skin, rig.geometry.by_id()[mesh], rig.skeleton, limits)
                for clip in rig.clips.values():
                    validate_animation(rig, clip)
            elif ref.get('kind') == 'hatch-3d-animation':
                from rig_contracts import validate_contract
                clip = validate_contract(decode_json(payloads[target]), 'AnimationClip')
                if clip['id'] != ref['id'] or clip['version'] != ref['version']:
                    fail('REFERENCE', 'Native clip identity differs from declared scene binding.', target)
                fail('UNSUPPORTED', 'Detached native clips require an explicit bound rig before technical verification.', target)
    # Admit every document closure before following nested consumption bindings.
    for name, document in documents.items():
        if document['kind'] == 'scene3d-asset':
            content = document['content']['path']
            suffix = Path(content).suffix.lower()
            if suffix not in ('.glb', '.gltf', '.obj'):
                fail('UNSUPPORTED', 'Scene asset needs an implemented geometry payload adapter.', content)
            if content not in geometry:
                geometry[content] = _inspect_geometry(root / content, limits=limits)
            measured = geometry[content]['geometry']
            expected = document['resources']
            if (expected['bytes'] != len(payloads[content]) or expected['vertices'] != measured['vertices']
                    or expected['triangles'] != measured['triangles']):
                fail('RESOURCES', 'Declared scene mesh resources differ from actual geometry.', name)
            if document['bounds'] is not None:
                declared = [document['bounds']['minimum'], document['bounds']['maximum']]
                if not np.allclose(declared, measured['bounds'], rtol=1e-6, atol=1e-8):
                    fail('BOUNDS', 'Declared scene bounds differ from actual geometry.', name)
            for component, mesh_ids in document['components'].items():
                if not set(mesh_ids) <= {m['id'] for m in geometry[content]['meshes']}:
                    fail('COMPONENT', 'Scene component names unavailable native mesh identifiers.', component)
        if document['kind'] in ('scene3d-scene', 'scene3d-prefab'):
            # Reuse the Scene3D consumer validators: a schema-valid binding or
            # override is not necessarily meaningful in the consuming graph.
            for node in document['nodes']:
                for binding in node['animation_bindings']:
                    _binding_context(binding, document, documents, binding_context)
                if 'prefab_overrides' in node:
                    _prefab_overrides(node['prefab_overrides'], documents[node['prefab']['path']])
                if 'asset' in node:
                    asset = documents[node['asset']['path']]
                    if asset['asset_type'] in ('material', 'texture'):
                        fail('CATEGORY', 'Geometry node references a non-geometric asset.', node['id'])
                    if node['type'] == 'skinned_character' and asset['asset_type'] != 'character':
                        fail('CATEGORY', 'Skinned character must reference a character asset.', node['id'])
            if document['kind'] == 'scene3d-scene' and document['world']['environment'] is not None:
                environment = document['world']['environment']
                if environment['kind'] == 'scene3d-asset' and documents[environment['path']]['asset_type'] != 'environment':
                    fail('CATEGORY', 'World environment must reference an environment asset.', name)
            nodes = {node['id']: node for node in document['nodes']}
            transforms = {}
            def evaluate(identifier):
                # Iterative composition remains safe for user-controlled deep
                # node trees; validation below also enforces a useful depth cap.
                chain = []
                active = set()
                current = identifier
                while current is not None and current not in transforms:
                    if current in active or len(chain) >= 128:
                        fail('RECURSION', 'Scene hierarchy has a cycle or exceeds 128 levels.', identifier)
                    active.add(current)
                    chain.append(current)
                    current = nodes[current]['parent']
                for current in reversed(chain):
                    node = nodes[current]
                    matrix = local_matrix(node['transform'])
                    if node['parent'] is not None:
                        matrix = transforms[node['parent']] @ matrix
                    if not np.isfinite(matrix).all():
                        fail('TRANSFORM', 'Composed node transform is nonfinite.', current)
                    transforms[current] = matrix
            for identifier in nodes:
                evaluate(identifier)
            reports.append({'path': name, 'kind': document['kind'], 'id': document['id'],
                            'node_count': len(document['nodes']),
                            'world_matrices': {key: value.tolist() for key, value in transforms.items()},
                            'sockets': list(document.get('sockets', []))})
    active = set()
    heights = {}
    def visit(name, depth=0):
        if depth >= 128 or name in active:
            fail('RECURSION', 'Scene/prefab dependency graph has a cycle or exceeds depth.', name)
        if name in heights:
            return heights[name]
        active.add(name)
        height = 1
        for child in edges[name]:
            height = max(height, visit(child, depth + 1) + 1)
            if height > 128:
                fail('RECURSION', 'Scene/prefab dependency graph exceeds 128 levels.', name)
        active.remove(name)
        heights[name] = height
        return height
    for name in documents:
        visit(name)
    return reports


def validate_asset(library, reference, *, limits=None, requirements=None):
    """Inspect an exact immutable library version and its registered native content.

    Requirements may select validated mesh/rig/animation/material capabilities or
    exact measured skeleton/rest signatures. They are never matched to asset names.
    """
    limits = _limits(limits)
    requirements = {} if requirements is None else requirements
    allowed = {'capabilities', 'skeleton_hierarchy_sha256', 'skeleton_rest_sha256'}
    if not isinstance(requirements, dict) or set(requirements) - allowed:
        fail('REQUIREMENTS', 'Unknown technical compatibility requirement.')
    wanted = requirements.get('capabilities', [])
    if not isinstance(wanted, list) or len(wanted) > 16 or any(type(x) is not str for x in wanted):
        fail('REQUIREMENTS', 'Capabilities must be a bounded string list.')
    supported = {'mesh', 'skin', 'animation', 'morph', 'material', 'texture', 'scene', 'prefab'}
    if set(wanted) - supported:
        fail('REQUIREMENTS', 'Unknown technical capability.')
    manifest = library.resolve(reference)
    if len(manifest['files']) > MAX_CLOSURE:
        fail('LIMIT', 'Version file closure exceeds validation budget.')
    expected_total = sum(record['bytes'] for record in manifest['files'])
    if expected_total > min(limits['dependency_bytes'], limits['memory_bytes'] // 4):
        fail('LIMIT', 'Registered closure exceeds the bounded validation snapshot working set.')
    from rig_load_limits import LoadBudget
    budget = LoadBudget(limits)
    payloads = {}
    total = 0
    for record in manifest['files']:
        budget.captured_input(key=uuid.uuid4().hex, size=record['bytes'])
        payload = library.read_file(reference, record['path'])
        total += len(payload)
        if total > limits['dependency_bytes']:
            fail('LIMIT', 'Version bytes exceed the validation budget.')
        payloads[record['path']] = payload
    results = {}
    with tempfile.TemporaryDirectory(prefix='hatch-asset-validation-') as directory:
        root = Path(directory)
        for name, payload in payloads.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        for record in manifest['files']:
            if record['role'] in ('runtime', 'geometry') and record['format'] in ('glb', 'gltf', 'obj'):
                results[record['path']] = _inspect_geometry(root / record['path'], limits=limits, budget=budget)
        scenes = _scene_documents(root, manifest, payloads, results, limits)
    if not results and not scenes:
        fail('UNSUPPORTED', 'No supported runtime geometry or Scene3D payload was registered.')
    capabilities = set()
    for result in results.values():
        capabilities.add('mesh')
        if result['skins']: capabilities.add('skin')
        if result['clips']: capabilities.add('animation')
        if result['authored_materials']: capabilities.add('material')
        if result['textures']: capabilities.add('texture')
        if any(mesh['morphs'] for mesh in result['meshes']): capabilities.add('morph')
    for scene in scenes:
        capabilities.add('prefab' if scene['kind'] == 'scene3d-prefab' else 'scene')
    if set(wanted) - capabilities:
        fail('COMPATIBILITY', 'Required actual capabilities are absent: ' + ', '.join(sorted(set(wanted) - capabilities)))
    for field, measured in [('skeleton_hierarchy_sha256', 'hierarchy_sha256'), ('skeleton_rest_sha256', 'rest_sha256')]:
        if field in requirements:
            expected = requirements[field]
            if type(expected) is not str or len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
                fail('REQUIREMENTS', 'Skeleton signatures must be lowercase SHA-256 digests.')
            signatures = {item['skeleton'][measured] for item in results.values() if item['skeleton']}
            if signatures != {expected}:
                fail('COMPATIBILITY', 'Actual skeleton structure/rest differs from the required signature.')
    kind = manifest['identity']['kind']
    needed = {'character': 'mesh', 'prop': 'mesh', 'rig': 'skin', 'animation': 'animation',
              'material': 'material', 'texture': 'texture',
              'prefab': 'prefab', 'environment': 'scene'}.get(kind)
    if needed is None:
        fail('UNSUPPORTED', 'No technical validation adapter for asset category: ' + kind)
    if needed not in capabilities:
        fail('CATEGORY', 'Actual payload does not satisfy the registered asset category: ' + kind)
    return {'schema_version': 1, 'kind': 'asset-library-technical-report', 'tool_version': VERSION,
            'reference': deepcopy(reference), 'manifest_sha256': manifest['manifest_sha256'],
            'limits': limits, 'file_hashes': {n: hashlib.sha256(v).hexdigest() for n, v in sorted(payloads.items())},
            'capabilities': sorted(capabilities), 'geometry': results, 'scene_documents': scenes,
            'bytes': total, 'status': 'technically-verified', 'visual_review': 'unreviewed',
            'production_approved': False, 'publication_changed': False}
