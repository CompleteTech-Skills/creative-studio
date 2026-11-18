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

Crossfades capture both the evaluated source pose and its pre-control input,
including an interrupted fade. Normal transitions blend pre-control inputs and
then execute the shared native control pipeline once. Reapplying partial-strength
aim or copy controls to already controlled poses would increase their effective
influence and cause a jump at the fade boundary. By default,
`control_transition="continuous"` admits a nonzero transition only when applying
the incoming controls at entry preserves the captured final FK within 1e-7.
An incompatible goal rejects atomically with `RIG_STATE_CONTACT_TRANSITION`;
the caller can supply compatible goals or explicitly choose a zero-duration
transition. Callers that intentionally require immediate incoming control
projection can select `control_transition="project"`. That explicit mode keeps
the incoming clip's placed contact goals authoritative while other channels
fade; constrained joints can then jump when goals differ. The runtime never
interpolates replacement contact targets. Interruptions retain the actual final
returned pose as well as the bounded pre-control snapshot.

When an outgoing contact correction is released and the incoming clip has no
active authored contact, the fade interpolates from the actual frozen source pose
to the once-controlled incoming target. During this bounded release, fractional
world aim/copy controls are not reapplied to the intermediate interpolation.
Hard hinge, ball, swing/twist, locked-axis and translation limits still project
the blended pose through the native constraint owner. A hard limit can override
interpolation continuity when the shortest quaternion path crosses a forbidden
range. The result explicitly reports `control_stage="contact-release-blend"` and
records any hard-limit corrections in `constraints_applied`; normal evaluation
reports `control_stage="native"`.
Release provenance survives request/cancel interruptions. An incoming active
contact always restores the normal authoritative contact-solving stage.

`evaluate`, `request` and `cancel` also return `contacts` and
`constraints_applied`. Positional and orientation residuals are measured against
the final returned FK, including a contact disturbed by a later contact solve.
An unresolved contact reports `status="controls-unsatisfied"`; the separate
`pending_completion` field still describes the state transition boundary. Native
per-solve reports remain under `solve_contacts`; `contacts` and `final_contacts`
describe the final geometry. Direct `evaluate_animation` preserves its existing
per-solve `contacts` result and adds separate `final_contacts`. Reachability and
convergence diagnostics are preserved rather than discarded.
The contact regression suite exercises default and custom nonzero fades,
interruption, cancellation, fixed-world controls, impossible targets and
conflicting chains on synthetic geometry. These checks do not grant visual or
production approval.

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
controls. All trajectory and origin samples use the canonical native
`evaluate_track` for the root translation, with its authored rest fallback:
the first clip origin, each clip's start and end, the selected local sample,
complete-cycle sums and the target origin used at state entry. Unrelated future
channels are not evaluated solely to extract this translation. Runtime blend
inputs disable controls during full pose sampling;
`apply_animation_controls` then applies the same native stage used by direct
`evaluate_animation`. The explicit contact-release interpolation above is the
only final-pose exception to that projection order.

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

The original preservation layer was prepared without an executor. The later
crossfade correction ran 50 focused runtime/contact/animation tests and all 98
tests in the animation-IK gate on Linux, without skips. Schema and template
checks also passed. These tests use the actual native controls, FK and skin
deformation on synthetic engineering geometry. Historical preparation statements
above remain revision-bound; they do not replace these executed corrections or
independent review and exact-current-head hosted checks.

The subsequent hard-limit release correction passed the full 51-test focused
runtime/contact/animation suite without skips. Its added regression reproduces a
forbidden hinge angle between legal endpoint rotations on the previous source,
then checks the corrected angles and actual finite skinned vertices through the
whole release. Existing fractional-control and contact-release assertions remain.

The subsequent integration preserves concurrent remote head
`9b0bff39ec23697aca27da2a42f597b005434964` and both of its eight-test control
result and transition-admission modules unchanged. The combined 67-test focused
suite passed without skips. A later additive policy-type test covers malformed
selection values; final-head execution and review remain required. The shared
native control pass retains source blending and hard limits while adopting the
remote default of continuous transition admission and distinct final reports.

The existing integration has already added `test_rig_state_runtime.py` to the
`animation-ik` group in `scripts/test_rig_gate.py`. Its original 16 tests remain
byte-for-byte preserved. The direct evaluator contact test remains in the
existing `test_rig_animation.py` group member. The independent
`test_rig_state_runtime_contacts.py` regression module is also included in that
gate without removing other entries. Its 22 regression methods use the real
native solver and deformation functions, including nonzero contact fades,
fractional controls, contact release and interruptions. Execution results above
come from the recorded test runs, not merely the presence of those methods.
Run the focused tests and release gates against the final integrated revision:

```bash
PYTHONPATH=scripts:tests python -m unittest test_rig_state_runtime test_rig_state_runtime_contacts test_rig_state_runtime_control_results test_rig_state_runtime_transition_admission test_rig_animation -v
python scripts/test_rig_gate.py animation-ik
python scripts/test_rig_gate.py schema-math
python scripts/test_rig_gate.py interoperability
```

Report actual failures, optional-tool skips and environmental blockers separately.
These numerical fixtures cannot provide artistic review, external interoperability
acceptance when tools are skipped, or full six-fixture/parent acceptance.

## Sampled geometry and finite native diagnostics

Issue #530 extends the admission boundary to every evaluated sample. Native
animation controls validate composed FK and the canonical morph domain even
when a clip has no contacts or constraints. Runtime sampling also evaluates the
actual native deformation after controls and contact-release projection, before
returning a pose or committing transition state. Finite transforms alone do not
establish finite skinned vertices. Rejected samples restore all nine runtime
transaction fields, including the captured source/input poses and contact
placement. Both the captured pre-control input and the returned controlled pose
undergo actual native deformation admission, adding two deformation evaluations
per runtime sample. A constraint cannot conceal an unusable captured input.
This does not perform rendering or external application validation.

Within one bone, positional copy precedes hard translation limits. World-space
rotation controls then use that final position, and hard angular limits follow.
Constraint identifiers no longer determine whether a positional follower can
escape a hard translation limit. The contact-release path and ordinary native
evaluation therefore retain the same hard bounds at the completion boundary.

IK segment lengths, target distances and residual reports use overflow-safe
Euclidean distances. Finite coordinates can have an unrepresentable combined
distance; such samples raise a structured error before runtime state commits.
Representable large distances retain finite, strict-JSON diagnostics without
claiming that an unreachable contact converged. The legacy per-solve reports and
the final-geometry reports both retain this finite boundary.

On Linux/Python 3.12.14, source
`d8c0484c972c059c58086395b2648b34cb45e3c1`, which includes protected main
`165d57101e667c58a5af217d306422ecd32d87e6`, passed the full `animation-ik`
gate: 132 tests in 64.687 seconds, exit 0, zero skips. The `skinning` gate passed
63 tests in 7.162 seconds, exit 0, zero skips. Ten independently authored sampled
pose/deformation cases and six independently authored numerical-boundary cases
are additive gate members. The latter six methods previously produced 15
assertion/subtest failures, zero errors and zero skips on source `38fafd68`;
the corrected focused run passed all six in 2.801 seconds. These are executed
synthetic native FK, IK and skin-deformation results. The final published tree
still requires independent review and exact-head protected hosted checks.

### Representable completion boundaries

Runtime completion and internal clip selection compare against absolute clock
boundaries. A positive interval cannot finish at its entry timestamp when that
timestamp's spacing exceeds the duration. Recipe durations and interval prefixes
sum the exact represented durations before rounding once; a known completed
non-looping recipe always samples its final clip's exact endpoint, including a
final clip shorter than the preceding prefix's floating-point spacing. Ordinary
and looping intervals remain half-open, so their terminal STEP key cannot flash
before the next clip or cycle. An unavailable completion holds the composed
boundary pose; a non-looping state without a completion edge can finish its
ordinary transition fade after its clip ends.

The native direction normalizer scales finite vectors before computing their
length. Large representable world-space aim directions retain their direction.
Final contact reports recompute reachability and convergence from final geometry
after all native solves; per-solve reports remain separate historical results.

Linux/Python 3.12.14 source `671349bd` passed 142 animation/IK tests and 63
skinning tests, with no skips. Its schema/math gate ran 318 tests: 317 passed and
the explicitly gated Blender optical case skipped; discovery of 1,377 isolated
2D tests did not execute those tests. Subsequent source `66ebef1b` passed all 144
animation/IK tests in 257.029 seconds, exit 0, zero skips, but independent review
then reproduced inaccurate multi-clip prefixes and a missed tiny final endpoint.
Two new independently authored regressions failed on that source. With the
single-rounding prefix and final-endpoint correction, all 14 completion and
numerical-boundary methods passed in 13.178 seconds, exit 0, zero skips. The
earlier full-gate pass does not establish the corrected tree's full-gate result;
that tree still needs full execution, independent review and protected CI.

Source `62f5ba94` / published tree `94263057` subsequently passed the full
146-test animation/IK gate in 88.278 seconds, exit 0, zero skips. Independent
review then reproduced three additional boundaries: blend completion altering
playback time at a large clock origin, an unusable pre-control skin snapshot
hidden by a valid locked output, and valid earlier playback rejected because
root-trajectory extraction evaluated unrelated invalid future channels. Three
additive independent methods failed on that source. Playback elapsed and blend
progress are now separate, captured input receives actual skin admission, and
root trajectory extraction samples only the root translation. The corrected
completion and sample-admission modules passed all 27 methods in 43.733 seconds,
exit 0, zero skips. These focused executions do not substitute for the corrected
tree's full gate, independent re-review or protected hosted checks.

The subsequent source `ae221055` / published tree `0f29d019` passed all 149
animation/IK tests in 103.762 seconds, exit 0, zero skips, before security review
identified an interior phase mismatch and inconsistent large integer clocks.
Interior loop samples now retain the native elapsed-time remainder. Rounded
absolute boundaries only determine interval ownership and exact starts; they
do not replace that interior clock. Five independent regression/control methods
cover an ordinary STEP blink, cubic scale admission at a large origin, and
integer constructor/playback/rollback boundaries. Together with prior completion
and sample-admission cases, all 32 methods passed in 47.360 seconds, zero skips.

The runtime clock stores binary64 seconds. Integer timestamps are accepted only
when they round-trip exactly through that representation; an inexact integer
raises `RIG_STATE_TIME` on first admission rather than silently changing its
monotonic order. Exactly representable large integers and repeated timestamps
remain supported. This explicit representation boundary applies to construction,
evaluation, requests and cancellation. Corrected full-gate execution and fresh
exact-head independent review remain required.

Published `341403c1` then passed 155 animation/IK and 63 skinning tests with
zero skips, before a fresh reviewer reproduced two collapsed-cycle failures.
At a large clock origin, distinct absolute cycle boundaries can become the same
binary64 value. That fallback now compares the original loop remainder against
the same once-rounded cumulative prefixes and subtracts the selected prefix
only once. It does not repeatedly subtract clip durations. Two independently
authored regressions preserve the valid fourth-clip morph boundary and an
incoming fourth clip with finite FK but invalid native skinning. Both failed
on the preceding head; the latter now rejects without changing any of the nine
runtime fields, source data or replay geometry. Together with the previous
clock and completion modules, all 22 methods passed in 28.030 seconds, exit 0,
zero skips. Full corrected gates and fresh exact-head review remain required.

## Exact cycle boundaries and final contact validation

Recipe duration, complete-cycle offsets and within-cycle prefixes retain the exact
values represented by authored floating-point durations until the final absolute
clock boundary is rounded. Equivalent unrolled and looping recipes therefore
select the same half-open interval. A unique rounded clip start samples local
zero. When multiple successive starts collapse onto one timestamp, the relative
clock uses the shortest repeating clip sequence. It rounds that period and its
cumulative prefixes once, selects the interval from the remainder, and subtracts
the selected prefix once. Embedded root displacement sums the exact
represented endpoint differences across complete cycles and preceding clips,
then rounds once for native placement. This preserves residual travel after
opposing motions and supports cycle counts beyond floating-point range.

The canonical rig validator consumes `final_contacts` for final orientation and
convergence decisions, while `contacts` continues to retain each original solve
receipt. A later solver may repair an earlier intermediate miss, and a final
unreachable goal remains a failed validation even when its angle is correct.

Eleven independently authored native clock/validation methods reproduced five
failing methods on predecessor7397872b and all pass after this correction, zero
skips. They include exact .9s equivalence, adjacent clocks, scaled future cycles,
cycle prefixes, actual skinned vertices, repaired overlapping contacts and real
unreachable/unsatisfied controls. Full integrated gates and exact-head independent
review remain separate requirements; no visual or production acceptance follows.
