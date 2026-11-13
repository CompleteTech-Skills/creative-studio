# Director MVP actual execution evidence

Executed implementation: `6cc52fef434c4d80a24c36d9887ca6136a42997c`.
The original `receipt.json` is preserved as a historical recording. Its
`integrated_candidate`, `integrated_main` and fields named `current_runtime_*`
describe earlier integration/correction stages; they do not describe the current
PR head. See `post-execution-source-disclosure.json` for the later exact runtime
hash comparison and host guard changes. No completed render was repeated.

The retained local artifact directory is recorded in the receipt. It contains
120 actual opaque 640×360 PNG masters, two source-bound Blender Cycles CPU
requests/results, the exact portable snapshot, backend/worker logs and a silent
MP4 independently decoded as 120 frames at 24 fps, exactly five seconds, zero
audio. The full original native performance, GLBs and independently reopened
Blender scene remain in the separately retained Director #29 artifact directory.

The first 60 frames completed before optional PNG decoding was available. Their
trusted supervisor-owned bytes were externally checkpointed, then independently
decoded and continued without rerendering them; only the remaining 60 frames were
rendered during completion. The checkpoint proves continuity from its capture,
not retrospective authentication of an untrusted producer. Lost original process
usage is explicitly null/unavailable. The supplied valid-PNG substitution control
rejects a changed valid image after that checkpoint and preserves the original.
Missing/corrupt frame, static-native-motion and audio-present controls also pass.

Both actual contact sheets were inspected at frames 0/30/59 and 60/90/119.
Persistent robots, laboratory and articulation appear in distinct camera views.
Visual review remains **unreviewed**. This engineering fixture does not constitute
provider execution, production artwork or artistic approval.

Use a new output destination and the shared native validation lock when running
`python scripts/director_delivery.py PERFORMANCE_ROOT NEW_DESTINATION --blender BLENDER_4_5_3`.


After actual execution, fresh review identified a separate completed-result resume
branch that did not compare mutable image metadata with the external checkpoint.
The original successful120-frame execution used the raw-stage recovery branch,
which already enforced that checkpoint. The later fix requires exact backend and
frame/hash coverage for every reused completed shot, including the discovery shot.
Three focused unit controls and two whole-orchestration controls on private copies
of the actual artifacts reject a valid replacement PNG with rewritten result
metadata and reject absent discovery-shot checkpoint coverage. No native render
was repeated; all original artifacts remain immutable. The receipt separately
records the original executed fingerprints and then-current corrected runtime
fingerprint. The additive source disclosure identifies subsequent changes to the
renderer host guards as well. The completed artifact bytes do not establish
execution of those later source revisions.

The canonical scene-evidence relocation from `8daaf85` shortens three historical
artifact paths while preserving their payload hashes and an old/new path manifest.
It repairs Windows checkout failures before tests, without changing runtime files.
The confirmed Director Windows capability test correction is also integrated.
