# Independent semantic core review

Result: **CLEAN** at `43ecea75cadbbbd8f2bab255040eea05fd369982`, based on `0226b6f3b2c37ff76d06a60df67fba37b3494dde`. No actionable correctness, security or regression findings remain in the assigned semantic core scope. The checkout was clean and implementation source was not modified by this reviewer.

Reviewed the complete new semantic core, evidence binding, native snapshot I/O, CLI and versioned surface wrappers, with associated core tests, executable demonstration and semantic documentation. The PBR/UV/projection algorithms and surface test module have a separate independent review; this receipt does not replace that review or certify whole-program acceptance.

The prior review findings were corrected and independently verified: duplicate internal attachment targets follow the duplicated subtree; opaque glTF extras are not mistaken for texture bindings; geometry/evidence are frozen through bounded no-follow reads pinned to the selected source root before native parsing; malformed nested records and boolean scalars receive structured failures; cumulative hierarchy depth cannot bypass its limit through cached parents; known fixture replacements retain fixture provenance; and copy/transform resource admission occurs before expensive cloning. The selected-mesh transform containers reuse the native transformation authority without repeated whole-asset array copies.

The new transform-only grouping helper preserves world placement and rejects cycles. Identity ratio rules distinguish owned meshes from explicitly requested subtree bounds. Immutable earlier snapshots and input geometry remain unchanged; incompatible combinations, corrupted data, unknown schemas, source races and unrepresentable float32 exports fail rather than silently degrading source evidence. Explicit semantic labels, style bindings, measured identity rules and rigid poses do not imply anatomical, animation or artistic approval.

## Independent validation

- Frozen semantic suite: **17 tests passed in 2.390 seconds**. Log: `model47-semantic-independent-frozen-tests.log`.
- Independent correction/convention probes: **10 tests passed in 0.526 seconds**. Log: `model47-independent-final10-probes.log`. These independently exercised duplicate internal/external bindings; original-source NPZ replacement after admission; opaque texture-named extras; malformed nested snapshots; mixed boolean transforms; parent-first hierarchy depth; fixture replacement/export propagation; initial copy-budget admission; group placement/cycle rejection; and owned-mesh versus subtree ratios.

Both commands used the shared bounded pure-test launcher, one thread per library, and owned home temporary directories. The source hashes are pinned in the adjacent JSON receipt. The exact corrected source is the committed `43ecea75cadbbbd8f2bab255040eea05fd369982`.

No Blender, browser, Khronos or human artistic review was executed by this reviewer. Final combined regressions, hosted checks, #45/#46/#49 integration and root-controlled normal merge remain separate acceptance gates.
