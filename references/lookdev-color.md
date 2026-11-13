# Shared Blender color authority

`scripts/lookdev_color.py` is the shared color authority from reviewed LookDev
commit `c4257d63529dcecb4ea2b6e324e26cb8d9a2801e` (PR 356). This isolated extraction
preserves its white-balance implementation and widens exposure to the camera's
existing +/-32-stop range, with matching native RNA preflight. It introduces no
second camera color schema or image-filter implementation.

`configuration(view='AgX', exposure=0, temperature_kelvin=6500, tint=10,
output='srgb-png')` returns the exact version 1 hash-bound dictionary. Its fields
are `schema_version`, `working_space`, `view`, `exposure_stops`,
`temperature_kelvin`, `tint`, `output` and `sha256`. `validate_configuration`
rejects unknown fields, malformed/nonfinite values, unsupported ranges and stale
hashes. The existing profile has no per-camera duplicate WB parameters.

The supported pinned Blender 4.5 bounds are exposure [-32,32], WB temperature
[1800,100000] K and tint [-500,500]; neutral tint 10 matches daylight. `Standard`
and `AgX` have actual retained output evidence; requesting a missing OCIO view
rejects. Filmic remains subject to actual backend availability. See the
[official Blender 4.5 API](https://docs.blender.org/api/4.5/bpy.types.ColorManagedViewSettings.html).

`apply_configuration(scene, config)` applies the verified `use_white_balance`,
`white_balance_temperature` and `white_balance_tint` RNA properties. White balance
is a view transform: it does not recolor physical lights, source textures or
materials. The function resets unmanaged grading curves/export overrides and
sets exactly one scene exposure. PNG is display-transformed 16-bit sRGB; 32-bit
EXR remains scene-linear and does not bake view WB/exposure/tone mapping.

A camera composition must validate the shared profile and its render contract
before mutation, pass the **same** `exposure_stops` into the authoritative
`apply_camera`, then call `apply_configuration`. Match `png`+`srgb` to
`srgb-png`, or `exr`+`linear-srgb` to `scene-linear-exr`; reject incompatible
pairs. Bind the shared config hash into the camera/receipt. Camera geometry,
physical optics, transparency and full renderer lifecycle remain their owners'
responsibility. The shared profile's encoded PNG/scene-linear EXR policy is not
a promise that WB changes will be baked into linear masters.

The retained c425 native style/color workflow 43431419984 produced 18 actual
outputs (12 style images and 6 OCIO/HDR comparisons). Independent checks measured
actual WB image differences, one-stop display exposure ratio 2.00767 and unchanged
scene-linear HDR samples under different display settings. Its artifact 14039968700
has SHA256 `c9c169c448a9d10021395d987b27d98c4d75aa5dabab971dd36fa146e1a8ab6c`.
This is proof of the shared implementation, not proof that a camera binding
called it. A new camera-color composition still needs its own actual source-bound
WB exercise, independent fresh review, current checks and postverification before
full #76/#14 acceptance. Frozen PRs 311/339 and PR 356 remain unchanged.
