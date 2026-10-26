# Editor3D contract foundation

This document describes the bounded #99 foundation at main baseline
`bb5a6810afd06cf57185ffdfe2234e0fdc6e6240`. It does not describe a running
scene editor. Persistence/undo/autosave (#100), browser authoring (#101), asset
workflows (#102), inspectors (#103), timelines (#104), rendering (#105), and
end-to-end/security/browser acceptance (#106) remain separate requirements.
The full parent #17 remains open.

## Repository audit and integration ownership

The baseline contains no persistent general-purpose 3D scene editor, React
application, scene database, or editor command service. There is no tracked
AGENTS.md or CONTRIBUTING.md on this baseline. SKILL.md retains the compatible
`hatch-pet` name and requires source-bound review, optional 3D isolation and
separate engineering/production evidence. Existing delivery media was recovered;
the historical missing-media premise is not a current blanket failure claim.

| Existing owner | Actual behavior and reuse boundary |
| --- | --- |
| `scripts/hatch.py`, `doctor.py`, `profile_spec.py` | Existing workflow/CLI and dependency-free capability discovery; profiles retain fixed Codex atlas and existing 2D behavior. New editor imports are not added to these paths. |
| `production.py`, `production_contracts.py` | CLI and immutable portable project/cast/clip/scene/shot references. These scenes describe layered 2D performances, not a 3D graph. `load_project` verifies exact dependency hashes and explicit missing-resource diagnostics. |
| `production_performance.py`, `production_render.py` | Integer-frame evaluation and deterministic raster composition; browser playback is not the authoritative clock. No 3D image result is inferred from these outputs. |
| `production_workflow.py` | Existing source/acquisition claims, dependency impact, append-only scoped review and portable archive lifecycle. These remain the authority for original 2D assets/reviews, not rewritten as editor approvals. |
| `production_sources.py`, `production_bundle.py` | Exact source delivery and archive reconstruction; source bytes and hashes stay untouched. |
| `production_adapter.py` | Actual PNG/WAV/OpenRaster/Krita handoff, distinct from editable 3D scene data and native application acceptance. |
| `review_ui.py`, `assets/review.html`, `assets/playback.js` | Offline artifact review and playback, not general scene authoring. |
| `decomposition_spec.py`, `character_decomposition.py`, `decomposition_review.py` | Source-bound visible-pixel parts and semantic review; neither masks nor 2D layers become a mesh by linking them. |
| `rig_contracts.py`, `rig_models.py`, `rig_math.py` | Actual rig/model/skin/animation contracts and implementation; right-handed +Y-up, +Z-forward world, meters, XYZW, column-vector transforms. This is the coordinate authority. |
| `rig_http.py`, `rig_jobs.py`, `rig_operations.py` | Existing loopback bearer/origin/body limits and serialized native job operations. The editor's admission helpers do not add routes to this server or weaken its checks. |
| `rig_viewer.py`, `assets/rig-viewer/` | Actual Three.js rig preview, controls and animation inspection. It is not yet an editor for multi-scene projects. |
| `model3d_contracts.py`, `model3d_validation.py` | Strict source/model references plus actual file inspection. New editor references reuse their portable file syntax; manifest validation is not geometry acceptance. |
| Scene3D PR #210 / issue #59 | Active independent scene contract owner, absent from this main baseline. Editor scene/node wrappers pin its future native resources. They do not create a competing scene format or copy its implementation. |
| Camera3D, LookDev, Animation3D owners | Native camera/light/material/animation behavior stays outside this editor envelope. A missing owner fails explicitly. |

The expected flow is native owner documents → hash-bound editor bindings →
validated command request → native owner transaction → new immutable document
revision → derived viewport/render projections. A future service must revalidate
native resources and command preconditions inside the owner's transaction.
No current helper performs that transaction.

## Implemented shared contracts

`scripts/editor3d_contracts.py` is the single source for
`profiles/editor3d.schema.json` and `assets/editor3d/contracts.ts`. The central
schema generator also registers the JSON schema. TypeScript types are generated
from these definitions, not independently handwritten. They express structural
shape; runtime constraints (hash closure, cycles, rational reduction, native
ownership) still require runtime admission.

Eleven roots use `schema_version: 1` and the `editor3d-` namespace:

| Root | Meaning |
| --- | --- |
| `project` | Stable identity, revision/timestamps, app version, fixed coordinates, rational clock, exact pointers to editor bindings, native shots/characters/color/render resources, provenance and independent review state. |
| `scene` | Editor projection of one native scene: native reference, ordered node hashes, collections, camera selection and half-open timeline. It contains no parallel scene mesh/world/material model. |
| `node` | Stable binding to a native node, scene/hierarchy/order and editor selection/lock metadata. The native owner retains transform/mesh/material/constraint payloads. Supported bindings distinguish meshes, groups, characters, armatures, bones, cameras, physical light kinds, worlds, primitives and instances. |
| `asset` | Exact native manifest and provenance/license metadata, original/normalized format, dependency and thumbnail references, validation state and previous version. Filename equality never establishes asset identity. |
| `animation` | Exact native clip plus editor target bindings and half-open frame interval. Native skeletal/property channels are not redefined. |
| `command` | Targeted operation, native input/inverse resources, expected project hash/revision and transaction ID. This is an intent envelope, not an executed edit or undo implementation. |
| `revision` | Parent pointer, immutable project hash, command IDs, author/timestamp and exact snapshot file. It is a record contract, not a revision repository. |
| `session` | Nonpersistent selection, active scene, playhead, viewport, tool, overlays and unsaved command IDs, tied to a saved hash/revision. Tokens and machine paths have no portable fields. |
| `derived` | Nonpersistent frame projection/cache evidence tied to an exact saved project and native dependencies. No geometry is inferred from an empty or unavailable projection. |
| `request` | Typed service operation, exact project/resource scope, argument file, mutation preconditions and idempotency key. |
| `response` | Request outcome, optional exact project revision, file evidence and actionable diagnostics. Admission never emits `complete`; response validation alone cannot prove an operation occurred. |

The CLI opens a bounded regular descriptor with nonblocking/no-follow flags where
available, rejects static symlink inputs, and limits the read independently of
reported size. It does not claim transactional protection against concurrent
ancestor replacement on platforms without no-follow support.

The native reference consists of `owner`, native `id`, exact `version`,
`native_kind`, and `file` (portable path, SHA-256, byte length, media type).
Binding pointers use the canonical semantic JSON hash returned by `contract_hash`;
file references use SHA-256 of actual file bytes. These hashes are intentionally
different and must not be substituted. A native format with only `schema_version`
uses its format version `N.0.0`; formats carrying `version` retain that version.

The fixture project binds the real retained Model3D robotic manifest. Its
scene3d reference deliberately names an unavailable owner and nonexistent file.
`validate_project_bundle` validates editor closure only and explicitly returns
`native_resources_verified: false`. `verify_native_reference` rejects this
missing owner before opening a path. This is not a scene, model generation,
interactive browser demonstration, or accepted production asset.

## Runtime admission and invariants

`validate_contract` rejects unknown root kinds/versions/fields, duplicate source
JSON keys (through `decode_json`), invalid hashes/paths, nonfinite values, excessive
structural arrays, malformed or offset-free timestamps (validated with the standard library without
optional RFC3339 checker dependencies; supported timestamps use at most six
fractional digits and no leap-second value), nonsingular-scale violations in math,
non-unit quaternions, non-reduced rates, and empty/reversed intervals. JSON Schema
permits integral JSON numbers such as `24.0`; arithmetic normalizes them while
preserving original bytes and hashes.

`validate_project_bundle` checks exact pointer coverage, unique IDs, same-scene
parent membership, acyclic parent chains, distinct sibling order, collections,
asset and rig references, camera node type and animation target existence. It
never checks the truth of projected relationships against an unavailable native
scene. The future owner adapter must do that before opening/editing/rendering.

`validate_runtime_state` checks session/project identity and hash/revision,
selection membership and active-scene playhead bounds. Derived clocks must match
the project and their frame must lie in at least one saved scene range, including
its terminal boundary. Both contexts reject nodes outside the saved scene closure. Persistent serialization
has no fields for session/derived state, so loading such fields into a project is
an error rather than silent data loss.

`migrate_document` is an identity copy for validated v1 documents only. No
unsupported-version coercion or destructive legacy migration is implemented.

## Coordinate and clock adapters

`frame_time(frame, fps)` returns an exact `Fraction` in seconds. Schema-valid
integral JSON boundaries (for example `720.0`) are normalized for arithmetic. A 24/1 clock at
boundary 720 is exactly 30 seconds. Renderable frames in `[0,720)` are 0–719;
720 remains a terminal boundary. The editor session may seek that boundary.
Existing millisecond animation and 2D integer-frame contracts remain unchanged.

`transform_to_blender` lazily delegates matrix construction to `rig_math` and
conjugates object transforms by its canonical-to-Blender basis. It does not
mutate saved coordinates. Camera transforms require the camera owner's local
forward/up adapter and are not handled by this generic object helper.
`euler_to_quaternion`/`quaternion_to_euler` explicitly use fixed-axis extrinsic XYZ
radians (`Rz @ Ry @ Rx`); the latter reports a gimbal-lock warning and gives a
canonical equivalent rotation. Quaternion data remains authoritative.
World-preserving reparenting, shear handling and authored hierarchy execution
remain #100/#101/native scene work, not behavior of the binding validator.

## Local API policy and exact operation contracts

`validate_api_request` is executable preflight for original UTF-8 JSON bytes.
It authenticates a minimum 24-character bearer token with constant-time comparison,
requires an exact configured HTTP loopback origin/port (no cross-origin browser
access), application/json, and exact Content-Length of 1–4 MiB. Native paths never
arrive as arbitrary local absolute identifiers. A service must reject duplicate
HTTP headers, bind loopback, bound sockets/workers, and read no more than the
admitted length. The helper does not start a server or issue any network request.

Read operations: `projects.list/open/get/validate`, `revisions.list`,
`scenes.list/get/validate`, `assets.list/get/content/validate/dependencies`,
`render.capabilities/status/progress/manifest/output`, and
`recovery.inspect/list/compare/dependencies`. They accept no mutation preconditions,
idempotency key or input file and must have no persistent side effect.

Mutation operations: `projects.create/save/close`, `revisions.create/restore`,
`scenes.create/command/delete`, `assets.import/thumbnail/relink`,
`render.create/cancel/retry`, and `recovery.recover`. They require an idempotency
key and, except create, a project ID, expected revision and expected canonical
project hash matching the supplied current snapshot. A service must recheck under
the native owner lock. Rejection preserves the saved state; conflict recovery is
reload, compare and explicit reapplication, never overwrite.

The four global operations `projects.list`, `projects.create`,
`render.capabilities`, `recovery.list` omit project ID; all others require it.
Resource-targeted operations require `target_id`; project-level open/get/validate/
save/close, list operations, revision create, scene create, asset import, render
create and recovery inspect/dependencies omit it. An exact input-file argument is
required only for project create/save, scene create/command, asset import/relink,
render create/retry and recovery recover. Arguments remain untrusted until their
native owner validates their bytes and semantics.

Responses distinguish accepted work, actual completion, cancellation, rejection
and unavailability. Failed/unavailable results require a diagnostic with an
object path and recovery guidance. The runtime preflight returns only
`admitted, executed: false`; no success handler, job scheduler, cancellation,
streaming, retry, or export verification is implemented here. Future long-running
operations must return a job identity promptly and support bounded polling; SSE
or WebSockets must not bypass identical authorization/origin/revision controls.

## Validation and evidence

Reproduce generated artifacts and standalone contract admission:

```bash
python scripts/editor3d_contracts.py --check
python scripts/generate_schemas.py --check
python scripts/editor3d_contracts.py --validate examples/editor3d-contracts/project.json
PYTHONPATH=scripts:tests python -m unittest test_rig_editor3d_contracts -v
python scripts/test_rig_gate.py schema-math
```

The existing schema-math gate includes the editor contract module; it stays out
of 2D-only discovery because its math probes use optional NumPy. The new tests
exercise structural parity, all root fixtures, adversarial schema
and semantic inputs, exact bundle closure, real native Model3D manifest admission,
changed bytes, API preflight rejection, coordinate rotation/basis checks and exact
rational timing. They do not count a static TypeScript file as a running editor,
or a contract fixture as native scene rendering. Baseline/test results and
remaining environment blockers belong to the revision-bound evidence receipt
published with this change; historical DELIVERY_STATUS results are not fresh
results for this patch.

| #99 requirement | Implementation | Remaining boundary |
| --- | --- | --- |
| Audit current frontend/backend/assets/review/history and baseline | This audit, inherited owners, independent baseline logs | No claim of complete existing full-suite rerun unless the receipt says so |
| Shared project/scene/node/asset/animation/command/revision types and equivalence | Single schema authority, generated TS, strict admission, hash closure | Native scene owner semantics and broad editor state required by parent §5 remain blocked on #59/#210 and runtime work |
| Persistent/session/derived separation, stable IDs, coordinate/time adapters | Closed roots, context checks, existing math and exact fractions | Scene evaluation, world-preserving reparenting, UI persistence remain separate |
| Local API safety/revision/integration specification | Request/response types, preflight, operation matrix, native validators | No HTTP server, filesystem mutation, render job or editor control is represented as delivered |

A future integration must preserve the existing 2D, style/state, decomposition,
source/review and portable-package tests. No browser support, real Blender scene
rendering, production visual approval, or end-to-end editor acceptance is claimed.
