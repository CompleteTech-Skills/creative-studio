# Additional offline 3D inspection controls

`assets/asset-browser/inspector-tools.js` exports
`installInspectorTools({THREE, scene, camera, orbit, renderer, element})`.
The caller supplies its existing pinned renderer, scene, perspective camera,
OrbitControls instance and an empty panel element. No renderer, model loader,
source record or animation engine is created here.

Call `setModel(model, meshes)` after the existing loader accepts a model,
`update()` from its animation frame, and `clear()` before disposing or replacing
the model. `dispose()` additionally removes the control panel.

The module provides:

- Per-mesh visibility using actual `Object3D.visible` (first 256 controls).
- Front, back, left, right, top and three-quarter camera presets, with an
  aspect-aware fit computed from all eight model bounding-box corners.
- Bounding-box and canonical coordinate-axis overlays owned by this module.
- Original, warm studio, soft-fill and flat lighting presets, changing the
  existing view lights and restoring their original intensities/colors on clear.
- Actual hierarchy, material/texture and skeleton-binding inspection. Names and
  JSON are rendered using text nodes, never HTML interpolation.

Inspection changes do not mutate the canonical library. Clearing a model restores
its visibility and the original view lighting, removes/disposes only module-owned
helpers, and releases model references. Model geometry, textures and materials
remain the caller's responsibility. Hierarchy display is capped at 512 nodes;
material inspection is capped at 256 meshes and 64 material slots per mesh.
Bounds for animated skins are recomputed at at most ten updates per second and
only while their overlay is enabled.

Integration acceptance should load the existing real animated browser fixture,
then prove checkbox visibility changes rendered pixels, overlay objects are in
the scene, camera presets fit the model at wide and portrait aspects, lighting
changes actual existing lights, and clear removes all module-owned helpers and
restores view state. The existing browser API/model acceptance must still pass.
