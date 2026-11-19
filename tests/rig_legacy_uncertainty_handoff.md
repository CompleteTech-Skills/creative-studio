# Historical anatomy uncertainty preservation oracle

Test-only scope, frozen at public revision
`74b1d9f1435560f20aa5112f0e42fcf349c39b92`. No corrective implementation or
implementation diff was inspected. Read the repository README, SECURITY and
CONTRIBUTING guidance and public native dispatcher, helpers and tests. No
AGENTS.md or CLAUDE.md was tracked at that revision.

`test_rig_legacy_uncertainty.py` uses synthetic GLB geometry, real public `auto`
and `correct` dispatch, native `save_rig`, and independent `load_rig` reopening.
Every correction checks original source bytes, the source record and preexisting
revision/correction bytes. No network, external provider or live service runs.

## Automatic baseline

Exact invocation, from the isolated worktree (the executable was the supplied
`env-rig/bin/python`):

```text
PYTHONPATH=scripts:tests <local-work-dir>/env-rig/bin/python -m unittest -v test_rig_legacy_uncertainty
```

Observed exit **1**, **4 tests**, **2 failures**, **2 passes**, **0 skips**.
The failures were observed before any fix:

- `test_unrelated_role_edit_preserves_unowned_historical_record`: the unrelated
  right-jaw role edit dropped the stale left-jaw anatomy record.
- `test_known_bone_removal_preserves_unowned_historical_record`: removing the
  known right jaw removed its own issue and also dropped the stale left issue.

Positive controls passed:

- `test_known_owner_rename_preserves_pending_evidence`.
- `test_known_owner_rename_and_name_reuse_track_distinct_bones`: rename the left
  jaw, rename the right jaw into its old spelling, then remove that reused name;
  only the right-jaw issue is removed, retaining the original left-jaw evidence.

The automatic historical fixtures **simulate historical saved bytes**. They run
a current public rename, restore the original uncertainty element spelling, and
save/reopen with operation `synthetic-historical-uncertainty-format-fixture`.
They are not represented as genuine old-writer execution.

## Separate genuine cross-version reproduction

The unmodified independent reproduction source is
`<local-work-dir>/campaign-evidence/review486-74b1-integrity-upgrade.py`, SHA256
`b85540ca144ad897c9bd2b06096feab08dd39e6214cfc149490c4a6441c9f166`.
It was not edited, copied into this commit or overwritten.

Its `create` mode executed against exact old public source revision
`929643842be2d4abc2554080c83e4df0683c9a89` and exited **0**. The old public
`auto` produced pending `jaw.left` geometry-fit uncertainty; the old public
rename changed the bone to `left-renamed` while retaining the issue's
`element: jaw.left`, original evidence and `resolution: pending`.

The genuine old output remains untouched at
`<local-work-dir>/review486-old-writer-evidence/` (13 files). Its input
`fixture.glb` SHA256 is
`f100c5e75113a23c9eeffea151154070fd216d9bc0afc53192612a549f269a2e`;
its saved `fixture/rig.json` SHA256 is
`d56209f881dbdde5ca4c9b934a35b44c7186d8affbd1a4ca95a93a1144fe3605`.
The retained source copy still matches the original input bytes.

Only a byte copy at `<local-work-dir>/review486-upgrade-runtime/` was upgraded.
Running the same unmodified script's `upgrade` mode against public74b1 exited
**1** at `unrelated role edit dropped pending evidence`; after the unrelated
right-jaw role edit its uncertainty list was empty. The script's prior source
and history byte-preservation assertions passed before this failure. This is
actual local cross-version runtime evidence on synthetic geometry; it does not
claim production, rendering, artistic review or external interoperability.

Only the new automatic test module and this sanitized handoff belong to this
commit. No CI, gates, schemas, implementation, shared branch references or
historical evidence were changed.
