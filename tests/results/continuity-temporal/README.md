# Issue 116 temporal replay evidence

Implementation frozen at `fcbb418860173f3623cb910c0befcff8bd454d10`, based on
`3f31e3fbbed75ca61dde86cdc3f6a31ed2c347a2` (the dependent continuity115 branch).
This directory is synthetic structural verification, not production artwork or
independent visual approval. `report.json` records exact source/runtime hashes;
`native/` contains the real Scene3D fixture closure; `plan.json`, `events.json`,
`history.json` and six snapshot files retain the observed replay state.

## Reproduction

Run from the repository root with requirements-delivery installed:

```sh
/home/complete/.cache/hatch-coordination-20261112/verifier-venv/bin/python scripts/continuity_contracts.py --generate-schemas profiles --check
flock /home/complete/.cache/hatch-coordination-20261112/heavy-tests.lock /home/complete/.cache/hatch-coordination-20261112/verifier-venv/bin/python -m unittest discover -s tests -p 'test_*continuity*.py' -v
flock /home/complete/.cache/hatch-coordination-20261112/heavy-tests.lock /home/complete/.cache/hatch-coordination-20261112/verifier-venv/bin/python -c 'import sys,unittest; sys.path[:0]=["tests","scripts"]; names=["test_scene3d_contracts.py","test_rig_scene3d_math.py","test_production_contracts.py","test_production_performance.py"]; suite=unittest.TestSuite(unittest.defaultTestLoader.discover("tests",pattern=name) for name in names); result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(not result.wasSuccessful())'
/home/complete/.cache/hatch-coordination-20261112/verifier-venv/bin/python tests/continuity_temporal_probe.py NEW_OUTPUT_DIRECTORY
```

The original output directory was
`/home/complete/.cache/hatch-coordination-20261112/continuity116-probe-final`.
The probe requires a new output directory. Its SQLite sidecar is disposable and
is omitted from this source-controlled evidence; the exact authored history and
native closure required to reconstruct it are retained as JSON/native files.

## Assessment and failure history

Reused the existing continuity registry connection, transactions, content hashes,
native source-reference validation, canonical coordinates, Scene3D closure
loader, static hierarchy evaluator and exact rational `frame_time` helper.
Added three separate temporal contract kinds without changing the existing 23
kind family, existing generated schemas or existing test files. Runtime imports
remain free of optional numerical/renderer dependencies until evaluation.

Initial test attempts under system Python and `analysis-venv` failed because
`jsonschema` was unavailable (all nine initial temporal setup cases). The
configured `verifier-venv` provides Python 3.12.15, jsonschema 4.26.0 and NumPy
2.5.4. Its first functional run found one transform precondition mismatch caused
by integer versus float JSON serialization. Exact numerical equality now treats
2 and 2.0 as equal while preserving the distinction between true and 1; explicit
regression coverage is retained. The first combined compatibility runner also omitted `scripts` from its import path, producing two loader errors while 59 production tests passed. `compatibility-initial.log` preserves that failed invocation; the corrected command above supplies both import roots. Subsequent implementation checks passed. Pillow emits its
existing `getdata` deprecation warning in native capability tests.

See `continuity.log` and `compatibility.log` for retained exact final results.
The independent probe verifies out-of-order replay equality, terminal N versus
N−1, terminal/next-shot equivalence, before/after events, and flashback mapping.
Unit tests additionally cover same-tick ordering, between-frame/offscreen events,
time skips, atomic failures, missing mappings, duplicate application,
preconditions, branches, stale caches, changed dependencies, restart recovery,
native-source contradictions and real CLI calls.

Native animation/constraint/physics sampling is not supplied by the temporal
event engine; unsupported dynamic declarations fail closed and are explicitly
reported. Rich costume, ownership and layered continuity rules remain separate
117–119 work. No claim is made for full parent107/109 acceptance, unavailable
historical production artwork, cross-platform execution, final rendering or
visual approval. The implementation needs independent review before merge.
