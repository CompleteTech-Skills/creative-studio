# Optional planar Blender optimization

`model3d_optimization_blender.decimate_blender` is an optional local Blender 4.5
LTS adapter for **planar and angle-limited DISSOLVE optimization**. It uses native
Geometry, GLB export/import, strict image admission, geometry persistence and
exclusive publication authorities. The fixed trusted worker starts Blender with
factory settings, auto-execution disabled and two threads. Source-provided Python
and arbitrary Blender operations are never executed.

The profile retains independent mesh parts and supports base-color PBR factors,
PNG textures, UV0 and explicitly replaceable normals. Blender's NORMAL,
MATERIAL, SEAM, SHARP and UV delimiters constrain dissolving. Geometric hard edges
and open boundaries are detected in canonical geometry; their vertices and edge
connectivity must survive native reimport. Explicit protected source vertex
indices additionally preserve attachment/feature points. `recompute_normals=True`
is required when replacing source normals; authored custom-normal appearance is
not certified. General target-ratio textured COLLAPSE remains unsupported.

Successful publication requires two distinct Blender processes: create/apply/save
an editable `.blend`, then reopen that file with scripts disabled and export a
real GLB. The native importer independently decodes this GLB. Before publication,
the adapter checks actual triangle reduction, requested hard vertex/triangle/
material/texture-pixel budgets, named-part coverage, source-to-output sampled
surface distance, three common orthographic pixel silhouettes, sampled UV
interpolation, exact decoded RGBA texture pixels, PBR factors and protected edge/
point preservation. Source native geometry and optional original immutable import
bytes remain in the output. Failed processing preserves the input and does not
replace an existing destination.

Resource ceilings apply before geometry copies/native work; protected-point and
metric comparisons have explicit finite work budgets. The source must be clean,
static nonflat geometry within 10,000 canonical meters with stable triangles.
UV interpolation samples are source vertices projected onto output triangles;
this profile can reject curved or numerically unstable surfaces instead of
inventing texture fidelity. Metric samples and three raster views are not global
continuous bounds or aesthetic approval. Rich shaders, custom samplers, JPEG
re-encoding, other UV sets, tangents, colors, morphs and rigs reject explicitly.
Robust self-intersection detection and registry-level lifecycle operations remain
separate owners.

Example (select a new destination and an explicit useful polygon budget):

```bash
python examples/model3d-optimization-blender/optimize.py \
  --input input/textured-subdivided-box.glb --destination output/planar-lod \
  --triangle-budget 700 --angle-degrees 1 \
  --blender /path/to/blender
```

Python callers can supply `protected_vertices={'mesh-id': [0, 40]}` and stricter
`max_distance`, `min_silhouette_iou`, `uv_tolerance` and `point_tolerance` policies.
Always choose tolerances appropriate to source units/float32 interchange. Output
contains retained source geometry, `source.glb`, `editable.blend`,
`optimized.glb`, save/reopen reports and a hash inventory in `optimization.json`.
Passing `source_snapshot` from `import_snapshot` retains exact original source
files through the existing Model3D interchange adapter.

The native test is explicitly enabled with `HATCH_MODEL3D_DECIMATE_TEST=1` and
`HATCH_BLENDER`. Ordinary optional-dependency test runs report its environmental
skip separately. Full #48/parent #10 acceptance still requires integrated LOD
chains, broader material/attribute coverage, full demonstrations and release
review; this adapter alone does not close either issue.
