# Transactional clip command adapter

`animation3d_edit.apply_clip_commands(clip, request, *, rig_root=None, actor_id=None)`
returns a detached validated replacement. The editor repository publishes that
replacement and updates exact native references in one project transaction.

```python
from animation3d_edit import apply_clip_commands
from rig_errors import object_hash
request = {
    "schema_version": 1,
    "kind": "animation3d-edit-request",
    "expected_document_hash": object_hash(clip),
    "commands": [{"op": "edit-clip", "name": "New working name"}],
}
result = apply_clip_commands(clip, request, rig_root=rig_root, actor_id="actor")
```

The result contains `clip`, `before_document_hash`, `after_document_hash`,
`changed`, `applied_count`, `copied_keys` and `native_validated`. Document hashes
use canonical `object_hash`; exact serialized-file SHA256 is calculated by the
publication owner. The caller verifies its original raw reference and project
revision, chooses a new immutable published version when needed, and commits
new bytes and references atomically. The command adapter writes no files.

The closed request admits at most 128 commands. Clipboard keys share an aggregate
4,096-key allowance, and the complete clip plus all clipboard output shares the
node/text policy. Admission precedes each clipboard clone. Supported operations are:

- `insert-key`: `track_id`, complete canonical `key`.
- `remove-key`: `track_id`, exact `time`.
- `replace-key`: `track_id`, old exact `time`, complete replacement `key`.
- `replace-track`: complete canonical `track` retaining an existing track ID.
- `copy-keys`: `track_id`, inclusive exact `start` and `end`; results are returned
  in `copied_keys` with the command index.
- `paste-keys`: `track_id`, up to 4,096 canonical `keys`, optional rational `offset`.
- `move-keys`: `track_id`, inclusive `start`/`end`, optional positive rational
  `scale` and rational `offset`.
- `edit-clip`: optional `name`, integer `duration`, rational `fps`, `add_track`
  and/or `remove_track`; existing identity/version, tags/categories, loop/completion
  policy, speed and canonical event additions/removals are also supported.
- `set-region`: `identifier`, `name`, integer `start_frame`/`end_frame`.
- `remove-region`: existing `identifier`.
- `retime-clip`: optional positive rational `scale` and rational `offset`.
- `reverse-clip`: no additional fields.
- `splice-frames`: integer `start` and nonzero integer `count`.

Rational controls use `{numerator, denominator}` with positive denominators.
Timestamps reuse the canonical frame/subframe schema. Unknown fields, boolean
numeric controls, stale document hashes, invalid joins and collisions fail with
a typed error. A later failing command discards the entire detached batch.
The source/request remain unchanged. A key replacement validates the complete
result rather than exposing an invalid intermediate curve with one key removed.

Supplying both native context arguments checks all targets, including disabled
channels, and evaluates actual native geometry at clip boundary frames. Further
frame previews use `RigClipEvaluator.evaluate` or `evaluate_sources`; their
returned pose, world/skin matrices and geometry are detached. The native context
is a rig with registered skeletal/morph channels. Scene-property owners retain
their existing adapters. Source closure validity is checked again at later
execution/publication boundaries.

Programmatic documents have explicit node/depth/text policies. CLI input uses
bounded nonblocking descriptor reads, rejects symlink inputs and limits each
JSON file to 2 MiB. The CLI prints a JSON result for the publication caller:

```sh
python scripts/animation3d_edit.py --clip working-clip.json --request commands.json \
  --rig-root saved-rig --actor actor
python scripts/animation3d_edit.py --schema
```

The named native `PoseLibrary` delegates editing, symmetry, validation and I/O to
`animation3d_poses`. Its `max_entries` and `max_pose_bytes` bound cached pose data
before cloning new entries. Duplicate identical content is idempotent; a different
pose cannot replace an existing name. Native rig geometry and whole-process RSS
have their own admission authority. Pose snapshots are mutable working documents;
immutable asset publication belongs to the canonical asset library.
