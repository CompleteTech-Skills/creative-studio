# Reproduce the six-fixture rig verification

The authoritative executable is `scripts/verify_rig_demos.py`. Its six inputs are
procedural humanoid, quadruped, winged-creature and robotic rigs plus proportion-
retargeted humanoid and quadruped rigs. These are engineering fixtures. Automated
success does not establish artistic approval or complete the parent rig mission.

Use a new destination for every source revision. Install the pinned rig testing,
Blender, browser and Khronos dependencies described in `3d-rigging.md`, then run:

```bash
python scripts/verify_rig_demos.py --destination NEW_DIRECTORY --generate \
  --blender --browser --validator --render --archives --specializations
```

Point `HATCH_BLENDER` at Blender 4.5 LTS and `HATCH_GLTF_VALIDATOR_ROOT` at the
pinned validator installation when they are outside the default locations.
`PLAYWRIGHT_BROWSERS_PATH` must point at the browser cache matching the installed
Playwright version when it uses a separate cache. Use one short, owned temporary
directory for `TMPDIR`, `TMP` and `TEMP`; long Chromium socket paths can fail.
Keep Blender and numerical library threads at one or two. In a coordinated test
host, hold its native validation lock for this mixed Blender/browser/render run.

All six fixtures execute the requested independent Blender save/reopen/sample,
exact-project preview, Three.js playback/vertex and Khronos checks. Retargeted
fixtures use their actual available clip; no neutral animation is fabricated.
The browser verifies reduced motion against actual displayed rest vertices when
no authored neutral clip exists. Native rendering and archive restoration also
run for all six. Individual flags permit narrower runs, and the recorded gate
flags describe what executed.

`--specializations` additionally calls the shared specialization fixture helper.
It derives separate garment, tail and cooperative grip proof closures from the
six inputs. These demonstrate multijoint garment deformation and export, tail
aim and seeded secondary-motion baking, and an actual rendered hand/prop frame.
The helper records source-bound numeric and artifact reports. `--validator` and
`--blender` extend these proofs through their requested export checks. Derived
runs retain their own correct manifests; the six base closures must retain the
same bytes before and after the specialization proofs.

Inspect `acceptance.json`, its exact six fixture names, the per-fixture validation
reports, specialization reports, rendered frames, browser screenshots and archive
hashes. A requested gate failure stops the run before final acceptance is written.
A partial receipt is incomplete evidence. Preserve failed artifacts, fix the
source and use a new destination when the source revision changes.

Record the exact source commit/tree, clean working tree and tool versions beside
the receipt. Whole-head source review, hosted checks and post-merge verification
remain separate gates. Review actual pictures and motion before making any visual
or production approval claim.
