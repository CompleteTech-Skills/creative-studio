# Independent Scene60 final-stack review

Reviewer: **gpt-6-astra**, detached checkout `/tmp/hatch-review-scene60`.
Exact reviewed head: **`82352b2db4a02da4fed2df16c7147148e17dbdca`**.

**Whole final-head review: PASS. No unresolved local findings.**

The nested optional-child admission/reopening fix `175c7f677dce8db1caeca9fc00176e903e9448d5` and final dependency integrations are independently validated together. Root's separate prerequisite-merge, publication and exact published-head CI gates still apply.

## Historical integration finding resolved

At e437 the Model3D embedded-PNG bomb test returned `RIG_LIMIT_TEXTURE` instead of its existing `MODEL3D_MODEL` boundary diagnostic. Final central correction `e9ef226816f747b2790c73168c38606ddbcb00f3` is now integrated: the shared `inspect_native` boundary covers primary geometry and secondary LODs, preserves the nested native diagnostic and correction, maps absent strict JPEG verification to `MODEL3D_DEPENDENCY`, and leaves unrelated native errors unchanged. The original assertion was not weakened. Independent full-suite execution passes the original case and real primary/secondary-LOD corrupted-JPEG and unavailable-verifier CLI cases, preserving input bytes.

Scene59 `91f13a750a639ea446acca63765fe58cd005769d` changes only tests/documentation: the real loader must reject malformed Unicode before filesystem operations on that name; optional Linux byte-name materialization is supplementary. Runtime admission remains unchanged. Entire final delta source-reviewed.

## Exact-head tests

- **257 tests passed, zero failures, zero skips; 94.519 seconds.** Modules: Scene60 service, Scene59 contracts, Scene3D math, material validation, import transforms, native interoperability, texture integrity, strict JPEG, correction validation, compatibility and Model3D contracts.
- Complete read-only Python 3.12 environment `/tmp/hatch-strict-jpeg-env/bin/python`; pinned Node 22.16.0; Blender 4.5.3 LTS; Chromium 140.0.7339.16; Three.js 0.180.0. All three native flags enabled; BLAS/OpenMP threads bounded to one.
- Genuine imported skin/morph/animation Blender save/reopen: 24 compared vertices; maximum vertex error `0.0010001737325902988 m`, joint error `0.0019911425853699413 m`, within `0.01 m`. Three bones, one armature, actions and NLA survived.
- Genuine browser geometry: 24 compared vertices per report, maximum error `6.9884171931019664e-09 m`, no browser errors. Playback, frame step, default reduced-motion rest and explicit mapped aliases executed.
- Original/normalized Khronos imports executed. Compatibility discovered 897 legacy 2D tests without optional 3D dependencies; discovery is not a claim that all 897 ran.
- Schema and rig-template checks passed; detached source clean. Six interoperability hooks plus correction operations hook preserved.

## Independent adversarial and integration probes

- Unknown/mistyped nested optional-child parameters, invalid effective scale and nonunit quaternion reject before API mutation. Foreign scenes constructed outside the service reject during actual staged snapshot reopening with no destination. Valid nested controls expand, propagate fixture provenance and reopen.
- Separately valid 70-level graphs reject when composed too deeply. **Exact composed depth 128 passes; 129 rejects atomically.** Invalid quaternion defaults reject registration before catalog mutation.
- The manifest-depth regression remains distinct: 127 dependency prefabs plus project passes; 128 plus project rejects, in both registration orders. Dependency-only references isolate manifest depth; new composed-depth tests cover actual transforms. The default-quaternion test now verifies earlier atomic rejection, not permissive acceptance.
- Noncanonical manifests with schema integer positions encoded as integral floats, three-space JSON and CRLF endings pass real API animated duplication, prefab instantiate/detach, snapshot/reopen and actual CLI duplication. Eight original source files remain byte-identical, including the original project under hash-named history. Opaque metadata floats remain floats; animation source bytes/SHA bindings stay exact.
- **18 real Scene60 texture imports:** valid PNG/JPEG through GLB, external glTF and data URI succeed (six controls); four entropy-truncated JPEG variants through all three routes reject (12 negatives). Rejections register no asset version; sources unchanged. Successful imports preserve original native payloads and fixture/no-production-approval classification.
- Previous exact-byte, reference-closure and resource probes rerun successfully. Tampered parsed animation context rejects. Thirty unique DAG clips derive with 59 mapping calls in about 0.42 seconds. Optional-child conflict resolution cascades descendants. Derived animation target deletion rejects, and the intact graph snapshots. Combined file/byte limits reject atomically; exact 1,330-byte boundary succeeds.

## Source review

Read final contracts/prefab helper, service call sites and loader context checks, prerequisite runtime deltas and integration hooks. Shared effective composition is NumPy-free and operates on validated copies. Schema-position integer normalization preserves opaque JSON. Defaults, parameters, node/material overrides and optional children share composition; expanded identities, actual hierarchy/dependency cycles, logical node/work budgets, memoized variants, collections and fixture propagation are checked before promotion/reopening.

Final Scene59 `91f13a750a639ea446acca63765fe58cd005769d` (including integer-copy `11fb54c`), central texture/JPEG/Model3D `e9ef226816f747b2790c73168c38606ddbcb00f3` (including `10f6c0b`) and main `dcba912` are ancestors. Core graph/import/snapshot/CLI modules remain byte-identical to reviewed e304; the effective-prefab helper is a genuine additional fix. Model3D generator/discovery hooks coexist with existing families.

## Evidence and limitations

- `review-scene60-823-verification.json`: exact head, 29 source hashes, 24 evidence/probe hashes, environment versions and Node binary hash.
- `review-scene60-823-tests.log`: complete independent 257-test passing result.
- `review-scene60-823-native-evidence/`: three actual Blender/browser reports with measured errors and artifact hashes.
- `review-scene60-823-{nested,depth,integration,round3,reference,resource}.json`: independent results; executable probes retained alongside them.

All paths above are under `/tmp/hatch-fleet`. All six probe JSON files parse successfully, all 18 texture routes meet their expected outcomes, and all three retained native reports explicitly report success. Source and evidence hashes bind this exact tested head.

Historical e437 (253/254 with the now resolved integration failure) is preserved in `review-scene60-e437.md` and its original logs/hashes. Its initial reviewer probe mistakenly expected destination project.json itself unchanged; the corrected probe verified the original retained under hash-named history. This final-head probe run passes without that error. Historical e304 scoped PASS, 157-test result and retained native demo remain in `review-scene60-e304.md`; earlier twelve findings/fixes remain in `review-scene60-rounds1-3.md`. Historical passes are separate from this fresh whole-head PASS.

Native test binaries are normally cleaned; actual measured reports/hashes remain. The earlier retained demo supplies genuine source/native/portable artifacts but was not rerun at 823. Fixtures imply no artistic/production approval. Unknown future capabilities remain unsupported. Current published-head CI and root's prerequisite/authorization gates remain separate. No implementation edits, GitHub writes or merges by reviewer. Test freeze ended **2026-10-24 05:58:24 UTC**; exclusive backend/discovery/lifecycle slot explicitly released to corrected Physics170 owner/reviewer, then Editor99, per root's updated queue.
