# Independent PR486 owner/reload regression handoff

Frozen public implementation: `22a661de1e8fb982c1fcbbde23d9d20b64393e68`.
No future implementation, integrator worktree or proposed representation was inspected.
Only the new test module and this handoff are authored. Existing tests, source,
contracts, schema and gates are unchanged.

Issue52 requires observable fitting, unresolved anatomy, hash-checked human
corrections and version diffs, preserved immutable source evidence, real affected
workflow tests and honest validation limits. Repository CONTRIBUTING.md,
SECURITY.md and relevant README material were read. The cached real issue52
body at `campaign-evidence/review486-74b1-correctness-issue52.json` was read in the
separate read-only evidence checkout. Its response reports three comments but
contains no comment bodies. No offline cache of issue52 comments or parent #11
was located; those continuation requirements were not independently re-fetched
by this offline test lane. The integrator owns their complete review.

The initial historical-format seed is **simulated**, using the unchanged
`test_rig_legacy_uncertainty.RigLegacyUncertaintyTests.historical_format` helper.
It executes the current native rename and restores the pre-rename uncertainty
record through `save_rig`. This suite does not claim execution of the old writer.
The separate genuine old-writer probes remain separate evidence. No test requires
historical Git objects, so shallow CI checkouts can execute the suite.

After the seed, all corrections use public `rig_operations.mutate`, native
`save_rig`, and `load_rig`. Each correction checks successful persisted job state,
source-record and source-byte preservation, and byte-for-byte preservation of
pre-existing correction/revision JSON files. Extra save/reload boundaries check
those immutable bytes again, public manifest persistence, skeleton contents and
uncertainty records. Synthetic GLB geometry and native Python dependencies only;
no Blender, browser, external host, live API, production asset or paid execution.

The new suite has six test methods, covering ten scenarios:

- Roll and head edits of an unrelated added bone or a renamed known bone that
  reuses the old spelling cannot mark the historical pending record corrected.
- Later removal of either reused-name bone cannot delete the historical record.
- Later rename of either reused-name bone cannot remap the historical record.
- A genuinely known owner still renames, becomes corrected after an anatomical
  edit, and loses its own record on removal, across native save/reload boundaries.
- Two real missing-landmark geometry-fit records are made byte-identical by the
  simulated left orphan and native right-owner rename. After repeated reloads,
  removing the actual known bone must leave exactly one historical occurrence.

The first four methods run two independent native runs each; the final two run
one each. Assertions target observable pending evidence, record occurrences and
actual skeleton changes, with no private ownership representation assumptions.

## Exact runtime commands and results

Working directory: `<oracle-worktree>` at the frozen revision. Runtime:
`<workspace>/env-rig/bin/python` (existing rig virtual environment).

```bash
PYTHONPATH=scripts:tests <workspace>/env-rig/bin/python -m unittest -v test_rig_uncertainty_owner_reload
```

Exit **1**. Six test methods in **8.503 seconds**, **9 assertion failures**,
**0 errors**, **0 skips**. The known-owner rename/edit/removal control passed.
All eight reused-name edit/removal/rename subcases and the identical-occurrence
removal failed at the expected evidence outcomes. This is deliberately retained
red regression evidence against the frozen implementation, not a passing result.

```bash
PYTHONPATH=scripts:tests <workspace>/env-rig/bin/python -m unittest -v test_rig_legacy_uncertainty
```

Exit **0**. Four unchanged legacy tests passed in **1.459 seconds**, no skips.
No broader rig, Blender, rendering, artistic-acceptance or production claim is made.

```bash
git diff --check
```

Exit **0** (before staging); staged whitespace is checked again before commit.

## Setup/discovery diagnostics

The initial supplied repository path was its ordinary main checkout at
`b1f2f6bee16a10916f331ae91396430c62c6c657`; that checkout did not contain the legacy
helper test. No files there were edited. An isolated no-checkout worktree was
created at exactly the frozen public head, then populated with sparse root test
files, scripts, profiles, rig-templates, styles, schemas and contracts. The helper
imports also required the existing `test_rig_math.py` and `test_rig_skinning.py`.
After those sparse additions, the fixture smoke probe exited **0** and produced
the expected two pending native uncertainty records. There was no failed Python
import, skipped runtime case, or dependency-install attempt.

One exploratory shell command ended in:

```bash
cat ../review486-74b1-correctness/campaign-evidence/*issue52*comments*
```

Exit **1**, diagnostic:

```text
cat: '../review486-74b1-correctness/campaign-evidence/*issue52*comments*': No such file or directory
```

Another discovery command was:

```bash
rg --files ../review486-22a-correctness/campaign-evidence ../review486-22a-integrity/campaign-evidence ../review486-74b1-correctness/campaign-evidence | rg '(issue|comment|mission)'
```

Shell pipeline exit **0** because its final filter found the cached issue52 file.
Its first command reported two missing evidence directories:

```text
rg: ../review486-22a-correctness/campaign-evidence: No such file or directory (os error 2)
rg: ../review486-22a-integrity/campaign-evidence: No such file or directory (os error 2)
```

These are offline evidence-cache discovery limitations, not product failures or
test skips. Full raw runtime logs remain in local temporary storage; the complete
failure log below changes the checkout path to repository-relative form and
strips unittest trailing whitespace,
consistent with CONTRIBUTING.md. No historical failed evidence was overwritten.

## Artifact hashes

| Artifact | SHA-256 |
| --- | --- |
| `tests/test_rig_uncertainty_owner_reload.py` | `460064e93a873eea7a456503d15bdbc83952003b143c4111c6957744173ea36a` |
| Raw frozen baseline failure log | `a6077d28fd00970cb04d29bbcb9d15d9c4a0ed1253b0311360d114505ba5ef4b` |
| Repository-relative failure log reproduced below | `5436ddc7866a027a5f222d4e6050e81127b7b891b35477d9f25541edab982c48` |
| Raw unchanged legacy passing log | `97a01cf8f7852adc9218b47ce76df116f7500fffec37b0ea18e92bd7c8f6d2c6` |

## Complete frozen baseline failure log (repository-relative paths)

```text
test_byte_identical_occurrences_keep_distinct_ownership_after_reload (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_byte_identical_occurrences_keep_distinct_ownership_after_reload) ... FAIL
test_head_edit_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_head_edit_after_reload_preserves_orphan_pending_evidence) ...
  test_head_edit_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_head_edit_after_reload_preserves_orphan_pending_evidence) (reuse='add-new', field='head') ... FAIL
  test_head_edit_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_head_edit_after_reload_preserves_orphan_pending_evidence) (reuse='rename-known', field='head') ... FAIL
test_known_owner_rename_edit_removal_still_work_across_reloads (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_known_owner_rename_edit_removal_still_work_across_reloads) ... ok
test_removal_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_removal_after_reload_preserves_orphan_pending_evidence) ...
  test_removal_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_removal_after_reload_preserves_orphan_pending_evidence) (reuse='add-new') ... FAIL
  test_removal_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_removal_after_reload_preserves_orphan_pending_evidence) (reuse='rename-known') ... FAIL
test_rename_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_rename_after_reload_preserves_orphan_pending_evidence) ...
  test_rename_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_rename_after_reload_preserves_orphan_pending_evidence) (reuse='add-new') ... FAIL
  test_rename_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_rename_after_reload_preserves_orphan_pending_evidence) (reuse='rename-known') ... FAIL
test_roll_edit_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_roll_edit_after_reload_preserves_orphan_pending_evidence) ...
  test_roll_edit_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_roll_edit_after_reload_preserves_orphan_pending_evidence) (reuse='add-new', field='roll') ... FAIL
  test_roll_edit_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_roll_edit_after_reload_preserves_orphan_pending_evidence) (reuse='rename-known', field='roll') ... FAIL

======================================================================
FAIL: test_byte_identical_occurrences_keep_distinct_ownership_after_reload (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_byte_identical_occurrences_keep_distinct_ownership_after_reload)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_rig_uncertainty_owner_reload.py", line 154, in test_byte_identical_occurrences_keep_distinct_ownership_after_reload
    self.assertEqual(self.rig.manifest['uncertainties'], self.initial[:1],
AssertionError: Lists differ: [] != [{'element': 'jaw.left', 'code': 'RIG_ANAT[263 chars]ng'}]

Second list contains 1 additional elements.
First extra element 0:
{'element': 'jaw.left', 'code': 'RIG_ANATOMY_UNCERTAIN', 'message': 'Inspect joint position, direction and anatomical role before production approval.', 'evidence': {'source': 'geometry-fit', 'reference': 'Measured mesh bounds; anatomical placement is uncertain', 'confidence': 0.4}, 'resolution': 'pending'}

- []
+ [{'code': 'RIG_ANATOMY_UNCERTAIN',
+   'element': 'jaw.left',
+   'evidence': {'confidence': 0.4,
+                'reference': 'Measured mesh bounds; anatomical placement is '
+                             'uncertain',
+                'source': 'geometry-fit'},
+   'message': 'Inspect joint position, direction and anatomical role before '
+              'production approval.',
+   'resolution': 'pending'}] : Only the actual removed bone loses its own occurrence

======================================================================
FAIL: test_head_edit_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_head_edit_after_reload_preserves_orphan_pending_evidence) (reuse='add-new', field='head')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_rig_uncertainty_owner_reload.py", line 82, in anatomical_edit_after_reload
    self.assertEqual(self.rig.manifest['uncertainties'][0], self.initial[0],
AssertionError: {'ele[242 chars]t is uncertain', 'confidence': 0.4}, 'resolution': 'corrected'} != {'ele[242 chars]t is uncertain', 'confidence': 0.4}, 'resolution': 'pending'}
  {'code': 'RIG_ANATOMY_UNCERTAIN',
   'element': 'jaw.left',
   'evidence': {'confidence': 0.4,
                'reference': 'Measured mesh bounds; anatomical placement is '
                             'uncertain',
                'source': 'geometry-fit'},
   'message': 'Inspect joint position, direction and anatomical role before '
              'production approval.',
-  'resolution': 'corrected'}
?                 ^^^^ ^^^

+  'resolution': 'pending'}
?                 ^ ^ +++
 : Editing the reused name cannot correct historical anatomy

======================================================================
FAIL: test_head_edit_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_head_edit_after_reload_preserves_orphan_pending_evidence) (reuse='rename-known', field='head')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_rig_uncertainty_owner_reload.py", line 82, in anatomical_edit_after_reload
    self.assertEqual(self.rig.manifest['uncertainties'][0], self.initial[0],
AssertionError: {'ele[242 chars]t is uncertain', 'confidence': 0.4}, 'resolution': 'corrected'} != {'ele[242 chars]t is uncertain', 'confidence': 0.4}, 'resolution': 'pending'}
  {'code': 'RIG_ANATOMY_UNCERTAIN',
   'element': 'jaw.left',
   'evidence': {'confidence': 0.4,
                'reference': 'Measured mesh bounds; anatomical placement is '
                             'uncertain',
                'source': 'geometry-fit'},
   'message': 'Inspect joint position, direction and anatomical role before '
              'production approval.',
-  'resolution': 'corrected'}
?                 ^^^^ ^^^

+  'resolution': 'pending'}
?                 ^ ^ +++
 : Editing the reused name cannot correct historical anatomy

======================================================================
FAIL: test_removal_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_removal_after_reload_preserves_orphan_pending_evidence) (reuse='add-new')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_rig_uncertainty_owner_reload.py", line 104, in test_removal_after_reload_preserves_orphan_pending_evidence
    self.assertEqual(self.rig.manifest['uncertainties'], expected)
AssertionError: Lists differ: [{'element': 'jaw.right', 'code': 'RIG_ANATOMY_UNCERTAIN', [245 chars]ng'}] != [{'element': 'jaw.left', 'code': 'RIG_ANATOMY_UNCERTAIN', '[555 chars]ng'}]

First differing element 0:
{'element': 'jaw.right', 'code': 'RIG_ANATOMY_UNCERTAIN', [244 chars]ing'}
{'element': 'jaw.left', 'code': 'RIG_ANATOMY_UNCERTAIN', '[245 chars]ing'}

Second list contains 1 additional elements.
First extra element 1:
{'element': 'jaw.right', 'code': 'RIG_ANATOMY_UNCERTAIN', 'message': 'Inspect joint position, direction and anatomical role before production approval.', 'evidence': {'source': 'human-landmark', 'reference': 'Synthetic right-jaw landmark for ownership controls', 'confidence': 0.5}, 'resolution': 'pending'}

Diff is 827 characters long. Set self.maxDiff to None to see it.

======================================================================
FAIL: test_removal_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_removal_after_reload_preserves_orphan_pending_evidence) (reuse='rename-known')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_rig_uncertainty_owner_reload.py", line 104, in test_removal_after_reload_preserves_orphan_pending_evidence
    self.assertEqual(self.rig.manifest['uncertainties'], expected)
AssertionError: Lists differ: [] != [{'element': 'jaw.left', 'code': 'RIG_ANAT[263 chars]ng'}]

Second list contains 1 additional elements.
First extra element 0:
{'element': 'jaw.left', 'code': 'RIG_ANATOMY_UNCERTAIN', 'message': 'Inspect joint position, direction and anatomical role before production approval.', 'evidence': {'source': 'geometry-fit', 'reference': 'Measured mesh bounds; anatomical placement is uncertain', 'confidence': 0.4}, 'resolution': 'pending'}

- []
+ [{'code': 'RIG_ANATOMY_UNCERTAIN',
+   'element': 'jaw.left',
+   'evidence': {'confidence': 0.4,
+                'reference': 'Measured mesh bounds; anatomical placement is '
+                             'uncertain',
+                'source': 'geometry-fit'},
+   'message': 'Inspect joint position, direction and anatomical role before '
+              'production approval.',
+   'resolution': 'pending'}]

======================================================================
FAIL: test_rename_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_rename_after_reload_preserves_orphan_pending_evidence) (reuse='add-new')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_rig_uncertainty_owner_reload.py", line 118, in test_rename_after_reload_preserves_orphan_pending_evidence
    self.assertEqual(self.rig.manifest['uncertainties'], expected)
AssertionError: Lists differ: [{'element': 'unrelated-renamed', 'code': 'RIG_ANATOMY_[568 chars]ng'}] != [{'element': 'jaw.left', 'code': 'RIG_ANATOMY_UNCERTAIN[559 chars]ng'}]

First differing element 0:
{'element': 'unrelated-renamed', 'code': 'RIG_ANATOMY_[258 chars]ing'}
{'element': 'jaw.left', 'code': 'RIG_ANATOMY_UNCERTAIN[249 chars]ing'}

Diff is 863 characters long. Set self.maxDiff to None to see it.

======================================================================
FAIL: test_rename_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_rename_after_reload_preserves_orphan_pending_evidence) (reuse='rename-known')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_rig_uncertainty_owner_reload.py", line 118, in test_rename_after_reload_preserves_orphan_pending_evidence
    self.assertEqual(self.rig.manifest['uncertainties'], expected)
AssertionError: Lists differ: [{'element': 'unrelated-renamed', 'code': 'RIG_ANATOMY_[576 chars]ng'}] != [{'element': 'jaw.left', 'code': 'RIG_ANATOMY_UNCERTAIN[567 chars]ng'}]

First differing element 0:
{'element': 'unrelated-renamed', 'code': 'RIG_ANATOMY_[258 chars]ing'}
{'element': 'jaw.left', 'code': 'RIG_ANATOMY_UNCERTAIN[249 chars]ing'}

Diff is 871 characters long. Set self.maxDiff to None to see it.

======================================================================
FAIL: test_roll_edit_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_roll_edit_after_reload_preserves_orphan_pending_evidence) (reuse='add-new', field='roll')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_rig_uncertainty_owner_reload.py", line 82, in anatomical_edit_after_reload
    self.assertEqual(self.rig.manifest['uncertainties'][0], self.initial[0],
AssertionError: {'ele[242 chars]t is uncertain', 'confidence': 0.4}, 'resolution': 'corrected'} != {'ele[242 chars]t is uncertain', 'confidence': 0.4}, 'resolution': 'pending'}
  {'code': 'RIG_ANATOMY_UNCERTAIN',
   'element': 'jaw.left',
   'evidence': {'confidence': 0.4,
                'reference': 'Measured mesh bounds; anatomical placement is '
                             'uncertain',
                'source': 'geometry-fit'},
   'message': 'Inspect joint position, direction and anatomical role before '
              'production approval.',
-  'resolution': 'corrected'}
?                 ^^^^ ^^^

+  'resolution': 'pending'}
?                 ^ ^ +++
 : Editing the reused name cannot correct historical anatomy

======================================================================
FAIL: test_roll_edit_after_reload_preserves_orphan_pending_evidence (test_rig_uncertainty_owner_reload.RigUncertaintyOwnerReloadTests.test_roll_edit_after_reload_preserves_orphan_pending_evidence) (reuse='rename-known', field='roll')
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_rig_uncertainty_owner_reload.py", line 82, in anatomical_edit_after_reload
    self.assertEqual(self.rig.manifest['uncertainties'][0], self.initial[0],
AssertionError: {'ele[242 chars]t is uncertain', 'confidence': 0.4}, 'resolution': 'corrected'} != {'ele[242 chars]t is uncertain', 'confidence': 0.4}, 'resolution': 'pending'}
  {'code': 'RIG_ANATOMY_UNCERTAIN',
   'element': 'jaw.left',
   'evidence': {'confidence': 0.4,
                'reference': 'Measured mesh bounds; anatomical placement is '
                             'uncertain',
                'source': 'geometry-fit'},
   'message': 'Inspect joint position, direction and anatomical role before '
              'production approval.',
-  'resolution': 'corrected'}
?                 ^^^^ ^^^

+  'resolution': 'pending'}
?                 ^ ^ +++
 : Editing the reused name cannot correct historical anatomy

----------------------------------------------------------------------
Ran 6 tests in 8.503s

FAILED (failures=9)
```

## Complete unchanged legacy baseline log

```text
test_known_bone_removal_preserves_unowned_historical_record (test_rig_legacy_uncertainty.RigLegacyUncertaintyTests.test_known_bone_removal_preserves_unowned_historical_record) ... ok
test_known_owner_rename_and_name_reuse_track_distinct_bones (test_rig_legacy_uncertainty.RigLegacyUncertaintyTests.test_known_owner_rename_and_name_reuse_track_distinct_bones) ... ok
test_known_owner_rename_preserves_pending_evidence (test_rig_legacy_uncertainty.RigLegacyUncertaintyTests.test_known_owner_rename_preserves_pending_evidence) ... ok
test_unrelated_role_edit_preserves_unowned_historical_record (test_rig_legacy_uncertainty.RigLegacyUncertaintyTests.test_unrelated_role_edit_preserves_unowned_historical_record) ... ok

----------------------------------------------------------------------
Ran 4 tests in 1.459s

OK
```

## Pre-commit whitespace diagnostic

The first `git diff --cached --check` exited **2**, detecting four trailing spaces
emitted by unittest after `...` in the embedded raw failure log. The original
runtime log was retained unchanged; only its Markdown reproduction had trailing
whitespace stripped. The table reports both the original and reproduced hashes.
No product tests or outcome assertions changed. The repeated staged whitespace
check exited **0** before the commit.
