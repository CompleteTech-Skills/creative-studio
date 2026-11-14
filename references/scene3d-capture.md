# Exact private input capture

`scene3d_capture.capture_verified_files(root, verified_inventory)` is a context
manager yielding a private project root. Paths, project bytes and source hashes
are preserved; this is an admission copy, not a new project revision. The caller
selects the complete exact dependency closure. Inventory accepts the canonical
project mapping (`path -> {sha256, bytes}`) or a list of exact file references
with `path` and `sha256` (and optional observed `bytes`).

Every selected file is read through the canonical bounded regular-file reader,
checked against its hash and optional byte count, and copied before the context
yields. File counts, individual bytes and aggregate bytes are bounded by the
existing native/library limits. Repeated paths, case aliases, path traversal and
file/directory aliases reject. Inventory metadata is copied before source reads.
The private tree survives for the complete context and is removed on exit.

Legacy geometry/native readers must receive only this private root. Reading or
hashing an original path first and then reopening that mutable path is not an
admission boundary. The helper does not infer missing dependencies or authorize a
decoder to follow external references outside the selected closure.

`capture_verified_native(root, manifest_relative, verified_inventory)` selects
the manifest's registered subtree and yields its private rig directory. For
example, a project-relative `actors/hero/rig.json` yields the private
`actors/hero` directory. It requires that exact manifest in the inventory.
Consumers needing a Pose, scene or import report outside the native subtree use
`capture_verified_files` with the complete selected closure instead.

The existing imported-asset `_load_verified_native` wrapper translates its
registered native prefix and loads through this shared context, preserving its
previous return value. Evaluators should complete source-dependent work while
the context remains open.

`test_rig_scene3d_capture` exercises genuine arbitrary-path native loading,
private file lifetime, original-source and inventory mutation, exact hashes and
byte counts, malformed inventories, bounded budgets, symlink files/ancestors,
and actual FIFO rejection in a timeout-bounded subprocess. Existing imported
service FIFO and source-replacement regressions remain in the service suite.

Exact empty evidence files are admitted only when their registered hash is the
SHA256 of empty bytes. The canonical reader retains strict nonempty defaults
for ordinary library ingestion; capture explicitly opts into empty regular-file
reads. An exact aggregate budget can include trailing empty evidence files.
