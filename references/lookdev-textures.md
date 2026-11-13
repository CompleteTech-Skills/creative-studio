# Executable LookDev texture operations

`lookdev_textures.py` is an optional 3D module. Importing it requires only the
standard library and existing contracts; executing raster operations requires
the existing NumPy/Pillow/strict JPEG rig environment. No dependencies are added
to ordinary 2D installation.

`decode_texture(bytes, role=..., encoding=...)` verifies and decodes the exact
supplied bytes to float32 RGBA pixels. Raster arrays have top-down rows. Color
roles require explicit sRGB or linear-sRGB; data roles require `data`. Alpha is
linear. PNG/JPEG reuse the strict native importer container/entropy checks;
static WebP requires exact RIFF length and complete decoding. Radiance RGBE
supports canonical flat or channel-RLE scanlines with bounded headers, dimensions,
run lengths and exact exhaustion. Source exposure/color-correction headers and
legacy repeated-pixel HDR encoding reject explicitly. HDR values remain above
1.0 and cannot be silently clipped into PNG. RGB(A) scanline EXR decodes bounded
half/float channels with NONE, ZIPS and ZIP compression. Deep, tiled, multipart,
subsampled, non-sRGB primaries and other EXR compression modes reject explicitly.
The decoder follows the [OpenEXR file layout](https://openexr.com/en/latest/OpenEXRFileLayout.html).
EXR version 2 optionally permits the LONG_NAMES flag (0x400): attribute and
type names are bounded to 31 bytes without it, 255 with it; other flags remain
rejected. Actual Blender 4.5.3 float EXR output is retained with provenance and
compared exactly against independent OpenEXR 3.3.4 decoded pixels.
Six fixtures independently written by OpenEXR 3.3.4 verify exact half/float pixels.
Noninterlaced 16-bit grayscale/RGB PNG with optional alpha is reconstructed at
full sample precision after the native importer verifies its container. All five
PNG row filters preserve low bits; Pillow's implicit 8-bit conversion is not used.
16-bit interlaced PNG rejects explicitly rather than silently losing precision.

`verify_texture_document(document, payload)` first validates the existing
Texture3D contract, then checks exact hash/bytes and decoded dimensions, channels,
bit depth, role and encoding. A manifest cannot certify unavailable content.
`load_library_texture(library, asset_reference, document_path)` resolves the exact
shared AssetLibrary version, loads its inventoried Texture3D document and source
through the public resolver, and returns the validated document and decoded
original. Wrong version hashes, missing payloads, changed metadata and unsafe
logical paths fail through the respective owner. Registration, byte deduplication,
backup, recovery and canonical GC protection stay with AssetLibrary; HDR/EXR may
be inventoried as exact binary blobs and are decoded only by this module.
`create_blender_image(decoded, name=...)` installs actual validated pixels into a
packed float-buffer Blender image, flips top-down rows to Blender's lower-left
origin, and labels the already-linear values Non-Color to prevent a second color
transform. `create_bpy_image(bytes, ...)` is the convenience decoding wrapper.

## Procedural and derived maps

`procedural_maps(kind, ...)` returns actual base-color, height, OpenGL tangent
normal, roughness and metallic PNG bytes plus the full deterministic recipe.
Kinds: wood, stone, marble, concrete, fabric, leather, scratched-metal,
brushed-metal, clay, sand, painted-plastic, ceramic and toon-ramp. Seed, resolution,
scale, frequency, variation, turbulence and two linear RGB endpoints are explicit.
These are analytic engineering textures; they are not provider-generated assets
or material-quality approval. Normal generation accepts normalized [0,1] height
samples, converts integer arrays before arithmetic, and differentiates the field
with an explicit repeat/clamp/mirror boundary and positive-Y tangent convention.

`mipmaps(decoded)` computes an area-filtered pyramid in linear/data space,
including odd-sized inputs without dropping edge pixels. Normal maps are
renormalized after filtering. `pack_channels` packs three explicit scalar data
channels, conventionally occlusion/roughness/metallic. Source arrays/bytes are
never rewritten. PNG output is a new derivative with its own content hash;
8-bit quantization is explicit. KTX2/Basis encoding is not implemented here.

## UV operations

`transform_uv` implements glTF scale → rotation about origin → translation and
explicit repeat/clamp/mirror/no wrapping. `validate_uvs` handles up to eight UV
sets, index coverage, degenerate and mirrored triangles, and positive-area
triangle overlaps. Pair work is bounded; incomplete overlap inspection is
returned as `overlap_complete: false`. It validates supplied glTF XYZW unit
tangents and +/-1 handedness; tangent generation stays with the rig importer.
`project_uvs` provides planar, cylindrical and spherical projections. Box and
triplanar projection need face/shader coordinates and are not silently replaced
with planar projection.

`atlas_udim` converts actual same-size/role/encoding tiles 1001–1100 into a
bounded atlas and reports the exact UV scale and missing tiles. Missing tiles
are transparent. Atlas packing is an explicit new derivative, never an in-place
rewrite of approved originals.

## Publication and limits

`Limits` bounds encoded source bytes, decoded pixels, working memory and outputs.
Default admission is 64 MiB encoded / 4,194,304 pixels / 512 MiB working memory /
256 MiB derived output. Invalid/nonfinite parameters reject before generation.
`publish_texture` writes exact originals plus attribution, license, operation
history, hash and decoded metadata to a new directory. Linux/macOS use held
no-follow directory descriptors and exclusive native rename; a racing path is
not replaced. Windows publication explicitly rejects until equivalent anchored
publication is provided by the asset-store owner. Decode/transform operations
are portable independently of this publication restriction.

The shared AssetLibrary remains the authority for blob deduplication, cache
storage, version lifecycle and eviction. This module does not create a second
asset index. Image-to-material provider generation, renderer/GPU cache integration,
directional tangent ground-truth comparisons and complete #85 acceptance remain
integration work; the implementation must not be presented as full parent #15
acceptance.

## Validation

Run `python -m unittest discover -s tests -p test_rig_lookdev_textures.py -v` in
the optional rig environment. The suite decodes real raster/HDR fixtures, checks
all thirteen procedural outputs and deterministic hashes, checks linear-light
and normal mip behavior, exercises packing/UV/UDIM transforms, and verifies
exact source publication and rejection of corrupt/oversized/unsafe inputs.

## Actual Blender texture gate

Enable `HATCH_LOOKDEV_TEXTURE_TEST=1`, set `HATCH_BLENDER` to the pinned Blender
4.5.3 executable and `HATCH_LOOKDEV_TEXTURE_EVIDENCE` to a new directory, then run
`python -m unittest discover -s tests -p test_rig_lookdev_texture_blender.py -v`.
The host strictly decodes a generated PNG normal map and passes those exact
validated float pixels and original hash to a trusted Blender fixture. Actual
Cycles CPU renders compare normal strength zero versus one on curved, hard-edge,
mirrored-UV, rotated-UV, nonuniform-scale and animated shape-key-deformed meshes.
Each comparison requires a measured visible pixel change; this proves execution
on those geometries. Directional ground-truth comparisons under UV reflection,
rotation and deformation, cross-engine comparisons and MikkTSpace equivalence
remain unverified. Unit checks
separately assert positive-Y derivative orientation and tangent handedness.
The map remains assigned to a real shader when the packed project is saved;
reopening must recover every decoded pixel within 1e-7 and the original hash.
Unassigned Blender images have no users and can be omitted by Blender's save;
callers must bind the returned image to a shader or deliberately retain it.
A successful process exit alone is insufficient: the test checks every render
hash, measured differences and packed-image pixel values after reopen.

## Disposable upload memory

`lookdev_texture_cache.DerivedTextureCache` owns disposable CPU float32 upload
pyramids only. Canonical originals and versions remain in AssetLibrary; clearing
or evicting this in-memory LRU cannot remove them. `prepare(decoded, profile=...)`
keys exact source hash, actual decoded pixel hash, role/encoding and adapter/profile
settings. A repeated source digest cannot alias changed pixels. Default mobile,
browser and offline profiles limit retained dimensions and total upload bytes;
custom `UploadProfile` values are bounded. Every mip byte is admitted before
construction. Receipts record original and upload dimensions, all mip dimensions,
byte estimates and the explicit area-filter derivation; approved originals remain
untouched. Returned arrays/receipts are independent copies. Eviction releases least
recently used derived buffers within the configured retained-byte budget.

This is an application memory cache, not durable disk cache, GPU allocation or
KTX2 compression. Source decoding and mipmap working memory retain their own
separate admission limit. Shader binding, GPU texture upload and backend-native
compression must consume these explicit buffers through their owning renderer.
Run `python -m unittest discover -s tests -p test_lookdev_texture_cache.py -v`.

## Directional tangent ground truth

The opt-in `test_rig_lookdev_texture_direction.py` gate uses actual Cycles shader
normal-vector output encoded into lossless float32 EXR. A constant tangent vector
with nonzero X and Y is compared to an independently calculated world-space
basis on a plane: ordinary UVs, mirrored U, UV rotation, nonuniform object scale,
and a keyframed shape-key tilt. Nonuniform scale uses the inverse transpose of
the object transform and renormalizes the world shading normal. The assertions check vector direction and sign,
not only a changed image. The tolerance is 0.002 per world-vector component.
Set `HATCH_LOOKDEV_TEXTURE_TEST=1`, `HATCH_BLENDER` and a new
`HATCH_LOOKDEV_DIRECTION_EVIDENCE` directory before running the gate. Use the
shared native validation flock and an owned disk TMPDIR. The separate six-mesh
response gate still covers curved and hard-edge geometry; directional correctness
on curved/skinned imported MikkTSpace assets and cross-engine equivalence remain
separate validation obligations.
