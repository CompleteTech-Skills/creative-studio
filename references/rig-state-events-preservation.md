# Catalog event, recovery and reduced-motion preservation

This additive layer preserves PR #347 for #56 and parent #11, especially
sections 17.1, 17.4 and 19.2. It reuses the current selected-state scheduler and
native geometry evaluator. It does not complete either issue or grant visual
or production approval.

## Source boundary

Preparation parent: `6af31b3a48c7b4e5a24c321bb6feff690996aab6` (PR #504),
tree `8372602c3a039698826b9371eaabd47bbc455d21`.
That runtime layer already contains preserved #309/#334 behavior, native
world-target/root-placement fixes and seven independent contact regressions.
The source being preserved here is PR #347 head
`f9cb0f6657184f94d95705e62720724b64248f9d`.
The original source remains authoritative historical evidence; no branch is
rewritten and no old runtime file replaces the current implementation.

The four original event/recovery/reduced-motion test methods are retained
verbatim in `tests/test_rig_state_events.py`, with their original fixture
helpers. Five additional methods cover canonical events, executable-only event
selection, catalog-copy isolation, ambiguous event rejection, transaction
rollback after automatic completion, recovery budget failure, returned-Pose
isolation, explicit still recovery, time/policy guards and malformed optional
alternatives. These test bodies are source additions, not execution evidence.

## Behavior and integration

`scripts/rig_state_events.py` contains a small mixin and catalog compilation
helper. The existing runtime continues to own scheduling, transactions,
priority, transitions, finite monotonic time, root motion and native evaluation.
No second scheduler, event source, clock or geometry evaluator is introduced.

`compile_state_events(library, recipes)` copies each selected executable
recipe's canonical catalog event list and returns an event-to-state index.
Each canonical state and its catalog aliases also receive `state:<identifier>`
events. Unavailable states receive no executable event. Conflicting event
targets raise `RIG_STATE_EVENT`; the catalog itself is not modified.

`dispatch(event, now)` delegates to the existing request policy. Unknown events
and legal-transition/priority failures preserve playback. `recover(now)` uses
the active recipe's catalog recovery target as an explicit override, inside
the existing rollback transaction. A missing selected recovery target or an
exhausted transition budget raises a structured error and restores runtime state.
Cancellation retains the selected executable `static-neutral` target.

Opt-in `reduced_motion=True` requires `reduced_motion_poses` to cover every
selected executable state. Each value must be a valid current `Pose`, with
ID equal to the first recipe clip's `reduced_motion_pose`, current skeleton
version and successfully evaluable native deformation. Inputs and returned
poses are copied. Alternatives supplied while the mode is disabled are still
validated, as in #347. No catalog 2D frame index is treated as 3D geometry.

Held alternatives bypass clip sampling, crossfades and automatic completion.
Explicit requests/events, recovery and cancellation select another authored
still under their existing policies. Evaluation reports
`status='held-reduced-motion'`, with no pending completion. Runtime hash guards
remain active; reconstruct after manifest/clip changes. Direct mutation of
in-memory geometry/skin arrays remains outside the existing runtime signature.

The integrator must apply these small edits to shared files:

1. In `scripts/rig_states.py`, import `compile_state_events` from
   `rig_state_events`; after the selected-state compilation loop call
   `events=compile_state_events(library,compiled)`; add `events=events` to the
   returned mapping. Keep existing discovery/capability/resource fields.
2. In `scripts/rig_state_runtime.py`, import `RigStateEvents`, inherit it,
   and add keyword arguments `reduced_motion=False, reduced_motion_poses=None`.
   After `self._require_state(initial)`, call
   `self._configure_state_events(reduced_motion, reduced_motion_poses)`.
3. At the beginning of runtime `_pose(now)`, return
   `self._held_state_pose()` when `self.reduced_motion` is true. Keep the entire
   normal animation/root-placement path unchanged. In `_evaluate(now)`, guard
   the completion loop with `not self.reduced_motion`; return
   `'held-reduced-motion' if self.reduced_motion else status` for status.
4. Add `test_rig_state_events.py` to the `animation-ik` group in
   `scripts/test_rig_gate.py`, preserving all existing members.
5. Update the state-runtime paragraph in `references/3d-rigging.md` to document
   dispatch/recovery and authored still arguments; link this preservation note.
   Its old sentence saying those features are unimplemented must be updated.
   Keep detailed dependency discovery explicitly separate.

No `rig_animation.py` edit is needed. Preserve the authored-root samples,
native placement before controls, world-space targets, exact internal clip
boundary ownership and all seven current contact tests. PR #354's detailed
discovery and PR #491's default-morph discovery tweaks are outside this layer.

## Required verification and remaining blockers

The preparation environment has no executor. No Python test, deformation run,
release gate, browser or Blender test was executed for this change. API source
readback and equality comparisons establish source retention only. Historical
#347 results apply to its own head and cannot establish current-head behavior.
The dedicated tests need the shared integration edits above before execution.

Run against the final integrated head with the existing isolated optional rig
dependencies. The focused command uses actual native skinning, not mocks:

```bash
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=scripts:tests python -m unittest test_rig_state_events test_rig_state_runtime test_rig_state_runtime_contacts test_rig_animation -v
python scripts/test_rig_gate.py animation-ik
python scripts/test_rig_gate.py schema-math
python scripts/test_rig_gate.py interoperability
```

Use the separate existing delivery environment for the legacy 2D gate:

```bash
HATCH_DECOMPOSITION_BROWSER_TEST=1 python scripts/test_rig_gate.py compatibility
```

Independent review and fresh hosted checks on the integrated exact head remain
required. Optional-tool skips do not prove interoperability; synthetic numerical
fixtures do not establish artistic acceptance. Full six-fixture acceptance,
persistent jobs, automatic event subscriptions and detailed dependency discovery
are not supplied by this layer. Preserve all source, review and 2D contracts.
