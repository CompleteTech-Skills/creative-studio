# Actual native lossless sequence exports

These are actual exports of the canonical Blender backend owner's retained
procedural engineering fixtures, not artwork or visual approval. `render` is a
complete three-frame PNG8 source; `png16`, `exr16` and `exr32` are complete
one-frame format-boundary sources. EXR half/float values above 1 remain unchanged.
The exporter copied exact encoded bytes. Independently decoded first/middle/last
samples (deduplicated for one-frame sources) match every source channel exactly.
Alpha retains opaque, transparent and semitransparent pixels where present.

Each directory includes a portable `sequence.json`, source-bound `plan.json`,
process-lock inode and exact `frames` inventory. `execution.json` records original
native ownership, actual hashes, observed runtime module origins, source commit,
all four exports, channel/precision metadata and test logs. No new renderer or
image conversion was used. Original snapshot closures remain with their renderer
owner; these exports do not claim to package or re-open absent source snapshots.

This increment depends on the canonical Render3D verifier (`render3d_verify`,
`render3d_contracts`, `render3d_snapshot` and compatible `rig_errors`) plus the
optional packages in `requirements-sequence.txt`. Those owner modules are not
vendored by this branch. Once integrated, normal Python imports resolve them in
the repository. Before integration, use explicit compatible owner checkouts;
never silently mix mismatched shared owner modules through an accidental
`PYTHONPATH` order. The proof preloaded the canonical verifier's `rig_errors`
from owner revision `72bb9ac`, then imported this media branch, retaining the
updated media `atomic_publish` implementation. Every loaded repository module's
actual path and SHA-256 is recorded in `execution.json`; the first failed mixed
import attempt was preserved in owned scratch evidence.

An explicit bootstrap for the recorded staged checkouts is:

```python
import sys
sys.path.insert(0, str(canonical_verifier_checkout / 'scripts'))
import rig_errors  # compatible canonical decode_json(max_bytes=...)
sys.path.insert(0, str(media_checkout / 'scripts'))
from silent_sequence import verify_sequence
verify_sequence(media_checkout / 'examples/silent-sequence235/exr32')
```

This is pending owner integration, not a claim that installing only the ordinary
2D requirements supplies a native verifier. See
[the sequence guide](../../references/silent-sequence.md) for the API, CLI,
resource limits, resumable staging and integrity/approval boundaries. The
remaining full film, shared CLI and release integration remain with #97/#241.
