# Deterministic procedural assemblies

The `model3d_assemblies` modules implement a finite, local text-to-mesh baseline
for #45. They reuse `Mesh`, `Geometry`, editable native primitives, native
transforms and the native glTF builder/importer. No external provider runs.
The output contains actual independent named meshes; it is not a sprite or a
manifest standing in for absent geometry. Coordinates are right-handed, +Y up,
+Z forward, meters, XYZW quaternions. This does not complete parent #10.

## Working commands and text language

With the existing optional rig dependencies installed:

```bash
python scripts/model3d_assemblies.py capabilities
python scripts/model3d_assemblies.py explain --description 'robot height=2m width=1m depth=.6m material=metal color=blue seed=7'
python scripts/model3d_assemblies.py create --description 'mug height=20cm width=10cm depth=10cm material=ceramic color=#c02820' --destination output/new-mug --format glb
python scripts/model3d_assemblies.py create --file examples/model3d-assemblies/robot.json --destination output/new-robot --format gltf
python scripts/model3d_assemblies.py schema
python scripts/model3d_assemblies.py demo --destination output/new-assemblies-demo
```

The destination's parent must already exist. The destination must be new;
existing directories, files and dangling symlinks are preserved. The standalone
entry point is available independently of central Model3D CLI integration.

Text consists of exactly one lowercase recipe followed by zero or more unique
`key=value` tokens. Recipes: `robot`, `animal`, `mug`, `table`, `chair`, `lamp`,
`sword`, `tree`, `rock`, `room`, `doorway`, `arch`, `staircase`, `building`.
Optional attributes are `height`, `width`, `depth`, `material`, `color`, `seed`,
`id`, and staircase-only `steps`. Dimension suffixes are `m`, `cm`, `mm`; omitted
suffix means meters. Dimensions are proportion parameters in [.01,100] meters;
small subparts must additionally satisfy the shape minimum of .0001 meters.
The geometry report records actual bounds; decorative details and handles may
extend beyond the selected template proportions. Seed is an integer from 0 to
4294967295; staircase steps are 1..32. Rock uses the seed to vary lobe placement;
other templates retain it as provenance without artificial random variation.

Supported colors are `white`, `black`, `red`, `blue`, `green`, `yellow`, `brown`,
`gray`, `pink`, or a six-digit `#RRGGBB` value. Material families are `plastic`,
`metal`, `rubber`, `cloth`, `wood`, `stone`, `glass`, `ceramic`, `painted`. These
are explicit scalar PBR finishes, not generated textures, physical glass
transmission, learned fur, a full style adapter or artistic review. The finite
language rejects unknown words, adjectives, attributes, repeated keys and
unsupported families. It never silently ignores an unsupported description.

## Versioned editable intermediate representation

`schema()` in `model3d_assemblies_contracts.py` publishes the strict Draft 2020-12
IR schema; `validate_ir()` also checks parent/material references, cycles,
quaternion normalization, form-specific parameters and aggregate budgets before
mesh allocation. A request records version/kind, ID, original description, seed,
fixture classification, materials, and parts. Each part has a stable ID, named
semantic role, local TRS transform, parent, material, form parameters and named
local attachment points. Explicit authoring supplies semantics; no automatic
segmentation or anatomical certainty is claimed.

Forms include the existing box, plane, sphere, cylinder, cone and torus;
ellipsoids; capsules; hollow straight tubes; polygonal curve sweeps; and simple
polygon extrusions. New forms use the existing editable polygon triangulator.
Capsule height includes both hemispheres. Tube outer radius must exceed inner
radius. Curve centerlines have 2..64 points, circular section radius, bounded
segments, no zero/reversing joints, and minimum segment length twice the radius.
Extrusions use a simple planar XZ polygon and Y height; holes require another
representation. No robust surface self-intersection or Boolean union is
provided. Invalid/nonplanar/self-crossing polygons fail native triangulation.

All forms preserve immutable original polygon loops separately from render
triangulation. Generate with `generate(ir)` to obtain local `EditableMesh`
objects, actual world-space native `Geometry` and world transform matrices.
Assembly defaults cap 256 parts, 100,000 vertices, 200,000 triangles and a
256 MiB conservative working estimate; per-call limits can only tighten them.
Form tessellation is bounded to 64 subdivisions, and coordinate/parameter checks
precede allocation. Failing a budget leaves the input and destination unchanged.

`revise(ir, part_id, transform=..., parent=..., material_id=..., parameters=...)`
returns a validated copy. Omitted fields and unrelated records remain byte-value
identical. A parent transform changes actual descendant geometry on evaluation.
`attach(ir, part_id, parent_id, own_anchor=..., parent_anchor=...)` reparents and
aligns those real local anchors. Attachments are geometric authoring metadata;
they do not imply IK, contacts or animation. Export a revision to a new directory
to retain the previous immutable artifact. General mesh editing, hide/duplicate,
UV editing and Blender authoring belong to their existing or subsequent owners.

## Export and reproducibility

`export_assembly(ir, destination, format='glb'|'gltf')` exports local native
meshes through the native glTF builder with actual parent-child nodes, local
TRS, materials, stable mesh IDs and attachment/semantic extras. It reimports the
written artifact and checks all part IDs, names, indices, vertex counts,
material assignments/values and evaluated world positions before publication.
The standalone glTF contains one embedded buffer and needs no network.
Every nondegenerate face must survive encoding without collapse or orientation
reversal, and per-face coordinate error must remain within 1e-3 of its own edge
span; a large part cannot hide precision loss in a small feature.

Each new directory contains `model.glb` or `model.gltf`, exact normalized
`assembly.json`, retained source polygon `editable.json`, and `acceptance.json`.
The report binds all three payloads by hash/byte count, records the normalized
request hash, generator version, Python/NumPy versions, seed, fixture flag,
actual part/world bounds, counts and validation results. Output asset extras
retain deterministic procedural provenance and fixture classification.
Repeated identical requests under the same dependencies produce identical
artifact bytes; versions are recorded rather than promising bitwise identity
across arbitrary numerical-library versions. No timestamp or random output
path enters serialized mesh/provenance bytes.

The mug has a genuine open hollow body, separate bottom and editable volumetric
handle. Components may intersect and are not Boolean-welded manufacturing
solids. Procedural characters are explicitly stylized assemblies, not skinned
rigs. Room/building/arch components remain independently editable. Local scalar
materials are assigned to actual surfaces. UVs, image textures, Blender source
export, multi-view rendering, browser review and human approval are separate
acceptance gates; they are never inferred from a successful mesh export.

## Traceability and validation

`tests/test_model3d_assemblies.py` covers all fourteen real recipes, independent
pygltflib accessors/node hierarchy/material inspection, deterministic bytes,
seed-sensitive rock geometry, hollow tube/curve/capsule/extrusion topology and
analytic bounds/volumes, explicit revisions, preallocation failures, malformed
references/graphs/language and exclusive publication races. Run it with the
optional rig test dependencies. `python -m unittest discover -s tests -p
 'test_model3d_*.py' -v` also runs the existing foundation regressions.

Engineering fixtures do not establish production-art approval. The parent
mission's Blender editability/renders, full legacy regression and hosted
cross-platform checks still need their own exact-head evidence before their
acceptance can be claimed. No network, model download or paid call is made.

## Executed evidence

The complete Model3D test selection passed 184 tests on the candidate before the
final allocation-order-only validator hardening. The final assembly selection
passed 11 tests after that change; an independent core reviewer also rechecked
its affected validation/revision cases. The executable demo produced 28 real
GLB/glTF exports, each reimported and numerically checked. All 28 additionally
passed Khronos validator 2.0.0-dev.3.10 with zero errors and zero warnings.
Three actual GLB fixtures and their exact source/receipts are retained under
`examples/model3d-assemblies/verified/`. Source module hashes and evidence scope
are recorded in `examples/model3d-assemblies/evidence.json`. Native engine,
Blender, browser, rendering, artistic approval and whole-parent acceptance were
not inferred from these pure-CPU geometry and independent file checks.
