"""Read-only technical verification of a registered normalized glTF 2.0 GLB."""
from fractions import Fraction
import hashlib
import math
from pathlib import Path
import re
import tempfile

from asset_library_import import EXTENSIONS
from rig_animation import validate_animation
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError, object_hash, require
from rig_gltf import WIDTHS, _index, preflight_gltf, read_gltf, scene_graph
from rig_import import load_character_model
from rig_interop import validate_gltf
from rig_load_limits import _JSON_FACTOR, effective_limits, image_dimensions
from rig_models import RigAsset
from rig_skeleton import validate_skeleton
from rig_skinning import validate_skin
from scene3d_import import _json

_PAYLOAD = 'normalized.glb'
_CATEGORY_CAPABILITY = {'prop': 'mesh', 'character': 'mesh', 'rig': 'skin',
                        'animation': 'animation', 'material': 'material', 'texture': 'texture'}
_GEOMETRY_FIELDS = ('vertices', 'triangles', 'bounds', 'dimensions_m', 'surface_area_m2')
_MESH_FIELDS = ('vertices', 'triangles', 'components', 'boundary_edges', 'nonmanifold_edges',
                'degenerate_triangles', 'surface_area_m2', 'signed_volume_m3', 'bounds')


def _ratio(seconds):
    value = Fraction.from_float(float(seconds))
    return {'numerator': value.numerator, 'denominator': value.denominator}


def _exact_record(manifest, limits):
    require(not (manifest['dependencies'] or manifest['provenance']['parents']
                 or manifest['metadata']['style_refs']),
            'ASSET_TECHNICAL_UNSUPPORTED', 'This verifier accepts self-contained versions.')
    require(manifest['identity']['kind'] in _CATEGORY_CAPABILITY, 'ASSET_TECHNICAL_UNSUPPORTED',
            'This category has no normalized GLB verification adapter.')
    payloads = [file for file in manifest['files'] if file['role'] == 'payload']
    require(len(payloads) == 1 and payloads[0]['path'] == _PAYLOAD and payloads[0]['format'] == 'glb',
            'ASSET_TECHNICAL_UNSUPPORTED', 'Expected a single normalized GLB payload.')
    record = payloads[0]
    closure_bytes = sum(file['bytes'] for file in manifest['files'])
    require(record['bytes'] <= limits['source_bytes'], 'RIG_LIMIT_SOURCE',
            'Normalized GLB exceeds the source byte limit.')
    require(closure_bytes <= limits['dependency_bytes'], 'RIG_LIMIT_DEPENDENCIES',
            'Registered inventory exceeds the verification byte limit.')
    # The library verifies each own blob against both index and durable storage
    # when enabled. Account for copies there and before parsing GLB JSON.
    reserve = max(4 * max(file['bytes'] for file in manifest['files']),
                  (_JSON_FACTOR + 4) * record['bytes'])
    require(reserve < limits['memory_bytes'], 'RIG_LIMIT_MEMORY',
            'Inventory and GLB JSON exceed the verification working-set estimate.')
    return record


def _require_embedded(payload):
    document = _json(payload, '.glb')
    require(isinstance(document, dict), 'ASSET_TECHNICAL_FORMAT', 'Expected a GLB JSON object.')
    require(set(document.get('extensionsUsed', [])) <= EXTENSIONS,
            'ASSET_TECHNICAL_UNSUPPORTED', 'GLB declares an unsupported extension.')
    for kind in ('buffers', 'images'):
        resources = document.get(kind, [])
        require(isinstance(resources, list) and all(isinstance(item, dict) for item in resources),
                'ASSET_TECHNICAL_FORMAT', 'Expected GLB resource records.')
        require(all('uri' not in item for item in resources), 'ASSET_TECHNICAL_EXTERNAL',
                'Normalized GLB resources must be embedded.')


def _stage_glb(path, payload):
    # Check before either native glTF reader or Khronos can resolve resources.
    _require_embedded(payload)
    path.write_bytes(payload)


def _animation_workspace(document, order, limits):
    body = document.json
    animations = body.get('animations', [])
    require(len(animations) <= limits['clips'], 'RIG_LIMIT_CLIPS',
            'GLB animations exceed the native clip budget.')
    accessors = body.get('accessors', [])
    mesh_instances = {i: len(body['meshes'][body['nodes'][i]['mesh']]['primitives'])
                      for i in order if 'mesh' in body['nodes'][i]}
    keys = scalars = tracks = 0
    for animation in animations:
        samplers = animation['samplers']
        for channel in animation['channels']:
            sampler = samplers[_index(channel['sampler'], len(samplers), 'animation sampler')]
            times = accessors[_index(sampler['input'], len(accessors), 'key times')]
            values = accessors[_index(sampler['output'], len(accessors), 'key values')]
            target = channel['target']
            copies = max(1, mesh_instances.get(target['node'], 0)) if target['path'] == 'weights' else 1
            keys += copies * times['count']
            require(keys <= limits['keyframes'], 'RIG_LIMIT_KEYFRAMES',
                    'Expanded native animation tracks exceed the aggregate key budget.')
            scalars += copies * values['count'] * WIDTHS[values['type']]
            tracks += copies
    # Retained Python float lists may repeat accessor values per target and
    # need room alongside NumPy validation buffers and exact-time summaries.
    return 128 * keys + 64 * scalars + 1024 * tracks


def _generated_workspace(document, order, limits):
    body = document.json
    nodes = body.get('nodes', [])
    skins = body.get('skins', [])
    accessors = body.get('accessors', [])
    may_build_skeleton = bool(skins or body.get('animations'))
    reserve = 0
    for node_index in order:
        node = nodes[node_index]
        if 'mesh' not in node:
            continue
        primitives = body['meshes'][node['mesh']]['primitives']
        joint_matrices = 0
        if 'skin' in node:
            skin = skins[_index(node['skin'], len(skins), 'skin')]
            joint_matrices = len(skin['joints'])
            require(joint_matrices <= limits['bones'], 'RIG_LIMIT_BONES',
                    'Declared skin joints exceed the native bone limit.')
            if 'inverseBindMatrices' in skin:
                matrix_accessor = _index(skin['inverseBindMatrices'], len(accessors), 'inverse bind accessor')
                joint_matrices = max(joint_matrices, accessors[matrix_accessor]['count'])
        # Both import and measurement evaluate the skeleton for each binding.
        # Charge those passes plus per-primitive bind matrix materialization.
        if may_build_skeleton:
            reserve += len(primitives) * (2 * 256 * limits['bones'] + 768 * max(1, joint_matrices))
        for primitive in primitives:
            vertices = accessors[primitive['attributes']['POSITION']]['count']
            missing_positions = sum('POSITION' not in target for target in primitive.get('targets', []))
            reserve += 4 * 24 * vertices * missing_positions
            if may_build_skeleton and 'skin' not in node:
                reserve += 4 * 40 * vertices  # Native rigid joint indices and weights.
    return reserve


def _preflight(path, size, limits):
    document = read_gltf(path, limits)
    raw = preflight_gltf(document, encoded_bytes=size, limits=limits)
    require(1 <= raw['mesh_instances'] <= 4096, 'RIG_GEOMETRY',
            'Expected 1–4096 active mesh primitives.')
    parents, world, order = scene_graph(document)
    require(all(math.isfinite(value) for i in order for value in world[i].flat),
            'ASSET_TECHNICAL_TRANSFORM', 'Selected-scene transforms must be finite.')
    hierarchy = [dict(node=i, parent=parents.get(i), world_matrix=world[i].tolist()) for i in order]
    image_views = [document.view(image['bufferView'])[:3] for image in document.json.get('images', [])]
    image_bytes = sum(size for _, _, size in image_views)
    require(image_bytes <= limits['dependency_bytes'], 'RIG_LIMIT_DEPENDENCIES',
            'Expanded embedded textures exceed the verification byte limit.')
    # Admit simultaneous loader, geometry analysis and native raster allocations.
    # Analysis includes transient topology, vertex and nearest-neighbor arrays.
    reserve = (raw['estimated_memory_bytes'] + 4 * size + image_bytes
               + 256 * raw['triangles'] + 120 * raw['vertices'] + 16 * 1024**2
               + _animation_workspace(document, order, limits)
               + _generated_workspace(document, order, limits))
    require(reserve < limits['memory_bytes'], 'RIG_LIMIT_MEMORY',
            'Native decoding and geometry analysis exceed the verification working-set estimate.')
    decode_limits = dict(limits, memory_bytes=limits['memory_bytes'] - reserve)
    dimensions = []
    total_pixels = 0
    for buffer, offset, length in image_views:
        width, height = image_dimensions(buffer[offset:offset + length])
        pixels = width * height
        total_pixels += pixels
        require(pixels <= 16_777_216 and total_pixels * 16 <= decode_limits['memory_bytes'] // 2,
                'RIG_LIMIT_TEXTURE', 'Embedded textures exceed the native raster admission budget.')
        dimensions.append((width, height))
    return raw, hierarchy, len(document.json.get('materials', [])), dimensions, decode_limits


def _measure_rig(model, limits):
    skeleton = model.skeleton
    summary = None
    if skeleton is not None:
        validate_skeleton(skeleton, limits)
        bones = skeleton['bones']
        summary = dict(bones=len(bones), deform_bones=sum(bone['deform'] for bone in bones),
                       hierarchy_sha256=object_hash([(b['id'], b['parent'], b['deform']) for b in bones]),
                       rest_sha256=object_hash([(b['id'], b['rest'], b['head'], b['tail']) for b in bones]))
    require(skeleton is not None or not model.clips, 'ASSET_TECHNICAL_UNSUPPORTED',
            'Native animation validation requires a skeleton.')
    rig = RigAsset(dict(id='imported', skeleton=skeleton, limits=limits,
                        controllers=dict(ik=[], constraints=[])),
                   model.geometry, model.weights, model.clips)
    skins = []
    rigid_bindings = 0
    for index, mesh in enumerate(model.geometry.meshes):
        if mesh.id in model.weights:
            skin = model.weights[mesh.id]
            metrics = validate_skin(skin, mesh, skeleton, limits)
            if skin.method == 'rigid':
                rigid_bindings += 1
            elif skin.method == 'imported':
                skins.append(dict(mesh=index, joints=len(skin.joints), metrics=metrics,
                                  native_inverse_bind_sha256=hashlib.sha256(skin.inverse_bind.tobytes()).hexdigest()))
            else:
                raise RigError('ASSET_TECHNICAL_SKIN', 'Unknown native GLB binding classification.')
    clips = []
    keys = 0
    for clip in model.clips.values():
        track_keys = sum(len(track['times']) for track in clip['tracks'])
        keys += track_keys
        require(keys <= limits['keyframes'], 'RIG_LIMIT_KEYFRAMES',
                'Expanded native animation tracks exceed the aggregate key budget.')
        metrics = validate_animation(rig, clip)
        tracks = [dict(path=track['path'], interpolation=track['interpolation'],
                       keys=len(track['times']), first_key_exact=_ratio(track['times'][0]),
                       last_key_exact=_ratio(track['times'][-1]),
                       key_times_sha256=object_hash(track['times'])) for track in clip['tracks']]
        source_end = max(track['times'][-1] for track in clip['tracks'])
        clips.append(dict(source_end_exact=_ratio(source_end),
                          native_playback_duration_exact=_ratio(clip['duration']),
                          metrics=metrics, tracks=tracks))
    return summary, skins, rigid_bindings, clips


def _native_report(path, record, limits):
    raw, hierarchy, materials, dimensions, decode_limits = _preflight(path, record['bytes'], limits)
    independent = validate_gltf(path, timeout=min(120, limits['timeout_seconds']))
    require(independent.get('ok') is True and independent['issues']['numErrors'] == 0,
            'ASSET_TECHNICAL_VALIDATION', 'Independent Khronos validation must pass.')
    require(independent['asset_sha256'] == record['sha256'], 'ASSET_TECHNICAL_HASH',
            'Independent validation must check the exact registered bytes.')
    version = independent.get('validator')
    require(isinstance(version, str)
            and re.fullmatch(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(?:-dev(?:\.[0-9]{1,4}){1,4})?', version),
            'ASSET_TECHNICAL_VALIDATION', 'Independent validator version must use a recognized format.')
    model = load_character_model(path, limits=decode_limits)
    require(model.report['source_sha256'] == record['sha256'], 'ASSET_TECHNICAL_HASH',
            'Native import must decode the exact registered bytes.')
    geometry = {key: model.report[key] for key in _GEOMETRY_FIELDS}
    require(geometry['vertices'] == raw['vertices'] and geometry['triangles'] == raw['triangles'],
            'ASSET_TECHNICAL_GEOMETRY', 'Preflight and decoded geometry counts differ.')
    require(max(abs(value) for point in geometry['bounds'] for value in point) <= 1e9,
            'ASSET_TECHNICAL_GEOMETRY', 'Decoded geometry exceeds the supported coordinate domain.')
    meshes = []
    for mesh, metrics in zip(model.geometry.meshes, model.report['meshes'], strict=True):
        meshes.append(dict({key: metrics[key] for key in _MESH_FIELDS},
                           source_node=mesh.source_node, material_slot=mesh.material,
                           morph_targets=len(mesh.morphs), texcoord_sets=sum(
                               key.startswith('TEXCOORD_') for key in mesh.attributes)))
    textures = []
    for image, (width, height) in zip(model.geometry.images, dimensions, strict=True):
        textures.append(dict(width=width, height=height, mime_type=image.mime_type,
                             sha256=hashlib.sha256(image.data).hexdigest()))
    skeleton, skins, rigid_bindings, clips = _measure_rig(model, limits)
    capabilities = ['mesh']
    for capability, observed in (('material', materials), ('texture', textures), ('skin', skins),
                                 ('animation', clips), ('morph', any(m['morph_targets'] for m in meshes))):
        if observed:
            capabilities.append(capability)
    return dict(geometry=geometry, meshes=meshes, hierarchy=hierarchy, authored_materials=materials,
                textures=textures, skeleton=skeleton, skins=skins, native_rigid_bindings=rigid_bindings, clips=clips,
                capabilities=sorted(capabilities),
                independent_validation=dict(status='passed', version=version, asset_sha256=record['sha256'],
                                            errors=0, warnings=independent['issues']['numWarnings']))


def validate_asset(library, reference, *, limits=None):
    """Measure native technical properties of an exact AssetLibrary GLB version."""
    stage = 'asset-technical-validation'
    try:
        profile = effective_limits({**DEFAULT_LIMITS, 'influences': 8}, limits)
        manifest = library.inspect(reference, verify_payloads=False)
        record = _exact_record(manifest, profile)
        payload = library.read_file(reference, _PAYLOAD)
        require(len(payload) == record['bytes'] and hashlib.sha256(payload).hexdigest() == record['sha256'],
                'ASSET_TECHNICAL_HASH', 'Inspected manifest and returned payload must match.')
        with tempfile.TemporaryDirectory(prefix='hatch-asset-validation-') as directory:
            path = Path(directory) / _PAYLOAD
            _stage_glb(path, payload)
            del payload
            result = _native_report(path, record, profile)
        require(_CATEGORY_CAPABILITY[manifest['identity']['kind']] in result['capabilities'],
                'ASSET_TECHNICAL_CATEGORY', 'Decoded content does not satisfy the registered category.')
        return dict(schema_version=1, kind='asset-library-technical-report',
                    reference=dict(reference), payload={key: record[key] for key in ('path', 'bytes', 'sha256')},
                    limits=profile, status='technically-verified', visual_review='unreviewed',
                    production_approved=False, **result)
    except RigError as exc:
        code = exc.report['code']
        counts = {key: exc.report[key] for key in ('expected', 'observed')
                  if type(exc.report[key]) is int}
        raise RigError(code, f'Cannot technically verify {_PAYLOAD} ({code}).', stage=stage,
                       asset=_PAYLOAD, retryable=exc.report['retryable'], **counts) from None
    except MemoryError:
        raise RigError('RIG_LIMIT_MEMORY', 'Cannot allocate verification working memory.',
                       stage=stage, asset=_PAYLOAD) from None
    except OSError:
        raise RigError('ASSET_TECHNICAL_IO', 'Cannot access the verification payload or tools.',
                       stage=stage, asset=_PAYLOAD) from None
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, OverflowError, RecursionError):
        raise RigError('ASSET_TECHNICAL_FORMAT', 'Cannot interpret the verification payload or tool response.',
                       stage=stage, asset=_PAYLOAD) from None
