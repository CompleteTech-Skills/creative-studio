# Security reporting

Report sensitive vulnerabilities through the Security tab's **Report a vulnerability**
control when available. Do not put credentials, exploit payloads, or private source
assets in public issues. If that control is unavailable, do not assume this repository
has a monitored private inbox; contact the repository administrator through an
established private channel first. No email address or response-time guarantee is
promised here.

The verified repository administrator is `CompleteDotTech`. Dependency alerts,
private reporting and secret scanning are separate GitHub settings. Their current
state is established by settings readback, not by this document.

Security fixes use the same independent review, current-head regression checks,
source preservation and release gates as other changes. Preserve the original
report privately and publish only a sanitized advisory when appropriate. Imported
models, archives, images and local HTTP requests remain untrusted inputs.
