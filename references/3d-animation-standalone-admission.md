# Standalone exact Clip admission

`animation3d_assets.admit_clip(root, exact_reference, *, budgets=None, reader=None)`
admits one persisted Animation3D clip through the same snapshot visitor and
native target/binding validator as project admission. It creates no source file
or published project. The temporary project-shaped owner envelope exists only in
memory to reuse canonical ResourceBudget3D and binding validation.

```python
from animation3d_assets import admit_clip
receipt = admit_clip(project_root, {
    "id": clip_id,
    "version": clip_version,
    "kind": "animation_clip_3d",  # "clip" is also accepted
    "path": "clips/body.json",
    "sha256": exact_serialized_file_sha256,
})
```

The SHA256 is the hash of the **original serialized bytes**. A canonical
`object_hash` is a different identity and cannot replace it. The clip ID/version,
all transitive raw hashes, native rig binding, skeleton signature and actual
bone/morph targets are validated. Disabled targets remain subject to admission.
Unregistered scene/object targets need their owner context and fail explicitly.
This does not evaluate a whole scene, grant visual approval or publish an asset.

The result contains:

- `clip`: detached validated root document.
- `documents`: detached typed animation documents in its exact closure.
- `inventory`: each logical path's raw SHA256 and encoded byte count.
- `bytes`: total encoded snapshot bytes.
- `memory_admission`: the effective limit and conservative shared
  snapshot/retained-native estimate; this is not an OS RSS guarantee.
- `native_context`: metadata `rig_bindings` (raw manifest identity/hash,
  skeleton signature, actual vertex/bone counts) and declared `actor_ids`.
- `source_ready` and a narrow admission `scope`.

`native_context` is binding metadata, not a mutable rig, a scene-actor ownership
mapping or a second evaluator. The Scene publication owner verifies these exact
bindings against its scene instances and unions the inventory into its own
aggregate file/byte budget. Source files remain unchanged.

## Trusted byte-map and registered-directory readers

A host can supply its existing source authority without another filesystem walk:

```python
def read_source(path, max_bytes):
    data = verified_files[path]
    if len(data) > max_bytes:
        raise ValueError("source exceeds requested bound")
    return data

receipt = admit_clip(None, reference, reader=read_source,
                     budgets={"max_memory_bytes": 64 * 1024 * 1024})
```

The callback is a fixed **trusted host function**, never selected by document
contents. It must return the complete immutable bytes for one portable path or
raise; truncating source files is not a valid reader. This owner verifies the
returned type, requested byte ceiling and exact reference hash, captures private
copies and invokes the same canonical native loaders. In reader mode no source
root is resolved or read. A registered FD reader retains its own authority while
serving the callback. Ordinary filesystem mode keeps canonical descriptor reads;
when the shared helper provides `normalize_root`, explicit scoped FD authority
is retained through it.

Portable full paths and parent components cannot collide by case. Names are
bounded to 1,024 characters and 64 components; namespace bookkeeping is charged
before opening a new source. File count, encoded bytes, JSON object estimates,
ZIP directory/header/expanded bytes and all retained native rig estimates share
bounded admission. Caller `budgets` use canonical ResourceBudget3D names. Positive
integer ceilings (including schema-valid whole-number floats) take restrictive
runtime copies; larger caller ceilings cannot raise internal policy. Unknown
keys, booleans, fractions and unbounded values fail typed.

The default encoded/memory ceiling is 256 MiB, with 4,096 files, 4,096 tracks,
250,000 keys, 1,000,000 vertices, 256 bones per rig and 128 morph targets. The
remaining fields use bounded canonical owner policy. This API starts no workers
or renders. Caller documents and supplied references are not normalized in place.

## Source clocks

Reusable clips retain their own rational source frame rates. The existing exact
`source_time` adapter maps output frame/rate into each clip clock; changing the
project clock does not reinterpret stored clip key times. Project sequence and
render documents still require the project frame rate. This applies equally to
standalone provenance closures and complete project admission.

## Verification

`tests/test_animation3d_standalone_admission.py` exercises actual native rigs and
private snapshots through filesystem and rootless byte-map sources, raw hash
versus object hash, source mutation/missing data, fabricated targets/signatures,
work/memory/callback bounds, mutable callbacks, path aliases, namespace resource
bounds, restrictive whole-float policies and independent rational clip clocks.
The source inventory is checked unchanged. These are synthetic engineering
fixtures and make no production-artwork or visual-approval claim.
