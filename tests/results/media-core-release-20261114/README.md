# Media core integration evidence

This candidate preserves the complete histories of PR391 (`f59514e3`), PR292
(`3d30731e`) and PR341 (`6ba61738`) on main `e2765dd4`. The tested implementation
merge is `f95b234501f857e7e28248ea2ab38558b0054a99`. Later commits add only this
evidence. `integration.json` records all90 input-file blobs and SHA256 values;
89 match at least one frozen source exactly. README.md combines both source
additions. The two explicit conflicts retain PR391's additive atomic APIs and
documentation verbatim. Collector memberships preserve every source and main.

The actual commands ran with the mandatory three-slot pure pool or exclusive
media/native lanes, numerical threads1, owned home TMPDIR, and the interpreters
recorded in receipt.json. From the repository root, their test payloads are:

```sh
PYTHONPATH=scripts:tests:tests/optional python -m unittest test_atomic_fd_stage test_atomic_private_stage test_portable_paths test_rig_directory_capability test_rig_workflow test_decomposition_analysis test_silent_video test_rig_cinematics3d_contracts test_atomic_private_stage_linux test_atomic_capability_linux test_decomposition_package test_production_sources test_rig_skinning -v
python scripts/test_rig_gate.py schema-math
python scripts/generate_schemas.py --check
HATCH_SILENT_MEDIA_TEST=1 python -m unittest discover -s tests/optional -p test_silent_video_native.py -v
HATCH_ANALYSIS_BACKEND_TEST=1 python -m unittest discover -s tests/optional -p test_decomposition_analysis_backend.py -v
```

The focused suite passed208 cases without skips. Schema/math ran263:262 passed,
one explicit Blender-optics opt-in skipped. Its1127 ordinary2D cases were
**discovered only**, with optional3D imports blocked; this is not a full2D run.
Actual FFmpeg/ffprobe passed42 cases without skips, including zero-audio
verification, audio-bearing negatives, source/publication replacement and
legacy AAC review CLI. Actual OpenCV passed both cases without skips, with real
face/eye proposals, one-attempt resume, source fidelity and explicit unsupported
blank imagery. Generated schema parity passed. Raw logs are hash-bound in the
receipt; original source receipts remain unchanged.

This evidence does not approve the combined candidate. A newly spawned reviewer
must inspect its complete final head, and required CI must pass on that head.
Root alone performs an ordinary merge and verifies actual merged main. Only
issue224 may be closed after its complete leaf acceptance and postmerge gates;
full silent CLI/delivery, effects, film241, parent programs and Windows HANDLE
publication remain open. No artistic or general semantic quality is approved.
