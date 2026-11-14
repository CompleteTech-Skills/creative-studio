# Read-only canonical prefab runtime projection

A saved `prefab_instance` remains an authored instance with its original ID,
transform, exact prefab reference and overrides. The editor can derive a separate
runtime scene through `editor3d_prefabs.prepare_prefab_projection(captured_root,
snapshot)`. This adapter calls the existing
`PrefabLibrary.from_files(captured_root, snapshot).expand(snapshot['scene'])`;
it implements no second expansion, dependency store or prefab controller.

Call the adapter while the verified captured-source directory is still alive.
It preserves original noncanonical JSON bytes and their SHA-256 references.
It rejects changed source identity, unsafe paths, conflicting virtual namespaces,
and excessive source/derived bytes, files, nodes or ancestry. Interactive limits
also respect the native project's budgets. No derived file is written or
published and the repository snapshot is never replaced.

The result separates two values:

- `runtime_snapshot` is private static evaluation context for the existing native
  graph, asset and component consumers. It includes explicitly virtual derived
  documents and their byte/hash inventory. It is never a saved project revision.
- `projection` contains the expanded scene, native mappings, original prefab
  references, derived document inventory and derived-node-to-authored-anchor
  ownership. Its revision and digest identify the unchanged saved snapshot.

`project_prefab_state(prepared)` reuses the canonical editor/native static graph
evaluator, adds read-only transform diagnostics for derived children, and retains
explicit `base_pose_only: true`, `animation_evaluated: false`. Deriving scoped
clip documents is not executing animation or deformation. Unsupported native
pose/animation/material/light behavior must retain the existing owner's explicit
failure or capability boundary.

The browser metadata helper `createPrefabProjection(snapshot)` validates exact
snapshot identity and every derived node's ancestry, then returns a cached
read-only ownership interface. Render the runtime scene with the existing native
asset/component decoder, while commands and inspectors use the authored scene.
Every command path—selection, numeric edits, gizmos, keyboard actions, copy,
delete, parenting/drop and collections—must resolve a derived pick to its
original authored instance anchor or reject it. Derived IDs must never become
writable repository targets. The server's native authored graph remains the
mutation authority. Editing a real instance anchor, saving and reopening must
retain the instance and source references; detaching or hiding its children is
not prefab support.

The additive module tests use genuine OBJ geometry in multiple instances,
canonical native world vertices, nested prefabs and derived clips, noncanonical
source preservation, malformed paths/source identity, resource ceilings and real
JavaScript ownership checks. End-to-end browser acceptance requires the shared
viewport consumer hooks and an actual native vertex/display comparison plus
instance-anchor edit/save/reopen. The four Scene66 repositories also need the
existing Scene/LookDev schema and lighting integration; module admission alone
does not establish their complete editor acceptance.
