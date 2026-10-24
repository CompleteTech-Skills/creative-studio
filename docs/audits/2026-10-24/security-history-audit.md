# Security, dependencies, and historical-fix audit

Scope: read-only source review of runtime bb5a681 under docs HEAD 768c6e2 on 2026-10-24. No repository files, environments, GitHub state, or running services were changed. No broad tests were rerun. Existing `main-contract-regressions.log` is independent parent-run evidence (117 run, 114 pass, 3 skip); this review does not convert it into a full security assessment.

## Actionable dependency finding

`requirements-rig-test.txt` pins Pillow 12.3.0, while `requirements-delivery.txt` pins Pillow 13.0.0. A live OSV query of nine exact name/version pairs returned 41 advisory IDs for Pillow 12.3.0 and zero for the other eight pairs. Full raw request/result: `dependency-osv-audit.json`. This is direct-package database matching, not a complete resolved dependency scan or demonstrated exploitation of this repository.

Representative upstream advisory [GHSA-22hg-r758-rrw9](https://github.com/python-pillow/Pillow/security/advisories/GHSA-22hg-r758-rrw9) describes an infinite loop reading `n_frames` from crafted MIC content when optional olefile is present, affecting >=7.2.0,<13.0.0. Full record: `dependency-osv-example.json`. Its optional-package prerequisite was not checked against a user environment. Aligning the rig-test Pillow pin with the already recorded 13.0.0 delivery version and rerunning affected compatibility tests is warranted. The 41 database matches are not 41 proven reachable vulnerabilities.

`requirements-test.txt` specifies only lower bounds (Pillow>=10.1, jsonschema>=4, playwright>=1.40); the direct pins in the other files do not lock all transitives or hashes. A fresh resolved dependency lock and advisory scan would make reproduction and exposure determinate. No current installed-package inventory was exported.

## Controls verified in source

- Archives: decomposition_package has portable member-name validation, duplicate/case collision rejection, file/count/expanded-size budgets, regular-file restrictions, bounded receipt parsing and hash inventory verification. rig_package streams extraction into a temporary directory, validates sizes and hashes, reloads the rig, then publishes with exclusive rename. portable_runs verifies member paths, duplicate/count/expanded-size bounds and receipt membership/hashes. These are different implementations, with different budgets; a single common hardened parser cannot be assumed.
- Paths: rig_errors.safe_path enforces containment and rejects symlink components, with portable names shared from decomposition_package. production_contracts rejects traversed symlinks. Source checks are valuable but do not establish immunity to concurrent hostile filesystem replacement between validation and use.
- JSON: shared rig decoder caps 32 MiB, UTF-8 encoded string bytes, nesting to 64, rejects duplicate keys/nonfinite values, and normalizes parser/integer conversion failures. Other scripts still use ordinary json.loads; the shared decoder's guarantees do not apply repository-wide.
- Subprocesses: rig_blender invokes a fixed in-repository worker with argument lists, factory startup, disabled auto-execution, timeout and exit/report checks. production render/probe also have deadlines. There is no general OS sandbox or memory cap implied by these checks.
- HTTP: rig_http binds loopback, uses random bearer tokens and constant-time comparison, rejects foreign Origin values, caps JSON bodies to 4 MiB, applies 10-second connection deadlines, limits dispatch concurrency, and routes paths through safe_path.
- Raster: asset_contracts caps source bytes/pixels, frames and estimated raster working set; decomposition adds component/mask/run budgets.

## Remaining demonstrated hardening limits

These are source-level limitations, not confirmed remote exploits:

1. `scripts/hatch.py:96-100` executes child Python helpers using capture_output without timeout. A stalled helper can stall its caller indefinitely, and captured output is not bounded while executing. The last-4000-character error truncation happens after collection.
2. `rig_http.RigServer` subclasses ThreadingHTTPServer. Its worker semaphore is acquired inside `_run`, after a request thread exists and headers have been read; it does not globally cap accepted connections or pre-authentication threads. A local process can therefore consume thread resources independently of the dispatch worker count. Loopback and socket deadlines limit, but do not remove, this concern.
3. `portable_runs.verify_archive` hashes each decompressed member via `archive.read(name)`, materializing it. A declared 1 GiB archive budget can still allow a very large single-member allocation. rig_package uses streaming instead. Current size limits bound this behavior; they do not make it small-memory.
4. Publication protection covers directories in historical fixes, but `portable_runs.archive_run` still checks that an archive destination does not exist and later publishes the archive file with `os.replace`. A file created concurrently at that destination can be replaced. This is narrower than the repaired restoration/bundle directory race and should not be described as a regression of those exact fixes without an explicit broader no-clobber contract.

## Historical fixes retained

| PR | Current-source verification | Scope of conclusion |
|---|---|---|
| #194 | production_performance owners compare instance identity when a character repeats across endpoints (lines 306-325) | Corrected repeated-actor ownership distinction remains present. |
| #198 | atomic_publish uses Windows exclusive rename, Linux RENAME_NOREPLACE, macOS RENAME_EXCL, and fails closed if unavailable; portable_runs, rig_package and production_sources call it | Repaired directory-publication race protection remains present. |
| #199 | production_bundle.create_bundle obtains new destination and uses production_sources publication helpers | Bundle directory publication still routes through exclusive helper. |
| #200 | rig contract schemas, controller/export validation code and focused tests are present | Contract validation implementation retained; no fresh Blender/browser interop claim. |
| #203 | rig_corrections validates skeleton/controllers, attachment and eye references before correction persistence (lines 140-148) | Corrected-IK admission precedes writes. |
| #204 | rig_import separately transforms normal covectors and tangent directions and scales direction morphs against corresponding base lengths | Mathematical fix retained; no new visual equivalence claim. |
| #205 | rig_errors.decode_json measures UTF-8 string bytes in chunks and catches ValueError/RecursionError into RigError | Integer conversion and Unicode byte-admission fix retained. |

Source comparison found no removal of these specific repairs. It is not a proof that all paths or adjacent feature branches are regression-free.

## Third-party license and version inventory

Root project: Apache-2.0 (`LICENSE.txt`). Vendored three.js is declared 0.180.0 by assets/rig-viewer/vendor/manifest.json with per-file SHA-256 values and an included MIT license; example review-ui vendor directories also include licenses. This audit did not independently download and byte-compare every vendor asset.

Primary PyPI release metadata was fetched only for declared exact direct dependencies; retained in `dependency-metadata-audit.json` with requires_dist:

- Pillow 13.0.0 / 12.3.0: MIT-CMU.
- jsonschema 4.26.0 / 4.25.1: MIT.
- playwright 1.64.0: Apache-2.0; 1.55.0 older metadata lacks a license expression (see recorded license text field).
- numpy 2.3.5: BSD classifier and bundled-license text in metadata; binary distributions may carry additional component notices.
- pygltflib 1.16.5: MIT classifier.

These permissive declarations do not complete distribution-license obligations for bundled browsers, Blender, FFmpeg, transitive dependencies, binary wheel components, or generated/imported art. Upstream release endpoints follow `https://pypi.org/pypi/{name}/{version}/json`; only public package metadata was transmitted. No secret scan, installed-environment audit, browser binary advisory scan, or OS-level sandbox test was performed.

## Expanded Actions governance review: 10 registered workflows

All ten saved workflow sources were read in full, including all seven absent from main. Structured per-workflow evidence (permissions, triggers, source digest, observed last actor/head/result, purpose, recommendation) is in `workflow-audit.json`. Inputs were `audit/workflow-inventory.json` and all `raw/workflow-ID.yml` files. Active registration is distinct from current-main presence and does not by itself demonstrate that a workflow is currently dispatchable. No jobs were run, rerun, disabled or edited.

| ID | Workflow | Current classification | Permission / trigger |
|---|---|---|---|
| 390391411 | Public production delivery | Main CI | contents:read; PR, manual, push main/fix-public-delivery branch |
| 390468376 | 3D rigging and compatibility | Main CI | contents:read; push, PR, manual |
| 394356214 | Model3D contracts | Main CI | contents:read; push, PR, manual |
| 394264892 | Roadmap source/dependency capture | Historical operational branch | contents/issues/pull-requests:read; push named roadmap branch, manual |
| 394275719 | Roadmap interchange test tools | Historical operational branch | contents:read; named branch + workflow-path push filter |
| 394350696 | Model3D source transfer | Historical operational branch | contents:write; named branch + workflow/transport path push filter |
| 394370612 | Model3D fixture correction transfer | Historical operational branch | contents:write; named branch + workflow-path push filter |
| 394394294 | Model3D stat correction transfer | Historical operational branch | contents:write; named branch + workflow-path push filter |
| 394850441 | Strict JPEG decoder admission | Active unmerged feature CI | contents:read; filtered PR/main push, manual |
| 394853102 | Trusted physics backend lifecycle | Active unmerged feature CI | contents:read; PR/main push, manual |

The three write-permission transfer workflows bind source payloads/transformations to exact hashes, verify expected source and target Git object identity, and POST only blobs/trees. Reviewed code does not create commits, update refs, merge PRs, or execute the transferred implementation. Tokens are passed to fixed GitHub API endpoints; checkout, where used, disables credential persistence. This is deliberately narrow implementation behavior despite the broader contents:write token capability. If a writer changes a workflow, that content is a different security boundary; existing source checks are not immutable authorization policy.

All three transfer workflows hardcode `CompleteTech-Skills/hatch-pet` and reject redirects before following them. Repository rename can therefore make a historical rerun fail at the old endpoint. Their latest recorded outcomes were initial source transfer failure, fixture transfer success, and stat transfer success; these observations are historical, not a reason to rerun. Preserve original hashes, branches, artifact receipts and failure history. The fixture workflow's saved source name says "snapshot correction", while registered metadata says "fixture correction"; both are recorded rather than silently treated as different workflows.

Roadmap capture also hardcodes the old repository name, but uses standard urllib redirect handling then validates the final URL against that old base. Consequently a redirect to the renamed canonical repository causes validation failure after the redirected request, rather than being forbidden before following. It does not write GitHub state. It archives full fetched Git history with `git bundle --all` and captures issue comments/relationships. A capture replacement should explicitly approve its history/data scope and use current repository identity; do not broadly remove redirect/origin checks to restore a green run. Artifacts have seven-day retention in the five operational workflows, so preserve downloaded evidence independently of future cleanup.

Interchange tools pins pygltflib itself and downloads binary wheels, but resolved transitive wheel versions remain time-dependent; emitted SHA256SUMS are a receipt for that run, not a pre-download lock. The strict-JPEG and physics workflows use commit-pinned Actions, disabled checkout credential persistence and explicit deadlines. Physics verifies a fixed Blender4.5.3 checksum before extraction, isolates the PyBullet interpreter, retains actual probe/cache evidence and cancels superseded runs. These properties do not establish code-level physics correctness or complete supply-chain closure.

Governance recommendation: assign an accountable maintainer (latest actor CompleteDotTech is an observed initiator, not proof of permanent ownership); preserve historical transfer/capture evidence; then disable the five completed operational registrations if their owner confirms completion. Do not delete branches or rewrite receipts as part of disabling. Keep the two feature workflows attached to their respective PR review and merge disposition. Keep the three main gates; additionally pin tag-based Actions to immutable commits, explicitly disable credential persistence in rig-3d checkouts and add practical job deadlines there. No `pull_request_target`, scheduled trigger, self-hosted runner or secret-reference interpolation appears in the ten reviewed source files.

Additional dependency coverage found through the workflow review: `tools/rig/package-lock.json` pins gltf-validator2.0.0-dev.3.10 (Apache-2.0) and three0.180.0 (MIT), with registry integrity values. Main rig CI uses `npm ci --ignore-scripts`. The extra exact gltf-validator OSV query returned no match (`dependency-osv-gltf.json`). This supplements, rather than replaces, the Python/direct-package limitations above.

## Full history secret-scan triage

Parent-run Gitleaks8.30.1 inspected all refs with `--log-opts=--all --redact`, reporting175commits,40.73MB and111 generic-api-key findings. Every finding was individually resolved to its exact commit, path and line, with candidate values inspected only in memory. `audit/gitleaks-triage.json` retains111 classifications and their reasons without matched values. No candidate was submitted to any service or tested as a credential.

Results:111 false positives for the generic external-API-key rule;0 potential secrets remained among these111 findings:

-58 SHA-256 evidence digests.55 match the identified file bytes in the same commit. One Windows evidence digest additionally matches CRLF-normalized fixture bytes. One explicitly named API-probe-script SHA-256 is an evidence field without an accompanying exact script path. One historical rig-release source digest was not reproduced from available source revisions; its field and surrounding source-inventory structure identify it as recorded source-integrity evidence, not an authentication value. This does not validate that historical digest's correctness.
-52 historical local generation claim IDs. Each candidate independently matches a claim event in the same commit's canonical Milo/Luma imagegen-jobs record; every corresponding job has status `generated` and no matching active token. The implementation generates these local IDs with uuid.uuid4().hex, uses them to reconcile a local job, and clears the active token when generation completes. They are historical local workflow identifiers, not external service API keys. An active local claim identifier should still retain its coordination semantics; this finding does not authorize publishing live claims.
-One Python parameter expression in decomposition_cli.py, containing chroma-key argument references and a numeric default, with no string literal or credential assignment.

These results classify the scanner's111 reported matches, not every possible secret in repository history. Detector blind spots, inaccessible/deleted refs and material outside the scanned Git history remain outside this claim. The raw scan remains redacted and no source history was altered.
