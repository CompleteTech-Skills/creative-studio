# Bounded native rig state runtime preservation

This layer advances #56 and the contact portion of #54 under parent #11. It
uses the existing native evaluator and preserves the selected catalog mapping owner.
It does not complete those issues or establish visual or production acceptance.

## Source boundary

The original preparation parent was `75e57cba1b8bca4b5ef87f3888d2568ed07d3d04`
(tree `ae8b4fad967121fa00d1b4f2404da3b85096c4e0`), the core preservation
layer. The later integration base is `01edd0913e0119d89d5f9b78298d03dfa1e37855`
(tree `c4fc696ada395ff653e94c1220c4f09f67cca1fe`). These are distinct source
boundaries. Retained controls, retargeting, specialization, estimates, scaled-wing
correction and role-ID correction remain prerequisites.

The initial root-placement correction was prepared against candidate
`d19eefe4a0a0decbb84b43e9712719e256685686`
(tree `8f736ad52071e2a860e1e5913fbbca8f037751f9`). The authored-trajectory
correction below is prepared against its reviewed integration candidate
`02f3f6ce2c4cafe318f90762afee42588a08660f`
(tree `6eb60290ce627e0683bd0b24d465a5c7aff6fec6`).

| Source | Exact inspected head | Disposition |
| --- | --- | --- |
| #309 | `70c672be3e9c72f990d0728b6bb87e29b4183446` | Complete original 16-test module retained byte for byte; runtime root placement corrected below |
| #334 | `c5c8384ff86490e511dd1f49d73c3f43d4a9bc78` | Contact evaluator correction and complete numerical regression retained |
| #459 | `f2476e66f18fd56237ed68dc705dce0c25c3e505` | Inspected read-only; its contact correction and test body are identical to #334 |
| #347 | `f9cb0f6657184f94d95705e62720724b64248f9d` | Event dispatch, explicit recovery and reduced-motion execution deferred |
| #354 | `fb08259c1e31c5508dfd88f76d8e218b31f840e4` | Detailed dependency discovery and provenance reporting deferred |
| #491 | `baf9332d9ea3030a16c1b8731763a8c31b4a99aa` | Scaled-wing part already in parent; default-morph discovery correction and its three tests must accompany deferred #354 |

No source branch is rewritten or removed. The original runtime source is no
longer byte-identical to #309: the correction places accumulated root motion
through the native evaluator. Source retention does not mean these changes have
reached main, and does not supersede historical revision-bound evidence. The
separate #459 lane retains its own branch and integration decision.

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

### Root placement before native controls

Source inspection identified a defect in the originally retained runtime: it
evaluated a clip at local non-looping time with constraints/contact IK, then
translated only the already-solved root for accumulated cycles, preceding clips
and state-entry alignment. A matching contact endpoint did not establish a
matching native pose. The IK chain's authored pole is a fixed world-space point;
moving a solved chain changes its relation to that pole and can change the
correct intermediate joints and skinned vertices. World-space constraints also
need to observe the final placement.

The runtime computes the recipe trajectory from authored root channels and a
finite placement offset, then evaluates the selected clip through `evaluate_animation` with
the private `_root_offset` argument. Native evaluation validates that offset as
a finite three-vector and applies it to the root and active contact targets
before constraints and contact IK. Authored world-space poles, constraint
points and orientation goals remain unchanged. The default evaluator call
without the offset retains its existing behavior. No separate IK implementation
is introduced, and final pose evaluation keeps constraints and contacts enabled.

A second source review identified that the first correction still sampled
trajectory roots after native constraints. A root translation clamp could then
reduce the recipe's accumulated contact displacement even though the native
evaluator derives complete-cycle displacement from authored channels before
controls. All trajectory and origin samples now use native evaluation with
`contacts=False, constraints=False`: the first clip origin, each clip's start
and end, the selected local sample, complete-cycle sums and the target origin
used at state entry. Only trajectory extraction disables controls. The final
placed pose goes through full native constraints and contact IK.

For the same one-second 0-to-0.5-meter root motion and contact at `[0, 1.5, 0]`,
a root X translation clamp of `[0, 0.2]` demonstrates the distinction at 2.25
seconds: native complete-cycle contact displacement is 1.0 meter on X, while
constrained-endpoint accumulation gives only 0.4 meter. Both can clamp the root
itself to 0.2 meter, so checking only root position misses the defect. These
numbers follow from the source's displacement formulas; no numerical execution
is claimed. The independent constrained-root regression compares the resulting native pose
and deformation as well as contact displacement; it is present but unexecuted.

This handles complete embedded cycles, different clip origins and state-entry
alignment through the same native controls. In-place cycles still do not
accumulate their authored root displacement. Existing exact-cycle fallback and
finite-motion guards remain; there is no arbitrary cycle-count cap. Transition
rollback and captured-source crossfade behavior are retained.

Within a multi-clip recipe, internal intervals are half-open: the next clip
owns an exact internal boundary. This avoids solving the outgoing clip's old
contact at the instant the incoming clip begins. The final non-looping clip
still clamps to its endpoint. The independent two-clip oracle covers that
boundary as well as accumulated complete sequence cycles.

The reported synthetic case uses root translation from zero to 0.5 meters on X
over one second, contact target `[0, 1.5, 0]`, fixed pole `[0, 0, 1]` and runtime
time 2.25 seconds. The original source's post-solve placement cannot reproduce
the native bend direction after repeated embedded motion. This diagnosis and
the fix's ordering are source-derived, not an executed numerical result.
The independent regression module compares full FK and actual skinned geometry
for embedded loops, in-place controls, displaced recipe entry, multi-clip
alignment and world-space aim constraints. A negative control distinguishes
endpoint agreement from the complete pose. Its numerical behavior still needs
execution on the integrated revision.

This is an in-memory Python API. Durable jobs, event dispatch, explicit catalog
recovery, authored reduced-motion execution and detailed dependency inventories
are separate work. No 2D frame index is interpreted as 3D geometry. Existing
catalog definitions, 2D contracts and persisted schemas remain authoritative.

## Verification and integration requirements

The preparation environment has no executor. No Python test, native deformation
run or local release gate was executed for this layer. Source equality checks
establish retention only; historical passing runs apply to their original
revisions. Independent review and exact-current-head hosted checks remain needed.

The existing integration has already added `test_rig_state_runtime.py` to the
`animation-ik` group in `scripts/test_rig_gate.py`. Its original 16 tests remain
byte-for-byte preserved. The direct evaluator contact test remains in the
existing `test_rig_animation.py` group member. The independent
`test_rig_state_runtime_contacts.py` regression module is also included in that
gate without removing other entries. All seven new methods use the real native
solver and deformation functions; no execution result is inferred from their
presence.
Run the focused tests and release gates against the final integrated revision:

```bash
PYTHONPATH=scripts:tests python -m unittest test_rig_state_runtime test_rig_state_runtime_contacts test_rig_animation -v
python scripts/test_rig_gate.py animation-ik
python scripts/test_rig_gate.py schema-math
python scripts/test_rig_gate.py interoperability
```

Report actual failures, optional-tool skips and environmental blockers separately.
These numerical fixtures cannot provide artistic review, external interoperability
acceptance when tools are skipped, or full six-fixture/parent acceptance.
