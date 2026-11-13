# Technical asset validation

`asset_library_validation.validate_asset(library, reference)` reads an exact
immutable version through the existing `AssetLibrary` resolver and copies its
verified file closure into a private temporary directory. It does not create a
second registry or change publication or review status. Reports bind the exact
manifest digest and each file digest, include the effective resource limits, and
keep `visual_review: unreviewed` and `production_approved: false`.

```python
from asset_library import AssetLibrary
from asset_library_validation import validate_asset

library = AssetLibrary("library")
report = validate_asset(library, exact_reference,
                        requirements={"capabilities": ["mesh", "skin", "animation"]})
```

Use `inspect_geometry(path, limits=...)` for a standalone GLB/glTF/OBJ. It captures
the entry and external resources with the same bounded descriptor reader as the
import pipeline before any native inspection. Its `source_files` digest/size map
identifies the exact captured bytes. Optional 3D dependencies are loaded only by
an actual inspection call; install `requirements-rig.txt` and the pinned
`tools/rig` Node dependencies. `HATCH_GLTF_VALIDATOR_ROOT` can locate the installed
Khronos validator package.

The implementation delegates model decoding, geometry diagnostics, skin and
skeleton checks, animation checks, and Scene3D contracts to their existing owners.
Native rig dependencies also invoke the complete `rig_ik.validate_controllers`
authority, including IK chains and constraints unused by animation clips.
It reports real transformed bounds, triangle/vertex counts, topology warnings,
normal/tangent unit error, UV ranges, material slots, decoded texture dimensions,
morph channels, hierarchy matrices, rest and hierarchy signatures, inverse-bind
and weight digests, and exact rational representations of decoded key times.
GLB/glTF must additionally pass the independent Khronos validator. Native import
FPS is identified as an import default, not an inferred original sampling rate.

For Scene3D scene/prefab closures, every referenced file and typed identity is
checked against the same snapshot. Geometry resource counts, bounds and component
identifiers are compared with actual decoded geometry; node matrices are composed
and deep/cyclic hierarchies fail. Supported embedded native rigs use the rig
owner's full loader and validators. Detached animation documents cannot pass
without an explicit rig binding. Prefabs and environments require the matching
actual scene structures. Asset kinds without an implemented validation adapter
fail explicitly rather than becoming ready from their label.

Limits can only be tightened from the native profile (with up to eight influence
slots for import inspection). The snapshot is capped at 256 registered files and
one quarter of the memory budget as well as the dependency byte limit. No repair
runs automatically. Open boundaries, duplicates and nonmanifold topology are
reported as observations, not universally invalid geometry.

This module does not render previews, grant human approval, preserve unsupported
Blender control systems, or validate arbitrary renderer shaders. OBJ does not
carry skinning or animation. Material/texture inspection is supported through the
implemented native geometry payloads, not arbitrary standalone shader formats.

Run the executable tests with:

```sh
PYTHONPATH=scripts:tests python -m unittest discover -s tests -p test_rig_asset_validation.py -v
```
