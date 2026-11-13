# Executable styles and authored look state

`lookdev_styles` resolves twelve existing artistic IDs through the authoritative
style registry and freezes source instructions plus executable recipe hashes.
It never changes existing 2D style resolution. `material_resource` creates an
ordinary material3d resource for the PBR owner. `execute_mapping` applies this
through a supplied real material builder, isolates shared mesh datablocks, sets
flat/smooth faces and installs real grain/noise roughness and bump nodes.
Glass rejects open geometry. Stained glass requires a separate authored frame;
foil requires real angular folds. No missing geometry is invented.

`bind_identity` binds explicit object/slot targets to exact Registry material
references. Protected fields are checked against the original effective surface
when selecting a replacement. This is a semantic authoring boundary; a consumer
must still validate targets against its imported mesh slot table. It does not
infer anatomical assignments or establish visual identity approval.

`lookdev_color.configuration` freezes scene-linear sRGB shading, a Blender OCIO
view, exposure in stops, Kelvin/tint white balance and output encoding. Applying
it uses Blender4.5 view settings, never rewrites physical lights or source
textures, and rejects unavailable views. PNG is a 16-bit display transformed
sRGB image; 32-bit EXR is scene-linear and does not bake display exposure, white
balance or tone mapping. Source texture encoding remains the texture owner's
responsibility. Standard is reference mapping; AgX and Filmic are supported only
when present in the actual OCIO configuration. ACES and browser equivalence are
not implemented by this module.

`lookdev_tracks` evaluates integer-frame scalar/vector/scene-linear color,
normalized XYZW orientation and discrete tracks without prior frame state.
Step switches remain discrete; linear/smooth quaternion paths call native rig
slerp. Smooth is smoothstep easing. Cubic calls the native rig CUBICSPLINE
evaluator with explicit incoming/outgoing derivatives in value units per frame;
this is Hermite interpolation (equivalent to explicitly derived Bézier handles).
Property paths must already exist, duplicate/overlapping tracks reject, and
continuity locks cover exact paths or complete namespaces. `shot_state` always
copies authored base state, preventing one shot's changes from leaking.

Run pure checks with the optional rig Python:

```
python -m unittest discover -s tests -p 'test_lookdev*py' -v
```

`tools/lookdev/styles_probe.py` is a trusted opt-in Blender fixture accepting a
job path after `--`. The job identifies immutable snapshot script directories
and a new output directory. Run with `--factory-startup --python-use-system-env
--threads 1`, using the pinned Blender4.5 and optional Python3.11 jsonschema
dependencies. It executes every recipe with actual geometry under Cycles,
retains rendered PNGs, mapping/source hashes and OCIO comparison PNG/EXR files.
Use the shared native flock when running on the coordination host.

This package does not complete #88: browser color/shading comparison, ACES-inspired
mapping, seeded flicker events, automated anatomy
assignment, native timeline/Scene3D slot publication, and complete character
pose/shot visual acceptance still require their owning adapters and evidence.
Numerical tests and synthetic style spheres do not establish production artwork
or cross-engine equivalence.
