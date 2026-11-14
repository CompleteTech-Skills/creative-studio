# Cinematic Blender execution adapter

The optional `cinematics3d_blender` adapter compiles immutable cinematic stacks
into the existing `render3d_blender.BlenderBackend`. It creates no geometry,
subprocess manager, coordinate system, timeline or image verifier. The canonical
renderer captures native Scene3D/rig sources, imports owner-sanitized GLB,
executes its fixed Blender worker and independently decodes output before
publishing. The adapter requires Blender 4.5 LTS and optional Render3D packages;
ordinary 2D workflows import neither Blender nor this adapter.

`BlenderBackend.cinematics_probe()` starts that worker with factory defaults,
autoexecution disabled and bounded time, memory, storage and logs. It queries
actual engine enums, Cycles device inventory, compositor/shader node creation,
view transforms, camera DOF properties, transparency and auxiliary pass
properties. Devices are discovered, not initialized or benchmarked; the only
executable policy is currently Cycles CPU. A property/node inventory is never
pixel evidence. Reports retain `rendered: false`, `pixel_verified: false` and
`initialized: false`; unavailable mappings have explicit reasons.

`compile_plan(registry, stack_ref, request, probe, input_bytes=...)` validates the
native RenderRequest and cinematic registry, resolves an effective scene/shot
stack, binds exact original configuration/optics bytes, and returns a new request
plus a worker plan. It inserts the optional `settings.cinematics_sha256` digest into the returned
request. This binds the whole native plan/configuration into the ordinary request
and frame-cache settings identities. Changing fog/DOF cannot reuse vanilla or
other cinematic pixels. Backends reject the digest without its matching plan.
The scheduler does not yet persist compiled cinematic bundles; use direct adapter
execution until the operations owner integrates it. The additive optional schema
field preserves existing v1 requests; ordinary requests omit it.

It does not mutate caller data or render. Current mappings
are display exposure, Standard/AgX transform, physical perspective-camera DOF
with the referenced native Camera3D optics, and world volumetric fog.
Projection gate and shift conversion use the canonical Camera3D `optical_plan`;
the fixed worker transports its computed native values. The worker
reads back mapped properties and fails on clamping. Object-local global effects,
multiple competing native controls for the same property, unsupported engines, motion blur
and other unmapped effects reject explicitly. Disabled effects remain in the
retained source configuration and produce no native operation.

The probe may list an auxiliary pass as present in Blender. This does not admit
its canonical semantics: the current renderer accepts combined output only.
Pass conversion and motion shutter/subframe evaluation remain their existing
owners' work. No preview is substituted when requested production behavior is
unavailable. GPU discovery does not authorize GPU rendering.

Use `render(registry, stack_ref, request, snapshot_root, destination,
backend=..., input_bytes=...)` to probe, compile and invoke the canonical backend.
For an already obtained probe, pass the compiler result as the optional
`cinematics=` keyword to `BlenderBackend.render_frames`. The backend recompiles retained original documents and validates the resulting
plan/request/configuration binding before creating output directories. Hashes
alone cannot make an invented native setting follow an authored effect.
Optional adapter and Camera3D implementation hashes also participate in plan
and cache identity; changed code requires recompilation. Its
normal staging, supervision, cancellation, verification and exclusive
publication continue to apply. Failed work remains unpublished diagnostic
staging. Successful outputs include exact original files under `cinematics/`,
the compiled plan, adapter hashes, actual worker settings, source snapshot,
request binding and independent PNG/EXR verification. The optional editable
`.blend` output is not implemented or claimed.

Run the pure compiler/protocol tests:

```sh
PYTHONPATH=scripts:tests python -m unittest test_rig_cinematics3d_blender -v
```

Run actual procedural geometry, PNG/EXR decoding, retained-config reopening and
measured exposure/DOF/fog pixel comparisons in a new directory:

```sh
python scripts/cinematics3d_backend_example.py NEW_DIRECTORY --blender /path/to/blender
```

The example renders 96×96 engineering fixtures. It retains source geometry,
camera/scene configuration, every output, original effect configuration and
worker report. Its numerical before/after evidence is not visual approval,
production artwork, motion-blur validation or the final three-shot film. Parent
#238 and downstream #244–#258 remain open until their complete acceptance runs.
For shared development hosts, acquire the coordination native-validation flock
for the entire example invocation.

The [current validation receipt](../examples/cinematics3d/backend-current/receipt.json)
records exact source hashes, actual tool/settings and independently decoded
outputs. Its adjacent `engineering-evidence.zip` preserves the complete native
fixture deliveries and test logs with a verified per-file inventory. The receipt
identifies the tested source commit separately from this evidence-only commit.

The [historical validation receipt](../examples/cinematics3d/backend/receipt.json) and its original archive remain unchanged. The current receipt binds all Python script hashes, exact native execution and the complete tested tree to the integrated implementation. Source-equivalent ancestry merges are recorded explicitly. Per-frame Scene3D and camera animation remain a shared-owner followup and are not silently approximated.

Before exclusive publication, the independent verifier reopens every retained
cinematic document, input, native plan and probe against its admitted byte length
and hash. The cinematic subtree must contain exactly the expected regular files
and directories. Progress callbacks run before this audit. The worker report is
parsed and hashed from one bounded capture, and its retained file and the actual
supervisor log are audited too. A final audit after evidence writes rejects
changed provenance instead of publishing a result that declares earlier bytes.
