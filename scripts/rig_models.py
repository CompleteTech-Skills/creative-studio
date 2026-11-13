"""Typed geometry/skin data and deterministic, non-pickle binary storage."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import sys
from pathlib import Path
import stat
from typing import Any
import zipfile
import numpy as np

from rig_contracts import DEFAULT_LIMITS, validate_contract
from rig_errors import (atomic_bytes, decode_json, finite_json, object_hash, read_json, require,
                        safe_path, sha256)


@dataclass
class Mesh:
    id: str
    positions: np.ndarray
    triangles: np.ndarray
    material: int = 0
    attributes: dict[str, np.ndarray] = field(default_factory=dict)
    morphs: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)
    name: str = ''
    region: str = ''
    source_node: int = -1
    morph_defaults: dict[str, float] = field(default_factory=dict)


@dataclass
class TextureImage:
    name: str
    mime_type: str
    data: bytes


@dataclass
class Geometry:
    meshes: list[Mesh]
    materials: list[dict] = field(default_factory=list)
    images: list[TextureImage] = field(default_factory=list)
    textures: list[dict] = field(default_factory=list)
    samplers: list[dict] = field(default_factory=list)
    extensions_used: list[str] = field(default_factory=list)

    @property
    def vertex_count(self) -> int:
        return sum(len(mesh.positions) for mesh in self.meshes)

    @property
    def triangle_count(self) -> int:
        return sum(len(mesh.triangles) for mesh in self.meshes)

    def by_id(self) -> dict[str, Mesh]:
        return {mesh.id: mesh for mesh in self.meshes}


@dataclass
class SkinWeights:
    mesh: str
    joints: list[str]
    indices: np.ndarray
    weights: np.ndarray
    inverse_bind: np.ndarray
    locked: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    method: str = 'distance'
    fallback_reason: str | None = None


@dataclass
class RigAsset:
    manifest: dict
    geometry: Geometry
    weights: dict[str, SkinWeights] = field(default_factory=dict)
    clips: dict[str, dict] = field(default_factory=dict)
    root: Path | None = None

    @property
    def skeleton(self) -> dict:
        require(self.manifest.get('skeleton') is not None, 'RIG_NO_SKELETON',
                'Generate or import a skeleton before this operation.')
        return self.manifest['skeleton']


# Texture fields in the material extensions already supported by rig_gltf.
# Only declared texture slots are traversed: arbitrary extras are not bindings.
_MATERIAL_TEXTURE_SLOTS = {
    'KHR_materials_clearcoat': ('clearcoatTexture', 'clearcoatRoughnessTexture', 'clearcoatNormalTexture'),
    'KHR_materials_iridescence': ('iridescenceTexture', 'iridescenceThicknessTexture'),
    'KHR_materials_sheen': ('sheenColorTexture', 'sheenRoughnessTexture'),
    'KHR_materials_specular': ('specularTexture', 'specularColorTexture'),
    'KHR_materials_transmission': ('transmissionTexture',),
    'KHR_materials_volume': ('thicknessTexture',),
    'KHR_materials_anisotropy': ('anisotropyTexture',),
}


def _material_number(value, label: str, *, minimum=None, maximum=None) -> None:
    # Compare ints before isfinite (which converts to float) so even malicious
    # huge native integers fail with a domain diagnostic rather than OverflowError.
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and -sys.float_info.max <= value <= sys.float_info.max
            and math.isfinite(value)
            and (minimum is None or value >= minimum)
            and (maximum is None or value <= maximum),
            'RIG_MATERIAL', 'Material numeric parameter is invalid.', asset=label)


def _material_index(value, size: int, label: str, *, code='RIG_TEXTURE_REFERENCE') -> int:
    # Keep the existing native glTF _index profile: actual ints, never bools.
    require(type(value) is int and 0 <= value < size,
            code, 'Material/texture reference index is invalid.', asset=label)
    return value


def _texcoord_index(value, label: str) -> int:
    require(type(value) is int and value >= 0, 'RIG_TEXTURE_REFERENCE',
            'Texture coordinate set must be a nonnegative integer.', asset=label)
    return value


def _material_vector(value, width: int, label: str, *, minimum=None, maximum=None) -> None:
    require(isinstance(value, (list, tuple)) and len(value) == width,
            'RIG_MATERIAL', 'Material vector has an invalid shape.', asset=label)
    for item in value:
        _material_number(item, label, minimum=minimum, maximum=maximum)


def _texture_binding(info, geometry: Geometry, label: str, *, normal=False, occlusion=False) -> int:
    require(isinstance(info, dict), 'RIG_TEXTURE_REFERENCE', 'Texture information must be an object.', asset=label)
    _material_index(info.get('index'), len(geometry.textures), label)
    # A coordinate-set index need not refer to a globally declared resource;
    # its actual availability is checked on every assigned primitive below.
    coord = info.get('texCoord', 0)
    coord = _texcoord_index(coord, label + ':texCoord')
    extensions = info.get('extensions', {})
    require(isinstance(extensions, dict), 'RIG_MATERIAL', 'Texture extensions must be an object.', asset=label)
    if 'KHR_texture_transform' in extensions:
        transform = extensions['KHR_texture_transform']
        require(isinstance(transform, dict), 'RIG_MATERIAL', 'Texture transform must be an object.', asset=label)
        for field in ('offset', 'scale'):
            if field in transform:
                _material_vector(transform[field], 2, label + ':' + field)
        if 'rotation' in transform:
            _material_number(transform['rotation'], label + ':rotation')
        if 'texCoord' in transform:
            coord = _texcoord_index(transform['texCoord'], label + ':transform.texCoord')
    if normal and 'scale' in info:
        # glTF normal scale is finite, not restricted to [0,1] or nonnegative.
        _material_number(info['scale'], label + ':scale')
    if occlusion and 'strength' in info:
        _material_number(info['strength'], label + ':strength', minimum=0, maximum=1)
    return coord


def _validate_materials(geometry: Geometry) -> list[set[int]]:
    """Validate portable core bindings without changing source material dictionaries."""
    require(isinstance(geometry.materials, list) and isinstance(geometry.textures, list)
            and isinstance(geometry.samplers, list), 'RIG_MATERIAL', 'Material resources must be arrays.')
    finite_json(geometry.materials)
    finite_json(geometry.textures)
    finite_json(geometry.samplers)
    for index, sampler in enumerate(geometry.samplers):
        require(isinstance(sampler, dict), 'RIG_TEXTURE_SAMPLER', 'Texture sampler must be an object.')
        for key, allowed in (('magFilter', (9728, 9729)),
                             ('minFilter', (9728, 9729, 9984, 9985, 9986, 9987)),
                             ('wrapS', (33071, 33648, 10497)), ('wrapT', (33071, 33648, 10497))):
            if key in sampler:
                value = sampler[key]
                require(type(value) is int and value in allowed, 'RIG_TEXTURE_SAMPLER', 'Unsupported texture sampler parameter.',
                        asset=str(index) + ':' + key)
    for index, texture in enumerate(geometry.textures):
        require(isinstance(texture, dict), 'RIG_TEXTURE_REFERENCE', 'Texture must be an object.')
        _material_index(texture.get('source'), len(geometry.images), str(index) + ':source')
        if 'sampler' in texture:
            _material_index(texture['sampler'], len(geometry.samplers), str(index) + ':sampler')
    bindings = []
    for index, material in enumerate(geometry.materials):
        label = 'material-' + str(index)
        require(isinstance(material, dict), 'RIG_MATERIAL', 'Material must be an object.', asset=label)
        pbr = material.get('pbrMetallicRoughness', {})
        require(isinstance(pbr, dict), 'RIG_MATERIAL', 'PBR parameters must be an object.', asset=label)
        if 'baseColorFactor' in pbr:
            _material_vector(pbr['baseColorFactor'], 4, label + ':baseColorFactor', minimum=0, maximum=1)
        for key in ('metallicFactor', 'roughnessFactor'):
            if key in pbr:
                _material_number(pbr[key], label + ':' + key, minimum=0, maximum=1)
        if 'emissiveFactor' in material:
            _material_vector(material['emissiveFactor'], 3, label + ':emissiveFactor', minimum=0, maximum=1)
        if 'alphaCutoff' in material:
            _material_number(material['alphaCutoff'], label + ':alphaCutoff', minimum=0)
        if 'alphaMode' in material:
            require(material['alphaMode'] in ('OPAQUE', 'MASK', 'BLEND'),
                    'RIG_MATERIAL', 'Unsupported alpha mode.', asset=label)
        if 'doubleSided' in material:
            require(type(material['doubleSided']) is bool, 'RIG_MATERIAL', 'doubleSided must be boolean.', asset=label)
        coords = set()
        for key in ('baseColorTexture', 'metallicRoughnessTexture'):
            if key in pbr:
                coords.add(_texture_binding(pbr[key], geometry, label + ':' + key))
        for key in ('normalTexture', 'occlusionTexture', 'emissiveTexture'):
            if key in material:
                coords.add(_texture_binding(material[key], geometry, label + ':' + key,
                                            normal=key == 'normalTexture', occlusion=key == 'occlusionTexture'))
        extensions = material.get('extensions', {})
        require(isinstance(extensions, dict), 'RIG_MATERIAL', 'Material extensions must be an object.', asset=label)
        for name, fields in _MATERIAL_TEXTURE_SLOTS.items():
            if name not in extensions:
                continue
            extension = extensions[name]
            require(isinstance(extension, dict), 'RIG_MATERIAL', 'Material extension must be an object.', asset=name)
            for key in fields:
                if key in extension:
                    coords.add(_texture_binding(extension[key], geometry, label + ':' + name + ':' + key,
                                                normal=key == 'clearcoatNormalTexture'))
        bindings.append(coords)
    return bindings


def validate_geometry(geometry: Geometry, limits: dict | None = None) -> None:
    limits = limits or DEFAULT_LIMITS
    require(bool(geometry.meshes) and len(geometry.meshes) <= 4096, 'RIG_GEOMETRY', 'Expected 1–4096 mesh primitives.')
    require(geometry.vertex_count <= limits['vertices'], 'RIG_LIMIT_VERTICES', 'Vertex budget exceeded.',
            expected=limits['vertices'], observed=geometry.vertex_count)
    require(geometry.triangle_count <= limits['triangles'], 'RIG_LIMIT_TRIANGLES', 'Triangle budget exceeded.')
    require(len(geometry.by_id()) == len(geometry.meshes), 'RIG_DUPLICATE_MESH', 'Mesh IDs must be unique.')
    bindings = _validate_materials(geometry)
    allocation = 0
    for mesh in geometry.meshes:
        v, f = np.asarray(mesh.positions), np.asarray(mesh.triangles)
        require(v.ndim == 2 and v.shape[1] == 3 and len(v) > 0 and np.isfinite(v).all(),
                'RIG_VERTICES', 'Mesh positions must be nonempty finite Nx3 arrays.', asset=mesh.id)
        require(f.ndim == 2 and f.shape[1] == 3 and len(f) > 0 and f.dtype.kind in 'iu'
                and f.min() >= 0 and f.max() < len(v), 'RIG_TRIANGLES', 'Triangle indices are invalid.', asset=mesh.id)
        material = _material_index(mesh.material, len(geometry.materials), mesh.id, code='RIG_MATERIAL')
        for coord in bindings[material]:
            require(coord in (0, 1) and 'TEXCOORD_' + str(coord) in mesh.attributes, 'RIG_TEXTURE_UV',
                    'Assigned material requires a missing texture coordinate set.', asset=mesh.id)
        require(len(mesh.morphs) <= limits['morphs'], 'RIG_LIMIT_MORPHS', 'Morph target budget exceeded.')
        require(set(mesh.morph_defaults) <= set(mesh.morphs) and all(np.isfinite(v) and -2 <= v <= 2 for v in mesh.morph_defaults.values()),
                'RIG_MORPH_RANGE', 'Default morph weights must reference targets within [-2, 2].')
        for name, values in mesh.attributes.items():
            width = 3 if name == 'NORMAL' else 4 if name in ('TANGENT', 'COLOR_0') else 2
            require(name in ('NORMAL', 'TANGENT', 'COLOR_0', 'TEXCOORD_0', 'TEXCOORD_1'),
                    'RIG_ATTRIBUTE', 'Unsupported native attribute: ' + name)
            require(values.shape == (len(v), width) and np.isfinite(values).all(),
                    'RIG_ATTRIBUTE', 'Invalid vertex attribute array.', asset=mesh.id + ':' + name)
            if name == 'COLOR_0':
                require(np.all((values >= 0) & (values <= 1)), 'RIG_COLOR_RANGE',
                        'Vertex color components must be in [0, 1].', asset=mesh.id)
            allocation += values.nbytes
        for name, targets in mesh.morphs.items():
            require('POSITION' in targets, 'RIG_MORPH', 'Each native morph needs position deltas.', asset=name)
            for semantic, values in targets.items():
                require(semantic in ('POSITION', 'NORMAL', 'TANGENT') and values.shape == v.shape
                        and np.isfinite(values).all(), 'RIG_MORPH_TOPOLOGY',
                        'Morph deltas must match the original vertex topology.', asset=name)
                allocation += values.nbytes
        allocation += v.nbytes + f.nbytes
    require(all(i.mime_type in ('image/png', 'image/jpeg') for i in geometry.images),
            'RIG_TEXTURE_FORMAT', 'Use PNG or JPEG textures for the portable core profile.')
    allocation += sum(len(i.data) for i in geometry.images)
    require(allocation * 4 <= limits['memory_bytes'], 'RIG_LIMIT_MEMORY',
            'Estimated geometry processing allocation exceeds the memory budget.', observed=allocation * 4)


def _binary_bundle(metadata: dict, arrays: dict[str, np.ndarray]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        payloads = {'metadata.json': json.dumps(metadata, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()}
        for key, array in arrays.items():
            data = io.BytesIO()
            np.lib.format.write_array(data, np.asarray(array), allow_pickle=False)
            payloads[key + '.npy'] = data.getvalue()
        for name, payload in sorted(payloads.items()):
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, payload)
    return stream.getvalue()


def _read_bundle(path: Path, limits: dict) -> tuple[dict, dict]:
    require(path.stat().st_size <= limits['dependency_bytes'], 'RIG_LIMIT_BINARY', 'Binary asset exceeds its byte budget.')
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        require(len(infos) <= limits['archive_files'], 'RIG_LIMIT_BINARY', 'Too many binary members.')
        names = [i.filename for i in infos]
        require(len(set(names)) == len(names) and 'metadata.json' in names,
                'RIG_BINARY', 'Binary data needs unique members and metadata.')
        require(sum(i.file_size for i in infos) * 3 <= limits['memory_bytes'],
                'RIG_LIMIT_MEMORY', 'Expanded binary data exceeds its processing budget.')
        for info in infos:
            require(info.orig_filename == info.filename and '/' not in info.filename
                    and '\\' not in info.filename and (info.filename == 'metadata.json'
                    or info.filename.endswith('.npy')) and stat.S_IFMT(info.external_attr >> 16) != stat.S_IFLNK,
                    'RIG_BINARY', 'Unsafe binary member name or type.')
        metadata = decode_json(archive.read('metadata.json'))
        arrays = {}
        for name in names:
            if name.endswith('.npy'):
                with archive.open(name) as raw:
                    version = np.lib.format.read_magic(raw)
                    require(version in ((1, 0), (2, 0)), 'RIG_BINARY', 'Unsupported NumPy array format.')
                    shape, _, dtype = (np.lib.format.read_array_header_1_0(raw) if version == (1, 0)
                                       else np.lib.format.read_array_header_2_0(raw))
                    count = 1
                    for size in shape:
                        require(isinstance(size, int) and 0 <= size <= 2_000_000, 'RIG_BINARY', 'Invalid array dimensions.')
                        count *= size
                    require(not dtype.hasobject and dtype.kind in 'fiu' and count * dtype.itemsize <= limits['memory_bytes'] // 3,
                            'RIG_LIMIT_MEMORY', 'Unsafe or oversized NumPy array header.')
                with archive.open(name) as raw:
                    arrays[name[:-4]] = np.lib.format.read_array(raw, allow_pickle=False)
    return metadata, arrays


def _store(root: Path, folder: str, payload: bytes) -> dict:
    digest = hashlib.sha256(payload).hexdigest()
    name = folder + '/' + digest + '.npz'
    target = safe_path(root, name)
    if target.exists():
        require(sha256(target) == digest, 'RIG_HASH', 'Content-addressed data was modified.', asset=name)
    else:
        atomic_bytes(target, payload)
    return dict(path=name, sha256=digest)


def save_geometry(root: Path, geometry: Geometry, limits=None) -> dict:
    validate_geometry(geometry, limits)
    arrays, meshes = {}, []
    for i, mesh in enumerate(geometry.meshes):
        prefix = f'm{i}'
        arrays[prefix + '.positions'] = np.asarray(mesh.positions, dtype='<f8')
        arrays[prefix + '.triangles'] = np.asarray(mesh.triangles, dtype='<u4')
        attributes, morphs = {}, {}
        for key, values in mesh.attributes.items():
            name = prefix + '.' + key; arrays[name] = values; attributes[key] = name
        for j, (key, values) in enumerate(mesh.morphs.items()):
            morphs[key] = {}
            for semantic, data in values.items():
                name = f'{prefix}.morph{j}.{semantic}'; arrays[name] = data; morphs[key][semantic] = name
        meshes.append(dict(id=mesh.id, name=mesh.name, region=mesh.region, source_node=mesh.source_node,
                           material=mesh.material, positions=prefix+'.positions', triangles=prefix+'.triangles',
                           attributes=attributes, morphs=morphs, morph_defaults=mesh.morph_defaults))
    images = []
    for i, image in enumerate(geometry.images):
        name = f'image{i}'; arrays[name] = np.frombuffer(image.data, dtype='u1')
        images.append(dict(name=image.name, mime_type=image.mime_type, data=name))
    metadata = dict(schema_version=1, meshes=meshes, materials=geometry.materials, images=images,
                    textures=geometry.textures, samplers=geometry.samplers, extensions_used=geometry.extensions_used)
    return dict(schema_version=1, version='1.0.0', data=_store(Path(root), 'geometry', _binary_bundle(metadata, arrays)),
                meshes=[m.id for m in geometry.meshes], vertices=geometry.vertex_count, triangles=geometry.triangle_count)


def load_geometry(root: Path, descriptor: dict, limits=None) -> Geometry:
    limits = limits or DEFAULT_LIMITS
    path = safe_path(root, descriptor['data']['path'], exists=True)
    require(sha256(path) == descriptor['data']['sha256'], 'RIG_HASH', 'Geometry bytes differ from their manifest.')
    meta, arrays = _read_bundle(path, limits)
    require(meta.get('schema_version') == 1, 'RIG_BINARY_VERSION', 'Unsupported geometry data version.')
    meshes = [Mesh(m['id'], arrays[m['positions']], arrays[m['triangles']], m['material'],
                   {k: arrays[v] for k, v in m['attributes'].items()},
                   {k: {s: arrays[v] for s, v in fields.items()} for k, fields in m['morphs'].items()},
                   m['name'], m['region'], m['source_node'], m.get('morph_defaults', {})) for m in meta['meshes']]
    images = [TextureImage(i['name'], i['mime_type'], arrays[i['data']].tobytes()) for i in meta['images']]
    value = Geometry(meshes, meta['materials'], images, meta['textures'], meta['samplers'], meta['extensions_used'])
    validate_geometry(value, limits)
    require(value.vertex_count == descriptor['vertices'] and value.triangle_count == descriptor['triangles']
            and [m.id for m in meshes] == descriptor['meshes'], 'RIG_GEOMETRY', 'Geometry inventory mismatch.')
    return value


def save_weights(root: Path, weights: SkinWeights, version='1.0.0') -> dict:
    metadata = dict(schema_version=1, mesh=weights.mesh, joints=weights.joints)
    arrays = dict(indices=np.asarray(weights.indices, dtype='<u2'), weights=np.asarray(weights.weights, dtype='<f8'),
                  inverse_bind=np.asarray(weights.inverse_bind, dtype='<f8'), locked=np.asarray(weights.locked, dtype='<u4'))
    return dict(schema_version=1, id=weights.mesh+'-skin', version=version, mesh=weights.mesh, joints=weights.joints,
                data=_store(Path(root), 'rig/weights', _binary_bundle(metadata, arrays)), method=weights.method,
                locked_vertices=weights.locked.tolist(), fallback_reason=weights.fallback_reason)


def load_weights(root: Path, descriptor: dict, limits=None) -> SkinWeights:
    path = safe_path(root, descriptor['data']['path'], exists=True)
    require(sha256(path) == descriptor['data']['sha256'], 'RIG_HASH', 'Skin bytes differ from their manifest.')
    meta, arrays = _read_bundle(path, limits or DEFAULT_LIMITS)
    require(meta == dict(schema_version=1, mesh=descriptor['mesh'], joints=descriptor['joints']),
            'RIG_SKIN', 'Skin metadata differs from the rig.')
    require(set(arrays) == {'indices', 'weights', 'inverse_bind', 'locked'}, 'RIG_SKIN', 'Skin arrays are incomplete.')
    require(arrays['locked'].tolist() == descriptor['locked_vertices'], 'RIG_SKIN', 'Manual weight lock inventory differs.')
    return SkinWeights(meta['mesh'], meta['joints'], arrays['indices'], arrays['weights'], arrays['inverse_bind'],
                       arrays['locked'], descriptor['method'], descriptor['fallback_reason'])


def rig_signature(manifest: dict) -> str:
    """Review and computed reports bind the core without recursive self-hashes."""
    return object_hash({k: v for k, v in manifest.items() if k not in ('review', 'validation')})


def load_rig(root: str | Path, verify_source: bool = True) -> RigAsset:
    root = Path(root).resolve()
    manifest = validate_contract(read_json(root/'rig.json'), 'Rig')
    limits = manifest['limits']
    require(sha256(safe_path(root, 'identity.json', exists=True)) == manifest['character']['identity_sha256'],
            'RIG_IDENTITY_CHANGED', 'Character identity bytes differ from the manifest.')
    identity=read_json(root/'identity.json')
    require(identity['id']==manifest['character']['id']==manifest['id'] and identity['fixture']==manifest['source']['fixture']
            and identity['source_sha256']==manifest['source']['sha256'] and identity['origin']==manifest['source']['origin'],
            'RIG_IDENTITY_CHANGED','Source origin, fixture classification and character identity must match their immutable record.')
    dependencies=manifest['source']['dependencies']
    require(len({d['path'] for d in dependencies})==len(dependencies),'RIG_SOURCE_INVENTORY','Source inventory repeats a path.')
    require(any(d['path']==manifest['source']['path'] and d['sha256']==manifest['source']['sha256'] for d in dependencies),
            'RIG_SOURCE_INVENTORY','Original model must be present in the source inventory.')
    require(len({d['mesh'] for d in manifest['skins']})==len(manifest['skins']) and
            len({d['id'] for d in manifest['animations']})==len(manifest['animations']),
            'RIG_ASSET_INVENTORY','Skin or animation descriptors repeat an identifier.')
    if verify_source:
        for descriptor in manifest['source']['dependencies']:
            path = safe_path(root, descriptor['path'], exists=True)
            require(sha256(path) == descriptor['sha256'], 'RIG_SOURCE_CHANGED',
                    'Original source changed; dependent rig evidence is stale.', asset=descriptor['path'])
    geometry = load_geometry(root, manifest['geometry'], limits)
    weights = {d['mesh']: load_weights(root, d, limits) for d in manifest['skins']}
    clips = {}
    for descriptor in manifest['animations']:
        path = safe_path(root, descriptor['path'], exists=True)
        require(sha256(path) == descriptor['sha256'], 'RIG_HASH', 'Animation content changed.', asset=descriptor['id'])
        clip = validate_contract(read_json(path), 'AnimationClip')
        require(clip['id'] == descriptor['id'] and clip['version'] == descriptor['version'],
                'RIG_ANIMATION', 'Animation identity/version mismatch.')
        clips[clip['id']] = clip
    for descriptor in manifest['review']['receipts']+manifest['validation']:
        require(sha256(safe_path(root,descriptor['path'],exists=True))==descriptor['sha256'],
                'RIG_EVIDENCE_CHANGED','Referenced review or validation evidence was modified.')
    for descriptor in manifest['review']['receipts']:
        receipt=validate_contract(read_json(safe_path(root,descriptor['path'],exists=True)),'RigReviewReceipt')
        for evidence in receipt['evidence']:
            require(sha256(safe_path(root,evidence['path'],exists=True))==evidence['sha256'],
                    'RIG_EVIDENCE_CHANGED','Evidence covered by a retained review was modified.')
    if manifest['review']['status']=='approved':
        from rig_validation import REVIEW_DIMENSIONS
        receipts=[read_json(safe_path(root,d['path'],exists=True)) for d in manifest['review']['receipts']]
        require(any(r.get('expected_sha256')==rig_signature(manifest) and r.get('reviewer_kind') in ('human','agent')
                    and set(r.get('dimensions',{}))==set(REVIEW_DIMENSIONS) and all(v['verdict']=='approved' for v in r['dimensions'].values()) for r in receipts),
                'RIG_REVIEW_MISSING','Approved status requires a matching real visual review receipt.')
    return RigAsset(manifest, geometry, weights, clips, root)
