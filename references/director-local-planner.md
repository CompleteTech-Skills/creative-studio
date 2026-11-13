# Restricted local Director planner

`python scripts/director_planner.py` emits a **planned intent**, preserving the
original prompt verbatim. Its documented grammar is:

```
Two robots (explore|exploring) [a|an] [abandoned] laboratory[.|!]
```

Matching is case-insensitive and ignores spacing for recognition only. The
original text is retained. Unsupported subjects, additional directives, extra
characters, contradictory durations and audio instructions fail explicitly.
This is a deterministic parser, not live model inference or arbitrary language
understanding. User-controlled structured intent edits must pass the same closed
schema and semantic validator before use.

The local MVP plan uses persistent identities Aster and Moss, one access-key
prop, a real laboratory requirement, two camera shots, exactly 120 frames at
24 fps and 640×360. Aster investigates and lifts the key while Moss examines
and gestures toward the console. The story communicates through poses, gaze
and actions; delivery requires zero audio streams. These are authoring
requirements, not claims that assets, animation or rendering have executed.

Shot lengths use deterministic largest-remainder allocation after reserving a
minimum duration. Equal remainders favor the original beat order. Each marker
belongs to its half-open shot interval. There are no frame gaps or accidental
overlaps, and terminal boundary 120 is exactly five seconds.

The default output is `state: planned` with `admission: null`. To obtain an
admitted intent, provide a current evidence-backed CapabilityRegistry result,
its exact evidence directory, a resource-estimate JSON file and a project budget snapshot:

```sh
python scripts/director_planner.py \
  --capabilities /path/to/probe/result.json \
  --evidence-root /path/to/probe \
  --resources /path/to/estimated-resources.json \
  --budget /path/to/budget-snapshot.json
```

`create_budget(limits)` constructs a versioned, hash-bound local policy snapshot. It does not reserve or spend money; transactional accounting remains with the BudgetLedger owner. A changed budget requires a new revision. Resource estimates declare `max_cpu_seconds`, `max_memory_bytes`,
`max_storage_bytes` and `max_provider_calls`. Every estimate must fit both the project budget and the actual capability policy. The local path permits zero
provider calls. Admission verifies Blender CPU, H.264 and native GLB import
through the capability owner's actual evidence. The exact capability snapshot, intent, project budget and resource estimates
are hash-bound. `validate_admitted` requires the current execution budget and
estimates separately, repeats evidence validation before downstream use and
rejects edited intent, replacement budgets or rebound snapshots. Backend capability admission
does not establish that a particular scene meets render-time limits. Downstream
native model, scene, animation and render owners must bind and validate actual
artifacts and measured use. An admitted plan still cannot claim production
completion or visual approval.

The generated `profiles/director-mvp-intent.schema.json` belongs to this draft
intent format. Existing Director core documents and all legacy 2D schemas keep
their formats. This planner does not create placeholder native artifact hashes;
subsequent graph/native adapters bind actual request/specification bytes.
