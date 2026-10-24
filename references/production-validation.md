# Production validation

> Historical report, 19 October 2026. Its missing-public-payload status was
> superseded by the 20 October recovery and verified delivery recorded in
> [DELIVERY_STATUS.md](../DELIVERY_STATUS.md). The original results and limits
> below remain unchanged; they are not a current-head validation run.

Evidence date: **19 October 2026 (UTC)**. The final local regression run completed with **299 tests: 298 passed, one skipped, no failures and no errors**. The supplied Milo/Luma pilot has current, explicitly scoped assistant review receipts. The implementation, its automated checks, production review, editor adapter, independent skill exercise and Git publication have separate evidence and limits.

**Public delivery remains incomplete.** Automatic approval review blocked source PNG and retained TTS WAV publication, textual review-history fragments and a later production-evidence metadata batch. Complete local evidence and saved compiled outputs are available, but the public bundles and evidence are incomplete; no merge or completed Git transport is claimed. See [delivery status](#delivery-status-and-remaining-limits).

## Contents

- [Runtime and regression evidence](#runtime-and-regression-evidence)
- [Skill validation and independent forward task](#skill-validation-and-independent-forward-task)
- [Current production pilot and review scope](#current-production-pilot-and-review-scope)
- [Offline timing and actual browser playback](#offline-timing-and-actual-browser-playback)
- [Editor handoff and application boundary](#editor-handoff-and-application-boundary)
- [Isolated dependency changes](#isolated-dependency-changes)
- [Fresh synthetic regression demonstration](#fresh-synthetic-regression-demonstration)
- [Local production archive and restore](#local-production-archive-and-restore)
- [Delivery status and remaining limits](#delivery-status-and-remaining-limits)
- [Reproduce the checks](#reproduce-the-checks)

## Runtime and regression evidence

| Saved evidence | Observed result | Scope |
| --- | --- | --- |
| [Fresh baseline](../tests/results/production-baseline-123.log) | 123 passed; no skips; 140.326 seconds | Current-environment baseline before the production extension; historical baseline notes do not replace this run. |
| [Final complete regression](../tests/results/production-complete-regression.log) | 299 run; 298 passed; one skipped; zero failures/errors; 160.239 seconds | The complete current suite after binary transport attributes and the actual Git checkout portability regression. |
| [Preserved 298-test checkpoint](../tests/results/production-complete-regression-superseded-298.log) | 298 run; 297 passed; one skipped; 149.929 seconds | The earlier complete run after the preparation-helper collision fix and its eleven tests, preserved byte-for-byte. |
| [Preserved 287-test checkpoint](../tests/results/production-complete-regression-superseded-287.log) | 287 run; 286 passed; one skipped; 150.052 seconds | The earlier complete run after Milo's rear-reference annotation, also retained unchanged. |
| Real browser tests within the final run | 11 passed | One exported-player test, six extended playback tests and four production-preview tests. These are subsets of the 299 tests. |
| Retained production-pilot tests within that final run | 13 passed | Actual retained non-fixture source reconstruction, acting/geometry, provenance, original speech, reuse and deterministic raster checks. These are also a subset of the final total. |
| New production-art preparation tests within that final run | 11 passed | Existing destinations and sibling-input collisions, symlinks, exclusive creation, owned-file cleanup and successful local preparation for both characters without generation. |
| Source transport tests within the final run | 13 passed | Includes the new actual Git staging/checkout case with `core.autocrlf=true` across all three production transport paths. |
| [Installed skill validator](../tests/results/production-skill-validation.log) | `Skill is valid!`; exit code 0 | Basic skill frontmatter, name and description validation. |

The final suite ran from **21:24:29.456211 UTC to 21:27:10.030628 UTC**. Its measured process wall time was 160.574398 seconds; unittest reported 160.239 seconds. The saved log retains exact stdout and stderr separately, the command, environment, versions and source manifests. All **123 test IDs from the baseline** are present and pass in this final run; the separate baseline was not rerun.

The final log is **95,236 bytes**, SHA-256 `f44870e189ce28ce0750bbf99fcd5181609dca58b41dbb7422542d338ebfd0d4`. Its embedded stdout and stderr were independently compared with the exact captured streams: 49 and 50,185 bytes respectively. The superseded 298-test log retains SHA-256 `48875ba393d39205e6b87b7863fa32a6cf32c26f87025d269856cee0754df3a3`, and the superseded 287-test log retains `5e2290ff4333b50ef6d15980b98ee22a5a0e72b2ff0572b2a41a43074d54f572`. The new evidence does not silently replace either checkpoint.

| Final execution dependency | Observed version |
| --- | --- |
| Python | 3.12.14 |
| Pillow | 13.0.0 |
| jsonschema | 4.26.0 |
| Playwright | 1.64.0 |
| Actual Chrome | 155.0.8059.84 |
| FFmpeg / FFprobe | 6.1.1-3ubuntu5 |
| Actual Git | 2.51.1 |

The run used `PYTHONDONTWRITEBYTECODE=1`, the recorded `PLAYWRIGHT_BROWSERS_PATH`, and `HATCH_BROWSER_EXECUTABLE` pointing to the installed local Chrome. Playwright's legacy headless-shell entry resolves to that same executable. Browser tests exercised temporary local profiles; they did not exercise or alter a live user installation.

The sole skip was `test_production_adapter.ProductionAdapterTests.test_actual_local_krita_import_export_and_layer_roundtrip`, with the exact reason **“Krita not available; adapter tests remain separate from integration.”** The separate editor adapter tests passed. No browser or retained-pilot test was skipped.

All **91 selected runtime, test and configuration files** had identical SHA-256 values before and after the final run. The manifest covers Python under `scripts/` and `tests/`, schemas, presets, static HTML/JavaScript, legacy example profiles, test requirements and `.gitattributes`. Generated artwork, production example subdirectories, renders, reviews, documentation and result files are outside that source-stability comparison. Their own evidence uses artifact and dependency hashes.

The preparation helper now refuses existing destinations, sibling profile/identity files and recovery journals before writing. It also rejects linked destinations and input aliases, uses exclusive input creation, and rolls back only files still owned by that invocation after failure. That repair and its new test module were already included in the 298-test checkpoint.

The final packaging revision adds binary attributes for `.bin` parts in the image, audio and review-history transport paths. Relative to the 90-file checkpoint, only `.gitattributes` was added and `tests/test_production_sources.py` changed. Its new case copies the actual repository attributes into a disposable Git repository, stages valid synthetic source bundles at all three exact paths, and forces checkout with `core.autocrlf=true`. It checks exact staged and checked-out bytes/hashes, including whitespace-terminated 65,535-byte LF fragments and CRLF input, then verifies all three complete bundles. An unprotected control must convert to CRLF, proving that conversion is active.

Before the complete run, all thirteen focused source tests passed in 0.314 seconds. A bounded negative control omitted only the attributes copied into that temporary fixture: stage/checkout byte assertions and all three bundle verifications rejected the corrupted transport. The actual repository attributes and original sources were unchanged by that check.

| Selected current source | Identical SHA-256 before and after the final run |
| --- | --- |
| `.gitattributes` | `ba51f7878340a5740fcfbfd10dce7d43ea46ef4d2e2ed106e7cd0af769838826` |
| `tests/test_production_sources.py` | `37feed06c6d5bafbc86e9b95a19f72b417fae80dc15a64ecaa325333ffeeab0e` |
| `scripts/prepare_production_art.py` | `e7ae991f52df86a25b968e07f5c34a03c9b60fd3bc0ab654bfbce33601c17981` |
| `tests/test_production_prepare.py` | `bfe8fc15825d1257b706971516c335bb4bad95dd1f711319801e6030df0300c4` |
| `scripts/production_demo.py` | `93e83b14df812b962d24313e08cb9a8f6f5f1e9c8383c7c5e7cb856e1c5a7d31` |

The renderer identity uses `production_render.py`, `production_performance.py` and `production_contracts.py`, whose combined code SHA-256 remains `7b8b1c68aef4e55ca5941f32ba6c928b599f059b81a0f9b3b9989424f6c53833`. All seven retained complete scene/shot manifests match the current renderer. Their exact manifest hashes and the complete review ledger stayed unchanged throughout the new run; the ledger SHA-256 remains `cce42c52460d4525f18eb00f7525f8df8d99ebda9e31b3d875dccba6d93220d4`. Neither the preparation repair nor the packaging/test revision altered the recorded raster or review identity, and this test run created no new review approval.

Focused development logs and superseded runs remain useful history. They should not be added together to invent a larger test total.

## Skill validation and independent forward task

The installed `quick_validate.py` passed using the Hatch runtime Python. That environment lacked PyYAML, so this single invocation used the already installed primary-runtime **PyYAML 6.0.3** through an explicit `PYTHONPATH`. No package installation or skill change was needed. The [validator log](../tests/results/production-skill-validation.log) records the exact invocation, dependency resolution and unchanged validator/skill hashes. This basic metadata check alone does not validate every workflow or artistic result.

A separate minimal-context forward task followed the installed skill to construct a new model-free two-character demonstration, inspect scoped evidence, export editor assets, archive it and restore it to a fresh directory. The [consolidated forward-task report](../tests/results/production-skill-forward.json) preserves the saved acceptance, performance, restoration and review results, with SHA-256 references to their original records. Consolidating those records did not create any new review decision.

| Forward-task result | Evidence |
| --- | --- |
| Shared cast | `a@1.0.0` and `b@1.0.0`; two scenes preserve the same versions, world positions, uniform scale and prop ownership across the cut. |
| Actual render | 60 frames / 5 seconds, then 36 frames / 3 seconds: **96 frames / 8 seconds**, at 640 × 360 and exactly 12/1 fps. |
| Cache reuse | Verification passes reused all 60 + 36 frames and rendered zero new frames. |
| Contact | At frame 28, both grip anchors and the cup pivot equal `[320, 240]`; measured contact error and ownership attachment jump are zero. |
| PCM masters | Stereo, 48 kHz; exactly 240,000 and 144,000 sample frames; cue intervals `[112000, 124000)` and `[48000, 72000)`; no clipping or nonzero samples outside the cues. |
| Editor adapter | Two complete PNG/WAV handoffs; five plus four OpenRaster snapshots, **nine exact pixel roundtrips**. |
| Archive and restore | **752 project files** restored; four cached shot/scene renders remain reusable; renderer unchanged; no rerender required; recomputed review statuses match the originals. |
| Actual archive inventory | 6,637,679 bytes; 753 ZIP entries consisting of 752 project files plus `archive-receipt.json`. SHA-256: `389ab2fa6d06cc1d480e56340a6a88277da907477980e5ace7671120485defff`. |

The forward task used geometric fixture artwork and deterministic square-wave PCM cues, with **zero model calls and a zero-call project limit**. Its assistant actually inspected the full-size contact frame and chronological all-frame static sheets. That supports the recorded construction, contact, composition, continuity and final raster artifact dimensions. Reusable clip gates and exact timing checks are explicitly synthetic.

Its shot-level **`pose_motion` and `facial_dialogue` remain unreviewed**, so complete eight-dimension shot review is false. No full timed playback, auditory listening, browser behavior, real editor application or native timeline was exercised in that task. `production_art_acceptance` and `human_review` remain false. This is one successful bounded forward exercise, with those limits; it does not establish arbitrary-project success or a broad usability result. Its 752-file archive is separate from any production-pilot delivery archive.

## Current production pilot and review scope

The current non-fixture pilot uses **Milo and Luma at version 3.0.0**, one shared cast across two scenes and five shots. It contains **20 authored clips**. The **13 clips actually used by these shots** have current scoped assistant review; seven unused clips remain unreviewed. The [review recording results](../examples/production-pilot/evidence/review-recording-results.json) list both sets explicitly.

| Scene | Shots | Frames | Duration at 12/1 fps | Exact 48 kHz PCM sample frames |
| --- | --- | ---: | ---: | ---: |
| Seed exchange | `arrival`, `exchange`, `departure` | 432 | 36 seconds | 1,728,000 |
| Garden promise | `garden`, `tomorrow` | 264 | 22 seconds | 1,056,000 |
| Total | Five shots | **696** | **58 seconds** | **2,784,000** |

The [seed render report](../examples/production-pilot/evidence/render-seed-final.json) and [garden render report](../examples/production-pilot/evidence/render-garden-final.json) bind completed output to the current manifests. The [production recipe](production-pilot.md) explains reconstruction from the exact retained artwork and original speech; later deterministic reconstruction does not call imagegen or synthesize replacement speech.

The [final review status](../examples/production-pilot/evidence/reviews-final.json) and exact local review ledger record **20 actual assistant receipts**, all current for their declared scopes. The [review-history transport index](../examples/production-pilot/reviews-source-bundle/sources-index.json) binds that ledger's retained bytes; its public fragments remain incomplete. The [archive and restore evidence](../examples/production-pilot/evidence/archive-restore-final.json) independently verifies exact review preservation.

| Reviewed targets | Receipts | Current dimensions |
| --- | ---: | --- |
| Two characters | 2 | Identity and construction. |
| Thirteen used clips | 13 | Pose/motion, contacts/interactions and timing/AV sync. |
| Five completed shots | 5 | All eight shot dimensions, each bound to the current dependencies and final artifacts. |

The eight shot dimensions are identity/construction, pose/motion, facial/dialogue, contacts/interactions, camera/composition, continuity, timing/AV sync and final artifacts. The named reviewer is `assistant:/root`, **ChatGPT, primary production reviewer**. The status records report `current_pass: true`, scoped `production_art_acceptance: true`, `synthetic_gate_only: false` and **`human_review: false`**. These decisions come from the recorded actual inspections, not from unittest success, source segmentation or numerical geometry alone.

The [final coverage report](../examples/production-pilot/evidence/coverage-final.json) finds **245 requirements and 245 reusable requirements**, with zero missing, incompatible, unreviewed or stale-source items. Coverage is ready, and it requests no new acquisition work. This applies to the declared shots; it does not approve unused clips or every possible action/view combination.

The receipts explicitly limit acceptance to the supplied **frontal cutout pilot**. Fixed scale, authored stepping, restrained body acting and current construction/contact/start-transition repairs were inspected. The handoff uses open paws; articulated finger wrapping is not supplied. Mouth choices are manually editable approximations, and small eye-patch offsets give limited gaze. No phoneme-accurate speech, auditory voice quality or persuasive vocal acting approval is claimed.

Milo's rear design reference is **rejected** because the scarf knot appears on viewer-right in both the front and back panels. It remains retained as provenance and is excluded from the frontal production rig. The original turnaround references do not establish side or rear animation rigs; another viewpoint requires separately authored and reviewed components. These limitations are preserved in the actual character and shot receipts.

## Offline timing and actual browser playback

The deterministic PNG/PCM masters are the timing authority. Both scene reports validate the exact frame count, rational rate, duration, sample count and bound manifest. Review MP4s were also produced with actual codec/stream checks; H.264 flattens alpha, and encoded audio uses the recorded packet-duration tolerance. The exact RGBA PNG and PCM masters retain the authoritative pixels and timing.

The separate [actual browser playback report](../examples/production-pilot/evidence/browser-playback.json) exercised both complete production scenes in **Chrome 155.0.8059.84**:

| Scene | Completed final frame | Final audio clock / duration | Observed wall time | Controls and errors |
| --- | ---: | ---: | ---: | --- |
| Seed exchange | 431 of 432 | 36 / 36 seconds | 36.350 seconds | Scrub, step, pinned comparison and track visibility passed; no reported errors. |
| Garden promise | 263 of 264 | 22 / 22 seconds | 22.468 seconds | The same controls passed; no reported errors. |

Both players stopped at the authored boundary. The garden run stopped with the media element's `audioEnded` flag still false; completion is evidenced by the final painted frame, stopped player and 22-second clock. Sampled observations also show occasional paint lag of up to two frames in the first scene and one in the second. This demonstrates actual clock-driven preview behavior, not a guarantee that a browser paints every frame on an exact deadline.

The browser record explicitly sets **auditory inspection, human review and production-art acceptance to false**. Successful media clocks and exact PCM sample intervals do not establish that anyone listened to the voices. The independent visual and acting decisions are in the scoped review receipts described above.

## Editor handoff and application boundary

The [operational evidence](../examples/production-pilot/evidence/operational-evidence.json) records a direct export of the actual **168-frame exchange shot** at 640 × 360 and 12/1 fps. The handoff retains the complete numbered PNG sequence, exact WAV master, source render manifest and hash inventory. Exchange frames **83, 84 and 85** were exported as layered OpenRaster snapshots; a separate arrival snapshot uses **frame 96**. All **four ORA files** passed exact pixel roundtrip checks.

The operations worker actually inspected the exported exchange frame 84 and arrival frame 96 for character, prop and layout presence. That two-still spot check does not claim complete motion, listening, AV acting, human approval or production-art acceptance. Root's later scoped production reviews have their own evidence and authority.

Krita was unavailable, so real application import/export and native animation-timeline integration are **not performed**. No editor was installed. The PNG/WAV/OpenRaster adapter result remains valid within its tested format scope. Transformed raster layers are editable drawings; the original rig hierarchy, pivots, tracks and timing remain in the production JSON rather than becoming a native editor rig automatically.

## Isolated dependency changes

Three isolated variants exercised change detection against the actual pilot. The [impact summary](../examples/production-pilot/evidence/dependency-impact/summary.json) preserves the baseline and each exact mutation. Original declared inputs and the live workflow ledger were unchanged by this operation; it made no provider call and recorded no production review.

| Isolated change | Affected shots | Unaffected shots |
| --- | --- | --- |
| Move the exchange camera one pixel in X | `seed-exchange/exchange` | The other four shots. |
| Move Milo's shared mouth rest attachment one pixel in X | All five shots | None. |
| Change the authored Milo reaction head key from −4° to −5° | `garden-promise/garden`, `garden-promise/tomorrow` | All three seed-exchange shots. |

Each affected scope correctly requests rerendering and review of changed dimensions. These reports demonstrate dependency selection; they do not themselves approve the altered artwork or silently modify the accepted project.

## Fresh synthetic regression demonstration

The [fresh regression report](../tests/results/production-regression-final-run.json) records a new build in `/tmp/hatch-production-final-regression-wn1w32a4`, using the current renderer. It completed three scene renders in 18.206 seconds and checked their actual offline frames, PCM and review movies. A verification pass reused **all 300 frames**, rendering zero new frames.

| Synthetic scene | Frames | Duration | Verification reuse |
| --- | ---: | ---: | ---: |
| `first-scene` | 12 | 1 second | 12 |
| `second-scene` | 12 | 1 second | 12 |
| `regression-performances` | 276 | 23 seconds | 276 |
| Total | **300** | **25 seconds** | **300** |

Fresh [acceptance](../examples/production-fixture/acceptance.json) and [performance evidence](../examples/production-fixture/performance-evidence.json) were copied into the fixture example; their hashes match the fresh-run report. Coverage is ready. Four planted-stance intervals measure zero drift; pickup and putdown anchors measure zero distance; ownership changes on the declared frames. Sit/stand offsets, independent face/gaze/mouth changes and the exact full-frame override boundaries are also recorded.

All fixture receipts remain explicitly synthetic automated gates. The PCM cues are square waves, not spoken dialogue. `fixture: true`, `human_review: false`, `production_art_acceptance: false` and no external integration remain explicit. Numerical geometry, file integrity and timing do not substitute for anatomy, artistic judgment, listening or human signoff.

The [fixture README](../examples/production-fixture/README.md) links the retained generator and the two small evidence JSON files committed with that example. Editable project definitions, full previews, movies and frame/layer trees must be regenerated in a new output directory; this report does not present absent generated directories as working Git-hosted previews.

## Local production archive and restore

The completed [production archive and fresh-restore report](../examples/production-pilot/evidence/archive-restore-final.json), recorded at **20:36:43.755765 UTC**, verifies the actual non-fixture pilot independently of the smaller skill exercise above.

| Production archive result | Verified value |
| --- | --- |
| Local archive | `hatch-production-pilot-complete.zip`; **285,751,359 bytes**. |
| Exact SHA-256 | `c7ced7e76a08be61abef766f8a1131c75175bb56619676fd02c54bc2889e7e00`. |
| Inventory | **8,811 project files**, plus the archive inventory receipt: **8,812 ZIP entries**. |
| Restored project | Valid; coverage ready; every restored archive file verified against its retained source. |
| Render freshness | All **seven** current scene/shot renders reusable; renderer unchanged; no rerender required or performed. |
| Review preservation | All **20** original assistant review records preserved; exact current status match for all five shots and their eight dimensions. |

The unchanged archive safeguards initially rejected the larger project inventory. A new portable staging copy excludes only **9,685 unreferenced layer-cache PNGs**, verified absent from all seven current complete manifests and every recorded inspection-evidence path. Every referenced artifact and recorded inspection is retained, and the original project files remain unchanged. The successful archive uses the existing limits without weakening them.

Restoration appends the normal `project-restored` history event while preserving every prior review and history entry. It does not create a new artistic decision, human approval or external-application integration. This is a completed local archive and restore; it does not resolve the separate public source-upload block.

The later [review-transport reconciliation](../examples/production-pilot/evidence/review-transport-reconciliation.json) found 18 obsolete gzip transport fragments alongside the 64 current UTF-8 parts. The exact-inventory guard correctly rejected the extra files. Their bytes matched the independently preserved superseded bundle; only those extras were moved outside the active, archive-staging and restored project directories. All three current review bundles then verified, and an additional fresh-directory transport restore reproduced the exact 4,167,150-byte original ledger. Each project remained valid and its ledger and seven render manifests stayed unchanged. The original ZIP above is a historical snapshot and still contains the obsolete fragments; the reconciliation report gives their exact hashes and preservation procedure. This artifact repair occurred after the earlier 298-test regression and before the final 299-test run; it changed no implementation, review decision or render.

## Delivery status and remaining limits

| Area | Current evidence and status |
| --- | --- |
| Local implementation and regression | Complete for the recorded environment and source hashes; one declared optional Krita skip. |
| Current frontal pilot review | Twenty current assistant receipts covering two characters, thirteen used clips and five shots; explicit visual/acting limits remain. |
| Human and auditory approval | Not performed or claimed. |
| External editor and native timeline | Not performed; Krita unavailable. |
| Original artwork and speech provenance | Retained locally with exact source/receipt hashes; reconstruction and transport guards are tested. |
| Local production archive and restore | Complete: 8,811 project files, all seven renders reusable and all 20 review records preserved. |
| Saved compiled outputs | Seven output packages successfully saved for the user, with exact byte/hash inventories and metadata applied. |
| Public source-bundle delivery | Incomplete. Automatic review rejected a source PNG upload at part 114 and a retained TTS WAV upload at part 13. |
| Public review and production-evidence metadata | Incomplete. Two UTF-8 review-history fragments and a later production-evidence metadata batch were rejected; further publication of that material stopped. |
| Pull request and merge | [PR #4](https://github.com/CompleteDotTech/hatch-pet/pull/4) is open, non-draft and mechanically mergeable at public commit `7e6dcf0beba172254bcba7558215ca8ba2a3cfe5`; publication is blocked, so it remains unmerged. |

The [saved output inventory](../examples/production-pilot/evidence/render-delivery.json) records three review movies and four compiled output ZIPs: the two production scenes, their masters, the editor handoff and the synthetic regression. The ZIP inventories and file hashes were verified. These outputs exclude the original acquisition sources and source code, and their successful delivery does not publish the missing original source/audio or review-history fragments to GitHub.

The [delivery block record](../examples/production-pilot/evidence/delivery-blockers.json) preserves the automatic rejection evidence. Automatic review did not accept the available authorization evidence for publishing those specific source payloads to `CompleteDotTech/hatch-pet`. The TTS rejection identified external audio/voice-data publication as the risk. The textual review fragments were also rejected despite a separate proof of two UTF-8-only tree entries containing no WAV payload. A later plain-JSON production-evidence batch was rejected as sensitive derived-source disclosure. Further production-media and evidence-metadata publication stopped; the blocked content was not sent through another route.

This local report links actual retained evidence; those links are not a claim that every target has been published. Incomplete public bundles must not be described as self-contained or fully restorable from Git. Continuation requires explicit payload-publication approval accepted by automatic review.

The final delivery step must record the public source/evidence inventory and the pull request after those actions actually succeed. The 752-file archive belongs only to the independent synthetic forward task; the 8,811-file production archive has its own successful restore evidence. Neither local archive nor the seven saved compiled outputs establishes complete Git transport.

## Reproduce the checks

From the repository root, use the documented dependencies and configure an available local browser as described in [rendering and preview](production-rendering.md). The exact full test selection is:

```sh
python -m unittest discover -s tests -v
```

Generate a new model-free regression project and all of its inspectable renders with:

```sh
python scripts/production_regression_demo.py --output-dir output/production-regression-review --movie
```

Choose a new output directory. The generator writes editable sources, evidence, numbered PNG/PCM masters, local previews, storyboards and review movies under that destination. Open those regenerated local files with their adjacent assets retained. The [production recipe](production-pilot.md) covers rebuilding the non-fixture pilot from complete retained source bundles; that workflow requires the missing public bundle parts or the complete local delivery sources.
