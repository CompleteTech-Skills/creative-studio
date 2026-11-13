# Actual Render3D acceptance

Run the opt-in CPU fixture suite with the optional rig dependencies, Pillow, OpenEXR and pinned glTF validator configured:

```bash
HATCH_RENDER3D_REAL_TESTS=1 HATCH_BLENDER=/path/to/blender \
HATCH_GLTF_VALIDATOR_ROOT=/path/to/gltf-validator \
PYTHONPATH=scripts:tests python -m unittest test_render3d_acceptance -v
```

Set `HATCH_RENDER3D_ACCEPTANCE_DIR` to retain the generated native source, immutable snapshot, actual output, and decoded contact sheet. Without it, the temporary evidence is cleaned after each test. Serialize native work with the host's configured resource queue.

The three tests render a real three-frame, 64×64 Cycles CPU sequence with a deforming two-triangle rig. They inspect Blender-evaluated vertex movement, independently decode all frames, check measured pixel changes, reject altered/missing output, preserve an existing destination, reject insufficient memory admission, and reject cancellation without publishing partial output. `inspect_render` rechecks the full output and binds bounded PNG previews to verified source bytes. Its pixel-change measurement does not establish the cause of a change or grant visual approval.

On 2026-11-13 the three tests passed with Blender 4.5.3. The retained contact sheet was visually inspected: the narrow white fixture changes angle across the three frames on the dark preview background. It is deliberately minimal engineering geometry. This does not satisfy full #98 acceptance: multi-shot/120-frame delivery, scheduler retry/recovery/cache reuse, silent-video validation, and complete production visual review remain separate requirements.
