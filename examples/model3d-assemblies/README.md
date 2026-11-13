# Procedural assembly engineering fixtures

The fourteen JSON requests are accepted version-one deterministic assembly IR.
They create actual independent named meshes through the existing native mesh
and glTF owners. `verified/mug`, `verified/robot` and `verified/arch` retain real
GLB files, exact editable source/IR, hash inventories and independent Khronos
validator receipts. They are synthetic engineering fixtures, visually unreviewed.

Run from the repository root with the optional rig dependencies:

```bash
python scripts/model3d_assemblies.py create --file examples/model3d-assemblies/mug.json --destination output/new-mug
python scripts/model3d_assemblies.py demo --destination output/new-assemblies-demo
```

The demo executes all fourteen recipes in both GLB/glTF, reimports each real
artifact, checks part/world geometry and material assignments, and writes exact
hashes in its `acceptance.json`. The executed demonstration is retained at
`/home/complete/.cache/hatch-coordination-20261112/model-evidence/assemblies45-demo-0226`.
All 28 artifacts passed official Khronos validator `2.0.0-dev.3.10` with zero
errors and zero warnings. `evidence.json` pins generator module bytes for those
artifacts. Independent pygltflib tests additionally decode real accessors and
inspect hierarchy/material assignments. Rendering and Blender editability were
not executed for this scoped change.

The mug has a hollow tube body, separate bottom and editable torus handle; its
surface parts overlap and are not a Boolean manufacturing solid. Robots/animals
are stylized separate meshes, not automatically skinned rigs. Curve sweeps,
materials and all finite-language limitations are documented in
[assemblies](../../references/model3d/assemblies.md). Parent Model3D completion,
textures, Blender, review and full lifecycle acceptance remain separate gates.
