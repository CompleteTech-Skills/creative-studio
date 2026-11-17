"""Bounded, offline glTF 2.0 ingestion into the canonical AssetLibrary."""
from copy import deepcopy
import hashlib
from pathlib import Path
import struct
import tempfile

from asset_library import AssetLibrary, detect_format, file_record, read_source_bytes
from asset_library_contracts import MAX_FILE_BYTES, canonical_bytes, seal, validate
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError, decode_json, require, timestamp
from rig_gltf import MATERIAL_EXTENSIONS, preflight_gltf, read_gltf
from rig_import import load_character_model
from rig_interop import validate_gltf
from rig_load_limits import effective_limits
from scene3d_import import _dump, _local_uri, _wireframe_preview, source_snapshot

TOOL = 'hatch-native-gltf-import'
VERSION = '1.0.0'
# These extensions use retained node/material/texture indices and do not
# introduce additional external resources or buffer offsets.
EXTENSIONS = MATERIAL_EXTENSIONS | {'KHR_lights_punctual'}


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


def _normalize(document, images, output, scale, fixture):
    """Pack explicit glTF references, preserving the source scene and indices."""
    value = deepcopy(document.json)
    # glTF permits trailing bytes in URI-backed buffers. The native reader
    # exposes their declared lengths. Exact full files remain in provenance.
    binary = bytearray()
    offsets = []
    for buffer in document.buffers:
        binary.extend(b'\0' * (-len(binary) % 4))
        offsets.append(len(binary))
        binary.extend(buffer)
    for view in value.get('bufferViews', []):
        view['byteOffset'] = offsets[view['buffer']] + view.get('byteOffset', 0)
        view['buffer'] = 0
    for index, data in images.items():
        image = value['images'][index]
        mime = detect_format(data)[1]
        require(mime in ('image/png', 'image/jpeg'), 'ASSET_IMPORT_TEXTURE',
                'Normalization accepts PNG or JPEG texture bytes.')
        binary.extend(b'\0' * (-len(binary) % 4))
        views = value.setdefault('bufferViews', [])
        image['bufferView'] = len(views)
        views.append(dict(buffer=0, byteOffset=len(binary), byteLength=len(data)))
        binary.extend(data)
        image['mimeType'] = mime
        del image['uri']
    if binary:
        value['buffers'] = [dict(byteLength=len(binary))]
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
    encoded = canonical_bytes(value)
    encoded += b' ' * (-len(encoded) % 4)
    binary.extend(b'\0' * (-len(binary) % 4))
    length = 20 + len(encoded) + (8 + len(binary) if binary else 0)
    require(length <= min(MAX_FILE_BYTES, document.limits['source_bytes']),
            'ASSET_IMPORT_LIMIT', 'Normalized GLB exceeds the output byte limit.')
    with output.open('xb') as stream:
        stream.write(struct.pack('<4sIIII', b'glTF', 2, length, len(encoded), 0x4E4F534A))
        stream.write(encoded)
        if binary:
            stream.write(struct.pack('<II', len(binary), 0x004E4942))
            stream.write(binary)


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

    def _snapshot(self, source, root):
        return source_snapshot(source, root,
            maximum_file_bytes=min(MAX_FILE_BYTES, self.limits['source_bytes']),
            maximum_total_bytes=self.limits['dependency_bytes'])

    def probe(self, source):
        """Inspect declarations and report exact closure prior to geometry decoding."""
        with tempfile.TemporaryDirectory(prefix='asset-gltf-probe-') as temporary:
            try:
                snapshot = self._snapshot(source, Path(temporary) / 'source')
                _, _, inventory, embedded, preflight = _inspect(snapshot, self.limits)
                return dict(format=snapshot['path'].suffix[1:], original_filename=snapshot['original_filename'],
                            files=inventory, embedded=embedded, preflight=preflight, geometry_validated=False)
            except RigError:
                raise
            except (TypeError, ValueError, AttributeError, KeyError, IndexError,
                    OverflowError, RecursionError) as exc:
                raise RigError('ASSET_IMPORT_FORMAT', 'Malformed native glTF metadata.',
                               stage='asset-import') from exc

    def import_asset(self, source, *, identity, version, metadata, license_file,
                     unit_scale=1.0, fixture=False):
        """Independently validate source/output and publish one immutable version."""
        require(type(unit_scale) in (int, float) and 1e-3 <= unit_scale <= 1e6,
                'ASSET_IMPORT_SCALE', 'Unit scale must be in [1e-3, 1e6].')
        require(type(fixture) is bool, 'ASSET_IMPORT_REQUEST', 'Fixture classification must be boolean.')
        validate(identity, 'AssetIdentity')
        validate(metadata, 'AssetMetadata')
        validate(dict(id=identity['id'], version=version, sha256='0'*64), 'AssetRef')
        license_bytes = read_source_bytes(license_file, 1024 * 1024)
        with tempfile.TemporaryDirectory(prefix='asset-gltf-import-') as temporary:
            try:
                return self._import(source, Path(temporary), identity, version, metadata,
                                    license_bytes, float(unit_scale), fixture)
            except RigError:
                raise
            except (TypeError, ValueError, AttributeError, KeyError, IndexError,
                    OverflowError, RecursionError) as exc:
                raise RigError('ASSET_IMPORT_FORMAT', 'Malformed native glTF metadata.',
                               stage='asset-import') from exc

    def _import(self, source, root, identity, version, metadata, license_bytes, scale, fixture):
        snapshot = self._snapshot(source, root / 'source')
        document, images, inventory, embedded, preflight = _inspect(snapshot, self.limits)
        source_validation = _checked(snapshot['path'], self.limits)
        declarations = {name: len(document.json.get(name, []))
                        for name in ('skins', 'animations', 'materials', 'textures')}
        extras = document.json['asset'].get('extras', {})
        if isinstance(extras, dict):
            fixture = fixture or any(isinstance(extras.get(key), dict) and
                extras[key].get('fixture') is True for key in ('hatchSource', 'hatchRig'))
        normalized = root / 'normalized.glb'
        _normalize(document, images, normalized, scale, fixture)
        del document, images
        output_validation = _checked(normalized, self.limits)
        output = read_gltf(normalized, self.limits)
        preflight_gltf(output, encoded_bytes=normalized.stat().st_size, limits=self.limits)
        del output
        model = load_character_model(normalized, limits=self.limits)
        preview = _wireframe_preview(model.geometry, root / 'diagnostic.png')
        (root / 'license.txt').write_bytes(license_bytes)
        _dump(root / 'source-khronos.json', source_validation)
        _dump(root / 'normalized-khronos.json', output_validation)
        _dump(root / 'import-report.json', dict(schema_version=1, original_filename=snapshot['original_filename'],
            source_files=inventory, embedded=embedded, preflight=preflight, fixture=fixture,
            operation='pack glTF buffers and textures into GLB and apply explicit scene-root units',
            coordinate_system='glTF right-handed Y-up', source_unit_scale=scale,
            source_sha256=source_validation['asset_sha256'], normalized_sha256=output_validation['asset_sha256'],
            declarations=declarations,
            diagnostic=dict(preview, width=256, height=256, visual_approval=False),
            native_observation=dict(vertices=model.geometry.vertex_count, triangles=model.geometry.triangle_count,
                morph_targets=sum(len(mesh.morphs) for mesh in model.geometry.meshes), clips=sorted(model.clips))))
        return self._publish(root, snapshot, inventory, identity, version, metadata, scale, fixture, model,
                             output_validation['validator'])

    def _publish(self, root, snapshot, inventory, identity, version, metadata, scale, fixture, model, validator):
        original = {f'source/{entry["path"]}': entry for entry in inventory}
        roles = {path: 'source' for path in original}
        roles.update({'normalized.glb': 'payload', 'license.txt': 'license', 'diagnostic.png': 'evidence',
                      'source-khronos.json': 'evidence', 'normalized-khronos.json': 'evidence',
                      'import-report.json': 'evidence'})
        paths = {path: root / path for path in roles}
        records = {path: file_record(paths[path], path, role) for path, role in roles.items()}
        sources = [records[path] for path in original]
        created = timestamp()
        manifest = seal(dict(schema_version=1, kind='asset-library-version', identity=identity,
            version=version, files=list(records.values()), dependencies=[], metadata=metadata, previews=[],
            compatibility=dict(coordinate_system='right-handed-y-up-z-forward', unit_scale=scale,
                rig_signature=[], skeleton_signature=[], material_capabilities=[], texture_channels=[],
                animation_channels=sorted({track['path'] for clip in model.clips.values() for track in clip['tracks']}),
                renderers=[], formats=sorted({'glb', snapshot['path'].suffix[1:]}),
                extensions=model.geometry.extensions_used, consumer_capabilities=[]),
            provenance=dict(method='fixture' if fixture else 'import', origin='Native local glTF import',
                fixture=fixture, sources=sources, provider=[], tools=[dict(name=TOOL, version=VERSION),
                    dict(name='Khronos glTF Validator', version=validator)],
                parents=[], edits=[], license_evidence=[records['license.txt']], permissions=[],
                uncertainty=['Diagnostic preview has no visual review'],
                source_records=[dict(file=records[path], source_uri='local:' + original[path]['path'],
                    original_filename=(snapshot['original_filename'] if 'source' in original[path]['roles']
                                       else original[path]['path']),
                    acquired_at=created, author=metadata['author']) for path in original],
                conversions=[dict(tool=TOOL, version=VERSION, timestamp=created, operation='Pack and normalize GLB',
                    inputs=sources, outputs=[records['normalized.glb'], records['import-report.json']])]),
            publication=dict(visibility='project-local', distribution='restricted', license_evaluation='evidence-retained',
                inventory=list(records), verification='payload-verified', integration='native-reference')))
        reference = self.library.register(manifest, paths)
        self.library.resolve(reference)
        return reference


def resolve_import(library: AssetLibrary, reference):
    """Read retained import evidence through the exact immutable reference."""
    library.resolve(reference)
    return decode_json(library.read_file(reference, 'import-report.json'))
