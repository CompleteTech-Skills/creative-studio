# Scene frame evaluation

`scene3d_evaluate.evaluate_scene(project, scene, shot, frame)` evaluates one
integer or `fractions.Fraction` frame from a verified project directory. Scene
and shot selectors accept immutable document paths or unique IDs. A preloaded
bundle can also be passed; prefab expansion and native animation sampling then
require `source_root` pointing to the exact project dependency closure.

Frames occupy the half-open interval `[0, duration_frames)`. Clip bindings use
their owner's rational frame rate, start frame, speed and explicit loop flag;
clips retain their own frame rates. Terminal keys guide interpolation but are
not additional output frames. Native sampling converts exact time to floating
point only at the rig owner's numerical boundary.

The evaluator copies authored data before applying node/shot animation, temporal
constraints, material overrides and camera overrides. The canonical SceneGraph
then evaluates transforms, attachments, visibility and bounds. Shot tracks have
final precedence over clip tracks. Overlapping native clips on one instance are
rejected because the contract has no declared native blending policy. Evaluation
does not mutate source documents or shared character assets.

`sample_geometry=True` invokes the rig animation/deformation owner and, for
imported assets, the importer's component transform mapping. This supports two
instances of the same immutable imported rig with independent clips and world
placement. Rig and clip identity, version, path and source hash must match the
actual native manifest. Outputs contain deformed world-space vertices and retain
the exact native binding. Without sampling, `native_animation_evaluated` remains
false: a retained binding alone is not evidence of evaluated deformation.

`camera_matrices(evaluation, render)` delegates Scene3D optical conversion and
projection to Camera3D. Perspective vertical FOV and orthographic height are
preserved. Scene3D has no physical sensor/DOF intent; its explicit mapping uses
a 24 mm vertical gate and disabled DOF. Nonrigid inherited camera transforms are
rejected. `resolve_materials(evaluation, registry)` delegates inherited material
resolution to LookDev and requires exact versioned source pins; it does not
generate replacement materials or grant visual approval.

The result is rendering input, not a render receipt. Blender and browser light,
shadow, atmosphere and material parity belong to their renderer/LookDev adapters.
World and native light fields are evaluated and preserved here, but this module
does not claim unsupported atmosphere effects or physical equivalence between
engines. Full scene/environment acceptance still requires the renderer and
four-environment lifecycle gates under Scene3D issues #65 and #66.

Validation runs with `PYTHONPATH=scripts:tests python -m unittest
test_rig_scene3d_evaluate -v`, with the pinned Khronos validator installed under
`tools/rig` (or `HATCH_GLTF_VALIDATOR_ROOT` selecting that exact module). Tests
exercise real GLB export/import and native deformation, rational timing, clips
at different frame rates, active constraint intervals, independent shared assets,
exact source rejection, budgets, material inheritance, and both camera models.
The interoperability release gate and Scene3D hosted workflow include the suite.

## Explicit lighting and path bindings

A light node may carry `light_binding` with `schema_version: 1`, an exact
canonical `light3d` reference under `light`, `intensity_scale` in [0, 1000000],
and a linear RGB `color_multiplier` in [0,1]. This multiplies the physical source
light; it never interprets the legacy unitless `light.intensity` as physical power.
The lighting owner retains photometric tint semantics: candela/lux colors are
normalized to unit luminance after tint (black stays dark); use intensity_scale
for brightness changes. Area watt sources retain direct RGB multiplication.
`light_intensity_scale` and `light_color_multiplier` tracks animate those explicit
multipliers. Optional shot `light_overrides` replace a target's complete binding
as the final shot override. The world binding instead carries `light`,
`intensity_scale`, and `exposure_stops` in [-32,32], and requires an environment
light source. A simultaneous legacy environment authority is rejected.
`light_instances(evaluation, registry)` resolves exact source references and
returns hashed instance intent. The canonical LookDev lighting adapter must build
physical light plans; legacy renderers must reject these bindings until connected.

Optional shot `path_bindings` carry schema_version, id, target, points,
start_frame, end_frame, start_fraction, end_fraction, space='world', and
orientation='preserve'. The interval is half open and bounded by shot duration.
Exact rational progress selects arc-length interpolation through the spatial
owner. Paths override translation channels before ordinary scene constraints;
parent path bindings precede their children. Parent transforms and node pivots
are accounted for. Rotation remains authored; aim constraints can orient cameras.
Zero-length paths, missing targets, duplicate targets, unsupported spaces and
orientation modes fail explicitly. All path points share a 250000-point budget.

`frame_camera(evaluation, render, target_ids, **options)` delegates framing to
Camera3D `frame_subject` using the exact evaluated world bounds and node positions.
It returns camera/framing output without changing authored documents. Evaluated
`state.channels` retains local input translation/quaternion/scale (and optional
pivot), after channel/path evaluation and before ordinary graph constraints; curve
editors can consume these values without decomposing matrices or resampling tracks.

## Immutable native poses

Optional node `pose_binding` has `schema_version:1`, an exact `rig` reference of
kind `hatch-3d-rig`, and `pose:{id,rig_version,file:{path,sha256}}`. The pose pin's
`rig_version` identifies the canonical skeleton version, independently of the rig
manifest version. Pose JSON must satisfy the rig owner's `Pose` contract. Its
file is a real project dependency, copied by snapshots and checked on reopening.

Bindings on group roots propagate only to descendant assets whose exact
`asset.skeleton` equals the rig pin. Multiple pose bindings or an active native
clip on the same affected component are rejected because no blend policy exists.
The evaluator checks exact rig/pose bytes, IDs and versions, then delegates pose
constraints to `rig_operations.pose_operation`. It never changes the rest pose,
asset, other instances or authored documents. Mesh-qualified morph names and
bone references must exist in the admitted native rig.

With a binding, evaluation needs `source_root` even without geometry sampling.
Each affected component exposes `native_pose` (the exact binding),
`native_pose_evaluated:true`, and `native_state` containing `pose`, `skeleton`,
`bone_matrices`, `bone_matrix_space:'rig-bind'`, `world_from_rig`, and
`morph_targets:{mesh_id:[names]}`. Bone matrices remain in canonical rig space;
`world_from_rig` explicitly maps the instance or imported component into scene
world space. Authored roots expose `native_pose_components` identifying affected
components. `sample_geometry=True` returns actual deformed world vertices and
updated component/ancestor bounds through the existing importer and rig owners.
Exposed bone/morph state is capped at 250000 total entries across components;
posed and animated vertices share the evaluator's output vertex budget.
Authored rig attachment sockets additionally appear as `socket_matrices` with
`socket_matrix_space:'rig-bind'`, evaluated by `rig_attachments.socket_world` for
the same canonical pose. Consumers obtain scene matrices by multiplying
`world_from_rig @ socket_matrices[socket_id]`. Only manifest-declared sockets are
returned; rigs without sockets expose an empty mapping. Socket entries share the
existing 250000-entry native state budget.
Each exact rig pin is loaded once per evaluation and shared across its distinct
poses. Every instance still revalidates its rig/Pose source and identity pins.
Distinct retained native rigs share a fixed 2000000-input-vertex budget even when
geometry sampling is disabled. Distinct pose deformation cache entries share a
separate fixed 2000000-vertex budget, charged before native deformation allocates
arrays. These limits are independent of the selected component output budget.

Consumers must use this state or reject pose-bound scenes; a binding is not
permission to render the rig's rest pose. Before any native pose decoder runs,
`scene3d_capture.capture_verified_files` admits the trusted bundle inventory into
an owned private project tree. Rig, Pose, import report and hierarchy reads stay
inside that context until native evaluation finishes. No sampler scans mutable
sources to manufacture a trusted inventory. Renderer/browser consumption remains
an integration gate owned by the lifecycle adapter.
The Pose suite exercises real two-instance bone/morph deformation, imported
components, snapshot/reopen equivalence, exact-source rejection, active-clip
conflicts and vertex budgets. These tests are included in the hosted native
Scene3D workflow and interoperability gate.

Native clip sampling retains one loaded rig per complete immutable rig reference.
Distinct evaluated clip/time results share those source geometry arrays. Separate
aggregate caps of 2000000 vertices bound loaded rig inputs and retained deformation
arrays; deformation capacity is checked before the native evaluator allocates the
next sample. These input/cache caps are independent of the selected output vertex
budget, so selecting a small imported component cannot hide large retained rigs.
