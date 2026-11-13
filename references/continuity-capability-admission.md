# Continuity capability admission and bounded fallback

This implements the operation-admission work package #115 under #108/#107. It
consumes #113's exact references and reuses native rig loading, FK, deformation,
geometry inspection, CPU rendering and native OS job locks. It does not replace
acquisition, immutable identity storage, renderer jobs, physics caches, review
records or editor services. It does not complete the parent continuity mission.

## Discovery, execution and authority

`python scripts/continuity_capabilities.py discover` reports installed package
metadata and thirteen requested semantic capabilities. Every entry starts as
**untested**. Package installation, a path, a schema or an equal vertex count is
not evidence that an operation executed or that an identity is approved.

A trusted native owner implements `OwnerAdapter.snapshot/reserve/execute` and is
registered by the Python host, never by untrusted request JSON. A snapshot names
owner/backend and actual version, exact contract version, supported input/output
types and tested operations, limits, offline/rights policy, expiry and retained
execution evidence. Probe evidence must exist and hash correctly. Every admitted
request still executes; a snapshot is not an output cache. Native owners remain
responsible for the semantic validity of their receipts, source snapshots and
resource reservations. Arbitrary owner plugins are trusted application code,
not a security boundary or a way to load user-specified commands.

`Policy` is trusted host configuration. Request fields cannot grant asset rights,
add owners, enable a network provider or increase any resource limit. Admission
validates all exact references globally, including checks whose backend is absent.
The complete request is hashed once initially. New/observably changed exact refs
are checked incrementally; a single flattened, deduplicated fresh final hash pass
rechecks the entire request, snapshots and retained evidence before returning a
usable report. Cheap observation fingerprints avoid repeated large-content reads;
final hashes remain authoritative even when metadata appears unchanged; changed earlier references stale
the whole integrity gate while retaining diagnostics. A conflicting hash, missing canonical reference or unsafe path is an integrity
failure, not an optional missing capability. No fallback can waive integrity,
permissions, network policy, owner exclusivity or resource ceilings.

Adapters reserve their **existing native authority** during execution. The rig
adapter uses `rig_errors.job_lock`, not a second job ledger, and reloads verified
native data before and after evaluation. The image-master adapter reserves only
its bounded, read-only image inspection. Admission serializes its own in-process
call but does not pretend that this replaces project/native resource ownership.
Complete admission CPU/wall accounting includes request validation, integrity
hashing, native operations, schema checks and bounded report preparation. Budget
is enforced again after final digest emission; the timing measurement itself is
taken before that digest to avoid a self-referential hash. CPU and disk reservations are summed over the complete request; peak memory/GPU
ceilings apply to serial checks. Each selected owner is gated before reservation,
after reservation and after execution against the native whole-process lifetime
peak resident memory (including previous work in that process). An unavailable
measurement or an exceeded reservation fails closed; historical process peaks
can therefore reject a check conservatively. This prevents a successful result
from waiving its measured memory ceiling, but does not prevent an in-flight
allocation or provide hard memory isolation. Native evidence emission enforces its disk
reservation before writing. Working-memory estimates are admission bounds, not
OS allocation enforcement. The host must authorize the explicit owner probe before registering it. Creating
a `RigOwner` or `ImageMastersOwner` is an explicit real probe, not passive discovery.

The built-in bounded rig adapter admits at most 100,000 vertices and triangles,
uses native loading/resource limits, and produces 64×64 CPU reference images.
Actual process CPU and a conservative wall deadline both use the CPU reservation.
The deadline is checked after a synchronous bounded native call; it
is not hard OS preemption or a memory sandbox. Expensive native jobs need their
owner's worker isolation/cancellation and budget reservation; JSON cannot launch
a child process through this interface. Admission cancellation is checked before
and after each check; canceled and failed execution are preserved separately.
A backend that starts and fails is not silently replaced by another backend.

## Executable API

```python
from continuity_capabilities import Admission, Policy
from continuity_capability_owners import RigOwner

# project_root contains a verified native rig and a caller-owned evidence/ folder.
# This explicit probe executes real geometry inspection and retains its receipt.
owner = RigOwner(project_root, 'rig/rig.json', 'evidence', 'geometry-inspection')
policy = Policy(asset_rights=True, allowed_owners=frozenset({'hatch-rig'}))
result = Admission(project_root, policy=policy, owners=[owner]).run(request)
```

The request must include the owner's exact `owner.bound` rig pointer in the
check's `inputs`. Required native output type is `native-inspection`, input type
`hatch-3d-rig`, contract version `1.0.0`. The project revision itself is exact and
hash-verified. Schemas are retained under `profiles/continuity-capability-*.schema.json`.

`python scripts/continuity_capabilities.py admit --root ROOT --request REQUEST`
is a useful fail-closed CLI: it validates inputs and reports unavailable checks
without granting native reservations. It does not implicitly authorize tools or
register a provider. Native Python/established-service hosts register authorized
owners. The existing authenticated loopback `RigServer` exposes equivalent
`GET /v1/continuity/capabilities` discovery and `POST /v1/continuity/admit`
operations. Construct it with `continuity=Admission(...)` using trusted policy
and owner instances. The admission project root must be inside the server's
root and cannot change after binding. No HTTP field can choose a root, module,
command, rights or policy. Unconfigured POST admission returns structured
`RIG_API_CONTINUITY_UNAVAILABLE`. Existing bearer authentication, exact Origin,
worker semaphore and four-MiB request-body limits apply before the new routes;
legacy rig routes keep their behavior. This is an additive adapter to the
existing server, not a new server or graphical editor.

Every response retains the exact used capability snapshots and safe trusted host
policy (no credentials), plus canonical request and report hashes; original request
bytes and caller objects are unchanged. JSON Schema integer fields accept finite
whole-number floats via a validated copy. Booleans, fractional/nonfinite numbers,
unknown fields and unbounded resources are rejected. Histories and native review
lifecycle remain owned by their existing subsystems.

## Complete fallback matrix

| Request | Permitted implementation | Enforced boundary |
|---|---|---|
| GLB/glTF inspection | `independent-geometry-parser` | Actual native geometry/material/skin/morph observations; original source node hierarchy remains unavailable, so this native-rig adapter reports partial geometry inspection. It is not a direct GLB/glTF parser. |
| Blender-native inspection | `validated-data-export` | Requires native source and conversion binding; absent owner/data stays blocked. Never execute imported scripts. |
| Scene/animation evaluator | `bounded-native-evaluator` | Built-in rig FK/deformation is partial; contacts, phase and general scene world state remain unavailable. |
| Rig compatibility/retargeting | `exact-rig-data` | Built-in exact hierarchy/rest/bind/skin inspection is partial; no target compatibility or approved retarget profile inferred. |
| Physics continuation | `verified-native-cache` | Requires exact physics state/continuation evidence from the physics owner; none is fabricated by this module. |
| Spatial/screen direction | `world-camera-math` | Requires actual world/camera mapping and screen measurements; unknown calibration remains blocked. |
| Visual reference | `tested-geometry-renderer` | Actual base-color native triangle CPU reference; no PBR or aesthetic approval claim. |
| Visual comparison | `measured-image-comparison` | Metrics may execute, but admission always retains separate visual-review requirement. |
| GPU render | `tested-cpu-renderer` | Actual CPU reference can run only if GPU is not explicitly required and host/runtime/memory budgets permit. |
| Video encoding | `preserve-image-masters` | Actual bounded decode and exact master hashes; outcome remains partial, with video/timing/audio-absence gates unsatisfied. |
| Graphical editor | `python-cli-service` | CLI/API adapters never satisfy the GUI integration gate. |
| AI interpretation | `deterministic-rules` | Trusted rule owner must execute a supported typed interpretation; no LLM invocation is implied. |
| Missing historical media | `labeled-test-inputs` | Fixture output never verifies original media. Missing-source/restoration gate remains unsatisfied. |

Selection prefers the exact capability, then only the matrix's named bounded
fallbacks. Fallback must be explicitly permitted by the request. Owner version,
schema, operation, input/output formats, expiry, limits, evidence and host policy
must all match. Expiry is evaluated at each operation and again after native
reservation, rather than frozen at batch start. Unknown/unavailable backend
versions may be null; an available snapshot requires an actual version. No global umbrella issue must be complete before an independent
supported check can execute. Missing Blender, rig data, simulation caches, GUI or
encoder leaves those precise fields/gates unavailable; other checks continue.

Outcomes are `executed`, `supported_fallback`, `partial`,
`blocked_missing_capability`, `needs_visual_review`, `failed`, `canceled`.
They are operation outcomes, not identity/revision/review lifecycle replacements.
Native rig-data inspection can be useful while full rig compatibility stays
partial. Image masters can survive missing FFmpeg while video export stays
partial. A report always has `visual_approval: false`; no automatically generated
receipt becomes a human decision. A hard failure dominates aggregate status;
otherwise missing checks alongside completed checks produce partial status.

## Acceptance and limitations

`tests/test_rig_continuity_capabilities.py` tests real retained robotic geometry,
actual FK/deformed vertices/bind weights and CPU triangle renders, exact source
preservation, all thirteen missing-capability rows, source/permission/resource/
schema/expiry/cancellation negatives, actual image-master fallback and rejection
of CPU when GPU is mandatory. No new art approval, external model call, encoder,
physics solver, GUI, Blender execution or full visual identity is claimed.
Those native owners can register bounded adapters; absence remains explicit.

Categorical GUI/video/historical fallbacks retain their unsatisfied gates even
if a native adapter overreports fields. A deterministic-rules fallback must
identify that no model executed. A painted 2D input cannot replace the mandatory
native 3D source reference for geometry/evaluation/rendering operations.

Native responses and each merged check are validated within the per-check failure
boundary. Each check is limited to 1 MiB and cumulative check data to 4 MiB; an
oversized/malformed response becomes a structured failed check without losing
independent results. Explicit report limits can leave detailed measurements only
in retained native evidence; failure never implies acceptance.

The eight actual loopback HTTP tests additionally cover authorized native
admission, unavailable owners, default-closed configuration, bearer/Origin
rejection, worker saturation, root confinement and JSON policy/plugin injection.

The new three-OS CI executes these suites against pinned Python/rig dependencies.
Existing workflows and legacy raster/atlas/catalog contracts are unchanged.
Security is bounded local owner integration: safe portable file components and
stable handle/hash checks detect observed changes; arbitrary hostile ancestor
renames are not a filesystem sandbox. Native owner isolation remains mandatory
for untrusted executable formats and long-running tools.
