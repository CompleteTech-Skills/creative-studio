# Edit named static Model3D parts in Blender

Issue #49 task 2 uses `scripts/model3d_blender.py` to turn a validated
`rig_models.Geometry` into two editable Blender projects and two GLB exports.
The original part ID selects the object. The offset moves its mesh vertices in
canonical +Y-up, +Z-forward meters. The new base color uses linear RGBA.
If two objects share the target material, the target receives its own copy.

Supply a local Blender 4.5 LTS executable through `HATCH_BLENDER` or the
`executable` argument. The repository's native job pins Blender 4.5.3.
Use a new destination inside a trusted, existing directory on a filesystem
supporting exclusive rename. Under WSL, use the Linux filesystem for the
destination. Blender receives only the generated source GLB and the two
projects it writes under the private working directory.

With `scripts` on the Python import path, an existing geometry owner calls:

```python
from model3d_blender import roundtrip_named_part

receipt = roundtrip_named_part(
    geometry, destination,
    part="fin", offset_m=[0.125, 0.25, -0.5],
    base_color=[0.25, 0.125, 0.75, 1], fixture=True,
)
```

Set `fixture` from the geometry's actual provenance. The source GLB carries
that classification in its asset extras. Each Blender object and both
reopened exports carry it as the `hatchFixture` custom property.

| Artifact | How it is produced and checked |
| --- | --- |
| `source.glb` | The existing rig geometry encoder writes all named parts and materials. |
| `source.blend` | Blender imports the generated GLB and saves the first project. |
| `reopened-source.glb` | A fresh Blender process opens the first project and exports the pre-edit state. |
| `edited.blend` | The same process changes the named mesh vertices and shader base color, then saves to a new project. |
| `edited.glb` | A third Blender process opens the edited project and exports its scene. |
| `receipt.json` | The host independently decodes both reopened GLBs and verifies all parts, coordinates, oriented faces, material names, RGBAs, roughness and metallic factors. It records input and output SHA-256 values and the effective edit. |

The geometry profile supports at most 64 named triangle parts, 16,384 vertices
and 32,768 triangles in total. It accepts static positions within 10,000 meters,
with distinct float32 positions and no unused vertices per part. Object and mesh
names must fit in 63 UTF-8 bytes and be unique within their Blender namespace.
All material slots must be used. They support opaque, untextured base color,
metallic and roughness factors. The shared-material copy uses the available
name `edited.<part ID>`. Source-authored normals, UVs, textures, morphs, skins,
animation, scene hierarchies, optimization and review have other owners.
The geometry check uses the float32 interchange positions. It permits
exporter vertex renumbering and normal splits while checking every oriented
triangle and its owning part. The color and scalar material tolerance is 1e-6.

Run the two-part synthetic fixture and its boundary controls from repository
root, with the rig Python requirements available:

```sh
PYTHONPATH=tests:scripts HATCH_RIG_BLENDER_TEST=1 HATCH_BLENDER=/path/to/blender \
  python -m unittest -v test_model3d_blender
```

The native tests exercise both shared and private materials. The controls
reject a missing target, invalid edit vectors, unsupported attributes, an
unchanged edit and an existing destination. They also exercise missing-runtime
cleanup and challenge the verifier with pre-edit geometry, reversed winding
and an incorrect sibling material. These checks cover the named-part authoring
slice. Full #48 integration and #49's provider, multiformat export validation
and interactive review requirements remain open.
