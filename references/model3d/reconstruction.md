# Local evidence reconstruction

These bounded methods create genuine editable native `Mesh`/`Geometry` surfaces in right-handed +Y-up, +Z-forward meters. They implement local silhouette, sketch/profile, relief and calibrated visual-hull reconstruction for #46. A silhouette extrusion or relief is a limited reconstruction. None establishes hidden anatomy, unseen textures, production artwork approval or complete parent #10 acceptance.

## Sources and explicit interpretations

`model3d_reconstruction.read_image(path, role='source')` captures original PNG, JPEG or WebP bytes through the shared descriptor-safe reader, verifies a single frame, and checks encoded/decode bounds. Its `Evidence` carries original dimensions, byte count and SHA256. `mask_from_image(evidence, mode='alpha'|'luminance', threshold=127)` uses the original resolution and an explicit threshold. Alpha requires authored alpha/transparency; opaque photos and line drawings require an explicitly supplied mask or a chosen luminance interpretation. No automatic semantic labeling, background inference, cleanup, resampling, asymmetry mirroring or physical calibration occurs. Existing canonical/decomposition component images enter through this same reader; it does not change their source contracts.

Masks/contours preserve only visible membership. Open/crossing/touching sketch contours, unresolved boundaries and diagonal nonmanifold contacts reject with diagnostics. Annotated profile coordinates, metric scale/thickness, source hashes, mask hashes and interpretation parameters should accompany every result. Unknown depth is never inferred from image brightness implicitly. A supplied or generated depth map must first be explicitly converted into calibrated metric height samples; provider-produced maps remain that provider's attributed evidence.

The image limits are 64 MiB encoded and four million decoded pixels. Geometry is bounded to 250,000 vertices/500,000 triangles. Contours have at most 256 total vertices and 16 holes; sampled heightfields have 2..256 samples per axis. Raster-mask extrusion accepts at most 262,144 binary pixels and also preflights actual surface budgets. These are reconstruction limits, independent of ordinary 2D atlas limits. Excessive work rejects before output allocation; no geometry is silently simplified.

## Geometry APIs

```python
from model3d_reconstruction_solids import (
    extrude_contour, extrude_mask, revolve_profile, heightfield,
)
from model3d_reconstruction import read_image, mask_from_image, publish

source = read_image('source.png')
mask = mask_from_image(source, mode='alpha', threshold=127)
result = extrude_mask(mask, pixel_size=.002, thickness=.15,
                      evidence=[source.descriptor()])
publish(result, 'new-directory', evidence=[source], format='glb')
```

`extrude_contour(outer, holes=(), thickness=..., evidence=...)` accepts authored XY polygon loops and extrudes along Z around zero. Loops need no duplicated closing vertex and may have either winding. Exact binary-rational slab tessellation handles concavity and nonintersecting holes without filling or bridging holes. Shared slab vertices split consistently; cap/side winding, degenerate triangles, edge incidence and every vertex link are validated. Polygon boundaries are constrained observations; cap placement, hidden caps and thickness walls remain explicit procedural inference.

`extrude_mask(mask, pixel_size=..., thickness=..., origin=(0,0), evidence=...)` builds an exact binary pixel-cell silhouette, including resolved holes and disconnected components. Raster X maps +X, raster Y maps -Y. No anatomical part ownership is inferred from disconnected regions. Edge/point-only component contacts reject rather than being labeled watertight. The report binds original binary mask bytes and shape; original image bytes can be delivered separately.

`revolve_profile([[radius_m,Y_m], ...], segments=32, cap_ends=True, evidence=...)` requires increasing Y, nonnegative radii and positive interior radii. Axis tips and optional end caps produce actual finite surfaces. Angular tessellation is 3..512. Rotational symmetry is an explicit assumption, not observation. Normals and UV0 are generated; render UV seam vertices are duplicated at U=0/1 after the shared geometric surface has been verified. The closed/source topology receipt refers to that shared pre-seam surface. Open uncapped ends are explicitly reported.

`heightfield(samples, spacing=(X_m,Z_m), base_thickness=..., evidence=...)` interpolates supplied metric Y heights on an XZ grid. A flat bottom at Y=-base_thickness and boundary walls make a finite solid. Every supplied height must stay above the bottom. This is a bas-relief, not recovered complete geometry. Top sample constraints and inferred interpolants/bottom/walls remain separate in surface attribution.

Each result has `.geometry` and `.report`, with source/constraint attribution, limitations, parameters, topology, partial completeness, pending review and zero provider calls. Returned geometry is editable; original observations remain immutable.

## Calibrated multiview reconstruction

See [visual hulls](reconstruction-views.md). `calibrated_view` requires explicit intrinsic/extrinsic/projection information and known/estimated calibration status. Unknown calibration rejects. `carve_visual_hull` intersects complete silhouette observations inside explicit metric bounds at a bounded voxel resolution, extracts a manifold surface and reprojects the actual triangles. Pixel IoU, missing/excess coverage, centroid residuals, redundant cameras and voxel uncertainty are reported. Concavities/internal geometry remain unknown. No camera calibration, photogrammetry or generative provider is silently substituted.

## Publication and verification

`publish(result, NEW_DIRECTORY, evidence=[captured...], format='glb'|'gltf'|'obj')` exports real native geometry, independently reimports the encoded artifact through the established geometry owner, retains exact observation bytes, and inventories all outputs. Visual-hull occupancy is persisted as bounded C-order XYZ uint8 boolean bytes with shape/hash. `reconstruction.json` retains parameters/uncertainties; `acceptance.json` inventories source/geometry/report hashes. Method completion binds the actual native arrays, method report and occupancy. Editing geometry, parameters or reprojection claims invalidates reconstruction publication with `RECON_STALE`; use the native editing owner or regenerate/recompute evidence instead of retaining stale reconstruction claims. Publication uses a private sibling stage and an exclusive rename; an existing or racing destination is preserved. A failed staged workflow removes only its own temporary directory.

This is a standalone Python API plus executable demonstration. Central `model3d.py` dispatch, independent JSON Schema registration/capability discovery and durable job integration remain with the shared CLI/contracts/job owners. They are explicit integration requirements, not completed work. Blender authoring, optional real reconstruction providers, automatic calibration, general ambiguous-sketch interpretation and full #10 demonstrations beyond C/D/E/F remain separate.

## Acceptance traceability

| Requirement | Executable evidence |
|---|---|
| Concave contour, multiple holes, exact cap/wall volume and watertightness | `test_model3d_reconstruction.py` |
| Explicit masks/disconnected parts, immutable PNG/JPEG/WebP sources, structured failures | same suite |
| Revolution normals/UVs/open/closed profiles; relief finite thickness | same suite |
| Source/mesh hashes, exclusive publication, three actual format roundtrips | same suite |
| Explicit perspective/orthographic cameras, bounded carving, actual mesh reprojection, contradictions, unknown/redundant viewpoints | `test_model3d_reconstruction_views.py` |
| Actual retained artwork extrusion, authored sketch/height fixtures, reconstruction from forward-rendered synthetic observations, missing/contradictory evidence | `python scripts/model3d_reconstruction_demo.py --destination NEW` |

The demo's C input is the retained Milo canonical artwork. It explicitly point-subsamples alpha with stride 16 and uses an assumed 0.005 m/original-pixel scale, preserving original bytes/full dimensions and the exact sampling/threshold assumption. The result is labeled a sampled silhouette extrusion. D/E are synthetic engineering evidence, distinct from C. The same source mesh is used only to create E's forward observations; reconstruction receives only those silhouettes/cameras and creates a separate voxel mesh. F records rejected unknown cameras, contradictory masks and incomplete redundant-camera coverage. No automatic or fixture receipt grants visual approval.
