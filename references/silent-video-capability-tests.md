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


## Exact-name implementation

The source correction parses the name column of each flag/name/description row
instead of searching the entire transcript. It requires video encoder rows,
output muxer rows and output-convertible pixel formats; comma-separated advertised
aliases are treated as separate exact names. Suffixes and description text do not
grant capabilities. Unavailable binary/probe handling, evidence and fixed profiles
remain unchanged.

Table layouts were checked against the primary FFmpeg n6.1.1 source:
https://github.com/FFmpeg/FFmpeg/blob/n6.1.1/fftools/opt_common.c .
The tests were authored independently before this implementation. Their mocked
probe results are contract fixtures; actual codec evidence comes from the separate
native silent-media workflow. Current-head execution remains pending at authoring.

## Later failure-evidence correction (#525 / #227)

The correction based on main `3358f0d533984c522e9e8df5c6fe92b6a69bb4af`
passed 43 portable tests and 66 tests in the optional native suite locally on
Linux, with no skips. The native suite includes inherited portable contract
tests and actual FFmpeg/ffprobe 7.1.5 checks on synthetic media. An actual
corrupt MP4 produced ffprobe exit 1; Python and CLI failures retained the raw
bounded command result and identity hashes without fabricated stream counts.
Portable malformed-probe, timeout and evidence-bound fixtures separately cover
the diagnostic contract; they do not establish native codec behavior. Malformed
aspect-ratio and integral-count fields fail with retained raw probe evidence
through both Python and CLI paths. UTF-8 replacement expansion also sets the
truncation flag when its encoded transcript exceeds the byte limit; invalid
UTF-8 stdout remains an invalid command result.

The later security correction limits ffprobe to the fields actually used by the
verifier while retaining every stream. An actual tagged audio-only MP4 fails
without echoing its arbitrary editorial metadata. Missing or unknown stream
types reject before measured counts are reported. Historical full-probe receipts
remain valid through a shared projection onto the same authoritative fields;
codec, missing-field, numeric-type and extra-stream tampering still reject.
Actual corrupt-media tests also verify that exported failure JSON and errors
replace command-owned absolute directory paths with stable aliases. Byte limits
are reapplied after replacement. A separate contract fixture proves that a
truncated strict-decode transcript cannot succeed merely because the retained
prefix contains only whitespace; that fixture is not a native artifact bypass.
The 45 portable cases combine the core, capabilities and production-adapter
modules; the core module alone contains 30 cases, including undecodable and
control-byte printed path forms. A real long temporary path
that is cut inside ffprobe's stderr is also redacted in Python and CLI evidence.
Stale source errors identify the frame index without copying relative source
names. Portable deeply nested JSON fixtures exercise the installed JSON parser's
actual nesting error at both probe and timeline stages; simulated command results
are explicitly contract evidence, not native ffprobe behavior.

Actual corrupt-media cases cover all four UTF-8 capture alignments in a long
Unicode directory name through Python and CLI. Incomplete trailing code points
are deferred at a truncated capture boundary so replacement characters cannot
hide an otherwise known path prefix. Probe and decode inputs permit only the
MOV/MP4 and Matroska/WebM demuxer families and the local file protocol. Actual
FFconcat edit lists disguised with either supported extension are rejected
before referenced clip names can be opened or echoed. The format is still
independently detected, and every stream remains in the admission inventory.

The original 17 core and 30 native test method bodies remain unchanged. The new
regressions failed on the preceding source before the correction. Independent
review and required hosted checks must identify the final integrated head;
these local results do not grant production-art or full-film acceptance.
