# Import native 3D frames into an existing 2D run

`rig_2d_bridge.py` renders actual evaluated native mesh triangles to transparent
PNG sources and hands those sources to the existing `import-source` workflow.
It does not modify the fixed Codex atlas, generate missing animations, approve
artwork, or bypass the ordinary 2D extraction, validation, review and packaging
gates.

Prepare the destination with the existing `hatch.py prepare` command and an
explicit profile. Use that run's exact `profile.json` for the bridge. The run's
fixture label must match the rig: procedural engineering fixtures cannot become
production artwork by importing them.

First render and import a canonical base. Replace the SHA argument with the
current `rig_sha256` from `python scripts/rig.py status --run-dir RIG`:

```bash
python scripts/rig_2d_bridge.py render --rig RIG --destination OUTPUT/base \
  --profile RUN/profile.json --job base --clip neutral \
  --expected-rig-sha256 RIG_SHA --camera three-quarter
python scripts/rig_2d_bridge.py import --rig RIG --bridge OUTPUT/base \
  --run RUN --expected-sha256 BRIDGE_SHA --note 'Description of the supplied source'
```

`render` returns the exact `BRIDGE_SHA` for `bridge.json`. Inspect the rendered
base and record a real `hatch.py base-review` before importing a state. The bridge
never records that review. Then render an explicitly selected profile state,
using a clip that actually exists:

```bash
python scripts/rig_2d_bridge.py render --rig RIG --destination OUTPUT/state \
  --profile RUN/profile.json --job STATE_ID --clip CLIP_ID \
  --expected-rig-sha256 RIG_SHA --camera three-quarter
python scripts/rig_2d_bridge.py verify --rig RIG --bridge OUTPUT/state \
  --expected-sha256 BRIDGE_SHA
python scripts/rig_2d_bridge.py import --rig RIG --bridge OUTPUT/state \
  --run RUN --expected-sha256 BRIDGE_SHA --note 'Description of the supplied sequence'
```

State samples occur at the cumulative profile durations. For durations
`[40,70,90]` milliseconds, the sample times are exactly `0`, `1/25` and `11/100`
seconds. The whole profile interval must fit the selected clip. The bridge does
not compress the clip or change profile timing. `--start-time` accepts a bounded
nonnegative decimal or rational string. All samples share one viewport and
bounds computed over the selected motion, so individual poses are not resized.

Outputs contain the exact profile, source rig manifest, clip, ordered PNGs,
legacy import descriptor and bridge manifest. The manifest binds all file hashes,
source/rig/clip identities, camera, bounds, timing, fixture label and renderer
implementation. Import rejects changed source revisions, profiles or frame bytes,
then uses private verified captures while the legacy importer opens the files.
The 2D import receipt retains the complete bridge manifest and original frame
hashes, so the lineage survives the existing archive/export workflow.

The renderer is the existing native CPU triangle z-buffer: it uses material base
colors, a fixed ambient/diffuse light and an sRGB output transfer. It does not
claim texture shading, physically based rendering or Blender equivalence.
`verify` checks exact artifact/source bindings and decoded image dimensions; it
is not a visual verdict or an independent re-render. Subjective identity, motion,
style and extraction quality still require the normal review workflow.

The bridge enforces the existing combined two-million-pixel 2D source ceiling,
native frame dimensions and rig memory limits. Each render needs a new output
directory and publishes through an exclusive rename. Failure leaves existing or
racing destinations untouched. No image model, audio track, installation or
external application is invoked.
