# Native material-reference validation

The shared `rig_models.validate_geometry` owner validates material/texture bindings on real model import, native geometry save/load, and GLB/glTF export. This is a partial correction for #52, #44 and #139; it does not complete their import, fitting, asset-library or interoperability work packages.

Previously, a valid triangle glTF could declare `baseColorTexture.index=99999` without any corresponding texture and still import successfully. Missing UV sets and dangling sampler references were also admitted. The corrected native validator rejects these before run publication or native export, using structured diagnostics.

## Validated scope

- Every texture references an existing image and, when supplied, an existing sampler. Sampler filters/wrapping modes must belong to the supported glTF core enums.
- Core base-color, metallic-roughness, normal, occlusion and emissive texture slots reference existing textures. Every assigned mesh supplies each effective UV set; the current native vertex profile supports `TEXCOORD_0` and `TEXCOORD_1`.
- `KHR_texture_transform.texCoord` overrides the original slot selection. Finite offsets, scales and rotations are retained, including valid negative/zero UV scale values.
- Texture slots in the already supported clearcoat, iridescence, sheen, specular, transmission, volume and anisotropy material extensions receive the same reference/UV checks. Unrelated extension metadata and extras remain unchanged.
- Native vertex color RGB/alpha components stay in `[0,1]`, including RGB sources promoted to RGBA.
- Core numeric factors have correct finite types, dimensions and ranges. In particular, normal scale can be negative and alpha cutoff can exceed one, as allowed by glTF. Boolean values cannot substitute for numbers or indices.
- References use actual non-boolean Python integers, matching the existing native glTF `_index` policy. Contract adapters that admit JSON numerically integral floats must normalize them before invoking this native typed profile. No material-schema/runtime-equivalence expansion is claimed.

The implementation follows Khronos' [material](https://github.com/KhronosGroup/glTF/blob/main/specification/2.0/schema/material.schema.json), [PBR](https://github.com/KhronosGroup/glTF/blob/main/specification/2.0/schema/material.pbrMetallicRoughness.schema.json), [texture-info](https://github.com/KhronosGroup/glTF/blob/main/specification/2.0/schema/textureInfo.schema.json), [normal](https://github.com/KhronosGroup/glTF/blob/main/specification/2.0/schema/material.normalTextureInfo.schema.json), [occlusion](https://github.com/KhronosGroup/glTF/blob/main/specification/2.0/schema/material.occlusionTextureInfo.schema.json) and [sampler](https://github.com/KhronosGroup/glTF/blob/main/specification/2.0/schema/sampler.schema.json) definitions and the existing native profile.

## Evidence and limits

`tests/test_rig_material_validation.py` builds actual binary GLB and embedded-buffer glTF with real PNG bytes, complete vertex arrays, two UV sets and all five core texture slots. Positive cases preserve source material/image/sampler bytes through native storage and GLB/glTF re-export. Negative cases cover malformed core parameters, absent/out-of-range/wrong-type texture/image/sampler/material references, missing UVs on one of multiple primitives, and extension UV overrides. The actual CLI rejects invalid references with `RIG_TEXTURE_REFERENCE` and leaves the requested run absent. Native save/export failures create no output artifact.

The ten new tests join the existing `interoperability` release gate. The retained [verification receipt](../tests/results/native-material-safety-verification.json) binds exact commands, source hashes, observed results and evidence hashes. The final focused/regression run passed 41 tests without skips, including actual Khronos, Blender save/reopen/render and browser integrations on the existing rig fixture. Separate independent Khronos checks of both newly textured export formats report zero errors and zero warnings. Eight baseline in-memory controls reproduce actual admission of invalid bindings under the original validator, while corrected admission rejects them and preserves source bytes.

This validator is not a complete glTF or shader validator. It does not execute PBR shading, validate every extension-specific numeric domain, reconstruct UVs/tangents, establish rendered texture equivalence in Blender, or grant visual/production approval. Existing source bytes, valid extension dictionaries, source origins, optional dependency isolation and all 2D contracts are preserved. Unsupported material modes fail explicitly; future format capabilities require their own tested adapters.
