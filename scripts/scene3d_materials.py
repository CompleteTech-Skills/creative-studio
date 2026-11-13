"""Canonical native primitive/material projection shared by renderer and editor.

A source material index is global to the source glTF. Normalization emits one
primitive per native mesh. Blender local slot indexes must be resolved against
its imported objects by the Blender adapter, never copied from this index.
"""
from copy import deepcopy

from scene3d_graph import require


def native_material_bindings(document, geometry):
    """Record source and normalized primitive identities from actual native import."""
    slots = {}
    by_node = {}
    for mesh in geometry.meshes:
        by_node.setdefault(mesh.source_node, []).append(mesh)
    normalized = {mesh.id: index for index, mesh in enumerate(geometry.meshes)}
    for node_index, meshes in sorted(by_node.items()):
        require(type(node_index) is int and 0 <= node_index < len(document['nodes']),
                'Native material component lacks a source node.')
        source_mesh = document['nodes'][node_index]['mesh']
        primitives = document['meshes'][source_mesh]['primitives']
        require(len(meshes) == len(primitives), 'Native primitive material mapping is incomplete.')
        for primitive_index, (mesh, primitive) in enumerate(zip(meshes, primitives)):
            require(type(mesh.material) is int and 0 <= mesh.material < len(geometry.materials),
                    'Native primitive has an invalid material index.')
            source_material = primitive.get('material')
            require(source_material is None or source_material == mesh.material,
                    'Native material mapping differs from source primitive.')
            slot = slots.setdefault(mesh.material, dict(slot='material-'+str(mesh.material),
                material_index=mesh.material, primitives=[]))
            slot['primitives'].append(dict(mesh=mesh.id, encoding='normalized-glb', source_mesh=source_mesh,
                source_primitive=primitive_index, source_material_index=source_material,
                normalized_mesh=normalized[mesh.id], normalized_primitive=0))
    return [slots[index] for index in sorted(slots)]


def obj_material_bindings(asset_id):
    """Declare the single selectable surface of a generated triangular OBJ.

    OBJ has no original material or normalized GLB primitive in this profile.
    A renderer must establish its local object slot before assigning a material.
    """
    return [dict(slot='surface', material_index=None, primitives=[dict(mesh=asset_id,
        encoding='obj', source_mesh=None, source_primitive=0, source_material_index=None,
        normalized_mesh=None, normalized_primitive=None)])]


def project_material_slots(asset, *, component=None, overrides=()):
    """Return explicit normalized primitives and exact override pins.

    A named override addresses the persisted slot. ``default`` intentionally
    applies to every primitive, and a named slot takes precedence. No generic
    material pin is reinterpreted as a native glTF PBR record; callers resolve
    the exact pin with the LookDev Registry before rendering it.
    """
    from scene3d_contracts import validate_document
    asset = validate_document(asset, 'asset')
    bindings = asset.get('material_bindings')
    require(bool(bindings), 'Asset has no canonical material primitive mapping.',
            'SCENE3D_MATERIAL_MAPPING')
    require(isinstance(overrides, (list, tuple)) and len(overrides) <= 128,
            'Material overrides must be a bounded list.')
    selected = None
    if component is not None:
        require(isinstance(component, str) and component in asset['components'],
                'Material projection references an unknown asset component.')
        selected = set(asset['components'][component])
        require(bool(selected), 'Material component must contain actual meshes.')
    named = {}
    known = {binding['slot'] for binding in bindings
             if selected is None or any(p['mesh'] in selected for p in binding['primitives'])}
    for override in overrides:
        require(isinstance(override, dict) and set(override) == {'slot', 'material'},
                'Material override needs slot and exact material pin.')
        slot = override['slot']
        require(isinstance(slot, str) and slot in known | {'default'} and slot not in named,
                'Material override has an unknown or duplicate slot.')
        # Reuse the canonical node schema to validate an exact material pin.
        from scene3d_example import node
        probe = node('material-projection', 'group', None)
        probe['material_overrides'] = [deepcopy(override)]
        validate_document(probe, 'node')
        named[slot] = deepcopy(override['material'])
    output = []
    for binding in bindings:
        for primitive in binding['primitives']:
            if selected is not None and primitive['mesh'] not in selected:
                continue
            output.append(dict(**deepcopy(primitive), slot=binding['slot'],
                material_index=binding['material_index'],
                override=deepcopy(named.get(binding['slot'], named.get('default')))))
    require(selected is None or {p['mesh'] for p in output} == selected,
            'Material mapping does not cover the selected component.')
    return output
