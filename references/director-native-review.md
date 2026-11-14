# Native Director review journal

`production_workflow.NativeReviewJournal` extends the existing production review
authority. It uses the same `production-workflow.json`, `hatch.ownership` lock,
hash-linked review history, event writer and atomic save operation. Existing 2D
review formats and hashes are unchanged. A native journal has an explicit kind
and will refuse to overwrite a legacy or foreign project's journal. It does not
create a second project, acquisition controller or approval database.

Construct it with an existing owned journal directory and an exact Director
project `ArtifactRef`:

```python
from production_workflow import NativeReviewJournal

journal = NativeReviewJournal(editor_repository.root, director_project_ref,
                              artifact_root=workspace_root)
```

The project is decoded and validated through the Director owner. Identity,
schema/version/hash and fixture classification are checked again before writes.
Ordinary 2D loading does not import the Director or optional 3D packages. The
native adapter currently requires the Director owner's secure descriptor-relative
artifact reader; an unsupported platform fails explicitly before persistence.

`record(receipt, transaction_id, current_dependencies=...)` records a scoped
attestation. The current dependency map comes from the authoritative QA/project
adapter, rather than being inferred from a reviewer's claims. Each dimension
uses exact `ArtifactRef` dependencies, an explicit `pass` or `fail`, notes and
inspectable evidence:

```json
{
  "schema_version": 1,
  "kind": "director-native-review",
  "project_id": "the-current-project",
  "fixture": true,
  "reviewer": {"kind": "assistant", "id": "identified-reviewer", "name": "Actual reviewer name"},
  "dimensions": {
    "camera_composition": {
      "verdict": "fail",
      "notes": "The actual inspected framing needs correction.",
      "dependencies": ["Exact ArtifactRef objects are supplied here"],
      "evidence": [{
        "artifact": "An exact ArtifactRef object is supplied here",
        "method": "visual_inspection",
        "frames": {"start": 20, "end": 21},
        "note": "Actual observation and uncertainty, where applicable"
      }]
    }
  }
}
```

The strings standing in for references above are explanatory and are deliberately
not admissible receipts. An actual call must provide complete, current references.
Every inspected artifact must be among its dimension's current dependencies.
Frame intervals are bounded and half-open. A passing human or assistant review
must include an actual fully decoded image inspected by the identified reviewer;
movies and metrics can supplement that evidence. A synthetic test cannot grant a
passing visual review. Decoding an image by itself never performs subjective
inspection. Reviewer identity is a disclosed self-attestation, not authenticated
by an external identity provider. The journal always returns
`production_art_acceptance: false`; broader artistic or final acceptance belongs
to the applicable review authority and complete requirements.

`status(current_dependencies)` reports each requested dimension as unreviewed,
pass, fail or stale. It checks actual dependency and evidence bytes. Changing a
camera input stales that scope; unrelated dimensions can retain their reviews.
An empty read-only scope returns `current_pass: false`, including for structural
profiles that declare no visual reviews. Recording a review still requires a
nonempty explicit scope.
An unrelated Director project metadata version does not become a whole-project
review invalidation key. The active project identity and fixture status remain
fixed, while each record preserves its exact historical project reference.

The mutable journal is explicitly forbidden as a current input dependency,
including through a project's review/dependency references. Existing native
editor snapshot inventories and exact native artifact references remain the
signature authority. Appending a review therefore does not change an editor
snapshot or invalidate that same review. There is no replacement content-hash
authority for native project state.

`append_validation(report, transaction_id)` stores a separate structural report
chain and event. Reports identify `schema_version: 1`, `kind:
director-qa-report`, the current `project_id`, `fixture`, exact `project` and
`profile` references, a status, nonempty declared `inputs`, and `checks`.
Each check identifies its target `artifact`, status and bounded `evidence` refs.
A passed report needs nonempty passed checks, no cancellation/deadline failure,
and verified bytes for every declared input and proof; it cannot assert artistic
acceptance. The QA implementation remains responsible for executing its checks.

Failed, partial, canceled, unavailable and unverified reports retain expected
references to the broken or missing inputs they diagnose. Their references still
undergo structural/path/size validation, but those failed input bytes are not
required to verify before preserving the failure. The current project's and
profile's primary bytes always verify, as do every check's primary target and
evidence files when that check claims to have passed. Declared failed dependencies
of a valid project/profile are retained without reading them. Native owner
descriptors inside check details remain native metadata and are not reinterpreted
as additional Director file-read authority. Non-passed records explicitly set
`all_required_inputs_verified: false`; a historical failure never grants current
execution or visual verification.

`validation_history()` explicitly returns historical
reports with `current_execution_verified: false`; reading a past report does
not execute QA again or promote a visual milestone. `history()` returns both
chains and their event references.

Transaction identifiers are shared across review and validation appends. An
identical retry returns the original record hash and performs no write; a reused
identifier with different content fails. Concurrent writers share the existing
nonblocking owner lock: busy callers must retry within their own bounded policy.
Failures preserve old records. No compaction, deletion or relabeling occurs.

Journal metadata is limited to 8 MiB and 4,096 total records. Each operation has
bounded reference counts and byte totals; decoded inspected images have a
128-megapixel aggregate limit per scoped review. Larger reviews must be split
into bounded scopes. Symbolic links, FIFOs, devices, modified hashes, foreign
project contexts, malformed history and unsupported secure-read platforms fail
closed. These guarantees use the repository's existing trusted owned-directory
and cooperative owner-lock model; they are not a sandbox for hostile same-user
code.

Run the actual-file and owner-lock regressions with the optional native test
environment:

```bash
PYTHONPATH=scripts:tests python -m unittest test_rig_director_native_review -v
PYTHONPATH=scripts:tests python -m unittest test_production_review_regressions test_production_workflow -v
```

Tests use explicit synthetic policy inputs and real native repository revisions.
They do not fabricate production reviews or execute a film renderer.
