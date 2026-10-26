# Physics v1 configuration contracts

This foundation implements [#167](https://github.com/CompleteTech-Skills/hatch-pet/issues/167)
after the [merged stage-0 audit](physics-3d-audit.md). It validates data; it does
not simulate, probe a backend, bake a cache, render, or grant visual approval.
[Backend execution #168](https://github.com/CompleteTech-Skills/hatch-pet/issues/168),
exact stepping #169, collision admission #171, and the later solver/integration
issues remain independent requirements. No backend/category is advertised as
available merely because a configuration passes validation.

## Public operations and schemas

[`scripts/physics_contracts.py`](../scripts/physics_contracts.py) exposes:

- `validate_contract(value, kind='PhysicsWorld')`: validate without mutation or
  filesystem effects, returning the original object; failures are `PhysicsError`
  with a frozen typed `Diagnostic` and serializable `report`. Generated diagnostic
  messages/paths are bounded and contain scalar Unicode; oversized malformed
  values cannot produce an invalid diagnostic receipt.
- `load_contract(path, kind='PhysicsWorld')`: bounded strict JSON input. Duplicate
  keys, nonfinite numbers, excessive depth/bytes, nonregular inputs and symlinks
  fail. Unsupported Python numeric types and integers beyond a 64-bit admission
  magnitude also fail through typed diagnostics. POSIX opens use `O_NOFOLLOW` and `O_NONBLOCK`; Windows uses the available
  flags and a preliminary symlink check. No publication or ownership guarantee is
  inferred from this read-only loader.
- `document_hash(value, kind='PhysicsWorld')`: SHA-256 of validated canonical
  JSON. This is a complete-document identity, not a selective physics cache key.
- `material_preset(name, pbr_material=None)` and `combine_materials(a, b)`:
  reusable engineering inputs and documented Bullet coefficient arithmetic.
- `schemas()` and `generate_schemas(check=False)`: standalone Draft 2020-12
  schemas and deterministic schema generation. Schema construction imports no
  NumPy, Blender, PyBullet, mesh loader or browser; runtime validation needs the
  existing `jsonschema` dependency. No dependency pin changes.

The versioned documents are `PhysicsWorld`, `PhysicsJob`, `PhysicsCache`, and
`PhysicsDiagnostic`, with corresponding [`physics-3d-*.schema.json`](../profiles/physics-3d-world.schema.json)
files. Embedded definitions are also accepted by `validate_contract` for small
adapter-boundary checks. Unknown properties, schema versions, solver option bags,
unsupported categories and unknown collision shape/joint types are rejected.
V1 has no migrations because there is no earlier physical-world contract; future
incompatible versions need an explicit migration, never silent coercion.

Run schema parity and validate the labelled configuration fixture:

```bash
python scripts/physics_contracts.py --check
PYTHONPATH=scripts python -c "from physics_contracts import load_contract; load_contract('examples/physics-3d/contracts-world.json')"
python -m unittest discover -s tests -p test_physics_contracts.py -v
```

The fixture's zero hashes and references are deliberate structural placeholders;
there are no original asset bytes, measured backend versions, simulation results,
cache files or review receipts behind them. A future admission stage must resolve
and hash actual files before execution. The fixture is not a Physics Laboratory
scene or evidence that a ball has fallen.

## Coordinates, units and clocks

The contract imports the authoritative `rig_contracts.COORDINATES`: right-handed
+X right, **+Y up**, +Z forward; meters, radians, seconds, XYZW unit quaternions,
column-vector matrices. The default gravity constant is `[0, -9.81, 0]` m/s².
Kilograms, Newtons, Newton-meters, m/s, rad/s and kg/m³ are explicit physical units.
Existing raster pixel coordinates, millisecond atlas clocks, production frame
math, audio, and rig animation schemas are unchanged.

Body/collider/domain transforms contain translation and normalized rotation only.
Scale must be baked into geometry and dimensions before physics admission;
nonuniform/animated scales cannot silently change collision or inertia semantics.
Dynamic transforms are world-owned and cannot have a transform parent. Static
hierarchies and other parent references are validated for closure and cycles.
Kinematic objects require a versioned animation reference and zero simulated
initial velocities. Physics dynamic bodies have positive mass, symmetric positive
definite inertia with physically valid principal-moment inequalities; static and
kinematic objects have zero effective solver mass/inertia. Center of mass and
collider offsets are in the body's local rigid frame.

`fps` and solver `fixed_step` are independent reduced positive rational objects
`{numerator, denominator}`. FPS has units frames/second; fixed step has units
seconds. `substeps` subdivides each configured step. Output ranges are nonempty
integer `{start, end}` **half-open** intervals. Numerically integral JSON numbers
such as `24.0` have the same integer meaning as `24`, as required by JSON Schema;
calculations coerce bounded values locally without mutating their recorded input. Events must be inside the range,
ordered strictly by `(frame, sequence)`, and have unique IDs. Simultaneous events
therefore have explicit sequence numbers. Terminal boundary `end` is not an
output frame or event timestamp. Admission calculates exact duration with
`Fraction` and conservatively counts `ceil(duration / fixed_step) * substeps`.
Actual backend effective timing and interpolation remain #169's responsibility;
these bounds are not evidence that a solver honored a requested step.

Blender conversion must reuse `rig_math.BLENDER_FROM_CANONICAL` at the adapter
boundary: vectors `(x,y,z)` map to `(x,-z,y)`, transforms by `H M H^-1`, and
inertia by `C I C^T`. Canonical gravity becomes Blender `[0,0,-9.81]`.
Quaternion basis conversion must use the same proper rotation. glTF already
shares the canonical basis and meters. No duplicate numerical conversion is
introduced here; tested adapter conversion remains an execution prerequisite.

## Reference, ownership and geometry boundaries

External resource references use the shared minimum pointer
`{id, version, path, sha256, kind}`. Versions are strict numeric SemVer triples;
paths are canonical project-root-relative POSIX/NFC names with the existing
portable Windows-name policy. Geometry, rig, animation, scene, PBR material,
texture, physics-world and physics-cache references remain separately typed.
Hashes must be 64 lowercase hexadecimal characters. Configuration validation
checks shape and reference closure; it does not certify that the referenced bytes
exist or match. File-byte verification and immutable version resolution stay with
the asset/scene owner and future physical compiler.

Body modes have one transform authority: static/static, dynamic/physics, or
kinematic/animation. Character bodies require bounded controller speed, step
height and slope settings; fracture bodies require a geometry source, at least
two distinct fragment body IDs and positive force/torque thresholds. Fragment ownership is exclusive and its
directed graph must be acyclic, including indirect cycles. These fields
do not create a controller or fracture solver. Joints reference two distinct
existing bodies with at least one dynamic endpoint, normalized axis and ordered
limits. Hinge/spring angular limits use radians, slider limits meters; motor
force is Newtons for translational axes or Newton-meters for rotational axes.
A future adapter must reject a combination it cannot represent exactly.

Collision shapes include positive-dimension boxes, spheres, capsules/cylinders
(with explicit local axis), supplied convex-hull or triangle-mesh proxy data, and
bounded compounds. Compound leaf shapes count individually against shape admission. Each collider has a physical material ID, local transform,
32-bit group/mask and explicit sensor flag. Triangle indices, repeated triangles,
degenerate triangles and nonvolumetric hulls fail. Edge directions and inertia
matrices are normalized before rank/positive-definiteness checks, so uniformly
tiny valid geometry or inertia does not underflow. Geometry conditioning uses a
dimensionless angular tolerance of 1e-12, rather than a threshold in meters.
Static triangle meshes may be a single triangle; only hulls require at least four
vertices/faces and a volumetric rank. Dynamic concave triangle meshes
require explicit decomposition. **These checks do not prove convexity,
manifoldness, outward winding, backend compatibility or that the proxy matches the
visual asset.** Actual geometry inspection and collision behavior remain #171.
Raw proxy vertices count toward aggregate resource admission, including compound
children. This preserves original source identity rather than pretending a
collision proxy is the original visual geometry.

Cloth/soft-body/hair components bind versioned geometry, mass, stiffness, damping,
pinned vertex indices and collider body IDs. Hair additionally declares a bounded
strand count. Liquid declares its maximum secondary-particle count; gas cannot
request liquid secondary particles. Particle configuration supplies
count, seed, lifetime, mass, radius and initial velocity. Gas/liquid domains carry
rigid transform, physical extent, grid resolution, density, viscosity,
temperature, combustion policy, emitter body IDs and collider body IDs. Liquid
combustion is invalid. Each used category must be explicitly required in solver
settings. Forces carry explicit target IDs, category applicability, vector,
application point, seed, strength and a bounded frame range. Unsupported parameter
combinations must still be rejected by actual backend capability admission; this
foundation never silently drops parameters.

Configured total/dynamic bodies, compound leaf shapes, physical joints/pins,
collision-mesh triangles, aggregate/deformable vertex counts, hair strands,
particles/secondary particles, separate gas/liquid and aggregate grid cells, output
frames, duration and solver/substep counts must fit declared budgets. Grid cells use the product of all
three dimensions, not an individual-axis check. RAM/VRAM, total cache/bake-file/temporary storage, CPU workers, concurrent jobs,
restart attempts and timeout budgets are bounded typed requests for later measured admission; a schema cannot predict
Blender's peak memory or enforce a subprocess timeout.

## Physical materials and actual coefficient rules

Physical materials are independent of PBR materials; an optional PBR ID must
resolve to a material resource. They retain density, separate static/dynamic
friction requests, restitution, linear/angular damping and collision margin.
`rubber`, `wood`, `metal` and `ice` presets are explicit engineering starting
values, not measured universal properties or validated solver presets. Callers
can edit them before validation and retain the final explicit values.

V1 supports only the declared `bullet-product` combination policy. Bullet's
[default source implementation](https://github.com/bulletphysics/bullet3/blob/d3afe76132fc76b0f9b2359cbaef47a3766e7fbb/src/BulletCollision/CollisionDispatch/btManifoldResult.cpp)
multiplies contact friction and clamps it to [-10,10], and multiplies restitution.
Because this contract accepts nonnegative friction, the helper's clamp is [0,10].
It applies that arithmetic separately to requested static and dynamic friction;
**it does not establish that a backend supports separate static/dynamic values**.
Blender/PyBullet adapters must verify the actual pinned build, available physical
parameters and combination behavior, and reject unrepresentable values/policies.
No minimum/maximum/average override or anisotropy option is invented. Actual
backend integration and physical contact tests remain unexecuted here. Damping
and collision margin apply per body/collider; the helper does not combine them.

## Jobs, caches, diagnostics and evidence

Jobs bind a physics-world file/hash, actual backend identity fields, exact frame
range, FPS, step and substeps, input inventory, budgets, output directory, bounded
attempts and explicit status. Clean restart is distinct from a verified
checkpoint. The latter requires a hash-bound checkpoint file; structural presence
alone cannot establish that the solver can restore it. Job configured time/step
admission is exact. Actual resource-usage fields are factual records and may exceed
a budget after a failed run; validation never changes those observations.

Caches bind world and physical-input hashes, upstream cache hashes, backend and
application/adapter/solver versions, executable digest, platform, precision,
CPU/GPU device, exact sampling/step settings, seed, numerical tolerance, inventory,
actual usage, fixture flag and independent review status. A V1 cache has one
sample per output frame and its usage byte count must equal its inventory. Cache
file existence/hash/coverage verification is deliberately separate (#184): a
well-formed manifest cannot prove replay, numerical reproduction, restoration,
recovery or visual approval. `approved` is a recorded review state, never generated
by this validator. Reproducibility classifications distinguish verified byte
replay, pinned numerical rerun, approximate rerun and nonguaranteed rerun; actual
classification evidence must come from execution.

The frozen diagnostic distinguishes invalid input, capability gap, setup error,
numerical instability, timeout, resource exhaustion, cache corruption and backend
crash. Errors preserve actionable field paths. No placeholder backend API methods
or successful solver results are supplied.

## Acceptance traceability

| #167 requirement | Implementation and verification |
| --- | --- |
| Versioned world/body/collider/material/joint/category/force/job/cache/diagnostic contracts | `definitions()`, generated schemas; world/job/cache roundtrips and schema meta-validation |
| SI, canonical basis, XYZW and authority | Reused rig coordinate constant, explicit units, rigid transforms, mass/inertia and authority negative cases |
| References, geometry, masks, time, parameters and budgets | `_common`, `_world`, `_shape`, exact Fraction admission; broken IDs, cycles, duplicate IDs, invalid triangles, masks, events and aggregate grid/count tests |
| Physical versus PBR materials, coefficient rules | PBR closure checks, reusable presets, Bullet source link, product/clamp tests; actual backend support remains separate |
| Nonfinite, bad mass/quaternions, unsafe paths | Bounded strict JSON, normalized quaternions, portable/NFC paths, tensor and mass tests |
| Structural/runtime parity and optional-dependency boundary | Same authoritative definitions, deterministic checked schemas, positive/negative schema parity, import blocker excluding all optional 3D/solver imports |
| Legacy compatibility | Existing frozen-byte and optional-import tests plus the full 2D gate recorded in the PR; no legacy schema/runtime changes |

JSON Schema and runtime use the same **structural** definitions. Graph closure,
physical inertia inequalities, event order, aggregate admission and other
cross-field semantics are executable runtime checks, documented above; a generic
JSON Schema validator alone is insufficient for execution admission. Tests exercise
both shared structural parity and the additional semantic boundary.


## Retained execution evidence

The [machine-readable validation record](../tests/results/physics-contracts-20261024/validation.json)
binds exact source commit/tree, an unchanged before/after checkout fingerprint,
actual interpreter/dependency versions, commands, timings and raw log SHA-256.
The pre-integration corrected source `e39f9b4` has **22 passing contract tests and four passing legacy
compatibility checks** on Python 3.12.15; all 817 2D cases are discoverable with
optional 3D/solver imports blocked. Legacy/generated physics schema parity,
rig-template parity and actual original production-source delivery verification
also pass.

The separate broad baseline ran 811 tests on frozen original candidate
`68b9b87b8ad3a0635027be8ce5db3dfbd6e20a9b` using Python 3.14.7, Pillow 13.0.0 and
actual Chromium 152.0.7977.82: 810 passed, zero failures/errors, one unavailable
native-Krita skip, 839.990 seconds. That baseline predates the independent-review
corrections and **is not a full-suite result for the corrected source**. Final
exact-head cross-platform CI remains a separate merge gate. No solver, cache
replay, production visual approval or human signoff is established by these logs.


## Shared decoder prerequisite integration

The physics branch normally merges the independently reviewed shared decoder
`cf15e97027ab48d028de6dfd8dc8f2440f2cf2d8` from
[PR #205](https://github.com/CompleteTech-Skills/hatch-pet/pull/205). That prerequisite
owns oversized-integer and UTF-8 byte-budget translation. The domain loader now
translates only structured `RigError`; unrelated programming `ValueError` remains
visible. No duplicate shared decoder implementation is introduced.

The [integration validation record](../tests/results/physics-contracts-20261024/decoder-integration/verification.json)
binds exact integrated source `387a41367c44e46aecc64abc4255f0886bdfacef`, shared helper
commit, identical before/after checkout fingerprints and actual logs. All 22
physics tests and four compatibility checks pass on Python 3.12.15; all 824 2D cases
are discoverable with optional imports blocked. Physics/legacy schema and template
parity, original-source verification and 14 actual API/file probes pass. Those
probes cover signed 5000-digit integers, mixed-width UTF-8 at exact/over byte caps,
malformed encodings/duplicate keys/nonfinite input, unchanged source bytes and
propagation of unrelated programming errors. They do not change the interpreter's
integer digit limit.

The earlier 811-case run remains bound only to 68b9b87. No new full-suite or solver
execution is inferred from this integration. Fresh whole-tree independent review
and exact-head GitHub CI remain required before merge; PR #205 is an explicit
predecessor.


## Exact-byte preservation under CRLF checkout

The normal merge of reviewed shared candidate
`91d14b8339eb16a6285969d9bc772b11bdfec3ec` supplies the common recursive validation-
receipt/example byte policy and source LF rules. No duplicate physics-only
attributes or runtime changes are introduced. This corrects the confirmed
conversion of a hash-bound nested log under `core.autocrlf=true`.

The [preservation validation record](../tests/results/physics-contracts-20261024/provenance-integration/verification.json)
binds source `4b7deeea1473c08a6382018fd1fefb3a11c5cc55`, unchanged before/after
checkout fingerprints and actual commands/log hashes. A real Git checkout with
`core.autocrlf=true` and `core.eol=crlf` checked 1645 tracked files, verified exact
Git blobs for 1524 explicitly byte/LF-protected files, and preserved all 17 existing
physics evidence artifacts. Every referenced physics raw-log digest and current
artifact-source digest matched. The formerly changed log now retains its original
SHA-256 `66d868d02ad7723fdcac74f8b6d9599962d5d9a56402048a28f0fda9d7eeec91`.

The compact [CRLF summary](../tests/results/physics-contracts-20261024/provenance-integration/crlf-summary.json)
retains all relevant receipt/source checks and hashes the full per-file inventory
held in the supervisor checkpoint. The accompanying `crlf-probe.py` reproduces
that inventory against the pinned source checkout using `--repository` and
`--destination`. This tests actual Git newline conversion on Linux; it does not
claim native Windows execution.

All 22 physics tests, four compatibility checks, schema/template parity and 14 actual
byte-boundary API probes pass after integration. No 811-case legacy rerun is claimed.
Earlier 7cff push/PR CI runs remain attributed to 7cff; final-head independent review
and new exact-head CI remain separate merge gates.
