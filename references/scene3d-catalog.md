# Editable environment geometry catalog

`scene3d_environment.plan` accepts `generator: "template"` with parameters
`preset`, optional uniform `scale` (0.1–10), and integer `detail` (6–32).
Recipe version 1, a 32-bit seed, and an output node budget remain mandatory.
Template recipe IDs are limited to 24 characters so child IDs stay inside the
existing Scene3D identity bounds. `scene3d_catalog.catalog()` exposes independent
input schemas, bounds, node types, versions, and regeneration limits.

Fantasy presets are castle, village, ruins, forest-clearing, dungeon, temple,
magical-laboratory, cavern, fantasy-tower, and mystical-landscape. Science-fiction
presets are spaceship-interior, hangar, corridor, research-facility,
industrial-chamber, control-room, space-station, futuristic-city, landing-pad,
alien-landscape, and modular-technical-structure.

Each preset compiles into ordinary editable triangle-mesh nodes through the
existing generator. It uses the same hash-bound asset publisher, named surface
material projection, local transform convention, and manual-override/deletion
policies; it does not create another scene model or renderer. A template has at
most 128 nodes, respects the enclosing recipe budget, and stays within its
published base bounds multiplied by uniform scale. Seeds affect terrain,
vegetation distributions, ruins, and city heights deterministically.

Buildings contain actual segmented external door and window openings. The
village has three separate buildings and a hollow well; the castle has an open
gate, four towers and a keep; the forest has a flattened clearing with excluded
vegetation; temples contain real raised platforms, stairs and columns. The
cavern uses a modeled hollow stone tube with genuinely open annular ends and a
separate floor. It is explicitly not a claim that heightfields create caves or
that arbitrary cave networks exist. Space assemblies add editable service pipes,
consoles, hangar geometry, structural modules and landing surfaces.

These are geometric engineering references. Material and lighting assignments,
production artwork approval, collision-aware character placement and integrated
cinematic acceptance remain owned by their shared subsystems. Floating fantasy
platforms are authored geometry and do not claim a physics simulation.

Save and reopen through the same API as other generators:

```python
from scene3d_environment import create_environment
create_environment(destination, {
    "id": "village", "version": 1, "seed": 42,
    "generator": "template", "budget": 128,
    "parameters": {"preset": "village", "scale": 1, "detail": 12}
})
```

The catalog tests build every declared preset as actual bounded meshes, check
coordinate transforms and deterministic scaling, independently prove the cave
entrance stays open, and save/reopen representative project dependency closures.
