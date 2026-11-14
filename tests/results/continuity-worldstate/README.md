# Persistent structural world state — local acceptance evidence

Issue117 adds explicit temporal v1.1 rich state to the existing SQLite immutable
history and replay authority. The implementation validation passed 70 tests in
94.470 seconds, including all 12 rich worldstate tests, existing contract/registry
and temporal suites. Both schema-generation checks passed. The final trace-only
change adds an actual SQLite close/reopen assertion; runtime and unit-test source
are unchanged from that regression run. Exact source/file hashes are in
verification.json; independent review and hosted checks are separate gates.

| Issue117 requirement | Executed evidence |
| --- | --- |
| World/entity/instance/render IDs; canonical mapped meters | Exact native Scene3D closure and revision pins; duplicate instance/entity and native asset substitution tests |
| Costumes/accessories/presence/locations/interactive state | Actual persistent trace with two character stand-ins and prop; costume/accessory, door/light state survives second scene file; entry/exit/occlusion and explicit relocation tests |
| Scoped facts/knowledge/prerequisites/causes/uncertainty | Retained character knowledge; temporary half-open fact lifetime, unresolved state, missing cause/prerequisite and malformed certainty tests |
| Entry/exit/occlusion/travel distinct | Camera occlusion keeps physical presence and native render flag; entry/exit change presence; relocation requires mapped destination and authored cause |
| Failure detection | Unmapped location, duplicated IDs, missing prerequisites, incompatible costume, unrecorded incoming reset and immutable identity replacement reject atomically |
| Durability and exact replay | Nonsequential snapshot equivalence, immutable native file hashes and actual SQLite close/reopen retain the exact same snapshot/head |

Reproduce with the optional rig Python environment:

```
python -m unittest discover -s tests -p test_rig_continuity_worldstate.py -v
python tests/continuity_worldstate_probe.py NEW_OUTPUT_DIRECTORY
python scripts/continuity_contracts.py --generate-schemas profiles --check
python scripts/generate_schemas.py --check
```

The trace/native directory retains actual hash-bound source files, events, plan,
immutable history and snapshots. The SQLite sidecar is disposable and recreated
by the probe; its binary bytes are not a portable acceptance format. These are
labeled engineering fixtures. Stored clothing/environment records establish
structural persistence, not geometry deformation, door motion, light rendering,
cloth/physics execution or visual/production approval. Issue118 owns exclusive
prop handoff transactions; the issue116 query followup must use the same replay
and does not introduce another store. Broader missions107/13 remain open.
