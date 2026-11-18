# Explicit absolute-light HDR color increment

This optional extension uses `cinematics3d_color` for closed-profile capture,
source context validation, measured working primaries, alpha handling and linear
processing. `cinematics3d_hdr` adds no renderer, encoder, global OCIO config or
replacement profile engine. It discovers the actual pinned OCIO 2.5.2 display
builtins for PQ and 1000-nit HLG, then executes them through that source profile's
CIE XYZ D65 interchange role.

`make_plan(root, inventory, source, target=..., reference_white_nits=...)` requires
an explicit absolute-light mapping: source RGB [1,1,1] represents the supplied
cd/m² value. It is not inferred from file labels. Targets are `hdr-pq` (BT.2020,
SMPTE ST2084, reference domain up to 10000 cd/m²) and `hdr-hlg-1000` (BT.2020,
ARIB STD-B67, 1000 cd/m² display and system gamma 1.2). The latter is a fixed
display assumption, not a claim that every HLG viewing environment is identical.
Plans calibrate neutral luminance and colored patches against independent
BT.2100 equations and chromaticities. Unsupported builtins or a failed absolute
calibration reject the request.

`transform_pixels(root, plan, rgba)` reconstructs the plan, rechecks the pinned
profile and executes actual OCIO CPU processing. It preserves source pixels and
returns float32 straight-alpha RGBA. Any source alpha unassociation happens once
in the shared canonical linear stage. A media consumer must composite its
explicit background in linear light before using the output as opaque video.

There is no creative gamut mapping or tone mapping. The receipt reports input
linear BT.2020 components below zero or above the declared reference peak,
OCIO's transfer boundary behavior and the final encoded [0,1] clamp counts.
These component counts are numerical diagnostics, not artistic approval.

The output contains no inferred mastering display, MaxCLL or MaxFALL metadata.
`mastering_metadata` is null and `encoded_video_verified` is false. Transforming
an RGBA array does not prove HDR video encoding, native rendering or display
playback. Issues #246/#237 require additional integration and acceptance.

Sources:

- [ITU-R BT.2100-2, including PQ and HLG transfer functions](https://www.itu.int/dms_pubrec/itu-r/rec/bt/R-REC-BT.2100-2-201807-S%21%21PDF-E.pdf)
- [OpenColorIO builtin display transform registry](https://opencolorio.readthedocs.io/projects/config-aces/en/v1.0.0/generated/opencolorio_config_aces.BUILTIN_TRANSFORMS.html)
- [OpenColorIO primary source usage of the PQ builtin](https://github.com/AcademySoftwareFoundation/OpenColorIO/blob/main/tests/cpu/ConfigUtils_tests.cpp)
- [ASWF ColorInterop display color space recommendations](https://github.com/AcademySoftwareFoundation/ColorInterop/blob/main/Recommendations/02_DisplayColorSpaces/DisplayColorSpaces.md)
