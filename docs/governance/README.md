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

The five historical branch-only workflow records require individual disposition:
`model3d-source-transfer.yml`, `model3d-stat-transfer.yml`,
`model3d-test-fixture-transfer.yml`, `roadmap-capture.yml`, and
`roadmap-test-tools.yml`. Preserve their source bytes, run IDs and artifacts and
check current owner use before disabling them. They are not main release checks.
This PR does not claim that reconciliation or live settings activation occurred.

Before closing #216, append or link immutable evidence for current-head CI,
independent policy review, final settings readback, a rejected direct update (or
equivalent rule evaluation), and the five historical workflow dispositions.
The review and release requirements are in [the release policy](../release-policy.md).
