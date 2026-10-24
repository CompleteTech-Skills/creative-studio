# Independent review: native rig loading resource admission

- Verdict: **CHANGES_REQUIRED**.
- Reviewer: independent `gpt-6-astra`, high reasoning, agent `/root/rig_resource_limits/review_corrected_loader`; separate from the implementer.
- Exact reviewed HEAD: `dffbf6f22b59e46378c712babf62449b728fb569`.
- Base: `d932d08a781e5c37a472a595b238972799f8bbf4`.
- Branch supplied for review: `agent/rig-resource-limits-fleet`.
- Frozen review checkout: `/home/complete/.cache/hatch-fleet/review-rig-load-limits-corrected-tree` (detached at the exact HEAD).
- Scope: shared native loader only. This is neither full issue #115 acceptance nor approval of the complete #107 continuity mission.

## Findings

### P1 — Admit or reject ZIP decoder workspace before opening members

Location: `scripts/rig_load_limits.py:151–164`, particularly the member checks at 155–159 and first `archive.read` at 164.

The new admission code accounts for encoded archive bytes and four times the members' expanded bytes, but does not restrict `ZipInfo.compress_type`. Python therefore accepts BZIP2 and LZMA members as well as stored/deflated members. Their decoder workspace is not bounded by the expanded member length or the 2 MiB I/O reserve. In the actual Python 3.12.15 stdlib used by the tests, `zipfile.LZMADecompressor.decompress` constructs `lzma.LZMADecompressor(FORMAT_RAW, filters=[_decode_filter_properties(...)])` from the member's properties without a decoder memory limit. This allocation is reached during metadata/header reads in preflight, before NumPy materialization.

Consequently, a small admitted bundle can require materially more decoder memory than its reported loading estimate. This is an admission-accounting defect, independent of whether the operating system enforces RSS. The existing native writer uses deflate, so a narrow stored/deflated native profile is a practical correction. Reject other methods before any member is opened/read, or explicitly admit and enforce decoder workspace. Add bounded tests that prove unsupported methods are rejected before decoder construction; a large-memory reproduction is unnecessary.

Evidence: complete `scan_bundle` review plus `/home/complete/.local/share/mise/installs/python/3.12.15/lib/python3.12/zipfile/__init__.py`, `LZMADecompressor`, `_check_compression`, and `_get_decompressor`. This finding was established by code inspection; no new malformed archive program or oversized decoder run was executed.

### P1 — Image.open performs unaccounted metadata expansion during admission

Location: `scripts/rig_load_limits.py:199–215`, especially `Image.open(...)` at 203.

The code uses Pillow as though opening an image were only a fixed-size dimension probe. The installed Pillow PNG implementation parses chunks through the first IDAT during `_open`, including compressed `zTXt`/`iTXt` text. It expands and retains that text before returning the image dimensions. Its separate `MAX_TEXT_MEMORY` permits up to 64 MiB of text, whereas this loader reserves only 2 MiB for I/O/header scratch and charges encoded PNG bytes plus pixel dimensions. The decompression-bomb warning addresses pixel count, not ancillary text expansion. A small-dimension PNG with compressed metadata can therefore consume more than a strict caller memory reservation during preflight, despite a small reported estimate.

Use a bounded dimension-only parser that skips ancillary payloads without decoding them, or bound/account for metadata processing before invoking Pillow. Preserve explicit format/dimension checks and clarify that header admission is not full pixel validation. Add a small metadata-bearing PNG regression that proves admission does not enter a general image/metadata decoder. Test ordinary PNG/JPEG format variants as well as rejection cases.

Evidence: `scan_bundle` plus the installed `PIL/PngImagePlugin.py` constants `MAX_TEXT_CHUNK`/`MAX_TEXT_MEMORY`, `_open`, `chunk_zTXt`, `chunk_iTXt`, and `check_text_memory`. This is static execution-path evidence; no new high-expansion image was generated or opened during this review.

### P1 — Repeated image references can materialize uncharged encoded copies

Locations: `scripts/rig_models.py:362` (image construction) and `scripts/rig_load_limits.py:189–215` (image admission).

Native metadata can contain several image records referring to the same admitted NPY byte vector. The scanner charges expanded bytes once for that ZIP member, then charges decoded pixels for each image record. `load_geometry`, however, calls `arrays[i['data']].tobytes()` separately for every record, producing a new retained encoded-data copy each time. These copies are not charged per reference before construction. The subsequent `validate_geometry` aggregate byte check runs only after all copies have been created.

For images whose encoded data is large relative to their pixel dimensions, such as small images carrying substantial ancillary metadata, these repeated encoded copies can exceed the admitted working set. The per-record JSON estimate and per-record pixel estimate do not generally cover the referenced encoded vector's length. This remains a problem after replacing Pillow with a dimension-only probe.

Either charge each image record's encoded copy before construction, or reuse one immutable `bytes` value per referenced image-data key while retaining each image's independent name/MIME metadata. Add a bounded alias-reference regression that checks the accounting or identity-sharing behavior and rejection timing. No new reproduction program was run; this follows directly from the per-member accounting and unconditional per-record `tobytes()` call.

### P1 — Check locked-index inventory length before creating its Python list

Locations: `scripts/rig_models.py:386` (`arrays['locked'].tolist()`) and `_check_load_headers`' skin branch at 415–420.

The skin preflight does not compare the locked array's shape/count with `descriptor['locked_vertices']`. Loading later creates a Python integer list for the entire array before comparing it with the descriptor. When the actual array is much longer than the declared list, the manifest's JSON allowance covers only the short declared inventory, while four times the numeric array bytes does not cover a large list of separately allocated Python integers. The load may therefore exceed its conservative estimate before reporting the inventory mismatch.

Bind the locked array's one-dimensional count to the validated descriptor inventory during preflight, before array/list construction, and retain the eventual value comparison. Add a bounded mismatch regression proving early rejection. This issue was raised by the implementation owner's follow-up audit and independently confirmed here against the frozen source; no new input program was executed.

The first three findings were delivered to the implementation owner promptly. The owner acknowledged them and began changes in its own branch; those changes are outside this frozen review. The first three were also sent to the root agent. The fourth is recorded here after source confirmation of the owner's follow-up observation.

## Review coverage and acceptance assessment

Reviewed the complete source/documentation/test changes from the base to the exact HEAD: the new `rig_load_limits.py`, all modified `rig_models.py` paths and their surrounding models/material/geometry validation and binary writer, the CI gate registration, the resource-admission guide, and every one of the 15 added tests and their fixture construction. Inspected retained validation/error artifacts, source inventories, command logs, and their recorded digests.

Read repository README, SKILL.md and DELIVERY_STATUS.md, the exact cached #115 request, the full #107 body and continuation, and its scope/fallback addendum and reopening history. The issue context is retained under `/home/complete/.cache/hatch-fleet/rig-resource-issue-context/`. These require actual bounded behavior and honest unavailable states; a test log or file's existence cannot establish acceptance.

Traced native loader use through CLI, job, HTTP and archive paths, and inspected the separate historical/current local `RigOwner` caller in `/tmp/hatch-continuity115/scripts/continuity_capability_owners.py`. That caller loads before its vertex/triangle-only reservation check and loads again afterward. This helper change does not update that caller or establish its overall operation reservation, resident interpreter budget, output budget, deadline, or post-operation pins. The helper documentation appropriately leaves those responsibilities with callers.

The following aspects are supported by source inspection and the executed bounded tests:

- Restrictive validated partial caller limits use a fresh copy and retain manifest/signature/source data.
- Geometry and skins share an aggregate admission budget; clips, identity, manifest, review JSON and input files are counted.
- Shape/dtype/rank/payload agreement is checked before array materialization. Object/pickle arrays and unsafe or duplicate member names are rejected.
- The canonical directory-end equality at `rig_load_limits.py:110–115` fixes the prior offset-reconciliation mismatch. The prewalk bounds actual central-directory entries before `ZipFile` constructs its list; the harmless prefixed-directory regression covers the reported defect.
- Binary header parsing and materialization use one descriptor-hash-checked immutable snapshot. Endianness, values, empty numeric arrays and writeability are covered. The Fortran test checks numerical interpretation, not a promise to preserve Fortran-contiguous storage.
- The real robot fixture, multiple skins/clips, 48 actual 512×512 PNGs and compressed pixel expansion exercise the original reservation problem with real native data.
- Existing material-validation regions are unchanged. Native material roundtrips and CLI rejection remain covered by the combined run.
- Optional 3D imports remain isolated from ordinary 2D discovery/capabilities. Texture admission in this revision newly uses Pillow, explicitly handling missing Pillow as a dependency error.

Acceptance remains **failed for conservative loading-memory admission** because of the four findings above. No hard RSS/CPU sandbox was expected or claimed. No issue was found with the corrected central-directory equality itself. The review does not certify full decoded-image validity, visual correctness, rendering/evaluation limits, external integrations, or parent-mission completion.

## Independent tests and evidence

Executed only the existing bounded local suites and their ordinary Python CLI/in-process native tests, from the frozen checkout:

```text
PYTHONPATH=scripts:tests OPENBLAS_NUM_THREADS=1 \
TMPDIR=/home/complete/.cache/hatch-fleet/review-rig-load-limits-corrected-tmp \
/home/complete/.cache/hatch-fleet/rig-py312/bin/python -m unittest \
test_rig_load_limits test_rig_skinning test_rig_material_validation \
test_rig_contracts test_model3d_contracts test_rig_compatibility -v
```

Result: **109 tests, 21.377 seconds, zero failures, zero errors, zero skips**, exit code 0. The 809 legacy 2D cases were **discovered only**, not executed by this run. Retained Blender report schema coverage is not a fresh Blender execution.

Independent log: `/home/complete/.cache/hatch-fleet/review-rig-load-limits-corrected-tests.log`.
SHA-256: `5a6dc75bc7e545617ecccd1cf5c7b6ebb7ef97d0723e66c0d1d96c9a79bfef53`.

All artifact digests in `validation.json` and `correction-validation.json` match their retained files. All 150 entries in `corrected-source-before.json` match the exact reviewed checkout after tests. The older 150-file inventory differs only in `scripts/rig_load_limits.py` and `tests/test_rig_load_limits.py`, consistent with the directory correction. The older evidence accurately reports 108 tests at its earlier source commit; corrected retained evidence reports 109 tests in 20.343 seconds at `6d327e439fef47de19d289d8a33cee0839c2effe`. These are distinct from this independent run.

The earlier reviewer error is preserved, not treated as approval. `review-agent-error.json` records `AGENT_ERRORED_NO_FINAL_REVIEW_NO_PASS` at `399058f8571bd2cdb8228c346fbde134b05214c4`, its real ZIP finding, and the exact possible-cybersecurity-risk runtime failure. This review completed without that runtime blocker, found the prior correction sound, identified three additional defects and confirmed the fourth reported by the owner. It does not retroactively convert the earlier failure into PASS.

No source edits, GitHub writes, merges, external backend executions, doctor/version probes, browser/Node/FFmpeg/Blender/PyBullet workers, or new malformed-data programs were performed. Review artifacts and temporary test data are in the owned home-cache locations. The detached checkout is clean.

## CI and recommendation

The new test family is registered in the existing skinning gate, invoked by `.github/workflows/rig-3d.yml`. Current-HEAD GitHub CI was not executed or claimed by this review. The local combined suite is useful regression evidence but does not cover the four newly identified branches.

Request changes. Preserve this report and prior error/finding history, correct all four accounting paths, add bounded behavioral regressions, rerun the appropriate suites, and obtain a fresh independent full review of the new exact HEAD. Root's individual publication lease, required CI and merge gates remain in force. This report is an agent code review, not GitHub-native approval, human signoff, visual approval, or permission to merge.
