"""Pure frame evaluation over the canonical Scene3D and native rig owners.

Scene3D frame intervals are half open; a terminal key may guide interpolation but
is not an output frame. Exact rational arithmetic ends only at numeric/native
sampling. Authored scene, shot, clip and asset documents are never changed.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import bisect
import hashlib
from pathlib import Path

from rig_errors import RigError
from scene3d_contracts import COORDINATES, load_project, validate_document, require
from scene3d_graph import SceneGraph
from scene3d_prefabs import PrefabLibrary


def _rational(value):
    return {'numerator': value.numerator, 'denominator': value.denominator}


def _rate(value):
    return Fraction(value['numerator'], value['denominator'])


def _select(bundle, selector, kind):
    documents = bundle['documents']
    if isinstance(selector, str) and selector in documents:
        value = documents[selector]
        require(value['kind'] == 'scene3d-'+kind, 'Selected document has the wrong kind.')
        return selector, value
    matches = [(path, value) for path, value in documents.items()
               if value['kind'] == 'scene3d-'+kind and value['id'] == selector]
    require(len(matches) == 1, 'Select exactly one '+kind+' by its immutable path or unique ID.')
    return matches[0]


def _sample(track, frame):
    """Sample the existing track contract, using the canonical quaternion math."""
    keys = track['keys']
    right = bisect.bisect_right([key['frame'] for key in keys], frame)
    if right == 0:
        return deepcopy(keys[0]['value'])
    if right == len(keys) or track['interpolation'] == 'step':
        return deepcopy(keys[right-1]['value'])
    a, b = keys[right-1], keys[right]
    fraction = float((frame-a['frame']) / (b['frame']-a['frame']))
    if track['interpolation'] == 'slerp':
        from rig_math import slerp
        return slerp(a['value'], b['value'], fraction).tolist()
    from rig_animation import evaluate_track
    # The native owner interpolates numeric values. Normalized segment times
    # avoid losing an exact key boundary to conversion of a large frame number.
    value = evaluate_track({'times': [0., 1.], 'values': [a['value'], b['value']],
                            'interpolation': 'LINEAR', 'path': track['property']}, fraction)
    return value.tolist()


def _reference_identity(document, path):
    return {'kind': document['kind'], 'id': document['id'],
            'version': document['version'], 'path': path}


def evaluate_scene(project, scene, shot, frame, *, source_root=None,
                   sample_geometry=False, max_track_samples=250000, max_geometry_vertices=2000000):
    """Evaluate a verified project at an exact frame without authoring mutations.

    ``project`` is a project directory or a bundle from ``load_project``. When
    supplying a bundle, ``source_root`` is required for prefabs/native geometry.
    Native geometry sampling is opt-in and returns actual deformed vertex arrays;
    it requires the exact source closure. Renderers can consume ``native_clip``
    directly while importing the same native asset through their own adapter.
    """
    require(type(frame) is int or isinstance(frame, Fraction), 'Frame must be an exact integer or Fraction.')
    frame = Fraction(frame)
    require(type(max_geometry_vertices) is int and 0 < max_geometry_vertices <= 2000000,
            'Invalid evaluated-geometry budget.')
    require(type(max_track_samples) is int and 0 < max_track_samples <= 250000,
            'Invalid evaluated-track budget.')
    if isinstance(project, (str, Path)):
        source_root = Path(project)
        bundle = load_project(source_root)
    else:
        require(isinstance(project, dict) and {'project', 'documents', 'inventory'} <= set(project),
                'Expected a verified project bundle.')
        bundle = project
    scene_path, authored = _select(bundle, scene, 'scene')
    shot_path, authored_shot = _select(bundle, shot, 'shot')
    require(authored_shot['scene']['path'] == scene_path and
            authored_shot['scene']['id'] == authored['id'] and
            authored_shot['scene']['version'] == authored['version'], 'Shot targets another scene.')
    require(0 <= frame < authored_shot['duration_frames'], 'Frame outside half-open shot interval.')
    validate_document(authored, 'scene')
    validate_document(authored_shot, 'shot')
    before_scene, before_shot = deepcopy(authored), deepcopy(authored_shot)
    working = deepcopy(authored)
    documents = deepcopy(bundle['documents'])
    if any('prefab' in node for node in working['nodes']):
        require(source_root is not None, 'Prefab evaluation requires exact source bytes.')
        expansion = PrefabLibrary.from_files(source_root, bundle).expand(working)
        working = expansion['scene']
        documents.update(expansion.get('derived_documents', {}))
    nodes = {node['id']: node for node in working['nodes']}
    native = {}
    samples = 0

    def tracks(value, position):
        nonlocal samples
        for track in value['tracks']:
            samples += 1
            require(samples <= max_track_samples, 'Evaluated-track budget exceeded.', 'SCENE3D_EVALUATION_LIMIT')
            target, prop = track['target'], track['property']
            result = _sample(track, position)
            if prop == 'world_exposure':
                working['world']['exposure'] = result
            else:
                require(target in nodes, 'Track target is absent from expanded scene.')
                node = nodes[target]
                if prop in ('translation', 'rotation', 'scale'):
                    node['transform'][prop] = result
                elif prop == 'visible':
                    node['visible'] = result
                    node['render']['visible'] = result
                elif prop == 'light_intensity':
                    require(node['type'] == 'light', 'Light channel targets a nonlight.')
                    node['light']['intensity'] = result
                elif prop == 'material':
                    matches = [ref for ref in value['dependencies'] if
                               ref['kind'] == 'material' and ref['id']+'@'+ref['version'] == result]
                    require(len(matches) == 1, 'Material key has no exact unique dependency.')
                    # The contract's material track selects the default material
                    # slot; named slots remain explicit instance overrides.
                    node['material_overrides'] = [m for m in node['material_overrides'] if m['slot'] != 'default']
                    node['material_overrides'].append({'slot': 'default', 'material': deepcopy(matches[0])})
                else:
                    raise RigError('SCENE3D_EVALUATION_PROPERTY', 'Unsupported animation property: '+prop)

    def bindings(values, position, owner_rate, stack=()):
        nonlocal samples
        for binding in values:
            samples += 1
            require(samples <= max_track_samples and len(stack) < 128,
                    'Animation evaluation budget exceeded.', 'SCENE3D_EVALUATION_LIMIT')
            if not binding['start_frame'] <= position < binding['end_frame']:
                continue
            reference = binding['clip']
            seconds = (position-binding['start_frame']) / owner_rate * Fraction(str(binding['speed']))
            if reference['kind'] == 'hatch-3d-animation':
                target = binding['target']
                require(target not in native, 'Overlapping native clips have no declared blend policy.')
                native[target] = {'reference': deepcopy(reference), 'time': _rational(seconds), 'loop': binding['loop']}
                continue
            require(reference['path'] not in stack, 'Recursive clip evaluation.')
            require(reference['path'] in documents, 'Clip document is missing.')
            clip = documents[reference['path']]
            validate_document(clip, 'animation')
            clip_frame = seconds * _rate(clip['fps'])
            if binding['loop']:
                clip_frame %= clip['duration_frames']
            else:
                clip_frame = min(clip_frame, Fraction(clip['duration_frames']))
            bindings(clip['bindings'], clip_frame, _rate(clip['fps']), (*stack, reference['path']))
            tracks(clip, clip_frame)

    rate = _rate(authored_shot['fps'])
    for node in working['nodes']:
        bindings(node['animation_bindings'], frame, rate)
    bindings(authored_shot['bindings'], frame, rate)
    # Shot channels are the explicit final override, preserving authored clips.
    tracks(authored_shot, frame)
    for item in authored_shot['temporal_constraints']:
        if item['start_frame'] <= frame < item['end_frame']:
            nodes[item['target']]['constraints'].append(deepcopy(item['constraint']))
    for override in authored_shot['material_overrides']:
        target = nodes[override['target']]
        target['material_overrides'] = [item for item in target['material_overrides']
                                        if item['slot'] != override['slot']]
        target['material_overrides'].append({'slot': override['slot'], 'material': deepcopy(override['material'])})
    camera = nodes[authored_shot['camera']]
    for name, value in authored_shot['camera_overrides'].items():
        camera[name] = deepcopy(value)
    # Bindings have been consumed; the graph owns final transform, attachment and
    # constraint evaluation. Retaining them here would imply a second evaluator.
    for node in working['nodes']:
        node['animation_bindings'] = []
    validate_document(working, 'scene')
    asset_bounds = {d['id']+'@'+d['version']: d['bounds'] for d in documents.values()
                    if d['kind'] == 'scene3d-asset' and d['bounds'] is not None}
    graph = SceneGraph(working, asset_bounds=asset_bounds)
    evaluated = graph.evaluate_base()
    for identifier, state in evaluated.items():
        node = nodes[identifier]
        state.update(type=node['type'], visible=state['render_visible'],
                     medium='projected-2d' if node['type'] == 'billboard_2d' else '3d',
                     material_overrides=deepcopy(node['material_overrides']),
                     base_pose_only=False, animation_evaluated=True)
        for name in ('camera', 'light', 'asset'):
            if name in node:
                state[name] = deepcopy(node[name])
        if identifier in native:
            state['native_clip'] = native[identifier]
            state['native_animation_evaluated'] = bool(sample_geometry)
    # A native binding may own an imported subtree whose individual mesh nodes
    # reference the immutable asset. Propagate only to matching descendants.
    for target, binding in list(native.items()):
        matches = []
        for identifier, node in nodes.items():
            cursor = identifier
            while cursor is not None and cursor != target:
                cursor = nodes[cursor]['parent']
            if cursor != target or 'asset' not in node:
                continue
            asset = documents.get(node['asset']['path'])
            if asset and binding['reference'] in asset['animations']:
                matches.append(identifier)
        require(matches, 'Native binding has no matching asset in its subtree.')
        for identifier in matches:
            require(identifier not in native or native[identifier] == binding,
                    'Native bindings overlap without a declared blend policy.')
            native[identifier] = binding
            evaluated[identifier]['native_clip'] = deepcopy(binding)
            evaluated[identifier]['native_animation_evaluated'] = bool(sample_geometry)
    geometry = _sample_native_geometry(graph, evaluated, documents, native, source_root,
                                       max_geometry_vertices) if sample_geometry else None
    require(authored == before_scene and authored_shot == before_shot, 'Authored scene changed during evaluation.')
    result = {'schema_version': 1, 'coordinate_system': COORDINATES,
              'scene': _reference_identity(authored, scene_path), 'shot': _reference_identity(authored_shot, shot_path),
              'frame': _rational(frame), 'time': _rational(frame/rate), 'camera': authored_shot['camera'],
              'world': deepcopy(working['world']), 'nodes': evaluated, 'authored_mutated': False,
              'track_samples': samples, 'native_geometry_sampled': bool(sample_geometry)}
    if geometry is not None:
        result['native_geometry'] = geometry
    return result


def _sample_native_geometry(graph, evaluated, documents, native, root, max_vertices):
    require(root is not None, 'Native geometry requires the exact source root.')
    from rig_models import load_rig
    from rig_animation import evaluate_animation
    from rig_skinning import evaluate_deformation
    from rig_math import transform_points
    from rig_errors import decode_json
    from asset_library import read_source_bytes
    from scene3d_contracts import portable_path
    import numpy as np
    result, cache, count = {}, {}, 0
    nodes = {node['id']:node for node in graph.document()['nodes']}
    for identifier, binding in native.items():
        node = nodes[identifier]
        if 'asset' not in node:
            continue  # An imported root delegates to matching mesh components.
        require(node['asset']['path'] in documents, 'Native clip requires a registered asset.')
        asset = validate_document(documents[node['asset']['path']], 'asset')
        require(asset['skeleton'] is not None, 'Native clip requires the actual native rig.')
        rig_ref, reference = asset['skeleton'], binding['reference']
        require(reference in asset['animations'], 'Native clip is not part of this asset.')
        rig_path = Path(root)/portable_path(rig_ref['path'])
        payload = read_source_bytes(rig_path, 16*1024*1024)
        require(hashlib.sha256(payload).hexdigest() == rig_ref['sha256'], 'Native rig source changed.')
        clip_payload = read_source_bytes(Path(root)/portable_path(reference['path']), 16*1024*1024)
        require(hashlib.sha256(clip_payload).hexdigest() == reference['sha256'], 'Native clip source changed.')
        time = Fraction(**binding['time'])
        key = (rig_ref['path'], reference['id'], time, binding['loop'])
        if key not in cache:
            rig = load_rig(rig_path.parent)
            require(rig.manifest['id'] == rig_ref['id'] and rig.manifest['version'] == rig_ref['version'],
                    'Native rig identity/version differs from the binding.')
            require(reference['id'] in rig.clips, 'Native clip is absent from the rig.')
            clip = rig.clips[reference['id']]
            pose = evaluate_animation(rig, clip, float(time), loop=binding['loop'])['pose']
            deformed = evaluate_deformation(rig, pose)
            cache[key] = (rig, deformed)
        rig, deformed = cache[key]
        projected_count = sum(len(value['positions']) for value in deformed.values())
        require(count + projected_count <= max_vertices, 'Native output vertex budget exceeded.',
                'SCENE3D_EVALUATION_LIMIT')
        if 'source_component' in node.get('metadata', {}):
            # Retain the importer ownership of source transforms, component maps
            # and canonical bind-space geometry. Its evaluator avoids applying
            # the imported source hierarchy twice.
            from scene3d_import import ImportedScene, evaluate_imported_vertices
            def original(suffix):
                entries = [entry for entry in asset['dependencies'] if entry['path'].endswith(suffix)]
                require(len(entries) == 1, 'Imported asset is missing its exact '+suffix+' record.')
                entry = entries[0]
                data = read_source_bytes(Path(root)/portable_path(entry['path']),16*1024*1024)
                require(hashlib.sha256(data).hexdigest() == entry['sha256'], 'Imported authoring source changed.')
                return decode_json(data)
            report, hierarchy = original('/import-report.json'), original('/hierarchy.json')
            imported = ImportedScene({},node['asset'],asset,[],hierarchy['nodes'],report)
            class ComponentView:
                def document(self):
                    return {'nodes':[node]}
                def evaluate_base(self, selected):
                    return graph.evaluate_base(selected)
            sampled = evaluate_imported_vertices(ComponentView(),imported,rig_path.parent,
                clip=reference['id'],time=float(time),loop=binding['loop'])
            positions = sampled['nodes'][identifier]
        else:
            world = np.asarray(evaluated[identifier]['world_matrix'])
            positions = {mesh:transform_points(world,value['positions']).tolist() for mesh,value in deformed.items()}
        count += sum(len(value) for value in positions.values())
        require(count <= max_vertices, 'Native output vertex budget exceeded.', 'SCENE3D_EVALUATION_LIMIT')
        result[identifier] = positions
    return result


def camera_matrices(evaluation, render):
    """Map Scene3D camera optics into the canonical Camera3D projection owner.

    A vertical sensor gate of 24 mm represents the native vertical field of view
    exactly; no physical sensor size is inferred from imported pixels. Nonrigid
    inherited camera transforms are rejected by the camera owner.
    """
    import math
    from camera3d_math import projection_matrix, world_transform
    from rig_math import decompose
    state = evaluation['nodes'][evaluation['camera']]
    camera = state['camera']
    require(type(render.get('width')) is int and type(render.get('height')) is int and
            0 < render['width'] <= 8192 and 0 < render['height'] <= 8192,
            'Camera raster must be bounded integer dimensions.')
    projection = dict(type=camera['projection'], near_clip_m=camera['near'], far_clip_m=camera['far'],
                      sensor_width_mm=36, sensor_height_mm=24, sensor_fit='vertical',
                      shift_x=0, shift_y=0)
    if camera['projection'] == 'perspective':
        projection['focal_length_mm'] = 12 / math.tan(camera['vertical_fov']/2)
    else:
        projection['orthographic_height_m'] = camera['orthographic_height']
    optics = dict(schema_version=1,kind='camera3d-optics',id='scene-camera-optics',version='1.0.0',
        projection=projection, depth_of_field=dict(enabled=False,focus_distance_m=1,
            f_stop=2.8,aperture_blades=7,aperture_rotation_rad=0,anamorphic_ratio=1))
    raster = dict(schema_version=1,kind='camera3d-render',id='scene-camera-render',version='1.0.0',
        width=render['width'],height=render['height'],pixel_aspect=dict(numerator=1,denominator=1),
        gate_fit='crop',overscan=1,max_pixels=render['width']*render['height'],
        transparent=render.get('transparent',False),color_space=render.get('color_space','srgb'),
        output=render.get('output','png'))
    # Validate physical world transform through the existing camera math owner.
    transform = decompose(state['world_matrix'])
    world = world_transform(transform)
    projection = projection_matrix(optics,raster)
    return dict(world_matrix=world.tolist(),projection_matrix=projection.tolist(),
                optics=optics,render=raster)


def resolve_materials(evaluation, registry):
    """Resolve generic Scene3D material pins through the LookDev registry owner.

    Only the kind name changes at the boundary. Identity/version/path and exact
    source SHA must match before native inheritance is evaluated. This does not
    manufacture a new material, shader graph, texture or visual approval.
    """
    result = {}
    for identifier,state in evaluation['nodes'].items():
        materials = {}
        for override in state['material_overrides']:
            reference = override['material']
            require(reference['kind'] == 'material', 'Scene material reference has wrong kind.')
            native = registry.reference(reference['id'],reference['version'])
            require(native['kind'] in ('material3d','material-instance3d','material-variant3d') and
                    all(native[key] == reference[key] for key in ('id','version','path','sha256')),
                    'Scene material pin differs from the canonical LookDev resource.')
            materials[override['slot']] = registry.effective_material(native)
        if materials:
            result[identifier] = materials
    return result
