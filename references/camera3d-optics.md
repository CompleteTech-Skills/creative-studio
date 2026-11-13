# Camera3D physical optics

The optional Camera3D optics adapter implements perspective and orthographic
projection with the existing Y-up, meter, XYZW camera authority. Production v1
pixel cameras and audio-enabled review movies are unchanged. Camera motion,
target constraints, sequencing, editor persistence and full production rendering
remain separate owner integrations; this module does not create a second scene
loader or render scheduler.

`camera3d_math.optical_gate` derives the effective gate from focal length, sensor
fit, render/pixel aspect, crop/expand and overscan. `field_of_view` reports
horizontal/vertical angles in radians. `with_field_of_view` returns a new optics
document with derived focal length, so the persisted lens remains authoritative.
`projection_matrix`, `project`, `unproject`, rays, frusta and projected bounds
continue to use the same reference matrix. Raster coordinates denote pixel edges.

`scene_camera_optics` converts a validated Scene3D embedded camera to Camera3D
optics. A 24 mm vertical gate with vertical fit preserves Scene3D's vertical FOV;
this explicit adapter does not imply that the source contained physical sensor
or DOF metadata. Orthographic height is preserved and DOF starts disabled.

`camera3d_blender.camera_world(camera, parent_world=..., rig=...)` composes the
resolved native scene parent, optional camera-rig ancestor chain and local camera
TRS in that order. Missing declared scene parents, cycles, ambiguous mounts and
nonrigid parents fail. Scene/rig animation owners supply evaluated parent matrices
for each exact time; the adapter never invents missing scene state.

Inside Blender, configure an already loaded scene with:

```python
from camera3d_blender import apply_camera

scene.render.engine = 'CYCLES'
camera = apply_camera(
    scene, resolved_optics, resolved_render,
    world_matrix=evaluated_camera_world,
    exposure=0.0, motion_blur=True, shutter_frames=0.5,
)
```

`apply_camera` creates a real camera, applies the canonical-to-Blender world basis
conversion and makes it active. Callers retain responsibility for verified source
references, scene loading, time sampling, rendering, output color/alpha and atomic
publication. Blender's camera-local -Z view/+Y up already matches Camera3D, so its
local basis must not be converted again.

The adapter uses a horizontal effective gate and explicitly converts normalized
principal-point shift to Blender's opposite sign and horizontal-gate units.
Orthographic height is converted to Blender's horizontal `ortho_scale`. Physical
DOF sets focus distance, f-stop, blade count, blade rotation and aperture ratio.
Orthographic physical DOF, blade counts one/two, non-Cycles engines, unsupported
RNA ranges and silent numeric clamping fail instead of producing a misleading
render. Exposure is a separate scene color-management value; motion shutter is
measured in frame intervals. Exposure is bounded to ±32 stops and shutter to
zero through one frame. ISO/white-balance simulation is not claimed.

A supplied camera must be unparented and free of animation/constraints: its
already evaluated world matrix is authoritative. Repeated updates replace only
adapter-owned unused camera datablocks; unrelated source datablocks are retained.
Imported source data is never edited by the adapter.

## Actual Blender verification

The fixed verification script creates original depth-separated mesh cubes,
compares Blender projection matrices to Camera3D for sensor fits, gate policies,
shift, overscan and non-square pixels, renders physical DOF/exposure/motion-blur
comparisons and saves/reopens the `.blend` camera state. These are engineering
fixtures, not human visual approval or the complete camera-system demonstration.

Blender 4.5.3 embeds Python 3.11. Install the schema validator into a separate
directory for that interpreter; ordinary 2D installations do not need it:

```bash
python -m pip install --target /tmp/camera-blender-site --python-version 3.11 \
  --only-binary=:all: jsonschema==4.25.1
HATCH_CAMERA_TEST_SITE=/tmp/camera-blender-site blender --background \
  --factory-startup --disable-autoexec --python-exit-code 1 \
  --python scripts/verify_camera3d_optics.py -- --destination /tmp/camera-optics-new
```

Use a new destination. The script writes actual PNGs, a reopened Blender project
and `acceptance.json` with image differences, projection errors and exact file
hashes. The native automated test is enabled by `HATCH_CAMERA3D_BLENDER_TEST=1`
with `HATCH_BLENDER` and `HATCH_CAMERA_TEST_SITE` set. Without that explicit flag,
the Blender case reports a skip; numerical tests still run with the optional rig
dependencies. A successful numerical test alone does not establish rendered DOF.


## Shared white balance

`camera3d_color.apply_camera_with_color` binds resolved camera optics to the
canonical `lookdev_color.configuration` profile. Supply its hash-bound
`temperature_kelvin`, `tint`, exposure, OCIO view and output encoding. Optical
state remains separate from global lighting. The adapter rejects mismatched
PNG/sRGB and EXR/linear-sRGB output settings before native scene mutation.
Exposure and white balance come from the shared profile; the camera cannot
supply a competing exposure value.

Blender 4.5 is required for explicit native view white balance. The transform
affects PNG output once; scene-linear EXR preserves unmodified radiance. This
API changes neither source materials nor light energy. Backend RNA bounds and
view availability can further constrain the validated portable profile. Other
renderers must provide an explicit color-management adapter. Browser white
balance remains unsupported by this binding.

Actual acceptance: `HATCH_CAMERA3D_COLOR_TEST=1 python -m unittest discover -s
tests -p test_rig_camera3d_color.py -v`. Set `HATCH_BLENDER`, and optionally
`HATCH_CAMERA_TEST_SITE` to a schema-validator installation usable by Blender.
`HATCH_CAMERA_COLOR_EVIDENCE` must name a new directory. The engineering fixture
renders independent temperature/tint PNG controls, identical linear EXR
controls, saves and reopens the native project, and records input/source/output
hashes. Fixture success supplies no production-art visual approval.
