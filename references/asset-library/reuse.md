# Exact asset reuse in independent scenes

`SceneLibraryAdapter(DependencyGraph({'local': library}))` is a minimal real
Scene3D loader, not an editor drag-and-drop integration. It keeps immutable
canonical versions under their existing library and dependency owners.

Create a document with `adapter.new_project('project-id')`, then
`adapter.add_scene(document, 'scene-a')`. `instantiate(document, scene, instance,
bindings=..., transform=..., material_overrides=..., animation_state=...)` returns
a new document. Inputs remain unchanged. Bindings map `asset` (or `character`),
and optional `rig`, `animation`, `material`, to
`{'location': {'library': 'local', 'reference': exact_ref}, 'path': 'runtime/asset.glb'}`.
`AssetBinding(location, path)` is the typed equivalent. Every reference retains
ID, semantic version and exact manifest digest; latest is never implicit.

Geometry bindings use actual self-contained GLBs. Separate rig and animation
sources must match actual geometry/rest calibration. Material GLBs supply real
PBR definitions and texture bytes. Optional per-slot overrides use decimal slot
keys and `{base_color: [r,g,b,a], metallic: n, roughness: n}`. Camera/light assets
contain ordinary standalone Scene3D node JSON, validated by the existing owner.
`set_animation(document, scene, instance, {'clip': native_id, 'time': '1/2'})`
and `set_overrides(...)` change only the explicit instance. Coordinates and
transforms follow Scene3D; source times are bounded rational seconds.

`load_scene(document, scene, NEW_DIRECTORY)` evaluates actual vertices, native
skin/clip channels and materials, writes GLB derivatives, and validates the full
Scene3D project closure before exclusive publication. Each instance keeps its
local transform. The returned `native_scene` and `shot` identify the generated
Scene3D resources. A camera produces an ordinary one-frame shot, suitable for
the shared renderer. Animation is an evaluated single sample, not a new engine
or a claim that authored control rigs survive the derivative. No default camera
or light is fabricated. Rendering and artistic review remain separate.

`replace(document, scene, instance, role, replacement)` validates the actual new
payload and returns `{project, receipt, invalidated}`. Other scenes and original
receipts remain exact. `undo(document, receipt)` restores that binding while
preserving later local transform edits; a subsequently changed binding rejects
stale undo. History retains both exact versions. `impact`, `versions`,
`dependencies`, `compatibility`, `discover` and `verify` expose consumer state.
Dependency conflict rules apply per independent instance; two scenes can pin
different immutable versions without weakening the graph owner's rules.
`ProjectStore(root).save(document, expected_sha256=...)` offers locked CAS
persistence and retained content-addressed revisions; use `None` for creation.

The optional `package_project` delegates to the package owner with independently
verified scopes, complete retained history and the binding document as context.
The package module must be integrated first. The optional direct `preview`
delegates to the preview owner only for a plain asset without composite/local
overrides; use a loaded Scene3D snapshot for composite rendering.

`legacy(binding, legacy_type=...)` returns original bytes with `dimensionality:
'2d'` and no geometry. Profiles, casts, clips, style snapshots and state selections
use existing validators; drawing/sprite references decode as 2D images. Original
receipt bytes are exposed without reinterpreting contextual approval. Legacy IDs,
fixed atlas dimensions, styles and state definitions are never rewritten.

CLI equivalents return JSON and nonzero errors:

```sh
python scripts/asset_library_reuse.py verify --library local=/private/library --project bindings.json
python scripts/asset_library_reuse.py load --library local=/private/library --project bindings.json --scene scene-a --destination NEW_DIRECTORY
```

Limits: 32 scenes, 128 instances, four MiB document, 256 history events,
one million evaluated vertices and two million triangles per snapshot. Native
importers retain their own bounds. A failed load preserves existing destinations.
Tests use procedural engineering fixtures; numerical checks are not artwork approval.

## Scoped unbind and repair

`adapter.unbind(project, scene, instance, expected_revision=revision)` removes
one whole instance. Supply `role='rig'`, `'animation'` or `'material'` to remove
only an optional binding and optionally `expected_binding=exact_binding` to
check the selected pin. Primary-asset removal requires explicit whole-instance
removal. An active rig or animation source cannot be removed: clear its state explicitly
with `set_animation(..., None)` first. Other instance fields are never silently
cleared. All operations retain immutable assets, scene receipts and history.

The result contains the new project, exact removal `receipt`, recomputed `locks`
and scoped `invalidated` instance. Historical locks keep removed source versions
available for undo. `adapter.undo_unbind(project, receipt,
expected_revision=current_revision)` restores only an empty target; a newly
occupied slot fails closed. Unrelated scenes and subsequent local edits survive.
Old replacement/undo history remains readable with the same lock names.

For durable mutations use the existing `ProjectStore` wrappers:
`store.unbind(adapter, scene, instance, expected_sha256=hash,
expected_revision=revision, role=None, expected_binding=None)`;
`store.set_animation(adapter, scene, instance, None, expected_sha256=hash,
expected_revision=revision)`; and
`store.undo_unbind(adapter, receipt, expected_sha256=hash,
expected_revision=revision)`. Each uses the existing locked CAS publication and
retains content-addressed revisions. Racing calls with one snapshot admit one
winner; stale writers preserve the winning saved document.
