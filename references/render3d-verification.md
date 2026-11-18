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
