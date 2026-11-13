# Blender export integrity evidence

The verification record distinguishes current source tests from the retained artifact run. The artifacts are procedural engineering fixtures, not production artwork or artistic approval.

`artifacts/verified.blend` was saved and reopened in distinct Blender 4.5.3 processes. Its full native request is retained in `export-request.json`; the original stored fixture manifest remains `native-run/rig-before-socket.json`. `native-run/rig.json` reproduces the exact exported in-memory request and loads with the same rig signature. The original geometry, weights and animation files are retained unchanged.

The two PNGs are real Cycles CPU frames at 96×96. Raw receipts retain their original local paths; use `artifacts/artifact-index.json` for the repository-relative exact-byte inventory. File hashes identify the artifacts independently of those historical paths.

The first socket threshold failure is retained separately. No browser, Khronos, human review or full rig mission completion is inferred from these tests.
