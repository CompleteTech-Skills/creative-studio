# Explicit silent video exports

`scripts/silent_video.py` is the media authority for the silent export workstream
(#223, #227, #228), integrated by Render3D #97. It does not replace rendering.
The existing `production_render.mux_video()` and `production.py render --video`
continue to produce H.264/AAC **audiovisual review movies**, including one AAC
stream. WAV/PNG masters, production manifests, preview timing, archives and
review decisions are unchanged. A zero-valued AAC or Opus track is not silent
under this new contract: the independent verifier rejects every audio stream.

## Supported API and commands

The explicit API admits bounded ordered **opaque 8-bit RGB/RGBA PNG** sources.
It supports H.264 MP4 (`libx264`, CRF 18, veryfast, `yuv420p`, `+faststart`) and
VP9 WebM (`libvpx-vp9`, CRF 30, zero bitrate target). Required binaries are
separately discovered and probed; unsupported codecs never select a fallback.
Secure encoding currently requires Linux with procfs and descriptor-relative exclusive rename. Verification and contract admission remain portable, but non-Linux encoding reports unsupported rather than using untested directory-swap protection. The shared publisher also exposes macOS directory-descriptor support; a macOS media-path adapter is not claimed here.

The profiles are untagged SDR; neither HDR nor color-managed transforms are
claimed. Alpha-bearing, EXR, high-bit-depth, odd-size or oversized images fail
instead of silently being flattened, tone-mapped, resized or padded.

```python
from silent_video import build_source, make_request, encode, verify_export
source = build_source(root, ordered_png_paths,
                      fps={"numerator": 30000, "denominator": 1001},
                      origin="synthetic")
request = make_request(source, profile="h264-mp4")
receipt = encode(request, root, new_output_path)
assert receipt["audio_stream_count"] == 0
verified = verify_export(new_output_path, request, root=root)
```

The source and request are ordinary strict JSON documents. Their SHA-256
identities use sorted compact JSON. Source records retain original file hashes,
byte lengths and indices. `source_from_production(root)` adapts a complete
existing 2D shot or scene through its existing `validate_render()` authority,
including validation of original audio; that WAV is not supplied to the silent
encoder. `source_from_blender_manifest(root, manifest)` accepts a
`blender-frame-manifest` containing `schema_version:1`, `engine:CYCLES` or
`BLENDER_EEVEE_NEXT`, `fps`, and exact `frames` records (`index`, `path`, `sha256`,
`bytes`). It validates actual PNG bytes. The claimed Blender origin alone is
not proof of geometry, renderer execution, production quality or visual approval;
the calling renderer must bind this manifest to its own verified render result.
EXR adaptation is deliberately rejected until #229/#235/#237 provides its
precision, color and alpha contract. No Pillow image is presented as real 3D.

```bash
python scripts/silent_video.py capabilities
python scripts/silent_video.py encode --request request.json --root frames-root --destination new-film.mp4 > receipt.json
python scripts/silent_video.py verify-export new-film.mp4 --request request.json --root frames-root --require-silent
```

Requests require `audio: {"policy":"forbidden"}`. There are no custom arguments,
audio input paths, stream-map options or executable fields in the JSON contract.
The executable overrides in Python are trusted deployment/test hooks; never map
untrusted HTTP or JSON values to those parameters. Unsupported schema versions
fail and require explicit future migration; legacy manifests are adapted,
never rewritten in place. Six strict schemas are retained in
`schemas/silent-video-v1/`; the test suite checks exact parity with `schemas()`.
The job schema defines downstream lifecycle admission; it does not claim the
persistent scheduler/restart support tracked in #239.

## Verification and publication

The encoder copies each exact input into a private directory after checking
hashes, file type, decoded dimensions and opaque alpha. POSIX source traversal
uses directory descriptors and no-follow opens. Unsafe paths, aliases, symlinks,
nonregular files, missing files and modified source hashes fail. Each source
inventory is limited to 18,000 frames, 2 GiB encoded total, 64 MiB per file and
8,847,360 pixels per image; resolution axes cannot exceed 4096. Exact frame rates
reuse the production rational validator (1–120 fps). Profiles reject odd sizes
because `yuv420p` cannot represent them faithfully.

Encoding uses an owned `output.partial.mp4` or `.webm` and explicit
`-map 0:v:0 -an -sn -dn -map_metadata -1 -map_chapters -1`. It fixes frame count,
rate, codec, pixel format and single-threaded encoding. No source WAV is opened.
Timeout, cancellation, output bytes and captured evidence are checked while the
process runs and again after fast exits. The default limits are 300 seconds,
512 MiB encoded output and 4 MiB for each captured stdout/stderr stream; bounded
requests can reduce these or increase them within documented schema maxima.
Storage admission requires enough available bytes for the snapshot and maximum
output. This is an admission check, not a global disk reservation.

The codec and input guard share one held private input-directory descriptor.
Every captured input is hashed and its inode, size, mtime and ctime are recorded
before progress callbacks. A metadata fence runs before encoding, and complete
hash/identity checks run after all encoding and verification callbacks, before
publication. This detects replacement and modify/restore writes even when the
original bytes and mtime are restored. The fence detects filesystem mutation;
it is not an OS seal or hostile-process isolation. It adds bounded streaming
hash passes and one directory authority, without holding every frame open or
changing the representative decoded-pixel comparison contract.

Independent verification copies the encoded bytes into another private snapshot
so a mutable source pathname cannot switch between probe and decode. It runs:

- `ffprobe -count_frames -show_entries` for verifier-owned stream and format
  fields, without filtering the stream inventory;
- actual frame enumeration with integer presentation timestamps;
- `ffmpeg -xerror -err_detect explode` decoding of the complete video.

All verification commands limit input demuxers to the MOV/MP4 and Matroska/WebM
families and protocols to local files. The verifier also checks the private
snapshot's file type. WebM reads at most the first 64 KiB and requires the EBML
document type `webm`. MP4 walks all top-level box extents on one open snapshot
descriptor. The metadata budget is 4096 boxes and 64 KiB of headers and
file-type data. After MP4 identification, a separate allowance covers one
`moof`/`mdat` pair per supported frame, including 64-bit box extents.
After MP4 identification, a rejected file may require one eight-byte lookahead
to classify the next box. The walk seeks past padding and media payloads. Extents must fit the captured file;
extended sizes and a final size-zero box extending to EOF retain their defined
meaning. Partial headers, invalid extents, exhausted budgets and repeated
`ftyp` declarations fail. Exactly one `ftyp` is required, with a major brand of `mp41` or
`mp42`. An `isom`, `iso2` through `iso9`, or `avc1` major brand also qualifies
when the compatible brands include `mp41` or `mp42`. An `avc1` compatibility
entry alone is insufficient. QuickTime, 3GP and 3G2 major brands are rejected
even if they list MP4 compatibility. This distinguishes containers that ffprobe
reports under the same demuxer family. An edit list disguised as a supported
media file is refused before following its referenced inputs. Failure
transcripts exclude unrelated metadata and
replace command-owned paths, including a path cut at a UTF-8 capture boundary.
Their matching spellings also cover POSIX filesystem bytes that decode to
replacement characters in a native tool's transcript.

Portable box-conformance tests cover padding, media skipping, extended and
size-zero extents, later conflicting type declarations and both resource
limits. These are parser checks, without native media claims. The native
positive regression constructs a one-frame H.264 fragmented MP4 with a
65536-byte leading `free` box, checks unchanged stream and timeline facts,
and exercises actual strict decode plus Python and CLI verification. A separate
native control verifies a final `mdat` whose size is zero (extends to EOF).

It requires exactly one video and zero audio/subtitle/data/attachment streams;
checks the explicit codec, container, pixel format, resolution, square samples,
display aspect, untagged SDR properties, rational FPS and actual counted frames;
and verifies every presentation timestamp against its exact frame index.
Timestamp tolerance is one stream timebase tick. Container duration permits two
ticks for the independent rounding of the last timestamp and frame duration.
The order is presentation order, so H.264 B-frame decode reordering is valid.
Absent `nb_frames` is harmless: actual counted/decoded frames are mandatory.
When `root` is supplied, original hashes are rechecked and three representative
decoded frames are compared to source pixels (RGB PSNR at least 18 dB, a
conservative defect detector, not visual approval).

Only successful independent verification permits exclusive native atomic
publication. The output directory is opened without following symlinks and pinned
by descriptor through staging, subprocesses and the final rename. A concurrent
parent rename retains that original directory identity; a substituted pathname
cannot redirect publication elsewhere. An existing destination, including a racing empty directory or
symlink, is preserved. The private staging directory is also pinned before any
frame or encoded bytes are written; its replaceable basename is never reopened
for publication or recursive cleanup. Cleanup preserves a foreign replacement.
The shared [private staging authority](atomic-publication.md) clears only its
owned contents and retains tiny empty outer wrappers for explicit maintenance;
it never deletes a possibly replaced outer basename. Cancellation callbacks run
during encoding and verification, with no callback
between the final original-byte recheck and exclusive publication. Private
partials are removed after failures. The returned
receipt binds the actual artifact, source manifest and request hashes and
retains commands, raw probe output, timeline and decode evidence. Persist it in
the enclosing renderer's existing transaction; this low-level API does not
pretend a separate output-file/sidecar pair is an atomic transaction.
`validate_receipt()` rejects altered fields and reruns actual verification.
A failed CLI report has `verified:false`; when stream inspection succeeded it
also includes the observed stream counts and raw proof. No failure is a valid
success receipt.

## Requirements and evidence

[#223][foundation] owns the foundation audit, contracts, adapters and capability
probes. [#97][integration] owns Render3D integration and delivery. The media
implementation stays in [`silent_video.py`](../scripts/silent_video.py). The
table maps current code and named tests to those requirements. Test names refer
to the suites linked in the corresponding row.

| Requirement | Implementation and compatibility boundary | Evidence |
| --- | --- | --- |
| #223 / #97 — Legacy 2D contracts, manifests, timing and render locks | [`production_contracts.py`](../scripts/production_contracts.py) loads the 2D project. [`production_render.py`](../scripts/production_render.py) uses `_lock_output()` for shot and scene rendering, writes resumable checkpoints, and uses `validate_render()` to verify complete PNG/WAV masters, their hashes and rational timing. | [2D contract tests](../tests/test_production_contracts.py) cover schema/runtime parity and unsupported versions. [Renderer tests](../tests/test_production_render.py) cover partial resume, tampering, destination collisions and PCM validation, including `test_partial_resume_exact_manifest_and_tampered_frame_recovery` and `test_audio_rational_duration_offsets_gains_and_tampering`. |
| #223 / #97 — H.264/AAC review movie | [`production.py`](../scripts/production.py) dispatches `render --video` to `mux_video()`. The mux explicitly maps numbered PNGs as `0:v:0` and the WAV master as `1:a:0`, encodes one H.264 video stream and one AAC audio stream, and checks counted frames, FPS and audio duration before writing `review.mp4` and `mux-receipt.json`. One audio stream is intentional even if the PCM samples are all zero. | `test_actual_offline_movie_codec_frame_rate_and_audio_duration` in the [renderer suite](../tests/test_production_render.py) exercises the mux directly. `test_legacy_render_video_cli_keeps_aac_audio` in the [native suite](../tests/optional/test_silent_video_native.py) probes the CLI result for H.264 and AAC and checks the WAV and receipt. |
| #223 / #97 — Preview, editor handoff, snapshots and archive/restore | `write_preview()` uses 2D render manifests. [`production_adapter.py`](../scripts/production_adapter.py) exports PNG/WAV handoffs and layered OpenRaster snapshots. [`production_workflow.py`](../scripts/production_workflow.py) owns dependency snapshots, review, project archive verification and restore. [`production_bundle.py`](../scripts/production_bundle.py) verifies portable transport parts before delegating to restore. | [Preview tests](../tests/test_production_preview.py) exercise timing and audio seek. `test_portable_handoff_sequence_audio_and_relocation` covers the [editor adapter](../tests/test_production_adapter.py). `test_archive_roundtrip_preserves_identity_review_and_dependency_hashes` and `test_archive_preserves_acquisition_records_without_reading_runtime_locks` cover the [legacy workflow](../tests/test_production_workflow.py). See [2D production traceability](production-traceability.md) for the broader inventory. |
| #223 / #227 / #228 — Explicit v1 request and zero audio streams | `make_request()` and `validate_request()` require `audio.policy=forbidden`. The request has a closed field set and a fixed profile choice. `_encode_pinned()` gives FFmpeg one PNG input, explicitly maps its video, and disables audio, subtitles and data. `verify_export()` independently probes the actual unfiltered stream list and decodes the result. The artifact schema requires `audio_stream_count=0`. | [Portable tests](../tests/test_silent_video.py) reject relaxed policy, injected flags and injected profiles. The [native suite](../tests/optional/test_silent_video_native.py) exercises adjacent WAV isolation, all-zero AAC and Opus rejection, extra video, full decode and receipt tampering. |
| #223 — Profile, limits and capability probing | Fixed profiles specify codec, encoder, container, pixel format, alpha, color, precision and quality. Source and request carry raster size, rational FPS and resource budgets. The per-profile capability schema describes platform, raster limits and estimated source/output bytes. Separately, the aggregate `capabilities()` diagnostic runs version commands for both binaries and encoder, muxer and pixel-format listings for FFmpeg. Failed discovery/probing reports `unavailable`. With usable tools, a missing requirement for a specific profile reports `unsupported` for that profile. | [Portable tests](../tests/test_silent_video.py) check schema parity, mixed codec/container rejection, injected profiles, exact FPS, missing tools and resource bounds. The [native suite](../tests/optional/test_silent_video_native.py) exercises locally available H.264 and VP9 profiles. An exhaustive profile/platform probe matrix remains open. |
| #223 / #229 — Version admission and 2D/3D source adaptation | The strict silent source and request validators require integer `schema_version: 1`. `source_from_production()` adapts a complete, validated legacy shot/scene checkpoint with opaque source frames into a new v1 inventory by checking the original PNG hashes and using its FPS. The original checkpoint and WAV stay under the 2D workflow. `source_from_blender_manifest()` accepts the defined v1 Blender PNG manifest shape and matches its ordered records to decoded input files. Renderer-owned job provenance requires a verified #96 result. | [Portable tests](../tests/test_silent_video.py) cover committed schema parity, path safety, nonimages, precision and PNG transparency. `test_blender_manifest_requires_integer_v1` covers the prior boolean and floating-point version acceptance and a valid integer v1. The local Blender and FFmpeg result below covers actual PNG admission and explicit EXR rejection. |
| #223 / #228 / #239 — Immutable inventory, lifecycle and receipts | `build_source()`, `validate_source()` and `_snapshot()` bind contiguous ordered file indices, lengths and hashes and reuse checked paths and production rational timing. The six [v1 schemas](../schemas/silent-video-v1/) cover source, request, profile, capability, job and verified artifact. The artifact schema describes the verification receipt. Encode also returns its command evidence and output basename. `validate_receipt()` revalidates actual media. Persistence and recovery belong to #239 and the caller's transaction. | [Portable tests](../tests/test_silent_video.py) check schema parity, unsafe paths and symlinks. [Native tests](../tests/optional/test_silent_video_native.py) cover stale or missing sources, post-admission staged-input changes, cancellation, and exclusive publication. |
| #97 / #96 / #240 / #241 — Render3D integration and acceptance | The low-level media CLI exposes `capabilities`, `encode` and `verify-export`. Full Render3D job operations, actual-frame/pass previews, portable delivery and the end-to-end multi-shot Blender film still require renderer-owned evidence. | The local suites use synthetic PNGs and a synthetic 2D project. The separate Blender result covers a two-frame CPU fixture and media verification. Renderer-owned workflows, EXR admission and visual acceptance remain open. |

Silent v1 rejects unsupported source/request versions. There is no silent
manifest migration routine. Legacy production adaptation reads the existing
supported checkpoint and constructs a distinct silent source record. Any future
silent version needs an explicit admission or migration implementation with
compatibility tests. The audited baseline Blender adapter compared versions
using Python equality, which accepts `true` and `1.0` as `1`. The adapter
now requires an exact integer v1, consistent with silent source and request
admission.

[#229][adapters] owns frame-source adaptation from verified masters,
[#235][sequences] owns lossless PNG/EXR inventory and source integrity, and
[#237][color] owns HDR, alpha and color transforms. The current PNG adapter
rejects EXR. [#230][codecs] and [#236][profiles] own expanded codecs and delivery
profiles. [Draft PR #417][profile-pr] overlaps those owners with AV1, named
profiles and sequence work. Its draft behavior and retained runs belong to its
own revisions. The current supported set documented above is the audited
baseline.

## Local baseline

Commands ran from the repository root on Linux x86_64 with Python 3.12.15
and FFmpeg/ffprobe n9.0.2. The baseline revision was
`62dc9f9a9cae319933c9f0bc8c39fcde9d26663a`. The portable gate runs without the optional
native module. The dedicated Linux gate runs both the real-media cases and
inherited portable cases. Historical results remain in the
[foundation evidence receipt](../tests/results/silent-video-foundations.json).

| Command | Observed evidence |
| --- | --- |
| `../venv312/bin/python -m unittest discover -s tests -p test_silent_video.py -v` | Baseline 16 passed. With the integer-version fix, 17 passed. Zero skips in both runs. Exercises local synthetic inputs and mocked missing-tool discovery. |
| `HATCH_SILENT_MEDIA_TEST=1 ../venv312/bin/python -m unittest discover -s tests/optional -p test_silent_video_native.py -v` | Baseline ran 45 tests, with 44 passed, zero skips and one environment error. H.264 and VP9 synthetic exports passed independent probe/decode, and the legacy CLI retained one AAC stream. The private-input modify/restore case stopped when staging synthetic PNGs hit a disk quota. |
| `HATCH_SILENT_MEDIA_TEST=1 TMPDIR="$(mktemp -d ../silent-video-223.XXXXXX)" ../venv312/bin/python -m unittest discover -s tests/optional -p test_silent_video_native.py -k test_private_input_modify_restore_during_actual_encoding_is_rejected -v` | The targeted retry passed with temporary storage on the workspace filesystem. It used the integer-version fix and exercised actual encoding and the staged input mutation. |
| `../venv312/bin/python -m unittest discover -s tests -p 'test_production*.py' -v` | 190 test cases reported, with 189 passed and one Krita integration skipped because Krita was unavailable. The pilot class setup separately errored when rebuilding from retained local sources hit a disk quota. The legacy mux test passed, and the preview tests ran Chromium 156.0.8078.4. |

Blender 4.5.3 LTS rendered two distinct 64 × 48 opaque RGB PNGs with Cycles on
two CPU threads. The local fixture passed the ordered v1 Blender manifest
adapter, then FFmpeg and ffprobe n9.0.2 verified one H.264 `yuv420p` video
stream with two frames at 24 fps and zero audio or other streams. Both decoded
frames passed source-pixel comparison. The same Blender fixture produced an
EXR that the adapter rejected with the documented 8-bit PNG requirement.
Source files retained their original hashes and metadata.

| Local artifact at `e497d7a6` | SHA-256 |
| --- | --- |
| Canonical adapted source record | `0a42c792439de9952ea4b8a4cc00f723eebb05710092b77eb5d6b24fd0d589c5` |
| Independently verified MP4 | `841a613358cf4f271bb650ab0b817ef68f7e6c51863e60c7507d51499acea6de` |
| Post-publication ffprobe result | `5ca791923f05a0d9c287c32aeee048434a179fb4418b3fe1c6324c87d39cc335` |

<details>
<summary>Synthetic adapter and version smoke check</summary>

This local check used temporary 2D renders. The opaque shot and scene adapted
with the same FPS and frame hashes, and the checkpoint and WAV hashes stayed
identical. The original transparent fixture was rejected. The Blender-shaped
manifest used the same synthetic 2D PNGs to exercise admission only.

```bash
PYTHONPATH=scripts:tests ../venv312/bin/python - <<'PY'
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from production_render import file_hash, render_scene, render_shot
from silent_video import (make_request, source_from_blender_manifest,
                          source_from_production, validate_request, validate_source)
from test_production_render import fixture_project

with TemporaryDirectory() as temporary:
    root = Path(temporary)
    project = fixture_project(root / 'project')
    transparent = root / 'transparent'
    render_shot(project, 'first', transparent)
    with TestCase().assertRaisesRegex(ValueError, 'nonopaque'):
        source_from_production(transparent)
    project['project']['render']['background'] = '#000000ff'
    for name, render, target in (
        ('shot', render_shot, 'first'),
        ('scene', render_scene, 'scene-one'),
    ):
        output = root / name
        manifest = render(project, target, output)
        originals = [output / 'render-manifest.json', output / manifest['audio']['path']]
        before = [file_hash(path) for path in originals]
        source = source_from_production(output)
        assert source['origin'] == 'production2d'
        assert source['fps'] == manifest['fps']
        assert [record['sha256'] for record in source['frames']] == [
            record['sha256'] for record in manifest['frames']
        ]
        assert before == [file_hash(path) for path in originals]

    declared_blender = dict(schema_version=1, kind='blender-frame-manifest',
                            engine='CYCLES', fps=source['fps'], frames=source['frames'])
    adapted = source_from_blender_manifest(output, declared_blender)
    assert adapted['origin'] == 'blender3d' and adapted['frames'] == source['frames']
    request = make_request(source)
    for document, validate in ((source, validate_source), (request, validate_request)):
        for version in (2, True, '1'):
            invalid = deepcopy(document)
            invalid['schema_version'] = version
            with TestCase().assertRaises(ValueError):
                validate(invalid)
    for version in (2, True, 1.0, '1'):
        with TestCase().assertRaises(ValueError):
            source_from_blender_manifest(output, dict(declared_blender, schema_version=version))
print('Synthetic 2D shot, scene, Blender PNG manifest shape and version admission passed')
PY
```

</details>

[foundation]: https://github.com/CompleteTech-Skills/creative-studio/issues/223
[integration]: https://github.com/CompleteTech-Skills/creative-studio/issues/97
[adapters]: https://github.com/CompleteTech-Skills/creative-studio/issues/229
[codecs]: https://github.com/CompleteTech-Skills/creative-studio/issues/230
[sequences]: https://github.com/CompleteTech-Skills/creative-studio/issues/235
[profiles]: https://github.com/CompleteTech-Skills/creative-studio/issues/236
[color]: https://github.com/CompleteTech-Skills/creative-studio/issues/237
[profile-pr]: https://github.com/CompleteTech-Skills/creative-studio/pull/417

## Failed verification evidence

Failure evidence retains the artifact, request and source-manifest hashes and
`verified: false`. Once a media process starts, a failed command retains its
argument list, observed exit status, stdout and stderr under `command_failure`
and its verifier stage (`probe`, `timeline` or `decode`). An OS launch refusal
instead records the command
aliases and numeric OS error code with `started: false`, empty capture buffers
and no exit status. Empty buffers do not claim that a process ran; prior
completed verification stages and available identity hashes remain attached.
Each transcript stays
within the request's `max_log_bytes` byte limit, including UTF-8 replacement.

Failure aliases for encoding and verification use the logical `ffmpeg` or
`ffprobe` role, including when an installed executable symlink resolves to a
privately named binary. They do not expose the resolved filename in command
arrays or known command-path text. Generic local subprocess helpers retain
their existing basename contract; this is not a general sanitizer for arbitrary
substituted executable output. Source-frame descriptor errors also omit caller
filenames. If the artifact cannot be captured at all, failure evidence retains
only the request and source-manifest hashes; it does not invent an artifact
hash, byte count or tool execution record.

Independent regressions reproduced private executable and unreadable-source
names in both Python and CLI on `db3e36bc`, using a byte-identical copy of actual
ffprobe and real POSIX permission failures. An additional root-authored native
case covers unreadable artifact capture and its available identity fields.
The corrected optional native suite ran 78 cases in 143.717 seconds, exit 0,
zero skips, on unprivileged Linux with FFmpeg/ffprobe 7.1.5. Its cases include
inherited portable checks; this is not 78 distinct codec executions. The new
permission checks explicitly skip where real unprivileged POSIX semantics
cannot be exercised. Existing test methods and earlier negative evidence remain.
Truncated streams carry an explicit truncation flag; timeout failures carry
`timed_out: true` and the observed terminated-process status.
If the operating system cannot launch a media executable, the attempted command
instead carries `started: false`, `launch_error.errno` and empty stdout/stderr.
It has no `exit_status`: no media executable returned a status. Earlier completed
probe or timeline observations remain available, including stream counts only
when the actual probe established them. The launch summary omits the operating
system exception's filename and retains the same bounded path aliases.
Failed exported records replace absolute executable and temporary-directory
paths with stable tool/work-directory aliases in commands, transcripts and the
error message. Per-stream byte limits apply after this replacement. Capture
also suppresses a partial known path cut at the retained byte boundary.
Partial-path suppression uses the same left token boundary as complete paths.
A rational value cut immediately after its slash remains observed probe data;
that slash alone does not establish a command-owned absolute path. CLI request
intake also converts numeric overflow during structural validation into the
fixed JSON refusal, without an interpreter traceback or caller filename.
Stale source diagnostics identify the frame index rather than its caller-chosen
directory or filename. Error summaries do not copy unsanitized transcript tails.
Strict decode requires complete diagnostics and rejects a truncated stderr transcript,
including one whose retained prefix contains only whitespace.

Successful raw command results are retained before JSON or stream interpretation.
Malformed ffprobe output therefore leaves inspectable raw evidence. If probing
or timeline parsing exceeds the JSON parser's nesting limit, the failure retains
the same structured evidence. If probing
cannot establish stream counts, the failure does not invent video/audio counts.
The ffprobe query inventories all streams but requests only fields consumed by
the verifier; arbitrary tags and unrelated container metadata are not requested.
Missing or unknown stream types reject before any counts are reported. Receipt
comparison checks the entire parsed current raw probe result. A recorded
historical query using both `-show_streams` and `-show_format`, without
`-show_entries`, triggers an additional full native probe of the same private
exact-byte snapshot. Comparison binds every parsed historical observation,
including tags, disposition, bitrate and empty-container presence. Only
`format.filename` is excluded because the verifier uses a new snapshot path.
Historical full-probe output stays private; accepted receipts return fresh
field-limited evidence. JSON nesting and digest numeric/nesting refusals retain
the primary verification evidence and fixed error text, without exporting
historical metadata. Current and historical installed-tool regressions retain
unchanged evidence and reject changed, missing or added observations.
File-identity checks remain mandatory.
Bounded process diagnostics do not promise general
sanitization of arbitrary output from a substituted tool executable.
Python exceptions expose this evidence, and the existing
`verify-export --require-silent` CLI includes it in its JSON failure response.
These records are failure diagnostics, never successful export receipts.

The caller's artifact is reread after snapshot verification. A changed, oversized,
missing or unsafe caller artifact still fails and retains completed snapshot
observations. Failure records label `artifact_sha256_scope: verification-snapshot`
and retain `artifact_bytes` for that captured snapshot; the hash never claims to
identify revised caller bytes. The request and source-manifest hashes retain
their existing identities.

Portable regressions simulate launch errors at every verifier stage and final
caller changes, including missing/oversized files and unchanged controls. These
fixtures are contract checks, not native decode evidence. The optional native
suite uses real six-frame synthetic H.264 media: an executable-format failure
occurs after two successful installed ffprobe commands, with no started FFmpeg
decode executable or observed decode exit status. Another case changes the
caller bytes after capture while installed ffprobe and FFmpeg successfully
verify the original snapshot; a local CLI wrapper performs actual FFmpeg decode
before changing the caller fixture. Both Python and CLI reject, retain the
snapshot hashes and command results, and omit synthetic private path markers.
Successful unchanged native verification remains a control. These synthetic
media checks do not establish production rendering or artistic acceptance.


### Unambiguous probe facts and native child diagnostics

Probe and frame-timeline JSON reject duplicate object members before deriving
counts or timestamps. Bounded raw transcripts and observed command/status
remain in failure evidence. Malformed transcripts do not manufacture stream
or frame facts; facts established by an earlier unambiguous stage remain.
Portable transcript simulations are separate from actual codec execution.

Native media children exclude dynamic-loader overrides (`LD_*`, `DYLD_*`,
`_RLD_*`) and locale/instrumentation path-report controls, plus FFREPORT and
forced colors. Loader startup diagnostics therefore cannot inherit those
unrelated instrumentation paths. Ordinary environment configuration and the
parent environment remain unchanged. Configured executables must resolve
dependencies without those inherited overrides; a startup refusal remains a
failure with bounded evidence. The unchanged independent control uses actual
installed ffprobe, a synthetic corrupt movie and a nonexistent synthetic
instrumentation library through both public interfaces.

Fresh, current-receipt and historical-receipt probe comparisons use the same
strict JSON admission: duplicate members and nonstandard NaN/Infinity constants
are refused before facts or equivalence are derived. Native controls compare
current and historical-format receipts against the actual six-frame synthetic
movie. The unchanged independent `tests/independent/test_silent_video_receipt_parser.py`
also invokes the pinned older runtime when `SILENT_RECEIPT_BASE` names its
checkout; the automatic full-probe-format test does not claim old-runtime execution.
