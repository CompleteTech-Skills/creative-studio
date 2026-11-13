# Static Scene3D frame export

`scene3d_export.export_scene_frame(source_root, scene, shot, frame, destination)`
exports the selected immutable persisted scene/shot at one integer or rational
frame. `destination` is a new directory under an existing trusted parent. The
published directory contains `scene.glb` and `report.json`. Source bytes remain
unchanged, and validation failure leaves no published directory.

The exporter delegates frame evaluation to `scene3d_evaluate`, imported component
transforms and deformation to `scene3d_import`, native topology/materials/textures
to the rig importer, and GLB serialization to `rig_gltf.GltfBuilder`. It preserves
semantic scene hierarchy, affine local transforms, mesh identities, visible
geometry, source material textures, and perspective/orthographic cameras.

A selected frame freezes native skin deformation, scene animation, constraints,
and expanded prefabs. It does not export editable skeletal animations, procedural
recipes, prefab links or live constraints. Normals are regenerated for the frozen
surface; tangents are omitted. Native world/background/exposure and unitless
lights are recorded as provenance extras rather than reinterpreted as portable
glTF lighting. Scalar material overrides reuse exact LookDev captured closures, canonical
primitive slot mappings, and the native `material_plan`/`gltf_material` projection.
Texture/graph override materials still require their dedicated embedding adapter
and fail explicitly rather than becoming flat approximations. These losses are retained in both the GLB metadata and JSON report.

Each export runs the independent Khronos glTF validator, then reimports the GLB
into an isolated native model and compares every exported mesh identity, vertex
count, exact triangle topology, source material/image bytes, and world-space vertex to the evaluated frame (maximum absolute error
1e-4 meters). The native reimport is accurately labeled non-independent; the
Khronos structural report is a separate independent validation. Receipts include
exact output byte count/hash and transitive input dependency hashes. Neither
static export nor these checks constitute art/production approval.

Run `test_rig_scene3d_export` with the pinned validator installed and configured
via `HATCH_GLTF_VALIDATOR_ROOT`. Tests exercise real GLB output, validator results,
hierarchy/camera preservation, native reimport positions, empty visibility,
half-open exact frame rejection, exclusive output ownership and source immutability.
