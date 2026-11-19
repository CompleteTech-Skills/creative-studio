"""Save, edit and independently verify named static Model3D parts with Blender."""
from __future__ import annotations

from pathlib import Path
import tempfile

import numpy as np

from atomic_publish import _rename_no_replace
from rig_blender import _run
from rig_errors import atomic_json, require, sha256
from rig_gltf import export_geometry, read_gltf
from rig_import import load_character_model
from rig_models import Geometry, validate_geometry


def _vector(value, size, lower, upper):
    require(isinstance(value, (list, tuple)) and len(value) == size
            and all(type(x) in (int, float) and lower <= x <= upper for x in value),
            'MODEL3D_BLENDER_EDIT', 'Expected a bounded numeric edit vector.')
    return np.array(value, dtype=np.float32)


def _names(values):
    require(all(isinstance(v, str) and v and v.isprintable() and v == v.strip()
                and len(v.encode('utf-8')) <= 63 for v in values)
            and len(set(values)) == len(values), 'MODEL3D_BLENDER_NAME',
            'Blender datablock names must be unique printable names of at most 63 UTF-8 bytes.')


def _profile(geometry, part, delta, color):
    require(isinstance(geometry, Geometry) and 1 <= len(geometry.meshes) <= 64
            and 1 <= len(geometry.materials) <= len(geometry.meshes)
            and geometry.vertex_count <= 16384 and geometry.triangle_count <= 32768,
            'MODEL3D_BLENDER_PROFILE', 'Expected at most 64 parts, 16384 vertices and 32768 triangles.')
    validate_geometry(geometry)
    _names([m.id for m in geometry.meshes])
    _names([m.name or m.id for m in geometry.meshes])
    _names([mat.get('name') for mat in geometry.materials])
    require(not (geometry.textures or geometry.images or geometry.samplers or geometry.extensions_used)
            and {m.material for m in geometry.meshes} == set(range(len(geometry.materials))),
            'MODEL3D_BLENDER_PROFILE', 'Only used, untextured material slots are supported.')
    for mesh in geometry.meshes:
        require(not (mesh.attributes or mesh.morphs or mesh.morph_defaults)
                and isinstance(mesh.region, str) and len(mesh.region) <= 128
                and np.max(np.abs(np.asarray(mesh.positions, dtype=float))) <= 10000
                and len(np.unique(mesh.triangles)) == len(mesh.positions),
                'MODEL3D_BLENDER_PROFILE', 'Expected referenced static positions within 10000 meters and triangle indices only.')
        points = np.asarray(mesh.positions, dtype=np.float32)
        require(len(np.unique(points, axis=0)) == len(points), 'MODEL3D_BLENDER_PROFILE',
                'This geometry-only profile requires distinct vertex positions within each part.')
    for mat in geometry.materials:
        pbr = mat.get('pbrMetallicRoughness', {})
        require(not set(mat) - {'name', 'pbrMetallicRoughness'}
                and not set(pbr) - {'baseColorFactor', 'metallicFactor', 'roughnessFactor'}
                and pbr.get('baseColorFactor', [1]*4)[3] == 1,
                'MODEL3D_BLENDER_PROFILE', 'Expected opaque named base-color, metallic and roughness factors.')
    require(isinstance(part, str) and part in geometry.by_id(), 'MODEL3D_BLENDER_PART', 'Unknown target mesh ID.')
    target = geometry.by_id()[part]
    old = geometry.materials[target.material]
    positions = np.asarray(target.positions, dtype=np.float32)
    moved = positions + delta
    require(np.any(positions != moved) and len(np.unique(moved, axis=0)) == len(positions)
            and color[3] == 1 and not np.allclose(color, old.get('pbrMetallicRoughness', {}).get('baseColorFactor', [1]*4),
                                                   rtol=0, atol=1e-6),
            'MODEL3D_BLENDER_EDIT', 'Edit must move the mesh, preserve distinct positions and change its opaque color.')
    shared = sum(m.material == target.material for m in geometry.meshes) > 1
    material_name = old['name']
    if shared:
        material_name = 'edited.' + part
        _names([m['name'] for m in geometry.materials] + [material_name])
    return shared, material_name


def _faces(positions, indices):
    # Canonicalize face order and cyclic corner order, while preserving winding
    # and multiplicity. Blender can renumber vertices and split flat normals.
    faces = []
    for points in positions[indices]:
        a, b, c = map(tuple, points)
        faces.append(min((a, b, c), (b, c, a), (c, a, b)))
    return sorted(faces)


def _verify(path, geometry, part, delta, color, material_name, fixture):
    document = read_gltf(path).json
    nodes = document['nodes']
    by_id = geometry.by_id()
    require(len(nodes) == len(by_id) and {n.get('name') for n in nodes} == set(by_id)
            and all('mesh' in n and n.get('extras', {}).get('hatchFixture') == fixture for n in nodes),
            'MODEL3D_BLENDER_VERIFY', 'Exported node identity or fixture provenance changed.')
    decoded = load_character_model(path).geometry
    require(set(decoded.by_id()) == set(by_id), 'MODEL3D_BLENDER_VERIFY', 'Reopened mesh IDs changed.')
    for actual in decoded.meshes:
        source = by_id[actual.id]
        positions = np.asarray(source.positions, dtype=np.float32)
        if source.id == part:
            positions = positions + delta
        require(actual.name == (source.name or source.id) and actual.region == source.region
                and nodes[actual.source_node]['name'] == source.id
                and _faces(positions, source.triangles) == _faces(actual.positions, actual.triangles),
                'MODEL3D_BLENDER_VERIFY', 'Exported part identity or oriented triangles changed.', asset=source.id)
        old = geometry.materials[source.material]
        expected = old.get('pbrMetallicRoughness', {})
        actual_mat = decoded.materials[actual.material]
        pbr = actual_mat.get('pbrMetallicRoughness', {})
        expected_color = color if source.id == part else expected.get('baseColorFactor', [1]*4)
        require(actual_mat.get('name') == (material_name if source.id == part else old['name'])
                and np.allclose(pbr.get('baseColorFactor', [1]*4), expected_color, rtol=0, atol=1e-6)
                and all(abs(pbr.get(k, 1) - expected.get(k, 1)) <= 1e-6 for k in ('roughnessFactor', 'metallicFactor')),
                'MODEL3D_BLENDER_VERIFY', 'Exported part material differs from the expected edit.', asset=source.id)


def roundtrip_named_part(geometry, destination, *, part, offset_m, base_color, fixture, executable=None, timeout=120):
    """Edit one mesh's vertices in canonical meters and its opaque linear RGBA.

    The destination must be new, inside an existing caller-controlled directory.
    Only this invocation's locally generated GLB and blend files enter Blender.
    """
    require(type(fixture) is bool and type(timeout) is int and 1 <= timeout <= 300,
            'MODEL3D_BLENDER_PROFILE', 'Specify fixture provenance and a 1-300 second per-process timeout.')
    delta = _vector(offset_m, 3, -10000, 10000)
    color = _vector(base_color, 4, 0, 1)
    shared, material_name = _profile(geometry, part, delta, color)
    destination = Path(destination).absolute()
    destination = destination.parent.resolve(strict=True) / destination.name
    require(not destination.exists() and not destination.is_symlink(), 'MODEL3D_EXPORT_EXISTS',
            'Named-part export requires a new destination.')
    with tempfile.TemporaryDirectory(prefix='.model3d-blender-', dir=destination.parent) as directory:
        stage = Path(directory)
        out = stage / 'payload'
        out.mkdir()
        source = out / 'source.glb'
        original = out / 'source.blend'
        before = out / 'reopened-source.glb'
        edited = out / 'edited.blend'
        after = out / 'edited.glb'
        export_geometry(geometry, source, provenance={'fixture': fixture})
        edit = dict(part=part, offset=delta.tolist(), color=color.tolist(), copy_material=shared, material_name=material_name)
        jobs = [dict(mode='model3d-create', input=str(source), output=str(original), fixture=fixture),
                dict(mode='model3d-edit', input=str(original), output=str(edited), export=str(before), **edit),
                dict(mode='model3d-export', input=str(edited), export=str(after))]
        versions = []
        hashes = {source.name: sha256(source)}
        for job in jobs:
            result = _run(dict(job, report=str(stage / (job['mode'] + '.json'))), executable=executable, timeout=timeout)
            versions.append(result['blender_version'])
            for key in ('output', 'export'):
                if key in job:
                    artifact = Path(job[key])
                    hashes[artifact.name] = sha256(artifact)
        require(len(set(versions)) == 1, 'MODEL3D_BLENDER_VERIFY', 'Blender version changed during the roundtrip.')
        _verify(before, geometry, None, delta, color, material_name, fixture)
        _verify(after, geometry, part, delta, color, material_name, fixture)
        require(all(sha256(out / name) == digest for name, digest in hashes.items()),
                'MODEL3D_BLENDER_VERIFY', 'An earlier artifact changed during a later authoring stage.')
        receipt = dict(schema_version=1, blender_version=versions[0], fixture=fixture, edit=edit,
                       verified_parts=[m.id for m in geometry.meshes], triangles_per_export=geometry.triangle_count,
                       files=hashes)
        atomic_json(out / 'receipt.json', receipt)
        _rename_no_replace(out, destination)
        return receipt
