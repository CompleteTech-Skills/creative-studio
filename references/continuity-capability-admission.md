# Continuity capability admission and bounded fallback

The offline admission engine implements a slice of #115 under #108. It checks
exact continuity references and delegates execution to host-registered owners.
The native rig owner inspects geometry, evaluates forward kinematics and
deformation, or renders a bounded CPU reference. The image-master owner decodes
and verifies input masters while keeping the video export gate pending.
Broader #115 and #108 acceptance remain open.

## Discovery and admission

`python scripts/continuity_capabilities.py discover` reports installed package
metadata, tool names, and thirteen semantic capabilities. Their initial status
is `untested`. An explicit, source-bound owner probe is required before a
capability can be admitted.

`python scripts/continuity_capabilities.py admit --root ROOT --request REQUEST`
validates a request and returns availability and integrity results using the
default closed policy. Use the trusted Python API to register local owners and
authorize resource reservations.

```python
from continuity_capabilities import Admission, Policy
from continuity_capability_owners import RigOwner

# project_root contains a native rig and a caller-owned evidence directory.
owner = RigOwner(project_root, 'rig/rig.json', 'evidence', 'geometry-inspection')
policy = Policy(asset_rights=True, allowed_owners=frozenset({'hatch-rig'}))
admission = Admission(project_root, policy=policy, owners=[owner])
result = admission.run(request)
```

To enable the authenticated loopback routes, construct
`RigServer(server_root, intake, continuity=admission)` with the host-configured
`Admission` instance. Its project root must be within the server root. Use the
server's bearer token for `GET /v1/continuity/capabilities` and
`POST /v1/continuity/admit`. The POST body is the admission request JSON.

Include `owner.bound` in the check's `inputs` and provide an exact project
revision pointer. This rig adapter uses input type `hatch-3d-rig`, output type
`native-inspection`, and contract version `1.0.0`. Creating an owner executes
its probe and writes evidence. Public rig and image pointers are copies of the
owner's pinned source bindings. The trusted host provides the evidence directory,
asset rights, owner allowlist, offline policy, and resource ceilings.

The request, result, and snapshot schemas live in the corresponding
`profiles/continuity-capability-*.schema.json` files.
`python scripts/continuity_capabilities.py schemas` emits their definitions.
Reports retain the used snapshot, safe host policy, exact evidence pointers,
and canonical request and report hashes. A snapshot's version, input and
output types, limits, tested operations, expiry, and evidence must match.
Every admitted check runs again under its owner reservation.

## Evidence and resource limits

All request pointers must resolve to their exact bytes, even for unavailable
checks. A final deduplicated hash pass rechecks request, snapshot, and
evidence pointers. `integrity_coverage` describes this closure and the native
dependency checks reached during execution. Missing, changed, or malformed
evidence and native dependency refusals fail the aggregate integrity gate.
Reports and CLI errors use fixed public messages with typed refusal codes. A
native input read error without a more specific code appears as `RIG_PAYLOAD_IO`.
The host must supply the project root and caller-owned evidence directory.
Portable path checks and stable reads bind observed files. Treat untrusted
executable formats as separate native worker jobs.

The rig adapter holds the native OS job lock throughout execution. Both native
loads require the SHA-256 of the exact probed manifest bytes. Native preflight
applies the request's memory allowance and a ceiling of 100,000 vertices and
triangles. That allowance subtracts the whole-process peak and a 32 MiB
evaluation workspace. The second load also accounts for the retained first rig.
CPU references use a fixed 64×64 three-quarter view with base-color shading.

CPU and disk reservations are aggregated across the request. Memory and GPU
ceilings apply to each serial check. The engine checks process-lifetime peak
memory before reservation, after reservation, and after execution. Historical
memory peaks can reject a check, and unavailable measurements fail closed.
Image decoding is limited to four million pixels per image and sixteen million
pixels total. Image master preflight checks the 128-item count, 8 MiB per-file
size, and aggregate dependency bytes before reading source contents. Per-check
and complete report payloads are bounded at 1 MiB and 4 MiB. Admission returns a
typed report-limit refusal when the required rows exceed the complete cap. The
engine accounts for wall time across validation, hashing, execution, and report
generation. Synchronous native calls can run until they return, so
hard preemption and allocation enforcement belong to the native worker.

## Fallback matrix

| Request | Permitted implementation | Enforced boundary |
|---|---|---|
| GLB/glTF inspection | `independent-geometry-parser` | The native rig owner reports geometry, materials, skin and morphs. Original source node hierarchy remains unavailable. |
| Blender-native inspection | `validated-data-export` | Requires a native source, conversion binding, and an authorized owner for data-only inspection. |
| Scene/animation evaluator | `bounded-native-evaluator` | Built-in rig FK/deformation is partial. Contacts, phase and general scene world state remain unavailable. |
| Rig compatibility/retargeting | `exact-rig-data` | Built-in exact hierarchy/rest/bind/skin inspection is partial. Target compatibility and approved retarget profiles require separate evidence. |
| Physics continuation | `verified-native-cache` | Requires exact physics state/continuation evidence from the physics owner. |
| Spatial/screen direction | `world-camera-math` | Requires actual world/camera mapping and screen measurements. Unknown calibration remains blocked. |
| Visual reference | `tested-geometry-renderer` | Actual base-color native triangle CPU reference. PBR and aesthetic approval require separate evidence. |
| Visual comparison | `measured-image-comparison` | Computed metrics retain a separate visual-review gate. |
| GPU render | `tested-cpu-renderer` | CPU fallback requires permission to substitute the device and sufficient host, runtime, and memory budgets. |
| Video encoding | `preserve-image-masters` | Actual bounded decode and exact master hashes. The outcome remains partial, with video/timing/audio-absence gates unsatisfied. |
| Graphical editor | `python-cli-service` | CLI/API adapters never satisfy the GUI integration gate. |
| AI interpretation | `deterministic-rules` | Trusted rule owner must execute a supported typed interpretation and report model execution status. |
| Missing historical media | `labeled-test-inputs` | Fixture output keeps the missing-source and restoration gates pending. |

Selection prefers an exact implementation and considers only the named
fallbacks when the request allows them. Owner or policy failures keep their
typed gates. An unavailable Blender, physics, or editor owner leaves its check
blocked while independent geometry checks can still run.

Outcomes are `executed`, `supported_fallback`, `partial`,
`blocked_missing_capability`, `needs_visual_review`, `failed`, and `canceled`.
A hard failure determines the aggregate outcome. Missing checks alongside
completed checks can produce a partial result. Reports keep
`visual_approval: false` and leave the visual decision to the reviewer.

## Offline verification

`python -m unittest discover -s tests -p test_rig_continuity_capabilities.py`
checks the retained procedural rig, bound native loader calls, native geometry
and pose evidence, CPU rendering, image masters, mixed availability, and
typed integrity, GPU, resource, and permission outcomes.
