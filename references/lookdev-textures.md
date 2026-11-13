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
1.0 and cannot be silently clipped into PNG. EXR decoding remains unsupported in
this module; it needs the renderer's separately bounded native decoder.

`verify_texture_document(document, payload)` first validates the existing
Texture3D contract, then checks exact hash/bytes and decoded dimensions, channels,
bit depth, role and encoding. A manifest cannot certify unavailable content.
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
or material-quality approval. Normal generation differentiates the height field
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
asset index. Image-to-material provider generation, real curved/deforming-mesh
normal-render comparisons, cache integration and complete #85 acceptance remain
integration work; the implementation must not be presented as full parent #15
acceptance.

## Validation

Run `python -m unittest discover -s tests -p test_rig_lookdev_textures.py -v` in
the optional rig environment. The suite decodes real raster/HDR fixtures, checks
all thirteen procedural outputs and deterministic hashes, checks linear-light
and normal mip behavior, exercises packing/UV/UDIM transforms, and verifies
exact source publication and rejection of corrupt/oversized/unsafe inputs.
