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

## Acceptance scope and traceability

This is an implemented subset of issue #64 and parent #12 sections 21–23.
`tests/test_rig_scene3d_language.py` exercises actual scene edits and reopened
immutable snapshots, CLI subprocesses, preserved geometry/source bytes, world
motion under a rotated parent, sequential target resolution, ambiguity, unsupported
text, stale revisions, malicious rehashed plans, resource limits and atomic failed
batches. These tests do not prove general environment construction or editor UI.

The existing Editor3D owner owns browser controls, accessible alternatives,
revisioned HTTP API, local-only protections and editable persistence. Scene3D
geometry, spatial placement, temporal sampling, materials, cameras and rendering
retain their existing subsystem owners. Broad environment prose, semantic asset
alternatives with explicit approval, every required project/scene/asset/environment/
render command category, and integrated browser acceptance remain required work
before #64 can be marked complete. The capabilities report states the current
bounded behavior, and no parent acceptance claim follows from a passing unit suite.
