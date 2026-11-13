# Verified asset inspection previews

`asset_library_preview.py` resolves an exact registered GLB through the canonical
library, evaluates native animation through the existing rig owner, builds strict
Scene3D documents, and calls the shared Render3D Blender backend. It does not
introduce another renderer, modify the registered source, grant visual approval,
or change an asset's readiness.

```sh
python scripts/asset_library_preview.py create --library /private/library \
  --reference exact-reference.json --settings preview-settings.json \
  --cache /private/preview-cache --blender /trusted/blender
python scripts/asset_library_preview.py verify /private/preview-cache/KEY
```

The reference is the existing `{id, version, sha256}` contract. The default
logical source is `runtime/asset.glb`; `--source-path` can select another exact
registered GLB. Settings accept `mode`, `width`, `height`, `samples`, `direction`,
`times`, `clip`, `background`, and `material`. Unknown fields are errors.

Modes are perspective, orthographic, idle, motion, material, environment,
wireframe, skeleton and UV. Shaded modes render the actual admitted materials
and geometry; environment mode inspects the geometry of an environment asset.
Wireframes are physical thin meshes along the actual triangle edges. Skeleton
inspection uses evaluated parent/bone positions. UV inspection places actual
triangle-edge coordinates in a plane. Missing bones, UVs or requested clips fail
explicitly. These views are technical inspections, not production film lighting
or texture-baking results. The renderer's documented material and backend limits
still apply; unavailable Blender never becomes a simulated success.

Motion takes exact rational strings, for example `"times": ["0", "1/2", "1"]`,
and an existing native clip identifier. All samples share one framing box and
camera scale, with both projected axes fitted to the output aspect ratio. At
most 32 samples, 1024-by-1024 pixels, 64 samples per pixel and
4096 inspection edges are admitted. The initial backend is CPU Cycles. Its actual
version, code hash, measured wall time, sampled memory and output sizes accompany
every preview; GPU execution is not claimed.

Before constructing sampled geometry, its arrays must fit a conservative
128 MiB aggregate estimate including source and working copies. Inspection edge
counts are checked before line-mesh allocation. Renderer limits remain an
additional bound, including a 120-second timeout for each sample.

An optional explicit material comparison is a derivative-only override:

```json
{"mode":"material","width":256,"height":256,"material":{
  "base_color":[0.1,0.7,0.3,1],"metallic":0.6,"roughness":0.25}}
```

The immutable cache key binds the full source/dependency inventory, exact
settings, renderer identity and adapter code. Every published file is hashed;
images are decoded and checked. Reuse re-verifies all files and images. New
source versions or settings create another cache entry; a failed or canceled
job cannot replace a prior entry. Corruption rejects reuse rather than silently
returning stale imagery. Cache storage is private to the current user and source
or cache symlink paths are refused. There is no destructive cleanup command.

The focused test module `test_rig_asset_preview.py` checks real native geometry,
strict settings and owner contracts. The opt-in `HATCH_ASSET_PREVIEW_TEST=1` suite
executes Blender and proves real color/motion pixel changes and cache corruption
handling. A skipped opt-in suite does not establish rendering acceptance.

For retained reproducible evidence (including source files, all rendered images,
renderer receipts, hashes and measured pixel changes), run:

```sh
python scripts/demonstrate_asset_preview.py --output NEW_DIRECTORY \
  --blender /trusted/blender
```

The example uses procedural engineering fixtures and writes `acceptance.json`;
it does not supply artistic review. This adapter depends on the shared Render3D
Blender backend and its optional PNG verification dependencies.
