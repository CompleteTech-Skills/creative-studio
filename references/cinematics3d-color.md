# Explicit OCIO color processing (246 increment)

This optional module supplies a shared color profile inventory, a worker-safe verifier, and actual PyOpenColorIO CPU processing. It does not render, decode native images, encode videos, certify a source's color interpretation, or close issues 246/247/238. The renderer owns active-config selection and source provenance; media adapters own decoded-frame binding and final quantization/metadata.

Install `requirements-color.txt` in an optional environment. NumPy 2.3.5 agrees with the existing rig dependency. Core 2D installations do not import these dependencies. PyOpenColorIO 2.5.2 is pinned and checked at runtime; its version is recorded separately from any renderer's internal OCIO version.

## Shared API

`capture_config(root, config_path='config.ocio')` admits a closed profile directory. It returns the exact config hash, sorted file inventory, declared roles, resolved file-transform references and `closure_sha256`. The closure includes all admitted profile files, including unused LUTs; changing any file invalidates the binding. Config/YAML parsing requires optional PyYAML. Aliases, duplicate YAML keys, environment substitutions, symlinks, external/ambiguous paths, unbounded declared LUT allocations and unsupported file formats fail closed.

Current profiles contain one or more `.ocio` files and bounded flat `.cube`, `.spi1d` and `.spimtx` resources. Only the selected config is interpreted. Referenced file transforms must resolve to a flat LUT inside the inventory. File names use a narrower portable ASCII subset than ordinary project paths. There are at most 256 files, 64 directories, 32 MiB per resource, 64 MiB total, and 1 MiB for the selected config. CUBE allocation is bounded to 65 cubed or 65,536 1D samples; SPI1D is bounded to 65,536 samples. Nested CLF/CTF, SPI3D, dynamic contexts and arbitrary resource resolvers are unsupported.

`verify_config_bytes(root, inventory)` (`verify_config` alias) verifies an already host-admitted inventory. It uses only Python's standard library and the existing lightweight `rig_errors` module: no Pillow, PyYAML, NumPy or PyOpenColorIO import. It is suitable for Blender's worker Python. This verifier checks exact regular-file bytes and the entire closed directory inventory; it does not independently authenticate the inventory's producer or re-interpret YAML. `snapshot_config(root, inventory)` is a context manager yielding a private closed profile directory. Set the worker's `OCIO` variable explicitly to `directory / inventory['config_path']`, and verify before/after execution. No function calls `GetCurrentConfig()` or changes the process-global OCIO configuration.

`inspect_config(root, inventory)` loads that private profile through actual OCIO and reports available roles, color spaces, displays, views and looks. `probe_scene_linear(root, inventory)` independently measures the config's scene-linear RGB basis through its declared CIE XYZ D65 interchange role. These numerical chromaticities describe the selected config, not an inference from an EXR's float pixels or file name. The probe explicitly reports `source_render_verified: false`.

`make_plan(root, inventory, source_context, ...)` selects a real discovered transform. A source context contains:

```json
{
  "kind": "cinematics-color-source", "schema_version": 1,
  "config_closure_sha256": "<actual closed-profile hash>",
  "working_space": "<actual scene_linear role value>",
  "primaries": {"r": [0.64, 0.33], "g": [0.30, 0.60], "b": [0.15, 0.06], "w": [0.3127, 0.3290]},
  "transfer": "linear", "alpha_mode": "straight",
  "view_applied": false, "exposure_applied": false,
  "source_manifest_sha256": "<verified native result manifest hash>",
  "source_artifact_sha256": "<verified native image hash>"
}
```

The Rec.709 numbers are an explicit declaration in this example, not a default for unknown native outputs. The plan verifies them against the actual pinned config's XYZ transform, including linearity checks. A config's role/space names alone are insufficient. Missing, mismatched or unknown primaries, alpha association, prior view or exposure state are not silently filled in. Callers must establish these properties from canonical renderer evidence. EXR `linear` labels and a requested RGBA output do not establish gamut, straight/premultiplied association or whether exposure was baked.

Targets are `sdr-srgb` and `scene-linear`. SDR requires a discovered sRGB display and a supported Standard, AgX or Filmic view. A separate reference calculation verifies the profile's Standard display against XYZ-to-Rec.709 and the sRGB transfer curve before an SDR label is emitted. An explicit named look is applied once before the view; implicit view looks are bypassed. Linear intermediates require empty display/view/look fields. PQ, HLG, HDR video and unrelated output encodings are unsupported.

`transform_pixels(root, plan, rgba, max_work_bytes=...)` consumes a native-contiguous floating `N×4` or `H×W×4` NumPy array and returns float32 pixels plus a hash-bound execution receipt. It revalidates the plan against copied profile bytes. Alpha must lie in [0,1]. Premultiplied RGB is unassociated before exposure/view processing and is reassociated only when requested. Nonzero RGB at zero alpha is rejected for premultiplied input. RGB is not implicitly clipped; HDR values above 1 survive a linear plan. Exposure is applied once, with finite checks; 0 stops is an explicit identity multiplication. The processor works in bounded chunks. Float64 input is deliberately processed at float32 precision and this conversion is recorded.

The receipt identifies input/output pixels, shape, dtype, config closure, source context, actual OCIO processor, exposure and output color semantics. Its `source_render_verified: false` is intentional: a numerical transform cannot prove the caller's array was decoded from its claimed source. The media adapter must first call canonical `verify_result`/`verify_image`, bind each decoded frame to the context's source hashes, then retain the color execution receipt. Float output may exceed [0,1]; any SDR clamp, quantization, alpha flattening or chroma encoding must be explicit in that adapter and independently checked. Original HDR/alpha masters remain unchanged.

## Renderer integration boundary

The current quality-owner draft locator is `manifest.color_context = {path, sha256, bytes}` for a `render3d-color-context-sequence` record. It contains exact per-frame worker display/view/look/exposure/gamma and output settings. `manifest.color_config = {root, inventory}` carries the copied profile. These companion artifacts must be retained by jobs, cache/archive and media consumers. Numeric primaries and alpha/baked-state may remain unknown until a controlled native calibration proves them; this module does not convert those unknown statuses into source acceptance.

The renderer's quality binding incorporates this closure digest together with plan/helper identities. This module introduces no competing renderer, grading registry, backend identity field or timeline. LUT grading and curve controls under 247 remain separate work; flat LUT admission here protects OCIO profile loading and is not a completed artist-facing LUT workflow.

## Commands and evidence

```bash
python scripts/cinematics3d_color.py capture --root /path/to/closed/profile
python scripts/cinematics3d_color.py inspect --root /path/to/closed/profile
python scripts/cinematics3d_color.py probe-scene-linear --root /path/to/closed/profile
python -m unittest discover -s tests/optional -p test_cinematics3d_color.py -v
```

Optional tests are discovered explicitly, keeping missing optional libraries out of required 2D test accounting. The committed small profile is a synthetic engineering fixture. Retained Blender-profile numeric proof exercises the real bundled config and real OCIO processors; it is not a 3D render, visual approval, full color-pipeline acceptance, or final video evidence. Actual emissive/shadow renders, source alpha/baked-state calibration, final decoded video checks, auto-exposure/stabilization and broader 247 grading remain open.

The API follows the official [OCIO Python usage](https://opencolorio.readthedocs.io/en/latest/guides/developing/usage_examples.html), [explicit Config loading](https://opencolorio.readthedocs.io/en/latest/api/config.html), and [DisplayViewTransform/looks-bypass semantics](https://opencolorio.readthedocs.io/en/v2.5.0/api/transforms.html).
