# Native pose editing and reuse

`animation3d_poses` edits the existing `hatch-3d-pose` representation. It does not
create a second pose format, skeleton evaluator, skinning implementation or
storage registry. Named immutable reuse remains `animation3d_evaluator.PoseLibrary`'s
responsibility; its rig context must be admitted before editing. These helpers
check the native skeleton version and actual channels, not character identity or
retarget compatibility.

Every result passes the native Pose schema, version binding, FK, authored native
constraint checks and actual morph/skinning evaluation. An operation that would
violate an authored constraint rejects rather than silently clamping the requested
pose. The helpers do not solve IK, contacts, physics or scene constraints for the
caller. Those execution stages retain their existing owners. Returned documents
are detached; invalid operations leave inputs and saved snapshots unchanged.

```python
from animation3d_poses import edit_pose, blend_pose, reset_pose

reach = edit_pose(rig, initial,
                  bones={'root': {'translation': [0.5, 0, 0]}},
                  morphs={'face': {'blink': 0.5}})
halfway = blend_pose(rig, reach, initial, 0.5)
reset = reset_pose(rig, reach, locked_bones=['root'])
```

Use actual IDs from the admitted rig. Sparse target poses blend toward native
rest/default channels, including neutral morph weights; missing target overrides
never accidentally preserve the source pose. Pose IDs can be changed through
`duplicate_pose`. `compare_poses` reports changes in actual world matrices and
skinned vertices. `inspect_rest_pose` returns the canonical rest Pose and the
existing rig inspector's hierarchy, heads, tails and world transforms.

## Explicit symmetry and locks

`mirror_pose(..., approved=True)` delegates reflection to `rig_skeleton.mirror_pose`.
It uses the rig's declared reciprocal bone mapping. A caller can supply a checked
reciprocal mapping through `mapping`; it changes only a detached skeleton used by
that operation. No left/right relationship is inferred from a name. Center bones
retain their native unmapped reflection behavior. Morph symmetry requires a
separate explicit `morph_mapping={mesh: {left: right, right: left}}`; otherwise
existing morph weights are retained.

`edit_pose(..., symmetric=True, approved=True)` accepts edits on one side of each
explicit pair and derives the reflected partner through the native mirror owner.
Ambiguous simultaneous bilateral edits, absent mappings and locked partners
reject. Bone locks preserve local channels. A locked child can still move in world
space when its parent moves; world-space contact locking requires the IK owner.
Blending, mirroring and reset preserve the original local values on locked bones.
An explicit edit to a locked bone fails.

## Transitions and independent facial controls

`transition_samples(rig, source, target, samples=N)` returns bounded uniformly
spaced native Poses. Frame-based consumers associate them with their exact rational
Animation3D clock. It does not invent a new clip format or accumulate playback time.
The explicit compatibility adapter `pose_transition(..., duration_seconds=...,
fps=...)` delegates to `rig_animation.clip_from_poses` and returns a native
seconds-based RigAnimationClip, retaining its integer 1–60 fps contract. This is
separate from Animation3D's rational frame-rate domain.

`apply_expressions` delegates to the existing `rig_facial` owner. Intensities are
finite values in [0,1]. Missing expressions and incompatible combinations reject;
no facial target is invented. Native reset semantics apply to declared facial
channels, while unrelated body channels remain intact. Locks also apply to facial
bones. Named expression presets remain the rig's own records.

## Editable file snapshots

`save_pose` validates first and reuses the native atomic pose writer. Its destination
is an editable snapshot in a caller-owned working directory, not an immutable
publication API. `load_pose` uses the shared bounded, nonblocking descriptor reader
and strict JSON decoder. Before decoding, the canonical native JSON object factor
is charged against memory remaining after the retained rig/geometry estimate;
symlink payloads and replaced/changed files reject.
Immutable source assets and named library records must continue through their
existing owners. A snapshot roundtrip establishes document and geometry integrity,
not visual approval.

## Executed evidence

`tests/test_animation3d_poses.py` uses actual four-vertex skinned geometry and an
explicit symmetric nonhumanoid skeleton. It checks neutral restoration, independent
body/facial deformation, mirrored bone and morph edits, local locks, native
transition geometry, named snapshot roundtrips, constraint rejection, unrepresentable
numeric inputs and symlink failures. No renderer, external application, production
artwork or visual approval is claimed by these numerical fixture tests.
