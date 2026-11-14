# Color processing numeric evidence

Run the synthetic, artwork-free numeric patch example against an explicitly selected closed OCIO profile:

```bash
python -m pip install -r requirements-color-test.txt
python scripts/cinematics3d_color_example.py --profile /path/to/profile --output NEW_DIR --retain-profile
```

The example executes real OCIO processors, preserves float `.npy` masters and exact config/LUT bytes when requested, and writes explicitly clamped 8-bit patch previews. It proves numerical execution and context binding. It is not a rendered 3D scene, source-gamut/alpha authentication, artistic approval, or completion of issues 246/247/238. Native alpha and baked-exposure/view state must come from canonical renderer calibration; this example does not infer them from EXR pixels.

The committed tiny OCIO profile under `tests/fixtures/cinematics-color/profile` is a synthetic engineering reference for portable tests. The separate Blender-profile proof records the exact installed profile closure and producer versions. See the retained receipt for source and artifact hashes and evidence boundaries.
