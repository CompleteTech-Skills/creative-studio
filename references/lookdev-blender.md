# Executable Blender surface adapter

`lookdev_blender.py` translates the existing, hash-bound LookDev material registry
into Blender 4.5 LTS shader nodes. The adapter is called inside a trusted worker;
it is not a second scene loader, scheduler, persistence service, or renderer.
Scene and Render3D owners retain those responsibilities. The module imports no
Blender dependency until `probe()`, `build_material()` or `apply_displacement()`
is invoked.

The host resolves an exact reference before it starts Blender:

```python
from lookdev_blender import material_plan
plan = material_plan(registry, exact_material_reference, engine='CYCLES')
```

This validates native inheritance and the effective render policy, then copies
its immutable values and exact source reference into a hash-bound internal plan.
The trusted worker sets the planned engine and calls `build_material(plan)`.
The plan is an internal handoff; external JSON still enters through the canonical
LookDev `Registry`. Inherited materials never mutate the base resource.

## Mappings

The implementation uses Blender's actual Principled BSDF sockets for linear
base color, metallic, roughness, IOR, dielectric reflection strength/tint,
transmission, coat, sheen, anisotropy and emission. Canonical specular strength
1 maps to Blender's neutral IOR Level 0.5. Anisotropy combines the authored 2D
direction and radian rotation before conversion to Blender turns.

Opaque coverage ignores alpha, mask coverage applies its explicit threshold,
and blend coverage retains continuous alpha. Optical transmission remains a
separate shader property. Backfaces on single-sided materials become transparent
through a Geometry/Transparent shader mixture; double-sided surfaces retain the
same shader on both sides.

Subsurface scattering mixes two Principled closures with identical coverage,
reflection and emission properties. One uses actual subsurface transport with
authored color, radius in meters and selected random-walk or Burley method. It is not a diffuse color change. EEVEE does not
receive a strict-physical subsurface or transmission capability from this
adapter. Equal minimum/maximum thin-film thickness and full iridescence weight
map to Principled thin film; variable thickness and partial weight currently
reject rather than silently approximate.

`apply_displacement` evaluates actual subdivision and an image-driven Displace
modifier into new mesh coordinates. It checks vertices, spatial bounds, UVs,
object scale, image size and degenerate faces, and replaces mesh data only after
validation. Existing modifiers are rejected so a material operation cannot bake
away someone else's rig. The caller must resolve and verify the image through
the texture owner first. Global self-intersection detection is not performed;
the geometry owner must validate that requirement for production acceptance.

Textures, shader graphs and volume attenuation currently require their dedicated
execution adapters. A render policy cannot turn their absence into a compatible
flat material. `gltf_material(plan)` emits core factors and supported extension
fields for the existing native geometry exporter; it explicitly rejects native
subsurface scattering because this subset has no corresponding glTF extension.
Returning glTF fields alone does not establish a rendered export roundtrip.

## Verification and limitations

Run ordinary admission tests without Blender:

```bash
python -m unittest discover -s tests -p test_rig_lookdev_blender.py -v
```

Run actual bounded Cycles frames, mesh displacement and fresh-process Blender
reopening with the integration gate:

```bash
HATCH_LOOKDEV_BLENDER_TEST=1 HATCH_BLENDER=/path/to/blender \
  HATCH_LOOKDEV_EVIDENCE=/new/evidence/directory \
  python -m unittest discover -s tests -p test_rig_lookdev_blender.py -v
```

The fixture is self-authored geometry, not production artwork. It measures
shader parameters, nonidentical actual rendered responses, displaced coordinates
and a reopened render. Artist approval, all material laboratory cases, browser
parity, volume absorption and complete scene/texture integration are distinct
acceptance work. Nothing in this document closes parent issue #15.

The socket reference is the [Blender 4.5 Principled BSDF manual](https://docs.blender.org/manual/en/4.5/render/shader_nodes/shader/principled.html)
and [Blender 4.5 RNA documentation](https://docs.blender.org/api/4.5/bpy.types.ShaderNodeBsdfPrincipled.html).
Runtime probing checks the supported version and actual socket names before
allocating a production material.
