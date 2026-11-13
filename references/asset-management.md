# Managed asset mutation adapters

`asset_library_management.management_adapter(owner, operation, parameters)` supplies
trusted adapters for the existing managed job journal. It adds no registry or
parallel lifecycle authority. The facade exposes `version`, `replace`, and `repair`
through explicit managed submission; execution is separate from submission.

`version` requests choose an explicit `action`:

- `create`: `manifest`, exact logical-path `sources`, and optional exact `previous`.
- `promote`: exact `reference`, `expected_revision`, optional `consumers` and `reason`.
- `rollback`: `asset_id`, `expected_revision`, optional `consumers` and `reason`.

Native `AssetVersions` validates semantic versions, parent provenance, attribution,
license retention, consumer compatibility and lifecycle revision. Source files are
hash-bound at admission and copied into owned staging before canonical validation.
The admission baseline is captured by the authority; caller-supplied baseline,
source hashes and lifecycle metadata are rejected. The durable journal alone may
replay its internally captured admission record.

`replace` requests supply the complete hash-bound native replacement `plan` and
explicit new consumer `versions` mapping. The existing dependency authority
validates native closures, current pins, compatibility and replacement semantics
inside an isolated canonical staging library. It creates new immutable binding
versions and retains existing versions and project pins. This is the canonical
library binding replacement; activating a changed production project remains the
production adapter's explicit operation.

`repair` requests specify a local exact byte `source`, `digest`, and
`expected_bytes`. Repair targets a damaged *index* blob referenced by an existing
immutable version. The canonical blob remains authoritative; native `recover_blob`
rejects missing identity, damaged canonical evidence, incorrect source bytes and
already intact targets. Recovery checks actual index bytes, rather than trusting
a prior receipt or creating a second version. Unreferenced blobs cannot be created
through this adapter.

Staging uses the canonical `AssetLibrary` to copy the exact admitted baseline in
dependency order. All copies and potential new manifests remain below the existing
job staging limits. Large complete baselines can therefore exceed the conservative
256 MiB staged-byte ceiling and fail with an explicit resource diagnostic. No
sources are deleted or overwritten. Staged registration reuses only an identical
existing immutable digest to resume a process interrupted during staging.

Before final publication, manifests, full source bytes and canonical closure are
verified. Final version publication uses native per-identity compare-and-swap
against the admitted reference set. An unrelated asset addition is allowed; a
competing addition to the same identity fails safely. Replacement publication is
atomic **per consumer version**, as in the native authority, not across all
consumers. Prepared immutable digests are published in dependency order; recovery
recognizes each exact committed version and resumes missing publications. A
conflicting ID/version is retained and reported. No rollback deletes a successful
independent transaction after a later consumer fails.

Lifecycle jobs stage the exact event through `AssetVersions`. A managed token in
the event reason binds the attempted selection to its durable job. Publication
uses the same native append-only stream CAS. After process exit between event
commit and job receipt, recovery compares the exact canonical event at the
expected revision. It never replays an already committed promotion or rollback,
and rejects an intervening different event. Stage, publication and recovery checks
remain cooperative trusted local code, not a sandbox for arbitrary callbacks.

Actual tests use genuine registered OBJ bytes, immutable native versions,
replacement plans, lifecycle events and corrupt index rows. Real child processes
exit immediately after canonical publication and resume through the journal.
Fixtures test operational behavior; no artistic review, production approval,
project activation or remote/provider execution is implied.

## Duplicate an existing registered asset

`assets version duplicate --library LIBRARY --request REQUEST.json --apply`
queues the canonical `AssetVersions.duplicate` operation in the private managed
stage. The request contains an exact `reference`, a complete new `identity`, and
an optional explicit `version` (default `1.0.0`). The browser's scoped version
endpoint accepts `action: duplicate` with the same fields, so no library-private
filesystem paths need to be exposed to HTTP intake.

The canonical lifecycle owner preserves exact payload bytes, source and license
evidence, fixture status and the full dependency closure, and records the old
reference as ancestry. It requires a different identity while preserving kind,
subtype and source attribution; it clears inherited previews and integration
selection. Native identifiers inside source payloads are retained and are not
silently rewritten. Publication uses the same expected-identity CAS and exact
reference crash recovery as managed version creation. Existing assets and
unrelated scene documents remain unchanged.


Private staging interruption is recoverable for both initial version creation
and duplication: the canonical owner prepares the exact duplicate manifest,
and the managed stage reuses a byte-verified identical new reference absent
from the original baseline. Pre-existing baseline identities and conflicting
private candidates still fail; no semantic version or rights check is bypassed.
