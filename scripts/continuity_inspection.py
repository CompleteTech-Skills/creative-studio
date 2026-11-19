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
from continuity_capabilities import OwnerAdapter
from continuity_contracts import verify_references
from rig_errors import RigError, job_lock, normalize_root, open_payload

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
            nodes = document.json['nodes']
            hierarchy = [dict(node_id=i, parent=parents.get(i),
                              local_matrix=node_matrix(nodes[i]).tolist(), world_matrix=world[i].tolist(),
                              mesh=nodes[i].get('mesh'), skin=nodes[i].get('skin')) for i in order]
            features = _source_features(document)
            del document
            model = load_character_model(path, limits=limits)
            model.report.update(source_features=features, source_preflight=admission, image_preflight=images)
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
    if isinstance(source, dict) and source.get('kind') == 'hatch-3d-rig':
        native_root, native_identity = _admitted_root(normalize_root(root))
        if native_identity != root_identity:
            fail('ROOT', 'native inspection root changed before capture')
        return _inspect_native(native_root, native_identity, source, dependencies=dependencies, landmarks=landmarks,
                               distances=distances, limits=limits, variants=variants)
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
            native_image_admission=model.report['image_preflight'], measurements=measurements,
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
    x_face, y_face = x.get('facial_controls'), y.get('facial_controls')
    result = dict(left=a,right=b,byte_identity=same_source and closure(x) == closure(y),
                source_byte_identity=same_source,
                geometry_identity=x['geometry_sha256']==y['geometry_sha256'],
                attribute_morph_identity=x['attribute_morph_sha256']==y['attribute_morph_sha256'],
                scale_normalized_geometry_identity=x['scale_normalized_sha256'] is not None and x['scale_normalized_sha256']==y['scale_normalized_sha256'],
                observed_material_identity=observed_material_identity,
                material_identity=observed_material_identity if material_complete else None,
                facial_controls_identity=x_face == y_face if x_face is not None and y_face is not None else None,
                semantic_equivalence='unverified',visual_approval=False,needs_visual_review=True)
    return _finish_comparison(root, root_identity, result, [a, b])


def _finish_comparison(root, root_identity, result, receipts):
    """Recheck both exact closures together at the result publication boundary."""
    canonical_bytes(result)
    for receipt in receipts:
        inspection = receipt['inspection']
        caps = inspection['native_load_admission']['limits']
        closure_root = normalize_root(root) if inspection['source']['kind'] == 'hatch-3d-rig' else root
        _read_closure(closure_root, root_identity, inspection['dependencies'], caps)
        _read_closure(closure_root, root_identity, inspection['variant_references'], caps)
    return result


@contextmanager
def _loaded_native(root, root_identity, source, dependencies, requested):
    from rig_models import load_rig

    limits = _limits(requested)
    refs = _references(source, dependencies)
    if Path(source['path']).name != 'rig.json':
        fail('ADAPTER_REQUIRED', 'native rig inspection requires rig.json')
    with job_lock((root / source['path']).parent):
        with tempfile.TemporaryDirectory(prefix='continuity-inspect-') as scratch:
            snapshot = Path(scratch).resolve()
            total = _read_closure(root, root_identity, refs, limits, snapshot)
            try:
                rig = load_rig((snapshot / source['path']).parent, limits=limits,
                               expected_manifest_sha256=refs[0]['sha256'])
                if (rig.manifest['id'], rig.manifest['version']) != (source['id'], source['version']):
                    fail('REFERENCE', 'native rig identity/version differs from exact source reference')
                _validate_rig(rig, limits)
                yield rig, _rig_hierarchy(rig), refs, limits, total
            except MemoryError:
                fail('LIMIT', 'native allocation failed inside admitted inspection')
            finally:
                _read_closure(root, root_identity, refs, limits)

def _rig_hierarchy(rig):
    if rig.manifest.get('skeleton') is None:
        return []
    from rig_skeleton import evaluate_fk
    world = evaluate_fk(rig.skeleton)
    return [dict(bone_id=b['id'], parent=b['parent'], rest=deepcopy(b['rest']),
                 world_matrix=world[b['id']].tolist(), semantic_role=b['role'],
                 role_evidence=deepcopy(b['evidence'])) for b in rig.skeleton['bones']]

def _validate_rig(rig,limits):
    """Delegate bone/bind/socket/constraint validation to their native owners."""
    if rig.manifest.get('skeleton') is None:
        if rig.weights or rig.manifest['attachments']:
            fail('RIG','skin or attachment requires actual skeleton')
        return
    from rig_skeleton import validate_skeleton,neutral_pose
    from rig_skinning import validate_skin
    from rig_constraints import constraint_order
    from rig_ik import validate_chain
    from rig_attachments import socket_world
    from rig_animation import validate_animation
    validate_skeleton(rig.skeleton,limits)
    for name,skin in rig.weights.items():
        mesh=rig.geometry.by_id().get(name)
        if mesh is None:
            fail('RIG','skin names a missing mesh')
        validate_skin(skin,mesh,rig.skeleton,limits)
    constraint_order(rig.skeleton,rig.manifest['controllers']['constraints'])
    for chain in rig.manifest['controllers']['ik']:
        validate_chain(rig.skeleton,chain)
    sockets=rig.manifest['attachments']
    if len({s['id'] for s in sockets})!=len(sockets):
        fail('RIG','duplicate socket identity')
    for socket in sockets:
        socket_world(rig,neutral_pose(rig.skeleton),socket['id'])
    keys=0
    for clip in rig.clips.values():
        keys+=validate_animation(rig,clip)['keyframes']
        if keys>limits['keyframes']:
            fail('LIMIT','aggregate native clip keys exceed caller cap')

def _inspect_native(root, root_identity, source, *, dependencies=(), landmarks=None, distances=None, limits=None, variants=()):
    """Inspect exact file closure. No source mutation or automatic approvals."""
    if not isinstance(variants,(list,tuple)) or len(variants)>64:
        fail('REFERENCE','variants must be a bounded list of existing exact references')
    canonical_bytes(dict(landmarks=landmarks,distances=distances,variants=variants))
    landmarks,distances,variants=deepcopy((landmarks,distances,variants))
    with _loaded_native(root, root_identity, source, dependencies, limits) as (model, hierarchy, refs, caps, encoded):
        source=refs[0]
        from rig_geometry import analyze_geometry
        geometry = model.geometry
        analysis = analyze_geometry(geometry, caps)
        height = analysis['dimensions_m'][1]
        meshes = []
        for mesh in geometry.meshes:
            meshes.append(dict(id=mesh.id, name=mesh.name, semantic_region=mesh.region,
                source_node=mesh.source_node, positions=_array(mesh.positions), triangles=_array(mesh.triangles),
                attributes={key:_array(value) for key,value in mesh.attributes.items()},
                morphs={name:{key:_array(value) for key,value in fields.items()} for name,fields in mesh.morphs.items()},
                morph_defaults=deepcopy(mesh.morph_defaults), material_slot=mesh.material,
                geometry_sha256=_surface(mesh), scale_normalized_sha256=_surface(mesh, height) if height > 1e-9 else None))
        materials = [dict(slot=i, inputs=deepcopy(material), graph_sha256=digest(material)) for i,material in enumerate(geometry.materials)]
        textures = [dict(index=i, name=image.name, mime_type=image.mime_type,
                         sha256=hashlib.sha256(image.data).hexdigest(), bytes=len(image.data)) for i,image in enumerate(geometry.images)]
        skin = {key:dict(joints=list(value.joints), indices=_array(value.indices), weights=_array(value.weights),
                       inverse_bind_matrices=value.inverse_bind.tolist(),
                       maximum_weight_sum_error=float(abs(value.weights.sum(axis=1)-1).max()))
                for key,value in model.weights.items()}
        skeleton = model.manifest.get('skeleton')
        from rig_gltf import MATERIAL_EXTENSIONS
        features=dict(
            extensions_used=list(geometry.extensions_used),extensions_required=None,
            unsupported_extensions=sorted(set(geometry.extensions_used)-MATERIAL_EXTENSIONS),
            animation_channels=[dict(clip=name,target=track['target'],path=track['path'])
                                for name,clip in model.clips.items() for track in clip['tracks']])
        controllers = deepcopy(model.manifest['controllers'])
        sockets = deepcopy(model.manifest['attachments'])
        measurements = _measure(geometry, {mesh.id: mesh.id for mesh in geometry.meshes},
                                {} if landmarks is None else landmarks,
                                {} if distances is None else distances, height)
        # Approved variant references are evidence pointers only. Approval is
        # deliberately not inferred from client labels or equal geometry.
        if variants:
            _references(source, variants)
            _read_closure(root, root_identity, variants, caps)
        result = dict(schema_version='1.0.0', kind='continuity-asset-inspection',
            source=deepcopy(source), dependencies=refs, coordinates='right-handed Y-up meters; native importer scale1',
            owners=['rig_models.load_rig','rig_skeleton.evaluate_fk'],
            software=dict(python=platform.python_version(),numpy=importlib.metadata.version('numpy'),
                          Pillow=importlib.metadata.version('Pillow'),
                          source_sha256={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                            for name in ('continuity_inspection.py','continuity_contracts.py','rig_models.py','rig_gltf.py',
                                         'rig_import.py','rig_geometry.py','rig_skeleton.py','rig_skinning.py','rig_load_limits.py')}),
            analysis=analysis, hierarchy=hierarchy, meshes=meshes, materials=materials,
            textures=textures, texture_bindings=deepcopy(geometry.textures), samplers=deepcopy(geometry.samplers),
            texture_color_spaces=_color_spaces(geometry.materials),
            extensions=list(geometry.extensions_used), skeleton=deepcopy(skeleton), skins=skin,
            source_features=features,status='partial' if features['unsupported_extensions'] else 'inspected-supported-data',
            native_load_admission=dict(model.resource_usage, limits=caps),
            native_image_admission=None, material_coverage='partial' if features['unsupported_extensions'] else 'supported-profile',
            controllers=controllers, sockets=sockets, measurements=measurements,
            facial_controls=deepcopy(model.manifest['facial']),
            native_approval_status=model.manifest['review']['status'],
            variant_references=list(variants),
            geometry_sha256=digest(sorted(m['geometry_sha256'] for m in meshes)),
            attribute_morph_sha256=digest([dict(attributes=m['attributes'],morphs=m['morphs'],defaults=m['morph_defaults']) for m in meshes]),
            scale_normalized_sha256=digest(sorted(m['scale_normalized_sha256'] for m in meshes)) if height > 1e-9 else None,
            material_sha256=digest(dict(materials=materials,textures=textures,bindings=geometry.textures,samplers=geometry.samplers,
                                       assignments=[dict(geometry=m['geometry_sha256'],slot=m['material_slot']) for m in meshes])),
            fixture=bool(model.manifest['source']['fixture']),
            bytes_inspected=encoded, visual_approval=False, semantic_equivalence='unverified',
            unavailable_fields=['subjective-visual-identity','renderer-specific-displacement/subsurface-node-graphs','original-native-scene-hierarchy'],
            limitations=['Material shader inputs are observed portable glTF/native data, not renderer output equivalence.',
                         'Shape fingerprint quantizes triangles to1e-9m, ignores winding/serialization and retains topology; it cannot prove semantic identity.',
                         'Scale-normalized fingerprint divides by measured height; approved scale variation still requires review.',
                         'No automatic anatomical inference or production artwork approval.'])
        receipt = dict(inspection=result, sha256=digest(result))
        _read_closure(root, root_identity, variants, caps)
        return receipt

class NativeInspectionOwner(OwnerAdapter):
    """Trusted host adapter for the existing capability admission authority.

    Construction executes and retains a real source-bound probe. It never adds
    acquisition, rig jobs or review authority. Native rig locks remain inside
    the canonical loader adapter above; GLB/glTF inspection is read-only.
    """
    def __init__(self,root,source,evidence_folder,*,dependencies=(),expiry_seconds=300):
        from datetime import datetime,timezone,timedelta
        if type(expiry_seconds) is not int or not 1<=expiry_seconds<=3600:
            fail('EXPIRY','probe expiry must be1..3600seconds')
        self.root=Path(root).resolve(strict=True)
        self.refs=_references(source,dependencies)
        self.source=self.refs[0]
        if self.source['kind'] not in ('mesh','model3d-asset','hatch-3d-rig'):
            fail('REFERENCE','capability input must use the existing mesh/model3d-asset/hatch-3d-rig native-reference kinds')
        self.folder=portable_path(evidence_folder)
        self._reservation_lock=__import__('threading').Lock()
        self._active=False
        with self.reserve(None):
            probe=self.execute(None)
        self._snapshot=dict(schema_version='1.0.0',kind='continuity-capability-snapshot',
            capability_id='geometry-inspection',implementation_id='independent-geometry-parser',
            owner='hatch-rig',backend='native-rig-gltf-inspection',backend_version='1.0.0',
            contract_version='1.0.0',input_types=[source['kind']],output_types=['native-inspection'],
            operations=['geometry-inspection'],status='available',discovered=True,
            tested_operations=['geometry-inspection'],evidence=probe['evidence'],
            limits=dict(memory_bytes=512*1024**2,disk_bytes=64*1024**2,cpu_seconds=60,gpu_bytes=0,concurrency=1),
            offline=True,asset_rights_required=True,
            expires_at=(datetime.now(timezone.utc)+timedelta(seconds=expiry_seconds)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            limitations=['Native glTF/rig inspection only; no Blender scripts, subjective identity or production visual approval.'])

    def snapshot(self):
        return deepcopy(self._snapshot)

    @contextmanager
    def reserve(self,check):
        if not self._reservation_lock.acquire(blocking=False):
            fail('OWNERSHIP','inspection adapter already reserved')
        try:
            with self._reserve(check):
                yield
        finally:
            self._reservation_lock.release()

    @contextmanager
    def _reserve(self,check):
        from continuity_capabilities import _peak_memory_bytes
        if self._active:
            fail('OWNERSHIP','inspection adapter already reserved')
        resource=check['resources'] if check is not None else dict(memory_bytes=512*1024**2,disk_bytes=64*1024**2)
        peak=_peak_memory_bytes()
        if peak is None:
            fail('LIMIT','whole-process peak memory is unavailable')
        available=min(CAPS['memory_bytes'],resource['memory_bytes']-peak-32*1024**2)
        disk=min(CAPS['dependency_bytes'],resource['disk_bytes']//2)
        if available<=0 or disk<=0:
            fail('LIMIT','no admitted workspace remains for native inspection')
        if check is not None and any(ref not in check['inputs'] for ref in self.refs):
            fail('REFERENCE','all exact probed sources must be admitted inputs')
        self._active=True
        self._resource=resource
        self._caps=dict(memory_bytes=available,dependency_bytes=disk,source_bytes=min(CAPS['source_bytes'],disk))
        try:
            yield
            peak=_peak_memory_bytes()
            if peak is None or peak>resource['memory_bytes']:
                fail('LIMIT','whole-process memory reservation exceeded')
            verify_references(self.root,self.refs,maximum_bytes=disk)
        finally:
            self._active=False

    def execute(self,check):
        if not self._active:
            fail('OWNERSHIP','native inspection requires an active reservation')
        result=inspect_asset(self.root,self.source,dependencies=self.refs[1:],limits=self._caps)
        payload=canonical_bytes(result)
        if len(payload)+result['inspection']['bytes_inspected']>self._resource['disk_bytes']:
            fail('LIMIT','snapshot plus inspection receipt exceeds disk reservation')
        from continuity_capability_owners import _write_new
        evidence=_write_new(self.root,self.folder,payload)
        fields=['geometry','materials','skins','morphs']
        unsupported=result['inspection']['source_features']['unsupported_extensions']
        if not unsupported:
            fields.append('extensions-and-channels')
        if self.source['kind']!='hatch-3d-rig':
            fields.append('source-hierarchy')
        return dict(outcome='partial' if unsupported else 'executed',evidence=[evidence],verified_fields=fields,
            measurements=dict(inspection_sha256=result['sha256'],vertices=result['inspection']['analysis']['vertices'],
                              triangles=result['inspection']['analysis']['triangles'],fixture=result['inspection']['fixture'],
                              visual_approval=False),limitations=result['inspection']['limitations'])

def classify_rigs(root, source, target, *, required_bones=(), required_sockets=(),
                  required_morphs=(), profile=None, review=None):
    """Inspect before classifying; approval decisions remain external evidence.

    Exact compatibility requires native owner-validated approval on both rigs.
    Retargetability requires an exact approved profile and actual native pose/
    deformation execution; this does not verify arbitrary future clip contacts.
    """
    from continuity_contracts import read_reference_bytes, validate_document
    from rig_errors import decode_json
    canonical_bytes(dict(source=source,target=target,profile=profile,review=review))
    source,target,profile,review=deepcopy((source,target,profile,review))
    for required in (required_bones, required_sockets, required_morphs):
        if (not isinstance(required,(list,tuple)) or len(required)>256 or
                any(not isinstance(x,str) or not 1<=len(x)<=128 for x in required) or len(set(required))!=len(required)):
            fail('RIG_REQUIREMENT','bounded unique bone/socket/morph names are required')
    root, root_identity = _admitted_root(normalize_root(root))
    a = _inspect_asset(root, root_identity, **source)
    b = _inspect_asset(root, root_identity, **target)
    x,y = a['inspection'],b['inspection']
    result=dict(status='unverified',source=a,target=b,issues=[],profile=profile,review=review,
                visual_approval=False,production_artwork_approved=False,
                limitations=['Structural/pose compatibility only; full clip timing, contacts and artistic identity require their owning validators.'])
    def finish():
        return _finish_comparison(root, root_identity, result, [a,b])
    if x['skeleton'] is None or y['skeleton'] is None:
        result['issues'].append(dict(code='MISSING_SKELETON',message='actual source/target skeleton is unavailable'))
        return finish()
    if any(set(v['skins'])!={m['id'] for m in v['meshes']} for v in (x,y)):
        result['issues'].append(dict(code='MISSING_SKIN',message='actual binding for every represented rig mesh is required'))
        return finish()
    source_bones={v['id'] for v in x['skeleton']['bones']}
    target_bones={v['id'] for v in y['skeleton']['bones']}
    source_sockets={v['id']:v for v in x.get('sockets',())}
    target_sockets={v['id']:v for v in y.get('sockets',())}
    morphs={name for mesh in y['meshes'] for name in mesh['morphs']}
    for kind,names,available in [('bone',required_bones,target_bones),('socket',required_sockets,target_sockets),('morph',required_morphs,morphs)]:
        for name in names:
            if name not in available:
                result['issues'].append(dict(code='MISSING_'+kind.upper(),id=name))
    if result['issues']:
        result['status']='incompatible'
        return finish()
    same=(x['skeleton']==y['skeleton'] and x['skins']==y['skins'] and
          x.get('controllers')==y.get('controllers') and x.get('sockets')==y.get('sockets') and
          x.get('facial_controls')==y.get('facial_controls') and
          x['geometry_sha256']==y['geometry_sha256'] and x['attribute_morph_sha256']==y['attribute_morph_sha256'])
    if same:
        result['status']='exact' if x.get('native_approval_status')==y.get('native_approval_status')=='approved' else 'unverified'
        result['structure_and_binding_equal']=True
        if result['status']=='unverified':
            result['issues'].append(dict(code='APPROVAL_REQUIRED',message='equal measured structures do not establish approved rig identity'))
        return finish()
    # Unavailable facial controls are unknown, as in compare_assets, not a
    # measured difference.
    x_face,y_face=x.get('facial_controls'),y.get('facial_controls')
    if x_face is not None and y_face is not None:
        if x_face!=y_face:
            result['issues'].append(dict(code='FACIAL_CONTROLS_DIFFER',message='captured facial controls differ between rigs'))
    elif x_face is not None or y_face is not None:
        result['issues'].append(dict(code='FACIAL_CONTROLS_UNAVAILABLE',
                                     message='facial controls are captured for only one rig and cannot be compared'))
    if profile is None or review is None:
        sb={v['id']:v for v in x['skeleton']['bones']}
        tb={v['id']:v for v in y['skeleton']['bones']}
        overlap=sorted(name for name in source_bones & target_bones
            if sb[name]['parent']==tb[name]['parent'] and sb[name]['rest']==tb[name]['rest'])
        result['verified_rest_joint_overlap']=overlap
        result['status']='partial' if overlap else 'unverified'
        result['issues'].append(dict(code='APPROVED_MAPPING_REQUIRED',message='different measured rigs require an exact approved retarget profile'))
        return finish()
    _references(profile,[review])
    verify_references(root,[profile,review])
    mapping=decode_json(read_reference_bytes(root,profile))
    decision=validate_document(decode_json(read_reference_bytes(root,review)),'review')
    verify_references(root,decision)
    retained=decode_json(read_reference_bytes(root,decision['native_receipt']))
    expected={k:v for k,v in decision.items() if k!='native_receipt'}
    covered={(r['path'],r['sha256']) for r in decision['inspected']}
    required={(r['path'],r['sha256']) for r in [profile,source['source'],target['source']]}
    if (retained!=expected or decision['verdict']!='approved' or decision['reviewer_kind']=='synthetic-test'
            or decision['subject_sha256']!=profile['sha256'] or not required<=covered
            or decision['fixture'] != (x['fixture'] or y['fixture'])):
        fail('REVIEW','mapping decision must bind exact profile and both inspected rigs, preserving fixture provenance')
    from rig_contracts import validate_contract
    validate_contract(mapping,'RetargetProfile')
    incompatible_socket=False
    for socket in required_sockets:
        old=source_sockets.get(socket)
        new=target_sockets[socket]
        if old is None or mapping['mapping'].get(old['bone'])!=new['bone'] or old['transform']!=new['transform']:
            result['issues'].append(dict(code='INCOMPATIBLE_SOCKET',id=socket))
            incompatible_socket=True
    if incompatible_socket:
        result['status']='incompatible'
        return finish()
    # Supported retarget execution belongs to the native rig authority. Imported
    # glTF inspection alone cannot fabricate rig controllers or approval state.
    if source['source']['kind']!='hatch-3d-rig' or target['source']['kind']!='hatch-3d-rig':
        result['issues'].append(dict(code='NATIVE_RETARGET_ADAPTER_REQUIRED'))
        return finish()
    from rig_retarget import retarget_pose
    from rig_skeleton import neutral_pose,rotate_bone,evaluate_fk
    from rig_skinning import evaluate_deformation
    from rig_math import axis_angle,decompose,qinverse,qmul
    import numpy as np
    # Source/target are distinct here: same-source exact path returned above.
    with _loaded_native(root,root_identity,source['source'],source.get('dependencies',()),source.get('limits')) as (sr,_,_,_,_):
        with _loaded_native(root,root_identity,target['source'],target.get('dependencies',()),target.get('limits')) as (tr,_,_,_,_):
            sw,tw=evaluate_fk(sr.skeleton),evaluate_fk(tr.skeleton)
            if not mapping['mapping'] or not set(mapping['mapping'])<=source_bones or not set(mapping['mapping'].values())<=target_bones:
                fail('RIG_MAPPING','approved mapping names missing native bones')
            if (set(mapping['orientation_offsets'])!=set(mapping['mapping']) or
                    mapping['mapping'].get(sr.skeleton['root'])!=tr.skeleton['root']):
                fail('RIG_MAPPING','calibration must cover exactly mapped joints and preserve actual root correspondence')
            source_morphs={name for mesh in sr.geometry.meshes for name in mesh.morphs}
            target_morphs={name for mesh in tr.geometry.meshes for name in mesh.morphs}
            if not set(mapping['morph_mapping'])<=source_morphs or not set(mapping['morph_mapping'].values())<=target_morphs:
                fail('RIG_MAPPING','morph map references missing actual morph channels')
            for left,right in mapping['mapping'].items():
                expected=qmul(qinverse(decompose(sw[left])['rotation']),decompose(tw[right])['rotation'])
                given=np.asarray(mapping['orientation_offsets'][left])
                if min(np.linalg.norm(expected-given),np.linalg.norm(expected+given))>1e-6:
                    fail('RIG_MAPPING','retarget orientation calibration disagrees with actual rest transforms')
            height=x['analysis']['dimensions_m'][1]
            if height<=1e-9 or abs(mapping['root_scale']-y['analysis']['dimensions_m'][1]/height)>1e-6:
                fail('RIG_MAPPING','retarget root scale disagrees with actual measured geometry')
            pose=neutral_pose(sr.skeleton)
            probe=next(iter(mapping['mapping']))
            pose=rotate_bone(sr.skeleton,pose,probe,axis_angle([0,1,0],.1))
            output=retarget_pose(sr,tr,pose,mapping)
            deformation=evaluate_deformation(tr,output)
            result['native_pose_proof']=dict(evaluated_bones=len(evaluate_fk(tr.skeleton,output)),
                deformed_vertices=sum(len(v['positions']) for v in deformation.values()),
                maximum_displacement_m=max(float(np.linalg.norm(v['positions']-tr.geometry.by_id()[k].positions,axis=1).max()) for k,v in deformation.items()))
            result['native_pose_proof']['software_source_sha256']={name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                for name in ('rig_retarget.py','rig_skeleton.py','rig_skinning.py','rig_constraints.py','rig_math.py')}
    omissions=sorted(source_bones-set(mapping['mapping']))
    morph_omissions=sorted(name for name in source_morphs if mapping['morph_mapping'].get(name,name) not in target_morphs)
    result['status']='partial' if omissions or mapping['omissions'] or morph_omissions else 'retargetable'
    result['unmapped_bones']=omissions
    result['unmapped_morphs']=morph_omissions
    verify_references(root,[profile,review,decision])
    return finish()


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
