# Trusted physics backend lifecycle

This implementation addresses [#168](https://github.com/CompleteTech-Skills/hatch-pet/issues/168).
It depends on the strict physics contracts (#167), native Scene3D admission (#59),
shared bounded JSON decoder (#205), and exclusive publication primitive (#198).
It does not complete the physics engine, rigid-body acceptance #170, or the
later constraint, deformable, fluid, coupling, recovery, animation or editor work.

## What executes

`scripts/physics_backends.py` provides real Blender Bullet and optional PyBullet
DIRECT adapters. Both admit a bounded physical world and a complete immutable
mapping of native project paths to exact bytes. `scene3d_contracts.load_project`
validates the copied native document/dependency closure. The adapter maps the
physics reference category `scene` to the native `scene3d-scene` kind explicitly;
it verifies identity, version and hash without rewriting source bytes.

Each physical body must bind an actual native mesh node and asset. The existing
rig importer independently decodes its OBJ. Counts, bounds, closed convex surface,
source identity, initial world transform and the declared sphere/box proxy are
checked. Unsupported materials, external OBJ resources, skins, clips, and LODs
are rejected. A hash-valid opaque JSON file is not accepted as a native scene.
The backend consumes real geometry rather than an uninspected filename.

The engineering input builder `scripts/physics_backend_example.py` creates genuine
native source manifests and OBJ meshes. Its sphere is an explicitly coarse
octahedral visual surface; collision uses the declared analytic sphere. The box
contains all eight corners and twelve actual surface triangles. Motion is not
stored in those sources: the numerical backend computes it.

## Installation and executed versions

The host uses Python3.12 and the existing optional3D pins:

```bash
python -m pip install -r requirements-rig-test.txt
```

Blender4.5.3 LTS, build `67807e1800cc`, is the admitted Blender build. The existing
rig workflow's Linux archive is retained: SHA-256
`975c58fcb244273838534bba771e64ad87739216b0f9b39a888531a49a72d845`.
Set `HATCH_BLENDER` to that installed executable. A different version/build is
an explicit unsupported result, not an automatic substitute.

PyBullet is optional. The actual local optional worker uses CPython3.11.17,
PyBullet3.2.7 and NumPy2.3.5 in a separate environment. A CPython3.12 binary-only
installation was unavailable; this does not imply the optional backend is absent
from every possible interpreter. Select the actual isolated worker explicitly:

```bash
python3.11 -m venv output/optional-pybullet
output/optional-pybullet/bin/python -m pip install --only-binary=:all: -r requirements-physics-pybullet.txt
export HATCH_PYBULLET_PYTHON="$PWD/output/optional-pybullet/bin/python"
```

No optional backend is imported when ordinary2D code starts. Importing
`physics_backends` itself imports no NumPy, Pillow, Blender, PyBullet, physics
contracts, or Scene3D implementation. A fresh empty Python environment tests
this actual import boundary, rather than mocking dependency availability.

## Discovery and readiness are separate

```bash
python scripts/physics_backends.py probe
python scripts/physics_backends.py probe --smoke
python scripts/physics_backends.py probe --backend pybullet --python "$HATCH_PYBULLET_PYTHON" --smoke
```

Without `--smoke`, discovery establishes the executable/module version only;
the implemented category remains `experimental`, not ready. `--smoke` performs
actual native scene admission, simulation, cache creation, verification and
independent inspection. It executes both dynamic-sphere/static-box and
dynamic-box/static-sphere profiles. The Blender path also reopens its own generated
`.blend` in a separate process and runs a cache-consumption control. A failed or
unavailable requested smoke exits2. It cannot return ready static geometry.

Capability states have distinct meanings:

| State | Meaning |
|---|---|
| `available` | This probe completed the real bounded category smoke. |
| `unavailable` | A required executable, module or native input owner is missing. |
| `unsupported` | Installed version/platform or requested feature is outside the admitted profile. |
| `experimental` | Implementation/discovery exists; this probe did not execute readiness. |
| `degraded` | A temporary resource condition, such as the occupied worker slot, prevents admission. |
| `failed-validation` | Attempted execution/output verification failed. |

All other physics categories report `unsupported`; joints, forces, impulses,
initial velocities, character authority, sensors, filtered collision groups,
compound colliders and external geometry formats are not silently ignored.
The supported profile is intentionally narrower than the world schema.

## Concrete limits and physical mappings

- One private globally locked physics process at a time, at most32 bodies.
- POSIX execution/cache admission; Linuxx86-64 is the actually executed platform.
  Windows native execution is not advertised. Its unsupported result is explicit;
  the existing2D and rig contracts remain unchanged.
- Canonical right-handed Y-up meters, XYZW quaternions and rational fps. Blender's
  proper basis rotation maps `(x,y,z)` to `(x,-z,y)` once; object transforms are
  conjugated, and read-back uses the inverse conversion. PyBullet uses canonical
  coordinates directly with explicitly configured gravity.
- Output frames are `[0,N)`, with `2 <= N <= 1024`. Blender frames1..N correspond
  to output frames0..N-1. A terminal frameN is not a cached output frame.
- The frame interval must contain an exact integral1..1000 fixed steps at1..240fps.
  Native effective substeps and solver iterations are read back and compared.
  This bounded profile does not replace #169's complete timing/stability work.
- Primitive dimensions .001..1000m; positions within10000m; dynamic mass
  .001..1,000,000kg. Bodies have no transform parent, shifted center of mass, or
  initial velocity. Inertia must match the uniform analytic primitive; unsupported
  custom tensors are rejected instead of being discarded.
- One collider per body with identity local transform, no sensor, group1 and
  mask`0xffffffff`. Both declared friction coefficients must agree and be<=1;
  Blender's single coefficient is not misrepresented as separate static/dynamic
  friction support. Actual native mass, shape, damping, restitution and margin
  observations are checked against admitted values.
- Nonzero additional contact skin is unsupported for both shapes; requested
  extra margin must be0. Analytic sphere margins are inherent in their radius,
  which PyBullet reports separately. An actual box control stored0.05m in the
  native property yet retained the original resting height on both backends.
  Property readback is therefore not misrepresented as extra physical separation.
- Explicit body mass is authoritative; material density does not override it.
  Seed is retained in the immutable identity, but this adapter has no stochastic
  solver control. Quality labels do not secretly retune explicit solver settings.
- Source bytes<=64MiB and cache/temporary caps<=256MiB. Conservative admission
  requires at least512MiB host memory plus input/mesh/sample allowances. The
  actual process RSS, elapsed time, output bytes and solver-step count are recorded.
  Log/file count/byte caps, deadlines and cancellation are enforced while running.
  After exit, owned descendants are stopped and final file/log sizes are checked
  again before any worker report is accepted, including for fast-exiting workers.
  POSIX termination escalates the entire owned process group even if its leader
  exits first; a child ignoring SIGTERM cannot survive that normal leader exit.
  RSS/disk enforcement is monitored polling, not an adversarial OS memory or
  network sandbox. GPU discovery/render devices do not establish GPU physics;
  both physics adapters use CPU.

## API lifecycle

The actual typed records are `CapabilityReport`, `ResourceEstimate`,
`PreparedSimulation`, and `SimulationResult`; worlds/caches use #167's canonical
runtime contracts. There is no competing scene, asset or durable job scheduler.
The existing native OS job lock supplies bounded cross-process ownership.

```python
from physics_backends import BlenderBackend
from physics_backend_example import build_inputs

backend = BlenderBackend()
world, exact_native_payloads = build_inputs()
backend.validate_scene(world)  # configuration only; explicitly not readiness
prepared = backend.prepare(world, exact_native_payloads)
try:
    result = backend.simulate(prepared)
    backend.bake(result, "output/new-physics-cache")
    state = backend.inspect("output/new-physics-cache", 12)
    report = backend.validate_cache("output/new-physics-cache")
    backend.export_animation("output/new-physics-cache", "output/new-transform-export")
finally:
    backend.cleanup(prepared)
```

The destination parent must exist, and each destination must be new. Complete
staging precedes the reviewed exclusive native rename. A destination or dangling
link created by another writer survives unchanged; only the attempt's own stage
is removed. Prepared attempts execute once. Cancellation, timeout or failed
output requires a fresh preparation; partial output is never presented as a
checkpoint. Failed process diagnostics include bounded stdout/stderr tails and
the owned workspace for inspection until explicit cleanup.

## Native Blender bake versus continuation

The Blender adapter actually calls native point-cache bake and retains the
generated `.blend`, every transform sample, a neutral128×128 CyclesCPU diagnostic
render, process logs and settings observations. It reopens that exact generated
file with automatic scripts disabled and compares all output frames in reverse
order. Original scene and sample bytes must remain unchanged.

Blender4.5.3 can expose both `is_baked=true` and `is_outdated=true` after reopening.
The flag is recorded, never force-cleared or hidden. The pinned native source
[marks an absent runtime world outdated](https://github.com/blender/blender/blob/v4.5.3/source/blender/blenkernel/intern/rigidbody.cc#L2011)
and then [prefers an exact baked read before stepping](https://github.com/blender/blender/blob/v4.5.3/source/blender/blenkernel/intern/rigidbody.cc#L2055).
Its [reset logic preserves baked data](https://github.com/blender/blender/blob/v4.5.3/source/blender/blenkernel/intern/pointcache.cc#L2718).
The real smoke additionally reverses gravity in memory: baked seeking retains
the prior result, whereas reopening and using the official Free Bake operation
before clean recomputation produces a different physical trajectory. The original
`.blend` hash is unchanged by this control. Independent review must assess that
bounded consumption evidence; pose agreement alone does not prove consumption.

This does **not** implement native solver continuation, checkpoint restart,
cross-version cache compatibility, or bit-identical recomputation. These remain
explicitly unsupported. PyBullet caches actual transform/contact samples, not
its native internal solver state. Recovery is clean restart only.

`verify` statically checks the complete bounded file inventory, exact hashes,
world/source/version/settings agreement, fixture propagation, finite complete
samples, and retained execution receipts. It re-admits retained native source and
geometry through their actual owners and checks that the retained worker request
contains that exact geometry. Internal receipt/request/report object shapes and
their backend/module observations must agree. Schema-valid integral JSON lengths
are canonicalized before file I/O; malformed records return structured failures.
Receipts are unsigned local evidence,
not cryptographic reviewer identity or a fresh solver execution. A consumer must
not reinterpret a supplied cache's receipt as human/production-art approval.
Inspection is labeled hash-verified sample-cache replay. No interpolation or
uncached timestamp is invented.

## Reproduce inspectable outputs

```bash
mkdir -p output
python scripts/physics_backends.py demo output/new-blender-physics
python scripts/physics_backends.py demo --backend pybullet --python "$HATCH_PYBULLET_PYTHON" output/new-direct-physics
python scripts/physics_backends.py verify output/new-blender-physics/cache
python scripts/physics_backends.py inspect output/new-blender-physics/cache --frame 12
python scripts/physics_backends.py export output/new-blender-physics/cache output/new-animation-export
```

The demo retains native editable source manifests/OBJ meshes, the exact physical
world, real solver samples/cache, numerical observations, and exact transform
export. The Blender cache includes `preview.png` and `.blend`; their neutral
engineering appearance is not visual acceptance. `native-transform-json` is the
only implemented animation export; GLB, authored rig clips and silent movies are
not advertised by this adapter. Those integrations remain in later work packages.

## Verification and traceability

The focused gate is:

```bash
HATCH_PHYSICS_BACKEND_TEST=1 python -m unittest discover -s tests -p test_rig_physics_backends.py -v
```

Set the actual Blender and optional interpreter paths first. Without the explicit
gate, real integration cases are reported as skips; they cannot establish readiness.
[The dedicated workflow](../.github/workflows/physics-backends.yml) installs both
real backends, enables those cases, executes fresh probes and both demonstrations,
and retains the actual native sources/caches/logs rather than successful badges.

| #168 requirement | Executable implementation | Meaningful verification |
|---|---|---|
| Typed real adapter lifecycle | `physics_backends.py` adapters and typed records | actual prepare/simulate/bake/inspect/verify/export and unmodified-owner checks |
| Trusted Blender worker/version | package-owned `physics_blender_worker.py`, factory state, disabled autoexec, exact build | actual pinned bake/reopen/render; missing/version/worker-job rejection |
| Categories/shapes/readiness | `probe(smoke=False/True)` and per-category states | both dynamic/static primitive profiles on each installed backend |
| Optional DIRECT lifecycle | isolated `physics_pybullet_worker.py`, explicit connect/finally-disconnect | actual gravity/contact/step/cache/export, plus actual empty interpreter unavailable result |
| No false success on failure | required complete outputs, strict state/receipt observations | real crash, timeout, cancellation, occupied OS lock and resource rejection; mocked zero exit only proves missing-output failure |
| Source/cache integrity | native scene/geometry owners, immutable snapshots, descriptor reads, exclusive publication | wrong bytes, links/FIFO/traversal, changed jobs, cache tampering, new/dangling/racing destination preservation |
| Numerical evidence | actual output samples | independent free-fall/semi-implicit oracle, gravity-axis control, floor response and native cache-consumption control |
| Preserve legacy boundaries | new optional modules/workflow; shared owners unchanged | actual empty-environment stdlib import; existing compatibility/schema gates on integrated candidate |

Local/CI results and exact source/dependency hashes belong in the retained
validation receipt for the final reviewed commit. Intermediate development runs
with sibling-branch imports are not final committed-tree evidence.
