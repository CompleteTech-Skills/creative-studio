# Editable clip documents and exact motion reuse

`animation3d_clips.py` edits the existing Animation3D clip/event/track contracts.
These operations produce detached validated documents. They do not create a
second scene evaluator, animation schema, asset store or editor database.
Coordinates and skinning remain with the rig owner; the native evaluator consumes
saved edited tracks and actually deforms their bound meshes.

## Timelines, keys and history

`create_clip(id, name, fps, duration_frames, provenance, ...)` constructs an editable
clip using caller-supplied provenance. An empty timeline does not establish motion
or approval. `edit_clip` changes its ID/version/name, rational frame rate, duration,
tags, categories, loop/completion policy, default playback speed, and actual tracks
or events. Trimming rejects stranded keys/events/contacts instead of dropping them.
Changing FPS preserves key frame indices; it changes their corresponding seconds.

`edit_key`, `copy_keys`, `paste_keys`, `move_keys`, `retime_clip` and `reverse_clip`
reuse the same typed key data. Time uses one million discrete ticks per frame.
Numeric offsets/rates accept integers, `Fraction`, and finite decimal floats;
float spelling is interpreted as an exact decimal rational at this edit boundary.
Unrepresentable tick positions, nonfinite values, booleans and collisions reject.
Hermite derivative units and Bezier handle time offsets scale with retiming.

`ClipHistory(document, max_edits=256, max_bytes=64*1024*1024)` commits only valid complete edits. The retained current/undo/redo documents share one conservative JSON-object estimate, checked before cloning the next owned state. Undo/redo
return detached values; failed operations preserve both the current state and
history. Reaching its explicit 1–4096 edit capacity or aggregate estimate fails rather than silently
evicting history. This is an in-process working history; EditorRepository owns
persistent transactions and history publication.

```python
from animation3d_clips import ClipHistory, edit_clip, splice_frames
history = ClipHistory(saved_clip)
history.apply(edit_clip, name='Revised', tags=['walk'])
history.apply(splice_frames, 8, 4)  # Insert a four-frame held range.
restored = history.undo()
revised = history.redo()
```

## Split, join and frame ranges

`split_clip(clip, frame)` returns left/right clips with an exact integer seam.
Plain events at the seam belong to the right. Contacts are clipped to their
half-open source intervals. The left entry pose and right exit pose are retained;
unknown new seam pose IDs are omitted instead of reusing the wrong named pose.
Interruption marker references follow the events that actually remain.

`join_clips(left, right)` requires matching execution/rig/source metadata and
compatible track sets. Actual seam values must agree; opposite quaternion
hemispheres representing the same rotation are accepted. Differing poses require
an explicitly authored transition. Contacts split from one ID may rejoin when
their metadata and intervals agree. Conflicting event/region IDs reject. Tags,
categories and interruption marker names are combined; the outer entry/exit pose
IDs remain with their respective ends. Source clips are unchanged.

Polynomial editing converts Hermite, easing, Catmull–Rom and TCB segments to
mathematically equivalent Bezier controls before subdivision. Weighted Bezier
curves use their actual time/value control geometry. SLERP and stepped channels
retain their own interpolation. Regression tests compare arbitrary source times,
including new boundaries, instead of merely checking key counts.

`splice_frames(clip, start, count)` inserts a held interval for positive count.
Unchanged intervals retain their source curve shape. Negative count removes
`[start, start-count)` and creates an intentional stepped cut at the new seam:
the old left value remains through the last representable preceding tick, and
the right source value begins exactly at the seam. Events inside the removed
range are explicitly removed; contact intervals are clipped. The operation,
source semantic hash, location and frame count are recorded in existing
provenance parameters. Empty/overflowing durations reject.

## Explicit bounded SQUAD conversion

An exact SQUAD subdivision cannot be represented by simply dropping neighboring
keys: the canonical SQUAD controls depend on those neighbors. A split, splice or
join containing SQUAD therefore rejects unless the caller requests an explicit
conversion with `registry=...`, `quaternion_tolerance_rad=...` and
`max_bake_keys=...` (default 4096, maximum 250000 across the operation).

The admitted target registry pins the real source channel. Reference samples
use canonical `sample_track`; the adaptive conversion emits ordinary shortest-arc
SLERP keys. Each interval checks quarter/mid/three-quarter probe errors, recursively
subdividing on the exact tick clock. The source semantic hash, tolerance, measured
maximum accepted probe error, sample count and key count are recorded in
`provenance.parameters` under distinct `squad_bake_N_` names. Work, depth, key and
provenance capacities are bounded; unmet tolerance rejects rather than silently
returning a coarser result. Existing conversion records are retained.

The tolerance is a deterministic numerical sampling criterion, not a formal
continuous supremum certificate. Independent regression samples include
near-antipodal quaternion encodings and nonsequential times. Original source
clips remain available for inspection; no visual approval is implied.

## Named regions through existing markers

`set_region(clip, id, name, start_frame, end_frame)` atomically creates/updates two
ordinary marker events for a half-open integer range. The reserved payload
`animation3d_region_kind: "boundary-v1"` declares an authoritative pair; its other
fields are `animation3d_region_id`, `animation3d_region_name`,
`animation3d_region_boundary` and `animation3d_region_peer`. Event IDs are
`ID:start`/`ID:end`, names are `NAME:start`/`NAME:end`, and the scope is `clip`.

`validate_named_regions(document)` is a pure semantic hook: it performs no file
I/O and never calls `validate_contract` or `_clip`. It rejects missing/repeated
boundaries, unknown versions, inconsistent names/peers/IDs, wrong event types and
empty/reversed ranges. The central contract owner integrates this hook into clip
validation; every edit in this module also applies it locally.

`region_range(clip, id)` returns exact `source_in`/`source_out` arguments for
`source_time`; selecting the region therefore changes actual evaluated native
geometry. `remove_region` removes both markers atomically. Reverse swaps boundary
roles, IDs/names and exact times; retiming must retain integer event boundaries.
Splits preserve complete pairs but reject cuts through a region. Joins reject
conflicting region identities. Insertions extend an active region and preserve a
region ending exactly at the insertion boundary. Removal rejects intersections
until the author explicitly edits/removes the affected region; it cannot leave a
dangling pair. These policies also apply through undo/redo.

## Immutable library reuse and working files

`ClipLibrary` is a detached view/index with a default 4096-entry and 64 MiB
conservative JSON-object estimate. Policy caps are checked before cloning a new
entry. Duplicate exact versions are idempotent; changed content under the same
ID/version is rejected. There is no eviction or mutable `latest` pointer.

`duplicate`, `versions`, `compare`, `restore`, name/tag `search`, and bounded
`import_clip` operate on exact semantic hashes. Selecting an earlier version
returns its unchanged document; it does not overwrite a newer version.

Persistent publication is delegated to existing `AssetLibrary`:

- `register_asset(library, manifest, sources, path)` first validates the explicit
  JSON clip and its exact expected bytes, checks index admission, then delegates
  the immutable publication transaction to `AssetLibrary.register`.
- `load_asset(resolver, exact_reference, path)` verifies the exact manifest
  identity/version/hash, obtains its actual JSON bytes through the canonical
  resolver, validates the clip and adds a detached view. No second blob store or
  registry is created. Asset IDs and clip IDs remain distinct authorities.

`save_clip(path, clip)` is an explicit mutable working-document save through the
shared atomic JSON writer. It is not immutable asset publication. `load_clip`
uses canonical `open_payload` with bounded reads, regular-file/symlink checks and
optional exact SHA-256. Its default ceiling is 16 MiB; lower caller limits apply
before allocation. A POSIX FIFO replacement regression exercises actual
nonblocking descriptor admission in a bounded owned child.

## Executable evidence and remaining integrations

Run `python -m unittest test_animation3d_clip_edits test_animation3d_clips
 test_animation3d_evaluator -v` with `PYTHONPATH=scripts:tests` in the optional 3D
environment, using the coordination test budget launcher. Tests check real
polynomial samples, quaternion angular error, saved edits deforming native skinned
vertices, exact asset registration/restart/retrieval, source preservation,
capacity/failure atomicity and hostile file types.

Nested clip/layer/state orchestration, retarget execution, procedural baking,
physical contacts, browser timeline controls and final rendering retain their
existing subsystem owners. This module preserves the needed document/reference
boundaries; it does not claim those separate integrations or close parent #13.
