# Scene3D lifecycle integration

`scene3d_lifecycle` adds verified revision inspection and selective dependency
receipts around the existing Scene3D and Editor3D owners. It does not replace the
durable `EditorRepository` transaction journal or the Render3D backend.

## Persistent revisions

Use `EditorRepository.create(source, destination, scene_path=...)` to import the
verified native closure into a new repository. `apply` accepts the authoritative
Scene3D typed command batch with an expected revision/hash and transaction id.
`undo`, `redo`, `restore_revision`, `checkpoint`, `recovery`, `recover`,
`discard_incomplete`, `save_as`, and archive APIs remain owned by Editor3D.

`scene3d_lifecycle.inspect_revision(repository_root, revision=None)` reopens and
verifies immutable native bytes, reconstructs prefab instances, and returns
actual authored base-pose world matrices, visibility, selected scene identity,
revision hash, recovery records, undo/redo availability and exact dependency
receipt. The result explicitly says `authored-base-pose`; it is not animation or
render approval. `native_snapshot(revision, sha256)` gives trusted renderer callers
the verified native root. They must recheck inputs at their admission boundary.

## Selective invalidation

`dependency_receipt(source_root, [exact_document_paths], purpose='...')` traverses
typed references from only those selected documents and binds each reachable
file's byte count and SHA256. For a geometry review, select the asset document;
for a final render, select the shot document. Exact document versions are
mandatory; no hidden `latest` binding exists.

`compare_dependencies(previous, current)` validates both receipts and returns
changed/added/removed paths and whether the selected target paths changed.
Evidence remains valid for its recorded old version; `stale` means the receipt
cannot approve the newly requested inputs. A camera edit invalidates scene/shot
receipts while preserving an unrelated mesh geometry receipt. Updating an unused
prefab or asset does not change an unrelated shot receipt. Provenance source bytes
remain part of the exact transitive dependency closure.

Receipts are self-consistency records, not authenticated approvals. Consumers
must acquire current receipts from verified native roots, never accept a supplied
receipt as proof that uninspected disk content is safe.

## New immutable shots

`snapshot_project(..., graph, scene_path=..., shot_documents=[...])` adds explicit
new shots to an edited scene. Add each new shot id to the edited scene's `shots`
list. Each submitted shot must initially reference the exact original selected
scene. Atomic snapshot publication rebinds those shots to the promoted scene,
then validates and catalogs them with immutable identities. Existing shots retain
their old versions and get one promoted version each. Invalid shot/camera
references fail before publication.

## Validation and remaining integration

`test_rig_scene3d_lifecycle` exercises actual snapshot/reopen, selective camera and
unused-prefab invalidation, tamper rejection, distinct shot promotion, durable
edit/restore, and reopening in a new Python process. The last workflow requires
the real Editor3D repository owner installed; absence is an explicit optional
integration skip when testing the Scene3D component alone.

The Render3D backend owner provides process locking, frame checkpoints, cache
validation, cancellation and decoded frame verification. Scene65 remains pending
until the authoritative Scene3D geometry/camera/light evaluation is wired into
that backend and actual animated scene PNG/GLB output is independently verified.
This document does not claim full #65 or parent #12 acceptance.
