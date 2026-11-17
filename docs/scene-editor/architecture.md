# Editor3D contract foundation

This document describes the bounded #99 foundation, refreshed against main
`d2744d9` after the Scene3D, Camera3D and LookDev foundations merged.
The original audit used `bb5a6810afd06cf57185ffdfe2234e0fdc6e6240`.
It does not describe a running scene editor. Persistence/undo/autosave (#100), browser authoring (#101), asset
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
| `scene3d_contracts.py` / issue #59 | Merged native scene contract and static transform authority. Editor scene/node wrappers pin native resources; manifest admission invokes this validator without claiming transitive payload verification or scene execution. |
| `camera3d_contracts.py`, `lookdev_contracts.py`, Animation3D owners | Merged camera and look-development manifest validators are invoked directly. Their payload closure, evaluation and rendering stay with those owners. The unmerged Animation3D adapter remains explicitly unavailable. |

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
Production2D `shot` records carry neither field and use the explicit v1 format
version `1.0.0`. Their ID and exact file hash/byte length remain mandatory, so
this convention does not treat different shot contents as the same resource.

The fixture project binds the real retained Model3D robotic manifest. Its
scene3d reference deliberately names a nonexistent native file.
`validate_project_bundle` validates editor closure only and explicitly returns
`native_resources_verified: false`. `verify_native_reference` recognizes the
merged Scene3D owner but rejects
this fixture's nonexistent native file. The separate unavailable-owner check
uses the unmerged Animation3D adapter. This is not a scene, model generation,
interactive browser demonstration, or accepted production asset.

## Native roles and capability boundaries

A valid manifest cannot serve every consuming role. The generated JSON Schema
and TypeScript preserve the paired owner/kind alternatives; runtime admission
uses the same definitions. The following roles bind current native documents:

| Consuming field | Permitted owner and native kind |
| --- | --- |
| `scene.native`, `node.native` | `scene3d` / `scene3d-scene` |
| `animation.native` | `rig` / `AnimationClip`; `scene3d` / `scene3d-animation`; `production2d` / `clip`; `camera3d` / `camera3d-animation` |
| `project.shots` | `scene3d` / `scene3d-shot`; `camera3d` / `camera3d-shot`; `production2d` / `shot` |
| `project.color_management` | `lookdev` / `look3d`, whose native `color_management` settings remain authoritative |
| `project.render_presets` | `scene3d` / `scene3d-render`; `lookdev` / `render-quality3d` |
| `project.characters` | `rig` / `Rig`; `model3d` / `model3d-asset`; `scene3d` / `scene3d-asset`; `production2d` / `cast` |
| `project.source_projects` | `scene3d` / `scene3d-project`; `production2d` / `project` |

The `asset.asset_type` discriminator also narrows its `native` reference:
`model3d` uses Model3D assets or Scene3D imported assets; `rig` uses a native
`Rig`; `material` uses LookDev material/instance/variant/graph resources;
`texture` uses LookDev `texture3d`; `camera` uses Camera3D `camera3d-camera`;
and `animation` uses the animation role above. The existing 2D manifest adapter
binds `image2d` and `reference2d` to the authoritative production `cast` manifest,
which retains its actual character/prop image references. It does not reinterpret
a project, shot, raster byte stream or model manifest as a single image. This
binding verifies the cast manifest only, not its transitive pixels. Legacy 2D
production documents and their loaders remain unchanged. The generated asset
type is a discriminated intersection/union with the same closed base fields;
`dependencies` and `previous` retain generic exact native references.

Generic dependency references remain inventories, not permission to reinterpret
those resources as a clip, shot, color configuration or rendering preset. An
unavailable reference may retain its exact intended role without claiming the
file exists. The retained animation binding names a deliberately unavailable
`scene3d-animation`, rather than relabeling a scene manifest as a clip.

The v1 admission adapter capability is `editor3d_contracts.VERSION` (`1.0.0`),
with versioned native identity and exact file hash/bytes checked by
`editor3d_adapters.verify_native_reference`. Its supported adapters are:

| Owner | Authoritative admission module | Capability boundary |
| --- | --- | --- |
| `rig` | `rig_contracts.validate_contract` | Existing standalone identity-bearing rig kinds; receipt-only kinds fail explicitly |
| `model3d` | `model3d_contracts.validate_contract` | Actual Model3D manifest validation, not transitive geometry execution |
| `scene3d` | `scene3d_contracts.validate_document` | Native Scene3D manifest semantics, not full project loading or rendering |
| `camera3d` | `camera3d_contracts.validate` | Native camera/optics/shot kinds and their own version |
| `lookdev` | `lookdev_contracts.validate_resource` | Native material/light/look/quality kinds and their own version |
| `production2d` | `production_contracts.validate_structure` | Existing project/scene/shot/cast/clip records; no legacy format migration |
| `animation3d` | No integrated adapter in this base | Explicitly unavailable; no invented clip kind or fallback validator |

Service operation names describe admission vocabulary, not available HTTP
handlers. A capability response must identify its adapter version, native
contract versions, supported operation names and unavailable optional tools;
installation or a parseable manifest does not establish execution. Native
render jobs and backend capabilities remain the Render3D owner's documents
(`render3d_contracts.py`); they must be inspected through that owner before any
render route advertises support. They are not accepted as generic model assets.

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

Responses distinguish accepted work with an explicit job identity, actual completion, cancellation, rejection
and unavailability. Failed/unavailable results require a diagnostic with an
object path and recovery guidance. The runtime preflight returns only
`admitted, executed: false`; no success handler, job scheduler, cancellation,
streaming, retry, or export verification is implemented here. Future long-running
operations must return a job identity promptly and support bounded polling; SSE
or WebSockets must not bypass identical authorization/origin/revision controls.

### Project locks and session ownership (service contract)

An editor session is the existing `editor3d-session.id` bound to project ID,
base revision and exact project digest. Authentication binds that session to the
local server instance; bearer tokens, operating-system lock handles and lease
secrets remain process state and never enter portable project/session files.
Opening a project establishes a session, not permission to overwrite another
session's work or a long-lived exclusive writer lock.

Every mutation acquires the native repository's exclusive writer lock for that
project's canonical repository identity. Lock acquisition is bounded and a busy
lock returns a retryable diagnostic without executing. Under the lock, the
service rereads the authoritative current revision/digest and applies the
request's comparison-and-swap preconditions before changing any file. A project
path replacement or mismatched project identity invalidates the acquisition;
checking a lock file by basename alone cannot grant ownership. Another session
or process cannot steal or remove a live lock. The owner releases its lock in
`finally` after the transaction commits or rolls back. Process termination must
release the operating-system lock; stale on-disk metadata is diagnostic evidence,
not a lock to delete without proving it has no live owner.

Session close invalidates further requests for that session, releases only its
own idle resources and asks its owned jobs to stop. It does not remove another
session's files, undo another writer's committed revision or publish unfinished
work. A revision conflict returns current identity/revision information and
requires reload, comparison and explicit reapplication. Retries with the same
idempotency key and identical input return the same recorded outcome; reuse with
different input is rejected. Session ownership and idempotency must be rechecked
under the same native transaction fence, not only at HTTP preflight. These are
requirements for the implementing service; this foundation has no lock manager,
session authentication registry or mutation executor.

### Asynchronous jobs, cancellation and partial outputs (service contract)

`response.job_id` is an optional bounded opaque identifier for synchronous v1
responses and is mandatory when `status` is `accepted`: that state means an
asynchronous job was durably registered, not completed. The owning service maps
the ID to the authenticated session, project snapshot and native job record.
It must return that identity promptly and resolve status/progress/cancel/retry
against that exact job, subject to the same token/origin and ownership rules.
The identifier is not a path, approval, output reference or permission to access
another session's job. Synchronous completion requires no fabricated job ID.

The native job owner governs execution states. An admitted job moves from
queued to running, then exactly one terminal state: completed, failed or
cancelled. A cancellation request is idempotent and may mark a queued/running job
as cancellation-requested while bounded native shutdown runs. It is acknowledged
as `cancelled` only after the worker has stopped and unpublished artifacts have
been cleaned up or retained in an owned diagnostic location. A cancellation
request alone never reports that computation stopped. Failure to stop within
the native deadline reports a diagnostic and retains truthful running/failure
state rather than claiming cancellation.

Completion and cancellation race under the native job's single publication
fence. If verified completion commits first, cancellation returns the existing
completed outcome and immutable outputs. If cancellation wins first, the worker
cannot publish a completed result later. Failed, cancelled, unavailable,
rejected and merely accepted responses publish no output references; partial
files and intermediate logs are diagnostic evidence, not completed deliverables.
Native output verification, destination ownership and exclusive atomic
publication precede the completion record. Cleanup is ownership-aware and never
deletes a concurrently replaced foreign file or directory. Retry creates or
resumes work according to the native owner's policy, preserving prior attempt
records and immutable successful outputs. Recovery must reconcile durable job
and publication state before reporting either terminal outcome.

These contracts specify the required service behavior without claiming an
implemented scheduler, cancellation controller or recovery manager. The response
validator enforces async identity and disallows output references in noncomplete
states; tests do not promote those checks into proof of worker shutdown.

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
| Shared project/scene/node/asset/animation/command/revision types and equivalence | Single schema authority, generated TS, strict consuming roles, async response identity, hash closure | Merged native manifest semantics are checked; transitive payload closure and broad editor state required by parent §5 remain runtime work |
| Persistent/session/derived separation, stable IDs, coordinate/time adapters | Closed roots, context checks, existing math and exact fractions | Scene evaluation, world-preserving reparenting, UI persistence remain separate |
| Local API safety/revision/integration specification | Request/response types, preflight, operation matrix, native validators, session/lock and cancellation/publication contracts | No HTTP server, filesystem mutation, render job or editor control is represented as delivered |

A future integration must preserve the existing 2D, style/state, decomposition,
source/review and portable-package tests. No browser support, real Blender scene
rendering, production visual approval, or end-to-end editor acceptance is claimed.
