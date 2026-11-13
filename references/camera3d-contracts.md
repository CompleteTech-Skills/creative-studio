# Camera3D contract foundation

This additive foundation implements issue #75. The camera system's rendering,
interactive authoring, motion, tracking, and production demonstration requirements
remain in #76–#82. A structurally admitted path or constraint is authoring intent;
it is not evidence that a path solver, collision solver, renderer or editor ran.
No visual or human production approval is created by these contracts.

## Inspected baseline and owners

The starting checkout is `a08569c` (PR #199's merged bundle publication fix).
Existing production schema v1 remains unchanged. `production_contracts.py` owns
shot/scene/project validation; shot cameras contain pixel-space `position` and
scalar `zoom` tracks and `bounds` (`max_pan_per_frame`, `min_zoom`, `max_zoom`).
`production_performance.evaluate_track` evaluates bounded integer frames with
step or linear interpolation, including the authored terminal key. Pixel X is
right, Y is down, and 2D rotation is clockwise in degrees. These are not meters
or 3D camera transforms.

`production_render.render_shot` evaluates the existing production state, creates
PNG/PCM masters and binds camera/evaluator code to dependency hashes. Scene
rendering assembles contiguous shot caches at exact integer cuts. The existing
canvas preview renders those outputs and supports seeking, source inspection,
track visibility and pinned comparisons. Existing WAV/audio and review semantics
remain intact. `generate_schemas.py` remains the owner of existing generated
schemas; their bytes are protected and checked against the starting commit.

The 3D rig subsystem already owns right-handed Y-up meters, XYZW quaternions,
column-vector TRS and Blender basis conversion in `rig_math.py`. The scene #59
workstream owns scene/node/shot documents; animation #67 owns tracks and exact
million-tick subframes. These camera contracts reference those authorities; they
do not replace the scene graph, animation evaluator, asset registry, or render
scheduler. `camera3d_math` imports NumPy and rig mathematics lazily, so normal
2D discovery and the camera definition module need no optional 3D dependencies.

## Versioned contract families

Sixteen closed root documents have integer `schema_version: 1`, explicit
`kind: camera3d-*`, stable camera-local ID and exact semantic version:

| Kind | Responsibility |
| --- | --- |
| camera | Unit-scale local TRS, optional scene parent, optics, constraints and animation references |
| optics | Discriminated pinhole or orthographic projection and DOF intent |
| transform | Native rig-shaped local transform with scene/node parent binding |
| animation | Exact duration/rate, native animation or camera path reference, optional terminal pose |
| constraint | Targets, priority, weight, bounds, up direction, collision radius and failure policy |
| target | Immutable scene reference, node ID, optional bone/socket, offset and coordinate space |
| path | Linear/Bezier/Catmull–Rom control points, timing, orientation and speed policy |
| rig | Parent-acyclic named camera rig nodes and local offsets |
| preset | Exact source camera and bounded typed non-destructive transform/optics overrides |
| binding | Scene/camera/rig/animation/target/constraint/render resources for a shot |
| shot | Explicit 2D wrapper or native 3D scene/binding, exact timeline interval and transition |
| transition | Cut or positive-duration dissolve/fade/wipe authoring intent |
| storyboard | Ordered exact shot references, frame rate and total duration |
| evaluated | Exact sample identity, world/view/projection matrices, resolved optical parameters and dependencies |
| render | Raster resolution, pixel aspect, gate fit, overscan, encoding and pixel budget |
| registry | Bounded list of exact resource-file references |

Definitions live in `camera3d_contracts.py`. Generated schemas contain only the
transitively required definitions. Runtime semantic validation additionally checks
quaternion normalization, reduced rationals, clipping order, graph cycles and
references, conditional target/path fields, terminal boundaries, matrix inverses,
pixel budgets and transition consistency. JSON Schema is structural admission;
it does not express every semantic invariant. Both layers are tested.

References have `{id, version, path, sha256, kind}`. The hash is the actual file's
SHA-256, not a reserialized object's semantic hash. Native IDs and kind tokens,
including animation's underscore-separated kinds, are preserved. Camera-local
IDs use the narrower documented lowercase identifier form. Canonical resource
serialization uses sorted keys, compact separators, finite JSON numbers and one
trailing newline. Unrecognized fields/versions and duplicate JSON keys fail.
Unknown fields are never discarded; future changes require a deliberate schema
version or migration adapter. There is no silent migration or `latest` binding.

`load_registry(root, registry)` verifies bounded native camera JSON resources and
exact references. External native resources receive byte/hash validation; their
semantic owner must validate them before actual evaluation. A wrapped v1 shot is
structurally delegated to the unchanged `production_contracts` validator.
Registry loading does not claim to validate an external Blender/model/scene
payload semantically. It is a read-only snapshot, not a transactional content
store or a defense against arbitrary concurrent ancestor replacement. Symlink
resources, traversal, absolute paths, reserved Windows names, Unicode/case aliases,
missing resources, changed bytes and incomplete reference closure fail. Each
resource is bounded to 8 MiB and the closure to 256 MiB/4096 files.

## Coordinate and projection reference

Canonical world axes are right-handed +X right, +Y up, +Z actor forward. Camera
local axes are +X right, +Y up, **-Z viewing forward**. Persisted camera TRS uses
`{translation, rotation, scale}`, XYZW normalized quaternions, meters and unit
scale. Parent hierarchy uses `world = parent_world @ local`; nonrigid or reflected
camera parent transforms are rejected by the reference matrix helper. Scene
parents are `{scene: Ref, node: ID}`; a scene node is not misrepresented as an
independently versioned resource file. Target offsets name their space explicitly.

For camera world matrix `W`, `view = inverse(W)` and `clip = P @ view @ point`.
Projection divides XYZ by W only when W is nonzero. NDC uses OpenGL/glTF depth
[-1,+1], with near at -1 and far at +1. Raster positions are pixel-edge coordinates
from the top-left; NDC +Y maps to decreasing raster Y. `screen_ray` returns a
near-plane origin and unit direction. Points behind the camera remain reportable
but cannot be `inside` the viewing frustum. This foundation supplies reference
projection/unprojection; it does not perform rendered visibility or occlusion.

The pinhole lens is authoritative in millimeters. Horizontal fit preserves sensor
width and derives height from raster/pixel aspect; vertical fit preserves height.
Auto fit crops or expands the physical gate according to `gate_fit`. Orthographic
height is authoritative in meters. Overscan expands the viewing frustum around
the nominal frame. Lens shift is a normalized principal-point offset in fractions
of full image width/height (+X right,+Y up); an eventual Blender optical adapter
must explicitly convert that sign/scale rather than blindly copying shift fields.
DOF is stored as intent, not simulated by pinhole math.

A 50 mm lens and 36 mm horizontal gate has a 39.597752709° horizontal FOV.
Independent reference tests check that value, near/far depths, correct handedness,
raster Y inversion, parallax, orthographic depth independence, sensor fit, principal
point shifts, pixel aspect, overscan and inverse projection.

`basis_transform` conjugates object/world-basis transforms using the existing rig
basis. **Camera transforms require a different adapter**: their local -Z/+Y axes
already match Blender's camera-local axes, so `blender_camera_transform(W)` is
`BLENDER_FROM_CANONICAL @ W`, without right-hand conjugation. The inverse adapter
roundtrips this exactly. glTF's Y-up basis is identity. These numerical adapters are
not evidence of a real Blender or browser camera export/render.

## Timing and editorial boundaries

Evaluated `time` is **shot-local**, not a global editorial frame. Global presentation time in exact million ticks is `shot.start_frame * 1000000 + time.frame * 1000000 + time.subframe_ticks`. Two 24-frame shots starting at 0 and 22 overlap for global frames 22 and 23: global frame 22 maps simultaneously to local frame 22 of the first shot and local frame 0 of the second. At global 24 the first shot is at its labelled terminal boundary while the second is at local 2. A transition renderer combines those states later; it must not emit two events or reinterpret local time as global time.

Frame rates are reduced positive rational `{numerator, denominator}` records,
within the shared animation policy 0.001–1000 fps. Subframes use one million
integer ticks per frame. Display samples are half-open `[0, duration_frames)`;
exact `duration_frames` is an explicitly labelled terminal boundary, never the
last displayed frame. The terminal pose may guide interpolation without adding
another output frame. Tests distinguish frame N-1, frame N, and N+one tick.

Native animation tracks are referenced, not duplicated. Camera path resources
store the required interpolation/speed/orientation policy but no implementation
of path evaluation is claimed here. A v2-equivalent additive camera shot has
`render_mode`; v1 shot JSON remains unchanged and needs no discriminator. A 2D
wrapper points to the unchanged v1 shot, while 3D shots bind scene and camera
resources. Editorial cuts have zero overlap. Non-cut transition intent requires
positive tail/head overlap; storyboard admission subtracts overlap from sequence
duration and verifies adjacent-shot bounds. Rendering actual transition pixels
and mixed sequences remains #79/#80.

## Reproduction and evidence

With the optional rig test environment installed:

```bash
python scripts/camera3d_contracts.py --generate
python scripts/camera3d_contracts.py --check
python scripts/camera3d_contracts.py --validate examples/camera3d/main-camera.json
python scripts/camera3d_contracts.py --validate examples/camera3d/registry.json
python scripts/test_rig_gate.py schema-math
python -m unittest discover -s tests -p test_rig_camera3d_contracts.py -v
```

The standalone generator temporarily owns only the new camera schemas while the
shared generator changes in #59 are integrated. Its exact parity test is part of
the existing schema/math CI gate. No v1 generator/schema is modified.

The retained validation record distinguishes an initial local legacy run missing
Playwright from the subsequent actual Chromium preview execution. Local math and
fixture tests are engineering validation, not artistic review, actual camera
animation, renderer parity, production signoff, or full parent #14 completion.

Validation results and exact source hashes are recorded in [latest integration verification](../tests/results/camera3d-contracts/main-model3d-integration/verification.json). The original e202ca4 execution is preserved separately from the corrected candidate, including its original logs and source snapshot. Discovery of the 809 legacy tests with optional 3D imports blocked is not full-suite execution.

The spatial reference layer also delegates vector/quaternion operations to `rig_math`, exposes explicitly fixed-axis/extrinsic XYZ Euler angles (`Rz @ Ry @ Rx`), world/camera point conversion, world or rotated target offsets, forward ray/plane intersection, normalized frustum planes/corners/containment, and exact convex AABB/frustum intersection projection. Bounding-box projection enumerates at most 220 plane triples and handles near-plane crossings and camera-enclosing boxes; it does not establish rendered occlusion visibility. Native bone/socket identifiers retain their original punctuation/case. Schema-valid integral JSON numbers such as `24.0` are accepted for integer fields by normalizing only arithmetic operands, leaving authentic source bytes and hashes unchanged. Camera-owned path/animation/shot timing and bound scene/rig relationships are checked; retiming is never implicit.

At the earlier `5d8bc535` camera candidate, **53 schema/math/compatibility tests
(including 22 camera tests and current shared-decoder/compatibility regressions), 51 legacy contract/render tests, and 4 actual
Chromium preview tests**, with zero final failures/errors/skips. The original
35-test schema/math execution is preserved at its authentic e202ca4 revision.

A bound rig must mount the selected camera exactly once, preventing ambiguous camera world transforms. File reads admit regular binary descriptors only, read at most 8 MiB, and use nonblocking/no-follow flags where available. The actual FIFO CLI regression terminates with a structured rejection on platforms with FIFOs; Windows has no `os.mkfifo`, so that subcase is inapplicable there.

The earlier camera integration used verified main `b16a2268e8cb5a3a29042058a95a7b2495903716`, including the final shared JSON diagnostic/UTF-8 byte-cap repair `cf15e97027ab48d028de6dfd8dc8f2440f2cf2d8` and repository provenance attributes. A normal merge reuses those authoritative changes. Fresh integrated validation reran camera/schema, legacy rendering and actual Chromium gates with every recorded source hash frozen; positive/negative 5000-digit camera CLI inputs reject with structured status 2 and empty stderr. No interpreter integer limit changes or texture dependency are introduced. Earlier exact execution records remain authenticated in Git history.

## Current Model3D main integration

Verified main `dcba9123c4b70fc2caa7b56267920f239fb35c12` was merged normally, retaining its Model3D schema generator entry, optional-dependency gate exclusions and operations tests alongside the camera gate. Camera runtime, generated-schema, test and example bytes remain identical to the independently passed `5d8bc535` candidate. Fresh frozen validation passed 53 schema/math tests and 54 Model3D integration tests, both generators, exact camera registry closure, actual robotic geometry inspection, source-delivery verification and two actual camera CLI rejection probes. All 115 recorded source entries remained unchanged; all 14 protected inputs match current main. Thirteen also match the original a08569c baseline; the shared generator alone carries the explicitly inherited Model3D hook from PR #202. The earlier 51 renderer/contract and 4 actual Chromium results remain historical, not claimed as freshly rerun. Scratch files used the home cache via TMPDIR. No texture dependency is introduced.
