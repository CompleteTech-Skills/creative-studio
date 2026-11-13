# Lossless PNG and OpenEXR image-sequence masters

`scripts/silent_sequence.py` exports complete deterministic `frames/000000.png`
or `frames/000000.exr` inventories. It copies original encoded bytes: no resample,
color transform, flattening, compression change, precision conversion or renderer
is introduced. PNG supports actual RGB/RGBA 8/16-bit sRGB; EXR supports actual
half/float RGB/RGBA scene-linear masters. An 8-bit display PNG cannot be requested
as an EXR/HDR export. Canonical image decoding rejects incorrect precision,
channels, dimensions, nonfinite EXR pixels and incompatible alpha declarations.

Native source admission calls the renderer owner's `verify_result`, checking the
request-bound snapshot, exact backend identity and complete frame/pass inventory.
The independent `verify_image` decoder then checks every copied image again.
Native EXRs retain their scene-linear values, including values above 1 and
straight alpha. No visual/semantic approval is produced. Imported files are a
separate `imported` origin: their explicit color attribution is the caller's
assertion, and their file format alone cannot establish genuine rendered HDR
provenance. Native provenance requires `export_native_sequence`, not relabeling
an imported source.

```python
from silent_sequence import export_native_sequence, verify_sequence

receipt = export_native_sequence(
    request, result, render_root, destination,
    snapshot_root=snapshot_root, expected_backend=observed_backend,
)
verified = verify_sequence(destination)
```

The expected backend must come from the executing renderer caller, rather than
blindly accepting the result's own claim. `request` and `result` use canonical
Render3D owner contracts. The supported subset has exactly the combined pass,
contiguous integer source frames, at most 18,000 frames, 4K/8,847,360 pixels per
frame, 256 MiB per encoded frame, 2 GiB aggregate encoded bytes, 512 MiB decoder
memory budget and 16 MiB per metadata document. Resource admission precedes
native decoding. Unsupported multipass or larger sources fail explicitly.
Optional canonical Render3D decoder dependencies (pypng, NumPy and OpenEXR) are
required for verification; no 3D packages are imported by ordinary 2D workflows.

For imported masters, call `build_source(root, ordered_paths, fps=..., output=...,
identity={'resolution': [width, height], ...})`, followed by
`export_sequence(source, root, destination, provenance=identity)`. Rational FPS
uses the same numerator/denominator contract as silent MP4/WebM. The sequence
sidecar records each output index, original source index/path, exact bytes/hash,
source format/precision/color/alpha, original source identity and decoder channel
ranges. Source indices may start above zero; output names always start at zero.
The source, request and artifact schemas are
`profiles/silent-sequence-{source,request,artifact}.schema.json`; relational,
filesystem and decode verification remain mandatory beyond structural schemas.

CLI uses a separate explicit command; legacy audiovisual export is untouched:

```bash
python scripts/silent_sequence.py export-native \
  --request render-request.json --result render/result.json --root render \
  --snapshot-root render/snapshot --backend observed-backend.json \
  --destination delivery-sequence
python scripts/silent_sequence.py verify --root delivery-sequence
```

`sequence.json` retains complete original request/result and owner verification
alongside byte-bound source mapping. For immutable transport, preserve its exact
SHA-256 externally and pass `--receipt-sha256 HASH` during verification. Offline
verification checks the portable receipt identities, exact contiguous inventory,
checksums and all decoded images; it does not claim to re-open a native snapshot
that was not packaged with the sequence. Original snapshots and owner artifacts
remain with the renderer. The receipt is integrity evidence, not a signed
attestation or authorization to trust an arbitrary edited receipt.

Output publication requires Linux procfs and secure descriptor-relative,
exclusive rename, using the existing silent-media filesystem helper. The final
destination must not exist, even as an empty directory. All original frames
remain unchanged. Work happens in the sibling `.DESTINATION.sequence-stage`,
protected by a process lock and exact source/provenance plan. Cancellation or
interruption retains verified cached frame bytes; rerunning the same export
revalidates and reuses them. Completed frames and immutable destinations are
never overwritten. Only recognized incomplete temporary writes under that bound
staging plan are removed on resume. Corrupted cache frames, foreign plans,
symlinks, special files and unknown staging entries fail closed. If interruption
occurs before a complete plan exists, an incomplete unidentifiable plan is
rejected; inspect that owned staging and explicitly select a new `stage_name`
rather than treating unknown bytes as a reusable cache.

The tests label all generated alpha/precision inputs as engineering fixtures.
Real renderer-owner smoke artifacts separately demonstrate actual source-bound
PNG8 (three frames), PNG16, EXR half and EXR float exports. The latter source
smokes each contain one frame; they establish those complete one-frame source
exports, not a three-shot production film. Full silent-film/CLI integration and
release regression evidence remain with #97/#241 and the canonical renderer.
