# Cohesive native PNG/JPEG admission review

**Verdict: PASS — assigned native PNG completeness and warning-aware JPEG source-admission scope.** No actionable finding remains in the reviewed change. This is not approval to close parent issues #52/#44/#139, not hosted-CI acceptance, not production-art approval, and not independent authorization to publish or merge.

Exact reviewed HEAD: `10f6c0bb4f303a42e32c06e9fc8cb7d4a3791bbb`
Runtime/source commit: `8c7f9eefc1ae5905e1584b95e3bb78f52338e7b0`
Original base: `bae65356567a10915658a6371c14ee9a4553db36`
Integrated standalone module commit: `e881182b12583dc1dcd99cba66714ed520d2e419`
Reviewer task: `/root/native_texture_integrity/review_texture`; parent explicitly declared `gpt-6-astra` launch configuration; runtime model identity was not independently introspected.
Detached worktree: `/tmp/hatch-fleet/review-native-texture-10f6c0b`; clean after review. No source/evidence changes in the checkout, dependency installation, GitHub write, or merge. Earlier CHANGES_REQUIRED/BLOCKED reports remain preserved as `review-native-texture-fe467c0.md` and `review-native-texture-b5b48f2.md`.

## Findings resolved and implementation assessment

The prior PNG P1 remains fixed: missing declared scanlines, incomplete zlib termination/checksum, bad/truncated IEND CRC and extra expanded/compressed data reject before source admission. The bounded chunk inflater and Adam7 byte accounting remain unchanged by JPEG integration. Actual Pillow decoding follows structural validation; original encoded bytes are retained.

The prior JPEG P1 is now fixed at the native callsite. The sole shared `_decode_texture` boundary invokes `verify_jpeg` before admission with actual Pillow header dimensions, the encoded-byte cap and remaining aggregate working allowance. Canonical Huffman entropy cuts 5/20/100/300 that Pillow recovers are rejected through GLB, glTF bufferView, external JPEG, data URI and OBJ/MTL. Missing or mismatched optional decoder dependencies fail explicitly without a permissive fallback. PNG imports remain independent of the JPEG dependency.

Reviewed the complete merged `rig_jpeg.py`, requirements, workflow, documentation, new fixtures/tests/evidence and native callsite. The module matches the separately reviewed e881182 implementation byte-for-byte. Its marker/scan walk is length-aware and skips opaque APP payloads; both native header and pixel calls explicitly enable strict warnings. Header dimensions, pixel/working limits and expected-size agreement precede full JPEG decoding. The local retained upstream simplejpeg implementation confirms strict native error/warning handling. Gray/CMYK remain in their native channel counts. No source repair/transcoding path or process-wide Pillow policy mutation was introduced.

The separately merged correction-validation change remains present, uses the existing `validate_controllers` authority before persistence, and all four correction-validation tests pass in the combined suite. Gate registrations retain correction, PNG, JPEG and prior interoperability coverage.

## Independent exact-HEAD execution

Used the declared complete optional rig environment `/tmp/hatch-strict-jpeg-env/bin/python`, Python 3.12, actual simplejpeg 1.9.0, Pillow 12.3.0 and NumPy 2.3.5. Tool locations were sourced from `/home/complete/.cache/hatch-fleet/environment.sh`; Playwright used its declared 1.55 browser path. No environment was modified.

`PYTHONPATH=scripts:tests HATCH_RIG_INTEROP_TEST=1 HATCH_RIG_BLENDER_TEST=1 HATCH_RIG_BROWSER_TEST=1 PLAYWRIGHT_BROWSERS_PATH=$HATCH_RIG_PLAYWRIGHT_BROWSERS_PATH /tmp/hatch-strict-jpeg-env/bin/python -B -m unittest -v test_rig_texture_integrity test_rig_jpeg test_rig_interop test_rig_contracts test_rig_skinning test_rig_compatibility test_rig_correction_validation`

**67 tests passed, zero skips, 29.597 seconds.** Actual Khronos, Blender and browser tests ran. Log: `/tmp/hatch-fleet/review-native-texture-10f-complete-regression.log`. Discovery/isolation of 809 ordinary 2D tests also passed; this does not claim execution of all 809.

An initial attempt with the older shared `hatch_rig_python` environment failed because that environment lacks newly required simplejpeg. That failed attempt is preserved in `review-native-texture-10f-regression.log`; it is not counted as a successful run. Switching to the candidate's already-prepared complete environment produced the result above. The absent dependency was not silently accepted and nothing was installed into shared environments.

Additional independent actual-file checks:

- **20 API + actual CLI corruption cases:** four original Huffman cuts through all five native storage routes. Every case returned structured `RIG_TEXTURE_DECODE`, published no run, and preserved all preexisting source hashes.
- **30 all-route positive cases:** Gray/RGB/CMYK, baseline/progressive at 41×23, including opaque APP13 payloads containing fake SOS/EOI/SOI marker bytes. Every admitted image retained exact encoded bytes.
- **10 all-route arithmetic original cases:** both retained baseline/progressive arithmetic fixtures imported unchanged across all five routes.
- Original PNG failure corpus rerun: valid control succeeds, every original malformed-row/footer/CRC case rejects.
- **105 independently constructed Adam7 combinations:** tiny/odd dimensions, all supported PNG color/bit-depth profiles, three-byte IDAT fragments. All accepted with expected dimensions.
- A one-pixel PNG whose compressed payload expands to **1 GiB** rejects under an actual **1 GiB address-space cap**, peak RSS **49,184 KiB**. Native JPEG headers declaring 4097×4096 and 65000×65000 reject with `RIG_LIMIT_TEXTURE` under the same cap, peak RSS **39,368 KiB**.

Evidence: `/tmp/hatch-fleet/review-native-texture-10f-routes.py`, corresponding `-routes.log`, `-png.log`, and `-adam7-resource.log`.

Independent actual djpeg controls were rerun against the originals, four warning-producing Huffman cuts and eight altered arithmetic cases. Native strict results agree with djpeg warning status in all 15 controls. Evidence: `/tmp/hatch-fleet/review-native-texture-10f-codec-controls.json`.

## CI and evidence integrity

The new three-OS workflow installs optional rig-test dependencies, uses Python 3.12 and portable unittest discovery with `PYTHONPATH=scripts`, preserves pipe failures, pins actions, and uploads exact-SHA logs. Changed-path triggers cover rig Python sources, both texture test families, helper tests, requirements and fixture bytes. Ordinary 2D requirements remain unchanged.

Executed both workflow commands locally: codec **12 passed**; native routes **19 run, 18 passed plus one explicitly disabled optional Khronos check**. Logs: `review-native-texture-10f-ci-codec.log` and `-ci-routes.log`. The fully enabled 67-test run above executes that independent check and has zero skips. Windows/macOS hosted execution remains pending; wheel availability and workflow inspection are not substitutes for those runs.

Verified every current receipt entry: ten input files, ten protected files, fifteen evidence artifacts, all byte lengths and SHA-256 values. The 326-entry frozen source-before/source-after dictionaries are identical. Of these, 325 match final HEAD; the sole post-run difference is the final `verification.json` receipt itself. No runtime/test/profile source drift was found. Authored source/test/doc/workflow diff whitespace check passes. Historical raw logs and reports remain unaltered; their known trailing unittest whitespace is documented rather than disguised.

## Boundaries and residual risks

The result establishes bounded source admission under the declared PNG/JPEG profile, not original-pixel authenticity. Arithmetic JPEG permits legal marker padding: the eight changed arithmetic inputs are warning-free under both strict decoding and independent djpeg and can represent different valid bitstreams. They must not be mislabeled a malformed-data warning bypass. Establishing original intended content requires an independently supplied original digest. The integrated documentation retains that distinction accurately.

The 16-byte-per-pixel working figure remains an admission estimate, not an OS-enforced allocation or time sandbox. Existing process-level constraints and filesystem admission guarantees remain separate. The new pinned codec is optional 3D functionality; unsupported/missing installations fail closed. No visual/artistic/rights acceptance, native Windows/macOS execution, comprehensive filesystem sandbox, or completion of #52/#44/#139 is claimed.

This exact source-admission change is independently ready for the root owner's next review/publication step, subject to its unchanged hosted-CI and merge gates.
