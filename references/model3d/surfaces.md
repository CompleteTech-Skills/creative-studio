# Portable material, UV and observed color surfaces

`model3d_materials`, `model3d_uv` and `model3d_projection` operate on the existing
native `rig_models.Geometry` and return copies. They do not change source files,
2D schemas or the native import/export authority. External `SkinWeights` are not
part of Geometry: these operations do not claim to regenerate skinned bindings.

`material(family, color=..., metallic=..., roughness=..., emission=...)` creates
explicit glTF core PBR parameters. Material factor colors are linear normalized
RGBA; emission is linear RGB. Supported parameter recipes cover plastic, metal,
rubber, cloth, stylized fur, wood, stone, glass, ceramic, paper, paint and a toon
approximation. The cloth/fur/glass/toon recipes explicitly identify their core
PBR limitations: they do not create strand geometry, transmission, learned
materials or custom shading. `assign_material` appends a material and assigns
selected real mesh IDs without changing other slots.

`procedural_texture` writes deterministic actual PNG bytes for checker, stripes,
wood, stone, cloth, flat normal, and packed ORM recipes. Color images use sRGB;
normal and ORM maps contain data channels. Stone noise uses an explicit seed.
Dimensions, pixel count and generator parameters are checked before allocation;
the hard supported image ceiling is four million pixels. `bind_texture` appends
native immutable image/texture/sampler resources and binds base color, normal,
occlusion, emission or metallic/roughness slots. ORM binds one packed image to
both occlusion and metallic/roughness with the glTF channel interpretation.
Native strict PNG/JPEG container and pixel validation runs before acceptance.
Existing decoded image inventory is charged together with every new binding;
compressed repeated images cannot bypass the native aggregate pixel budget.
The selected Geometry/image containers are snapshotted before strict decoding.
Texture receipts hash the same local immutable bytes that were decoded, so a
caller editing its original containers cannot publish unvalidated replacement
bytes or relabel a decoded payload with a different receipt hash.
Materials requiring a missing UV set fail through the native validator.

`generate_uv(geometry, method='box', resolution=(512,512), padding=2)` supports
XY planar projection, dominant-face-axis box mapping, +Y cylindrical mapping,
and spherical mapping about each mesh's bounds center. Unsupported collapsed
projections fail; choose box mapping for a general closed mesh. Explicit
per-triangle seam duplication transports every original attribute and morph
delta to the corresponding corners. Existing UV replacement requires an
explicit flag. Existing tangents fail with an actionable regeneration diagnostic;
UV changes cannot silently keep a stale tangent frame.

Each triangle occupies a separate padded atlas cell and has counterclockwise UV
winding. Those disjoint cells establish zero interior overlap by construction;
edge contact is not counted as overlapping surface area. Reports record islands
per part/material, actual UV area, unused texture fraction, source surface area,
and normalized per-triangle edge stretch. Packing preserves independent parts;
it does not invent segmentation or claim efficient artist-authored seam layout.
Final packed UVs must retain positive area/winding in both float64 and portable
float32, and their quantization error relative to the shortest island edge must
not exceed the explicit `max_relative_uv_error` (default 0.001). Unrepresentable
thin islands fail instead of exporting a collapsed texture parameterization.
Per-triangle islands can waste space. The operation rejects insufficient atlas
padding and preflights corner duplication against vertex and working-memory
budgets. General tangent, skin, robust UV repair and dense baking remain native
authoring integration work.

`project_image` and `project_views` require original native image bytes, explicit
camera-world and projection matrices, and supplied/measured calibration evidence.
Unknown calibration is rejected. Camera matrices use the established right-handed
+Y-up/-Z camera convention from `camera3d_math`; no pose or scale is guessed.
All selected images/cameras and geometry are snapshotted before processing, so
an editor changing caller inputs cannot relabel old colors with new geometry.

Projection computes the actual camera ray for each mesh vertex, tests all source
triangles for nearer intersections, samples the visible image pixel, and writes
portable linear RGBA `COLOR_0`. Two-sided opaque triangle occlusion prevents
projection through a represented surface. It does not model transmission or
alpha-tested occluders. Sampling is nearest-pixel with a top-left source origin.
Sources are interpreted as sRGB; embedded color-profile conversion is not a
supported operation. Transparent source pixels do not become observations.
Multiple visible observations use an equal mean in linear color space. Every
unseen vertex receives the explicit inferred fallback; reports list observed and
inferred indices and per-vertex observation counts instead of fabricated coverage.
Incident target faces are excluded from occluder tests; tiny real gaps are not
erased by a coarse world-distance cutoff. Numerically near-coincident nonincident
surfaces are recorded as unresolved depth and remain inferred.

Projection evidence retains source image hashes, camera matrices/calibration,
source/result native surface hashes, actual observed/occluded/outside counts,
color-space/sampling/blending rules and limitations. Strict vertex/triangle-pair
work budgets are checked before allocation; sources are capped at four million
pixels each and 64MiB total encoded bytes. Projection supports coordinates and
camera coefficients within magnitude 1e8 and fails explicitly on numerically
unrepresentable ray frames. It is vertex color projection, not a dense learned
texture, faithful unseen reconstruction, or artistic identity approval.

`test_model3d_surfaces.py` checks analytic UVs and padded nonoverlap, exact corner
attribute/morph transport, deterministic real PNGs, material bindings, native GLB
texture/UV/color reimport, two-layer occlusion, inferred backs, linear multiview
blending, transparent input handling, source/camera mutation during projection,
and resource/calibration rejection. No Blender or browser execution is claimed.
