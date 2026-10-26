"""Preserve source bytes, decode real geometry and retain compatible existing rigs."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import io
from pathlib import Path
import struct
import warnings
import zlib
import numpy as np

from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError, require, sha256, slug, timestamp
from rig_geometry import analyze_geometry, vertex_normals
from rig_gltf import GltfDocument, _index, node_matrix, read_gltf, scene_graph
from rig_math import decompose, transform_points
from rig_models import Geometry, Mesh, SkinWeights, TextureImage, validate_geometry
from rig_skeleton import validate_skeleton


@dataclass
class ImportedModel:
    geometry: Geometry
    source_files: list[Path]
    skeleton: dict | None = None
    weights: dict[str, SkinWeights] = field(default_factory=dict)
    clips: dict[str, dict] = field(default_factory=dict)
    report: dict = field(default_factory=dict)


def _verify_png_raster(data: bytes, size: tuple[int, int]) -> None:
    """Reject complete-looking PNGs that Pillow repairs with missing black rows.

    Check every chunk CRC (including IEND), the zlib footer and the exact
    filtered raster size for ordinary/Adam7 data. Inflate in bounded chunks;
    neither a joined IDAT stream nor an entire uncompressed raster is stored.
    Pixel reconstruction/filter validation still belongs to Pillow's decoder.
    """
    require(data.startswith(b'\x89PNG\r\n\x1a\n'), 'RIG_TEXTURE_DECODE', 'Invalid PNG signature.')
    view = memoryview(data)

    def chunks():
        offset = 8
        while offset < len(view):
            require(len(view) - offset >= 12, 'RIG_TEXTURE_DECODE', 'Truncated PNG chunk.')
            length = struct.unpack_from('>I', view, offset)[0]
            require(length <= len(view) - offset - 12, 'RIG_TEXTURE_DECODE', 'Truncated PNG chunk data or CRC.')
            kind = bytes(view[offset + 4:offset + 8]);end = offset + 8 + length
            yield offset, kind, view[offset + 8:end], end
            offset = end + 4

    seen_data = ended_data = seen_end = seen_palette = False
    expected = None
    for offset, kind, payload, end in chunks():
        require(all(65 <= b <= 90 or 97 <= b <= 122 for b in kind) and not kind[2] & 32,
                'RIG_TEXTURE_DECODE', 'Invalid PNG chunk type.')
        require(zlib.crc32(view[offset + 4:end]) == struct.unpack_from('>I', view, end)[0],
                'RIG_TEXTURE_DECODE', 'PNG chunk CRC does not match.')
        require(kind[0] & 32 or kind in (b'IHDR', b'PLTE', b'IDAT', b'IEND'),
                'RIG_TEXTURE_DECODE', 'Unsupported critical PNG chunk.')
        if offset == 8:
            require(kind == b'IHDR' and len(payload) == 13, 'RIG_TEXTURE_DECODE', 'PNG must begin with one IHDR.')
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            require((width, height) == size and color in depths and depth in depths[color]
                    and compression == filtering == 0 and interlace in (0, 1),
                    'RIG_TEXTURE_DECODE', 'Invalid PNG raster parameters.')
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
            passes = ((0, 0, 1, 1),) if not interlace else (
                (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
            expected = 0
            for x, y, dx, dy in passes:
                columns = max(0, (width - x + dx - 1) // dx)
                rows = max(0, (height - y + dy - 1) // dy)
                if columns and rows: expected += rows * (1 + (columns * channels * depth + 7) // 8)
        else:
            require(kind != b'IHDR', 'RIG_TEXTURE_DECODE', 'Duplicate PNG IHDR.')
        if kind == b'PLTE':
            require(not seen_data and not seen_palette and 0 < len(payload) <= 768 and len(payload) % 3 == 0
                    and color not in (0, 4), 'RIG_TEXTURE_DECODE', 'Invalid PNG palette.')
            seen_palette = True
        if kind == b'IDAT':
            require(not ended_data, 'RIG_TEXTURE_DECODE', 'PNG IDAT chunks must be consecutive.')
            seen_data = True
        elif seen_data:
            ended_data = True
        if kind == b'IEND':
            require(len(payload) == 0 and seen_data and end + 4 == len(view),
                    'RIG_TEXTURE_DECODE', 'Invalid PNG end-of-stream.')
            seen_end = True
    require(seen_end and (color != 3 or seen_palette), 'RIG_TEXTURE_DECODE', 'PNG end or palette is missing.')
    inflater = zlib.decompressobj();decoded = 0
    for _, kind, payload, _ in chunks():
        if kind != b'IDAT': continue
        for start in range(0, len(payload), 65536):
            pending = payload[start:start + 65536]
            require(not inflater.eof, 'RIG_TEXTURE_DECODE', 'PNG has data after its zlib stream.')
            while True:
                maximum = min(65536, expected - decoded + 1)
                output = inflater.decompress(pending, maximum)
                decoded += len(output)
                require(decoded <= expected and not inflater.unused_data,
                        'RIG_TEXTURE_DECODE', 'PNG raster has extra decoded or compressed data.')
                pending = inflater.unconsumed_tail
                if pending: continue
                if len(output) == maximum and not inflater.eof:
                    pending = b'';continue
                break
    require(inflater.eof and decoded == expected, 'RIG_TEXTURE_DECODE', 'PNG raster or zlib footer is incomplete.')


def _decode_texture(data: bytes, limits: dict, total_pixels: int = 0) -> tuple[str, int]:
    """Verify containers and decode pixels before accepting immutable source bytes.

    ``verify`` alone does not decode JPEG entropy data. Keep the existing
    aggregate working-memory bound ahead of either pixel allocation and never
    repair/re-encode a damaged source or change Pillow's process-wide policy.
    """
    from PIL import Image, ImageFile
    require(len(data) <= limits['source_bytes'], 'RIG_LIMIT_TEXTURE', 'Encoded texture exceeds its byte budget.')
    require(not ImageFile.LOAD_TRUNCATED_IMAGES, 'RIG_TEXTURE_POLICY',
            'Strict texture import requires Pillow LOAD_TRUNCATED_IMAGES to remain disabled.')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                require(image.format in ('PNG', 'JPEG') and getattr(image, 'n_frames', 1) == 1,
                        'RIG_TEXTURE_FORMAT', 'Portable textures must be static PNG/JPEG images.')
                pixels = image.width * image.height
                require(0 < pixels <= 16_777_216 and (total_pixels + pixels) * 16 <= limits['memory_bytes'] // 2,
                        'RIG_LIMIT_TEXTURE', 'Decoded textures exceed the pixel/memory budget.')
                mime = 'image/png' if image.format == 'PNG' else 'image/jpeg'
                if image.format == 'PNG':
                    _verify_png_raster(data, image.size)
                else:
                    from rig_jpeg import verify_jpeg
                    verify_jpeg(data, max_encoded_bytes=limits['source_bytes'],
                                max_working_bytes=limits['memory_bytes'] // 2 - total_pixels * 16,
                                expected_size=image.size)
                image.verify()
            # Reopen the same immutable bytes: verify invalidates the first
            # parser and does not establish that its actual pixels can decode.
            with Image.open(io.BytesIO(data)) as image:
                image.load()
        return mime, pixels
    except RigError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise RigError('RIG_LIMIT_TEXTURE', 'Texture exceeds Pillow decoding safeguards.') from exc
    except MemoryError as exc:
        raise RigError('RIG_LIMIT_TEXTURE', 'Texture working-memory allocation failed.') from exc
    except (OSError, ValueError, SyntaxError, zlib.error) as exc:
        raise RigError('RIG_TEXTURE_DECODE', 'Texture pixels or container cannot be decoded: ' + str(exc)) from exc


def _images(document: GltfDocument) -> list[TextureImage]:
    images = []
    total_pixels = 0
    for i, item in enumerate(document.json.get('images', [])):
        if 'uri' in item:
            data = document.uri(item['uri'])
        else:
            buffer, offset, length, _ = document.view(item['bufferView'])
            data = buffer[offset:offset+length]
        mime, pixels = _decode_texture(data, document.limits, total_pixels)
        total_pixels += pixels
        require(item.get('mimeType', mime) == mime, 'RIG_TEXTURE_FORMAT', 'Texture MIME does not match its actual bytes.')
        images.append(TextureImage(item.get('name', f'image-{i}'), mime, data))
    return images


def _import_skeleton(document: GltfDocument, parents, world, order, scale):
    nodes = document.json['nodes'];skins = document.json.get('skins', [])
    deform = set()
    for skin in skins:
        joints = skin.get('joints', [])
        require(bool(joints) and len(set(joints)) == len(joints), 'RIG_SKIN_JOINT', 'Imported skin joints must be nonempty and unique.')
        for joint in joints:
            _index(joint, len(nodes), 'joint');require(joint in world, 'RIG_SKIN_SCENE', 'Skin joint is outside the selected scene.')
        deform.update(joints)
    animated_nodes = {c['target']['node'] for a in document.json.get('animations', []) for c in a.get('channels', [])
                      if c['target'].get('path') != 'weights' and 'node' in c.get('target', {})}
    require(animated_nodes <= set(world), 'RIG_ANIMATION_SCENE', 'Animation targets nodes outside the selected scene.')
    if not skins and not animated_nodes:return None, {}, world
    # Unskinned parts get a rigid joint at their original scene transform.
    rigid = {i for i in order if 'mesh' in nodes[i] and 'skin' not in nodes[i]}
    deform.update(rigid)
    needed = set(deform) | animated_nodes
    for i in list(needed):
        while i in parents:
            i = parents[i];needed.add(i)
    order = [i for i in order if i in needed]
    ids, used = {}, set()
    for i in order:
        extra = nodes[i].get('extras', {}).get('hatchBone', {})
        name = extra.get('id', f'node-{i}-'+slug(nodes[i].get('name', 'joint')))
        require(isinstance(name, str) and name not in used, 'RIG_DUPLICATE_BONE', 'Imported bone identity is repeated.')
        ids[i] = name;used.add(name)
    roots = [i for i in order if i not in parents]
    virtual = len(roots) > 1
    normalized = {i: np.diag([scale, scale, scale, 1.]) @ world[i] for i in order}
    bones = []
    if virtual:
        require('import-root' not in used, 'RIG_DUPLICATE_BONE', 'Reserved import root collides with source bone.')
        bones.append(dict(schema_version=1, id='import-root', role='root', parent=None,
                          rest=decompose(np.eye(4)), head=[0., 0., 0.], tail=[0., .01, 0.], length=.01,
                          roll=0., deform=False, mirror=None, evidence=dict(source='existing-rig', reference='Common scene root', confidence=1.)))
    for i in order:
        n = nodes[i];extra = n.get('extras', {}).get('hatchBone', {})
        head = normalized[i][:3, 3]
        children = [c for c in n.get('children', []) if c in normalized and np.linalg.norm(normalized[c][:3, 3]-head) > 1e-7]
        if 'tail' in extra:tail = np.asarray(extra['tail'], dtype=float)*scale
        elif children:tail = normalized[children[0]][:3, 3]
        else:tail = head+normalized[i][:3, :3] @ [0, .05, 0]
        parent = parents.get(i)
        local = np.linalg.inv(normalized[parent]) @ normalized[i] if parent in normalized else normalized[i]
        mirror = extra.get('mirror')
        bones.append(dict(schema_version=1, id=ids[i], role=extra.get('role', 'unmapped-'+str(i)),
                          parent=ids[parent] if parent in ids else 'import-root' if virtual else None,
                          rest=decompose(local), head=head.tolist(), tail=tail.tolist(),
                          length=float(np.linalg.norm(tail-head)), roll=float(extra.get('roll', 0)),
                          deform=i in deform, mirror=mirror,
                          evidence=dict(source='existing-rig', reference=f'glTF node {i}', confidence=1.)))
    result = dict(schema_version=1, id='imported-skeleton', version='1.0.0',
                  root='import-root' if virtual else ids[roots[0]], bones=bones)
    validate_skeleton(result, document.limits)
    return result, ids, normalized


def _animations(document, ids, node_meshes, geometry, parents, scale):
    clips = {};total_keys = 0
    for index, animation in enumerate(document.json.get('animations', [])):
        require(index < document.limits['clips'], 'RIG_LIMIT_CLIPS', 'Imported animation clip budget exceeded.')
        name = slug(animation.get('name', f'animation-{index}'))
        if name in clips:name += '-'+str(index)
        tracks = [];duration = 0.;seen = set()
        for channel in animation.get('channels', []):
            sampler = animation['samplers'][_index(channel['sampler'], len(animation['samplers']), 'animation sampler')]
            target = channel['target'];node = target['node'];path = target['path']
            require((node, path) not in seen, 'RIG_ANIMATION_DUPLICATE', 'Animation repeats a target channel.')
            seen.add((node, path))
            require(path in ('translation', 'rotation', 'scale', 'weights'), 'RIG_ANIMATION_PATH', 'Unsupported animation target.')
            require('matrix' not in document.json['nodes'][node] or path == 'weights',
                    'RIG_ANIMATION_MATRIX', 'TRS animation cannot target a node with a matrix.')
            times = document.accessor(sampler['input']).ravel().astype(float)
            require(np.all(times >= 0) and np.all(np.diff(times) > 0), 'RIG_ANIMATION_TIME', 'Key times must be nonnegative and strictly increasing.')
            duration = max(duration, float(times[-1]));total_keys += len(times)
            require(total_keys <= document.limits['keyframes'], 'RIG_LIMIT_KEYFRAMES', 'Animation key budget exceeded.')
            interpolation = sampler.get('interpolation', 'LINEAR')
            require(interpolation in ('STEP', 'LINEAR', 'CUBICSPLINE'), 'RIG_INTERPOLATION', 'Unsupported interpolation.')
            raw = document.accessor(sampler['output']).astype(float)
            targets = node_meshes.get(node, []) if path == 'weights' else [ids.get(node)]
            require(bool(targets) and all(t is not None for t in targets), 'RIG_ANIMATION_REFERENCE', 'Animation references unavailable geometry or bones.')
            for destination in targets:
                width = len(geometry.by_id()[destination].morphs) if path == 'weights' else 4 if path == 'rotation' else 3
                require(width > 0 and raw.size == len(times)*width*(3 if interpolation == 'CUBICSPLINE' else 1),
                        'RIG_ANIMATION_VALUES', 'Animation output count does not match its target/time base.')
                values = raw.reshape(len(times), 3, width) if interpolation == 'CUBICSPLINE' else raw.reshape(len(times), width)
                if path in ('translation', 'scale') and node not in parents:values = values*scale
                item = dict(schema_version=1, target=destination, path=path, interpolation=interpolation, times=times.tolist())
                if interpolation == 'CUBICSPLINE':
                    item.update(values=values[:, 1].tolist(), in_tangents=values[:, 0].tolist(), out_tangents=values[:, 2].tolist())
                else:item['values'] = values.tolist()
                tracks.append(item)
        if tracks:
            require(duration <= document.limits['duration'], 'RIG_LIMIT_DURATION', 'Imported animation exceeds the duration limit.')
            clips[name] = dict(schema_version=1, kind='hatch-3d-animation', id=name, version='1.0.0',
                rig_id='imported', skeleton_version='1.0.0', duration=max(duration, 1/24), fps=24,
                loop=False, tracks=tracks, events=[], contacts=[], root_motion='embedded', interruptible=True,
                reduced_motion_pose='neutral', provenance=dict(schema_version=1, tool_version='1.0.0',
                operation='Imported existing glTF animation', created_at=timestamp(),
                input_hashes={'source': sha256(document.path)}, ancestry=[]))
    return clips


def _gltf_model(path, *, scale=1., limits=None) -> ImportedModel:
    document = read_gltf(path, limits)
    require(np.isfinite(scale) and 0 < scale <= 10000, 'RIG_SCALE', 'Import scale must be finite and positive.')
    parents, world, order = scene_graph(document)
    skeleton, ids, normalized = _import_skeleton(document, parents, world, order, scale)
    geometry = Geometry([], deepcopy(document.json.get('materials', [])), _images(document),
                        deepcopy(document.json.get('textures', [])), deepcopy(document.json.get('samplers', [])),
                        [x for x in document.json.get('extensionsUsed', []) if x in document.json.get('extensionsRequired', []) or x.startswith('KHR_materials') or x == 'KHR_texture_transform'])
    default_material = len(geometry.materials)
    if any('material' not in p for m in document.json.get('meshes', []) for p in m.get('primitives', [])):
        geometry.materials.append(dict(name='Default material'))
    weights, node_meshes = {}, {}
    for node_id in order:
        node = document.json['nodes'][node_id]
        if 'mesh' not in node:continue
        source_mesh = document.json.get('meshes', [])[_index(node['mesh'], len(document.json.get('meshes', [])), 'mesh')]
        for primitive_index, primitive in enumerate(source_mesh.get('primitives', [])):
            require(primitive.get('mode', 4) == 4, 'RIG_PRIMITIVE_MODE', 'Triangulate non-triangle primitives before rigging.')
            require(not primitive.get('extensions'), 'RIG_GLTF_EXTENSION', 'Decode compressed/extended primitives before rigging.')
            attributes = primitive['attributes']
            positions = document.accessor(attributes['POSITION']).astype(float)
            require(positions.shape[1] == 3, 'RIG_VERTICES', 'Positions must have VEC3 components.')
            indices = document.accessor(primitive['indices']).ravel() if 'indices' in primitive else np.arange(len(positions), dtype=np.uint32)
            require(indices.dtype.kind in 'iu' and len(indices) % 3 == 0, 'RIG_TRIANGLES', 'Triangle indices must be unsigned scalar triplets.')
            triangles = indices.reshape(-1, 3).astype(np.int64)
            name = f'mesh-{node_id}-{primitive_index}'
            native_id = source_mesh.get('extras', {}).get('hatchMeshId')
            if native_id and native_id not in geometry.by_id():name = native_id
            region = source_mesh.get('extras', {}).get('hatchRegion', '')
            matrix = np.diag([scale, scale, scale, 1.]) @ world[node_id]
            skin = None
            if 'skin' in node:
                original = document.json['skins'][_index(node['skin'], len(document.json.get('skins', [])), 'skin')]
                joints = original['joints']
                matrices = document.accessor(original['inverseBindMatrices']).reshape(-1, 4, 4).transpose(0, 2, 1) if 'inverseBindMatrices' in original else np.tile(np.eye(4), (len(joints), 1, 1))
                require(len(matrices) >= len(joints), 'RIG_BIND_MATRIX', 'Imported inverse bind array is incomplete.')
                matrices = matrices[:len(joints)]
                bind_shapes = np.array([normalized[j] @ m for j, m in zip(joints, matrices)])
                require(np.allclose(bind_shapes, bind_shapes[0], atol=1e-4), 'RIG_BIND_SHAPE',
                        'Inconsistent source bind shapes need explicit Blender repair; original bytes are preserved.')
                matrix = bind_shapes[0]
                sets = sorted(int(k.split('_')[1]) for k in attributes if k.startswith('JOINTS_'))
                require(sets and sets == list(range(len(sets))), 'RIG_MISSING_SKIN', 'Skinned mesh needs consecutive JOINTS/WEIGHTS sets.')
                require(len(sets)*4 <= document.limits['influences'], 'RIG_INFLUENCE_PROFILE', 'Select the explicit eight-influence profile to preserve additional weights.')
                js, ws = [], []
                for i in sets:
                    require('WEIGHTS_'+str(i) in attributes, 'RIG_MISSING_SKIN', 'Joint data are missing paired weights.')
                    ji, we = document.accessor(attributes['JOINTS_'+str(i)]), document.accessor(attributes['WEIGHTS_'+str(i)])
                    require(ji.shape == we.shape == (len(positions), 4) and ji.dtype.kind in 'iu', 'RIG_SKIN_SHAPE', 'Skin attributes must be vertex-aligned VEC4 arrays.')
                    js.append(ji);ws.append(we)
                skin = SkinWeights(name, [ids[j] for j in joints], np.concatenate(js, axis=1), np.concatenate(ws, axis=1),
                                   np.array([np.linalg.inv(normalized[j]) for j in joints]), method='imported')
            elif skeleton is not None:
                skin = SkinWeights(name, [ids[node_id]], np.zeros((len(positions), 4), dtype=np.uint16),
                    np.tile([1., 0, 0, 0], (len(positions), 1)), np.array([np.linalg.inv(normalized[node_id])]), method='rigid')
            decoded_attributes = {}
            direction_lengths = {}
            for semantic, accessor in attributes.items():
                if semantic == 'POSITION' or semantic.startswith(('JOINTS_', 'WEIGHTS_')):continue
                require(semantic in ('NORMAL', 'TANGENT', 'TEXCOORD_0', 'TEXCOORD_1', 'COLOR_0'),
                        'RIG_ATTRIBUTE', 'Unsupported attribute must be explicitly adapted: '+semantic)
                values = document.accessor(accessor).astype(float)
                if semantic in ('NORMAL', 'TANGENT'):
                    width = 3 if semantic == 'NORMAL' else 4
                    require(values.shape == (len(positions), width), 'RIG_ATTRIBUTE',
                            'Direction attributes must have the declared vertex-aligned shape.')
                    # Normals are covectors; tangent XYZ is a surface direction.
                    linear = np.linalg.inv(matrix[:3, :3]) if semantic == 'NORMAL' else matrix[:3, :3].T
                    transformed = values[:, :3] @ linear
                    lengths = np.linalg.norm(transformed, axis=1, keepdims=True)
                    require(np.isfinite(lengths).all() and np.all(lengths > 0), 'RIG_ATTRIBUTE',
                            'Direction attributes must have nonzero finite length.')
                    direction_lengths[semantic] = lengths
                    values[:, :3] = transformed/lengths
                    if semantic == 'TANGENT':values[:, 3] *= np.sign(np.linalg.det(matrix[:3, :3]))
                elif semantic == 'COLOR_0' and values.shape[1] == 3:values = np.c_[values, np.ones(len(values))]
                decoded_attributes[semantic] = values
            morphs = {};target_names = source_mesh.get('extras', {}).get('targetNames', [])
            for i, target in enumerate(primitive.get('targets', [])):
                morph_name = slug(target_names[i]) if i < len(target_names) else f'morph-{i}'
                require(morph_name not in morphs, 'RIG_MORPH_NAME', 'Morph names collide after portable normalization.')
                fields = {}
                for semantic, accessor in target.items():
                    values = document.accessor(accessor).astype(float)
                    require(semantic in ('POSITION', 'NORMAL', 'TANGENT') and values.shape == positions.shape,
                            'RIG_MORPH_TOPOLOGY', 'Morph topology differs from source geometry.')
                    linear = np.linalg.inv(matrix[:3, :3]) if semantic == 'NORMAL' else matrix[:3, :3].T
                    transformed = values @ linear
                    if semantic != 'POSITION':
                        require(semantic in direction_lengths, 'RIG_MORPH_ATTRIBUTE',
                                'Direction morphs require the corresponding base attribute.')
                        # Divide deltas by the SAME base length: normalization of
                        # base + weighted deltas then commutes with this baking.
                        transformed = transformed/direction_lengths[semantic]
                    fields[semantic] = transformed
                fields.setdefault('POSITION', np.zeros_like(positions));morphs[morph_name] = fields
            defaults = node.get('weights', source_mesh.get('weights', [0.]*len(morphs)))
            require(len(defaults) == len(morphs), 'RIG_MORPH_TOPOLOGY', 'Default morph weight count is invalid.')
            if np.linalg.det(matrix[:3, :3]) < 0:triangles = triangles[:, [0, 2, 1]]
            mesh = Mesh(name, transform_points(matrix, positions), triangles,
                        primitive.get('material', default_material), decoded_attributes, morphs,
                        source_mesh.get('name', name), region, node_id, dict(zip(morphs, defaults)))
            geometry.meshes.append(mesh);node_meshes.setdefault(node_id, []).append(name)
            if skin:weights[name] = skin
    validate_geometry(geometry, document.limits)
    if weights:
        from rig_skinning import validate_skin
        for mesh in geometry.meshes:validate_skin(weights[mesh.id], mesh, skeleton, document.limits)
    clips = _animations(document, ids, node_meshes, geometry, parents, scale)
    report = analyze_geometry(geometry, document.limits)
    report.update(import_format=Path(path).suffix.lower(), scale_conversion=scale,
                  preserved_existing_rig=skeleton is not None, preserved_animation_clips=len(clips),
                  source_sha256=sha256(path), warnings=document.warnings)
    extra = document.json.get('asset', {}).get('extras', {})
    report['fixture'] = bool(extra.get('hatchSource', {}).get('fixture', False) or extra.get('hatchRig', {}).get('fixture', False))
    return ImportedModel(geometry, document.dependencies, skeleton, weights, clips, report)


def load_character_model(path: str | Path, *, scale=1., limits=None, blender=None) -> ImportedModel:
    path = Path(path)
    require(path.is_file() and not path.is_symlink(), 'RIG_SOURCE', 'Choose a regular model file.')
    suffix = path.suffix.lower()
    if suffix == '.obj':
        from rig_obj import import_obj
        return import_obj(path, scale=scale, limits=limits)
    if suffix == '.blend':
        from rig_blender import import_blend
        return import_blend(path, scale=scale, limits=limits, executable=blender)
    require(suffix in ('.gltf', '.glb'), 'RIG_FORMAT', 'Supported model inputs are GLB, glTF, OBJ and Blender.')
    return _gltf_model(path, scale=scale, limits=limits)
