# Review and release policy

## Supported verification environment

Required CI uses Python 3.12. Public 2D delivery runs on GitHub-hosted Linux,
Windows and macOS with `requirements-delivery.txt`, including its pinned raster
and Playwright versions. Optional native 3D tests use `requirements-rig-test.txt`
in a separate environment. Never merge those dependency environments merely to
make a gate pass. The pinned native Playwright browser job uses Ubuntu 24.04;
Blender is the checksum-verified 4.5.3 LTS CPU build, and Khronos tooling is
installed from the committed Node lockfile with Node 22.16.0. Windows and macOS
support is verified by the delivery and Model3D matrices; a Linux native gate
does not establish native backend support on other systems.

All required checks must pass on the candidate's current head. Retain actual
render/decode, browser, Blender reopen, Khronos and 2D isolation evidence. A skip
or unavailable required backend is not a pass. Pin external actions by commit,
disable persisted checkout credentials, and retain read-only job permissions.
Upgrades require their own validated PR; tags in comments are explanatory only.

## Maintainers and independent review

`CompleteDotTech` is the administrator returned by the collaborator API and the
CODEOWNERS routing destination. No second human or team has been invented.
An author cannot approve their own PR through this account. A fresh reviewer must
inspect the complete current candidate, report findings with locations and
verification evidence, and re-review after substantive changes. Agent reviews
must identify their independence and reviewed SHA in the PR conversation; do not
represent them as a second GitHub identity or fabricate an approval.

The proposed main protection requires PRs, resolved review conversations and
all 15 existing always-running check contexts, including administrators. It
forbids force pushes and deletion. It does not require a GitHub approval count
while the only verified collaborator is the shared administrator: setting one
would deadlock author-owned PRs or encourage false approval. Independent review
remains a mandatory release/merge procedure, and this limitation is explicit.
Path-filtered Strict JPEG checks remain mandatory when triggered, but are not
configured as an always-required context that would block unrelated changes.
Apply protections only after current required jobs and the policy have been
reviewed; retain before/after settings and rejected-update evidence.

## Tags, releases and rollback

There is no automatic release on a successful merge. Create an annotated version
tag only for a main commit whose complete acceptance scope has been verified.
Use `vMAJOR.MINOR.PATCH`: incompatible documented contracts require a major
version; additive supported behavior a minor version; compatible repairs a patch.
The first release remains 0.x until its declared supported scope is complete.
Do not label unfinished parent programs production-ready.

A release must link the exact main SHA, independent review, current-head checks,
commands, dependency/tool versions, artifact hashes and provenance. Separate
numerical/structural results, independently decoded/rendered outputs, visual
inspection and production-art approval. List optional/provider-dependent and
unsupported integrations. Never reclassify fixtures or muted audio as production
artwork or a zero-audio-stream movie. Preserve the legacy audio-capable films.

Tags and published artifacts are immutable. Correct a bad release by withdrawing
its recommendation and issuing a new version, preserving its original evidence.
Rollback source changes with a reviewed revert PR and rerun affected gates;
restore only verified archives into new exclusive destinations. Do not reset
main, overwrite unrelated data, replace original sources or rewrite historical
review records. A rollback does not automatically reinstate stale approvals.
