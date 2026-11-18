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

The earlier launch correction integrated with main `24ffdd8a586cde58782b88c10785e1e0ece569be`
passed 45 portable tests and 70 tests in the optional native suite locally on
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
modules; the core module alone contains 30 cases. A real long temporary path
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

On POSIX, actual temporary directories with non-UTF-8 filename bytes also pass
the Python/CLI privacy regressions for complete and clipped native diagnostics.
Redaction includes the command path's filesystem bytes decoded through the same
UTF-8 replacement boundary as the captured transcript. A portable contract case
checks both spellings and partial error messages. The corresponding Windows
branch exercises an actual Unicode replacement-character filename; it is not
evidence of POSIX byte-path behavior on Windows. Original valid-Unicode cases
remain unchanged.

An actual OS refusal to launch an unsupported decoder retains the completed
probe/timeline records and all available identity hashes through Python and CLI.
Its stage record contains the command aliases, OS error code and `started: false`,
with empty capture buffers and no invented process exit status. Portable
injected launch errors separately cover missing, denied and invalid executables.
An initial 70-test native run had one failure: the long non-UTF-8 path case
observed ffprobe status -9 rather than its asserted status 1. The unchanged
two-case targeted rerun and full 70-test rerun both passed with zero skips; the
initial failure is retained as historical evidence, not reported as a pass.

The original 17 core and 30 native test method bodies remain unchanged. The new
regressions failed on the preceding source before the correction. Independent
review and required hosted checks must identify the final integrated head;
these local results do not grant production-art or full-film acceptance.

## Read refusals and observed termination

Filesystem refusal errors use verifier-owned text rather than forwarding the
descriptor reader's path-bearing exception or structured asset field. A source
failure identifies its inventory index and retains completed probe/timeline/decode
records and the verified snapshot identity. An artifact refused before snapshot
creation retains the valid request/source hashes, without inventing an artifact
hash, byte count, stream inventory or started media process. Two independent
portable cases and two actual mode-000 cases reproduce the earlier API/CLI leak
using only synthetic filenames; the native cases first complete real H.264
encoding and measure that read permission is actually denied.

An over-limit transcript records `evidence_limit_exceeded` and whether termination
was requested. The exit status remains the status returned by the process; a kill
request can race with a natural exit. The ordinary corrupt-file exit-1 assertions
remain. Long-path cases additionally require limit/redaction/hash evidence and a
platform-consistent observed exit. A real deliberately running process establishes
the requested-termination result, and separately labeled simulated process
fixtures cover natural completion before or during the request. This replaces
the incorrect assumption behind the previously retained intermittent test failure.

The dedicated workflow includes every portable silent-video module and both
native media modules. Current local completed unittest logs report 51 portable
tests and 76 tests in the native discovery, all passed with zero skips. The latter
includes inherited portable methods; it is not 76 independent codec exercises.
The new actual permission module separately completed two tests with exit 0,
and portable discovery completed with exit 0. Final exact-head review and hosted
checks remain mandatory; the original 17 core and 30 native methods are unchanged.
