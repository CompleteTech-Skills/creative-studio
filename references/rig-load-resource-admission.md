# Native rig loading with caller resource limits

`rig_models.load_rig(root, verify_source=True, *, limits=None,
expected_manifest_sha256=None)` accepts a partial
runtime limits dictionary. For example:

```python
rig = load_rig(root, limits={"memory_bytes": 128 * 1024**2,
                            "dependency_bytes": 64 * 1024**2})
print(rig.resource_usage)
```

Each effective limit is the smaller of the source manifest limit and the validated
caller value. Values must be finite, positive, bounded integers; integral floats
are accepted on a runtime copy. Unknown keys and booleans are rejected. The returned
`rig.manifest`, source bytes, content hashes and `rig_signature` are unchanged.
Increasing a caller limit never increases the manifest limit.

Callers holding an immutable rig reference can pass `expected_manifest_sha256`.
The shared bounded JSON reader verifies the exact captured `rig.json` bytes
against that pin before parsing or loading native arrays. Whitespace differences
also fail with `RIG_HASH`; object equivalence does not establish byte identity.
Omitting the pin preserves the existing load behavior. A caller must pass its pin
on every native reload where a bound reference remains authoritative.

Before the first NumPy array allocation, admission inspects the complete native
geometry/skin bundles and all retained JSON inputs. It measures actual NPY
shape/dtype/payload lengths, mesh/influence/morph counts, clip timing/key counts,
encoded files and PNG/JPEG dimensions. Manifest-declared vertex counts alone do
not authorize allocation. Archive directory size/member counts are checked before
constructing ZIP entry lists; unsafe names, pickles and forged array payload sizes
are rejected. Only unencrypted stored/deflate ZIP members are admitted; BZIP2/LZMA
workspace is not silently charged as ordinary deflate scratch. Bounded immutable binary snapshots bind the inspected header and
loaded data to the descriptor hash, including if an input changes between preflight
and loading. `verify_source=False` continues to skip original-source availability
and hash verification; it does not skip native bundle admission or hashes.

The `native-load-conservative-v1` working-set estimate charges a 2 MiB I/O/header
reserve, encoded bundle snapshots, ZIP directory/entry overhead, four times expanded
bundle bytes, 64 times retained JSON bytes, and four times decoded RGBA texture
bytes. PNG IHDR and JPEG SOF dimension-only probes follow the [PNG specification](https://www.w3.org/TR/png/#11IHDR) and [ITU-T T.81 frame header](https://www.w3.org/Graphics/JPEG/itu-t81.pdf). They do not instantiate image decoders or decompress ancillary text, and do not certify raster integrity. Repeated image records share one immutable encoded byte copy per native data key; locked-index header length must match its descriptor before any array/list allocation.

All geometry, attributes, morphs, skins, clips and retained review JSON share
one budget; per-file admission cannot waive the aggregate. Header-derived RGBA capacity counts even when the encoded PNG is tiny. JSON and NPY input sizes are
bounded before materializing their payloads. Source/dependency bytes also have a
shared byte inventory. A second aggregate check during loading prevents a changed
input from escaping the preflight limits.

`rig.resource_usage` reports this estimate and the observed input/array/JSON bytes plus header-derived texture capacity. These are conservative **loading-only admission estimates**, not
measured peak RSS, an operating-system memory sandbox, CPU scheduling, proof that
an image is visually correct, or authorization for later rendering/evaluation.
NumPy/Python/library resident memory is outside this additional-load estimate.
Callers must reserve their own output and evaluator/renderer memory, enforce
operation deadlines and ownership, and verify source pins again after an operation.
In particular, retaining one loaded rig while loading a second rig requires separate
reservation; this API does not make duplicate live objects free.

Clip, identity and review JSON admission verifies each captured bounded byte
snapshot against its immutable reference before parsing. Preflight and live clip
loads both enforce caller duration, FPS and aggregate key counts. A substituted
short clip cannot authorize loading a different manifest-bound longer clip.
JSON, hashing and binary snapshots use the same descriptor-safe reader, including
when a regular file becomes a FIFO between size admission and opening.

The `test_rig_load_limits.py` family is part of the existing skinning CI gate. Its
real retained robot exercises signature/hash preservation, aggregate multi-skin and
clip admission, 48 actual 512×512 PNG textures under a stricter caller cap, compressed
pixel expansion, malformed NPY/ZIP inputs and rejection before array materialization.


The shared loader also retains explicit scoped directory roots through native reads.
Metadata and array ZIP members use the canonical 64 KiB bounded reader, NumPy
headers pass the bounded header-reader adapter before parser allocation, and
JSON/hash/binary reads use the shared no-follow descriptor reader.
