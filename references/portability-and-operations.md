# Portability, recovery and limits

Current formats: controller run schema 3, immutable request schema 3, profile schema 1 or 2, extraction schema 4, source/annotation schema 1, visual review schema 2, animation metadata schema 2, full archive receipt schema 2. Package receipt schema 2 and the default Codex profile remain unchanged. Consumers that strictly accept only animation metadata v1 must update their schema check for the added playback fields. Unsupported run versions fail with recovery instructions instead of silently reinterpreting evidence.

## Dependencies and stale work

New preparations add a version-1 `resolved_style` snapshot to the existing immutable request schema 3. It contains the complete selected specification, modifiers, notes, rendered contract, catalog version/hash and snapshot hash. The controller records the snapshot hash and includes the full specification in generation handoffs/history. Saved snapshots are validated without resolving against a newer catalog; request hashes already bind them to generation/extraction/review. Historical requests and package receipt schema 2 remain supported without a new migration. See [style reproducibility](style-catalog.md#reproducibility-and-historical-runs).

Generation hashes cover the immutable request, required images, prompt/retry prompt and the relevant generation projection of the profile. Only actual derivations depend on their source state. Independent generation of a mirror-capable state never depends on the optional mirror source. Repair preserves history and invalidates actual descendants.

Extraction hashes cover relevant geometry/alignment/masking/calibration, annotation and source bytes. Playback timing/events and export options do not require another model call or re-extraction. `revise-profile` computes affected generation and extraction states; state-set/order changes deliberately require a new run. Direct file edits remain detectable but may be broader than a controlled revision. Reviews and packages bind exact artifacts and pipeline version, so profile edits require renewed applicable review even when raster sources are reused.

`configure --holes`, `--key-threshold`, `--shared-scale`, or `--resampling` changes deterministic processing, invalidates review, and reuses generation. Generation background/key changes rebuild affected prompts. Base review is refreshed after calibration. `status` reports active token age, stale inputs, progress, operation failures and next recovery action. It cannot establish that an external imagegen worker is alive from a token alone.

An unresolved token is never automatically expired or replaced. Recover its original selected output and `ingest`, or explicitly `fail` the token with the observed reason. `resume` records interrupted local operations, reconciles recoverable transactions, and never starts duplicate controllers. Locks release on process exit. No WSL lifecycle operations are used.

The legacy job status value `generated` means that an owned source is available. Always read its separate origin receipt: `generation_receipt`, `import_receipt`, or `derivation_receipt`, plus the run's fixture flag. The status word alone is not a claim that imagegen produced an imported source.

## Doctor and budgets

```powershell
python scripts/doctor.py --destination output/run --profile presets/flipbook.json
python scripts/hatch.py capabilities
python scripts/hatch.py limits --run-dir output/run --max-attempts 4 --concurrency 2 --max-calls 25
python scripts/hatch.py limits --run-dir output/run --estimate-units 2 --unit-budget 50
```

Doctor reports Python/Pillow support, codecs, local rename/write capability, free space, profile resource estimates and optional Playwright availability. Imagegen tool availability is not measurable by a Python subprocess and is reported honestly. Python 3.10+ and Pillow 10.1+ are supported by code paths; the actual tested versions are recorded in validation results. Browser binaries are optional for users but required for browser regression evidence.

Call budgets count recorded claims, including failed and uncertain calls. Estimates are user-defined units; no money/token prices are inferred. Limits do not erase attempts or claim history. Default: three attempts per job, concurrency one, no implied monetary budget. Raising a limit is an explicit operation recorded in history.

Bounds include 64 frames/state, 1,024 total frames, at most 2,000,000 source pixels and 64 MiB encoded bytes before decoding, a 64-million-pixel atlas ceiling plus a stricter estimated 768 MiB raster working-set bound, 256 MiB print-page buffers, 256-pixel cursor edges, 128-cell craft edges, and 1 GiB restored archive content. Estimates are conservative planning bounds, not precise peak-RAM guarantees. Native codec memory and browser decoding vary. Split larger jobs into profiles or reduce source resolution; do not bypass checks.

## Migration and verified restoration

Operational artifact references are relative to the run/frame tree. Original external paths and historical diagnostic path strings are preserved as attribution; they are not dependencies that must exist elsewhere. Locate restored assets through the relative frame manifest and current run directory. Keeping historical reports byte-for-byte avoids silently rewriting the evidence covered by an earlier review. Code, schema, Python, Pillow, platform and playback implementation versions are recorded. Generation receipts retain genuine call metadata when available and mark missing fields unavailable.

```powershell
python scripts/hatch.py migrate --run-dir work/old-run
python scripts/hatch.py validate --run-dir work/old-run
python scripts/hatch.py review-ui --run-dir work/old-run --destination work/migrated-review
python scripts/hatch.py archive --run-dir work/old-run --destination work/complete-run.zip
python scripts/hatch.py restore --source work/complete-run.zip --run-dir work/elsewhere/restored-run
python scripts/hatch.py status --run-dir work/elsewhere/restored-run
```

Migration accepts controller schema 2, verifies and preserves a pre-migration evidence backup, retains run ID/history and requires extraction v4 plus fresh visual review. Active tokens survive but become stale pending explicit reconciliation; a migration never invents a replacement model call. Older unsupported schemas need a separate prepared run and explicit imports of retained original artwork.

New claims retain exact UTF-8 prompt snapshots, input roles/hashes and identity. Completed attempts retain original source locations and output hashes. Superseded visual reviews and base checkpoints, plus prior/current calibration values, remain in the manifest history. Older runs cannot recover content that was never stored; migration preserves their existing hashes and honestly leaves unavailable evidence unavailable.

`archive` requires reconciled generation claims and includes the full deterministic/review artifacts in a hash-verified ZIP. Restoration checks the complete inventory, member hashes, sizes, traversal/links and versions before promotion into a new directory. Run identity and history survive. Compatible full snapshots retain only evidence whose hashes still validate. Tool-version changes or compact archives explicitly require revalidation. External package/install receipts are preserved as history and are not treated as current installations on the new host.

The restore destination must remain absent until publication. A destination symlink is rejected even if its target is missing. If another writer creates the destination during restoration, even as an empty directory or symlink, restoration fails and preserves it. Only this attempt's temporary extraction directory is removed. The restore CLI locks the requested leaf without following a newly introduced destination link and does not recover unrelated interrupted prepare transactions; their journals and files remain available to the prepare workflow.

Cleanup's compact repair archive can also be restored, but requires rebuilding removed deterministic artifacts. `minimal-delivery` omits decoded rows and therefore may need matching originals or new generation. Never use cleanup as an incidental way to delete external imagegen results, user art or unrelated files. Installed packages remain separate and require explicit installation authority.
