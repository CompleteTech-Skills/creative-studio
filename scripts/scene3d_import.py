"""Real GLB/glTF scene import through native rig and immutable library owners.

Original source files and semantic node paths are retained. Scene instances select
native mesh components rather than duplicating geometry or inventing skeletons.
The optional CPU preview uses the native evaluator for skin/morph animation.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path
import shutil
import struct
import tempfile
from urllib.parse import unquote, urlsplit

import numpy as np
from PIL import Image

from asset_library import AssetLibrary, file_record, read_source_bytes
from asset_library_contracts import MAX_FILE_BYTES, portable_path as library_path, seal
from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError, decode_json
from rig_errors import timestamp
from rig_gltf import MATERIAL_EXTENSIONS, export_geometry, export_gltf, node_matrix, read_gltf, scene_graph
from rig_jobs import create_run, save_rig
from scene3d_contracts import COORDINATES, all_refs, canonical_bytes, portable_path, validate_document
from scene3d_example import node as make_node
from scene3d_graph import SceneGraph, _inventories, _inverse, fail, require, transform_for_matrix
from scene3d_prefabs import _refs, scoped_id
from scene3d_snapshot import _new_destination
from atomic_publish import _rename_no_replace


MAX_SOURCE_FILES = 128
MAX_SOURCE_BYTES = 128*1024*1024


def _json(payload, suffix):
    if suffix == '.gltf':
        return decode_json(payload)
    require(len(payload) >= 20, 'Truncated GLB header.', 'SCENE3D_IMPORT_FORMAT')
    magic, version, length = struct.unpack_from('<4sII', payload)
    size, kind = struct.unpack_from('<II', payload, 12)
    require(magic == b'glTF' and version == 2 and length == len(payload) and
            kind == 0x4E4F534A and size <= len(payload)-20,
            'Invalid GLB JSON/header.', 'SCENE3D_IMPORT_FORMAT')
    return decode_json(payload[20:20+size].rstrip(b' \x00'))


def _local_uri(uri):
    require(isinstance(uri, str), 'Resource URI must be text.', 'SCENE3D_IMPORT_URI')
    if uri.startswith('data:'):
        return None
    try:
        parsed = urlsplit(uri)
    except ValueError as exc:
        raise RigError('SCENE3D_IMPORT_URI', 'Malformed local resource URI.',
                       stage='scene3d-import', asset=uri) from exc
    require(not parsed.scheme and not parsed.netloc and not parsed.query and not parsed.fragment,
            'Only explicit local glTF resources are supported.', 'SCENE3D_IMPORT_URI')
    try:
        path = unquote(parsed.path, errors='strict')
        portable_path(path)
        library_path(path)
    except (UnicodeError, RigError) as exc:
        raise RigError('SCENE3D_IMPORT_URI', 'The resource does not fit the portable ASCII library path profile.',
                       stage='scene3d-import', asset=uri) from exc
    return path


def source_snapshot(source, destination):
    """Safely read explicit source dependencies once into an owned private tree."""
    source = Path(source).absolute()
    require(source.suffix.lower() in ('.glb', '.gltf'), 'Import accepts GLB or glTF.', 'SCENE3D_IMPORT_FORMAT')
    payload = read_source_bytes(source, MAX_FILE_BYTES)
    document = _json(payload, source.suffix.lower())
    require(isinstance(document, dict), 'glTF root must be an object.', 'SCENE3D_IMPORT_FORMAT')
    primary = 'model'+source.suffix.lower()
    resources = {}
    for collection in ('buffers', 'images'):
        entries = document.get(collection, [])
        require(isinstance(entries, list) and len(entries) <= MAX_SOURCE_FILES,
                'Resource inventory exceeds its bound.', 'SCENE3D_IMPORT_LIMIT')
        for entry in entries:
            require(isinstance(entry, dict), 'Resource entry must be an object.', 'SCENE3D_IMPORT_FORMAT')
            if 'uri' in entry:
                path = _local_uri(entry['uri'])
                if path is not None:
                    resources[path] = source.parent/path
    require(primary not in resources, 'An external resource collides with the private primary filename.',
            'SCENE3D_IMPORT_URI')
    require(len(resources)+1 <= MAX_SOURCE_FILES, 'Too many external resources.', 'SCENE3D_IMPORT_LIMIT')
    names = [primary, *resources]
    aliases = set()
    for name in names:
        parts = name.casefold().split('/')
        require(name.casefold() not in aliases, 'Case-alias resource paths.', 'SCENE3D_IMPORT_URI')
        aliases.add(name.casefold())
        for index in range(1, len(parts)):
            require('/'.join(parts[:index]) not in {n.casefold() for n in names},
                    'File/directory resource alias.', 'SCENE3D_IMPORT_URI')
    files, total = {primary: payload}, len(payload)
    require(total <= MAX_SOURCE_BYTES, 'Aggregate source byte budget exceeded.', 'SCENE3D_IMPORT_LIMIT')
    for path, target in sorted(resources.items()):
        data = read_source_bytes(target, min(MAX_FILE_BYTES, MAX_SOURCE_BYTES-total))
        total += len(data)
        require(total <= MAX_SOURCE_BYTES, 'Aggregate source byte budget exceeded.', 'SCENE3D_IMPORT_LIMIT')
        files[path] = data
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    for path, data in files.items():
        target = destination/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return dict(path=destination/primary, document=document, original_filename=source.name,
                inventory={p: dict(bytes=len(v), sha256=hashlib.sha256(v).hexdigest()) for p, v in files.items()},
                total_bytes=total)


def _dump(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)+'\n').encode('utf-8'))


def _preview(rig, destination):
    """Generate an inspectable geometry-only PNG; no visual approval is inferred."""
    if rig.manifest.get('skeleton') and set(rig.weights) == set(rig.geometry.by_id()):
        from rig_render import render_rig_preview
        from rig_skinning import evaluate_deformation
        original = np.concatenate([m.positions for m in rig.geometry.meshes])
        deformed = np.concatenate([m['positions'] for m in evaluate_deformation(rig).values()])
        lower, upper = np.minimum(original.min(axis=0), deformed.min(axis=0)), np.maximum(original.max(axis=0), deformed.max(axis=0))
        bounds = [lower.tolist(), upper.tolist()]
        render_rig_preview(rig, destination, width=256, height=256, bounds=bounds)
        return dict(backend='hatch-native-cpu-triangle-zbuffer', textures='base-color-only',
                    animation='native-rest-pose', depth_test=True, bounds=bounds)
    # Static imports have no fabricated source skeleton. A wireframe projection
    # of genuine triangles is sufficient for their explicit geometry inspector.
    from PIL import Image, ImageDraw
    points = np.concatenate([mesh.positions for mesh in rig.geometry.meshes])
    center = (points.min(axis=0)+points.max(axis=0))*.5
    forward = np.array([2.3, 1.1, 3.8]); forward /= np.linalg.norm(forward)
    right = np.cross([0, 1, 0], forward); right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    basis = np.column_stack([right, up, forward])
    view = (points-center)@basis
    scale = 200/max(float(np.ptp(view[:, :2], axis=0).max()), 1e-8)
    image = Image.new('RGB', (256, 256), (20, 28, 40))
    draw = ImageDraw.Draw(image)
    for mesh in rig.geometry.meshes:
        p = (mesh.positions-center)@basis
        p = np.column_stack([128+p[:, 0]*scale, 128-p[:, 1]*scale])
        for tri in mesh.triangles:
            draw.line([tuple(p[tri[0]]), tuple(p[tri[1]]), tuple(p[tri[2]]), tuple(p[tri[0]])],
                      fill=(190, 218, 238), width=1)
    image.save(destination)
    return dict(backend='hatch-scene-wireframe-inspector', textures='not-shaded',
                animation='none', depth_test=False,
                bounds=[points.min(axis=0).tolist(), points.max(axis=0).tolist()])


def _hierarchy(document, rig, unit_scale):
    parents, world, order = scene_graph(document)
    nodes, records = [], []
    source = document.json['nodes']
    component_ids = {}
    cameras = document.json.get('cameras', [])
    lights = document.json.get('extensions', {}).get('KHR_lights_punctual', {}).get('lights', [])
    warnings = []
    for index in order:
        raw = source[index]
        identifier = 'node-'+str(index)
        transform = node_matrix(raw)
        if index not in parents:
            transform = np.diag([unit_scale]*3+[1.])@transform
        n = make_node(identifier, 'group', 'node-'+str(parents[index]) if index in parents else None)
        n['name'] = raw.get('name', identifier)
        n['transform'] = transform_for_matrix(transform)
        meshes = [m for m in rig.geometry.meshes if m.source_node == index]
        if meshes:
            n['type'] = 'skinned_character' if 'skin' in raw else 'animated_prop' if rig.clips else 'mesh_instance'
            component_ids[identifier] = [m.id for m in meshes]
            actual_world = np.diag([unit_scale]*3+[1.])@world[index]
            local = np.concatenate([m.positions for m in meshes])
            inverse = _inverse(actual_world)
            local = local@inverse[:3, :3].T+inverse[:3, 3]
            n['bounds'] = dict(minimum=local.min(axis=0).tolist(), maximum=local.max(axis=0).tolist())
        elif 'camera' in raw:
            camera_index = raw['camera']
            require(type(camera_index) is int and 0 <= camera_index < len(cameras), 'Invalid camera reference.')
            camera = cameras[camera_index]
            n['type'] = 'camera'
            if camera.get('type') == 'perspective':
                config = camera['perspective']
                n['camera'] = dict(projection='perspective', near=config['znear']*unit_scale,
                                   far=config.get('zfar', 1e5)*unit_scale,
                                   vertical_fov=config['yfov'], orthographic_height=1.)
                if 'zfar' not in config:
                    warnings.append(dict(node=index, feature='infinite-far-plane',
                                         disposition='finite-authoring-proxy-100000-source-units; original-camera-retained'))
            else:
                require(camera.get('type') == 'orthographic', 'Unsupported camera type.')
                config = camera['orthographic']
                n['camera'] = dict(projection='orthographic', near=config['znear']*unit_scale,
                                   far=config['zfar']*unit_scale, vertical_fov=.8,
                                   orthographic_height=2*config['ymag']*unit_scale)
        elif 'KHR_lights_punctual' in raw.get('extensions', {}):
            light_index = raw['extensions']['KHR_lights_punctual']['light']
            require(type(light_index) is int and 0 <= light_index < len(lights), 'Invalid light reference.')
            light = lights[light_index]
            require(light.get('type') in ('point', 'spot', 'directional'), 'Unsupported glTF light type.')
            n['type'] = 'light'
            n['light'] = dict(type=light['type'], color=light.get('color', [1, 1, 1]), intensity=light.get('intensity', 1.))
            if light.get('spot') or 'range' in light:
                warnings.append(dict(node=index, feature='light-cone/range',
                                     disposition='retained-in-source-hierarchy; not-in-basic-scene-light-proxy'))
        if 'camera' in raw and n['type'] != 'camera':
            warnings.append(dict(node=index, feature='combined-node-camera',
                disposition='retained-in-source-hierarchy; omitted-from-single-role-scene-node'))
        if 'KHR_lights_punctual' in raw.get('extensions', {}) and n['type'] != 'light':
            warnings.append(dict(node=index, feature='combined-node-light',
                disposition='retained-in-source-hierarchy; omitted-from-single-role-scene-node'))
        n['metadata'] = dict(source_node=index)
        nodes.append(n)
        records.append(dict(source_node=index, semantic_path='/'.join(
            source[i].get('name', 'node-'+str(i)) for i in _ancestry(parents, index)),
            parent=parents.get(index), name=raw.get('name', ''), mesh=raw.get('mesh'), skin=raw.get('skin'),
            camera=raw.get('camera'), light=raw.get('extensions', {}).get('KHR_lights_punctual', {}).get('light'),
            local_matrix=node_matrix(raw).tolist(),
            world_matrix=(np.diag([unit_scale]*3+[1.])@world[index]).tolist(),
            mesh_components=component_ids.get(identifier, []), extras=deepcopy(raw.get('extras', {}))))
    return nodes, records, component_ids, warnings


def _ancestry(parents, index):
    output = [index]
    while index in parents:
        index = parents[index]
        output.append(index)
    return list(reversed(output))


@dataclass
class ImportedScene:
    library_reference: dict
    asset_reference: dict
    asset: dict
    nodes: list
    hierarchy: list
    report: dict


def import_gltf(source, library: AssetLibrary, *, asset_id, version='1.0.0',
                attribution, license_file, unit_scale=1., fixture=False, limits=None,
                prior_reference=None, derivation=None, clip_aliases=None,
                license_summary='Evidence retained; distribution restricted pending rights evaluation'):
    """Decode, independently check an export, then transactionally register bytes.

    Supported native format loss is explicit in the returned report. Original
    scene hierarchy, skeleton, animation and material bytes are always retained;
    unsupported required extensions or unrepresentable transforms fail closed.
    """
    require(type(unit_scale) in (int, float) and math.isfinite(unit_scale) and 1e-6 <= unit_scale <= 1e6,
            'Source unit conversion must be finite and bounded.')
    require(type(fixture) is bool and isinstance(attribution, str) and attribution,
            'Fixture classification and attribution are explicit.')
    require(isinstance(license_summary, str) and 0 < len(license_summary) <= 256,
            'License summary must be bounded text; full evidence is retained separately.')
    from asset_library_contracts import validate
    # Validate caller identity/version before deriving any destination paths.
    validate(dict(id=asset_id, version=version, sha256='0'*64), 'AssetRef')
    prior = None if prior_reference is None else library.resolve(prior_reference)
    require(derivation is None or prior is not None and isinstance(derivation, str) and 0 < len(derivation) <= 4096,
            'Derived geometry needs a real exact parent version and bounded operation description.')
    if prior is not None:
        require(prior['identity']['id'] == asset_id and prior['version'] != version,
                'A prior reference must name another exact version of the same asset.')
        fixture = fixture or prior['provenance']['fixture']
    prefix = 'imports/'+asset_id+'/'+version
    selected_limits = {**DEFAULT_LIMITS, 'influences': 8, **(limits or {})}
    with tempfile.TemporaryDirectory(prefix='scene3d-import-') as temporary:
        private = Path(temporary).resolve()
        source_info = source_snapshot(source, private/'source')
        from rig_interop import validate_gltf
        source_validation = validate_gltf(source_info['path'])
        require(source_validation['ok'], 'Independent Khronos validation rejected original source declarations.',
                'SCENE3D_IMPORT_VALIDATION')
        package = private/'package'
        base = package/prefix
        base.mkdir(parents=True)
        try:
            native_id = asset_id+'-v'+version.replace('.', '-')
            rig = create_run(source_info['path'], base/'native', identifier=native_id, scale=unit_scale,
                             limits=selected_limits, fixture=fixture, attribution=attribution)
            clip_map, clips = {}, {}
            for original_id, original_clip in rig.clips.items():
                identifier = 'a-'+hashlib.sha256((native_id+'\0'+original_id).encode()).hexdigest()[:48]
                clip = deepcopy(original_clip)
                clip['id'] = identifier
                clip['provenance']['operation'] = 'Native scene import; original clip '+original_id
                clip_map[original_id] = identifier
                clips[identifier] = clip
            if clips:
                rig.clips = clips
                save_rig(rig, bump=False, operation='scope-imported-clip-identities')
            for alias, native in (clip_aliases or {}).items():
                require(isinstance(alias, str) and native in clip_map, 'An original clip alias has no retained native clip.')
                clip_map[alias] = clip_map[native]
            document = read_gltf(source_info['path'], selected_limits)
            nodes, hierarchy, components, losses = _hierarchy(document, rig, unit_scale)
            mesh_uses = [n['mesh'] for n in document.json['nodes'] if 'mesh' in n]
            if len(mesh_uses) > len(set(mesh_uses)):
                losses.append(dict(feature='source-mesh-instancing',
                    disposition='normalized-native-geometry-materializes-instances; original-indexes-and-source-bytes-retained; scene-components-share-one-asset-version'))
            for extension in sorted(set(document.json.get('extensionsUsed', []))-MATERIAL_EXTENSIONS):
                losses.append(dict(feature='optional-extension:'+extension,
                    disposition='retained-in-original-source; not-executed-by-native-geometry-preview'))
            normalized = base/'normalized.glb'
            export_report = export_gltf(rig, normalized, bake=False) if rig.manifest.get('skeleton') else export_geometry(
                rig.geometry, normalized, provenance=dict(fixture=rig.manifest['source']['fixture'], operation='import-normalization'))
            from rig_interop import validate_gltf
            validation = validate_gltf(normalized)
            require(validation['ok'], 'Independent Khronos validation rejected normalized geometry.',
                    'SCENE3D_IMPORT_VALIDATION')
            _dump(base/'khronos.json', validation)
            _dump(base/'source-khronos.json', source_validation)
            _dump(base/'hierarchy.json', dict(nodes=hierarchy, components=components,
                    original_cameras=document.json.get('cameras', []),
                    original_lights=document.json.get('extensions', {}).get('KHR_lights_punctual', {}),
                    source_inventory=source_info['inventory']))
            preview = _preview(rig, base/'preview.png')
        except RigError:
            raise
        except (OSError, ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError,
                Image.DecompressionBombError) as exc:
            raise RigError('SCENE3D_IMPORT_INVALID', 'Native import rejected the source: '+str(exc),
                           stage='scene3d-import') from exc
        license_payload = read_source_bytes(license_file, 1024*1024)
        (base/'license.txt').write_bytes(license_payload)
        _dump(base/'source-identity.json', dict(original_filename=source_info['original_filename'],
                    original_sha256=source_info['inventory'][source_info['path'].name]['sha256'],
                    snapshot_primary=source_info['path'].name, unit_scale=unit_scale,
                    attribution=attribution, fixture=rig.manifest['source']['fixture']))
        _dump(base/'preview-settings.json', dict(width=256, height=256, **preview))
        _dump(base/'preview-camera.json', dict(view='three-quarter', coordinate_system=COORDINATES,
                                              bounds=preview['bounds']))
        _dump(base/'preview-lighting.json', dict(mode=preview['backend'], visual_approval=False))
        def file(path):
            return dict(path=prefix+'/'+path, sha256=hashlib.sha256((base/path).read_bytes()).hexdigest())
        native_manifest = rig.manifest
        rig_ref = dict(id=native_manifest['id'], version=native_manifest['version'], kind='hatch-3d-rig', **file('native/rig.json'))
        animations = [dict(id=c['id'], version=c['version'], kind='hatch-3d-animation',
                           **file('native/'+c['path'])) for c in native_manifest['animations']]
        points = np.concatenate([m.positions for m in rig.geometry.meshes])
        provenance = dict(origin='procedural-fixture' if rig.manifest['source']['fixture'] else 'import',
            fixture=rig.manifest['source']['fixture'], tool='scene3d-native-import-v1',
            inputs=[file('native/source/'+source_info['path'].name)], attribution=attribution,
            license=license_summary)
        asset = dict(schema_version=1, kind='scene3d-asset', id=asset_id, version=version, name=Path(source).stem,
            asset_type='character' if document.json.get('skins') else 'mesh', content=file('normalized.glb'),
            source=file('native/source/'+source_info['path'].name), provenance=provenance,
            coordinate_system=COORDINATES, original_coordinate_system='glTF 2.0 right-handed Y-up meters',
            pivot=[0, 0, 0], meshes=[file('normalized.glb')], materials=[],
            bounds=dict(minimum=points.min(axis=0).tolist(), maximum=points.max(axis=0).tolist()),
            collision=None, sockets=[], components=components, skeleton=rig_ref if rig.manifest.get('skeleton') else None,
            animations=animations, lods=[], dependencies=[],
            resources=dict(vertices=rig.geometry.vertex_count, triangles=rig.geometry.triangle_count, bytes=normalized.stat().st_size),
            validation=[file('khronos.json')], review=dict(status='unreviewed', receipts=[]))
        report = dict(schema_version=1, fixture=provenance['fixture'], source_files=source_info['inventory'],
            original_filename=source_info['original_filename'], source_sha256=rig.manifest['source']['sha256'],
            native_clip_map=clip_map,
            unit_scale=unit_scale, geometry=dict(vertices=rig.geometry.vertex_count, triangles=rig.geometry.triangle_count,
                meshes=len(rig.geometry.meshes), skins=len(rig.weights), morph_targets=sum(len(m.morphs) for m in rig.geometry.meshes),
                clips=len(rig.clips)), losses=losses, native_warnings=rig.manifest.get('uncertainties', []),
            normalized_export=export_report, independent_validator=dict(ok=True, errors=validation['issues']['numErrors'],
                warnings=validation['issues']['numWarnings']), hierarchy_preserved=True,
            original_bytes_preserved=True, skin_animation_owner='native-rig', scene_motion_evaluator=False,
            material_preview=preview, native_clip_rates={name: clip['fps'] for name, clip in rig.clips.items()},
            source_animation_time_unit='seconds; native fps is sampling metadata, not inferred source cadence',
            visual_approval=False, production_approved=False)
        if derivation is not None:
            report['derivation'] = dict(parent=deepcopy(prior_reference), operation=derivation,
                                        original_source_preserved_in_parent=True)
            report['losses'].append(dict(feature='source-authoring-hierarchy',
                disposition='explicit-geometry-edit-uses-normalized-native-hierarchy; exact-original-is-retained-in-parent-version'))
        _dump(base/'import-report.json', report)
        # Every native dependency is exposed to the existing Scene3D closure
        # loader, even when it lives behind a native rig JSON descriptor.
        for target in sorted(base.rglob('*')):
            if target.is_file() and target != base/'.native.rig.lock':
                relative = target.relative_to(package).as_posix()
                asset['dependencies'].append(dict(id='file-'+hashlib.sha256(relative.encode()).hexdigest()[:48],
                    version='1.0.0', kind='evidence', path=relative, sha256=hashlib.sha256(target.read_bytes()).hexdigest()))
        validate_document(asset, 'asset')
        asset_payload = canonical_bytes(asset)
        (base/'asset.json').write_bytes(asset_payload)
        asset_reference = dict(id=asset_id, version=version, kind='scene3d-asset', path=prefix+'/asset.json',
                               sha256=hashlib.sha256(asset_payload).hexdigest())
        for node in nodes:
            if node['id'] in components:
                node['asset'] = deepcopy(asset_reference)
                node['metadata']['source_component'] = node['id']
        # This inventory is a library-facing authoring adapter, not an input to
        # the asset hash itself (which would create a content-hash cycle).
        _dump(base/'authoring-nodes.json', dict(schema_version=1, kind='scene3d-import-authoring-nodes',
                                               asset=asset_reference, nodes=nodes))
        records, sources = [], {}
        for target in sorted(base.rglob('*')):
            if target.is_file() and target != base/'.native.rig.lock':
                relative = target.relative_to(package).as_posix()
                role = 'license' if target.name == 'license.txt' else 'preview' if target.name == 'preview.png' else 'payload'
                record = file_record(target, relative, role)
                records.append(record)
                sources[relative] = target
        require(len(records) <= 256, 'Native package exceeds library file-count profile.', 'SCENE3D_IMPORT_LIMIT')
        by_path = {r['path']: r for r in records}
        def record(path): return by_path[prefix+'/'+path]
        created = timestamp()
        character = bool(document.json.get('skins'))
        library_manifest = seal(dict(schema_version=1, kind='asset-library-version',
            identity=dict(id=asset_id, namespace='scene3d', library='scene-imports', slug=asset_id,
                name=Path(source).stem, kind='character' if character else 'prop', subtype='gltf-scene',
                created_at=prior['identity']['created_at'] if prior is not None else created,
                attribution=attribution, owner=attribution), version=version,
            files=records, dependencies=[], metadata=dict(description='Exact native 3D scene import', category='geometry',
                author=attribution, publisher=attribution, source_url='local:'+Path(source).name,
                license=provenance['license'], allowed_uses=[], keywords=[], style_refs=[], capabilities=[], review_status='unreviewed'),
            compatibility=dict(coordinate_system='right-handed-y-up-z-forward', unit_scale=unit_scale,
                rig_signature=[], skeleton_signature=[], material_capabilities=[], texture_channels=[],
                animation_channels=sorted({t['path'] for clip in rig.clips.values() for t in clip['tracks']}),
                renderers=[], formats=['glb', 'gltf'], extensions=rig.geometry.extensions_used, consumer_capabilities=[]),
            previews=[], provenance=dict(method='derived' if derivation is not None else 'fixture' if provenance['fixture'] else 'import',
                origin='GLB/glTF imported through native validated geometry and rig owners', fixture=provenance['fixture'],
                sources=[record('native/source/'+source_info['path'].name)], provider=[],
                tools=[dict(name='hatch-native-import', version='1.0.0')],
                parents=[] if prior_reference is None else [deepcopy(prior_reference)], edits=[],
                license_evidence=[record('license.txt')], permissions=[], uncertainty=['No human or production-art approval'],
                source_records=[dict(file=record('native/source/'+source_info['path'].name), source_uri='local:'+Path(source).name,
                    original_filename=Path(source).name, acquired_at=created, author=attribution)]),
            publication=dict(visibility='project-local', distribution='restricted', license_evaluation='evidence-retained',
                inventory=list(sources), verification='payload-verified', integration='native-reference')))
        reference = library.register(library_manifest, sources)
        library.resolve(reference)
        for r in records:
            require(hashlib.sha256(library.read_file(reference, r['path'])).hexdigest() == r['sha256'],
                    'Registered payload verification failed.', 'SCENE3D_IMPORT_VALIDATION')
        return ImportedScene(reference, asset_reference, asset, nodes, hierarchy, report)


def materialize(library, reference, destination):
    """Independently export an already registered version to a new directory."""
    manifest = library.resolve(reference)
    destination = _new_destination(destination)
    with tempfile.TemporaryDirectory(prefix='.scene3d-asset-', dir=destination.parent) as temporary:
        stage = Path(temporary)/'asset'
        stage.mkdir(mode=0o700)
        for record in manifest['files']:
            payload = library.read_file(reference, record['path'])
            require(len(payload) == record['bytes'] and hashlib.sha256(payload).hexdigest() == record['sha256'],
                    'Asset changed during materialization.', 'SCENE3D_SOURCE_CHANGED')
            target = stage/portable_path(record['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        _rename_no_replace(stage, destination)
    return dict(destination=str(destination), reference=reference, files=len(manifest['files']))


def resolve_import(library, reference):
    """Reopen the exact registered adapter data without choosing mutable latest."""
    manifest = library.resolve(reference)
    require(manifest['identity']['subtype'] == 'gltf-scene', 'Asset is not a native Scene3D import.')
    prefix = 'imports/'+reference['id']+'/'+reference['version']
    asset_payload = library.read_file(reference, prefix+'/asset.json')
    asset = validate_document(decode_json(asset_payload), 'asset')
    require(asset['id'] == reference['id'] and asset['version'] == reference['version'], 'Asset identity/version mismatch.')
    asset_ref = dict(id=asset['id'], version=asset['version'], kind='scene3d-asset', path=prefix+'/asset.json',
                     sha256=hashlib.sha256(asset_payload).hexdigest())
    inventory = decode_json(library.read_file(reference, prefix+'/authoring-nodes.json'))
    require(isinstance(inventory, dict) and set(inventory) == {'schema_version', 'kind', 'asset', 'nodes'} and
            inventory['schema_version'] == 1 and inventory['kind'] == 'scene3d-import-authoring-nodes' and
            inventory['asset'] == asset_ref and isinstance(inventory['nodes'], list) and 0 < len(inventory['nodes']) <= 10000,
            'Invalid native authoring inventory.')
    from scene3d_contracts import _graph
    for node in inventory['nodes']:
        validate_document(node, 'node')
        require('asset' not in node or node['asset'] == asset_ref, 'Foreign source component asset.')
    _graph(inventory['nodes'], [n['id'] for n in inventory['nodes'] if n['parent'] is None])
    hierarchy = decode_json(library.read_file(reference, prefix+'/hierarchy.json'))
    report = decode_json(library.read_file(reference, prefix+'/import-report.json'))
    require(isinstance(hierarchy, dict) and isinstance(hierarchy.get('nodes'), list) and
            isinstance(report, dict) and report.get('schema_version') == 1 and
            report.get('fixture') == asset['provenance']['fixture'] == manifest['provenance']['fixture'],
            'Import provenance and hierarchy are inconsistent.')
    return ImportedScene(deepcopy(reference), asset_ref, asset, inventory['nodes'], hierarchy['nodes'], report)


def edit_mesh_vertices(library, reference, version, edits, *, attribution):
    """Create a real new asset version from simple bind-space vertex edits.

    Vertex count/topology, source bytes, other meshes, skins and clips are retained.
    Direction morphs/tangents need a geometry owner's rebake and are rejected by
    this simple-edit profile; no topology-editing capability is advertised.
    """
    imported = resolve_import(library, reference)
    require(isinstance(edits, list) and 0 < len(edits) <= 10000, 'Supply 1–10000 vertex edits.')
    from rig_models import load_rig
    from rig_geometry import vertex_normals
    with tempfile.TemporaryDirectory(prefix='scene3d-mesh-edit-') as temporary:
        root = Path(temporary).resolve()
        materialize(library, reference, root/'package')
        base = root/'package'/'imports'/reference['id']/reference['version']
        native = base/'native'
        rig = load_rig(native)
        manifest_record = next(r for r in imported.asset['dependencies'] if r['path'].endswith('/native/rig.json'))
        require(hashlib.sha256((native/'rig.json').read_bytes()).hexdigest() == manifest_record['sha256'],
                'Native manifest differs from registered bytes.')
        meshes = rig.geometry.by_id()
        seen, changed = set(), set()
        for edit in edits:
            require(isinstance(edit, dict) and set(edit) == {'mesh', 'vertex', 'position'}, 'Invalid vertex edit shape.')
            identifier, vertex = edit['mesh'], edit['vertex']
            require(isinstance(identifier, str) and identifier in meshes and type(vertex) is int and
                    0 <= vertex < len(meshes[identifier].positions), 'Invalid native mesh/vertex reference.')
            require((identifier, vertex) not in seen, 'A vertex is edited more than once in one operation.')
            seen.add((identifier, vertex))
            mesh = meshes[identifier]
            require('TANGENT' not in mesh.attributes and all(not set(m) & {'NORMAL', 'TANGENT'} for m in mesh.morphs.values()),
                    'Direction morphs or authored tangents require an explicit native rebake.', 'SCENE3D_MESH_REBAKE')
            try: point = np.asarray(edit['position'], dtype=float)
            except (TypeError, ValueError, OverflowError) as exc: fail('Invalid vertex coordinate: '+str(exc))
            require(point.shape == (3,) and np.isfinite(point).all() and (np.abs(point) <= 1e9).all(),
                    'Vertex coordinates must be finite bounded canonical bind-space meters.')
            mesh.positions[vertex] = point
            changed.add(identifier)
        for identifier in changed:
            meshes[identifier].attributes['NORMAL'] = vertex_normals(meshes[identifier].positions, meshes[identifier].triangles)
        source = root/'edited.glb'
        if rig.manifest.get('skeleton'):
            export_gltf(rig, source, bake=False)
        else:
            export_geometry(rig.geometry, source, provenance=dict(fixture=imported.report['fixture'], operation='explicit-vertex-edit'))
        operation = 'Canonical native bind-space vertex edits: '+json.dumps(edits, sort_keys=True, separators=(',', ':'))
        require(len(operation) <= 4096, 'Edit description exceeds retained operation bound; split into smaller versions.')
        return import_gltf(source, library, asset_id=reference['id'], version=version,
            prior_reference=reference, attribution=attribution, license_file=base/'license.txt',
            fixture=imported.report['fixture'], derivation=operation,
            clip_aliases=imported.report.get('native_clip_map', {}))


def add_import(graph, imported, identifier, *, parent=None, transform=None):
    """Create independently editable source nodes, sharing one immutable asset."""
    scene = graph.document()
    require(identifier not in {n['id'] for n in scene['nodes']}, 'Import root identifier exists.')
    root = make_node(identifier, 'group', parent)
    if transform is not None:
        root['transform'] = deepcopy(transform)
    mapping = {n['id']: scoped_id(identifier, n['id'], 'import') for n in imported.nodes}
    from scene3d_graph import _relink
    nodes = []
    for original in imported.nodes:
        node = _relink(original, mapping)
        if node['parent'] is None:
            node['parent'] = identifier
        node['metadata']['source_asset_sha256'] = imported.asset_reference['sha256']
        nodes.append(node)
    scene['nodes'].extend([root, *nodes])
    scene['provenance']['fixture'] |= imported.report['fixture']
    scene['dependencies'] = _refs([scene['dependencies'], imported.asset_reference])
    result = graph.replace_document(scene, expected_revision=graph.revision)
    return dict(mapping=mapping, root=identifier, **result)


def evaluate_imported_vertices(graph, imported, native_root, *, clip=None, time=0.):
    """Evaluate actual native deformation then each scene component's edit delta."""
    from rig_models import load_rig
    from rig_animation import evaluate_animation
    from rig_skinning import evaluate_deformation
    from rig_math import transform_points
    rig = load_rig(native_root)
    manifest_path = next(r for r in imported.asset['dependencies'] if r['path'].endswith('/native/rig.json'))
    payload = read_source_bytes(Path(native_root)/'rig.json', 16*1024*1024)
    require(hashlib.sha256(payload).hexdigest() == manifest_path['sha256'] and decode_json(payload) == rig.manifest,
            'Native rig manifest differs from the registered exact bytes.', 'SCENE3D_SOURCE_CHANGED')
    require(rig.manifest['source']['sha256'] == imported.report['source_sha256'],
            'Native run does not match the imported source.', 'SCENE3D_SOURCE_CHANGED')
    if clip is not None:
        clip = imported.report['native_clip_map'].get(clip, clip)
        require(clip in rig.clips, 'Unknown imported native clip.')
        evaluated = evaluate_deformation(rig, evaluate_animation(rig, clip, time)['pose'])
    elif rig.manifest.get('skeleton') and rig.weights:
        evaluated = evaluate_deformation(rig)
    else:
        evaluated = {m.id: dict(positions=m.positions) for m in rig.geometry.meshes}
    source_world = {h['source_node']: np.asarray(h['world_matrix']) for h in imported.hierarchy}
    by_id = rig.geometry.by_id()
    result = {}
    for node in graph.document()['nodes']:
        if node.get('asset') != imported.asset_reference:
            continue
        require(node['metadata'].get('source_asset_sha256') == imported.asset_reference['sha256'],
                'Imported component identity is absent or stale.')
        index = node['metadata'].get('source_node')
        require(type(index) is int and index in source_world, 'Missing original semantic node binding.')
        component = node['metadata'].get('source_component')
        require(component in imported.asset['components'], 'Missing mesh component binding.')
        current = np.asarray(graph.evaluate_base(node['id'])['world_matrix'])
        delta = current@_inverse(source_world[index])
        result[node['id']] = {}
        for mesh in imported.asset['components'][component]:
            require(mesh in by_id and by_id[mesh].source_node == index, 'Mesh component/source node mismatch.')
            result[node['id']][mesh] = transform_points(delta, evaluated[mesh]['positions']).tolist()
    return dict(nodes=result, native_clip=clip, time=time, actual_deformation=True, production_approved=False)
