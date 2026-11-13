# Camera contract engineering fixtures

These files are original procedural **contract fixtures**, not rendered artwork,
actual camera-animation footage, or production review evidence. `main-camera.json`
uses a 50 mm perspective camera at the identity transform; `optics.json` and
`render.json` define its physical gate and 360×240 raster intent. `registry.json`
pins their exact original bytes.

```bash
python scripts/camera3d_contracts.py --validate examples/camera3d/main-camera.json
python scripts/camera3d_contracts.py --validate examples/camera3d/registry.json
```

The first command validates the camera document and explicitly reports unresolved
references. The registry command additionally validates the actual three files'
SHA-256 values, identities and complete exact-reference closure. Neither command
renders an image or implies Blender/browser execution. See
[contract semantics](../../references/camera3d-contracts.md) for the ownership,
coordinate, timing and downstream execution boundaries.
