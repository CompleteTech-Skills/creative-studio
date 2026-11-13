# Rendered material laboratory

`python scripts/lookdev_laboratory.py --blender /absolute/path/to/blender --output NEW_DIRECTORY`

executes a fixed trusted Blender 4.5 Cycles experiment against the canonical
LookDev material adapter. It renders all nine metallic (0, .5, 1) and roughness
(.1, .5, .9) combinations using one unchanged sphere, camera and area emitter.
The output must be a new directory. Errors preserve the job and render log for
inspection. The caller must acquire the host native-validation flock when
running concurrent work in the shared environment.

Retained output includes exact material resources, resolved plans, nine RGBA
images, editable `sweep.blend`, actual shader socket values, Blender version,
settings, command, render duration, source hashes, and file hashes. Images are
engineering fixtures, not production artwork or aesthetic approvals.

The independent image analysis uses the opaque sphere silhouette and reports
display-referred luminance, percentile peak, baseline-subtracted half-height
area and weighted highlight radius. These are measurements of a fixed scene,
not calibrated physical photometry. Paired metallic and roughness renders must
change actual pixels by more than .5/255 average RGB units. The runner verifies
all nine actual shader socket values; it rejects missing cells and flat or empty
signals. The metric unit tests use independent analytic narrow/broad light
fields and empty/constant failure fixtures.

This experiment supplies the nine-cell matrix portion of demonstration B.
The end-point controls require the low-roughness peak to exceed the high-roughness
peak by 5% and high-roughness half-height area to exceed low-roughness area by 10%.
These explicit effect checks apply separately at all three metallic settings;
intermediate values are measured without a stronger monotonicity claim. Actual
images still require visual inspection. Environment-response control is also still
required before demonstration B is complete. Demonstrations A and C–J, portable
roundtrips, cross-engine comparisons, full legacy regressions and artistic
review remain separate obligations of #90. None is implied by this command.
