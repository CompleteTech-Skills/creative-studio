# Production operations

The production layer stores a persistent cast, versioned clips and scene documents beside source images, audio, acquisition history and review receipts. The legacy pet controller remains the authority for imagegen claims and ingestion. Planning, rendering, import, review, archive and transport commands do not call an image provider.

## Contents

- [Commands and JSON reports](#commands-and-json-reports)
- [Create and inspect a development fixture](#create-and-inspect-a-development-fixture)
- [Plan coverage before generation](#plan-coverage-before-generation)
- [Selective dependencies and local repair](#selective-dependencies-and-local-repair)
- [Record explicit review evidence](#record-explicit-review-evidence)
- [Import with immutable construction and provenance](#import-with-immutable-construction-and-provenance)
- [Bound acquisition, claims and recovery](#bound-acquisition-claims-and-recovery)
- [Render recovery and editor handoff](#render-recovery-and-editor-handoff)
- [Complete archive, safe restore and migration](#complete-archive-safe-restore-and-migration)
- [Complete project transport in ZIP parts](#complete-project-transport-in-zip-parts)
- [Small source and artifact transfer](#small-source-and-artifact-transfer)
- [Focused verification](#focused-verification)

## Commands and JSON reports

Use `python scripts/production.py --help` and append `--help` to any command for its arguments. `--project` accepts a directory containing `project.json` or that file. Source references are canonical relative paths; absolute paths, traversal, aliases and symlink escapes are rejected.

Without `--output`, a command prints its full JSON report. With `--output NEW.json`, the **full report is written to that file**, and standard output contains only:

~~~json
{"output": "/absolute/path/NEW.json"}
~~~

Open the written file to inspect the report. Existing output receipts are preserved: an occupied output path fails before a claim or other command action runs. Render progress is written to standard error. Invalid arguments and contract errors return exit status 2.

| Command | Main inputs | Result |
| --- | --- | --- |
| `create` | `--destination`, exactly one of `--fixture` or `--from-project` | New fixture or verified portable copy |
| `validate` | `--project`, optional `--diagnostic` | Contract, resource and source inventory report |
| `coverage` | `--project`, optional `--scene` | Reusable, missing, incompatible and unreviewed requirements |
| `snapshot` | `--project`, optional `--scene` | Stable dependency map per shot |
| `impact` | `--project`, `--receipt` previous snapshot | Affected shots, changed dependencies and next actions |
| `review-template` | Target flags, optional repeated `--dimension`, `--render-manifest` | Unreviewed receipt with exact dependency hashes |
| `review` | `--project`, `--receipt` | Append an explicit review attestation |
| `reviews` | `--project` and target flags | Current, stale, failed or missing review dimensions |
| `render` | `--project`, `--scene` or `--shot`, `--destination` | Deterministic frames, layers, audio and preview |
| `storyboard` | `--render-dir`, `--destination`, optional `--frames` | Selected-frame contact sheet |
| `export-editor` | `--render-dir`, `--destination`, optional `--frames` | Layered editor handoff and adapter verification |
| `archive` | `--project`, new `--destination` ZIP | Complete verified portable project |
| `restore` | `--source` ZIP, new `--destination` | Staged restore and actual render freshness report |
| `migrate` | `--project` | Explicit workflow migration with original backup |
| `bind-generation` | `--project`, `--binding`, `--run-dir`, `--requirements` | Immutable binding to an existing hatch acquisition |
| `generation-status` | `--project` | Claims, budgets, failures, model metadata and reconciliation |
| `claim` | `--project`, `--binding`, `--job` | Native hatch imagegen handoff and exact token |
| `fail` | Binding/job, `--token`, `--note` | Record the outcome of an active claim |
| `ingest` | Binding/job, `--token`, `--source`, `--receipt`, `--note` | Native hatch ingestion with bound coverage evidence |
| `repair` | Binding/job, `--note` | Explicit retry repair retaining attempt history |
| `limits` | Optional `--max-calls`, `--max-attempts`, `--concurrency` | Bounded project acquisition configuration |
| `import-component` | Character/version/component, `--new-version`, `--receipt` | New immutable character version from an external source |
| `adopt-generated-component` | Import target plus binding/job | New version from an existing generated job |

## Create and inspect a development fixture

~~~sh
python scripts/production.py create --destination /tmp/hatch-fixture --fixture --id example-fixture
python scripts/production.py validate --project /tmp/hatch-fixture
python scripts/production.py coverage --project /tmp/hatch-fixture --output /tmp/coverage.json
python scripts/production.py render --project /tmp/hatch-fixture --scene first-scene --destination /tmp/hatch-fixture/renders/first-scene
python scripts/production.py render --project /tmp/hatch-fixture --scene second-scene --destination /tmp/hatch-fixture/renders/second-scene
python scripts/production.py storyboard --render-dir /tmp/hatch-fixture/renders/first-scene --destination /tmp/first-scene.png --frames 0,5,11
~~~

The starter contains two explicit synthetic geometric characters and two scenes using the same character and clip versions. Its default generation-call limit is zero. Synthetic fixtures exercise software contracts; their view metadata and automated test receipts do not establish production artwork acceptance.

`create --from-project ORIGINAL --destination NEW` uses complete verified archive/restore and preserves project and cast identities. It rejects `--id` for a portable copy. The destination must be outside the source project and must not already exist.

For production, retain original imagegen or imported sources and their provenance, author character construction and independent tracks, then inspect the actual result. Do not relabel a fixture as production.

## Plan coverage before generation

Coverage uses independent dimensions: action, pose, viewpoint, facing, expression, intensity, gaze, mouth, hand, costume, prop state and contact. Clip requirements, explicit instance requirements, dialogue mouth cues and concrete instance bindings contribute to the plan.

- `reusable` requirements have declared capabilities and current asset reviews.
- `missing` requirements need undeclared capabilities or exact source-file recovery.
- `incompatible` requirements violate the structural contract or declared combinations that overlap in time.
- `unreviewed` requirements have available construction/clips whose reviews are absent, failed or stale.

A declared capability is a planning claim. It does not automatically prove convincing acting. Current construction review and the reusable clip's motion, contact and timing review are required before coverage is ready. Nonfixture asset reviews must be actual human or assistant inspections. **Coverage readiness is separate from final shot acceptance.**

Missing work is deduplicated by character version, dimension and value. The report separates raster asset requests, motion/track authoring and exact source recovery. It estimates minimum calls if every missing raster requires generation, remaining recorded calls and bounded retries. It does not generate a Cartesian product of every performance dimension.

Missing images, audio and provenance receipts are recovery work. Regeneration cannot substitute for their original identity or evidence. Missing unused drawings do not change unrelated shot hashes, but strict validation still reports the incomplete project inventory.

`validate --diagnostic` and coverage preserve missing/stale-source diagnostics without marking the project ready. Conflicting expected hashes, escaping references, unsupported contracts and invalid resource bounds fail closed. Repair manifest references deliberately after authoring; do not hide hash mismatches.

## Selective dependencies and local repair

~~~sh
python scripts/production.py snapshot --project PROJECT --output /tmp/before.json
# Edit a camera, track, drawing, clip or explicitly versioned source.
python scripts/production.py impact --project PROJECT --receipt /tmp/before.json --output /tmp/impact.json
~~~

Shot hashes bind identity, render settings and renderer, shot settings, camera, tracks, audio, used construction, selected drawings, placed clips, props and requested continuity endpoints. They avoid global cast/scene-manifest hashes that would invalidate every shot when unrelated assets are added.

Character-level provenance binds the current bytes of its primary source, acquisition receipt and optional `provenance.references`. Audio uses the same explicit supporting references for TTS receipts or original text. Put each record in a `{path, sha256}` reference; hashes written inside a note are not independently verified. Changing a referenced record expires the reviews and shot dependencies that consume it.

A camera-only change affects that shot's camera/final evidence and rerendering. An unchanged construction review can remain current. A new unused character or drawing does not alter existing shot dependencies. Component reviews include parent attachment construction; sibling changes alone do not stale them.

Continuity intentionally connects shots. Changing a requested endpoint position, scale, costume or prop owner can affect both ends of that declared link.

The renderer separately hashes evaluated visual state and exact used raster files for each frame. Resuming verifies files before reuse, allowing local track repairs to rerender affected frames. Audio has its own mix inputs and exact frame/sample timing.

## Record explicit review evidence

Use individual target flags or an exclusive `--target` JSON file:

~~~sh
python scripts/production.py review-template --project PROJECT --character milo --version 3.0.0 --output /tmp/milo-review.json
python scripts/production.py review-template --project PROJECT --character milo --version 3.0.0 --component mouth --output /tmp/mouth-review.json
python scripts/production.py review-template --project PROJECT --clip listen --version 1.0.0 --output /tmp/clip-review.json
python scripts/production.py review-template --project PROJECT --scene opening --shot exchange --render-manifest renders/opening/shots/exchange/render-manifest.json --output /tmp/shot-review.json
~~~

Inspect the actual artifacts, then complete reviewer identity, verdicts, notes and evidence. Every reviewed dimension needs an explicit pass/fail verdict and inspectable current evidence.

| Dimension | Purpose |
| --- | --- |
| `identity_construction` | Proportions, markings, scale, drawings and attachments |
| `pose_motion` | Pose, body motion, clip behavior and transitions |
| `facial_dialogue` | Face, gaze, expression, mouth and dialogue performance |
| `contacts_interactions` | Anchors, prop ownership, handoff timing and visible contact |
| `camera_composition` | Framing, relative scale in the shot and camera motion |
| `continuity` | Declared connections between shots and scene endpoints |
| `timing_av_sync` | Clip duration, playback timing, audio and mouth synchronization |
| `final_artifacts` | Complete exact rendered shot and inspected output artifacts |

Character/component targets review construction. Clips review motion, contacts and timing. Shots expose all eight dimensions. Repeated `--dimension` flags narrow the current receipt; they do not waive the remaining dimensions.

The reviewer requires `kind`, `id` and `name`. Allowed kinds are `human`, `assistant` and `synthetic`. An assistant identifies itself as an assistant. Automated tests use synthetic identity. Human or assistant reviewers cannot use automated metrics alone to establish artwork acceptance.

Evidence binds a project-relative path, SHA-256, note and method: `visual_inspection`, `audio_inspection`, `timed_playback` or `automated_check`. Optional frame indices must fit the target shot. Record what was actually inspected; file existence does not establish visual inspection.

Final artifact review requires the persisted complete shot `render-manifest.json`. The controller checks target identity, fixture label, current dependencies and renderer, settings, ordered frames, artifact hashes and audio frame/sample consistency. Include an actual rendered frame or layer from that manifest as visual evidence. Audio, movies and contact sheets can be additional evidence; they cannot satisfy the final visual gate alone. A stale camera render cannot support fresh final review. Earlier receipts lacking a bound visual frame or layer are reported stale and retain their history.

~~~sh
python scripts/production.py review --project PROJECT --receipt /tmp/completed-shot-review.json
python scripts/production.py reviews --project PROJECT --scene opening --shot exchange
~~~

Reviews append to a local hash-linked history. The latest decision for each dimension is checked against current dependencies and evidence; older decisions remain. The chain detects modified receipts but is not an externally signed identity service.

A receipt containing both passing and failing dimensions remains failed overall. Its eligible passing dimensions retain their acceptance while their dependencies remain current; reviewing a repaired failing dimension does not require repeating an unrelated passing decision.

Passing synthetic reviews remain software gates. Fixture reviews never become production art acceptance. Production acceptance applies only to explicitly inspected passing dimensions recorded by an identified human or assistant.

## Import with immutable construction and provenance

An import descriptor names the original external file. Relative `source` paths resolve beside the descriptor; use an absolute path for an in-memory Python descriptor.

~~~json
{
  "schema_version": 1,
  "kind": "component-import",
  "source": "/path/to/original-mouth.png",
  "fixture": false,
  "origin": "Original source and its attribution",
  "viewpoint": "front",
  "costume": "default",
  "drawing_id": "open",
  "note": "Specific reason for the revised drawing."
}
~~~

Optional `source_sha256` verifies the supplied original digest. Optional `attribution` and `permissions` preserve supplied information. Optional `crop` is an integer `[left, top, right, bottom]` rectangle inside the original image. There is no implicit resizing: the RGBA result must preserve the component viewport.

~~~sh
python scripts/production.py import-component --project PROJECT --character milo --version 3.0.0 --component mouth --new-version 3.0.1 --receipt /tmp/import-mouth.json
~~~

The importer preserves exact original bytes in content-addressed storage, writes a component PNG and new cast manifest, validates the candidate project, then promotes the project pointer. The old character version remains. Existing clips and shots retain their explicit old bindings until deliberately updated.

New versions require construction review and do not inherit view approvals. Fixture imports stay fixture. Undeclared viewpoints/costumes, conflicting source hashes, empty drawings and viewport changes are rejected before the project pointer changes.

For `adopt-generated-component`, provide binding/job and the same target/version fields. Omit `source` and `origin`; those come from the acquisition. A hash-bound snapshot retains the full original job, claim/failure/retry history, generation receipt, model metadata and coverage context. No unavailable provider identifiers are invented.

## Bound acquisition, claims and recovery

Prepare runs with existing `hatch.py prepare` inside the project, for example `PROJECT/acquisition/milo`. Actual generation follows the installed imagegen skill, including reference inspection and attachment rules.

Requirements are a nonempty JSON list of exact requested coverage:

~~~json
[
  {"character": "milo@3.0.0", "dimension": "viewpoint", "value": "front"},
  {"character": "milo@3.0.0", "dimension": "mouth", "value": "open"}
]
~~~

~~~sh
python scripts/production.py bind-generation --project PROJECT --binding milo --run-dir acquisition/milo --requirements /tmp/milo-needs.json
python scripts/production.py limits --project PROJECT --max-calls 4 --max-attempts 3 --concurrency 1
python scripts/production.py claim --project PROJECT --binding milo --job base --output /tmp/milo-handoff.json
~~~

A binding preserves native run identity, immutable request hash and complete requirements hash. Claiming includes that context in both original and retry prompts, then calls native hatch claim logic. Original images/roles, exact tokens, prompt snapshots, calibration, base-review prerequisites and native budgets remain authoritative.

The handoff is for installed imagegen. The controller implements no alternative image provider. Record actual model-call metadata only when exposed; unavailable identifiers and parameters remain null with an explicit reason.

~~~sh
python scripts/production.py ingest --project PROJECT --binding milo --job base --token ACTIVE_TOKEN --source /path/to/original-result.png --receipt /tmp/completed-handoff.json --note "Actual inspection notes."
python scripts/production.py generation-status --project PROJECT
~~~

Ingestion requires the exact native job/token/input hashes and `production_context`. Changing references/prompts stales a claim. Reconciliation preserves an active token so an uncertain outcome can be resolved explicitly; it does not silently expire that token and initiate another paid attempt.

~~~sh
python scripts/production.py fail --project PROJECT --binding milo --job base --token ACTIVE_TOKEN --note "Specific failed or uncertain outcome."
python scripts/production.py repair --project PROJECT --binding milo --job base --note "Specific corrective change for the next attempt."
~~~

Every recorded claim counts, including failed and uncertain attempts. Explicit repair can reset native per-job attempts but does not erase history or restore the project call budget. Project bounds apply across bound runs; native run limits remain in force.

## Render recovery and editor handoff

`render --shot ... --max-frames N` produces a bounded partial shot. Repeat without the limit to verify/reuse completed frames and finish missing work. Partial output cannot support final artifact review.

`render --video` requests a complete movie through available FFmpeg tools. RGBA frames, layers and PCM audio remain the authoritative outputs. Preview, encode and art inspection are separate checks.

~~~sh
python scripts/production.py export-editor --render-dir PROJECT/renders/opening --destination /tmp/opening-editor --frames 0,12,24
python scripts/production.py export-editor --render-dir PROJECT/renders/opening --destination /tmp/opening-editor-krita --frames 0,12,24 --exercise-krita
~~~

The adapter exports layered OpenRaster drawings plus animation/audio metadata. Per-frame transforms are baked into exported canvases; original pivots, anchors and independent tracks remain in the project. Adapter roundtrip verification and actual application integration are reported separately. An unavailable editor is not a successful consumer exercise.

## Complete archive, safe restore and migration

~~~sh
python scripts/production.py archive --project PROJECT --destination /tmp/project-complete.zip
python scripts/production.py restore --source /tmp/project-complete.zip --destination /tmp/restored-project
python scripts/production.py validate --project /tmp/restored-project
python scripts/production.py coverage --project /tmp/restored-project
~~~

The full tree is archived with a hash inventory. The shared verifier rejects duplicate/noncanonical paths, traversal, symlinks, unsupported receipts, missing inventory and hash mismatches. Bounds are 9,999 project files plus the receipt, 1 GiB expanded content and 256 MiB per member; the receipt is separately bounded.

All active acquisition tokens, including unbound runs, block archive creation. Finish or explicitly fail them first. Archived metadata does not establish an external application's installation or integration.

Restore verifies before extraction, copies only the inventory into staging, validates the project/bindings and promotes to a new destination. Existing destinations are preserved.

`renderer_changed` compares renderer identities. **`rerender_required` also checks actual cached render freshness**, including shot/scene manifests, child identities/order/boundaries, dependencies, artifact hashes and PCM duration. A camera edit archived without rerendering therefore returns true even with the same renderer. `render_freshness.affected_renders` explains changes; unaffected renders remain reusable.

Projects without cached renders still need their initial render. The report states that scope. Historical reviews remain and are re-evaluated against current dependencies.

`migrate` explicitly copies schema-1 workflow bytes to a unique backup before migration to schema 2. Old reviews remain historical and are not promoted to current approval. Unknown schemas are rejected. Acquisition bindings and native histories remain intact.

## Complete project transport in ZIP parts

~~~sh
python scripts/production_bundle.py create --archive /tmp/project-complete.zip --destination /tmp/complete-project --part-mib 1
python scripts/production_bundle.py verify --bundle /tmp/complete-project/production-bundle.json
python scripts/production_bundle.py restore --bundle /tmp/complete-project/production-bundle.json --destination /tmp/complete-restored
~~~

A delivery can expose editable documents and direct movies/storyboards while storing the full frame/layer tree as ordered ZIP parts. Each part is at most 24 MiB; bounds are 512 parts, 1 GiB total ZIP and a 1 MiB index. Duplicate JSON keys at every depth are rejected.

The index binds project identity, fixture label, ordered sizes/hashes and the full ZIP hash. Verification reassembles a bounded temporary ZIP, checks every byte count/hash, then uses the existing project verifier. Restore uses the same staged implementation. There is no extraction shortcut.

Creation verifies the staged transport before publishing it exclusively to a new destination. Existing destinations, including dangling symlinks and directories created during staging, are preserved. Destination normalization and the publication lock resolve parent-directory aliases while retaining the destination leaf, so a racing leaf symlink cannot redirect output or create a lock at its target. Failed publication removes only its owned staging directory; a platform without atomic exclusive publication support returns an error.

Missing, reordered, tampered, symlinked or aliased parts fail before promotion. Full-output pilot checks should restore the verified bundle when using bundled delivery; an explicit already-complete project override can remain direct. Missing/tampered output must fail, not silently use synthetic data or skip checks.

## Small source and artifact transfer

When a service limits payloads more tightly, `production_sources.py` transports explicitly selected originals in parts no larger than 65,535 bytes. This is separate from a complete project archive and suits original artwork, exact provenance records or selected final artifacts.

~~~sh
python scripts/production_sources.py create --root PROJECT --file sources/original.png --file provenance/original-receipt.json --provenance provenance/original-receipt.json --destination /tmp/original-source-parts --production --origin "Original artwork and exact retained acquisition receipt."
python scripts/production_sources.py verify --bundle /tmp/original-source-parts/sources-index.json
python scripts/production_sources.py restore --bundle /tmp/original-source-parts/sources-index.json --destination /tmp/restored-source-files
~~~

The source index binds original relative paths, sizes, hashes, an explicit fixture label and origin, and selected provenance paths. Sources and provenance records are retained byte-for-byte. Each original file has an ordered list of part references and a full-file hash. Parts use content-addressed names (`parts/<sha256>.bin`), so identical original/canonical copies and repeated blocks share one transfer. Assembled-byte limits still count every restored file.

Bounds are 65,535 bytes per part, 64 MiB per original file, 256 MiB of assembled files, 4,096 files, 8,192 unique parts, 32,768 part references and a 4 MiB index. Empty selected files retain their original empty-file hash without fabricated parts. Keep the bundle directory limited to the index and its exact part inventory; place upload caches or notes elsewhere.

Verification and staged restore reject duplicate JSON keys, unsafe paths, source/parent path conflicts, missing/extra/tampered parts and resource excess. A source that changes while parts are created is rejected. An interrupted restore leaves no promoted destination and can be retried with the same verified index. Selected-source restoration does not establish project completeness or create art approval.

## Focused verification

~~~sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_production_workflow.py' -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_production_bundle.py' -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -p 'test_production_sources.py' -v
~~~

These suites use explicit synthetic evidence and never invoke imagegen. They cover selective dependencies, reviewer identity/evidence freshness, import provenance, native retry accounting and negative archive/transport cases. Production art, audio/playback inspection and editor integration require separately recorded actual evidence.

### Explicit zero-audio export commands

`production.py export-video --request request.json --root masters --destination film.mp4 --silent --output receipt.json`
uses the separate silent encoder. The request pins the source inventory, rational
FPS, codec profile and audio-forbidden policy; CLI overrides cannot rewrite it.
`verify-export --artifact film.mp4 --request request.json --root masters --require-silent`
independently probes every stream and fully decodes the movie. Zero audio streams
are required; an AAC track of silence does not satisfy this workflow.

For complete legacy render folders, use `export-video --render-dir renders --destination film.webm --profile vp9-webm --silent`.
For an assembled timeline use `--timeline timeline-directory`; its returned
receipt retains the full source-to-output mapping. These commands preserve the
legacy `render --video` audiovisual review behavior.

`export-capabilities` reports actual installed encoder capabilities and separately
reports whether persistent jobs are available. `export-status`, `export-resume`
and `export-cancel` take `--job-root`; `export-batch --jobs jobs.json` consumes a
bounded array of existing job roots. Job commands require the canonical optional
`silent_jobs` authority. `export-sequence --request sequence.json --root masters
--format png --destination sequence` requires `silent_sequence` and rejects an
implicit format conversion. Missing authorities produce a nonzero structured
`EXPORT_DEPENDENCY_UNAVAILABLE` report, never a successful placeholder.

This CLI increment does not establish the complete #240 acceptance program:
integrated poster/contact-sheet and preview packaging, explicit resolution/FPS preparation,
portable delivery manifests and their current integrated acceptance remain open.

The public `silent_cli.prepare_preview(source, root, destination, width=320)`
uses the canonical explicit frame transform and returns a verified source plus
its immutable transform identity. `preview(...)` additionally encodes and fully
verifies a zero-audio H.264 derivative. `review_assets(..., indices=[0,72,192])`
publishes exact source-pixel poster bytes, bounded thumbnails and a contact sheet
with source and output hashes; all derivatives remain visually unreviewed.

Silent command `--output` reports use shared pinned exclusive publication and
reject artifact/workspace aliases before execution. A concurrently created report
is preserved and produces a failure. This secure file-report adapter currently
requires Linux procfs/dirfd support; stdout JSON remains available on other
platforms. Legacy production report behavior is unchanged.
