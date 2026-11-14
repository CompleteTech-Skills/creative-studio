# Director revisions

`director_revision.RevisionService` compiles bounded revision instructions to
hash-bound typed patches. It delegates persistence, immutable versions,
optimistic concurrency, recovery and undo/redo to `EditorRepository`.
It does not create another project database, scene evaluator, material registry,
job controller, artifact publisher or approval store.

```python
service = RevisionService(repository)
saved = repository.snapshot()
planned = service.plan(
    'Set camera camera near to 0.2',
    expected_revision=saved['revision'], expected_sha256=saved['sha256'])
receipt = service.apply(planned['patch'], transaction_id='camera-near-change')
```

Planning and impact are read-only. The exact original instruction is retained.
An application can supply a typed `change` produced by its existing planning
provider; this adapter never calls a provider or infers provider execution.
The deliberately small local grammar supports explicit camera optical scalars,
local camera translation/XYZW rotation, exact shot extension, explicit scalar
material changes and world exposure. Ambiguous instructions such as “make the
ending better”, unsupported geometry alterations or unspecified camera movement
fail with actionable typed diagnostics rather than invented changes.

Supported typed changes are:

- `camera`: exact camera node/name and `properties` for its native camera
  component. Partial properties are merged with the acknowledged source camera
  and validated as a complete native component.
- `camera-pose`: explicit local `translation` and/or canonical XYZW `rotation`.
  Native transform validation remains authoritative. Driven cameras or shot
  pose overrides require the native camera/timeline adapter instead.
- `material`: exact geometry node, canonical primitive `slot` and scalar PBR
  `surface` properties. The inspector owner publishes an immutable native
  material or derived version; original geometry and previous material bytes
  remain unchanged.
- `duration`: exact active shot and positive `additional_seconds`, expressed
  as a bounded decimal or integer ratio. Multiplication by exact rational FPS
  must produce a whole number of frames. Project and analysis frame budgets
  are enforced without rounding or unbounded exponent parsing.
- `ending`: exact active shot, existing inline native light, half-open `frame`
  and bounded `intensity`. This uses the canonical timeline `key-upsert` adapter,
  not a parallel lighting or animation store. Bound LookDev lighting requires
  its existing native authoring adapter.
- `world`: explicit supported native world properties.

Duration and ending application require the canonical `editor3d_timeline`
dependency. Its absence is a typed dependency failure; it is not a successful
metadata-only revision. The required positive timeline test is not skipped.

## Locks, source identity and authority

Applications can install original constraints through the trusted constructor
or an exact `brief_reference` plus its configured `artifact_root`. A pinned
brief is decoded, validated and checked for exact ID/version/kind/schema and
project identity on each operation. `director_project_id` in native project
metadata can declare the explicit logical Director project mapping.

Native project metadata can retain a bounded JSON string named
`director_locked_constraints` because native metadata values are scalar. Its
constraints are always included. Supported constraints are `identity:ID`,
`camera:ID`, `material:ID`, `duration:ID`, `ending:ID`, their `:*` forms, and
`world`. “Keep both robot designs exactly the same.” maps to the character
identity lock. Unsupported source constraints block editing until the planning
owner resolves them; they are never ignored.

Caller-supplied constraints cannot remove source constraints, even if a caller
recomputes a patch digest. Locked requirements require the application-installed
`authorizer` to validate this exact patch and applicable immutable authority
evidence. A user-supplied `approved` boolean does not grant authority. Native
node/collection locks remain enforced by the native graph owner as well.

## Impact and execution boundaries

Camera revisions invalidate their selected shots. Material/world revisions
conservatively invalidate active shots using the selected scene; duration changes
also require timeline reflow and movie reassembly. Ending revisions report the
affected half-open frame suffix. Historical scenes/shots are excluded by exact
active scene path/identity/version selection. The canonical graph impact helper
propagates affected shot inputs to their downstream graph consumers while
preserving unrelated shot branches. Coarse scene-only graph inputs are treated
conservatively.

Native asset documents and source bytes are preserved. Comparisons enumerate
actual changed, added, removed and preserved file hashes across retained native
revisions. Existing review records retain their original references; these
operations never grant visual or artistic approval to a new revision.

Receipts report render-frame estimates, zero provider calls, and explicitly
unknown CPU/GPU/currency regeneration costs. They report
`pending-native-execution-admission`: no images are rendered and no cached movie
is certified by this revision adapter. The existing execution service must admit
the stale work through its capability, budget, claim and verification owners.

## Canonical method envelopes and artifacts

`execute(request, artifact_root=..., publish_artifact=...)` adapts the canonical
`RevisionService.plan`, `.impact` and `.apply` request/results. It verifies exact
input bytes, context project/hash, deadlines, patch identity and native revision
bindings before mutation. Native project and descendant ArtifactRefs point into
the existing immutable repository closure; a bare manifest is never copied
without its native resources.

Only patch publication needs an application-supplied canonical asset publisher.
Its signature is `publish_artifact(payload, artifact_id=..., kind=...,
schema_version=..., semantic_version=..., dependencies=..., fixture=...) -> ArtifactRef`.
The returned reference is
validated and its actual bytes read back. It must use the existing immutable
asset authority; this module ships no alternative generic publisher. Without
that integration, canonical planning fails before publication or project edits.
The test fixture publisher exercises the envelope boundary only and is not a
production publication implementation.

The expected apply revision in the canonical envelope is the native project
manifest byte hash. Patches separately pin the editor repository revision number
and complete inventory hash. Those two identity domains are never interchanged.
The context project ID is the native project ID; `director_project_id` metadata
only binds an original logical brief and does not rewrite incoming operation
contexts. A dashboard must label this as a native-scene driver. Updating the
higher-level DirectorProject document is a separate existing-owner integration,
not an implicit effect of this native adapter.

## Validation and remaining integration

Run the focused native persistence tests with the optional rig environment:

```bash
PYTHONPATH=scripts:tests python -m unittest test_rig_director_revision -v
```

The core tests exercise real saved camera/material revisions, native reopen,
immutable versions, exact source preservation, undo/redo, idempotency, stale and
tampered patches, original brief locks, budget bounds and method artifact reads.
The separate required timeline test executes actual duration and ending edits
once the canonical timeline dependency is integrated. A missing dependency,
trusted publisher integration, unexecuted regeneration or planning-provider
backend remains explicit; this module does not establish full issue or parent
production acceptance by itself.
