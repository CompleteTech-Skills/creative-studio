# Independent Render3D image verification

`render3d_verify.RenderArtifactVerifier(snapshot_root, expected_backend).verify(request, result, output_root)` runs outside Blender. The caller supplies the frozen source directory and the backend identity observed by the worker supervisor. A worker's own success string is insufficient.

The verifier accepts a complete candidate whose verification is still `not-run`. It validates the result shape using a copy marked partial, then separately requires the exact requested frame/pass Cartesian product, complete frame categories, canonical filenames, actual bytes/hashes, formats, precision and dimensions. It reopens the request-bound snapshot and reruns its authoritative source adapter. A Scene3D request also verifies the project/scene/shot/camera selection and shot time range. The report records exact rational presentation/source timestamps and binds the request, settings, snapshot, backend and verifier code identity.

The supervisor-observed backend must also satisfy the requested engine and device
policy. CPU requests require CPU execution; GPU-required requests reject CPU;
GPU-preferred requests permit CPU only with explicit `allow_cpu_fallback=True`.
An explicitly selected GPU must match the observed GPU. Selecting `device="cpu"`
does not override a GPU policy's disabled fallback permission: that otherwise
schema-valid combination cannot authorize CPU output. This fail-closed
interpretation preserves the caller's explicit fallback decision.

The caller writes the returned report with the repository's atomic writer, records its actual hash and byte count as `result.verification.evidence`, and then assigns `result.verification.verifier` from the report and status `passed`. Only then may the caller mark the result complete. Publication and transaction ownership remain with the job controller.

PNG verification uses a real pypng decoder after signature, chunk CRC/order, bounded zlib expansion and complete-file checks. RGB/RGBA 8- and 16-bit noninterlaced frames are supported; animated PNG, unsupported critical chunks, truncated data and trailing bytes reject. Empty consecutive IDAT chunks after zlib end-of-stream are accepted; any nonempty trailing compressed data rejects. All IDAT chunks must form one consecutive run: an intervening ancillary chunk ends that run, and any later IDAT (including an empty one) rejects. PNG color-key transparency (`tRNS`) is explicitly unsupported and rejects before verification or preview decoding can treat the pixels as opaque; transparent frames must use explicit RGBA samples. Sixteen-bit samples remain sixteen-bit through decoding. EXR verification accepts only format-version-2, single-part, non-deep scanline images, with optional long names. Before constructing the native OpenEXR decoder, it checks the captured file's magic/version/flags and bounded header. Multipart, deep/non-image and all tiled images (including single-level, mipmap and ripmap variants), unknown flags/versions, non-scanline storage attributes and tile descriptions reject. Storage attributes are checked even when the file's flags are inconsistent. Header traversal is capped at the smaller of the caller's memory budget and 1 MiB, with bounded name lengths and attribute sizes; malformed or duplicate attributes reject. The independent OpenEXR decoder then verifies every admitted channel, dimensions, HALF/FLOAT precision, full-resolution sampling and finite pixel values. Both `dataWindow` and `displayWindow` must be exactly `(0, 0)` through `(width - 1, height - 1)` for the requested resolution. Cropped frames, larger display canvases and nonzero origins reject; the preview API therefore never discards supported frame placement. Color outputs require RGB and the requested alpha; depth uses a scalar channel, while normals require vector channels. Pixel statistics retain values above one in HDR images. Missing optional decoders fail explicitly.

Install optional verifier dependencies alongside the existing 3D dependencies:

```bash
python -m pip install OpenEXR==3.5.4 pypng==0.20220715.0
PYTHONPATH=scripts python -m unittest discover -s tests -p test_render3d_verify.py -v
```

`decode_image(path, output=..., resolution=..., pass_name=..., max_bytes=..., memory_bytes=...)` returns `(metadata, channel_arrays)` from the same validated bytes for previews. PNG arrays contain normalized display-encoded samples; EXR arrays retain linear/raw samples. Callers must explicitly map depth and normal values for display. This interface enforces conservative decoded-size checks; it does not claim a hard operating-system resident-memory limit for native decoder libraries. Worker isolation and memory enforcement remain controller responsibilities.

Both standalone image APIs validate the output format, precision, color and alpha
policy, dimensions and selected pass before opening the image. `max_bytes` and
`memory_bytes` must be integers in `1..2**40`; booleans, floats and nonpositive
values reject before capture or decoder construction. Standalone EXR inspection
can verify one depth or normal pass without a combined pass. Full render requests
still require the combined pass through the canonical settings contract.

The verification result proves decoded image integrity and input identity binding. It cannot prove that a render interpreted a material correctly or that a supplied image actually came from Blender. The supervisor's retained process evidence and the separate real-geometry, shadow, lighting, deformation, camera and color acceptance tests provide that evidence. Likewise, declared color-space/alpha policies are retained as requested metadata; pixel decoding alone cannot prove the scene's color transforms or alpha association. Numerical ranges are evidence, not production artwork approval.

## EXR envelope correction and evidence boundary

This bounded PR #276 correction addresses issue #93's image/pass integrity
requirement. It prevents the legacy decoder's default part or base tile level
from being mistaken for verification of a whole multipart or multilevel artifact,
and rejects deep storage before a native deep compositor can initialize.
The private captured stream remains the common input for hashing, envelope
admission and pixel decoding. Existing HALF/FLOAT, channel, window, alpha and
HDR checks remain in force.

The envelope definitions and fixture API were checked against pinned upstream
OpenEXR 3.5.4 sources:
[version flags](https://github.com/AcademySoftwareFoundation/openexr/blob/v3.5.4/src/lib/OpenEXR/ImfVersion.h),
[file layout and storage attributes](https://github.com/AcademySoftwareFoundation/openexr/blob/v3.5.4/website/OpenEXRFileLayout.rst),
[legacy decoder initialization](https://github.com/AcademySoftwareFoundation/openexr/blob/v3.5.4/src/lib/OpenEXR/ImfInputFile.cpp),
and [Python fixture API](https://github.com/AcademySoftwareFoundation/openexr/blob/v3.5.4/website/python.rst).

The added tests exercise both `verify_image` and `decode_image`: valid HALF/FLOAT
scanline controls including long names, actual synthetic multipart files with a
valid first part and invalid second-part samples/types, and actual synthetic
tiled/deep files. Constructor spies establish the required refusal boundary;
small hand-built malformed envelopes cover unsupported flags, mipmap/ripmap
tile descriptions, inconsistent storage metadata and header bounds. These
fixtures are decoder contract tests, not Blender renders or production evidence.

The original envelope correction was authored without an executor. Its historical
unexecuted status does not describe the later local decoder-admission correction.
That correction passed 29 focused verifier tests and all 88 Render3D tests with
OpenEXR 3.5.4 and pypng 0.20220715.0 on Linux, without skips. These are synthetic
PNG/EXR decoding and local contract tests, not Blender, GPU or production runs.
The focused hosted workflow is
`.github/workflows/render3d-verification.yml`, using
`requirements-render3d-test.txt` and
`PYTHONPATH=scripts python -m unittest discover -s tests -p test_render3d_verify.py -v`.
Only a retained hosted result on the corrected commit can establish that run's
outcome. This change does not close issue #93, establish broader rendering or
semantic acceptance, or replace controller-enforced native memory limits.

Image capture accepts active directory capabilities through the existing
no-follow payload reader. PNG empty trailing IDAT support is preserved. The EXR
envelope checks both full-frame windows before decoding and checks the scanline
chunk inventory for compression identifiers 0 through 9. Other compression modes
reject before the native decoder is constructed, including HTJ2K32, HTJ2K256,
LJ2K and ZSTD supported by the pinned decoder. Decoder support alone does not
establish complete-file verification. Genuine fixtures for those modes and
trailing-byte variants exercise this refusal; existing admitted HALF/FLOAT
controls remain in the suite.

`verify_result` and `RenderArtifactVerifier` accept an optional `max_total_bytes`
for source closure verification. Supplying it or a scoped snapshot requires the
bounded snapshot owner. If unavailable, the verifier returns `unsupported-feature`
instead of accepting a limit it cannot enforce. Ordinary source verification and
the independent encoded image budget remain available on the foundation base.


## PR #534 policy and capture boundary correction

The current Render3D verifier admits the exact observed device identifiers `cpu`
and `cuda`. These are the canonical CPU and GPU identifiers exercised by the
existing Render3D fixtures. The source tree has no Render3D worker identity
producer that establishes a broader hardware-ID mapping. The separate Director
probe's uppercase `CPU`/`GPU` output is not a Render3D `BackendIdentity`.
Consequently, `CPU`, `CUDA`, `unknown`, `unavailable`, and opaque hardware IDs
reject with `unsupported-device`, including when an opaque ID was explicitly
selected. An explicit selection still must equal the admitted observed GPU;
CPU fallback still requires `gpu-preferred` and `allow_cpu_fallback=True`.
A future hardware owner must supply an authenticated device class before
arbitrary per-device IDs can be admitted. These policy tests use synthetic
supervisor identities and do not establish GPU execution.

Standalone capture now uses `rig_errors.open_payload` for ordinary paths as
well as active directory capabilities. Its bounded read is followed by the
owner's file identity checks before the private capture is admitted for hashing
and decoding. Full-result verification supplies the original output root,
original relative filename, and pinned root identity to that same reader.
The inventory is enumerated beneath the same pinned output root: POSIX uses
the existing `atomic_publish.pinned_directory_fd` adapter, descriptor-relative
child opens and a final no-follow namespace identity recheck; Windows uses the
existing `atomic_windows.pinned_directory` adapter to deny replacement of held
directory ancestors during reads and inventory traversal. Active capabilities
keep their original directory authority even if an ordinary pathname is
replaced. No path is resolved to discard that authority.

EXR admission now parses the bounded `chlist` attribute before constructing
`OpenEXR.InputFile`. It requires a terminated, nonempty channel list without
duplicate names, no more than 64 channels, requested HALF/FLOAT precision,
full-resolution sampling, and valid channel metadata. It applies the existing
four-byte-per-decoded-sample estimate, or the stricter eight-byte preview
estimate, before native construction. The encoded field layout follows the
upstream [OpenEXR channel-list layout](https://openexr.com/en/latest/OpenEXRFileLayout.html#attribute-type-layouts).
Native decoding retains its independent channel checks. The 1 MiB header cap
and complete-file chunk accounting remain in force. These are admission and
sample-size checks; no native allocator or peak-RSS bound was measured.

The dedicated test file retains all 29 original test methods and their 79
assertion calls unchanged. Added regressions retain all six independent policy
probe cases and perform real disposable filesystem parent/root replacements,
with stable-path, restored-path and scoped-authority positive controls. Mocks
only schedule replacements or spy on constructor calls; image bytes, hashing,
accounting, identity checks and decoding execute normally. Added malformed
channel headers and actual synthetic 65-channel/over-budget EXRs must reject
before native construction. Native Windows execution is not established by
the Linux tests; using its existing pin adapter is an implementation claim.
Issues #92, #93 and #16 remain open beyond this bounded decoder correction.


## Diagnostic, aggregate-budget and inventory boundaries

Public diagnostics use stable categorized text. Standalone image failures use
`path="image"`, because even a caller's basename can disclose private information.
A result image failure can use its already checked canonical `frames/...` or
`passes/...` identity; source-snapshot failures use `snapshot.json`. Absolute
caller paths, arbitrary input values and native/OS exception text are excluded
from public error messages and paths. Owner contract/source failures retain their
error category and code while replacing their details with fixed text. Native
exception chains are suppressed at these public wrapping boundaries.

The aggregate encoded-byte budget is checked before opening each next image.
If the previous capture exactly consumed the available bytes, the next image
fails with `storage-exhaustion`; a valid render budget does not become an
invalid standalone-image input. An exact budget for the complete sequence remains
valid. Inventory traversal uses context-managed streaming `os.scandir` against
the held POSIX directory descriptor or pinned Windows path. It rejects on the
first excess file without materializing all directory names. Existing no-follow
opens, directory identity checks and exact inventory comparison still apply.

Image hashes and statistics describe the privately captured byte sequences.
They do not freeze the live destination after capture. Final inventory traversal
checks names and directory authority, rather than rereading each image's bytes.
The controller must retain an immutable candidate while verification and
publication run, or revalidate the candidate at its publication boundary.
An additional final hash pass would provide another observation time; it would
not prevent a later mutation or replace the controller's publication authority.
This verifier therefore does not add such a pass or claim an immutable live
output tree. Its returned limitation states this capture-evidence boundary.

Added regressions use actual missing/empty/over-budget synthetic files, exact
aggregate-budget boundaries and a real streaming directory scan that stops
at the first excess entry. Injected native/owner errors test only public
sanitization. A disposable mutation after capture demonstrates that reported
hashes bind captured bytes and that a caller still needs publication retention
or revalidation. These controls do not establish real-render or production
acceptance.


## Captured PNG extent and result-file materialization

PNG admission updates CRCs incrementally from the chunk type and payload.
During IDAT admission the input block, bounded inflate output and possible
unconsumed input suffix share the working budget. Admission releases each
payload before reading the next. The pixel decoder still uses its own buffers.
The supported RGB/RGBA profile excludes optional suggested palettes (`PLTE`).

Before reading a PNG chunk payload, the verifier checks its declared length
against both the configured memory budget and the actual remaining extent of
the private capture, including the four-byte CRC. A tiny truncated capture
therefore cannot cause a payload-sized read merely by declaring a large chunk.
The read-spy regression limits every permitted request to 64 bytes and checks
that only the eight-byte prefix or a valid thirteen-byte IHDR is requested in
these refusals; it does not request or allocate the declared large payload.
Existing actual 8/16-bit PNG decoding controls remain valid.

`RenderArtifactVerifier` now explicitly requires each canonical frame/pass output
to be an independently materialized regular file. The original stat record
returned by `open_payload` must report exactly one hard link before capture;
the held descriptor is checked again after the read, and final inventory
metadata retains the same single-link requirement. Missing link-count evidence
fails closed for result outputs. This rejects hard-link aliases between canonical
output names and hard-link aliases outside the candidate, even if their bytes
and declared hashes match. It does not discover arbitrary external symbolic
links to a canonical result file. Separate files containing identical pixels
remain valid.

This is a result-output materialization policy. Standalone `verify_image` and
`decode_image` intentionally continue to inspect an explicitly supplied linked
source; snapshot source captures also retain their existing ownership semantics.
The result policy is enforced before decoding using opened metadata, rather
than inferred from filenames or a later path-only observation. Disposable
fixtures use actual `os.link`, ordinary independent file writes, and a scheduled
unlink after opening to establish these admission boundaries. No native Windows
execution is claimed by the Linux tests.

These checks concern the captured source and observed inventory. They do not
freeze the live publication candidate, prevent every later alias or byte change,
or replace the controller's retention/revalidation responsibility described
above. The existing capture-evidence limitation remains in the returned report.


## Complete legacy EXR scanline consumption

The verifier's `code_sha256` is the canonical object hash of a mapping from
`render3d_verify.py` and `exr_consumption.py` to their individual SHA-256
file digests. This binds the verifier and its dedicated codec checks in the
existing verifier identity field.

Compression IDs 0–9 retain their full-size raw fallback and native HALF/FLOAT
pixel decoding. Before native decoding, the verifier now checks each bounded
compressed chunk and its internal streams: exact RLE output, exact zlib input
and transformed length, PIZ bitmap/Huffman extents, B44 block extents, and DWA
classified channel sizes, component extents and consumed AC coefficient words.
Huffman table location comes from actual code-length entries; its advisory
length field and ignored padding retain their existing compatibility. DWA
classification uses serialized rules and the last matching channel rule.
Declared lengths do not authorize allocation before geometry and working-budget
checks. These checks supplement the native pixel decoder and do not establish
scene, lighting or artistic correctness.

Format references are pinned to OpenEXR v3.5.4:
[codec dispatch/raw fallback](https://github.com/AcademySoftwareFoundation/openexr/blob/v3.5.4/src/lib/OpenEXRCore/compression.c),
[Huffman frames](https://github.com/AcademySoftwareFoundation/openexr/blob/v3.5.4/src/lib/OpenEXRCore/internal_huf.c),
[PIZ](https://github.com/AcademySoftwareFoundation/openexr/blob/v3.5.4/src/lib/OpenEXRCore/internal_piz.c),
[PXR24](https://github.com/AcademySoftwareFoundation/openexr/blob/v3.5.4/src/lib/OpenEXRCore/internal_pxr24.c),
[B44](https://github.com/AcademySoftwareFoundation/openexr/blob/v3.5.4/src/lib/OpenEXRCore/internal_b44.c),
and [DWA](https://github.com/AcademySoftwareFoundation/openexr/blob/v3.5.4/src/lib/OpenEXRCore/internal_dwa_compressor.h).

The independent positive suite generates 40 RGBA and eight scalar-Z files with
the installed native writer, makes 96 public verification/decode calls across
all ten codecs and both precisions, and checks four additional native-only
layered/custom DWA fixtures. Those custom layouts are not public color/depth
admission evidence. Bounded refusal controls alter tiny native-writer fixtures
and assert rejection before native decoder construction; they do not claim
malformed native decoding. Native positive controls also retain advisory
Huffman metadata and the DWA deflate AC representation. The dedicated workflow
requires both test modules and the original verifier suite to execute with no
skips.
