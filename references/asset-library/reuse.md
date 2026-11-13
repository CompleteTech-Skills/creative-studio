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
