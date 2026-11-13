# Calibrated silhouette reconstruction

`model3d_reconstruction_views` implements a bounded local visual hull from two or more supplied complete foreground masks. It creates a genuine editable native `Geometry` using outward-wound exposed voxel faces, with normals and a diffuse material. It does not invoke a provider or infer cameras, hidden anatomy, textures, or unseen concavities.

Use `calibrated_view(mask, name=..., calibration='known'|'estimated', evidence=..., camera_to_world=...|world_to_camera=..., intrinsics=...|orthographic=..., near=..., far=...)`. Masks are explicit nonempty two-dimensional boolean NumPy arrays. Source image acquisition/thresholding belongs to the reconstruction evidence module; no segmentation is silently applied here. Receipt metadata is an independent bounded JSON copy and includes the SHA256 of exact row-major boolean membership bytes. Preserve original source and mask bytes separately through the parent evidence publisher. Caller calibration/source claims are not authenticated approval.

The camera convention reuses `camera3d_math`: right-handed +Y-up meter world, column vectors, camera-local -Z viewing direction and +Y up. Supply exactly one rigid proper extrinsic direction. An unknown calibration status fails instead of becoming a guessed precise viewpoint. Estimated calibration is accepted only explicitly, retained in every source record and the aggregate report. There is no automatic feature matching or calibration path in this module.

Perspective intrinsics are the explicit upper-triangular pixel matrix `[fx, skew, cx; 0, fy, cy; 0, 0, 1]` with positive focal lengths. Image pixels are top-left origin; `(0,0)` denotes the upper-left pixel edge and `(0.5,0.5)` its center. Projection gives `u=fx*x/depth-skew*y/depth+cx` and `v=-fy*y/depth+cy` for camera-local depth `-z`. This adapts image-down intrinsics to camera-local +Y-up without altering the authoritative world basis. Orthographic calibration supplies `(left,right,bottom,top)` in camera-local meters, and near/far clips are explicit positive meter distances.

`carve_visual_hull(views, bounds=[[xmin,ymin,zmin],[xmax,ymax,zmax]], resolution=[nx,ny,nz], limits=None)` intersects center-sampled voxel membership in every view. Projection excludes cells outside the raster/frustum and near/far clips, so supply complete observations framing the relevant reconstruction volume. A missing/partial detail crop is not treated as an unrestricted view. Cell centers classify full cells; the boundary uncertainty recorded in meters is one voxel diagonal. No output smoothing or interpolation hides that discretization.

The result exposes `geometry`, a read-only occupancy array, and `report`. Integer lattice corner welding preserves exact neighboring cube topology. Every edge must have two opposite incidences and every vertex link must be one cycle. Ambiguous edge/corner-only voxel contacts fail with `MODEL3D_HULL_NONMANIFOLD`, leaving the masks unchanged; they are not silently repaired or labeled watertight. A zero intersection raises `MODEL3D_HULL_CONTRADICTION` with retained-cell counts after each view. Revise inconsistent masks/cameras, volume framing or resolution explicitly.

Reprojection rasterizes the actual exposed mesh triangles at target pixel centers after homogeneous near/far/frustum clipping. It does not project voxel centers as a substitute for the surface. Each view records IoU, observed/rendered foreground counts, missing foreground, excess background, centroid residual in pixels, and whether the surface differs from that observation. A nonzero discrepancy may reflect voxel discretization, calibration error, contradictory masks or a reconstruction volume that cuts off visible evidence; it does not establish a uniquely diagnosed anatomical error. Reports remain `review_status='pending'` and classify the surface as `derived-from-multiple-observations`.

Default hard ceilings are 16 views, 1,048,576 pixels per view, 262,144 voxels, 1,000,000 output vertices, 2,000,000 output triangles, a 512MiB working budget and 20,000,000 triangle pixel tests per view. Individual axes require 1..256 cells. Limits may be lowered, never raised above these ceilings. Work estimates and output counts are checked before dense volume/output construction; center projection uses bounded chunks and rasterization uses bounded rows. Evidence is limited to 4,096 nodes, depth32 and 64KiB encoded JSON.

```python
import numpy as np
from camera3d_math import look_at, world_transform
from model3d_reconstruction_views import calibrated_view, carve_visual_hull

mask = np.zeros((16, 16), dtype=bool)
mask[4:12, 4:12] = True
views = []
for name, position in [('front', [0, 0, 3]), ('right', [3, 0, 0])]:
    world = world_transform(dict(translation=position,
        rotation=look_at(position, [0, 0, 0]).tolist(), scale=[1, 1, 1]))
    views.append(calibrated_view(mask, name=name, calibration='known',
        evidence={'fixture': True, 'description': 'Authored orthographic engineering mask'},
        camera_to_world=world, orthographic=[-1, 1, -1, 1], near=.1, far=10))
result = carve_visual_hull(views, [[-1, -1, -1], [1, 1, 1]], [4, 4, 4])
# 8 retained voxels, bounds +/-0.5m, 1m^3 closed cube, both view IoUs 1.
```

`test_model3d_reconstruction_views.py` independently checks cube bounds/signed volume, opposite-camera/world-to-camera math, perspective K projection, actual surface reprojection errors, inconsistent silhouettes, unknown/malformed calibration, resource ceilings, diagonal nonmanifold contacts, source/mask immutability and genuine GLB/glTF/OBJ export/reimport. These are numerical engineering fixtures. Browser/Blender execution, production photographic calibration and artistic approval remain separate acceptance gates.

The report always labels completeness `partial`, provider calls `0`, and review `pending`. Pairwise camera baselines and viewing-axis angles, the numerical rank of viewing axes, and repeated calibration pairs make weak viewpoint collections explicit. Same-camera duplicates are compatible observations but add no independent geometric constraint. Collinear viewing axes trigger an uncertainty note; perspective baselines can still constrain depth, so axis rank is not mislabeled as reconstruction rank. Every output triangle is classified as a derived visual-hull boundary, never directly observed anatomy. The occupancy receipt binds its exact C-order XYZ boolean membership hash and shape; retain the returned occupancy bytes alongside published evidence when preserving all intermediate reconstruction state.
