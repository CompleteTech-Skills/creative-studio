# Procedural Director assets

`python scripts/director_assets.py --destination NEW_DIRECTORY --seed 42` creates
four reusable assets for Director #28: Aster, Moss, a laboratory and an access
key. These are procedural engineering fixtures, never provider-generated or
artistically approved characters. Aster and Moss are metallic variants of the
existing humanoid rig fixture, with different proportions and colors.

The builder delegates geometry to `rig_demo.make_geometry` and the Model3D
`box` primitive, skeleton construction to `rig_templates`, skin binding to
`rig_skinning`, GLB writing to `rig_gltf`, and import verification to `rig_import`.
Registration uses the existing `AssetLibrary`; no additional asset database or
scene graph is introduced. The local library must remain a private owned
folder according to its existing contract.

Each robot preserves an unrigged original GLB, a native editable rig directory,
a skinned GLB, authored landmarks and binding regions. `load_rig(path/'rig-aster')`
loads Aster for subsequent native animation. This stage authors no movement,
camera animation, Blender project, render or movie. `rig-moss` is independent
and keeps Moss's own identity. The source meshes retain named editable
components; material slots and canonical meters/Y-up coordinates are retained.

The laboratory has a floor, three walls with an opening, a lintel, a workbench,
a console and a cabinet. Its recipe records every named component's dimensions,
center/pivot and material. The GLB retains a separate node for each centered component with its
recorded translation/pivot; the recipe remains the editable construction source. It is not an interactive scene editor. The access key is a
separate mesh, allowing the animation/scene owner to assign its transform and
ownership without altering laboratory geometry.

`assets.json` contains exact source identities, native rig paths, immutable
library references, measured topology/bounds and explicit route/camera checks.
Each `*-asset.json` is a sealed native library manifest. Original GLBs, identity,
recipe and license bytes are registered and retrieved to verify preservation;
rigged GLBs are also registered for the two robots. Recipe records bind the
exact generator and native primitive source hashes. Native rig histories retain
their actual creation timestamps; reproducibility compares immutable source
identity and normalized geometry, not wall-clock-dependent history bytes.
The current recipe is deterministic without random perturbations; `seed` is a
retained explicit input for repeatability, not a claim of stochastic generation.

Generation accepts a new destination beneath an existing nonsymlink parent. The
Linux writer uses owned ordinary scratch paths for the registry and rig owners,
then verifies the exact copied bundle, including SQLite, inside descriptor-pinned
staging. The repository's exclusive rename preserves occupied destinations.
Build scratch cleanup finishes before promotion. A cleanup problem after
promotion adds a warning to the returned report, and the verified bundle stays
published. Only owned staging contents are cleaned. The empty wrappers remain for
owned maintenance under the shared publication contract. Other platforms receive
a typed unsupported diagnostic when that adapter is unavailable.
CLI failures include the typed error code and a path-free message. The Python
API provides the original structured error details.

Decoded source geometry is limited to 12,000 vertices and 24,000 triangles
across the four assets. The complete bundle, including the registry and native
rig histories, is limited to 64 MiB and 256 files before publication. The
geometry checks require valid indices, finite unit normals, positive closed
volume, and zero boundary, nonmanifold or degenerate surface elements.

Layout checks use the decoded neutral robot bounds expanded to the nominal
clearance envelope. Complete straight segments must remain on the laboratory
floor and clear its static AABBs. The receipt includes both measured and
clearance bounds, and 51 samples per segment. Camera positions must have
0.12 m clearance within the room. This evidence covers static feasibility.
Physics, moving bodies, line of sight and framing belong to subsequent work.

Tests independently load every GLB through `pygltflib`, decode positions and
check index counts. Native import and geometry validation check the indices.
Tests also reopen native rigs, evaluate actual skinned deformation,
reopen the registered library, compare repeated source identities/normalized
geometry and exercise occupied/racing destinations, unsafe paths and layout
obstacles. They run in the optional 3D operations gate:

```bash
PYTHONPATH=scripts:tests python -m unittest test_rig_director_assets -v
```

The builder requires the existing optional rig dependencies and the canonical
Model3D primitives from PR #221. Original 2D contracts and dependencies are
unchanged. The broader Director #18 production and visual acceptance gates
remain open.

The [local seed-42 fixture receipt](../tests/results/director-assets-codex-20261117/receipt.json)
records the source hashes, measured counts and exact registry references from
one build and reopen. Registry timestamps belong to that individual run.
