# Native lighting adapter

`lookdev_lighting.py` resolves existing `light3d` and `lighting-rig3d` references and
constructs real Blender 4.5 LTS light objects or a packed world shader. Scene,
render, cache and publication lifecycles remain owned by Render3D. Importing this
module does not import Blender, NumPy or Pillow. Plans retain exact resource hashes
and do not mutate source documents or existing materials.

## Mapping and units

The canonical world is right-handed +Y up, +Z forward, in meters. Light optical
axes use local -Z. The adapter reuses the authoritative `rig_math` and
`camera3d_math.blender_camera_transform` conversion rather than introducing a
second coordinate system.

Point and spot light intensity in candela maps to Blender normalized power as
`4*pi*candela/683`. Directional illuminance in lux maps to normalized Blender sun
irradiance as `lux/683`. Area intensity is already radiant watts and is passed to
normalized finite emitters. The constant 683 lm/W specifies a **555 nm equivalent
photometric convention**, not an assertion that a broadband lamp has this luminous
efficacy. Blender's RGB shading does not model a full spectrum. The source RGB
chromaticity is transformed from sRGB when declared, then normalized to unit
photopic luminance for candela/lux lights; that preserves the authored luminance
under color changes. Area watts preserve linear RGB values without this
normalization. Emitted electrical wattage is never inferred.

The backend supports point and spot source radius, spot inner/outer cone angles,
rectangle/disk/ellipse dimensions, and sun angular diameter. Spot blending is
computed in cosine space, matching the native cone falloff parameter. Local lights
retain native inverse-square distance attenuation. No artist-defined falloff is
silently substituted. See the [Blender 4.5 light documentation](https://docs.blender.org/manual/en/4.5/render/lights/light_object.html)
for the backend energy/normalization convention.

`temperature_color(kelvin)` integrates a Planck spectrum from 380 to 780 nm at
5 nm intervals using the analytic CIE matching-function approximation described
by [Wyman, Sloan and Shirley](https://jcgt.org/published/0002/02/01/).
The resulting linear-sRGB chromaticity is gamut clipped, so it is explicitly a
blackbody-inspired RGB approximation. Supported values are 1000–40000 K. Execution
options choose `multiply`, `replace` or `ignore`; multiply is the default when a
source declares a temperature. Tint is linear RGB. Neither conversion changes the
canonical resource or approved material palette.

## Public functions

- `light_plan(registry, reference, engine='CYCLES', options=None)` resolves a hash-bound
  light and preflights compatibility and numerical limits.
- `build_light(plan, collection=None, receivers=None)` constructs a real local light.
  `receivers` maps stable scene IDs to existing objects. Receiver include/exclude
  lists use a private native Cycles linking collection. Unknown identities fail
  before light creation. Failed construction removes its own new datablocks.
- `build_environment(plan, payload=bytes)` validates the exact payload hash/size,
  decodes it through `lookdev_textures.decode_texture`, and creates a new packed
  float image/world. It returns that world; it does not overwrite `scene.world`.
- `rig_plan(registry, reference, options=None, max_lights=64)` preflights all lights,
  bounds the count and rejects more than one authoritative environment.
- `lighting_preset(name)` returns source documents for one of sixteen deterministic
  versioned rigs. `preset_names()` enumerates them. Presets change lights only;
  toon quantization and clay surfaces stay with the material/style owner.

Execution options are an explicit versioned adapter companion, not silent new
fields in existing `light3d` documents. They include enablement, temperature mode,
tint, diffuse/specular/volume contribution, native receiver include/exclude IDs,
area shape, sun angle, and environment exposure/background visibility. Unknown
fields, inappropriate light-type options and unsupported EEVEE linking fail.

## Environment and shadows

Environment input is a 2:1 equirectangular scene-linear HDR image. Exact original
bytes remain the texture owner's responsibility; this adapter carries their hash,
size, dimensions and resource provenance. It never loads external paths or URLs.
Float pixels are decoded with bounded texture admission, flipped from the
image convention to Blender's image origin, and packed into a new Blender image.
World rotation and exposure are explicit and contribute to plan identity.
Camera background visibility is separated from diffuse/reflection illumination.

Cycles performs native importance sampling, path-traced geometric shadows and
indirect illumination; it does not substitute an AO texture for GI. EEVEE local
light objects are supported, while unrestricted path tracing and identical
transmission/GI are not claimed. Browser lighting, browser HDR prefiltering,
shadow-map controls and browser/Blender comparisons remain with the preview
integration work. OpenEXR environment decoding awaits the texture owner's bounded
EXR decoder. Radiance HDR decoding is available. These boundaries keep #87 open
until its complete integration acceptance is verified.

## Verification

Pure admission and preset checks:

```bash
python -m unittest discover -s tests -p test_rig_lookdev_lighting.py -v
```

Required native evidence (new destination; run under the repository's serialized
native validation queue when sharing a machine):

```bash
PYTHONPATH=/path/to/optional-python3.11-site-packages \
blender --background --factory-startup --disable-autoexec --python-use-system-env --threads 2 \
  --python-exit-code 1 --python scripts/verify_lookdev_lighting.py -- output/lighting-evidence
```

The worker creates actual geometry and preserves float EXR and display PNG outputs,
source HDR bytes and measured comparisons. It checks point distance attenuation,
sun translation invariance, spot illumination, finite-area penumbra, diffuse red
interreflection, emissive illumination, rotated HDR response and actual EEVEE
rendering. An opt-in test wrapper runs the same worker when
`HATCH_LOOKDEV_LIGHTING_TEST=1`; enabling it without Blender fails. Technical pixel
checks do not grant aesthetic or production-art approval.
