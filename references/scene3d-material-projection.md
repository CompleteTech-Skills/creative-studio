# Canonical Scene3D material primitive projection

`scene3d_materials.project_material_slots(asset, component=None, overrides=())`
returns validated primitive records with their exact override reference or `None`.
The optional `asset.material_bindings` records are authored by the native importer;
the original GLB/glTF materials and textures remain byte-preserved native content.
An imported native PBR record is not automatically a LookDev Registry resource.

Each imported slot `material-N` records the original source mesh, primitive and
material index, the actual native mesh ID and its normalized GLB mesh/primitive.
Normalization creates one primitive per native mesh; shared source mesh instances
remain separate native mesh identities. A source global material index is not a
Blender object-local material slot. A Blender adapter must verify its actual
imported object and establish a local slot before calling material assignment.

Generated triangular OBJ assets use `obj_material_bindings(asset_id)` and the
single named `surface` slot. They have no fabricated source material or normalized
GLB index. Their material index fields are explicitly null. Legacy assets without
bindings still load; material projection explicitly rejects them until an owner
provides a real mapping.

Named overrides must address a slot actually present on the selected component.
`default` intentionally selects every projected surface; a named override takes
precedence. Unknown, duplicate, malformed or unpinned overrides fail. Projection
returns copied exact material references; the caller uses the canonical LookDev
Registry to resolve them and includes that resource's complete dependencies in a
snapshot. This module neither evaluates shaders nor manufactures Registry refs.

Validation: `test_rig_scene3d_materials` exports and imports actual multi-primitive,
shared-instance GLBs, compares persisted records to the real normalized GLB JSON,
checks exact reference projection and immutability, and rejects incomplete,
unknown or incorrectly targeted bindings. Actual shaded rendering and independent
LookDev texture-closure validation remain the consuming adapter's acceptance work.
