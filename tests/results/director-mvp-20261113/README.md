# Director MVP actual execution evidence

Executed implementation: `6cc52fef434c4d80a24c36d9887ca6136a42997c`.
Current main was then integrated without changing any of the twelve executed
Director, scene, renderer, verifier or silent-video source files listed in
`receipt.json`. No completed render was repeated.

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
