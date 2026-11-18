# Explicit geometry optimization and LODs

`model3d_optimization.generate_lods` produces retained LOD0 plus two to five actual
reduced native `Geometry` values. It reuses the component-local vertex clustering
implementation preserved from commit `b1555e1`, together with the current native
topology, geometry, interchange and exclusive publication authorities. It never
changes source arrays. Each requested LOD must have fewer triangles than its
predecessor; an unsafe or ineffective grid fails explicitly.

Pass increasing `cell_sizes` in canonical meters, an explicit `source_version`,
`max_distance` and `min_silhouette_iou`. The default measured thresholds are 0.1m
and 0.85. These defaults suit the demonstration's one-meter object and must be
chosen explicitly for other scales. `budgets` bounds reduced vertices, triangles,
materials and texture pixels; `per_part` optionally bounds each named mesh.
Native hard memory/geometry limits are checked independently. Source LOD0 may
exceed an intended runtime budget but must fit native admission limits; every
reduced level must meet its selected runtime budget. No target-device label
silently replaces a numeric budget.

Supported simplification is smooth untextured position-only geometry. Existing
normals require the explicit `regenerate_normals=True` policy. UVs, tangents,
colors, morphs, skins, animation and textured meshes need a richer backend and
are rejected. Distinct named meshes/material slots never merge. Intentional
open boundaries remain fixed with identical boundary edges. Optional
`protected_vertices={mesh_id: [indices]}` pins source attachment/feature vertices
exactly; impossible combinations reject rather than move protected geometry.
Hard edges and facial semantics are not inferred from an unannotated mesh.

The remesher checks consistent manifold edges, vertex links, duplicate/degenerate
faces, component count and surviving-face orientation. `repair_mesh` delegates
explicit selected cleanup/orientation flags to the existing native copy-based
topology operations. Hole filling remains the explicitly selected native
`fill_planar_hole` workflow. Robust self-intersection detection is unavailable.

Quality reports retain the selected distance/silhouette thresholds, normal
replacement policy, runtime/per-part budgets, protected indices and native limits.
They include symmetric sampled vertex-to-triangle surface distances
and front/side/top pixel silhouette IoUs in one common per-pair viewport. Opposite
orthographic views have the same silhouette. Sampling and pixel resolution are
bounded; these measurements are not a continuous Hausdorff bound, all-view
silhouette guarantee or artistic approval. Metric inputs require nonflat stable
canonical geometry; unsupported numerical ranges fail with a structured code.

`export_lods` verifies retained mesh/material hashes, exports actual GLB/glTF/OBJ
assets through the native verified exporter, retains native source geometry and
publishes a new directory exclusively. CLI imports capture immutable original
files and store those originals with LOD0. Existing destinations are preserved.
`invalidated_derivatives` compares exact mesh dependencies and returns only
stale derivative IDs; the caller retains ownership of material/source dependency
invalidation, revisions, registry publication, archives and review decisions.

Run the standalone additive command (central `model3d.py` integration is separate):

```bash
python scripts/model3d_optimization.py --input source.glb --destination output/new-lods \
  --cells 0.08 0.16 --source-version source-v1 --regenerate-normals \
  --max-distance 0.15 --min-silhouette-iou 0.85
```

Run `examples/model3d-optimization/demo.py --destination NEW_DIRECTORY` for a
one-meter procedural sphere with three actual levels and reimported measurements.
These are engineering fixtures, not production artwork approval. Full #48
acceptance still needs rich-attribute optimization, registry-level derivative
invalidation and integrated parent-program demonstrations; this bounded local
profile does not close #48 or parent #10.
