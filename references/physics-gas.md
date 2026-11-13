# Native gas simulation

`physics_gas.GasBackend` is the gas adapter alongside the existing rigid-body
Blender/PyBullet adapters. It consumes the same canonical PhysicsWorld, exact
Scene3D source closure, actual OBJ geometry, coordinates and resource budgets.
Original source bytes are copied without rewriting. Its fixed package worker
constructs a fresh Blender scene; uploaded Blender projects and Python are not
accepted as simulation inputs.

The bounded profile is one cubic gas domain, static sphere/box source meshes,
static obstacles and constant wind. Turbulence is not yet admitted. It supports smoke or combustion,
source density and temperature, fuel, dissipation, vorticity, buoyancy, collision
distance, and native adaptive CFL/substep controls. Rigid-body iterations and
fixed stepping do not describe Mantaflow: the world must declare one output
interval, while GasSettings separately declare the actual adaptive solver policy.
The current output clock admits integer frame rates from 1 to 240 fps and checks
the native readback exactly. Fractional output frame rates remain unsupported.
Animated emitters, arbitrary source shapes, moving obstacles, multiple domains,
two-way coupling and continuation after interruption remain unsupported.

Density is converted to the Blender source's dimensionless density using the
declared `reference_density` in kg/m³; the admitted ratio is greater than zero
and at most one to match the native source range. Source and obstacle settings
are read back from Blender and compared with the request. Source temperature in kelvin is converted
relative to `ambient_temperature`, divided by `temperature_scale` in kelvin.
These are explicit appearance/solver mappings, not a claim of physically
calibrated combustion chemistry. Native buoyancy and combustion controls retain
their Blender meanings. All source/solver settings and actual backend identities
are retained with the output.

## Dependencies and execution

Install the ordinary optional rig dependencies. Set `HATCH_BLENDER` to the tested
Blender 4.5.3 LTS executable. Set `HATCH_OPENVDB_PYTHON` to a separate interpreter
with OpenVDB Python bindings. The current reader admission is OpenVDB 13; it is
isolated from ordinary 2D and rig environments. Neither ordinary 2D installation
nor import of the gas module loads Blender, OpenVDB, NumPy or Pillow.

```python
from physics_gas import GasBackend
from physics_gas_example import build_gas_inputs

world, exact_native_sources, settings = build_gas_inputs(fire=True)
backend = GasBackend()
prepared = backend.prepare(world, exact_native_sources, settings=settings)
try:
    result = backend.simulate(prepared)
    report = backend.bake(result, "output/new-gas-cache")
finally:
    backend.cleanup(prepared)
```

The CLI provides `validate`, `simulate` and `verify`:

```bash
python scripts/physics_gas.py validate world.json --settings gas-settings.json
python scripts/physics_gas.py simulate world.json --settings gas-settings.json \
  --source-root native-sources --destination output/new-gas-cache
python scripts/physics_gas.py verify output/new-gas-cache
```

`probe(smoke=False)` establishes executable and reader discovery only.
`probe(smoke=True)` must execute both smoke and fire, decode the actual caches,
reopen the generated native project and compare rendered pixels before claiming
readiness. Successful configuration alone never establishes readiness.

## Cache and validation boundaries

The native worker bakes a full OpenVDB frame sequence. A separate OpenVDB process
decodes actual density, flame, temperature and velocity grids. Header and expanded
voxel/memory bounds are enforced before/while inspecting fields. Finite values,
frame inventory, request identity and actual field contents are verified; fire
requires nonzero flame and both modes require nonzero density. A fresh Blender
process reopens the generated `.blend` and rerenders selected frames in reverse
order. Pixel comparison establishes consumed baked replay for those frames.

The initial development probe exposed a Blender 4.5.3 crash when iterating the
RNA `density_grid` after baking. The adapter avoids that interface and uses the
independent OpenVDB decoder. A crashed worker, incomplete cache, cancellation or
timeout does not publish a successful result.

`verify` checks the complete byte inventory, native source closure, strict
settings, required native/replay artifacts, decoded field records and pixel
comparisons. It does not execute a solver or authenticate an externally supplied
receipt. Caches are unsigned consistency evidence; changing all files and hashes
does not make an external record trusted provenance. Physics fixtures and their
renders are engineering examples, not production artwork approval.

Publication stages a verified complete output and uses the existing exclusive
no-replace primitive. Existing files, directories and racing destinations survive.
Originals and failure evidence are retained until their owning attempt is cleaned
up. A cancelled/failed attempt needs a fresh preparation; no hidden resumability
or arbitrary continuation is claimed.
