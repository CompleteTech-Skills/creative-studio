# Non-destructive scene-linear grading increment (247)

`scripts/cinematics3d_grading.py` processes actual admitted pixels. It consumes the existing `cinematics3d_color` CUBE admission, optional pinned OCIO runtime and source-context validation. It introduces no renderer, media decoder, color-space converter or animation timeline. This increment does not complete issue 247 or parent 238: native multi-shot rendered charts, decoded silent-video continuity, artist controls, matching tools and release integration remain required.

`make_grade(working_space=..., operations=[...], source_id=..., scope='shot', intensity=1)` captures a hash-bound immutable-by-validation document. An operation has exactly `{id, enabled, kind, parameters}`. Disabling an operation or changing intensity produces a new document; source pixels remain unchanged, permitting regrading without rendering geometry. Scene and shot grades are separately declared; the timeline owner supplies sampled intensity. This module neither interpolates tracks nor chooses inheritance order.

`apply_grade(lut_root, grade, rgba, source_context=..., mask=None)` accepts native contiguous float32/64 straight scene-linear RGBA arrays with explicit canonical `cinematics-color-source` provenance. Rec.709/D65 primaries are required because luminance controls use those coefficients. Names alone are insufficient. As in canonical color processing, source values must fit the finite float32 range even when the container dtype is float64; this is checked before any grade operation or LUT conversion. Other primaries and premultiplied pixels require an explicit canonical color adapter first. The context is checked and its hash retained, but it remains the consumer's responsibility to verify the claimed rendered source and decoded pixels. Receipts state `source_render_verified: false`.

Operations execute in document order:

| Kind | Parameters and numerical semantics |
| --- | --- |
| exposure | `stops`: RGB multiplied by 2 raised to stops |
| lift-gamma-gain | three-channel `lift`, `gamma`, `gain`: signed power of RGB + lift × (1 − RGB), exponent 1/gamma, then gain |
| contrast | `contrast`, `pivot`: affine contrast about explicit scene-linear pivot |
| saturation | `amount`: luminance plus amount × chromatic difference |
| vibrance | `amount`: saturation-weighted chromatic adjustment, weaker on already saturated pixels |
| temperature-tint | `temperature`, `tint`: explicit RGB balance in stops; this is an artistic control, not Kelvin chromatic adaptation |
| hue | `degrees`: Rodrigues rotation around the neutral RGB axis; neutral grays remain neutral |
| channel-matrix | three-by-three `matrix` and three-channel `offset` |
| tonal-balance | three-channel `shadows`, `midtones`, `highlights` balances in stops, with bounded luminance weights |
| curves | `rgb`: three ordered piecewise-linear curves, 2–64 control pairs each; unit-slope extension outside domains preserves HDR differences |
| lut | `inventory`: admitted versioned CUBE record |

The optional mask is a finite floating array matching the pixel dimensions, bounded to [0,1]. It controls blending between the untouched input and the complete grade, multiplied by grade intensity. It does not invent or infer an object/semantic matte. Alpha is carried through the explicit final float32 conversion without grading. Input/output dtypes are retained in the receipt; RGB is never silently clipped to SDR. Overflow and unsupported inputs fail before publication.

`import_cube(root, path, working_space=..., interpolation='linear')` admits bounded `.cube` 1D/3D LUT bytes through the existing color validator. Its record includes source path, exact bytes/hash, dimensions, grid, domain, same input/output working-space declaration and interpolation. LUTs requiring color-space changes are rejected. Actual LUT application uses OCIO's file processor on a private copy of the admitted bytes. Domain clamping is the explicit CUBE/OCIO behavior; use non-LUT curves when unit-slope HDR extension is needed. Changing the original LUT invalidates the grade rather than silently executing a newer look.

At most 32 operations and the existing color pixel/byte ceilings are admitted. Work preflight accounts for input, bounded pixel intermediates and a conservative LUT parser allowance of 64 times the recorded LUT byte count plus 64 KiB per enabled LUT before processing. A LUT is reopened with its recorded byte ceiling and its exact length/hash is checked before parsing; stale larger replacements cannot expand the admitted allocation. This is a working-buffer allowance, not an operating-system process memory limit. Runtime records are validated before allocation. Disabled operations still require valid documents.

Execution receipts bind the grade, source context, input/output pixels and mask. Measured diagnostics include histogram over [0,1], below-zero/above-one channel counts, source-neutral spread, luminance and mean absolute differences. These measurements support inspection; they do not provide aesthetic approval, automatic shot matching, a UI preview or a rendered-artifact provenance claim.

The media owner must bind the grade execution to the canonical final color transform, quantization and decoded video. The final view/output transform must execute exactly once through the canonical color authority. The current native-source context rejects already applied exposure; do not pass an original context unchanged after a grade applies exposure or changes pixels. A typed processed-source chain binding grade input/output hashes and effective exposure state remains an integration prerequisite. The standalone grade receipt does not authorize relabeling processed pixels as untouched native output. Renderer/scene-linear calibration, actual multi-shot before/after evidence, mask provenance, histogram UI, grade matching and final video pixel verification remain open.

Run the optional numeric tests with the pinned color environment:

```bash
python -m unittest discover -s tests/optional -p test_cinematics3d_grading.py -v
```

The tests execute real OCIO 1D/3D CUBE processors and compare output against independent numeric references. They use engineering pixel arrays, not rendered acceptance scenes or visual approval.

Measured Rec.709/D65 source chromaticities are admitted unchanged within 5e-5 per xy coordinate, matching canonical color calibration tolerance. This accommodates actual OCIO basis measurements without replacing provenance values with rounded constants. Materially different or unknown gamuts remain rejected.
