# Director dependency compiler

`scripts/director_graph.py` implements the read-only `GraphCompiler.compile`
boundary from #26. It uses #24's exact artifacts and method/result contracts,
#25's capability evidence admission, and native owner names. It neither submits
jobs nor declares assets, renders, reviews or a production complete.

## Input and execution boundary

Pass a strict `GraphCompiler.compile` request plus the project root to
`GraphCompiler().compile(request, root)`. The request pins three existing JSON
files: an `execution-plan`, capability probe result or snapshot, and asset
resolution result or value. All three and their transitive references must
match exact bytes. Capability evidence paths are relative to the project root;
create the initial probe directory as the project root, then add plan inputs.
Do not copy a snapshot from another root without preserving its relative files.

The generated `profiles/director-execution-plan.schema.json` describes the plan:

- `schema_version: 1`, `kind: execution-plan`, `state: planned`.
- `requirements`: unique `id`, test identifier/command, exact evidence refs.
- `nodes`: unique `id`, mandatory method `operation`, existing `inputs`,
  `output_schema`, `resource_class`, matching `native_owner`, capability IDs,
  estimated seconds (or explicit null), and requirement IDs.
- `edges`: source ID, target ID and exact shared input artifact.

An edge identifies a materialized native specification or common dependency,
present in both endpoint inputs. Future produced files have no hashes yet and
must not be represented by fabricated ArtifactRefs. `output_schema` expresses
the native result expected later. Native execution must bind and verify actual
produced outputs before consuming them; compilation does not execute, inspect
or approve future output. This compiler does not replace native job controllers.

The plan must cover every requirement. Unknown fields, complete-state claims,
wrong method owners/output schemas, duplicate IDs/edges, cycles, dangling edges,
conflicting path/identity aliases and stale byte references fail admission.
Inputs are reread after compilation to reject changes observed during the
operation. Execution must reverify pinned inputs again when starting work.

## Capabilities, fingerprints and estimates

Each node declares mandatory operation capabilities. Admission rejects missing,
expired, unavailable or discovery-only evidence, verifies the snapshot digest,
and delegates evidence interpretation to `director_capabilities.assert_admission`.
The fixture in the demonstration executes a genuine native OBJ importer probe;
it is not evidence that asset acquisition or rendering jobs ran.

Node order is a deterministic topological order, breaking ties by ID. Semantically
unordered input, capability, requirement and edge arrays are canonicalized.
Each cache key hashes the node, incoming edges, predecessor keys, pinned
capability snapshot and resolution. Changing a dependency changes descendant
keys. Refreshing capability expiry intentionally changes the snapshot identity.

CPU/render and GPU estimates must fit the supplied budget. Unknown estimates
remain null and make the critical-path estimate null; they are never invented
zeroes. Estimates do not reserve resources or authorize execution. The longest
dependency path is calculated from declared estimates; it is not a measured
runtime or a resource-constrained schedule.

`impact(previous_graph, current_graph, uncertain=[...])` reports changed,
transitively stale, preserved and removed node IDs. It follows old and new edges
so removal or reassignment does not hide downstream invalidation. Uncertain
nodes are conservatively stale even when their current inputs compare equal.
`traceability(plan, graph)` returns requirement → nodes → test/evidence mappings.

## Reproduce

```bash
python scripts/demonstrate_director_graph.py --destination output/director-graph
python scripts/director_graph.py --request output/director-graph/request.json --root output/director-graph
PYTHONPATH=scripts:tests python -m unittest test_director_graph test_director_capabilities test_rig_compatibility -v
python scripts/generate_schemas.py --check
```

The destination must be new. The demonstration retains the actual native import
receipt/source, strict request, compiled graph and traceability. It labels the
three-node plan synthetic and records `production_jobs_executed: false`.
Local compilation does not certify real Director MVP or final-film acceptance.

The file-backed compiler currently requires descriptor-relative, no-follow
filesystem access supplied by the existing Director boundary. Unsupported
platforms fail explicitly instead of silently reading unsafe paths.
