# Governance activation record

`main-protection.json` is the proposed REST branch-protection payload. It is not
evidence that protection is enabled. Its 15 exact contexts were observed passing
on main `d2744d91082f7ce7fe54f2bb13c5985825adadc2`; GitHub Actions app ID 15368
must also match the activation-time check-run readback. Recheck current workflows
and passing contexts before applying it. Strict status checking deliberately
requires an updated target branch before merge.

At preparation time, the collaborator API returned only `CompleteDotTech` with
admin access. The main protection endpoint returned 404 “Branch not protected”,
rulesets returned `[]`, private vulnerability reporting returned false, and
vulnerability alerts returned disabled. Secret scanning and push protection were
disabled. These observations are an initial baseline, not a final status.

Desired settings are: private vulnerability reporting enabled, vulnerability
alerts enabled, secret scanning enabled, and secret scanning push protection
enabled. Automated dependency-fix PRs are not required by this policy. Read back
each setting after mutation and retain errors without claiming success.

The five historical branch-only workflow records have individual source/run inventories in
[historical-workflows.json](historical-workflows.json):
`model3d-source-transfer.yml`, `model3d-stat-transfer.yml`,
`model3d-test-fixture-transfer.yml`, `roadmap-capture.yml`, and
`roadmap-test-tools.yml`. Exact source bytes were retained locally and hashed from the listed immutable Git
commits. All five latest runs completed on 23 October 2026; no active runs were
found during this audit. They are not main release checks. Their task-specific
transfer/capture work is superseded by normal Git publication and fresh live
inventory. The proposed disposition is to disable these five registrations after
review while retaining source branches, Git history and any available artifacts.
Disabling has not yet occurred. Expired historical artifacts must not be claimed
as newly verified evidence.
This PR does not claim that reconciliation or live settings activation occurred.

Before closing #216, append or link immutable evidence for current-head CI,
independent policy review, final settings readback, a rejected direct update (or
equivalent rule evaluation), and the five historical workflow dispositions.
The review and release requirements are in [the release policy](../release-policy.md).
