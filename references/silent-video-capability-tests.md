# Silent capability acceptance lane (#223)

Audited base: `0bdffd8d9db6582cd41732b6d6c8bd5b6342dc0e`.

`tests/test_silent_video_capabilities.py` uses **mock** FFmpeg table transcripts,
executable-discovery stubs and synthetic inventory metadata. No stub is executed.
These are portable unit/contract tests, not native media or Blender evidence.

| #223 requirement | Test coverage |
| --- | --- |
| Supported controls and binary/version/list discovery | Both profiles, both versions, encoder/muxer/pixel-format probes |
| Unsupported versus unavailable | Each missing encoder/muxer/pixel format; each missing binary and failed probe |
| No fallback or premature publication | Refusal before staging, snapshot, verification or publication |
| Strict admission | Audio enablement, arbitrary arguments, codec/container/pixel-format overrides |
| Exact advertised names | Suffixed names and names appearing only in descriptions |

The last row preserves a source-derived bug: the audited implementation searches
whole transcripts with word-boundary regexes, which accept these misleading
mentions. The regression is expected to fail on the audited base; **NOT RUN**.

Targeted command: `PYTHONPATH=scripts:tests python -m unittest discover -s tests -p test_silent_video_capabilities.py -v`.
Execution unavailable; no test exit status, pass count or native result is claimed.

Static gate inspection: `scripts/test_rig_gate.py` compatibility and
`scripts/verify_public_delivery.py:build_regression_suite('2d')` discover
`tests/test_*.py` and do not exclude this filename. The dedicated silent workflow
selects existing filenames explicitly; it does not directly select this file.

Current-head targeted, compatibility/Delivery and real silent-media/legacy checks
remain required. This lane does not complete #223, #97 or #16; genuine Blender
provenance, EXR and renderer-owned adapter acceptance remain separate.
