# Contributing and repository triage

Creative Studio is the repository name. `hatch-pet` remains the compatible skill
name and installation directory. Preserve existing atlas, profile, source,
review, archive and CLI contracts when adding functionality.

## Before implementation

Find the existing issue and inspect its native parent, sub-issues and blocking
relationships. Read the complete parent requirements and any continuation
comments. Claim a bounded scope in the existing issue or PR; avoid duplicate
controllers, registries, approval stores and parallel implementations of the
same shared interface. Do not infer dependency order from issue numbers.

Existing acquisition claims, immutable source provenance, review records,
exclusive publication and recovery mechanisms remain authoritative. New 3D
interfaces must explicitly convert coordinate and time domains rather than
reinterpret the fixed 2D contracts. General 3D programs remain separate from
the implemented rigging and 2D production capabilities.

## Evidence and review

Describe the exact source revision, commands, environment, results and skips.
Keep implemented, tested, independently verified, visually reviewed and
production-approved as distinct claims. A merged PR, schema-valid document,
fixture, generated file or successful unit test does not establish rendering,
solver behavior, external interoperability or artistic acceptance.

Keep historical logs and receipts bound to their original revisions. New
evidence supplements them; it does not rewrite an earlier failure or approval.
The existing workflow files define independent 2D, rig/native and Model3D gates.
Preserve optional dependency isolation and all applicable integration gates.
Use PRs for substantial documentation or implementation changes. Check the
current head, discussions and CI again before deciding a PR is ready; a review
or green run on an older head does not qualify a changed head.

Do not merge as part of a triage-only task. Do not force-push shared branches,
delete preservation branches or remove original artifacts to tidy the backlog.

## Labels

Label names use lowercase `dimension:value` syntax. Descriptions start with the
dimension and state an observable meaning. The repository taxonomy has these
independent dimensions:

| Dimension | Policy | Color |
| --- | --- | --- |
| `type:*` | One primary intent: bug, feature, documentation, test, maintenance, security or research | `1d76db` |
| `area:*` | One primary subsystem; add another only for a real interface or independently changed component | `5319e7` |
| `priority:*` | Exactly one for active issues; historical priorities only when justified by evidence | Severity colors below |
| `status:*` | At most one actionable workflow condition on open work; never copy native open/closed/merged state | `f9d0c4` |
| `scope:*` | Optional `program` or `epic`, mutually exclusive, based on native hierarchy | `bfdadc` |

Areas are `2d`, `3d-model`, `rigging`, `animation`, `scene`, `camera`, `lookdev`,
`rendering`, `editor`, `director`, `physics`, `continuity`, `asset-library`,
`delivery`, `infrastructure` and `documentation`. Keep new areas tied to real
architecture rather than individual files or speculative products.

| Priority | Meaning | Color |
| --- | --- | --- |
| `priority:p0` | Immediate security, data loss or critical release blocker | `b60205` |
| `priority:p1` | Major functional blocker or shared prerequisite | `d93f0b` |
| `priority:p2` | Important planned engineering or acceptance work | `fbca04` |
| `priority:p3` | Lower-impact enhancement or maintenance | `c2e0c6` |
| `priority:p4` | Speculative or explicitly deferred work | `e4e669` |

Workflow labels are `needs-triage`, `ready`, `blocked`, `needs-evidence`,
`needs-design`, `needs-review` and `deferred`. `ready` means the bounded issue
has no known unsatisfied prerequisite; it never grants merge approval.
`blocked` needs a concrete prerequisite or blocker, preferably a native edge.
Completed historical items receive no modern workflow label. Conventional
`accessibility`, `good first issue` and `help wanted` remain optional signals;
assign them only when their meaning has been checked.

Maintainers own taxonomy changes. Recheck live labels and item contents before
mutation to preserve concurrent edits. Audit usage before removal; migrate
`bug` to `type:bug`, `documentation` to `type:documentation`, and `enhancement`
to the evidenced primary type. An unused ambiguous `duplicate`, `invalid`,
`question` or `wontfix` label can be retired, but retain the actual disposition
and authoritative issue link in history. Native relationships remain the source
of truth; do not build a disconnected second roadmap or assign every large
feature the highest priority.

## Security and release governance

Do not post secrets or sensitive exploit details in public issues. Use the
repository's private vulnerability reporting control if enabled, otherwise
establish a private maintainer channel before transmitting sensitive material.
No dedicated security inbox is promised here.

Branch rules, CODEOWNERS, supported Python versions and release approvals must
be established by maintainers around checks that actually run. The absence of
branch protection is not permission to bypass review. Preserve Apache-2.0,
upstream OpenAI skill attribution and separately recorded third-party asset,
speech and dependency licenses. Repository licensing does not silently
relicense imported artwork or third-party components.
