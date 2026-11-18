# Native budget reservation before claim

This native API lets an already enrolled Hatch owner reserve its existing
operation before a Director canonical request supplies the reservation identity.
It creates no provider request, native claim, attempt token, generated output or
approval. The canonical acquisition adapter still needs its separate bridge.

Call `hatch.reserve_budget(root, data, job_name)` while holding the existing
`hatch.ownership(root)` lock. Existing enrollment is required; this function never
automatically enrolls a run or changes its ledger. Ordinary admission rejects recovery-only journal authority before any
reconciliation. With current authority, native reconciliation and the same
claim eligibility checks run before reservation. A later `hatch.claim`
rechecks eligibility, remaining capacity and retry state before publishing its
original native claim. A reservation is not a concurrency lease or execution
permission.

`NativeBudget.reserve_only(job, input_hashes)` persists the native intent and
uses the canonical ledger reservation. It preserves native tokens, attempts and
claim history. Identical normal admission reuses one reservation; changed inputs cannot
create replacement work. Settled, released and uncertain reservations cannot
admit fresh work. Existing pending-token recovery stays in `before_claim`.

`NativeBudget.verify_reservation(job, reservation_id, input_hashes=...)` only
reads authenticated state. The job must be the actual object owned by this
loaded native manifest. The verifier checks the original published claim/job bindings, exact operation,
policy, inputs when supplied, original normalized reserve event, receipt identity
and original reserved bound. It can authenticate that retained identity in
explicit recovery mode without consulting current generation inputs. It never calls reserve or reconcile and never repairs an absent
association. It returns valid existing uncertain/final states for authentication
of historical ownership; callers must separately enforce the native state
required by their action.

Both methods return `reservation_id`, `operation_id`, `ledger_revision` (the
current snapshot SHA), `state` and `reserved`. The snapshot is current evidence,
not a portable capability. The original owner/location/ledger and retained
enrollment constraints remain mandatory. Moving or restoring accounting records
does not transfer authority.

The independent admission tests use synthetic native runs and the actual local
ledger. Fault injection covers durable writer boundaries; it does not establish
provider execution. This prepared source has not executed in the connector-only
environment. Required validation is the admission suite, all existing native
budget regressions, compatibility and operations/archive gates at the integrated
head. Existing owner-retention and full-owner rollback limitations still apply.

Reservation and claim share `hatch.generation_inputs`, which hashes a detached
generation view of repaired imported or derived jobs. Reservation keeps the
current provenance and history intact. Provenance markers are removed only when
the native claim transitions, after budget admission returns its token. A prior
import or derivation cannot bind a different input set from the later claim.

Recovery-only authority cannot invoke `reserve_only`, `before_claim` or
`hatch.reserve_budget`, including when an existing reservation or authentic
pending usage is present. Rejection precedes ledger replay and journal writes.
Read-only verification never upgrades that authority. A forged claim/job input
identity is rejected against the original operation even when optional supplied
hashes match the forged record. The original 26 independent admission methods
remain unchanged; two further independently authored methods cover these
recovery boundaries, for 28 source methods. Fresh exact-head hosted execution
is required; source retention does not establish a passing result.
