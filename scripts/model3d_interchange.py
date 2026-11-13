"""Bounded unrigged mesh interchange through the native geometry authority.

OBJ is deliberately a strict diffuse-material profile, not a PBR/rig container.
Exports publish a new directory exclusively; source arrays and image bytes stay
unchanged. GLB/glTF encoding remains owned by rig_gltf.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import shutil
import shlex
import tempfile

import numpy as np

from atomic_publish import _rename_no_replace
from rig_contracts import DEFAULT_LIMITS
from rig_errors import require
from rig_models import Geometry, validate_geometry


def _name(value, fallback):
    value = value or fallback
    require(isinstance(value, str) and 0 < len(value) <= 4096
            and value == value.strip() and all(ord(c) >= 32 and ord(c) != 127 for c in value),
            'MODEL3D_EXPORT_NAME', 'Names must be bounded single-line strings without surrounding whitespace.')
    return value


def _obj_payloads(geometry, limits):
    require(not geometry.extensions_used, 'MODEL3D_EXPORT_LOSS', 'OBJ cannot preserve glTF extensions.')
    names = [_name(m.name, m.id) for m in geometry.meshes]
    require(len(set(names)) == len(names), 'MODEL3D_EXPORT_NAME',
            'OBJ object names must be unique to preserve independently editable parts.')
    materials, payloads = [], {}
    used_materials = sorted({m.material for m in geometry.meshes})
    material_names = {i:_name(geometry.materials[i].get('name'), f'material_{i}') for i in used_materials}
    require(len(set(material_names.values())) == len(material_names) and 'default' not in material_names.values(),
            'MODEL3D_EXPORT_NAME', 'OBJ material names must be unique and must not use reserved default.')
    for i in used_materials:
        material = geometry.materials[i]
        require(not set(material) - {'name', 'pbrMetallicRoughness', 'alphaMode'},
                'MODEL3D_EXPORT_LOSS', 'OBJ diffuse profile cannot represent this material metadata.')
        pbr = material.get('pbrMetallicRoughness', {})
        require(not set(pbr) - {'baseColorFactor', 'baseColorTexture', 'metallicFactor', 'roughnessFactor'}
                and pbr.get('metallicFactor') == 0 and pbr.get('roughnessFactor') == .65
                and material.get('alphaMode', 'OPAQUE') in ('OPAQUE', 'BLEND'),
                'MODEL3D_EXPORT_LOSS', 'OBJ requires explicit metallicFactor=0 and roughnessFactor=0.65 diffuse profile; '
                'omitted glTF factors default to metallic/roughness=1 and cannot be silently reinterpreted.')
        color = np.asarray(pbr.get('baseColorFactor', [1., 1., 1., 1.]))
        require(color.shape == (4,) and np.isfinite(color).all() and np.all((color >= 0) & (color <= 1)),
                'MODEL3D_EXPORT_MATERIAL', 'Diffuse color must contain four finite values in [0,1].')
        require(material.get('alphaMode', 'OPAQUE') == 'BLEND' or color[3] == 1,
                'MODEL3D_EXPORT_LOSS', 'Opaque material alpha would become OBJ opacity.')
        name = material_names[i]
        lines = ['newmtl '+shlex.quote(name), 'Kd '+' '.join(format(float(v), '.17g') for v in color[:3])]
        if material.get('alphaMode') == 'BLEND':lines.append('d '+format(float(color[3]), '.17g'))
        texture = pbr.get('baseColorTexture')
        if texture is not None:
            require(set(texture) <= {'index', 'texCoord'} and texture.get('texCoord', 0) == 0,
                    'MODEL3D_EXPORT_LOSS', 'OBJ diffuse texture requires untransformed TEXCOORD_0.')
            index = texture.get('index')
            require(type(index) is int and 0 <= index < len(geometry.textures),
                    'MODEL3D_EXPORT_MATERIAL', 'Invalid diffuse texture index.')
            tex = geometry.textures[index]
            require(set(tex) <= {'source', 'sampler'} and 'sampler' not in tex,
                    'MODEL3D_EXPORT_LOSS', 'OBJ cannot preserve texture sampler metadata.')
            image = geometry.images[tex['source']]
            suffix = '.png' if image.mime_type == 'image/png' else '.jpg'
            filename = 'texture_'+hashlib.sha256(image.data).hexdigest()+suffix
            payloads[filename] = image.data
            lines.append('map_Kd '+filename)
        materials.extend(lines+[''])
    obj = ['# Hatch Model3D diffuse OBJ; coordinates remain canonical meters.', 'mtllib materials.mtl']
    offset = uv_offset = normal_offset = 1
    for mesh, name in zip(geometry.meshes, names):
        require(not mesh.morphs and not mesh.morph_defaults
                and set(mesh.attributes) <= {'NORMAL', 'TEXCOORD_0'},
                'MODEL3D_EXPORT_LOSS', 'OBJ cannot preserve morphs, tangents, vertex colors or secondary UVs.')
        material = geometry.materials[mesh.material]
        if material.get('pbrMetallicRoughness', {}).get('baseColorTexture'):
            require('TEXCOORD_0' in mesh.attributes, 'MODEL3D_EXPORT_MATERIAL', 'Textured mesh requires UV0.')
        obj += ['o '+shlex.quote(name), 'usemtl '+shlex.quote(material_names[mesh.material])]
        obj += ['v '+' '.join(format(float(x), '.17g') for x in row) for row in mesh.positions]
        uv = mesh.attributes.get('TEXCOORD_0'); normal = mesh.attributes.get('NORMAL')
        # OBJ bottom-left UV origin; native/glTF attributes use top-left origin.
        if uv is not None:obj += ['vt '+format(float(u), '.17g')+' '+format(float(1-v), '.17g') for u,v in uv]
        if normal is not None:obj += ['vn '+' '.join(format(float(x), '.17g') for x in row) for row in normal]
        for triangle in mesh.triangles:
            corners=[]
            for vertex in triangle:
                v=int(vertex)+offset; u=int(vertex)+uv_offset; n=int(vertex)+normal_offset
                corners.append(str(v)+(('/'+str(u)) if uv is not None else '/')+('/'+str(n) if normal is not None else '')
                               if uv is not None or normal is not None else str(v))
            obj.append('f '+' '.join(corners))
        offset += len(mesh.positions)
        if uv is not None:uv_offset += len(mesh.positions)
        if normal is not None:normal_offset += len(mesh.positions)
    payloads['model.obj'] = ('\n'.join(obj)+'\n').encode('utf-8')
    payloads['materials.mtl'] = ('\n'.join(materials)+'\n').encode('utf-8')
    return payloads


def export_mesh(geometry: Geometry, destination: str | Path, *, format='glb', limits=None, editable=None):
    """Publish standalone native geometry as model.glb/.gltf or model.obj+MTL.

    No skin, animation, hierarchy or source polygon topology is implied by a
    Geometry value. Callers with those records must use their owning exporter.
    Native identities are additionally recorded in the hash-bound manifest;
    OBJ itself has object/material names rather than native mesh identifiers.
    """
    require(limits is None or isinstance(limits, dict), 'MODEL3D_EXPORT_LIMIT', 'Limits must be a mapping.')
    overrides = limits or {}
    require(not set(overrides)-set(DEFAULT_LIMITS), 'MODEL3D_EXPORT_LIMIT', 'Unknown limit name.')
    limits = dict(DEFAULT_LIMITS, **overrides)
    for key, value in limits.items():
        require(type(value) in (int, float) and 0 < value <= 2**53
                and math.isfinite(value) and value == int(value),
                'MODEL3D_EXPORT_LIMIT', 'Limits must be finite positive whole numbers.', asset=key)
        limits[key] = int(value)
    validate_geometry(geometry, limits)
    require(format in ('obj', 'glb', 'gltf'), 'MODEL3D_EXPORT_FORMAT', 'Choose obj, glb or gltf.')
    require(geometry.vertex_count*2000 + geometry.triangle_count*500
            + sum(len(i.data)*2 for i in geometry.images) <= limits['memory_bytes'],
            'RIG_LIMIT_MEMORY', 'Export text/binary working estimate exceeds memory budget.')
    destination = Path(destination).absolute()
    # Anchor publication in the resolved existing parent; never create a tree
    # through untrusted path components or replace an existing destination.
    parent = destination.parent.resolve(strict=True)
    destination = parent/destination.name
    require(not destination.exists() and not destination.is_symlink(),
            'MODEL3D_EXPORT_EXISTS', 'Export destination must not exist.')
    editable_bytes = None
    if editable is not None:
        from model3d_primitives import EditableMesh
        require(isinstance(editable, EditableMesh) and len(geometry.meshes) == 1 and geometry.meshes[0] is editable.mesh,
                'MODEL3D_EXPORT_TOPOLOGY', 'Editable source must own this exact render mesh.')
        from model3d_mesh_contracts import validate_editable
        editable_record = validate_editable(dict(schema_version=1, kind='hatch-model3d-editable-topology',
            mesh=editable.mesh.id, positions=editable.source_positions.tolist(), faces=[list(face) for face in editable.faces],
            triangle_faces=editable.triangle_faces.tolist(), normal_mode=editable.normal_mode))
        editable_bytes = (json.dumps(editable_record, allow_nan=False, separators=(',', ':'))+'\n').encode()
    payloads = _obj_payloads(geometry, limits) if format == 'obj' else None
    stage = Path(tempfile.mkdtemp(prefix='.model3d-export-', dir=parent))
    try:
        if payloads is None:
            from rig_gltf import export_geometry
            export_geometry(geometry, stage/('model.'+format))
            payloads = {p.name:p.read_bytes() for p in stage.iterdir()}
        else:
            for name, data in payloads.items():(stage/name).write_bytes(data)
        # Verify the encoded artifact, not merely its higher precision source.
        # Float32 GLB conversion can collapse otherwise finite small triangles.
        from rig_import import load_character_model
        encoded = load_character_model(stage/('model.'+format), limits=limits).geometry
        require(encoded.triangle_count == geometry.triangle_count,
                'MODEL3D_EXPORT_ROUNDTRIP', 'Encoded artifact changed triangle topology.')
        require(len(encoded.meshes) == len(geometry.meshes), 'MODEL3D_EXPORT_ROUNDTRIP', 'Encoded artifact changed mesh partitioning.')
        from model3d_topology import _degenerate, _face_vectors
        from model3d_coordinates import _positions
        for before, after in zip(geometry.meshes, encoded.meshes):
            require(len(before.positions) == len(after.positions), 'MODEL3D_EXPORT_ROUNDTRIP', 'Encoded artifact lost vertices.')
            original_corners = _positions(before)[before.triangles]
            encoded_corners = _positions(after)[after.triangles]
            span = np.max(np.abs(original_corners[:, 1:]-original_corners[:, :1]), axis=(1, 2))
            error = np.max(np.abs(original_corners-encoded_corners), axis=(1, 2))
            require(np.all(error <= span*1e-3), 'MODEL3D_EXPORT_ROUNDTRIP',
                    'Encoded face coordinates exceed the relative 1e-3 preservation tolerance.')
            before_vectors, _ = _face_vectors(before)
            after_vectors, _ = _face_vectors(after)
            require(np.all(np.sum(before_vectors*after_vectors, axis=1) >= 0), 'MODEL3D_EXPORT_ROUNDTRIP',
                    'Encoded precision reversed face orientation.')
            original_degenerate, encoded_degenerate = _degenerate(before), _degenerate(after)
            require(original_degenerate.shape == encoded_degenerate.shape
                    and not np.any(~original_degenerate & encoded_degenerate),
                    'MODEL3D_EXPORT_ROUNDTRIP', 'Encoded precision collapsed nondegenerate faces.')
        if editable_bytes is not None:
            payloads['editable.json'] = editable_bytes
            (stage/'editable.json').write_bytes(editable_bytes)
        require(sum(map(len, payloads.values())) <= limits['dependency_bytes']
                and all(len(data) <= limits['source_bytes'] for data in payloads.values()),
                'MODEL3D_EXPORT_LIMIT', 'Encoded export exceeds source/dependency limits.')
        files={name:dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest()) for name,data in sorted(payloads.items())}
        manifest=dict(schema_version=1, kind='hatch-model3d-mesh-export', format=format,
                      vertices=geometry.vertex_count, triangles=geometry.triangle_count, files=files,
                      mesh_identity=[dict(id=m.id,name=m.name,region=m.region,source_node=m.source_node) for m in geometry.meshes],
                      material_names=[m.get('name') for m in geometry.materials],
                      exported_material_slots=[dict(slot=i,name=_name(geometry.materials[i].get('name'),f'material_{i}'))
                                               for i in sorted({m.material for m in geometry.meshes})],
                      limitations=['Standalone Geometry has no scene hierarchy, skin or animation authority.']
                      +(['OBJ retains diffuse color/opacity/UV0 only; native IDs remain in this manifest.',
                         'Unreferenced material slots and images are not emitted to OBJ/MTL; source slot names remain in this manifest.'] if format=='obj' else []))
        from model3d_mesh_contracts import validate_export
        manifest = validate_export(manifest)
        (stage/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False,allow_nan=False)+'\n', encoding='utf-8')
        _rename_no_replace(stage, destination)
        return manifest
    finally:
        if stage.exists():shutil.rmtree(stage)
