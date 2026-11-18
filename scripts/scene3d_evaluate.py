"""Pure frame evaluation over the canonical Scene3D and native rig owners.

Scene3D frame intervals are half open; a terminal key may guide interpolation but
is not an output frame. Exact rational arithmetic ends only at numeric/native
sampling. Authored scene, shot, clip and asset documents are never changed.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import bisect
from pathlib import Path, PurePosixPath

from rig_errors import RigError
from scene3d_contracts import COORDINATES, load_project, validate_document, require
from scene3d_graph import SceneGraph
from scene3d_prefabs import PrefabLibrary

MAX_NATIVE_CACHE_VERTICES = 2000000
MAX_NATIVE_MATCH_VISITS = 250000


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
    Native geometry sampling is opt-in and returns actual deformed vertices
    from an exact privately captured closure. ``native_geometry_status`` reports
    whether geometry was sampled, omitted by request, or had no active binding.
    A node's ``native_animation_evaluated`` is true only for sampled geometry.
    This API supplies scene state and pins. Camera projection and material
    resolution have separate entry points, and no render is performed.
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
    before_scene, before_shot = deepcopy(authored), deepcopy(authored_shot)
    working = validate_document(authored, 'scene')
    shot_document = validate_document(authored_shot, 'shot')
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
            clip = validate_document(documents[reference['path']], 'animation')
            require(clip['id'] == reference['id'] and clip['version'] == reference['version'],
                    'Clip document differs from the binding identity.')
            clip_frame = seconds * _rate(clip['fps'])
            if binding['loop']:
                clip_frame %= clip['duration_frames']
            else:
                clip_frame = min(clip_frame, Fraction(clip['duration_frames']))
            bindings(clip['bindings'], clip_frame, _rate(clip['fps']), (*stack, reference['path']))
            tracks(clip, clip_frame)

    rate = _rate(shot_document['fps'])
    for node in working['nodes']:
        bindings(node['animation_bindings'], frame, rate)
    bindings(shot_document['bindings'], frame, rate)
    # Shot channels are the explicit final override, preserving authored clips.
    tracks(shot_document, frame)
    for item in shot_document['temporal_constraints']:
        if item['start_frame'] <= frame < item['end_frame']:
            nodes[item['target']]['constraints'].append(deepcopy(item['constraint']))
    for override in shot_document['material_overrides']:
        target = nodes[override['target']]
        target['material_overrides'] = [item for item in target['material_overrides']
                                        if item['slot'] != override['slot']]
        target['material_overrides'].append({'slot': override['slot'], 'material': deepcopy(override['material'])})
    camera = nodes[shot_document['camera']]
    for name, value in shot_document['camera_overrides'].items():
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
                     channels=deepcopy(node['transform']),
                     medium='projected-2d' if node['type'] == 'billboard_2d' else '3d',
                     material_overrides=deepcopy(node['material_overrides']),
                     base_pose_only=False, animation_evaluated=True)
        for name in ('camera', 'light', 'asset'):
            if name in node:
                state[name] = deepcopy(node[name])
        if identifier in native:
            state['native_clip'] = native[identifier]
            state['native_animation_evaluated'] = False
    children = {identifier: [] for identifier in nodes}
    for identifier, node in nodes.items():
        if node['parent'] is not None:
            children[node['parent']].append(identifier)

    # A native binding can select descendants of an imported group. Visit only
    # that subtree and bound repeated work across overlapping group selections.
    node_order = {identifier: index for index, identifier in enumerate(nodes)}
    match_visits = 0
    for target, binding in list(native.items()):
        require(target in nodes, 'Native binding target is absent from the expanded scene.')
        matches = []
        pending = [target]
        while pending:
            identifier = pending.pop()
            match_visits += 1
            require(match_visits <= MAX_NATIVE_MATCH_VISITS,
                    'Native binding traversal budget exceeded.', 'SCENE3D_EVALUATION_LIMIT')
            pending.extend(children[identifier])
            node = nodes[identifier]
            asset = documents.get(node['asset']['path']) if 'asset' in node else None
            if asset and binding['reference'] in asset['animations']:
                matches.append(identifier)
        require(matches, 'Native binding has no matching asset in its subtree.')
        for identifier in sorted(matches, key=node_order.__getitem__):
            require(identifier not in native or native[identifier] == binding,
                    'Native bindings overlap without a declared blend policy.')
            native[identifier] = binding
            evaluated[identifier]['native_clip'] = deepcopy(binding)
            evaluated[identifier]['native_animation_evaluated'] = False
    geometry = _sample_native_geometry(nodes, evaluated, documents, native, source_root,
                                       max_geometry_vertices,bundle['inventory']) if sample_geometry else None
    for identifier in geometry or {}:
        evaluated[identifier]['native_animation_evaluated'] = True
    for state in evaluated.values():
        state['native_bounds_evaluated'] = True
    if not sample_geometry:
        for identifier,state in evaluated.items():
            if 'native_clip' in state:
                cursor = identifier
                while cursor is not None and evaluated[cursor]['native_bounds_evaluated']:
                    evaluated[cursor]['native_bounds_evaluated'] = False
                    cursor = nodes[cursor]['parent']
    if geometry is not None:
        # Bind-pose asset bounds cannot describe a deformed frame. Publish the
        # sampled world-space bounds alongside the vertices for spatial users.
        import numpy as np
        for identifier, meshes in geometry.items():
            arrays = [np.asarray(points) for points in meshes.values() if len(points)]
            if arrays:
                evaluated[identifier]['own_world_bounds'] = dict(
                    minimum=np.min([points.min(axis=0) for points in arrays], axis=0).tolist(),
                    maximum=np.max([points.max(axis=0) for points in arrays], axis=0).tolist())
        def bounds(identifier):
            boxes = [evaluated[identifier]['own_world_bounds']]
            boxes.extend(bounds(child) for child in children[identifier])
            boxes = [box for box in boxes if box is not None]
            result = (dict(minimum=np.min([box['minimum'] for box in boxes], axis=0).tolist(),
                           maximum=np.max([box['maximum'] for box in boxes], axis=0).tolist())
                      if boxes else None)
            evaluated[identifier]['world_bounds'] = result
            return result
        for identifier, node in nodes.items():
            if node['parent'] is None:
                bounds(identifier)
    require(authored == before_scene and authored_shot == before_shot, 'Authored scene changed during evaluation.')
    result = {'schema_version': 1, 'coordinate_system': COORDINATES,
              'scene': _reference_identity(authored, scene_path), 'shot': _reference_identity(authored_shot, shot_path),
              'frame': _rational(frame), 'time': _rational(frame/rate), 'camera': authored_shot['camera'],
              'world': deepcopy(working['world']), 'nodes': evaluated, 'authored_mutated': False,
              'track_samples': samples, 'native_geometry_sampled': bool(geometry),
              'native_geometry_status': ('sampled' if geometry else
                                         'not_requested' if native else 'no_active_bindings'),
              'render_evaluated': False}
    if geometry is not None:
        result['native_geometry'] = geometry
    return result


def _sample_native_geometry(nodes, evaluated, documents, native, root, max_vertices, inventory):
    if not native:
        return {}
    require(root is not None, 'Native geometry requires the exact source root.')
    from scene3d_capture import capture_verified_files
    from scene3d_contracts import portable_path

    def pin(reference):
        path = portable_path(reference['path'])
        record = inventory.get(path)
        require(record is not None and record['sha256'] == reference['sha256'],
                'Native source pin differs from the verified inventory.', 'SCENE3D_SOURCE_CHANGED')
        require(type(record.get('bytes')) is int and 0 <= record['bytes'] <= 16*1024*1024,
                'Native referenced JSON exceeds its bounded source size.', 'SCENE3D_EVALUATION_LIMIT')
        return PurePosixPath(path)

    jobs, assets, rig_roots, metadata = [], {}, set(), {}
    for identifier, binding in native.items():
        node = nodes[identifier]
        if 'asset' not in node:
            continue
        asset_path = node['asset']['path']
        if asset_path not in assets:
            require(asset_path in documents, 'Native clip requires a registered asset.')
            assets[asset_path] = validate_document(documents[asset_path], 'asset')
        asset = assets[asset_path]
        rig_ref, clip_ref = asset['skeleton'], binding['reference']
        require(rig_ref is not None and clip_ref in asset['animations'],
                'Native clip requires a registered clip on the actual native rig.')
        rig_path, clip_path = pin(rig_ref), pin(clip_ref)
        require(rig_path.name == 'rig.json' and rig_path.parent in clip_path.parents,
                'Native clip must belong to its registered rig directory.')
        rig_roots.add(rig_path.parent)
        if 'source_component' in node['metadata'] and asset_path not in metadata:
            paths = []
            for suffix in ('/import-report.json', '/hierarchy.json'):
                entries = [entry for entry in asset['dependencies'] if entry['path'].endswith(suffix)]
                require(len(entries) == 1, 'Imported asset is missing its exact '+suffix+' record.')
                paths.append(pin(entries[0]).as_posix())
            metadata[asset_path] = paths
        jobs.append((node, asset, rig_ref, clip_ref, binding))

    # Admit a union of registered files. Overlapping rig trees and component
    # fanout never multiply the shared capture byte or file accounting.
    metadata_paths = {path for paths in metadata.values() for path in paths}
    selected = {path: record for path, record in inventory.items()
                if path in metadata_paths or any(parent in rig_roots for parent in PurePosixPath(path).parents)}
    with capture_verified_files(root, selected) as admitted:
        return _sample_admitted_native(evaluated, jobs, metadata, admitted, max_vertices)


def _sample_admitted_native(evaluated, jobs, metadata, admitted, max_vertices):
    from rig_models import load_rig
    from rig_contracts import DEFAULT_LIMITS
    from rig_animation import evaluate_animation
    from rig_skinning import evaluate_deformation
    from rig_math import transform_points
    from rig_errors import decode_json
    from asset_library import read_source_bytes
    from scene3d_import import ImportedScene, project_imported_vertices
    import numpy as np

    class ComponentView:
        def __init__(self, node):
            self.node = node

        def document(self):
            return {'nodes': [self.node]}

        def evaluate_base(self, selected):
            # The importer reads only the world matrix from this canonical state.
            return evaluated[selected]

    result, cache, rigs, imported_metadata, clip_pins = {}, {}, {}, {}, set()
    rig_vertices = sampled_vertices = output_vertices = 0
    identity = ('kind', 'id', 'version', 'path', 'sha256')
    for node, asset, rig_ref, reference, binding in jobs:
        identifier = node['id']
        rig_path = PurePosixPath(rig_ref['path'])
        time = Fraction(**binding['time'])
        # Cache only complete immutable references: another instance may carry
        # the same clip ID/path but an invalid version or hash that still needs
        # its own admission check before native geometry can be reused.
        rig_key = tuple(rig_ref[field] for field in identity)
        clip_key = (rig_key, tuple(reference[field] for field in identity))
        key = (clip_key, time, binding['loop'])
        if rig_key not in rigs:
            remaining = MAX_NATIVE_CACHE_VERTICES - rig_vertices
            require(remaining > 0, 'Native rig input vertex budget exceeded.', 'SCENE3D_EVALUATION_LIMIT')
            rig = load_rig(admitted/rig_path.parent, expected_manifest_sha256=rig_ref['sha256'],
                           limits={'vertices': min(DEFAULT_LIMITS['vertices'], remaining)})
            require(rig.manifest['id'] == rig_ref['id'] and rig.manifest['version'] == rig_ref['version'],
                    'Native rig identity/version differs from the binding.')
            size = sum(len(mesh.positions) for mesh in rig.geometry.meshes)
            require(size <= remaining,
                    'Native rig input vertex budget exceeded.', 'SCENE3D_EVALUATION_LIMIT')
            rigs[rig_key] = (rig, size, rig.geometry.by_id())
            rig_vertices += size
        rig, size, by_mesh = rigs[rig_key]
        if clip_key not in clip_pins:
            descriptor = next((item for item in rig.manifest['animations'] if item['id'] == reference['id']), None)
            require(descriptor is not None and reference['id'] in rig.clips and
                    reference['version'] == descriptor['version'] and reference['sha256'] == descriptor['sha256'] and
                    PurePosixPath(reference['path']) == rig_path.parent/descriptor['path'],
                    'Native clip pin differs from the actual rig animation.')
            clip_pins.add(clip_key)
        component = node['metadata'].get('source_component')
        mesh_ids = asset['components'].get(component, ()) if component is not None else by_mesh
        output_vertices += sum(len(by_mesh[mesh].positions) for mesh in mesh_ids if mesh in by_mesh)
        require(output_vertices <= max_vertices, 'Native output vertex budget exceeded.', 'SCENE3D_EVALUATION_LIMIT')
        if key not in cache:
            require(sampled_vertices + size <= MAX_NATIVE_CACHE_VERTICES,
                    'Native deformation cache vertex budget exceeded.', 'SCENE3D_EVALUATION_LIMIT')
            clip = rig.clips[reference['id']]
            pose = evaluate_animation(rig, clip, float(time), loop=binding['loop'])['pose']
            deformed = evaluate_deformation(rig, pose)
            cache[key] = deformed
            sampled_vertices += size
        deformed = cache[key]
        if 'source_component' in node.get('metadata', {}):
            # Retain the importer ownership of source transforms, component maps
            # and canonical bind-space geometry. Its evaluator avoids applying
            # the imported source hierarchy twice.
            asset_path = node['asset']['path']
            if asset_path not in imported_metadata:
                report, hierarchy = (decode_json(read_source_bytes(admitted/path, 16*1024*1024))
                                     for path in metadata[asset_path])
                imported_metadata[asset_path] = (report, hierarchy['nodes'])
            report, hierarchy = imported_metadata[asset_path]
            imported = ImportedScene({}, node['asset'], asset, [], hierarchy, report)
            sampled = project_imported_vertices(ComponentView(node),imported,rig,deformed)
            positions = sampled['nodes'][identifier]
        else:
            world = np.asarray(evaluated[identifier]['world_matrix'])
            positions = {mesh:transform_points(world,value['positions']).tolist() for mesh,value in deformed.items()}
        result[identifier] = positions
    return result


def camera_matrices(evaluation, render):
    """Map Scene3D camera optics into the canonical Camera3D projection owner.

    A vertical sensor gate of 24 mm represents the native vertical field of view
    exactly; no physical sensor size is inferred from imported pixels. Nonrigid
    inherited camera transforms are rejected by the camera owner.
    """
    from camera3d_math import projection_matrix, world_transform, scene_camera_optics
    state = evaluation['nodes'][evaluation['camera']]
    camera = state['camera']
    require(type(render.get('width')) is int and type(render.get('height')) is int and
            0 < render['width'] <= 8192 and 0 < render['height'] <= 8192,
            'Camera raster must be bounded integer dimensions.')
    optics = scene_camera_optics(camera)
    raster = dict(schema_version=1,kind='camera3d-render',id='scene-camera-render',version='1.0.0',
        width=render['width'],height=render['height'],pixel_aspect=dict(numerator=1,denominator=1),
        gate_fit='crop',overscan=1,max_pixels=render['width']*render['height'],
        transparent=render.get('transparent',False),color_space=render.get('color_space','srgb'),
        output=render.get('output','png'))
    # Validate physical world transform through the existing camera math owner.
    world = world_transform(dict(translation=[0,0,0], rotation=[0,0,0,1], scale=[1,1,1]),
                            parent=state['world_matrix'])
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


def frame_camera(evaluation, render, targets, **options):
    """Report why automated framing is unavailable for the evaluated targets."""
    require(isinstance(targets,(list,tuple)) and 1 <= len(targets) <= 128 and
            all(isinstance(target,str) and target in evaluation['nodes'] for target in targets),
            'Framing requires 1..128 evaluated node IDs.')
    require(all(evaluation['nodes'][target].get('native_bounds_evaluated', False) for target in targets),
            'Framing native deformation requires sample_geometry=True for the evaluated frame.',
            'SCENE3D_EVALUATION_GEOMETRY')
    raise RigError('SCENE3D_EVALUATION_UNSUPPORTED',
                   'Automated camera framing requires the Camera3D constraints owner.')
