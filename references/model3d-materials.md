# Model3D materials and UVs

The executable surface authority is `scripts/model3d_materials.py`,
`model3d_uv.py` and `model3d_projection.py`, using the existing native
`rig_models.Geometry`, material validator, image decoder and GLB/glTF exporter.
Install the repository's optional rig/test requirements; this CPU workflow needs
NumPy, Pillow and the existing native glTF dependencies. Blender/provider models
are not imported or required for these functions.

`material(family, color=..., metallic=..., roughness=..., emission=...)` creates
portable core PBR factors. Plastic, metal, rubber, cloth, stylized fur, wood,
stone, glass, ceramic, paper, paint and a toon approximation are parameter
recipes, not learned materials or universal shaders. Glass does not claim
physical transmission, fur does not create strands, and toon does not install a
custom renderer. Factor and vertex colors are linear RGBA; color PNGs are sRGB.

`procedural_texture(kind, width=..., height=..., seed=...)` produces actual PNG
bytes for checker, stripes, wood, stone, cloth, flat normal and packed ORM maps.
`bind_texture` attaches validated base-color, normal, occlusion, emission or
metallic/roughness resources to a native material and preserves all previous
resource bytes. Both the existing image inventory and new immutable image bytes
must pass the shared decoded-pixel budget. These functions do not call an image
generation provider or upload inputs. Optional learned PBR generation is separate.

`generate_uv` implements planar, box, +Y cylindrical and spherical projection.
It duplicates triangle corners with all vertex attributes/morph deltas, creates
explicit seams, and packs separate padded triangle islands. The measured report
records source surface area, UV area, unused space, per-part/material islands and
normalized edge stretch. Nonoverlap follows from disjoint cells. This predictable
packing is intentionally less efficient than an artist-authored seam layout.
Intentional shared/mirrored authored UVs are not automatically classified as an
error; replacing existing UVs must be explicit. Stale tangents and external skin
bindings are not silently regenerated. Float32 packing collapse or excessive
relative UV quantization fails before export.

`project_views` uses explicit calibrated cameras and original validated PNG/JPEG
bytes. Every vertex is tested against actual mesh triangles for occlusion; thin
gaps and unresolved depth do not turn into fabricated observations. Visible
samples blend equally in linear color space, and unseen vertices receive an
explicit inferred fallback. Reports retain exact images, source/result geometry,
camera matrices, calibration, observed/inferred indices and depth uncertainty.
This is real vertex-color projection, not dense learned texture reconstruction
or an absolute identity score. Opaque two-sided occlusion does not model glass,
transmission or alpha-tested cutouts. Source profile conversion is unsupported.

The hard image ceiling is 4,194,304 pixels per generated/projected source image;
projection accepts up to eight views, 20,000 vertices, and an explicitly bounded
vertex×triangle×view workload (default 2,000,000, maximum 10,000,000 tests).
Projection coordinates/camera coefficients are bounded to magnitude 1e8.
UV atlases support at most 4096×4096 resolution, native geometry/memory limits,
explicit padding and a default 0.001 relative UV quantization threshold.
Unsupported calibration, allocation requests, corrupted images and precision
failures return structured diagnostics rather than guessed results.

Versioned semantic wrappers are `generate_asset_uv`, `texture_asset` and
`project_asset` in `model3d_semantic_surfaces`. They preserve local hierarchy,
create new revisions, keep exact sources and hash-bound measurement reports, and
invalidate only derivatives bound to changed components/resources.

Run the actual combined fixture workflow with:

```bash
python scripts/demonstrate_model3d_semantic.py --destination output/new-semantic-demo
```

It exports and independently reimports a real textured/PBR/UV mesh hierarchy,
preserves native editable sources and evaluates a rigid pose on the same base
geometry. `test_model3d_surfaces.py` executes actual PNGs, padded UVs, native
export/reimport, occlusion, resource/precision boundaries and caller-mutation
regressions. Successful numeric fixtures do not establish artistic acceptance,
Blender material editing or browser review. Those gates remain explicit in the
authoring/acceptance program. Full API details: [surface algorithms](model3d/surfaces.md).
