"""Inspect exact GLB/glTF source closures through the canonical native readers."""
from collections import defaultdict
from contextlib import ExitStack, contextmanager
from copy import deepcopy
import hashlib
import importlib.metadata
from pathlib import Path
import platform
import tempfile

from continuity_contracts import canonical_bytes, digest, definitions, portable_path, _read_regular_bytes
from rig_errors import RigError, open_payload

CAPS = dict(source_bytes=8*1024**2, dependency_bytes=32*1024**2,
            vertices=100000, triangles=100000, bones=256, influences=4,
            morphs=32, clips=16, keyframes=100000, memory_bytes=256*1024**2)
MAX_FILES = 256


class _InspectionError(ValueError):
    def __init__(self, code, message):
        self.code = 'CONTINUITY_INSPECTION_' + code
        super().__init__(f'{self.code}: {message}')


def fail(code, text):
    raise _InspectionError(code, text)


def _limits(requested):
    from rig_contracts import DEFAULT_LIMITS
    result = dict(DEFAULT_LIMITS, **CAPS)
    if requested is not None:
        if not isinstance(requested, dict) or set(requested)-set(CAPS):
            fail('LIMIT', 'only documented restrictive native limits are accepted')
        for key, value in requested.items():
            if type(value) is not int or not 1 <= value <= CAPS[key]:
                fail('LIMIT', 'limits must be positive integers no greater than host caps')
            result[key] = value
    return result


def _references(source, dependencies):
    from jsonschema import Draft202012Validator
    validator = Draft202012Validator({'$defs': definitions(), '$ref': '#/$defs/ref'})
    if not isinstance(dependencies, (list, tuple)) or len(dependencies) >= MAX_FILES:
        fail('LIMIT', 'source closure must contain at most 256 files')
    supplied = [source, *dependencies]
    canonical_bytes(supplied)
    refs = deepcopy(supplied)
    seen = set()
    for item in refs:
        if not validator.is_valid(item):
            fail('REFERENCE', 'exact id/version/path/hash/kind references are required')
        path = portable_path(item['path'])
        key = path.casefold()
        if key in seen:
            fail('REFERENCE', 'duplicate or portable-alias source path')
        seen.add(key)
    return refs


def _read_closure(root, root_identity, refs, limits, snapshot=None):
    """Hash the exact bytes read from the owning descriptor; optionally copy them."""
    from rig_load_limits import _JSON_FACTOR
    total = 0
    closure_cap = min(limits['dependency_bytes'], limits['memory_bytes'] // _JSON_FACTOR)
    for ref in refs:
        relative = ref['path']
        maximum = min(limits['source_bytes'], closure_cap - total)
        try:
            with ExitStack() as scope:
                stream, info = scope.enter_context(open_payload(root, relative, root_identity=root_identity))
                if info.st_size > maximum:
                    fail('LIMIT', 'encoded closure exceeds its byte budget: ' + relative)
                if snapshot is not None:
                    target = snapshot / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    output = scope.enter_context(target.open('xb'))
                digestor = hashlib.sha256()
                size = 0
                while chunk := stream.read(min(1024 * 1024, maximum - size + 1)):
                    size += len(chunk)
                    if size > maximum:
                        fail('LIMIT', 'encoded closure exceeds its byte budget: ' + relative)
                    digestor.update(chunk)
                    if snapshot is not None:
                        output.write(chunk)
                if digestor.hexdigest() != ref['sha256']:
                    fail('HASH', 'source bytes disagree with the exact reference: ' + relative)
                total += size
        except RigError as exc:
            if exc.report['code'] != 'RIG_PAYLOAD_IO':
                raise
            raise RigError('RIG_PAYLOAD_IO', 'Cannot safely read the pinned payload.', asset=relative) from exc
    return total


@contextmanager
def _loaded(root, root_identity, source, dependencies, requested):
    from rig_gltf import read_gltf, scene_graph, node_matrix, preflight_gltf
    from rig_import import load_character_model

    limits = _limits(requested)
    refs = _references(source, dependencies)
    path_name = refs[0]['path']
    if Path(path_name).suffix.lower() not in ('.glb', '.gltf') or refs[0]['kind'] == 'hatch-3d-rig':
        fail('ADAPTER_REQUIRED', 'inspection requires a GLB or glTF source')
    with tempfile.TemporaryDirectory(prefix='continuity-inspect-') as scratch:
        snapshot = Path(scratch).resolve()
        total = _read_closure(root, root_identity, refs, limits, snapshot)
        path = snapshot / path_name
        try:
            document = read_gltf(path, limits)
            if any(len(document.json.get(name, [])) > cap
                   for name, cap in [('nodes', 512), ('materials', 256), ('images', 64)]):
                fail('LIMIT', 'bounded inspector report inventory exceeded')
            admission = preflight_gltf(document, encoded_bytes=total, limits=limits)
            images = _image_admission(document, admission, limits)
            expected = {ref['path'] for ref in refs}
            observed = {entry.relative_to(snapshot).as_posix() for entry in document.dependencies}
            if expected != observed:
                fail('CLOSURE', 'supplied pins differ from native source closure: ' +
                     ', '.join(sorted(expected ^ observed)))
            parents, world, order = scene_graph(document)
            animations = _animation_admission(document, images, limits, order)
            nodes = document.json['nodes']
            hierarchy = [dict(node_id=i, parent=parents.get(i),
                              local_matrix=node_matrix(nodes[i]).tolist(), world_matrix=world[i].tolist(),
                              mesh=nodes[i].get('mesh'), skin=nodes[i].get('skin')) for i in order]
            features = _source_features(document)
            del document
            model = load_character_model(path, limits=limits)
            model.report.update(source_features=features, source_preflight=admission, image_preflight=images,
                                animation_preflight=animations)
            yield model, hierarchy, refs, limits, total
        except MemoryError:
            fail('LIMIT', 'native allocation failed inside admitted inspection')
        except (KeyError, IndexError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
            fail('NATIVE_DOCUMENT', 'native structure is invalid: ' + type(exc).__name__)
        finally:
            _read_closure(root, root_identity, refs, limits)


def _image_admission(document,geometry_admission,limits):
    """Use native image framing and native load accounting before decoding."""
    from rig_load_limits import LoadBudget,image_dimensions
    budget=LoadBudget(limits)
    retained=geometry_admission['estimated_memory_bytes']
    budget.add(retained-budget.memory,'retained native geometry/metadata admission')
    budget.input_bytes=geometry_admission['input_bytes']
    budget.json_bytes=geometry_admission['json_bytes']
    budget.array_bytes=geometry_admission['array_bytes']
    images=[]
    for i,item in enumerate(document.json.get('images',[])):
        if 'uri' in item:
            payload=document.uri(item['uri'])
        else:
            buffer,offset,length,_=document.view(item['bufferView'])
            payload=buffer[offset:offset+length]
        width,height=image_dimensions(payload)
        rgba=width*height*4
        # Same canonical decoded working-copy accounting as scan_bundle;
        # full PNG/JPEG integrity/pixel decoding stays with rig_import.
        budget.texture_rgba_bytes+=rgba
        budget.add(rgba*4,'image-'+str(i)+': decoded texture working set')
        images.append(dict(index=i,width=width,height=height,encoded_bytes=len(payload),
                           sha256=hashlib.sha256(payload).hexdigest()))
    return dict(budget.report(),method='native-gltf-image-capacity-v1',images=images,
                scope='native image headers and combined geometry/decoded-texture capacity; not pixel integrity')


def _animation_admission(document, image_admission, limits, order):
    """Charge native retained tracks before any animation accessor expansion."""
    from rig_errors import require
    from rig_gltf import WIDTHS, _index
    from rig_load_limits import LoadBudget

    budget = LoadBudget(limits)
    budget.add(image_admission['estimated_memory_bytes'] - budget.memory,
               'retained native geometry/image admission')
    for field in ('input_bytes', 'json_bytes', 'array_bytes', 'texture_rgba_bytes'):
        setattr(budget, field, image_admission[field])
    body = document.json
    nodes, meshes, accessors = body.get('nodes', []), body.get('meshes', []), body.get('accessors', [])
    active = set(order)
    animations = body.get('animations', [])
    require(isinstance(animations, list), 'RIG_GLTF_PREFLIGHT', 'Animations must be an array.')
    require(len(animations) <= limits['clips'], 'RIG_LIMIT_CLIPS', 'Imported animation clip budget exceeded.')
    channels = destinations = source_keys = instantiated_keys = retained = working = 0
    for animation in animations:
        budget.add(4096, 'native animation clip metadata')
        retained += 4096
        samplers = animation['samplers']
        for channel in animation.get('channels', []):
            sampler = samplers[_index(channel['sampler'], len(samplers), 'animation sampler')]
            target = channel['target']
            node_id = _index(target['node'], len(nodes), 'animation target')
            path = target['path']
            require(path in ('translation', 'rotation', 'scale', 'weights'),
                    'RIG_ANIMATION_PATH', 'Unsupported animation target.')
            source = accessors[_index(sampler['input'], len(accessors), 'animation input')]
            output = accessors[_index(sampler['output'], len(accessors), 'animation output')]
            # Match the native reader's ravel(), even for malformed non-scalar
            # time accessors; validation of actual samples remains native.
            keys = source['count'] * WIDTHS[source['type']]
            scalars = output['count'] * WIDTHS[output['type']]
            source_keys += keys
            require(source_keys <= limits['keyframes'], 'RIG_LIMIT_KEYFRAMES',
                    'Animation key budget exceeded.')
            interpolation = sampler.get('interpolation', 'LINEAR')
            require(interpolation in ('STEP', 'LINEAR', 'CUBICSPLINE'),
                    'RIG_INTERPOLATION', 'Unsupported interpolation.')
            copies = 1
            if path == 'weights':
                require(node_id in active and 'mesh' in nodes[node_id], 'RIG_ANIMATION_REFERENCE',
                        'Animation references unavailable geometry.')
                mesh = meshes[_index(nodes[node_id]['mesh'], len(meshes), 'animation mesh')]
                copies = len(mesh['primitives'])
                require(copies > 0, 'RIG_ANIMATION_REFERENCE', 'Animation references unavailable geometry.')
            # rig_import._animations retains one times list and one nested
            # value list per destination, plus both tangent lists for cubic.
            # 64 bytes per scalar includes a Python float and its list slot;
            # 128 per list and 4096 per track cover containers/metadata.
            rows = 3 if interpolation == 'CUBICSPLINE' else 1
            track_bytes = 4096 + 128 + keys * 64 + rows * (128 + keys * 128) + scalars * 64
            retained_bytes = copies * track_bytes
            # Float64 conversions and transformed working copies are charged
            # in addition to the unique-accessor estimate, across all channels.
            working_bytes = (keys + scalars) * 8 * 4
            budget.add(retained_bytes + working_bytes, 'instantiated native animation tracks')
            budget.array_bytes += working_bytes
            retained += retained_bytes
            working += working_bytes
            channels += 1
            destinations += copies
            instantiated_keys += copies * keys
    return dict(budget.report(), method='native-gltf-animation-capacity-v1',
                scope='combined geometry/images and native animation lists/working arrays; not process RSS',
                channels=channels, destinations=destinations, source_keyframes=source_keys,
                instantiated_keyframes=instantiated_keys, animation_retained_bytes=retained,
                animation_working_bytes=working)


def _source_features(document):
    """Retain original extension/channel coverage, including unsupported inputs."""
    from rig_gltf import MATERIAL_EXTENSIONS
    body=document.json
    used=body.get('extensionsUsed',[])
    required=body.get('extensionsRequired',[])
    if any(not isinstance(v,list) or len(v)>128 or any(not isinstance(x,str) or not 1<=len(x)<=128 for x in v) for v in (used,required)):
        fail('NATIVE_DOCUMENT','invalid glTF extension inventory')
    unsupported=set(used)-MATERIAL_EXTENSIONS
    containers=[('root',body)]
    for category in ('nodes','meshes','textures','samplers','accessors','skins','animations'):
        containers.extend((category,item) for item in body.get(category,[]))
    for category,item in containers:
        extensions=item.get('extensions',{})
        if not isinstance(extensions,dict):
            fail('NATIVE_DOCUMENT','invalid extension object')
        unsupported.update(category+'/'+name for name in extensions)
    material_extensions = set(used) | set(required)
    for material in body.get('materials', []):
        declared = set(material.get('extensions', {}))
        material_extensions.update(declared)
        unsupported.update(declared - MATERIAL_EXTENSIONS)
    # The projector captures unlit presence and texture transforms. Other
    # recognized material extensions have semantics beyond the observed slots.
    partial_material_extensions = ((material_extensions & MATERIAL_EXTENSIONS)
                                   - {'KHR_materials_unlit', 'KHR_texture_transform'})
    channels=[dict(animation=i,target_node=c['target']['node'],path=c['target']['path'])
              for i,a in enumerate(body.get('animations',[])) for c in a.get('channels',[])]
    return dict(extensions_used=sorted(set(used) & MATERIAL_EXTENSIONS),
                extensions_required=sorted(set(required) & MATERIAL_EXTENSIONS),
                unsupported_extensions=sorted(digest(name) for name in unsupported),
                partial_material_extensions=sorted(partial_material_extensions),
                animation_channels=channels)


def _array(array):
    import numpy as np
    value = np.asarray(array)
    if not np.isfinite(value).all():
        fail('NUMERICAL', 'native arrays must contain finite observations')
    # Explicit portable scalar representation, independent of container encoding.
    canonical = np.asarray(value, dtype='<f8').copy()
    canonical[canonical == 0] = 0
    return dict(shape=list(value.shape), sha256=hashlib.sha256(canonical.tobytes()).hexdigest(),
                encoding='little-endian-float64; negative zero normalized')


def _surface(mesh, scale=1.):
    """Quantized triangle coordinates, independent of winding and ordering."""
    import numpy as np
    values = np.round(mesh.positions[mesh.triangles]/scale, 9)
    values[values == 0] = 0
    # Sorting vertices ignores triangle winding for this shape fingerprint only;
    # original indices/normals retain separate lossless array fingerprints.
    order = np.lexsort((values[:,:,2], values[:,:,1], values[:,:,0]), axis=1)
    flat = np.take_along_axis(values, order[:,:,None], axis=1).reshape(-1,9).astype('<f8')
    if len(flat):
        flat = flat[np.lexsort(tuple(flat[:, i] for i in reversed(range(9))))]
    return hashlib.sha256(flat.tobytes()).hexdigest()


def _measure(geometry, mesh_ids, landmarks, distances, height):
    import numpy as np
    if not isinstance(landmarks, dict) or len(landmarks) > 256 or not isinstance(distances, dict) or len(distances) > 256:
        fail('LANDMARK', 'bounded named vertex landmarks and pair distances are required')
    points = {}
    meshes = {mesh_ids[mesh.id]: mesh for mesh in geometry.meshes}
    for name, item in landmarks.items():
        if not isinstance(name, str) or not 1 <= len(name) <= 128 or not isinstance(item, dict) or set(item) != {'mesh_id', 'vertex_index'}:
            fail('LANDMARK', 'landmark must identify an actual mesh vertex')
        mesh = meshes.get(item['mesh_id'])
        index = item['vertex_index']
        if mesh is None or type(index) is not int or not 0 <= index < len(mesh.positions):
            fail('LANDMARK', 'landmark vertex does not exist')
        points[name] = dict(position_m=mesh.positions[index].tolist(), binding=deepcopy(item),
                            uncertainty='vertex location measured; anatomical naming is authored, not inferred')
    values = {}
    for name, pair in distances.items():
        if not isinstance(name, str) or len(name) > 128 or not isinstance(pair, list) or len(pair) != 2 or any(not isinstance(x,str) or x not in points for x in pair):
            fail('LANDMARK', 'distance must bind two declared vertex landmarks')
        value = float(np.linalg.norm(np.asarray(points[pair[0]]['position_m'])-points[pair[1]]['position_m']))
        values[name] = dict(landmarks=pair, distance_m=value, relative_to_height=value/height if height > 1e-9 else None)
    return dict(landmarks=points, distances=values)


def _material_inputs(material):
    """Project the canonical validator's observed shader fields."""
    from rig_models import _MATERIAL_TEXTURE_SLOTS

    def select(obj, fields):
        return {key: obj[key] for key in fields if key in obj}

    def binding(slot, info):
        value = select(info, ('index', 'texCoord'))
        if slot in ('normalTexture', 'clearcoatNormalTexture') and 'scale' in info:
            value['scale'] = info['scale']
        if slot == 'occlusionTexture' and 'strength' in info:
            value['strength'] = info['strength']
        transform = info.get('extensions', {}).get('KHR_texture_transform')
        if transform is not None:
            value['extensions'] = {'KHR_texture_transform': select(transform, ('offset', 'scale', 'rotation', 'texCoord'))}
        return value

    def bindings(obj, slots):
        return {slot: binding(slot, obj[slot]) for slot in slots if slot in obj}

    observed = select(material, ('emissiveFactor', 'alphaCutoff', 'alphaMode', 'doubleSided'))
    observed.update(bindings(material, ('normalTexture', 'occlusionTexture', 'emissiveTexture')))
    if 'pbrMetallicRoughness' in material:
        pbr = material['pbrMetallicRoughness']
        pbr_inputs = dict(select(pbr, ('baseColorFactor', 'metallicFactor', 'roughnessFactor')),
                          **bindings(pbr, ('baseColorTexture', 'metallicRoughnessTexture')))
        if pbr_inputs:
            observed['pbrMetallicRoughness'] = pbr_inputs
    extensions = material.get('extensions', {})
    observed_extensions = {name: bindings(extensions[name], slots)
                           for name, slots in _MATERIAL_TEXTURE_SLOTS.items() if name in extensions}
    if 'KHR_materials_unlit' in extensions:
        observed_extensions['KHR_materials_unlit'] = {}
    if observed_extensions:
        observed['extensions'] = observed_extensions
    return observed


def _admitted_root(root):
    root = Path(root).absolute()
    try:
        info = root.stat()
    except OSError as exc:
        raise RigError('RIG_DIRECTORY_PATH', 'Inspection root is unavailable.') from exc
    return root, (info.st_dev, info.st_ino)


def _color_spaces(materials):
    """Annotate glTF slot semantics; texture bytes themselves have no shader role."""
    result=[]
    srgb={'baseColorTexture','emissiveTexture','specularColorTexture','sheenColorTexture'}
    def visit(value,path):
        if not isinstance(value,dict):
            return
        for name,child in value.items():
            current=path+[name]
            if name.endswith('Texture') and isinstance(child,dict) and 'index' in child:
                transform = child.get('extensions', {}).get('KHR_texture_transform', {})
                texcoord = transform.get('texCoord', child.get('texCoord', 0))
                result.append(dict(slot_path=current,texture=child['index'],texcoord=texcoord,
                    color_space='sRGB color channels; alpha linear' if name in srgb else 'linear-data',
                    basis='portable glTF slot semantics; renderer implementation unverified'))
            else:
                visit(child,current)
    for i,material in enumerate(materials):
        visit(material,[i])
    return result


def inspect_asset(root, source, *, dependencies=(), landmarks=None, distances=None, limits=None, variants=()):
    """Inspect exact file closure. No source mutation or automatic approvals."""
    root, root_identity = _admitted_root(root)
    return _inspect_asset(root, root_identity, source, dependencies=dependencies, landmarks=landmarks,
                          distances=distances, limits=limits, variants=variants)


def _inspect_asset(root, root_identity, source, *, dependencies=(), landmarks=None, distances=None, limits=None, variants=()):
    if not isinstance(variants,(list,tuple)) or len(variants)>64:
        fail('REFERENCE','variants must be a bounded list of existing exact references')
    canonical_bytes(dict(landmarks=landmarks,distances=distances,variants=variants))
    landmarks,distances,variants=deepcopy((landmarks,distances,variants))
    with _loaded(root, root_identity, source, dependencies, limits) as (model, hierarchy, refs, caps, encoded):
        source=refs[0]
        from rig_geometry import analyze_geometry
        geometry = model.geometry
        analysis = analyze_geometry(geometry, caps)
        height = analysis['dimensions_m'][1]
        meshes = []
        primitive_counts = defaultdict(int)
        mesh_ids = {}
        for mesh in geometry.meshes:
            primitive_index = primitive_counts[mesh.source_node]
            primitive_counts[mesh.source_node] += 1
            mesh_id = f'mesh-{mesh.source_node}-{primitive_index}'
            mesh_ids[mesh.id] = mesh_id
            morph_ids = {name: digest(name) for name in mesh.morphs}
            meshes.append(dict(id=mesh_id,
                source_node=mesh.source_node, primitive_index=primitive_index, positions=_array(mesh.positions), triangles=_array(mesh.triangles),
                attributes={key:_array(value) for key,value in mesh.attributes.items()},
                morphs={morph_ids[name]:{key:_array(value) for key,value in fields.items()} for name,fields in mesh.morphs.items()},
                morph_defaults={morph_ids[name]: weight for name, weight in mesh.morph_defaults.items()},
                material_slot=mesh.material, geometry_sha256=_surface(mesh),
                scale_normalized_sha256=_surface(mesh, height) if height > 1e-9 else None))
        for observation in analysis['meshes']:
            observation['id'] = mesh_ids[observation['id']]
            observation.pop('region')
        material_inputs = [_material_inputs(material) for material in geometry.materials]
        materials = [dict(slot=i, inputs=material, graph_sha256=digest(material))
                     for i, material in enumerate(material_inputs)]
        textures = [dict(index=i, mime_type=image.mime_type,
                         sha256=hashlib.sha256(image.data).hexdigest(), bytes=len(image.data))
                    for i, image in enumerate(geometry.images)]
        texture_bindings = [{key: texture[key] for key in ('source', 'sampler') if key in texture}
                            for texture in geometry.textures]
        samplers = [{key: sampler[key] for key in ('magFilter', 'minFilter', 'wrapS', 'wrapT') if key in sampler}
                    for sampler in geometry.samplers]

        skeleton = deepcopy(model.skeleton)
        bone_ids = {}
        if skeleton is not None:
            bone_ids = {bone['id']: f'bone-{i}' for i, bone in enumerate(skeleton['bones'])}
            skeleton['root'] = bone_ids[skeleton['root']]
            for bone in skeleton['bones']:
                bone['id'] = bone_ids[bone['id']]
                bone['parent'] = bone_ids.get(bone['parent'])
                bone['mirror'] = bone_ids.get(bone['mirror'])
                bone['role_sha256'] = digest(bone.pop('role'))
        skin = {mesh_ids[key]:dict(method=value.method, joints=[bone_ids[joint] for joint in value.joints],
                       indices=_array(value.indices), weights=_array(value.weights),
                       inverse_bind_matrices=value.inverse_bind.tolist(),
                       inverse_bind_space='normalized importer rest space',
                       maximum_weight_sum_error=float(abs(value.weights.sum(axis=1)-1).max()))
                for key,value in model.weights.items()}
        features = model.report['source_features']
        material_partial = bool(features['unsupported_extensions'] or features['partial_material_extensions'])
        measurements = _measure(geometry, mesh_ids, {} if landmarks is None else landmarks,
                                {} if distances is None else distances, height)
        # Variant references serve as evidence pointers.
        if variants:
            _references(source, variants)
            _read_closure(root, root_identity, variants, caps)
        result = dict(schema_version='1.0.0', kind='continuity-asset-inspection',
            source=deepcopy(source), dependencies=refs, coordinates='right-handed Y-up meters; native importer scale 1',
            owners=['rig_import.load_character_model', 'rig_gltf.read_gltf/preflight_gltf/scene_graph'],
            software=dict(python=platform.python_version(),numpy=importlib.metadata.version('numpy'),
                          Pillow=importlib.metadata.version('Pillow'),
                          source_sha256={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                            for name in ('continuity_inspection.py','continuity_contracts.py','rig_errors.py','rig_gltf.py',
                                         'rig_import.py','rig_geometry.py','rig_models.py','rig_skeleton.py','rig_skinning.py',
                                         'rig_load_limits.py','rig_jpeg.py')}),
            analysis=analysis, hierarchy=hierarchy, meshes=meshes, materials=materials,
            textures=textures, texture_bindings=texture_bindings, samplers=samplers,
            texture_color_spaces=_color_spaces(material_inputs),
            extensions=features['extensions_used'], skeleton=skeleton, skins=skin,
            source_features=features, status='partial' if material_partial else 'inspected-supported-data',
            material_coverage='partial' if material_partial else 'supported-profile',
            native_load_admission=model.report['source_preflight'],
            native_image_admission=model.report['image_preflight'],
            native_animation_admission=model.report['animation_preflight'], measurements=measurements,
            variant_references=list(variants),
            geometry_sha256=digest(sorted(m['geometry_sha256'] for m in meshes)),
            attribute_morph_sha256=digest([dict(attributes=m['attributes'],morphs=m['morphs'],defaults=m['morph_defaults']) for m in meshes]),
            scale_normalized_sha256=digest(sorted(m['scale_normalized_sha256'] for m in meshes)) if height > 1e-9 else None,
            material_sha256=digest(dict(materials=materials,textures=textures,bindings=texture_bindings,samplers=samplers,
                                       assignments=sorted((m['source_node'], m['primitive_index'], m['material_slot']) for m in meshes))),
            fixture=bool(model.report['fixture']),
            bytes_inspected=encoded, visual_approval=False, semantic_equivalence='unverified',
            unavailable_fields=['subjective-visual-identity','renderer-specific-displacement/subsurface-node-graphs','native-rig-controllers-and-sockets'],
            limitations=['Material evidence covers core inputs and validated extension texture bindings.',
                         'Shape fingerprint quantizes triangle coordinates to 1e-9 m. Mesh index fingerprints record connectivity separately.',
                         'Scale-normalized fingerprint divides by measured height. Scale variation requires review.',
                         'Anatomical labels and production artwork require human review.'])
        receipt = dict(inspection=result, sha256=digest(result))
        _read_closure(root, root_identity, variants, caps)
        return receipt


def compare_assets(root, left, right):
    """Execute both inspections; never trust caller-supplied measurement receipts."""
    root, root_identity = _admitted_root(root)
    a = _inspect_asset(root, root_identity, **left)
    b = _inspect_asset(root, root_identity, **right)
    x, y = a['inspection'], b['inspection']

    def closure(value):
        import posixpath
        parent = Path(value['source']['path']).parent.as_posix()
        return sorted((posixpath.relpath(ref['path'], parent), ref['sha256'])
                      for ref in value['dependencies'][1:])

    same_source = x['source']['sha256'] == y['source']['sha256']
    observed_material_identity = x['material_sha256'] == y['material_sha256']
    material_complete = x['material_coverage'] == y['material_coverage'] == 'supported-profile'
    result = dict(left=a,right=b,byte_identity=same_source and closure(x) == closure(y),
                source_byte_identity=same_source,
                geometry_identity=x['geometry_sha256']==y['geometry_sha256'],
                attribute_morph_identity=x['attribute_morph_sha256']==y['attribute_morph_sha256'],
                scale_normalized_geometry_identity=x['scale_normalized_sha256'] is not None and x['scale_normalized_sha256']==y['scale_normalized_sha256'],
                observed_material_identity=observed_material_identity,
                material_identity=observed_material_identity if material_complete else None,
                semantic_equivalence='unverified',visual_approval=False,needs_visual_review=True)
    return _finish_comparison(root, root_identity, result, [a, b])


def _finish_comparison(root, root_identity, result, receipts):
    """Recheck both exact closures together at the result publication boundary."""
    canonical_bytes(result)
    for receipt in receipts:
        inspection = receipt['inspection']
        caps = inspection['native_load_admission']['limits']
        _read_closure(root, root_identity, inspection['dependencies'], caps)
        _read_closure(root, root_identity, inspection['variant_references'], caps)
    return result


def _public_failure(exc):
    if isinstance(exc, RigError):
        report = exc.report
        diagnostic = {'code': report['code']}
        # These errors report resource totals and limits, rather than arbitrary
        # JSON values or identifiers. Bound their public integer representation.
        if report['code'] in {'RIG_LIMIT_BINARY', 'RIG_LIMIT_MEMORY', 'RIG_LIMIT_MORPHS',
                              'RIG_LIMIT_TRIANGLES', 'RIG_LIMIT_VERTICES'}:
            for key in ('expected', 'observed'):
                count = report.get(key)
                if type(count) is int and 0 <= count <= 2**53 - 1:
                    diagnostic[key] = count
        return dict(error='Native inspection input rejected.', native_diagnostic=diagnostic)
    if isinstance(exc, _InspectionError):
        return dict(error=f'{exc.code}: Inspection input rejected.', native_diagnostic=None)
    message = 'Inspection I/O failed.' if isinstance(exc, OSError) else 'Inspection input rejected.'
    return dict(error=message, native_diagnostic=None)


def main(argv=None):
    import argparse,json
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',required=True,type=Path)
    parser.add_argument('--request',required=True,type=Path)
    args=parser.parse_args(argv)
    try:
        from rig_errors import decode_json
        request=decode_json(_read_regular_bytes(args.request,limit=1024*1024))
        fields = {'source', 'dependencies', 'landmarks', 'distances', 'limits', 'variants'}

        def validate_spec(spec):
            if not isinstance(spec, dict) or 'source' not in spec or set(spec) - fields:
                fail('REQUEST', 'unexpected or missing inspection request field')
            return spec

        if isinstance(request, dict) and set(request) == {'left', 'right'}:
            result = compare_assets(args.root, validate_spec(request['left']), validate_spec(request['right']))
        else:
            result = inspect_asset(args.root, **validate_spec(request))
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (ValueError,OSError) as exc:
        print(json.dumps(_public_failure(exc), allow_nan=False))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
