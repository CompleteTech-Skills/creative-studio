# Camera target constraints

`camera3d_constraints` solves camera constraints in canonical meters and +Y-up
coordinates. Camera optical forward is local -Z. It does not modify the legacy
2D production camera or add another scene/rig evaluator.

`SceneTargets.from_scene` evaluates static native Scene3D transforms and real
local bounding boxes. `SceneTargets.from_graph` uses the owning `SceneGraph`
base evaluator. For animated scenes, supply the owning evaluator's complete
node state to `SceneTargets` directly. The immutable scene bytes must match the
scene document, and each target carries the exact scene reference. Missing
objects, bones, sockets, bounds and rig bindings fail explicitly. Bone and
socket points use native rig FK and attachment evaluation.

`look_at_world` uses a deterministic least-aligned-axis fallback at an up-vector
pole. `frame_subject` fits all corners of actual world bounds inside the safe
rectangle and near/far clipping interval. Perspective fitting dollies within
the requested distance interval; orthographic fitting changes true scale.
Weighted group centers and composition anchors are supported. It uses 56
bounded search iterations and verifies the resulting geometry independently.
`calculate_focus_distance` uses camera-axis depth, with weighted targets and
explicit distance limits. `rack_focus` supports linear and smoothstep curves.
`track_subject` supports stateless offset, height, position and rigid rotation
following; position following also accepts scaled scene subjects.

`evaluate_camera_constraints` consumes existing camera constraint documents,
sorts them by priority and ID, resolves each exact target once, and returns
world/view/projection matrices, optics, diagnostics and a canonical SHA-256.
Zero weights disable constraints. Framing and focus are checked again after
later changes. Warning policies retain unsatisfied diagnostics, including a
focus target moved behind the camera by a later constraint.

`resolve_camera_collision` consumes the Scene3D owner's continuous
`sweep_sphere(start, end, radius)` query. A hit contains a fraction in [0,1], a
unit outward normal and an optional node ID; initial overlap must be reported
at fraction zero. Stop, slide, nearby, adjust-distance, reject and warning
policies have bounded work. Mandatory bounds cannot be downgraded to warnings.
`preflight_camera_path` checks declared linear segments continuously; it does
not claim clearance for a curved interpolant between those segments.

## Validation and remaining integration

Run `PYTHONPATH=scripts:tests python -m unittest
test_rig_camera3d_constraints test_rig_camera3d_contracts -v`. The constraints
module is also included in the schema-math release gate. Tests cover real
Scene3D hierarchy/bounds and native rig bone/socket FK, numerical framing and
focus, deterministic poles/order, missing references, later constraint
conflicts, and collision response using an analytical plane oracle.

The analytical collision oracle validates response policy only. The explicit native integration gate
`HATCH_CAMERA_NATIVE_SPATIAL=1` exercises the owning Scene3D #62
`SpatialIndex.sweep_sphere` against actual triangle geometry, including stop,
slide, nearby, adjust-distance, path clearance and initial overlap. Run with
the owning spatial modules available on `PYTHONPATH`; missing modules fail
when the gate is requested. Cross-owner acceptance ran against spatial
checkpoint `a3cc379`. Blender rendering and animated scene integration remain
acceptance work; this checkpoint does not claim the full Camera3D mission is complete.
