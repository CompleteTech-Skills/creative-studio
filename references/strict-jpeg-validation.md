# Strict JPEG admission

`rig_jpeg.verify_jpeg` provides a warning-aware JPEG boundary for optional native
3D imports. Pillow can return a complete-looking raster after a damaged entropy
segment. In the retained engineering regression, removing 5, 20, 100 or 300 bytes
from a JPEG and adding EOI makes Pillow load pixels while libjpeg reports a
premature entropy segment. The new boundary rejects those recovered inputs.
It does not repair, transcode, or replace source bytes.

The implementation uses pinned `simplejpeg==1.9.0`. Its maintained
[API documentation](https://github.com/jfolz/simplejpeg/blob/1.9.0/README.rst)
specifies that strict decoding rejects recoverable errors; the
[versioned implementation](https://github.com/jfolz/simplejpeg/blob/1.9.0/simplejpeg/_jpeg.pyx)
checks the native decoder result in strict mode. Header and full-pixel calls
both pass `strict=True` explicitly. It is installed through
`requirements-rig.txt`, never `requirements-delivery.txt`.

The [PyPI 1.9.0 release](https://pypi.org/project/simplejpeg/1.9.0/#files) publishes
CPython 3.12 wheels for Windows x64/ARM64, macOS x64/ARM64 and Linux x64/ARM64.
All six artifacts were actually downloaded and matched their published SHA-256
in this workstream; that proves availability, not execution on those machines.
The project's [license](https://github.com/jfolz/simplejpeg/blob/1.9.0/LICENSE)
is MIT and its wheels retain bundled library license notices. This repository
adds a dependency pin, not a copied third-party decoder implementation.

## Admission and resource boundaries

The public function accepts immutable bytes and explicit encoded-byte and
working-memory budgets. A marker walk bounds scan count, rejects incomplete
segments/missing EOI/trailing data, and skips length-delimited metadata so
marker-like EXIF/ICC bytes are not mistaken for scans. Native libjpeg-turbo
still interprets and validates entropy; the marker walk is not an entropy
decoder. The default maximum is 256 scans, a documented profile restriction.

Before full pixel decoding, the native header must have positive dimensions,
at most 16,777,216 pixels, and a conservative 16-bytes-per-pixel working estimate
within the caller's remaining allowance. Optional expected width/height must
match. Gray and CMYK/YCCK are decoded in their own channel counts, avoiding an
extra CMYK-to-RGB buffer. The return value contains source SHA-256, dimensions,
decoder identity and scan count. It never includes replacement encoded bytes.

Missing or differently versioned decoder dependencies produce
`RIG_TEXTURE_VERIFIER_UNAVAILABLE`. Warning/error, malformed-source and decoded
shape failures produce `RIG_TEXTURE_DECODE`; budget failures use
`RIG_LIMIT_TEXTURE`. No successful verification is returned on those paths.

The memory figure is a conservative admission estimate, not an OS-enforced
allocation sandbox or wall-time limit. Managed imports still need their
existing process-level limits. Numeric decode success does not prove source
rights, artistic quality, original intended pixels, or human approval. A
different, still-valid JPEG bitstream cannot be authenticated without an
independently supplied original digest.

## Verification and integration

Run the strict codec family using the optional rig environment:

```bash
python -m pip install -r requirements-rig-test.txt
PYTHONPATH=scripts:tests python -m unittest test_rig_jpeg -v
```

The tests use actual baseline/progressive RGB, grayscale and CMYK JPEGs,
small/odd dimensions, entropy-damaged native fixtures, source-byte hashes,
actual oversized SOF metadata, exact admission boundaries, metadata containing
fake marker patterns, dependency failures and an actual dependency-free import
subprocess. Spies only establish that budgets reject before full allocation;
successful image and corruption evidence uses the real native decoder.

`tests/fixtures/strict-jpeg-valid.jpg` is the exact procedural engineering
fixture retained by the native texture workstream, SHA-256
`f7aa64cf9f5f369a45ee48c602577ff4f7d7da1bdb1fc674ea907dae6cd284ec`.
It is neither production artwork nor a fabricated review receipt.

Two additional arithmetic-coded fixtures (baseline and progressive) were
generated from seeded 32×32 RGB engineering pixels by the actual local
`cjpeg` 3.2.0 encoder. Their committed bytes are independently decoded by the
pinned verifier; tests do not require the external encoder executable.

The native decoder now calls this verifier at its sole shared image-admission
boundary, before accepting JPEG pixels. Real GLB bufferView, glTF embedded
buffer, external-file and data-URI, and OBJ/MTL regressions exercise the callsite.
They reject all four canonical warning-producing entropy cuts, preserve valid
baseline/progressive Gray/RGB/CMYK and arithmetic bytes, and verify no run is
published on failure or missing dependency. The three-OS workflow runs both
the standalone codec and native-route test families; optional independent
Khronos/Blender/browser coverage remains in the existing interoperability gate.

Arithmetic coding has a concrete authenticity limit: a marker can complete a
changed arithmetic-coded stream using legal zero padding. Eight altered
baseline/progressive fixtures remain valid under both the strict decoder and
independent `djpeg`, with no warning. The
[upstream arithmetic decoder](https://github.com/libjpeg-turbo/libjpeg-turbo/blob/3.1.2/src/jdarith.c#L125)
defines this behavior. It must not be mislabeled a malformed-source warning
bypass or used to reject every arithmetic JPEG. Detecting a different valid
source still requires an independently supplied original digest. Parent issues
#52, #44 and #139 remain open.
