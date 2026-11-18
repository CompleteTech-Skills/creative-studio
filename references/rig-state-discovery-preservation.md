# State discovery preservation

This bounded source preservation advances #56 and #11 §19.3. The canonical
`rig_states.map_states` service remains the compiler. It receives the existing
`RigAsset` and unchanged state catalog; the extracted
`rig_state_discovery` module only builds additive dependency and provenance
reports from those owners. It creates no second rig, state registry, controller,
persistence format, review store or runtime.

## Preserved source

| Source | Exact revision | Preserved scope |
| --- | --- | --- |
| #354 | `fb08259c1e31c5508dfd88f76d8e218b31f840e4` | Selected-state catalog versus executable dependencies, exact native source/clip provenance, eight original discovery regression methods |
| #491 | `baf9332d9ea3030a16c1b8731763a8c31b4a99aa` | Authored default morph union and three added native-deformation regression methods |
| #504 integration base | `5b231a3367b703b76091d001869e0ebf8823316a` | Current native owners, bounded estimates and previously preserved runtime |

The corrected discovery test file is retained byte-for-byte from #491
(blob `8468564ad731dadea6eb37b0406819d5b0ebea1e`). All eight #354 test
methods remain unchanged. The helper is extracted from the corrected #491
`rig_states.py` reporting code; clip and controller validation stays with
the canonical compiler. The numerical wing twist correction from #491 is
outside this discovery slice; it was already preserved in the integration base.

## Report semantics

The additive top-level `discovery` map uses selected canonical state IDs.
`catalog_requirements` preserves anatomy, archetypes and props directly from
the catalog. `compatibility` reports missing mandatory capabilities,
unsatisfied alternatives, present exclusions, missing approved props and
archetype applicability.

The `recipe` report retains requested clip order and separately deduplicates
available and missing clip IDs. A fully validated executable recipe reports:

- Actual bone channels and mesh-qualified animated morph channels.
- Required morph targets formed from animated targets and authored defaults,
  including zero defaults, deduplicated by exact mesh and morph identities.
- Contact chains referenced by selected clips.
- Every native evaluated constraint, including named targets only for native
  copy-position, copy-rotation and aim constraints.
- Every declared skin joint, including zero-weight entries read by native
  deformation, plus required parent dependencies.

`morph_channels` describes actual animation tracks. A bone-only recipe with
an authored default may therefore have `morph_channels: []` and a nonempty
`required_morph_targets` inventory. Defaults with absent target data reject
with `RIG_MORPH_REFERENCE`, matching native deformation; targets with neither
defaults nor animated channels add no dependency.

Incomplete or capability-incompatible recipes retain unknown concrete
inventories as `null`. Catalog capabilities are never invented bone IDs.
Required props remain catalog requirements: explicit prop approval does not
establish a socket, attachment ownership, semantic suitability or visual approval.

Top-level `asset_provenance` records a deduplicated exact rig manifest hash,
retained source inventory and provenance, and selected executable clip hashes
and provenance. Per-state `provenance_refs` identifies the rig and executable
clips used. These are report copies. The persisted manifest's state mapping
retains exactly `status`, `clips` and `reason`; the existing mutation owner
writes the full additive report to `animations/state-mapping.json`.

The #287 `work_estimate` remains one cycle per unique existing executable clip,
with zero model calls, explicit unavailable costs and existing limitations.
#347 event compilation and runtime ownership remain separate native hooks.
These reports neither generate missing animations nor establish all 416
catalog states as animated.

## Regression coverage and verification boundary

`tests/test_rig_state_discovery.py` retains eleven synthetic regression methods:
exact bone/morph/contact/source dependencies; bone-only default deformation;
default/channel union, deduplication and distinct mesh identities; absent default
target rejection; incomplete recipes; anatomy and explicit props; independent
report copies and invalid channels; real mutation/save/reload; unweighted native
constraint targets and ancestors; ignored optional constraint targets; and
declared zero-weight skin joints.

This preparation used authenticated repository source reads and Git object
creation only. The executor was unavailable. No Python test, numerical execution,
mutation workflow, compatibility run, provider, Blender or browser exercise was
run for this candidate. Textual preservation comparisons are source evidence,
not runtime evidence. Historical #354/#491 execution claims remain bound to
their original revisions and are not acceptance of the new integration.

The combined preservation must add the reporting calls to the existing compiler and include
this module in the existing animation/IK gate before running:

```bash
PYTHONPATH=scripts:tests python -m unittest test_rig_state_discovery -v
python scripts/test_rig_gate.py animation-ik
HATCH_DECOMPOSITION_BROWSER_TEST=1 python scripts/test_rig_gate.py compatibility
```

These commands are requirements, not recorded passes. Inspect exact-current-head
hosted results, artifacts and skip accounting after integration. Fresh independent
code/security review and applicable broader gates remain required. #11/#54/#55/#56
remain open; this source preservation grants no production, provider or artistic
acceptance.
