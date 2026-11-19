"""Bounded offline glTF and static OBJ ingestion into the canonical AssetLibrary."""
from contextlib import contextmanager
from copy import deepcopy
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import struct
import tempfile

import numpy as np

import scene3d_import

from asset_library import AssetLibrary, detect_format, file_record, read_source_bytes
from asset_library_contracts import MAX_FILE_BYTES, seal, validate
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError, decode_json, require, timestamp
from rig_gltf import DTYPES, GltfDocument, MATERIAL_EXTENSIONS, build_static_gltf, preflight_gltf, read_gltf
from rig_import import load_character_model
from rig_interop import validate_gltf
from rig_load_limits import _JSON_FACTOR, effective_limits
from scene3d_import import _dump, _local_uri, _wireframe_preview, source_snapshot

BLENDER_VALIDATOR = '2.0.0-dev.3.10'
TOOL = 'hatch-native-gltf-import'
OBJ_TOOL = 'hatch-native-obj-import'
VERSION = '1.0.0'
# These extensions use retained node/material/texture indices and do not
# introduce additional external resources or buffer offsets.
EXTENSIONS = MATERIAL_EXTENSIONS | {'KHR_lights_punctual'}


@contextmanager
def _private_diagnostics(asset):
    """Report typed failures without echoing private paths or arbitrary input."""
    try:
        yield
    except RigError as exc:
        code = exc.report['code']
        numbers = {key: exc.report[key] for key in ('expected', 'observed')
                   if type(exc.report[key]) is int}
        raise RigError(code, f'Cannot process {asset} ({code}).', stage='asset-import',
                       asset=asset, retryable=exc.report['retryable'], **numbers) from None
    except MemoryError:
        raise RigError('RIG_LIMIT_MEMORY', f'Cannot allocate working memory for {asset}.',
                       stage='asset-import', asset=asset) from None
    except OSError:
        raise RigError('ASSET_IMPORT_IO', f'Cannot access {asset}.',
                       stage='asset-import', asset=asset) from None
    except (TypeError, ValueError, AttributeError, KeyError, IndexError, OverflowError, RecursionError):
        raise RigError('ASSET_IMPORT_FORMAT', f'Malformed metadata in {asset}.',
                       stage='asset-import', asset=asset) from None


def _admit_memory(estimated, limits, stage):
    require(estimated <= limits['memory_bytes'], 'RIG_LIMIT_MEMORY',
            'Combined working set exceeds the native import budget.',
            asset=stage, expected=limits['memory_bytes'], observed=estimated)


def _inspect(snapshot, limits):
    path = snapshot['path']
    document = read_gltf(path, limits)
    value = document.json
    used = set(value.get('extensionsUsed', []))
    require(used <= EXTENSIONS, 'ASSET_IMPORT_EXTENSION',
            'The normalization profile does not cover every declared extension.',
            observed=sorted(used - EXTENSIONS))
    preflight = preflight_gltf(document, encoded_bytes=snapshot['total_bytes'], limits=limits)
    resources = {path.name: {'source'}}
    embedded = []
    images = {}
    image_bytes = 0
    buffer_bytes = sum(map(len, document.buffers))
    for collection, role in (('buffers', 'buffer'), ('images', 'texture')):
        for index, item in enumerate(value.get(collection, [])):
            uri = item.get('uri')
            relative = _local_uri(uri) if uri is not None else None
            if role == 'buffer':
                data = document.buffers[index]
            else:
                if relative is not None:
                    size = snapshot['inventory'][relative]['bytes']
                elif uri is not None:
                    size = len(uri.partition(',')[2]) * 3 // 4
                else:
                    buffer, start, size, _ = document.view(item['bufferView'])
                image_bytes += size
                require(image_bytes <= limits['dependency_bytes'], 'RIG_LIMIT_DEPENDENCIES',
                        'Expanded texture bytes exceed the dependency budget.')
                _admit_memory(preflight['estimated_memory_bytes'] + 2 * (buffer_bytes + image_bytes),
                              limits, 'source resources')
                if uri is not None:
                    data = document.uri(uri)
                    images[index] = data
                else:
                    data = memoryview(buffer)[start:start+size]
            if relative is None:
                embedded.append(dict(role=role, index=index, bytes=len(data),
                                     sha256=hashlib.sha256(data).hexdigest()))
            else:
                resources.setdefault(relative, set()).add(role)
    inventory = [
        dict(path=name, roles=sorted(resources[name]), **entry)
        for name, entry in snapshot['inventory'].items()
    ]
    return document, images, inventory, embedded, preflight


_OBJ_PRECISION_BATCH = 4096
# Covers the gathered source and rounded coordinates and vectorized predicates.
_OBJ_PRECISION_BYTES_PER_TRIANGLE = 768
_OBJ_PRECISION_EXACT_WORKSPACE = 16 * 1024


def _exact_obj_area_components(points):
    corners = [[Fraction(float(coordinate)) for coordinate in point] for point in points]
    left = [b - a for a, b in zip(corners[0], corners[1])]
    right = [c - a for a, c in zip(corners[0], corners[2])]
    return tuple(left[i] * right[j] - left[j] * right[i] for i, j in ((0, 1), (1, 2), (2, 0)))


def _obj_area_estimate(points):
    """Signed XY, YZ, ZX areas and their cancellation scales, in float64."""
    left = points[:, 1] - points[:, 0]
    right = points[:, 2] - points[:, 0]
    first = left * np.roll(right, -1, axis=1)
    second = np.roll(left, -1, axis=1) * right
    precision = np.finfo(np.float64)
    # The floor also covers absolute rounding error in subnormal products.
    scale = np.maximum(np.abs(first) + np.abs(second), precision.tiny / precision.eps)
    return first - second, scale


def _check_obj_float32_triangles(geometry):
    for mesh in geometry.meshes:
        for start in range(0, len(mesh.triangles), _OBJ_PRECISION_BATCH):
            triangles = mesh.triangles[start:start + _OBJ_PRECISION_BATCH]
            source = mesh.positions[triangles]
            with np.errstate(over='ignore', under='ignore'):
                rounded = source.astype(DTYPES[5126]).astype(np.float64)
            require(np.isfinite(rounded).all(), 'RIG_EXPORT_ACCESSOR',
                    'OBJ coordinates must be finite at glTF precision.')
            changed = np.any(rounded != source, axis=(1, 2))
            if not changed.any():
                continue

            # A positive dot product preserves the side of the source face.
            # Each determinant's error is below 8 eps times its cancellation
            # scale. Allow for both determinants and the final dot product.
            precision = np.finfo(np.float64)
            with np.errstate(under='ignore'):
                original, original_scale = _obj_area_estimate(source)
                target, target_scale = _obj_area_estimate(rounded)
                dot = np.sum(original * target, axis=1)
                error = 32 * precision.eps * np.sum(original_scale * target_scale, axis=1) + precision.tiny
            for index in np.flatnonzero(changed & (dot <= error)):
                original_exact = _exact_obj_area_components(source[index])
                if any(original_exact):
                    target_exact = _exact_obj_area_components(rounded[index])
                    require(sum(a * b for a, b in zip(original_exact, target_exact)) > 0,
                            'RIG_OBJ_PRECISION', 'Converting OBJ to glTF precision loses triangle orientation.')


def _inspect_source(snapshot, limits):
    path = snapshot['path']
    if path.suffix != '.obj':
        return _inspect(snapshot, limits)

    from rig_obj import import_obj

    geometry = import_obj(path, limits=limits, geometry_only=True).geometry
    # The supported accessors use 32-bit values. Reserve the retained arrays,
    # builder buffers and scratch, and the synthesized JSON graph up front.
    geometry_bytes = packed_bytes = 0
    metadata_workspace = 4096
    for mesh in geometry.meshes:
        arrays = (mesh.positions, mesh.triangles, *mesh.attributes.values())
        geometry_bytes += sum(array.nbytes for array in arrays)
        packed_bytes += 4 * sum(array.size for array in arrays)
        metadata_workspace += 16384 + 64 * (len(mesh.id) + len(mesh.name))
    output_limit = min(MAX_FILE_BYTES, limits['source_bytes'], limits['dependency_bytes'])
    require(28 + packed_bytes <= output_limit, 'ASSET_IMPORT_LIMIT',
            'Converted OBJ binary cannot fit in the normalized GLB budget.')
    synthesis_workspace = 4 * geometry_bytes + 4 * packed_bytes + metadata_workspace
    _admit_memory(synthesis_workspace, limits, 'OBJ glTF synthesis')
    batch_triangles = min(_OBJ_PRECISION_BATCH, geometry.triangle_count)
    _admit_memory(geometry_bytes + _OBJ_PRECISION_EXACT_WORKSPACE
                  + _OBJ_PRECISION_BYTES_PER_TRIANGLE * batch_triangles,
                  limits, 'OBJ coordinate precision')
    _check_obj_float32_triangles(geometry)
    builder = build_static_gltf(geometry, generate_normals=False)
    del geometry
    document = GltfDocument(path, builder.json, [builder.binary], limits)
    encoder = json.JSONEncoder(ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    json_bytes = sum(len(fragment.encode('utf-8')) for fragment in encoder.iterencode(document.json))
    binary_bytes = len(builder.binary)
    encoded_bytes = 28 + json_bytes + (-json_bytes % 4) + binary_bytes + (-binary_bytes % 4)
    require(encoded_bytes <= output_limit, 'ASSET_IMPORT_LIMIT',
            'Converted OBJ glTF cannot fit in the normalized GLB budget.')
    preflight = preflight_gltf(document, encoded_bytes=encoded_bytes, limits=limits)
    preflight.update(obj_source_bytes=snapshot['total_bytes'],
                     generated_json_bytes=json_bytes, generated_binary_bytes=binary_bytes)
    inventory = [dict(path=path.name, roles=['source'], **snapshot['inventory'][path.name])]
    return document, {}, inventory, [], preflight


def _normalization_plan(document, images, scale, fixture, preflight):
    """Project the GLB layout while retaining the source scene and indices."""
    chunks = [*document.buffers, *images.values()]
    offsets = []
    end = 0
    for chunk in chunks:
        end += -end % 4
        offsets.append(end)
        end += len(chunk)
    packed_bytes = end + (-end % 4)
    limits = document.limits
    output_limit = min(MAX_FILE_BYTES, limits['source_bytes'], limits['dependency_bytes'])
    require(20 + (8 + packed_bytes if packed_bytes else 0) <= output_limit,
            'ASSET_IMPORT_LIMIT', 'Packed binary cannot fit in the normalized GLB budget.')
    # Source estimates include the original parsed JSON and geometry work.
    # Charge a second JSON object and up to three encoded JSON copies for
    # normalization alongside the captured buffer/texture bytes.
    _admit_memory(preflight['estimated_memory_bytes'] + _JSON_FACTOR * preflight['json_bytes']
                  + 2 * packed_bytes + 3 * output_limit, limits, 'GLB normalization')
    value = deepcopy(document.json)
    for view in value.get('bufferViews', []):
        view['byteOffset'] = offsets[view['buffer']] + view.get('byteOffset', 0)
        view['buffer'] = 0
    for (index, data), offset in zip(images.items(), offsets[len(document.buffers):]):
        image = value['images'][index]
        mime = detect_format(data)[1]
        require(mime in ('image/png', 'image/jpeg'), 'ASSET_IMPORT_TEXTURE',
                'Normalization accepts PNG or JPEG texture bytes.')
        views = value.setdefault('bufferViews', [])
        image['bufferView'] = len(views)
        views.append(dict(buffer=0, byteOffset=offset, byteLength=len(data)))
        image['mimeType'] = mime
        del image['uri']
    if packed_bytes:
        value['buffers'] = [dict(byteLength=end)]
    if scale != 1.0:
        nodes = value['nodes']
        wrapped = {}
        matrix = [scale, 0, 0, 0, 0, scale, 0, 0, 0, 0, scale, 0, 0, 0, 0, 1]
        for scene in value['scenes']:
            roots = []
            for index in scene.get('nodes', []):
                if index not in wrapped:
                    wrapped[index] = len(nodes)
                    nodes.append(dict(name='Hatch unit conversion', children=[index], matrix=matrix))
                roots.append(wrapped[index])
            if 'nodes' in scene:
                scene['nodes'] = roots
        # Native bone tails are the one understood world-space extras field.
        for index, node in enumerate(document.json['nodes']):
            extra = node.get('extras', {})
            bone = extra.get('hatchBone', {}) if isinstance(extra, dict) else {}
            if isinstance(bone, dict) and 'tail' in bone:
                value['nodes'][index]['extras']['hatchBone']['tail'] = [
                    value * scale for value in bone['tail']
                ]
    if fixture:
        value['asset'].setdefault('extras', {}).setdefault('hatchSource', {})['fixture'] = True
    container_bytes = 20 + (8 + packed_bytes if packed_bytes else 0)
    json_budget = (output_limit - container_bytes) // 4 * 4
    encoded = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    for fragment in encoder.iterencode(value):
        data = fragment.encode('utf-8')
        require(len(encoded) + len(data) <= json_budget, 'ASSET_IMPORT_LIMIT',
                'Normalized GLB exceeds the effective output byte budget.')
        encoded.extend(data)
    encoded.extend(b' ' * (-len(encoded) % 4))
    length = container_bytes + len(encoded)
    return encoded, chunks, offsets, packed_bytes, length


def _write_glb(output, plan):
    encoded, chunks, offsets, packed_bytes, length = plan
    with output.open('xb') as stream:
        stream.write(struct.pack('<4sIIII', b'glTF', 2, length, len(encoded), 0x4E4F534A))
        stream.write(encoded)
        if packed_bytes:
            stream.write(struct.pack('<II', packed_bytes, 0x004E4942))
            end = 0
            for offset, chunk in zip(offsets, chunks):
                stream.write(b'\0' * (offset - end))
                stream.write(chunk)
                end = offset + len(chunk)
            stream.write(b'\0' * (packed_bytes - end))


def _checked(path, limits):
    report = validate_gltf(path, timeout=min(120, limits['timeout_seconds']))
    require(report.get('ok'), 'ASSET_IMPORT_VALIDATION', 'Khronos rejected the glTF asset.',
            asset=path.name, observed=report['issues'])
    return report


class ImportPipeline:
    """Import an explicit local native source and retain exact evidence."""

    def __init__(self, library: AssetLibrary, *, limits=None):
        self.library = library
        self.limits = effective_limits({**DEFAULT_LIMITS, 'influences': 8}, limits)
        require(self.limits['memory_bytes'] >= 2 * 1024**2, 'RIG_LIMIT_MEMORY',
                'Memory budget is below the native admission reserve.')
        # Bound repeated references as well as captured bytes. The native
        # preflight separately budgets JSON and geometry materialization.
        self.limits['dependency_bytes'] = min(
            self.limits['dependency_bytes'], self.limits['memory_bytes'] // 66)

    def _snapshot(self, source, root, *, retained_bytes=0):
        total_limit = min(scene3d_import.MAX_SOURCE_BYTES, self.limits['dependency_bytes'])
        require(retained_bytes < total_limit, 'RIG_LIMIT_DEPENDENCIES',
                'Captured source leaves no budget for the import.')
        snapshot = source_snapshot(source, root,
            maximum_file_bytes=min(MAX_FILE_BYTES, self.limits['source_bytes']),
            maximum_total_bytes=total_limit - retained_bytes, allow_geometry_obj=True)
        snapshot.pop('document')
        return snapshot

    def probe(self, source):
        """Inspect exact source closure and budget the native representation."""
        with _private_diagnostics('source'):
            with tempfile.TemporaryDirectory(prefix='asset-gltf-probe-') as temporary:
                snapshot = self._snapshot(source, Path(temporary) / 'source')
                _, _, inventory, embedded, preflight = _inspect_source(snapshot, self.limits)
                fmt = snapshot['path'].suffix[1:]
                return dict(format=fmt, original_filename=snapshot['original_filename'],
                            files=inventory, embedded=embedded, preflight=preflight,
                            geometry_validated=fmt == 'obj')

    def import_asset(self, source, *, identity, version, metadata, license_file,
                     unit_scale=1.0, fixture=False):
        """Validate the captured source and GLB, then publish one immutable version."""
        return self._import(source, identity, version, metadata, license_file, unit_scale, fixture,
                            blender_fixture=False)

    def import_blender_fixture(self, *, identity, version, metadata, license_file):
        """Create and reopen a packed triangle with the pinned Linux Blender worker."""
        return self._import(None, identity, version, metadata, license_file, 1.0, True,
                            blender_fixture=True)

    def _import(self, source, identity, version, metadata, license_file, unit_scale, fixture,
                *, blender_fixture):
        require(type(unit_scale) in (int, float) and 1e-3 <= unit_scale <= 1e6,
                'ASSET_IMPORT_SCALE', 'Unit scale must be in [1e-3, 1e6].')
        require(type(fixture) is bool, 'ASSET_IMPORT_REQUEST', 'Fixture classification must be boolean.')
        validate(identity, 'AssetIdentity')
        validate(metadata, 'AssetMetadata')
        validate(dict(id=identity['id'], version=version, sha256='0'*64), 'AssetRef')
        with _private_diagnostics('license evidence'):
            license_bytes = read_source_bytes(license_file, 1024 * 1024)
        with _private_diagnostics('staged import'):
            temporary = tempfile.TemporaryDirectory(prefix='asset-gltf-import-')
            try:
                manifest, paths = self._stage(source, Path(temporary.name), identity, version,
                                              metadata, license_bytes, float(unit_scale), fixture,
                                              blender_fixture=blender_fixture)
                return self.library.register(manifest, paths)
            finally:
                try:
                    temporary.cleanup()
                except OSError:
                    # Cleanup must preserve the original failure or the
                    # exact reference returned by the canonical store.
                    pass

    def _stage(self, source, root, identity, version, metadata, license_bytes, scale, fixture,
               *, blender_fixture=False):
        blender = None
        if blender_fixture:
            from asset_import_blender import create_fixture
            fixture_limits = {**self.limits,
                'dependency_bytes': min(scene3d_import.MAX_SOURCE_BYTES, self.limits['dependency_bytes'])}
            original, source, blender = create_fixture(root / 'blender', fixture_limits)
        retained_bytes = blender['source_bytes'] if blender is not None else 0
        snapshot = self._snapshot(source, root / 'source', retained_bytes=retained_bytes)
        is_obj = snapshot['path'].suffix == '.obj'
        document, images, inventory, embedded, preflight = _inspect_source(snapshot, self.limits)
        declarations = {name: len(document.json.get(name, []))
                        for name in ('skins', 'animations', 'materials', 'textures')}
        extras = document.json['asset'].get('extras', {})
        if isinstance(extras, dict):
            fixture = fixture or any(isinstance(extras.get(key), dict) and
                extras[key].get('fixture') is True for key in ('hatchSource', 'hatchRig'))
        normalized = root / 'normalized.glb'
        plan = _normalization_plan(document, images, scale, fixture, preflight)
        del document, images, extras
        _write_glb(normalized, plan)
        del plan
        output_size = normalized.stat().st_size
        output = read_gltf(normalized, self.limits)
        output_preflight = preflight_gltf(output, encoded_bytes=output_size, limits=self.limits)
        # Reserve geometry work, extra encoded copies and the wireframe
        # working set before allowing the decoder its texture allocation.
        encoded_images = sum(output.view(image['bufferView'])[2] for image in output.json.get('images', []))
        decode_reserved = (output_preflight['estimated_memory_bytes'] + 4 * output_size
                           + encoded_images + 120 * output_preflight['vertices']
                           + 256 * 256 * 12 + len(license_bytes))
        _admit_memory(decode_reserved + 1, self.limits, 'native decode')
        decode_limits = {**self.limits, 'memory_bytes': self.limits['memory_bytes'] - decode_reserved}
        del output
        source_validation = None if is_obj else _checked(snapshot['path'], self.limits)
        output_validation = _checked(normalized, self.limits)
        model = load_character_model(normalized, limits=decode_limits)
        if blender is not None:
            self._admit_blender_result(root, original, blender, inventory, model,
                                       source_validation, output_validation)
        preview = _wireframe_preview(model.geometry, root / 'diagnostic.png')
        (root / 'license.txt').write_bytes(license_bytes)
        if source_validation is not None:
            _dump(root / 'source-khronos.json', source_validation)
        _dump(root / 'normalized-khronos.json', output_validation)
        _dump(root / 'import-report.json', dict(schema_version=1, original_filename=snapshot['original_filename'],
            source_files=inventory, embedded=embedded, preflight=preflight, fixture=fixture,
            **({'blender': blender} if blender is not None else {}),
            operation=('convert static OBJ mesh geometry into GLB and apply explicit scene-root units'
                       if is_obj else 'pack glTF buffers and textures into GLB and apply explicit scene-root units'),
            **({'source_geometry_reader': 'hatch-native-obj', 'source_validation': 'native-geometry'}
               if is_obj else {}),
            coordinate_system='glTF right-handed Y-up', source_unit_scale=scale,
            source_sha256=inventory[0]['sha256'] if is_obj else source_validation['asset_sha256'],
            normalized_sha256=output_validation['asset_sha256'],
            declarations=declarations, native_decode_reserved_bytes=decode_reserved,
            unit_scale_scope=('scene-root transforms' if is_obj
                              else 'scene-root transforms and native world-space bone tails'),
            unchanged_metadata_lengths=[] if is_obj else ['camera clipping planes',
                'orthographic camera magnitudes', 'punctual light range'],
            diagnostic=dict(preview, width=256, height=256, visual_approval=False,
                            geometry='base-mesh-before-morphs-and-skinning'),
            native_observation=dict(vertices=model.geometry.vertex_count, triangles=model.geometry.triangle_count,
                morph_targets=sum(len(mesh.morphs) for mesh in model.geometry.meshes), clips=sorted(model.clips))))
        return self._manifest(root, snapshot, inventory, identity, version, metadata, scale, fixture, model,
                             output_validation['validator'], blender=blender)

    def _admit_blender_result(self, root, original, blender, inventory, model,
                              source_validation, output_validation):
        require(len(inventory) == 1, 'ASSET_BLENDER_OUTPUT', 'Fixture GLB must be self-contained.')
        require(source_validation['validator'] == output_validation['validator'] == BLENDER_VALIDATOR,
                'ASSET_BLENDER_VALIDATOR', 'The fixture requires the pinned Khronos validator.')
        meshes = model.geometry.meshes
        require(len(meshes) == 1 and meshes[0].triangles.shape == (1, 3)
                and len(meshes[0].positions) == 3,
                'ASSET_BLENDER_OUTPUT', 'The native GLB has a different triangle topology.')
        vertices = [tuple(meshes[0].positions[index]) for index in meshes[0].triangles[0]]
        expected = [(0, 0, 0), (1, 0, 0), (0, 1, -2)]
        require(any(vertices == expected[index:] + expected[:index] for index in range(3)),
                'ASSET_BLENDER_OUTPUT', 'The native GLB changed triangle coordinates or winding.')
        limit = min(MAX_FILE_BYTES, self.limits['source_bytes'], self.limits['dependency_bytes'])
        original_bytes = read_source_bytes(original, limit)
        require(hashlib.sha256(original_bytes).hexdigest() == blender['source_sha256'],
                'ASSET_BLENDER_SOURCE', 'The packed original changed before publication.')
        (root / 'source' / 'fixture.blend').write_bytes(original_bytes)
        blender.update(original_filename=original.name, original_path='source/fixture.blend',
                       derivative_path='source/model.glb',
                       derivative_sha256=source_validation['asset_sha256'])

    def _manifest(self, root, snapshot, inventory, identity, version, metadata, scale, fixture, model, validator,
                  *, blender=None):
        original = {f'source/{entry["path"]}': entry for entry in inventory}
        roles = {path: 'source' for path in original}
        is_obj = snapshot['path'].suffix == '.obj'
        roles.update({'normalized.glb': 'payload', 'license.txt': 'license', 'diagnostic.png': 'evidence'})
        if not is_obj:
            roles['source-khronos.json'] = 'evidence'
        roles.update({'normalized-khronos.json': 'evidence', 'import-report.json': 'evidence'})
        if blender is not None:
            roles.update({'source/fixture.blend': 'source', 'source/model.glb': 'evidence'})
        paths = {path: root / path for path in roles}
        records = {path: file_record(paths[path], path, role) for path, role in roles.items()}
        sources = [records[path] for path in original]
        created = timestamp()
        origin = 'Native local static OBJ import' if is_obj else 'Native local glTF import'
        importer = OBJ_TOOL if is_obj else TOOL
        tools = [dict(name=importer, version=VERSION), dict(name='Khronos glTF Validator', version=validator)]
        source_records = [dict(file=records[path], source_uri='local:' + original[path]['path'],
                    original_filename=(snapshot['original_filename'] if 'source' in original[path]['roles']
                                       else original[path]['path']),
                    acquired_at=created, author=metadata['author']) for path in original]
        operation = 'Convert static OBJ to GLB' if is_obj else 'Pack and normalize GLB'
        conversions = [dict(tool=importer, version=VERSION, timestamp=created, operation=operation,
                            inputs=sources, outputs=[records['normalized.glb'], records['import-report.json']])]
        if blender is not None:
            origin = 'App-authored packed Blender triangle fixture'
            tool = dict(name='Blender', version=blender['inspection']['version'])
            tools.append(tool)
            sources = [records['source/fixture.blend']]
            source_records = [dict(file=sources[0], source_uri='local:app-authored-fixture',
                                   original_filename='fixture.blend', acquired_at=created,
                                   author=metadata['author'])]
            conversions.insert(0, dict(tool=tool['name'], version=tool['version'], timestamp=created,
                operation='Reopen packed triangle and export GLB', inputs=sources,
                outputs=[records['source/model.glb']]))
        manifest = seal(dict(schema_version=1, kind='asset-library-version', identity=identity,
            version=version, files=list(records.values()), dependencies=[], metadata=metadata, previews=[],
            compatibility=dict(coordinate_system='right-handed-y-up-z-forward', unit_scale=scale,
                rig_signature=[], skeleton_signature=[], material_capabilities=[], texture_channels=[],
                animation_channels=sorted({track['path'] for clip in model.clips.values() for track in clip['tracks']}),
                renderers=[], formats=sorted({'glb', snapshot['path'].suffix[1:]}),
                extensions=model.geometry.extensions_used, consumer_capabilities=[]),
            provenance=dict(method='fixture' if fixture else 'import', origin=origin,
                fixture=fixture, sources=sources, provider=[], tools=tools,
                parents=[], edits=[], license_evidence=[records['license.txt']], permissions=[],
                uncertainty=['Diagnostic preview has no visual review'],
                source_records=source_records, conversions=conversions),
            publication=dict(visibility='project-local', distribution='restricted', license_evaluation='evidence-retained',
                inventory=list(records), verification='payload-verified', integration='native-reference')))
        return manifest, paths


def resolve_import(library: AssetLibrary, reference):
    """Read retained import evidence through the exact immutable reference."""
    library.resolve(reference)
    return decode_json(library.read_file(reference, 'import-report.json'))
