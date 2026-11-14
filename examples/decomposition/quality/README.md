# Evaluation candidates, not an accepted held-out corpus

`candidates.json` inventories known source material, exact hashes for the two
repository artwork originals, visual observations and unresolved provenance.
It deliberately declares **zero accepted held-out cases**. It neither supplies
reference annotations nor authorizes automatic semantic approval.

Read the [frozen future evaluation protocol](../../../references/decomposition-quality-protocol.md).
The existing manual decomposition examples and real analyzer pilots retain their
own provenance and outcomes. This inventory does not copy or overwrite them.

Run the deterministic metric regressions with the ordinary Pillow test dependency:

```bash
python -m unittest discover -s tests -p test_decomposition_quality.py -v
```

These tests check counting and mask math, not model quality, source licensing,
human reference correctness, or the complete automatic workflow.
