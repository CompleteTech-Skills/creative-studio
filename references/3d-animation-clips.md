# Editable native skeletal and morph clips

Issue #68 builds on the #67 typed foundation. `animation3d_clips` edits detached,
validated clip documents. `animation3d_evaluator` initializes a canonical rest
pose for every call, samples registered channels, and invokes the existing
`rig_skeleton.evaluate_fk` and `rig_skinning.evaluate_deformation` authorities.
Morph deltas precede native linear blend skinning; skin matrices are evaluated
world joints multiplied by their native inverse binds. No humanoid assumptions,
replacement skeleton evaluator or replacement skinning implementation is added.

## Reproduce a saved clip evaluation

Install `requirements-rig.txt` in the optional 3D environment. Save a native rig
with its owner workflow and a real clip whose actor/bone/morph IDs are registered
on that rig. Then run:

```sh
python scripts/animation3d_evaluate.py --root output/rig --actor actor \
  --clip body.json --clip face.json --frame 12 --output output/frame12.json
python -m unittest discover -s tests -p 'test_animation3d*.py'
```

The output contains actual deformed mesh positions, canonical pose, world joint
matrices, skin matrices and an input signature. It contains no rendering or
visual approval. The synthetic tests compare every vertex with independent
literal body translation plus facial morph displacement. They evaluate frames
out of order and repeat them, and give facial/body clips independent clocks.

```python
from animation3d_clips import source_time, load_clip
from animation3d_evaluator import RigClipEvaluator
body, face = load_clip('body.json'), load_clip('face.json')
evaluator = RigClipEvaluator('output/rig', 'actor')
body_time = source_time(body, {'frame': 15}, {'numerator': 30, 'denominator': 1},
                        source_in=0, source_out=24, speed=1, loop=True)
state = evaluator.evaluate_sources([(body, body_time), (face, {'frame': 6})])
```

All clips must use disjoint registered channels; conflicting channels reject
instead of silently choosing a clip. Native dependency integrity is checked
once per frame, and detached entries sample the pinned rig without loading the
entire native closure per channel. A supplied initial canonical pose is explicit
input; no previous frame state is read. Clip rig bindings name the exact native
manifest hash and version. `skeleton_signature`, when authored, is the shared
canonical JSON `object_hash` of the complete native skeleton document.

## Editing and reusable libraries

`edit_key` inserts or removes one exact timestamp and rejects collisions.
`copy_keys`/`paste_keys` preserve detached control data. `edit_clip` changes names,
frame rates/durations and actual tracks; trims reject any stranded keys/events
instead of dropping data. `retime_clip` scales the complete clip and offsets its
keys/events/contacts, adjusting derivative units and handle times. Integer event
and duration boundaries stay integer; unrepresentable subframe edits reject.
`reverse_clip` mirrors key times, tangent directions, handles, ease controls,
TCB bias, events, contacts and declared cycle displacement. A reversed step
needs the preceding value one tick after a mirrored boundary: the document
clock explicitly has one million discrete ticks per frame.

`ClipHistory.apply(operation, ...)` commits only a fully valid detached document.
Failed edits leave history unchanged; undo and redo restore complete documents.
`save_clip` and `load_clip` use the native atomic writer and strict JSON reader.
`ClipLibrary` binds `(id, version)` to an immutable content hash and supports name
and tag searches; conflicting content cannot replace the same version.
`PoseLibrary` stores the native pose representation after actual deformation
validation. Its values can be used with the existing `rig_animation.blend_poses`,
`rig_skeleton.mirror_pose` (with explicit symmetry approval), `save_pose` and
`load_pose` APIs. This adds no second pose format.

`source_time` maps exact output frame/rate into source in/out, playback speed,
reverse and loop clocks. An ordinary source interval is half open; nonlooped
playback holds the terminal source key. `events_between` uses explicit source
intervals: forward `(previous,current]`, reverse `[current,previous)`. For a loop
crossing, submit the two source intervals explicitly. No implicit realtime
player cursor, event callback or mutable state is introduced.

## Interpolation

Stepped, linear, cubic Hermite and shortest-path quaternion SLERP preserve the
native rig mathematics. Hermite derivatives are values/frame. Added interpolation
uses the same typed registry and validates the final native property domain:

* `ease` applies cubic Hermite time warping with start slope `1-ease_out` on the
  left key and end slope `1-ease_in` on the right. Both default to one (smoothstep).
  `(1,0)` eases in, `(0,1)` eases out, `(1,1)` eases in and out.
* `bezier` requires a left outgoing and right incoming handle. Each handle's
  `time_delta_frames` and `value_delta` are offsets from its key. An optional
  explicit positive weight scales both offsets only when `weighted` is true.
  Time control points must be monotonic inside the segment; bisection solves the
  cubic time coordinate before the value curve is sampled.
* `catmull-rom` uses nonuniform adjacent secant slopes averaged at each key.
  Endpoint slopes use the adjacent secant. `tcb` applies Kochanek–Bartels tension,
  continuity and bias in `[-1,1]` to those secants; zero controls match Catmull–Rom.
* `quaternion-squad` computes neighborhood controls with quaternion log/exp and
  shortest-hemisphere alignment, then spherical quadrangle interpolation.

Boolean channels remain stepped. Quaternion channels use only step, SLERP or
SQUAD. Unused controls, mismatched dimensions and nonmonotonic Bezier times
reject. Sampled Hermite/Bezier/TCB overshoot must obey native scale/morph and
registered scalar bounds. Hold extrapolation preserves the endpoint. Cycle extrapolation repeats the exact
key interval, retaining an exact terminal key before wrapping later times. Linear
extrapolation continues the actual endpoint derivative: Hermite tangents, Bezier
time/value handles, ease slopes and TCB/Catmull slopes retain value/frame units.
Quaternion SLERP/SQUAD continue the canonical short-hemisphere logarithmic angular
derivative. Boolean linear extrapolation and Bezier endpoints with infinite time
derivatives reject explicitly. Linear extension is bounded to one million source
segment intervals and every result is checked against the native property domain.

## Acceptance boundary and remaining parent work

The implemented #68 core covers editable keyframes, source ranges, all requested
curve families, named immutable clip/pose reuse, native skeletal/morph geometry,
independent body/facial clocks, deterministic out-of-order evaluation and explicit
event intervals. A real rig's available targets determine expressions; no missing
expression is invented. The evaluator rejects world-space channels and enabled
root extraction, constraints or procedural instructions rather than omitting
execution. Contacts are retained metadata and are explicitly reported unsolved.

Parent #13's complete timeline/curve UI and track groups belong to the editor
integration; nested clips, animation layers, state transitions, procedural motion,
IK/contact execution and root-motion extraction belong to subsequent animation
packages. Scene/camera/light/material targets keep their owner adapters. Clip splitting/joining, range-local frame insertion/removal and named regions reuse
the canonical sampler through `animation3d_clips`; SQUAD cuts require explicit
caller-approved sampled tolerances and work limits. Symmetry-aware pose editing
uses explicit joint/morph mappings through `animation3d_poses`. Bounded sampled
continuity diagnostics report their coverage and limitations through
`animation3d_diagnostics`. These APIs do not claim continuous mathematical bounds
or artistic approval. See the respective clip-editing, poses and diagnostics
references. This document does not close #68 or #13 without reproducible owner
integration evidence and independent review of the final candidate.
