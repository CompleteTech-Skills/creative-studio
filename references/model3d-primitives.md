# Bounded Model3D primitive foundation (#44)

`scripts/model3d_geometry.py` provides `box`, `sphere`, and `cylinder` as
Python helpers returning the existing `rig_models.Mesh`. Supply a nonempty
semantic `name`; both mesh ID and exported object name preserve it. Wrap a mesh
in `Geometry([mesh], materials=[{}])` (or provide your own material table) for
analysis and `rig_gltf.export_geometry`. No alternate mesh format or importer
is introduced.

All shapes are centered at the origin in the established right-handed +Y-up,
+Z-forward, meter basis. Dimensions/radii must be finite numbers from 0.0001 to
10000 meters. Booleans are rejected. Segment counts are actual Python integers
from 3 to 256; sphere latitude subdivisions (`rings`) range from 2 to 256.
These caps bound the largest sphere to 65,282 vertices and 130,560 triangles
before allocation. The helpers have no file, network, provider or job side effects.

Surfaces are indexed, closed and outward wound. Sphere poles are unique;
no zero-area pole triangles are emitted. Normals use the shared area-weighted
vertex-normal implementation. Box corners and cylinder rims deliberately share
vertices and therefore have smooth shading; hard edges and UV seams are outside
this foundation. Tessellation controls approximation, not exact spherical bounds
for every odd segment count. These are procedural engineering fixtures, not
reviewed production artwork.

Validation: `python -m unittest discover -s tests -p test_model3d_geometry.py`
checks deterministic coordinates/indices, dimensions, minimal subdivisions,
closed connected manifold topology, positive volume, normals, invalid inputs,
and actual GLB export/reimport with names and coordinates retained.
`test_rig_math.py` supplies the unchanged shared math regression baseline.

This is a bounded part of #44 task 1. General editing, basis/pivot conversion,
new import/security behavior, hard-edge shading, UVs and full #44 completion
remain outside this change; existing import/export owners remain authoritative.
