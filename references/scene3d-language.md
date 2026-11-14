# Bounded Scene3D language and CLI

The offline planner compiles a precise supported grammar into the same typed
`scene3d_edit.apply_commands` transactions used by the native graph service.
It edits existing hash-validated projects and publishes immutable snapshots.
It never executes text as code, calls a remote provider, generates an image, or
claims a missing asset was constructed.

## Planning and applying an edit

Run with the dependencies in `requirements-rig.txt` installed:

```sh
python scripts/scene3d.py capabilities
python scripts/scene3d.py --output plan.json plan --project input-project \
  --scene courtyard.json --text 'move well 2 meters east; hide lantern'
python scripts/scene3d.py apply-plan --project input-project \
  --scene courtyard.json --file plan.json --destination edited-project
```

`plan`/`describe` return the original text, exact source revision and scene digest,
ordered typed commands, diagnostics, substitutions, and deterministic SHA-256.
Unknown text or an ambiguous/missing target gives status `unresolved` and exit 2.
The planner simulates each command privately through the canonical graph service;
it stops at the first diagnostic and refuses to apply any unresolved plan.
Applying a plan recompiles its original text and requires exact equality, so
rewriting both commands and the unkeyed hash cannot authorize a different edit.
A changed source revision or digest rejects an otherwise intact plan.

`revise` combines planning and application. `--dry-run` validates the complete
edit without publishing a destination. An existing destination is never replaced.
`--output` before the subcommand writes a new JSON report; an existing report is
never overwritten. Success is exit 0 and structured errors are exit 2.

Targets resolve exact stable IDs first, then case-insensitive unique display
names. Quote names containing spaces. Supported statements are:

- `move TARGET 2 meters east` (west/north/south/up/down also work).
- `position TARGET at 1, 0, 3` (world meters).
- `scale TARGET by 2` (positive local scale factor).
- `rotate TARGET 30 degrees around y` (local x/y/z axis, postmultiplied rotation).
- `hide TARGET`, `show TARGET`, `lock TARGET`, `unlock TARGET`.
- `duplicate TARGET as new-id`, `reparent TARGET under PARENT`, `delete TARGET`.

Numeric literals and the whole-number words zero through twenty are supported.
`remove` is an alias for `delete`. Separate statements with semicolons or newlines. Later statements see earlier
planned commands, including newly duplicated objects. Directions are world
+X east, +Y up and +Z north, including for rotated parents. Reparenting preserves
world pose and fails for unrepresentable transforms. Deletion uses the native
reject policy to protect external references. Every operation obeys native locks,
validation and budgets. An atomic successful batch advances one public revision.

Input is bounded to 16,384 UTF-8 bytes and 128 statements. Numeric magnitudes are
bounded to one million; final canonical transform validation applies as well.
Unsupported prose is retained in the diagnostic instead of silently discarded.
There are no automatic substitutions; unresolved descriptions must be changed by
the user and replanned. No external model authorization is implicit in this API.

## Typed commands and source concurrency

`scene-state --project input-project --scene courtyard.json` reports the exact
persisted revision, canonical scene SHA-256, document and base-pose evaluation.
Base-pose output does not claim imported animation has been sampled.

The `edit` command accepts a bounded nonexecutable JSON operations file:

```json
{"schema_version":1,"commands":[
  {"operation":"rename","parameters":{"identifier":"well","name":"Courtyard well"}},
  {"operation":"node-flags","parameters":{"identifier":"well","locked":true}}
]}
```

```sh
python scripts/scene3d.py edit --project input-project --scene courtyard.json \
  --file commands.json --expected-revision 0 --expected-sha256 EXACT_SCENE_SHA256 \
  --destination edited-project
```

Both expected revision and digest are required before any operation executes.
The input project remains immutable, a failed operation rolls back the private
batch, and no destination is published. See `scene3d-service.md` for native graph,
property, hierarchy and prefab operations. `validate`, `inspect` and `snapshot`
share the existing project-closure loader and snapshot publisher.

Python callers use `compile_description(graph, text)` (or `compile_revision`) and
`apply_plan(graph, library, plan)`. These raise `RigError` with structured
`report.code`, `message` and `stage` for invalid inputs; compilation instead records
per-statement canonical errors as unresolved diagnostics. The graph is single
owner. A server must keep its existing multiwriter transaction and local security
protections around these calls rather than inventing a second graph authority.

## Named native commands and temporal evaluation

The CLI exposes these existing native services with category-specific `--help`:

| Category | Operational commands |
| --- | --- |
| `scene`, `asset`, `shot` | `list`, `inspect`, `validate` |
| `object` | `inspect`, `validate`, `add`, `remove`, `duplicate`, `transform`, `reparent`, `visibility`, `configure`, `rename`, `memberships` |
| `prefab` | `list`, `inspect`, `validate`, `create`, `instantiate`, `update`, `override`, `reset`, `detach` |
| `camera` | `list`, `inspect`, `validate`, `create`, `configure` |
| `lighting`, `environment` | `configure` |

An inspection identifier is an exact stable ID or document path. If multiple
immutable document versions share an ID, choose the exact path. Every inspection
first validates the full hash-bound project closure. Native edits receive a JSON
**parameter object**, rather than a whole batch, and use the same mandatory
`--project`, `--scene`, `--expected-revision`, `--expected-sha256`, `--destination`
and optional `--dry-run` arguments as `edit`. For example:

```json
{"identifier":"camera","properties":{"camera":{"projection":"orthographic","near":0.1,"far":100,"vertical_fov":0.8,"orthographic_height":8}}}
```

```sh
python scripts/scene3d.py camera configure --project input-project \
  --scene courtyard.json --file parameters.json --expected-revision 0 \
  --expected-sha256 EXACT_SCENE_SHA256 --destination edited-project
```

Camera/light adapters additionally require the correct native target type before
executing any transaction. `camera create` requires a complete canonical camera
node under a `node` parameter. `object add` likewise delegates a canonical node to
`create`. `prefab update` performs the native explicit-version upgrade with
conflict resolutions; it does not overwrite an immutable prefab. See
`scene3d_edit.py` operation contracts for each exact parameter shape.

`evaluate --project input-project --scene courtyard.json --shot shot.json --frame
5/2` delegates to `scene3d_evaluate.evaluate_scene` using an exact rational frame.
The returned rational frame/time, evaluated nodes and visibility sample actual
scene animation without changing authored bytes. `--sample-geometry` additionally
requests the temporal owner's bounded native deformation sampling. Frames outside
the shot's half-open interval and invalid rational values fail explicitly. Base
inspection and this timeline evaluation remain distinct operations.

## Real geometry templates

`plan`/`describe`/`revise --environment` select a second bounded deterministic
parser. It delegates to the Scene3D environment owner's actual geometry recipes.
For example:

```sh
python scripts/scene3d.py --output plan.json plan --environment \
  --project input-project --scene courtyard.json \
  --text 'Create a workshop named shop with two rooms, width 5 meters, depth 5 meters, height 3 meters, seed 42'
python scripts/scene3d.py generate --project input-project --scene courtyard.json \
  --file plan.json --destination generated-project
```

Supported complete statement forms are:

- `Create a workshop named ID with N rooms, width W meters, depth D meters, height H meters, seed S`.
  `fantasy`, `science-fiction` and `rooms` use the same architecture form.
- `Create terrain named ID with width W meters, depth D meters, elevation E meters, resolution N, seed S`.
- `Create a box named ID with width W meters, depth D meters, height H meters, seed S`.
  `plane` uses the same form with height zero.

Up to sixteen statements form one atomic generation batch. Each recipe has an
explicit seed and 256-node budget. Geometry validation computes actual nodes and
triangles during planning before mutation. Plans retain original prose, exact
source revision/digest, typed recipes, geometry counts, generation seeds,
uncertainties, user constraints and explicit empty sections for unrequested asset,
character, camera or lighting intentions. Unknown words, missing dimensions,
nonintegral room counts and impossible geometry remain unresolved. Applying a plan
recompiles its original description exactly before delegating one immutable
`apply_recipes` snapshot; forged recipes and stale source tokens fail closed.
Readiness also checks the verified source project's effective prefab node limit
and exact snapshot file/byte budgets, including retained history and promoted
scene/shot documents. Regeneration uses the canonical recipe preparation logic,
so preserved manual edits and existing generated nodes are counted correctly.
Standalone in-memory graphs without a verified project source remain unresolved.
The snapshot owner's dry run prepares the exact byte closure and runs the same
path/hash/identity/schema/semantic validator used for real project files. It
returns `status: planned`; it does not publish a destination or claim independent
artifact verification. Persisted ordinary edit plans use this same preflight.
In-memory ordinary plans without a project context explicitly declare
`verification_scope: scene-only`; persisted plans declare `project-closure`.
Generated meshes are editable persisted native nodes with hash-bound OBJ bytes.
Existing authored nodes, source bytes and manual generator overrides retain their
native owner protections. No material, camera, character or asset substitution is
inferred from a theme name.

For richer exact recipe parameters, `terrain generate`, `room create`, `building
generate` and `layout generate` receive the same revision/digest/destination flags
as native edits. `--file` contains one canonical recipe, or a list for `layout`.
`--dry-run` computes and validates actual geometry and returns counts without
publishing. See `scene3d-environment.md` for supported shapes, openings, paths,
scatter, placement parameters and generation limitations.

## Acceptance scope and traceability

This is an implemented subset of issue #64 and parent #12 sections 21–23.
`tests/test_rig_scene3d_language.py` exercises actual scene edits and reopened
immutable snapshots, CLI subprocesses, preserved geometry/source bytes, world
motion under a rotated parent, sequential target resolution, ambiguity, unsupported
text, stale revisions, malicious rehashed plans, resource limits and atomic failed
batches. These tests do not prove general environment construction or editor UI.

`tests/test_rig_scene3d_environment_language.py` independently exercises real
workshop geometry bytes, reopening, unchanged source closure, strict unknown prose,
forged/stale plans, actual CLI generation and terrain dry runs.

The existing Editor3D owner owns browser controls, accessible alternatives,
revisioned HTTP API, local-only protections and editable persistence. Scene3D
geometry, spatial placement, temporal sampling, materials, cameras and rendering
retain their existing subsystem owners. Broad environment prose, semantic asset
alternatives with explicit approval, every required project/scene/asset/environment/
render command category, and integrated browser acceptance remain required work
before #64 can be marked complete. The capabilities report states the current
bounded behavior, and no parent acceptance claim follows from a passing unit suite.

## Durable editor language and lifecycle adapters

`scene3d_editor_language.plan_repository_description(repository, text,
environment=False)` binds one exact repository revision/hash and the native scene
plan. `apply_repository_plan(repository, plan, transaction_id)` calls the existing
`EditorRepository.transact_native` lifecycle. Native locking, stale checks,
idempotency, undo/redo and recovery retain their existing authority. Callers never
supply a deserialized callback. A browser handler must retain the existing server's
origin, Host and token protections; these trusted Python adapters alone do not
establish a browser integration acceptance claim.

`init --project SOURCE --scene PATH --destination NEW_REPOSITORY` captures an
existing validated native project into the canonical durable repository. `archive
--repository REPOSITORY --destination NEW_ZIP` and `restore --file ZIP --destination
NEW_REPOSITORY` delegate verified all-revision archives and exclusive restore.
`impact --project SOURCE --file TARGET_PATHS_JSON --purpose LABEL` delegates exact
transitive dependency receipts. None creates a competing persistence format.
Future schema conversion remains unsupported and fails under native validation.

`export-glb --project SOURCE --scene PATH --shot PATH --frame 5/2 --destination
NEW_DIRECTORY` delegates to `scene3d_export.export_scene_frame`. The exporter
samples actual geometry and writes independently validated `scene.glb` plus
`report.json` through exclusive atomic publication. This is one baked static
frame: animation/light/world format losses remain explicit in the native report.
Existing destinations fail, and authored project bytes remain unchanged.

All mutating CLI dry runs check the requested destination and prepare the same
complete immutable snapshot closure as a write. This includes typed batches,
named operations, ordinary language, environment recipes and snapshot copies.
A dry run rejects exhausted project budgets and existing or invalid destinations;
it publishes no output.
