# Bounded native rig state runtime preservation

This layer advances #56 and the contact portion of #54 under parent #11. It
preserves the existing native evaluator and the selected catalog mapping owner.
It does not complete those issues or establish visual or production acceptance.

## Source boundary

The exact parent is `75e57cba1b8bca4b5ef87f3888d2568ed07d3d04`
(tree `ae8b4fad967121fa00d1b4f2404da3b85096c4e0`), the current core
preservation layer. Its retained controls, retargeting, specialization, estimates,
scaled-wing correction and role-ID correction remain prerequisites.

| Source | Exact inspected head | Disposition |
| --- | --- | --- |
| #309 | `70c672be3e9c72f990d0728b6bb87e29b4183446` | Runtime and its complete 16-test module retained byte for byte |
| #334 | `c5c8384ff86490e511dd1f49d73c3f43d4a9bc78` | Contact evaluator correction and complete numerical regression retained |
| #459 | `f2476e66f18fd56237ed68dc705dce0c25c3e505` | Inspected read-only; its contact correction and test body are identical to #334 |
| #347 | `f9cb0f6657184f94d95705e62720724b64248f9d` | Event dispatch, explicit recovery and reduced-motion execution deferred |
| #354 | `fb08259c1e31c5508dfd88f76d8e218b31f840e4` | Detailed dependency discovery and provenance reporting deferred |
| #491 | `baf9332d9ea3030a16c1b8731763a8c31b4a99aa` | Scaled-wing part already in parent; default-morph discovery correction and its three tests must accompany deferred #354 |

No source branch is rewritten or removed. Preserving these source hunks does not
mean they have reached main, and does not supersede historical revision-bound
evidence. The separate #459 lane retains its own branch and integration decision.

## Python interface and behavior

Add `scripts` to the Python path. Construct
`rig_state_runtime.RigStateRuntime(rig, initial, recipes=recipes, selected=states)`
with existing validated clips and selected executable catalog state IDs. Calls
to `evaluate(now)`, `request(state, now)` and `cancel(now)` take caller-supplied
monotonic finite nonnegative seconds. Returned `pose` values can be passed to
`rig_skinning.evaluate_deformation` for actual native mesh evaluation.

Requests enforce legal edges and the incoming state's priority/interruption
policy, coalesce repeats, and advance available completion edges at clip
boundaries. Missing completion targets report `completion-unavailable` and hold
the last pose. Cancellation requires a selected executable `static-neutral`.
Rejected requests and exhausted completion chains restore prior runtime state.
The total transition budget defaults to 32 and is restricted to 1..1024.

Crossfades capture the evaluated source pose, including an interrupted fade.
Sparse target morph channels return to authored defaults. Embedded root motion
stitches different authored clip origins and accumulates across repeated recipes;
in-place clips retain their cycle reset. Finite-time guards retain the original
large integer, large timestamp and overflowing cycle-count regressions.
The manifest and clips are hash checked; reconstruct the runtime after changes.
Direct in-memory geometry or skin-array mutation is outside that signature.

For direct native clip evaluation, in-place contact targets remain at their
authored world locations on repeated cycles. Embedded contact targets advance by
the same complete-cycle root displacement as the pose. Non-looping evaluation
keeps authored contact locations. The retained contact regression checks exact
FK targets, convergence, full skinned geometry, an embedded positive control and
non-looping clamping.

This is an in-memory Python API. Durable jobs, event dispatch, explicit catalog
recovery, authored reduced-motion execution and detailed dependency inventories
are separate work. No 2D frame index is interpreted as 3D geometry. Existing
catalog definitions, 2D contracts and persisted schemas remain authoritative.

## Verification and integration requirements

The preparation environment has no executor. No Python test, native deformation
run or local release gate was executed for this layer. Source equality checks
establish retention only; historical passing runs apply to their original
revisions. Independent review and exact-current-head hosted checks remain needed.

The root integration must add `test_rig_state_runtime.py` to the existing
`animation-ik` group in `scripts/test_rig_gate.py`, preserving all other entries.
The contact test is already in the existing `test_rig_animation.py` group member.
Run the focused tests and release gates against the final integrated revision:

```bash
PYTHONPATH=scripts:tests python -m unittest test_rig_state_runtime test_rig_animation -v
python scripts/test_rig_gate.py animation-ik
python scripts/test_rig_gate.py schema-math
python scripts/test_rig_gate.py interoperability
```

Report actual failures, optional-tool skips and environmental blockers separately.
These numerical fixtures cannot provide artistic review, external interoperability
acceptance when tools are skipped, or full six-fixture/parent acceptance.
