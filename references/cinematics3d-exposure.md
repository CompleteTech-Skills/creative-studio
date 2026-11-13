# Measured optional auto exposure (246 increment)

`cinematics3d_exposure.measure(root, inventory, source, rgba)` uses the canonical color processor to validate the source profile and obtain straight scene-linear pixels without exposure or a view transform. It measures luminance using the actual profile's OCIO transform to CIE XYZ D65. Unknown color interpretation is rejected by the canonical API rather than guessed from file names. It accepts contiguous floating RGBA arrays of at most 4,194,304 pixels, with an explicit 512 MiB maximum working budget. Source pixels are unchanged.

Metering uses alpha-weighted geometric mean luminance. Fully transparent pixels are excluded; nonpositive visible luminance is counted and floored at 2^-40 for this statistic only. An alpha-weighted histogram covers log2 luminance [-40, 24]; values outside it and visible values above 1 are reported separately. This is a numerical exposure statistic, not a claim that bright scene-linear values are clipped in the source or in the eventual view transform. An all-transparent image proposes zero correction.

`advise(measurement, source_time, authored_stops=..., enabled=False, previous=..., reset=False, ...)` returns a hash-bound state containing authored exposure, bounded automatic correction and their total. Automatic exposure is disabled by default. Callers provide the canonical rational source time (for example the `Fraction` returned by `render3d_contracts.frame_times`); this module introduces no timeline. Temporal adaptation uses an exponential response in seconds and the exact rational time difference. The previous state is serializable for deterministic resume. A backwards seek, duplicate time, changed sequence/policy/switch or gap over one day requires an explicit reset. Shot cuts should explicitly reset metering. Random seeking without retained/replayed state is unsupported.

Authored exposure is retained separately and never rewritten. The combined total must fit the canonical color processor's [-24,24] stop domain. Out-of-domain combinations fail rather than silently clamping an authored animation. Advice applies no pixels and reports zero exposure applications. The media/render consumer must supply its `total_stops` exactly once to `cinematics3d_color.make_plan` on the original unexposed source; the canonical execution receipt records that application. Reusing previously exposed pixels is rejected by canonical source-context validation.

The measurement and state hashes detect accidental stale records; they do not authenticate an untrusted producer. The caller still owns verified native artifact decoding, source hashes and color calibration. The tests execute real OCIO processing and check numerical arrays, alpha association, HDR luminance, rational-clock adaptation, resume and negative boundaries. These tests do not constitute an actual emissive/shadow 3D scene, final decoded silent video or completion of issue 246. Those integrated demonstrations remain with the canonical renderer/media owners.

Run the optional collector explicitly with the pinned color environment:

```bash
python -m unittest discover -s tests/optional -p test_cinematics3d_exposure.py -v
```
