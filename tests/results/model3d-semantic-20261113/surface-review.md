# Independent Model3D47 surface review

Status: **CLEAN for the exact five source/document hashes below**. No actionable
correctness or regression finding remains in the reviewed surface scope.

Read the complete material, UV and projection modules, all fifteen surface
regression tests and the complete surface reference. Rechecked the relevant
native Geometry/material validator, strict image decoder, camera/ray math,
glTF vertex-color/UV/texture semantics, native exporter and reimport paths.
The reviewer changed no implementation source.

## Confirmed repairs

1. Retained decoded images now charge the native aggregate pixel/memory gate
   before binding a new map. The original three-image 64MiB-budget bypass fails
   with the native texture resource diagnostic and preserves prior inputs.
2. Final packed UV islands are checked at float64 and portable float32 precision,
   including positive area/orientation and configurable relative edge quantization
   error. The valid native 1-by-1e-11 triangle that previously exported collapsed
   UVs now fails explicitly instead of claiming valid portable surface coverage.
3. Thin-gap visibility no longer uses a coarse absolute world-distance cutoff.
   Exact incident target positions are excluded, including seam-duplicated
   corners. Near-coincident nonincident intersections remain unresolved/inferred.
4. Texture validation and binding snapshot the selected bytes/resources before
   strict decoding. A returned texture cannot contain a caller's later replacement
   bytes, and the public validation receipt hashes the exact data that decoded.
   The earlier accepted-undecodable-byte race was independently reproduced before
   correction and then independently shown repaired on this exact source.

## Independent final verification

The final **15-test suite passed in 1.053s**. In the same budgeted interpreter,
independent probes also verified:

- Editing both retained and new caller TextureImage data after strict decoding
  does not change either private returned texture; both returned images decode.
- Editing the caller image bytes and MIME after decoder return does not change
  the receipt's selected-data hash or the decoded MIME.
- All five source/document hashes matched before and after verification.

The suite includes real PNG creation/strict decoding, native textured GLB and
projected-color reimport, analytic UV coordinates, attribute/morph transport,
padded atlas construction, linear multiview blending, actual occlusion and thin
panels, transparent source handling, camera/source mutation snapshots, native
aggregate image budgets, portable UV precision rejection and UV seam integration.

Command environment used the shared `run-budgeted.py` launcher, thread counts of
one, owned home TMPDIR/TMP/TEMP and the explicit isolated workdir. Log:
`model47-surfaces-independent-race-fixed.log` in this evidence directory.
Earlier `model47-texture-race-probe.log` retains the original failing evidence.
An optional duplicate probe job from the earlier review round was cancelled only
while this reviewer's wrapper was confirmed unstarted; it is not counted as passed.

## Scope boundaries

This is a fresh full review of these five additive surface files. It does not
approve the separate semantic editor, central integration, hosted CI, Blender or
browser execution, complete #47/#10 acceptance or artistic identity. Documented
limits remain explicit: parameterized core-PBR approximations, per-triangle atlas
waste, no tangent/external-skin regeneration, vertex rather than dense texture
projection, opaque/two-sided visibility, explicit calibration, numerical budgets,
and inferred fallback for unseen or unresolved vertices.

## Exact reviewed hashes

- `scripts/model3d_materials.py`: `f2c39352252694612df11dcd9f08f7826e5eeb038a5aeea6f8e85af3d4259aa4`
- `scripts/model3d_uv.py`: `f0b3c256f694f4b28ea914f179ecc90941e4c2f02fec809377e13e9a3254badf`
- `scripts/model3d_projection.py`: `66f3a82d1b197e40220b3e9353c505eff8b49832a46adc5858080d6f760707d9`
- `tests/test_model3d_surfaces.py`: `609c0b65e4379a14d026e9bf362820fae397431ac71205dbc8c5cf6060003f8e`
- `references/model3d/surfaces.md`: `3a4f15945239310415a37b2032e14831e6d3f636bee3277cce02a75e95ad6773`
