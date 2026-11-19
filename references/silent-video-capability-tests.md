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
remain valid through an additional full native probe on the same private
exact-byte snapshot. Every parsed observation remains bound except the snapshot
dependent `format.filename`; codec, metadata, missing-field, numeric-type and
extra-stream tampering reject. Accepted results retain fresh field-limited
evidence, and historical metadata is not exported in failure diagnostics.
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

The dedicated workflow includes every matching portable and native silent-video
module. At predecessor `0ef11467`, local completed unittest logs report 51 portable
tests and 76 tests in the native discovery, all passed with zero skips. The latter
includes inherited portable methods; it is not 76 independent codec exercises.
The new actual permission module separately completed two tests with exit 0,
and portable discovery completed with exit 0. Final exact-head review and hosted
checks remain mandatory; the original 17 core and 30 native methods are unchanged.

## Logical executable identity and request refusal

Verification and encoding failures identify tools as `ffprobe` or `ffmpeg`,
including when the selected executable is a caller override or resolves through
a PATH symlink. The private executable filename is not exported. Portable
contract tests cover every verification stage and a launch refusal without an
invented exit status. A native control copies the actual ffprobe binary under a
synthetic filename and verifies both API override and CLI PATH-symlink failures.

The CLI refuses an unreadable or malformed request with fixed application text;
it does not forward path-bearing reader exceptions or claim media facts before
request admission. Direct verification also hides rejected request values.
Probe dimensions must be integers; separately labeled simulated records test
float, string and boolean refusals with a valid integer control. These cases do
not claim an actual ffprobe dimension bypass.

At the corrected source, portable discovery passed 56 methods, the optional
native discovery passed 77 methods, and the unchanged legacy renderer module
passed 20 methods, all with zero skips and actual exit 0. Native discovery
includes inherited portable and mocked contract methods as well as actual
encode/probe/decode cases; it is not 77 separate native codec executions.
The source file had the same SHA-256 before and after native execution. An
initial portable run failed because the correction added keyword arguments to
capability probes whose independent mocks require the original call contract.
Capability probe calls were restored without changing those assertions, and the
full portable rerun passed. Final integrated current-head checks and fresh
independent review remain required.

## Concurrent diagnostic preservation

The local integration of `542d152c` with `8aa88457` preserves the concurrent
`tool_role` implementation, token-aware command-path aliases, literal raw probe
facts, labelled executable stderr diagnostics and child-only reporting/color
controls. The request privacy and integral-dimension checks described above
remain. Capability helper call contracts retain their existing form. All
concurrent silent-video test method bodies remain unchanged; the additional
public-error test changes only its private keyword from `tool_name` to
`tool_role`, retaining every assertion.

Complete portable discovery passed 62 tests in 2.228 seconds, optional native
discovery passed 85 tests in 92.070 seconds and the legacy renderer module passed
20 tests in 2.602 seconds. Each command returned actual exit 0 with zero skips
on Linux/Python 3.12.14 and FFmpeg/ffprobe 7.1.5. The tested
`scripts/silent_video.py` SHA-256 before and after execution was
`6775a20b124e70cecefe8b644aa51c2859dab03c1b9b46188227bf3006797aeb`.
The native count includes inherited portable and simulated contract checks;
it does not represent 85 distinct native codec executions.

Both long non-UTF-8 path methods passed unchanged in that run. The restored
legacy method requires native exit 1, whereas the additional evidence-budget
method retains the observed termination flag and platform-consistent status.
The former remains scheduling-sensitive when the evidence limit requests a
kill before natural exit; this passing run does not prove deterministic exit 1.
No existing assertion was relaxed to obtain these results. Real permission
refusals, the installed probe named `video`, and inherited report/color controls
ran without skips using synthetic temporary media. These are local execution
results, not independent review or production acceptance. Final published-head
checks and fresh independent review remain required.

## Historical receipt integrity and caller aliases

The portable raw-transcript fixtures retain their original path spellings and
assertions alongside the added POSIX/Windows lexical controls. The legacy
non-UTF-8 privacy case now
uses the transparent completed-process schedule for both Python and public CLI
dispatch: the installed ffprobe finishes, and its same process object, actual
exit status and output files are passed to ordinary capture. This retains its
exit-1 assertion without racing the production log-budget kill. The adjacent
unscheduled termination test still exercises ordinary bounded capture and
records the actual termination outcome. Neither schedule fabricates output or
process status, and both keep their independent fixture timeout.

The six-method optional
[`test_silent_video_historical_receipt_native.py`](../tests/optional/test_silent_video_historical_receipt_native.py)
module executes the installed full-probe protocol on six synthetic H.264 frames.
It checks unchanged receipts and the filename exception, then changes every
other observed leaf, removes recorded metadata and adds unobserved metadata.
It exercises the historical command format with the current installed tools;
it does not emulate an older implementation.

The original independent reviewer oracle is preserved byte-for-byte at
[`tests/independent/test_historical_receipt_integrity.py`](../tests/independent/test_historical_receipt_integrity.py),
SHA-256 `c69bcd5fca2d4060014fd8a70f3c31112f08c38f2f653e13933cca7a3a14ede7`.
It imports a separately checked-out implementation pinned to
`094e4c5f04c255178be6a09cb10d25280201fe92`, creates its receipt using that
implementation's actual encode/probe/decode workflow, and compares acceptance
with the current implementation. No process result or older implementation is
simulated. Keep `tests/independent` without a package `__init__.py`: this manual
two-checkout oracle must stay outside ordinary automatic unittest discovery.
From the current checkout, run it explicitly after preparing the pinned base:

```bash
git worktree add --detach ../silent-receipt-base 094e4c5f04c255178be6a09cb10d25280201fe92
PYTHONPATH=scripts:tests SILENT_RECEIPT_BASE=../silent-receipt-base python tests/independent/test_historical_receipt_integrity.py
```

Portable read-refusal checks also run through an actual caller-directory alias
(a POSIX symlink or Windows junction). Injected descriptor refusals compare the
normalized physical root, so they occur for both caller spellings; evidence must
omit both aliases and physical private paths. These simulated media records
establish diagnostic contracts, not native encode/probe/decode behavior.
Separate portable cases exercise the installed JSON parser's actual nesting
refusal for historical JSON and simulated digest recursion/overflow errors.
Their fixed refusal retains completed field-limited command records and snapshot
hashes without exporting private historical metadata.

Fresh local verification used Linux, Python 3.12.14 and FFmpeg/ffprobe 7.1.5.
The source SHA-256 before and after execution was
`41e345055359afbd9de281d48d60c53ca0749fcf312721a86d553ce41ce7a304`.
The three discovery commands in the
[dedicated workflow](../.github/workflows/silent-video.yml), with
`PYTHONPATH=scripts:tests` and `HATCH_SILENT_MEDIA_TEST=1` for native discovery,
completed with actual exit 0: 72 portable methods in 20.835 seconds, 98 optional
native-suite methods in 243.057 seconds, and 20 unchanged legacy renderer
methods in 3.043 seconds. The explicit original oracle command against the
separate pinned base completed one method in 14.580 seconds, also exit 0.
Every run had zero skips. The oracle observed base encode/probe/timeline/decode
exit 0, accepted the unchanged receipt in both implementations and rejected a
copied changed bitrate in both. The optional native count includes inherited
portable and simulated contract methods; it is not 98 separate codec exercises.
The POSIX alias branch ran here; the Windows junction branch was not run.
These are local synthetic-media and contract results. Final integrated-head
review and hosted gates remain required, and no production-art acceptance is
claimed.
