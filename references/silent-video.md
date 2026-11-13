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
encoder. `source_from_blender_manifest(root, manifest)` accepts a strict
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

Independent verification copies the encoded bytes into another private snapshot
so a mutable source pathname cannot switch between probe and decode. It runs:

- unfiltered `ffprobe -count_frames -show_streams -show_format -of json`;
- actual frame enumeration with integer presentation timestamps;
- `ffmpeg -xerror -err_detect explode` decoding of the complete video.

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
Cancellation callbacks run during encoding and verification, with no callback
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

## Ownership and traceability

| Work | Authority / evidence |
| --- | --- |
| Legacy audio review, source frames, WAV timing | Unchanged production renderer, contracts, adapter and regression tests |
| Silent contracts, capabilities, limits, inventory | #223; `SilentContracts`, schema-parity and admission tests |
| Zero-audio independent verification | #227; real AAC/Opus, multiple-video, corruption, timing and metadata negatives |
| Explicit H.264 encoding and exclusive publish | #228; real 24/25/30/60/30000:1001 outputs, cancellation and existing-destination tests |
| Render manifests and complete multi-shot 3D integration | #96/#97/#229; renderer-owned provenance required |
| Lossless PNG/EXR delivery, HDR/alpha, profiles | #235/#236/#237; not completed by this implementation |
| Persistent jobs/recovery/batches and film acceptance | #239/#240/#241; not completed by this implementation |

Real acceptance tests create labeled synthetic PNGs and run installed media
binaries. They do not claim 3D geometry or production-art approval. See
`tests/results/silent-video-foundations.json` for executed results, source
revision, versions, logs and limitations. Run focused acceptance with
`python -m unittest discover -s tests -p test_silent_video.py -v` runs the
portable contract checks without optional skips. The dedicated Linux media
gate runs `HATCH_SILENT_MEDIA_TEST=1 python -m unittest discover -s tests/optional
-p test_silent_video_native.py -v` with real FFmpeg/ffprobe and descriptor
publication tests. Its optional directory is intentionally not a package, so
portable required discovery does not misreport unsupported native encoding as
executed or relax its unexpected-skip policy.
