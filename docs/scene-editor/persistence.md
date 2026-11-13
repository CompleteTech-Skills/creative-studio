# Native editor persistence

`EditorRepository` wraps the canonical Scene3D project, graph command service and
immutable snapshot publisher. It does not introduce another mesh, material,
animation or scene representation. The initial import and every saved revision
contain actual verified native dependencies, including geometry and provenance.
It never turns a successful save into visual or production approval.

```python
from editor3d_repository import EditorRepository

repo = EditorRepository.create("source-scene-project", "new-editor-project")
base = repo.snapshot()
result = repo.apply(base["revision"], {
    "schema_version": 1,
    "commands": [{"operation": "transform", "parameters": {
        "identifier": "mesh", "transform": {
            "translation": [3, 0, 0], "rotation": [0, 0, 0, 1],
            "scale": [1, 1, 1]}}}]
}, "move-mesh-1", expected_sha256=base["sha256"], label="Move mesh")
```

The native `scene3d_edit.apply_commands` interface owns operation names, typed
parameters, scene validation, cycles, reference constraints and matrix semantics.
The repository executes a batch against private graph objects and asks
`scene3d_snapshot.snapshot_project` to produce the full new dependency closure.
The repository verifies that closure and its bytes before publishing it.
Every compound batch either publishes one complete revision or leaves HEAD
unchanged. Source project files and historical revisions are never edited.

## Revisions, acknowledgement and recovery

The root contains `repository.json`, atomic `HEAD.json`, `revisions/NNNNNNNN/`
with native files and a hash-bound record, and `journal/` for interrupted attempts.
The revision record binds the complete native file inventory, the active scene,
parent revision, transaction identity, operation label and request digest.
`sha256` in the API is the canonical hash of this inventory, not a screenshot or
an unverified caller-supplied asset list.

An OS lock serializes writers. The expected revision and optional inventory hash
are checked under that lock. A stale client gets `EDITOR3D_STALE`; no automatic
rebase or overwrite occurs. Repeating the same transaction identifier with the
same request returns the original acknowledged revision. Reusing an identifier
with changed content fails. A returned revision means HEAD publication finished;
exceptions before acknowledgement retain the last valid HEAD or an inspectable
journal. A client should reload before reapplying a rejected optimistic edit.

Records and HEAD are flushed before publication; directories are synchronized
on POSIX systems. Exclusive native renames preserve even a newly created racing
destination. Windows uses its native exclusive rename semantics. These controls
protect cooperative editor writers and reject links and malformed metadata;
they are not an OS sandbox against a privileged process replacing ancestor
folders while the repository runs.

`recovery()` reports interrupted attempts. `recover(transaction_id,
expected_revision, expected_sha256=...)` validates the complete prospective HEAD
and native closure before committing a verified private/published revision.
A transaction whose HEAD already committed is reconciled idempotently. Invalid,
corrupt or stale journals are retained and rejected; no repair fabricates missing
native payloads. A genuinely interrupted unverified build can be rolled back with
`discard_incomplete`; this removes only its private attempt directory and moves
the exact journal to `failed/` for retained diagnosis. Other writes remain blocked
until recovery or that explicit discard. External import/render job recovery
stays with those job owners.

## Undo, redo, drag and autosave

`undo`, `redo` and `restore_revision` publish new durable revisions selecting the
exact prior native state. Restoring a deleted hierarchy restores the whole
validated state and its dependencies. They do not delete managed original assets.
A divergent edit invalidates redo. The interactive undo/redo lists are bounded
separately from retained revision records; pruning an interactive entry does not
rewrite history or imply every old revision is still on the undo stack.

`begin_drag`, `preview_drag`, `cancel_drag` and `commit_drag` provide one logical
undo entry for many previews. Previews run native validation in memory; they do
not write scene files or advance HEAD. Cancellation returns the currently saved
state. If another writer commits during the drag, commit fails with a conflict;
the caller can inspect or cancel the retained local preview.

`request_autosave` and `autosave` debounce an exact saved-revision checkpoint.
Defaults are a one-second quiet period and two-second minimum interval. Autosave
never commits an incomplete drag, invents an edit, or clears the undo stack.
`checkpoint` explicitly records a verified current revision with expected-state
checks. Since acknowledged edits are already durable, autosave is a recovery
checkpoint rather than a second mutable source of scene truth.

## Portable copies and exact content access

`save_as(new_directory)`, `archive(new_zip)` and
`EditorRepository.restore_archive(zip, new_directory)` preserve all retained
revisions, source geometry, native references, history and checkpoints. Archive
entries have exact byte sizes/hashes, portable names, normalized-name collision
checks, bounded count/expansion and no links. Restoration checks each actual byte
again and validates every native revision before exclusive publication. It
cannot overwrite an existing destination. Neither export nor restoration follows
absolute references or silently synthesizes missing assets.

`snapshot(revision)` reads a verified saved native state. `read_file(path,
snapshot_sha256)` serves only an exact file in that immutable inventory.
`native_snapshot(revision, expected_sha256)` supplies a trusted Python render
adapter with the immutable path and inventory; a renderer must still take its own
verified frozen input at admission. Paths are not an HTTP file-serving API.

Fixed native subsystem adapters may call `transact_native` with an in-process
builder. The builder must create a complete validated native snapshot at a new
private destination; unrelated files are rejected. Builders are selected by
trusted code, never deserialized from an HTTP request or loaded from a project.

## Resource limits and validation

Defaults allow 256 retained revisions, 128 interactive history entries, a 1 GiB
repository, 64 MiB per file, 10,000 files and bounded directory traversal. Limits
can be lowered at creation. When retention is full, new edits fail explicitly;
export/archive and begin a new repository instead of deleting old provenance.
The existing native Scene3D budgets also apply and may be stricter.

Run:

```bash
PYTHONPATH=scripts python -m unittest discover -s tests -p test_rig_editor3d_repository.py -v
```

Tests execute real native geometry/project edits, fresh repository reopening,
undo/redo, compound failure, stale writers, a competing OS process, drag previews,
checkpoint timing, interruption before and after HEAD publication, corrupt
journals, actual archive restoration, content tampering, path attacks, resource
limits and racing destinations. Browser interaction, render execution and full
editor acceptance remain the separately tested viewport/render integrations.

### Immutable viewport resources

The preview bridge identifies each native asset by its complete reference:
`[id, version, path, sha256]`. Browser lookup uses that same tuple; an asset ID
alone is insufficient when a restored project contains multiple revisions.
Geometry instance budgets count only nodes bound to that exact reference.
`test_distinct_asset_versions_keep_geometry_and_instance_budgets` verifies two
validated native versions retain their different geometry and budget counts;
`test_two_native_asset_versions_render_distinct_meshes` checks their actual
browser geometry. The preview remains a derived view of the saved snapshot.


### Local extension support

The launch fragment is removed immediately. Its bearer is retained in same-origin
`sessionStorage` so a normal tab refresh can authenticate; another browser context
without the launch fragment has no bearer. Storage is scoped to the local service
origin and tab session, and is never part of portable project state.

Trusted extensions can register a precommit interceptor. It receives native typed
commands and an immutable copy of the expected revision, inventory hash and
transaction ID. One interceptor may select an authenticated fixed route; multiple
claims or changed transaction identities are rejected before saving. The native
adapter owns the resulting atomic operation. A fresh acknowledged snapshot then
replaces the viewport; no separate base-scene save precedes an autokey transaction.

`subscribeInstalled` supplies an independent snapshot copy, epoch and `isCurrent`
guard after installation. Async subscribers return cleanup callbacks; cleanup
runs at replacement, WebGL loss, unsubscription or page disposal. An obsolete async
completion is cleaned immediately. Extensions can obtain the existing Three
renderer, world and camera to build source-bound projection adapters.

`capturePreview` requires the current saved revision/hash, exact registered shot,
active scene, native camera, integer frame, rational source time and bounded pixel
size. It asks the authenticated native timeline evaluator for that exact state and
renders a PNG through the native camera at the requested dimensions. Only sRGB
with the standard linear-exposure display transform is supported. Native clips
without a matching geometry adapter and changed material bindings are rejected;
no image is labelled as an equivalent production render. The viewer restores its
prior size, projection state, overlays and evaluated pose after capture.

Fixed authenticated routes may return `BinaryResponse` with verified artifact
bytes, one of the supported PNG/EXR/MP4/WebM media types and a safe basename.
`downloadArtifact` never accepts filesystem paths. Native render service admission
and artifact-byte verification remain with the render owner.
