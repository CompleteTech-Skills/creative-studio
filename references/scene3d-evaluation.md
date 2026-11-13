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
