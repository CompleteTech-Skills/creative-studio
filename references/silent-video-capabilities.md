# Silent-video capability admission — bounded #223 slice

This adds **read-only discovery**, not an encoder or a finished silent exporter.
Issue #223 and integration owner #97 remain open. The existing audiovisual
`production_render.mux_video()` and `production.py render --video` are unchanged:
they intentionally encode H.264 **and one AAC audio stream** from PNG/WAV masters.
A silent-valued AAC track is not accepted as video-only output by the new profile.

## API and command

```sh
python scripts/silent_video_capabilities.py --timeout 10
PYTHONPATH=scripts:tests python -m unittest test_silent_video_capabilities -v
```

```python
from silent_video_capabilities import silent_profile, validate_profile, probe_capabilities
profile = validate_profile(silent_profile())
report = probe_capabilities()
```

The strict version-1 profile has exactly these fields:

```json
{"schema_version":1,"container":"mp4","codec":"h264","encoder":"libx264","pixel_format":"yuv420p","alpha":"none","audio":{"policy":"forbidden","audio_stream_count":0}}
```

Unknown/missing fields, alternate codecs/containers, audio tracks (even silent
ones), alpha claims, boolean/float version or stream-count values and arbitrary
FFmpeg argument fields are rejected. Other formats are **unsupported**, not
silently mapped to H.264. The returned profile is a fresh copy, not mutable policy.
This Python-level profile validator is not a full export-request schema.

Discovery checks both binary versions and actual FFmpeg video-encoder, output
muxer, output pixel-format and encoder-specific pixel-format listings. Status:

- `available`: exact components are **advertised**, including libx264/yuv420p
  compatibility. No encode was attempted; this is not media verification.
- `unsupported`: tools responded with parseable tables, but a required component
  or encoder-specific format is absent. There is no fallback.
- `unavailable`: missing/unexecutable tools, malformed/empty output, nonzero exit,
  timeout, non-UTF-8 output or output over the accepted byte limit.

CLI exits 0 for advertised availability, 1 otherwise, and 2 for invalid timeout.
Reports retain resolved executable paths and actual version banners. The
`media_execution` field is always `not-performed` in this capability-only API.

## Boundaries and security

Only fixed argument arrays are executed, with no shell, no media input and stdin
closed. Each command has a configurable positive timeout at most 60 seconds;
accepted combined stdout/stderr is limited to 1 MiB. Regular-file capture avoids inherited-pipe cleanup waits. Overflow is checked
every 10 ms and after exit: accepted output is strictly bounded, while temporary
disk usage can exceed the threshold between observations. No truncated table is
accepted. These are trusted-tool controls, not hostile-process containment. Execution is sequential (at most six
commands), so the timeout is **per command**, not a whole-probe deadline.

Optional Python `ffmpeg`/`ffprobe` overrides require absolute existing paths and
never fall back to PATH when invalid. Paths and PATH itself are **trusted operator
configuration**, not fields accepted from a render manifest or remote request.
These controls do not sandbox arbitrary executables, attest binaries or prove
codec operability. Use only trusted installed tools. No backend is installed,
network call made, media published or existing artifact modified by this module.

## Requirement mapping / remaining work

| #223 requirement | This slice | Still required |
|---|---|---|
| Centralized discovery and exact capability probing | Implemented additive API | Wire final exporter after owner integration |
| Explicit forbidden-audio profile | Strict fixed Python profile | Generated schemas, full export request and migrations |
| Missing tools / malformed responses / unsupported settings | Executed negative tests | Platform matrix and actual encoding failures |
| Preserve legacy H.264/AAC | No existing file edited; focused and full candidate regression runs pass within the recorded environment | Hosted CI and optional integration matrix |
| Source inventory, frame adapters, jobs, verified receipts | Not implemented | Canonical 2D adapter; genuine 3D output after #16/#96 |
| Zero-audio artifact and full decode verification | Not implemented | #227 verifier and #228 encoder; final #97 integration |
| Color/alpha/resolution/rational FPS/resource admission | Not implemented | Full immutable export request and compatible conversion |

No consumer should interpret this report as permission to bypass subsequent
source validation, audio exclusion, actual encoding or independent ffprobe/full
stream decode verification. Existing Pillow rendering is not a 3D adapter.

## Observed local validation

Base: `bb5a6810afd06cf57185ffdfe2234e0fdc6e6240` (2026-10-31).
No AGENTS.md exists in that tracked tree. All 18 open PRs and their complete file
lists were checked before selecting these three new, non-overlapping files.

- Baseline `PYTHONPATH=scripts:tests python -m unittest test_production_contracts test_production_render -v`:
  **51 passed**. This includes the actual optional legacy audiovisual mux test;
  no test was modified or skipped. This is not the full 2D suite.
- New focused suite: **16 passed**, including installed-tool probing and actual
  subprocess timeout, overflow, nonzero-exit and UTF-8 rejection cases, plus an
  inherited-output-handle cleanup regression.
- Installed `/usr/bin/ffmpeg` and `/usr/bin/ffprobe`: actual version banners report
  `7.1.5-0+deb13u1`; exact H.264/MP4/yuv420p components are advertised. This command
  did not encode, render or verify any new video.

Full-suite follow-up used `python -m unittest discover -s tests -v`:

- Clean base in the original environment: **963 reported tests, two errors and
  ten skips** in 223.197 s. Both errors were missing `pygltflib` (Model3D's
  independent GLB reader and the rig-contract module import). The same two
  affected modules on the candidate reproduced those exact missing-dependency
  errors (55 reported tests), rather than disguising them as patch regressions.
- Installed the repository-declared `pygltflib==1.16.5` into an isolated
  `--system-site-packages` virtual environment; no repository pin or global
  package was changed. The two affected baseline modules then passed **72 tests**.
  This environment retains host Pillow 13.0.0 and is not claimed to reproduce
  every optional rig-test dependency pin.
- Frozen final candidate in that repaired environment: **996 reported tests,
  zero failures/errors, ten skips** in 223.687 s; exit 0. The separate discovery
  message reports 847 legacy/2D-compatible cases, not a second executed suite.
- Skips retain the existing absent Chromium/Krita and opt-in decomposition,
  Blender, browser-rig and Khronos integration boundaries. Native tests were
  not made to pass by disabling anything. Both full runs printed pre-existing
  Playwright pending-task/TargetClosedError cleanup warnings after the summary;
  those warnings remain in the logs and are not hidden as clean browser evidence.

Independent native full-diff review found an inherited-output-handle timeout bug
in the initial implementation. Regular-file capture and a genuine subprocess
regression fixed it. Re-review passed the corrected code with **67 total focused/legacy
tests**, and independently measured inherited-handle return and timeout
behavior. This is a native agent code review, **not GitHub approval or hosted CI**.
The full-suite results above were executed by the implementer, not the reviewer.

No complete #223, #97 or #16 acceptance, real 3D render, final silent video,
deployment or release is claimed by these results. Remote publication and CI
remain separate gates.
