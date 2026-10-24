# Editable scenes, native imports and immutable prefabs

Issue #60 adds operational services over the separately versioned Scene3D v1
contracts. It does not replace the 2D production controller, a native rig, an
animation timeline, a renderer, or editor persistence. Original Codex geometry,
production frame timing, state definitions and source/review bytes are unchanged.

## Graph operations

`scene3d_graph.SceneGraph` owns one in-memory working scene. Construct it through
`SceneGraph.from_bundle(load_project(root), scene_path, source_root=root)` to include external shot
and bound-clip references. A caller constructing a graph directly must supply an
explicit complete `external_references` map before deletion. Unknown reference
context fails closed. The service is single-owner; editor multiwriter storage,
autosave and crash recovery remain their respective owners' responsibilities.

The service creates and removes groups/nodes, queries and changes local/world
transforms, duplicates subtrees with deterministic scoped IDs, reorders siblings,
and reparents or promotes children without changing their world matrices. Source
display names do not determine identity. Reparenting computes `inverse(parent)
@ old_world`, preserves the explicit pivot, and rejects unsupported shear,
reflection, singularity or poor conditioning before promoting the candidate.
Driven world transforms must be detached explicitly before preserve-world edits.

`evaluate_base` evaluates parent-first canonical matrices, copy-transform,
look-at and socket constraints, and socket attachments. Camera look-at uses local
negative Z; ordinary objects use positive Z. Constraints blend representable TRS
with quaternion slerp. An attachment's transform authority is `socket_world @
offset @ local`; its parent still supplies ordinary visibility inheritance. The
dependency graph rejects cycles across parenting, constraints and attachments.
Returned values explicitly say `base_pose_only=true` and
`animation_evaluated=false`. Native clips are retained, not silently sampled by
the static graph service.

World matrices and per-node bounds are cached. Transform/visibility changes
invalidate descendants and constraint/attachment dependants; aggregate bounds
also invalidate ancestors, without reevaluating their unchanged matrices.
`evaluation_counts()` is a deterministic diagnostic. Query by stable ID, display
name, type, tag, collection or world AABB. Group bounds include descendants.
Changing geometry invalidates persisted aggregate bounds to `null` until computed.

Editor and render visibility are distinct. Collection/layer `visible` is a master
gate; optional `editor_visible` and `render_visible` further restrict each channel.
Node editor/render visibility and camera/shadow flags inherit through transform
parents. Node transform locks are local; collection/layer locks also protect
descendants of members. Selectability does not change render visibility. `isolate`
is a transient editor operation and never writes final-render flags.

Every edit validates a private candidate before promotion. Invalid edits preserve
the authored document. Expected revisions reject stale in-process edits. Command
batches use a private graph and catalog, so a later failure rolls back earlier
operations too; one successful batch commits one public revision.

## Prefab lifecycle

`scene3d_prefabs.PrefabLibrary` indexes immutable exact manifest bytes. It is not a
second asset payload store. The existing AssetLibrary remains the native byte
registry. Registering an existing identity/version or conflicting path fails;
resolution requires the exact kind, ID, semantic version, path and SHA-256.

Create a prefab from a selected subtree with `create`. Its root becomes local
identity and the result returns the original placement matrix separately. External
unversioned node constraints cannot become dangling cross-project references.
Source collections/layers are retained explicitly. Source provenance and fixture
classification remain attached; a fixture identity cannot be relabeled as
production in another version.

Instantiate linked definitions, set/reset instance overrides, detach one instance,
compare versions, upgrade with explicit conflict decisions, or revert to a retained
version. Upgrade reports changed/removed overridden fields and does not mutate the
instance until every conflict is resolved as `keep` or `source`. A removed target
cannot be kept. Source manifests and other instances never receive the edits.

`parameter_bindings` bind primitive exposed parameters to explicit source node
properties. Numeric translation/rotation/scale bindings require a component index;
boolean visibility bindings forbid one. All bindings apply simultaneously, then
unit quaternions and positive scales are validated. Values are never silently
normalized. Source defaults apply first, parameters second, instance node overrides
last. Exposed material slots must have actual authored node-slot bindings.

Optional instance children have their own source-local IDs. They cannot collide
with source children, use dangling parents, or introduce cycles. Nested expansion
scopes node/collection IDs deterministically and checks depth, output count and
operation budgets. Scene3D clips become separately hashed derived documents with
their source-local node targets remapped. Original clips remain exact dependencies.
Native rig clips keep their native identity and evaluator. `expand` returns the
derived documents for snapshotting instead of pretending they were persisted.

## Real import and component editing

`scene3d_import.import_gltf(source, library, asset_id=..., attribution=...,
license_file=...)` reads a real GLB/glTF and complete local buffers/textures into a
private immutable source snapshot. No network URI, traversal, symlink/reparse
source, unsupported required extension, nonfinite geometry or missing resource
is accepted. The initial library profile uses canonical ASCII source resource
paths, at most 128 source files, 64 MiB per file and 128 MiB aggregate source bytes;
library and native geometry budgets apply in addition. Unsupported paths or
unrepresentable positive-TRS scene transforms return explicit diagnostics.

The native importer owns actual geometry, skin weights, skeletons, morphs,
materials and animation. Original source bytes are retained unchanged under a
recorded original filename; renaming the private primary transport does not modify
its contents. Native IDs are qualified by asset version and original clip names
are retained as explicit aliases, preventing two asset versions from redefining
the same global native clip identity. A normalized GLB is independently checked
by the pinned Khronos validator before transactional library registration.

`add_import` creates editable source hierarchy nodes sharing one immutable asset.
The asset's explicit component map and recorded source-node index select real
native meshes. `evaluate_imported_vertices` uses native skeletal/morph evaluation
and then applies each component's scene edit delta. It does not fabricate skinning
or discard animation to produce a static demonstration. Source instancing may be
materialized by the normalized native geometry; that and unsupported optional
extension/light-camera proxy losses are reported, with original bytes retained.

The importer generates a real 256-pixel geometry preview. Skinned imports use the
native CPU triangle z-buffer/rest evaluator; static imports use an explicitly
labeled wireframe projection of the actual vertices. Full texture/PBR shading,
visual approval and renderer integration are not claimed. The generated PNG and
actual camera/settings records are inventoried; they are not a production review
receipt or the later Asset Library preview lifecycle.

`resolve_import` reopens exact registered data. `edit_mesh_vertices` performs real
canonical bind-space vertex edits into a new asset version, retaining topology,
weights, clips and original parent bytes. Its simple profile rejects authored
tangents/direction morphs needing a native rebake. A native normalization may change
the derived version's authoring hierarchy, which is explicitly reported; the old
version remains exact. This is not a topology sculpting or automatic rig repair tool.

## Portable snapshots and CLI

Snapshots materialize complete byte-verified dependency closures to a **new**
destination. Old scenes/shots remain untouched; edited manifests get new immutable
versions and exact references. The result explicitly selects `active_scene`.
Old project bytes are retained as history evidence. No machine-local library path
or mutable `latest` binding is needed to reopen a snapshot. Imported assets and
derived prefab clips can be supplied as exact additional payloads/documents.

Publication uses the shared exclusive native rename. A racing file, symlink or
empty directory remains intact. Unsupported exclusive filesystems fail closed.
Staging is inside an attempt-owned private directory; destination parents are an
existing trusted write boundary. This is not a general sandbox against hostile
replacement of that trusted parent by another same-user process.

```bash
python scripts/scene3d_edit.py --project INPUT --scene example-scene.json --evaluate
python scripts/scene3d_edit.py --project INPUT --scene example-scene.json \
  --commands edits.json --destination NEW_SNAPSHOT
```

An edit batch is versioned nonexecutable JSON:

```json
{"schema_version":1,"commands":[{"operation":"transform","parameters":{"identifier":"mesh","transform":{"translation":[3,0,0],"rotation":[0,0,0,1],"scale":[1,1,1]}}}]}
```

Operations include `create`, `delete`, `reparent`, `duplicate`, `reorder`,
`transform`, `node-flags`, `memberships`, `collection`, `tags`, `prefab-create`,
`prefab-instance`, `prefab-override`, `prefab-reset`, `prefab-upgrade` and
`prefab-detach`. Unknown fields/commands fail. A failed CLI batch returns structured
status 2, preserves original bytes, and publishes no destination. Exact prefab
references must already be known; planned hashes are never invented.

## Acceptance map

| #60 / parent requirement | Executed implementation and regression |
| --- | --- |
| Create/delete/reorder/duplicate and reconstruct hierarchy | `SceneGraph`; `test_create_reorder_duplicate_serialize_and_reconstruct` |
| Preserve world, pivots and reject shear/cycles | `reparent`, `set_transform`, `delete(promote)`; literal matrix/rollback tests |
| Cache invalidation, aggregate world bounds, spatial/tag queries | dependency/ancestor caches; selective-count and stale-bounds regressions |
| Visibility, locking, collections independent of parenting | collection/channel/solo/lock inheritance tests |
| External references and transactional edits | actual shot-camera protection, unknown-context rejection, batch rollback, real CLI tests |
| Immutable prefabs, bindings, optional children, material overrides | real registered prefab bytes, multiple instances, simultaneous quaternion failure tests |
| Nested expansion, detach/upgrade/revert/conflicts | derived clip target mapping and preserved original payload tests |
| Real hierarchical glTF with external resources | real binary buffer, two selectable mesh instances, source hashes, real component movement |
| Genuine skins/morphs/animation and preview | real exported/imported GLB, time-dependent native deformation, PNG pixels and Khronos checks |
| Actual simple geometry edits into immutable versions | changed vertex values after native reload; unchanged parent source bytes |
| Portable cross-project closure | actual imported native sources/geometry/rig files and edited scene/shot reopen |
| Archive/publication security | genuine racing empty destination and stale-source failures |

Validation commands and exact source/artifact hashes are retained separately in
the issue-specific acceptance receipt. Pending independent review or hosted checks
remain pending; this guide does not supply an approval.

The optional native review viewer accepts validated `clip_aliases` (display label to actual current-version native clip ID) and `reduced_motion_clip`. It selects a mapped neutral clip when configured, otherwise the native neutral clip, otherwise the actual rest pose. The browser verifier checks this exact choice and stopped playback. Display labels use DOM text APIs; aliases never rename source clips or change their provenance.

Hybrid snapshots delegate linked 2D packaging to `production_workflow.archive_project` and verify its complete closure; the supported portable layout places the canonical legacy manifest at `legacy/project.json`. Other hybrid layouts fail explicitly. The legacy files retain their exact bytes.

Run `python scripts/demonstrate_scene3d_service.py --destination NEW_DIRECTORY` for a real robotic skin/animation import, immutable prefab instances, and hash-verified portable reopen. `.github/workflows/scene3d-service.yml` runs the actual Blender, Chromium and Khronos acceptance tests on pull requests and `main`, retaining logs and demonstration artifacts. Automated checks do not confer human visual or production approval.

Bound Scene3D animations duplicate through `SceneGraph.from_bundle`'s verified animation context. Each copied clip receives a new immutable identity and remapped subtree targets; nested clips and original manifests remain dependencies. The graph exposes `derived_documents()`, and snapshots automatically materialize them. Direct graphs without the required clip context reject animated duplication before changing the scene. Native rig clips retain their native evaluator and IDs.

The independent validator checks both original source declarations and normalized exports. Mixed mesh/camera/light glTF nodes use one editable Scene3D role; other declared roles remain in original bytes/hierarchy and receive explicit per-feature loss entries. Invalid omitted-role indices or configurations still fail original-source validation. Setting `HATCH_SCENE3D_EVIDENCE` retains actual native test results or structured subprocess failures before fixture cleanup; the dedicated workflow uploads them even when assertions fail.

Animated editing retains exact source bytes through `source_root` or an explicit bounded `animation_payloads` mapping. Parsed animation dictionaries alone cannot authenticate references, including noncanonical JSON. Shared clip DAG derivation uses a per-copy memo and explicit operation/file/byte budgets. Every candidate edit rechecks remaining binding subtrees through the contract validator; removed bindings release their targets while genuine external shot references remain protected. Source-resolution of an optional-child upgrade conflict removes that entire optional subtree.
