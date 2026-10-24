"""Bounded glTF 2.0 buffers/accessors and standards-compatible animated export.

Independent acceptance is supplied by Khronos Validator, Blender and Three.js;
round-tripping through this module alone is never counted as interoperability.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass, field
import json
import hashlib
from pathlib import Path
import struct
from urllib.parse import unquote, urlsplit
import numpy as np

from rig_contracts import DEFAULT_LIMITS, validate_contract
from rig_errors import RigError, atomic_bytes, decode_json, require, safe_path
from rig_math import compose, qmatrix, quaternion, vector

DTYPES = {5120: np.dtype('i1'), 5121: np.dtype('u1'), 5122: np.dtype('<i2'),
          5123: np.dtype('<u2'), 5125: np.dtype('<u4'), 5126: np.dtype('<f4')}
WIDTHS = {'SCALAR': 1, 'VEC2': 2, 'VEC3': 3, 'VEC4': 4, 'MAT4': 16}
MATERIAL_EXTENSIONS = {'KHR_materials_unlit', 'KHR_materials_clearcoat', 'KHR_materials_emissive_strength',
                       'KHR_materials_ior', 'KHR_materials_iridescence', 'KHR_materials_sheen',
                       'KHR_materials_specular', 'KHR_materials_transmission', 'KHR_materials_volume',
                       'KHR_texture_transform', 'KHR_materials_anisotropy'}


def _index(value, size: int, label: str) -> int:
    require(type(value) is int and 0 <= value < size, 'RIG_GLTF_REFERENCE', 'Invalid '+label+' index.', observed=value)
    return value


def _count(value, label: str, maximum: int) -> int:
    require(type(value) is int and 0 <= value <= maximum, 'RIG_GLTF_COUNT', 'Invalid '+label+' count.', observed=value)
    return value


@dataclass
class GltfDocument:
    path: Path
    json: dict
    buffers: list[bytes]
    limits: dict
    dependencies: list[Path] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_reader: object = None
    source_sha256: str | None = None

    def uri(self, uri: str, maximum: int | None = None) -> bytes:
        maximum = maximum or self.limits['source_bytes']
        require(isinstance(uri, str), 'RIG_GLTF_URI', 'A glTF URI must be text.')
        if uri.startswith('data:'):
            prefix, separator, data = uri.partition(',')
            require(separator and prefix.endswith(';base64') and len(data)*3//4 <= maximum,
                    'RIG_GLTF_URI', 'Embedded data must be bounded base64.')
            try:return base64.b64decode(data, validate=True)
            except (ValueError, base64.binascii.Error) as exc:raise RigError('RIG_GLTF_URI', 'Invalid base64 data.') from exc
        parts = urlsplit(uri)
        require(not parts.scheme and not parts.netloc and not parts.query and not parts.fragment,
                'RIG_GLTF_URI', 'Only local relative glTF dependencies are supported.')
        target = safe_path(self.path.parent, unquote(parts.path, errors='strict'), exists=True)
        if self.source_reader is not None:
            payload = self.source_reader(target, maximum)
            if target not in self.dependencies:self.dependencies.append(target)
            return payload
        require(target.stat().st_size <= maximum, 'RIG_LIMIT_SOURCE', 'glTF dependency exceeds its byte budget.')
        if target not in self.dependencies:self.dependencies.append(target)
        require(sum(p.stat().st_size for p in self.dependencies) <= self.limits['dependency_bytes'],
                'RIG_LIMIT_DEPENDENCIES', 'Expanded source dependency inventory exceeds its byte budget.')
        return target.read_bytes()

    def view(self, index: int) -> tuple[bytes, int, int, dict]:
        views = self.json.get('bufferViews', [])
        view = views[_index(index, len(views), 'bufferView')]
        buffer = self.buffers[_index(view.get('buffer'), len(self.buffers), 'buffer')]
        offset = _count(view.get('byteOffset', 0), 'bufferView offset', len(buffer))
        length = _count(view.get('byteLength'), 'bufferView length', len(buffer))
        require(offset+length <= len(buffer), 'RIG_GLTF_BUFFER', 'Buffer view extends beyond its buffer.')
        return buffer, offset, length, view

    def _array(self, view_id: int, byte_offset: int, count: int, width: int, dtype, *, packed=False):
        buffer, start, length, view = self.view(view_id)
        offset = _count(byte_offset, 'accessor offset', length)
        stride = dtype.itemsize*width if packed else view.get('byteStride', dtype.itemsize*width)
        require(type(stride) is int and stride >= dtype.itemsize*width and stride % dtype.itemsize == 0,
                'RIG_GLTF_STRIDE', 'Accessor byte stride is invalid.')
        if not packed and 'byteStride' in view:
            require(4 <= stride <= 252 and stride % 4 == 0, 'RIG_GLTF_STRIDE', 'Vertex stride must be a multiple of four in [4,252].')
        require((start+offset) % dtype.itemsize == 0, 'RIG_GLTF_ALIGNMENT', 'Accessor components are misaligned.')
        last = offset + (count-1)*stride + width*dtype.itemsize if count else offset
        require(last <= length, 'RIG_GLTF_BUFFER', 'Accessor extends outside its buffer view.')
        return np.ndarray((count, width), dtype=dtype, buffer=buffer,
                          offset=start+offset, strides=(stride, dtype.itemsize)).copy()

    def accessor(self, index: int) -> np.ndarray:
        accessors = self.json.get('accessors', [])
        accessor = accessors[_index(index, len(accessors), 'accessor')]
        width = WIDTHS.get(accessor.get('type'))
        dtype = DTYPES.get(accessor.get('componentType'))
        require(width is not None and dtype is not None, 'RIG_GLTF_ACCESSOR', 'Unsupported accessor type or component type.')
        count = _count(accessor.get('count'), 'accessor', max(self.limits['vertices'], self.limits['keyframes']*128))
        require(count > 0 and count*width*8 <= self.limits['memory_bytes']//3,
                'RIG_LIMIT_ACCESSOR', 'Accessor allocation exceeds the working memory bound.')
        if 'bufferView' in accessor:
            data = self._array(accessor['bufferView'], accessor.get('byteOffset', 0), count, width, dtype)
        else:
            require(accessor.get('byteOffset', 0) == 0, 'RIG_GLTF_ACCESSOR', 'An accessor without a buffer view cannot have an offset.')
            data = np.zeros((count, width), dtype=dtype)
        if 'sparse' in accessor:
            sparse = accessor['sparse'];n = _count(sparse.get('count'), 'sparse accessor', count)
            desc = sparse['indices'];component = desc.get('componentType')
            require(component in (5121, 5123, 5125), 'RIG_GLTF_SPARSE', 'Sparse indices require unsigned integer components.')
            indices = self._array(desc['bufferView'], desc.get('byteOffset', 0), n, 1, DTYPES[component], packed=True).ravel()
            require(n > 0 and indices.max() < count and np.all(np.diff(indices.astype(np.int64)) > 0),
                    'RIG_GLTF_SPARSE', 'Sparse indices must be increasing, unique and in bounds.')
            values = sparse['values']
            data[indices] = self._array(values['bufferView'], values.get('byteOffset', 0), n, width, dtype, packed=True)
        if accessor.get('normalized', False):
            require(dtype.kind in 'iu' and dtype.itemsize <= 2, 'RIG_GLTF_NORMALIZED', 'Only byte/short attributes may be normalized.')
            data = data.astype(np.float64)/np.iinfo(dtype).max
            if dtype.kind == 'i':data = np.maximum(data, -1.)
        require(np.isfinite(data).all(), 'RIG_NONFINITE', 'glTF accessor contains nonfinite values.')
        return data


def read_gltf(path: str | Path, limits=None, *, source_reader=None) -> GltfDocument:
    limits = dict(limits or DEFAULT_LIMITS)
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'RIG_SOURCE', 'Model must be a regular local file.')
    path = path.absolute() if source_reader is not None else path.resolve()
    if source_reader is None:
        require(path.stat().st_size <= limits['source_bytes'], 'RIG_LIMIT_SOURCE', 'Encoded model exceeds the source byte limit.')
        raw = path.read_bytes()
    else:
        raw = source_reader(path, limits['source_bytes'])
    binary = None
    if path.suffix.lower() == '.glb':
        require(len(raw) >= 20, 'RIG_GLB_HEADER', 'GLB is truncated.')
        magic, version, length = struct.unpack_from('<4sII', raw)
        require(magic == b'glTF' and version == 2 and length == len(raw), 'RIG_GLB_HEADER', 'Invalid GLB magic, version or length.')
        offset, chunks = 12, []
        while offset < len(raw):
            require(offset+8 <= len(raw) and len(chunks) < 64, 'RIG_GLB_CHUNK', 'Truncated or excessive GLB chunks.')
            length, kind = struct.unpack_from('<II', raw, offset);offset += 8
            require(length % 4 == 0 and offset+length <= len(raw), 'RIG_GLB_CHUNK', 'Invalid GLB chunk length.')
            chunks.append((kind, raw[offset:offset+length]));offset += length
        require(chunks[0][0] == 0x4E4F534A and sum(k == 0x4E4F534A for k, _ in chunks) == 1,
                'RIG_GLB_JSON', 'GLB must start with exactly one JSON chunk.')
        require(sum(k == 0x004E4942 for k, _ in chunks) <= 1, 'RIG_GLB_BIN', 'GLB cannot repeat its BIN chunk.')
        document = decode_json(chunks[0][1])
        binary = next((data for kind, data in chunks if kind == 0x004E4942), None)
    else:
        require(path.suffix.lower() == '.gltf', 'RIG_FORMAT', 'Expected .gltf or .glb.')
        document = decode_json(raw)
    require(isinstance(document, dict) and document.get('asset', {}).get('version') == '2.0',
            'RIG_GLTF_VERSION', 'Only glTF 2.0 is supported.')
    missing = set(document.get('extensionsRequired', []))-MATERIAL_EXTENSIONS
    require(not missing, 'RIG_GLTF_EXTENSION', 'Decode required unsupported extensions before import.', observed=sorted(missing))
    result = GltfDocument(path, document, [], limits, [path], source_reader=source_reader,
                          source_sha256=hashlib.sha256(raw).hexdigest())
    buffers = document.get('buffers', [])
    require(len(buffers) <= 1024, 'RIG_GLTF_BUFFER', 'Too many buffers.')
    total = sum(_count(b.get('byteLength'), 'buffer byte length', limits['dependency_bytes']) for b in buffers)
    require(total <= limits['dependency_bytes'], 'RIG_LIMIT_DEPENDENCIES', 'Declared buffers exceed the byte budget.')
    for i, buffer in enumerate(buffers):
        content = result.uri(buffer['uri'], limits['dependency_bytes']) if 'uri' in buffer else binary if i == 0 else None
        require(content is not None and len(content) >= buffer['byteLength'], 'RIG_GLTF_BUFFER', 'Buffer content is missing or truncated.')
        if 'uri' not in buffer:require(len(content)-buffer['byteLength'] <= 3, 'RIG_GLB_PADDING', 'GLB BIN padding exceeds three bytes.')
        result.buffers.append(content[:buffer['byteLength']])
    return result


def node_matrix(node: dict) -> np.ndarray:
    if 'matrix' in node:
        require(not set(node) & {'translation', 'rotation', 'scale'}, 'RIG_GLTF_TRANSFORM', 'A node cannot combine matrix and TRS.')
        values = np.asarray(node['matrix'], dtype=float)
        require(values.shape == (16,) and np.isfinite(values).all(), 'RIG_GLTF_TRANSFORM', 'Invalid glTF node matrix.')
        result = values.reshape(4, 4).T
    else:
        rotation = quaternion(node.get('rotation', [0, 0, 0, 1]), strict=True)
        scale = vector(node.get('scale', [1, 1, 1]));translation = vector(node.get('translation', [0, 0, 0]))
        result = np.eye(4);result[:3, :3] = qmatrix(rotation) @ np.diag(scale);result[:3, 3] = translation
    require(np.allclose(result[3], [0, 0, 0, 1]),
            'RIG_SINGULAR_TRANSFORM', 'Node transform must be affine and nonsingular.')
    # Determinants carry cubic units: a well-conditioned micrometer conversion
    # has det=1e-18. Test column directions independently of their magnitudes,
    # retaining the existing tolerance against almost dependent directions.
    linear = result[:3, :3]
    magnitudes = np.max(np.abs(linear), axis=0)
    require(np.all(magnitudes > 0), 'RIG_SINGULAR_TRANSFORM', 'Node transform has a zero scale axis.')
    require(abs(np.linalg.det(linear / magnitudes)) > 1e-12,
            'RIG_SINGULAR_TRANSFORM', 'Node transform axes are numerically dependent.')
    # A nonzero determinant alone does not guarantee that downstream inverse
    # transforms (including translation) are representable in float64.
    try:
        with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
            inverse = np.linalg.inv(result)
    except np.linalg.LinAlgError as exc:
        raise RigError('RIG_SINGULAR_TRANSFORM', 'Node transform cannot be inverted.') from exc
    require(np.isfinite(inverse).all(), 'RIG_SINGULAR_TRANSFORM', 'Node transform inverse is not finite.')
    return result


def scene_graph(document: GltfDocument) -> tuple[dict, dict, list[int]]:
    nodes = document.json.get('nodes', [])
    require(len(nodes) <= 10000, 'RIG_LIMIT_NODES', 'Scene node count exceeds the bound.')
    parents = {}
    for i, node in enumerate(nodes):
        for child in node.get('children', []):
            child = _index(child, len(nodes), 'child node')
            require(child not in parents, 'RIG_GLTF_HIERARCHY', 'A node has multiple parents.')
            parents[child] = i
    scenes = document.json.get('scenes', [])
    require(bool(scenes), 'RIG_GLTF_SCENE', 'The model needs an explicit scene with mesh instances.')
    selected = scenes[_index(document.json.get('scene', 0), len(scenes), 'scene')]
    roots = selected.get('nodes', [])
    require(len(set(roots)) == len(roots), 'RIG_GLTF_HIERARCHY', 'Scene roots must be unique.')
    world, active, order = {}, set(), []
    def visit(i, parent_world):
        _index(i, len(nodes), 'scene node')
        require(i not in active and i not in world, 'RIG_GLTF_HIERARCHY', 'Scene node hierarchy has a cycle or repeated instance.')
        active.add(i);world[i] = parent_world @ node_matrix(nodes[i]);order.append(i)
        for child in nodes[i].get('children', []):visit(child, world[i])
        active.remove(i)
    for root in roots:
        require(root not in parents, 'RIG_GLTF_HIERARCHY', 'A scene root cannot also have a parent.')
        visit(root, np.eye(4))
    return parents, world, order


class GltfBuilder:
    def __init__(self):
        self.json = dict(asset=dict(version='2.0', generator='Hatch Pet rigging 1.0.0'),
                         buffers=[], bufferViews=[], accessors=[], nodes=[], meshes=[], scenes=[], scene=0)
        self.binary = bytearray()

    def view(self, payload: bytes, target=None) -> int:
        self.binary.extend(b'\x00'*(-len(self.binary) % 4))
        view = dict(buffer=0, byteOffset=len(self.binary), byteLength=len(payload))
        if target is not None:view['target'] = target
        self.binary.extend(payload);self.json['bufferViews'].append(view)
        return len(self.json['bufferViews'])-1

    def accessor(self, values, kind: str, component=5126, *, target=None, bounds=False) -> int:
        a = np.asarray(values, dtype=DTYPES[component]).reshape(-1, WIDTHS[kind])
        require(len(a) > 0 and np.isfinite(a).all(), 'RIG_EXPORT_ACCESSOR', 'Export accessor must contain finite data.')
        if kind == 'MAT4':a = a.reshape(-1, 4, 4).transpose(0, 2, 1).reshape(-1, 16)
        value = dict(bufferView=self.view(a.tobytes(), target), componentType=component, count=len(a), type=kind)
        if bounds:
            value['min'] = a.min(axis=0).tolist();value['max'] = a.max(axis=0).tolist()
        self.json['accessors'].append(value)
        return len(self.json['accessors'])-1

    def write(self, destination: str | Path) -> None:
        destination = Path(destination)
        self.json['buffers'] = [dict(byteLength=len(self.binary))]
        if destination.suffix.lower() == '.gltf':
            # One embedded, local buffer keeps the two-file promotion problem out
            # of the atomic core while remaining ordinary standards-compliant glTF.
            self.json['buffers'][0]['uri'] = 'data:application/octet-stream;base64,'+base64.b64encode(self.binary).decode('ascii')
            atomic_bytes(destination, (json.dumps(self.json, separators=(',', ':'), allow_nan=False)+'\n').encode())
        else:
            require(destination.suffix.lower() == '.glb', 'RIG_EXPORT_FORMAT', 'Expected .glb or .gltf destination.')
            payload = json.dumps(self.json, separators=(',', ':'), allow_nan=False).encode('utf-8')
            payload += b' '*(-len(payload) % 4)
            binary = bytes(self.binary)+b'\x00'*(-len(self.binary) % 4)
            chunks = struct.pack('<II', len(payload), 0x4E4F534A)+payload+struct.pack('<II', len(binary), 0x004E4942)+binary
            atomic_bytes(destination, struct.pack('<4sII', b'glTF', 2, 12+len(chunks))+chunks)


def _export_geometry(builder: GltfBuilder, geometry, *, weights=None, bone_nodes=None, root_node=None):
    from rig_geometry import vertex_normals
    from rig_models import validate_geometry
    validate_geometry(geometry)
    builder.json['materials'] = deepcopy(geometry.materials)
    if geometry.extensions_used:builder.json['extensionsUsed'] = geometry.extensions_used.copy()
    if geometry.images:
        builder.json['images'] = [dict(name=i.name, mimeType=i.mime_type, bufferView=builder.view(i.data)) for i in geometry.images]
    if geometry.textures:builder.json['textures'] = deepcopy(geometry.textures)
    if geometry.samplers:builder.json['samplers'] = deepcopy(geometry.samplers)
    mesh_nodes = {}
    for mesh in geometry.meshes:
        attributes = {'POSITION': builder.accessor(mesh.positions, 'VEC3', target=34962, bounds=True)}
        source_attributes = dict(mesh.attributes)
        source_attributes.setdefault('NORMAL', vertex_normals(mesh.positions, mesh.triangles))
        for name, values in source_attributes.items():
            attributes[name] = builder.accessor(values, 'VEC'+str(values.shape[1]), target=34962)
        primitive = dict(attributes=attributes, indices=builder.accessor(mesh.triangles.ravel(), 'SCALAR', 5125, target=34963),
                         material=mesh.material, mode=4)
        if mesh.morphs:
            primitive['targets'] = [{key: builder.accessor(values, 'VEC3', target=34962,
                                    bounds=key == 'POSITION') for key, values in target.items()} for target in mesh.morphs.values()]
        mesh_value = dict(name=mesh.name or mesh.id, primitives=[primitive],
                          extras=dict(hatchMeshId=mesh.id, hatchRegion=mesh.region))
        if mesh.morphs:
            mesh_value['extras']['targetNames'] = list(mesh.morphs)
            mesh_value['weights'] = [mesh.morph_defaults.get(name, 0.) for name in mesh.morphs]
        builder.json['meshes'].append(mesh_value)
        node = dict(name=mesh.id, mesh=len(builder.json['meshes'])-1)
        if weights is not None:
            skin = weights[mesh.id]
            require(skin.indices.shape[1] % 4 == 0, 'RIG_INFLUENCE_PROFILE', 'glTF influences must use groups of four.')
            for i in range(skin.indices.shape[1]//4):
                indices = skin.indices[:, i*4:(i+1)*4].copy()
                indices[skin.weights[:, i*4:(i+1)*4] == 0] = 0
                attributes['JOINTS_'+str(i)] = builder.accessor(indices, 'VEC4', 5123, target=34962)
                attributes['WEIGHTS_'+str(i)] = builder.accessor(skin.weights[:, i*4:(i+1)*4], 'VEC4', target=34962)
            builder.json.setdefault('skins', []).append(dict(name=mesh.id+'-skin', skeleton=root_node,
                joints=[bone_nodes[name] for name in skin.joints], inverseBindMatrices=builder.accessor(skin.inverse_bind, 'MAT4')))
            node['skin'] = len(builder.json['skins'])-1
        mesh_nodes[mesh.id] = len(builder.json['nodes'])
        builder.json['nodes'].append(node)
    return mesh_nodes


def export_geometry(geometry, destination: str | Path, *, provenance=None) -> dict:
    """Export actual unrigged input geometry for reproducible import demonstrations."""
    from rig_errors import sha256
    builder = GltfBuilder()
    nodes = _export_geometry(builder, geometry)
    builder.json['scenes'] = [dict(nodes=list(nodes.values()))]
    if provenance:builder.json['asset']['extras'] = dict(hatchSource=provenance)
    builder.write(destination)
    return dict(path=str(destination), sha256=sha256(destination), vertices=geometry.vertex_count, skinned=False)


def export_gltf(rig, destination: str | Path, *, bake=True) -> dict:
    """Export evaluated channels; Blender/IK constraints are not claimed to survive."""
    from rig_animation import bake_clip, validate_animation
    from rig_errors import sha256
    from rig_models import rig_signature
    from rig_ik import validate_controllers
    from rig_skinning import validate_skin
    from rig_skeleton import validate_skeleton
    validate_skeleton(rig.skeleton, rig.manifest.get('limits'))
    validate_controllers(rig.skeleton, rig.manifest['controllers'])
    require(set(rig.weights) == set(rig.geometry.by_id()), 'RIG_MISSING_SKIN', 'Cannot export a rig with missing bindings.')
    for mesh in rig.geometry.meshes:validate_skin(rig.weights[mesh.id], mesh, rig.skeleton, rig.manifest.get('limits'))
    builder = GltfBuilder();bones = rig.skeleton['bones']
    bone_nodes = {b['id']: i for i, b in enumerate(bones)}
    for bone in bones:
        node = dict(name=bone['id'], **deepcopy(bone['rest']), extras=dict(hatchBone={
            k: deepcopy(bone[k]) for k in ('id', 'role', 'tail', 'roll', 'mirror', 'deform')}))
        children = [bone_nodes[b['id']] for b in bones if b['parent'] == bone['id']]
        if children:node['children'] = children
        builder.json['nodes'].append(node)
    root_node = bone_nodes[rig.skeleton['root']]
    mesh_nodes = _export_geometry(builder, rig.geometry, weights=rig.weights, bone_nodes=bone_nodes, root_node=root_node)
    builder.json['scenes'] = [dict(nodes=[root_node, *mesh_nodes.values()])]
    key_count = 0
    for clip in rig.clips.values():
        validate_animation(rig, clip)
        clip = bake_clip(rig, clip) if bake else deepcopy(clip)
        samplers, channels = [], []
        for track in clip['tracks']:
            path = track['path'];values = np.array(track['values'], dtype=float)
            interpolation = track['interpolation']
            if interpolation == 'CUBICSPLINE':
                values = np.stack([track['in_tangents'], values, track['out_tangents']], axis=1)
            if path == 'weights':kind, node = 'SCALAR', mesh_nodes[track['target']]
            else:kind, node = ('VEC4' if path == 'rotation' else 'VEC3'), bone_nodes[track['target']]
            sampler = dict(input=builder.accessor(track['times'], 'SCALAR', bounds=True),
                           output=builder.accessor(values, kind), interpolation=interpolation)
            samplers.append(sampler);channels.append(dict(sampler=len(samplers)-1, target=dict(node=node, path=path)))
            key_count += len(track['times'])
        require(channels, 'RIG_NO_ANIMATION_CHANNELS', 'A named clip must contain executable channels.')
        builder.json.setdefault('animations', []).append(dict(name=clip['id'], samplers=samplers, channels=channels,
            extras=dict(hatch=dict(loop=clip['loop'], root_motion=clip['root_motion'], events=clip['events'], contacts=clip['contacts']))))
    require(key_count <= rig.manifest.get('limits', DEFAULT_LIMITS)['keyframes'], 'RIG_LIMIT_KEYFRAMES', 'Baked export exceeds the keyframe budget.')
    builder.json['asset']['extras'] = dict(hatchRig=dict(id=rig.manifest['id'], version=rig.manifest['version'],
        sha256=rig_signature(rig.manifest), fixture=rig.manifest['source']['fixture'],
        source_sha256=rig.manifest['source']['sha256'], review_status=rig.manifest['review']['status'],
        authoring_controls='Native/Blender only; exported joint and morph channels are evaluated motion.'))
    builder.write(destination)
    receipt = dict(schema_version=1, format=Path(destination).suffix[1:].lower(), path=str(destination), sha256=sha256(destination),
                rig_sha256=rig_signature(rig.manifest), vertices=rig.geometry.vertex_count,
                skins=len(rig.weights), joints=len(bones), animations=list(rig.clips), keyframes=key_count,
                independently_verified=False)
    return validate_contract(receipt, 'RigExportReceipt')
