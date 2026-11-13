# Independent review R6 — PR:newlocal139

- Repository: CompleteTech-Skills/hatch-pet
- Exact HEAD: `5cffd83b8070b7747dee8187f703cbb65b9209b8`
- Whole candidate base: `209569fa29d8be1b25b9a8cd014c26399e68a71b`
- Reviewer: independent `/root/asset139/review139`; parent-provided actual launch configuration `gpt-6-astra`, high reasoning, no history fork. Runtime identity is not independently introspected.
- Verdict: **CHANGES_REQUIRED**.
- Scope: carried-forward whole candidate and integrated decoder; complete R6 diff, modifier admission, actual weight associations, source closure, optional OBJ adapter, tests/receipts/CI. No source edits or GitHub writes.

## Reconciliation

R5's three findings are resolved at their reproduced boundaries:

- Envelope skinning rejects with the explicit unsupported-modifier diagnostic. Preserve-volume and invalid component regressions also pass.
- Both primary and Blender derivative retain all eight influences. Independent replay reads both accessor sets and obtains all eight 0.125 weights; the new suite additionally tests nonuniform joint associations and saved-Blender reimport.
- The case-folded file/directory prefix closure rejects before publication; both discovery orders are covered.

All nine earlier negative external reproduction scripts reject, and the former eight-weight loss reproduction now succeeds with preserved weights. Evidence: `/tmp/hatch-fleet/review139-evidence/r6-prior-repros.json`.

The decoded association comparison is a meaningful addition with correctly disclosed precision and reader identity. It compares after the first native conversion, however, so it cannot establish that every Blender deformation mode survived that first conversion.

## Findings

### P1 — Segmented Bendy Bone deformation still passes linear-skin admission and is lost

Location: `scripts/asset_import_worker.py:skin_bindings` / `enforce_conversion_profile`; first native conversion in `scripts/asset_library_import.py`.

The modifier options are now constrained, but the bound armature's segmented/Bendy Bone deformation is not. Actual source uses one ordinary ARMATURE modifier, a real vertex group with weight 1, `bone.bbone_segments=8`, and static pose `bbone_curveinx=2`, `bbone_curveoutx=-2`. It passes the declared linear vertex-group profile and publishes `imported-payload-verified`.

Actual source evaluation records vertex 0 as approximately `(-0.158114, -0.5, 0.474342)` and a source Z extent up to `2.474342`. Runtime GLB is the undeformed straight skin, with canonical Y extent only 0..2. Its weights/joint names match because the missing segment deformation is not represented by additional glTF joints. The report lists only sampling, unused-world/metadata and numeric-precision limitations, not loss of Bendy Bone geometry. This is far larger than the documented 1e-5 m precision.

Reject segmented/Bendy Bone deformation in the current profile, or use an explicitly supported baking/subdivision adapter and verify evaluated source/output geometry. Native authoring-metadata omission does not explain silently changing visible deformation.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/bendy_repro.py`
- `/tmp/hatch-fleet/review139-evidence/bendy-output.json`
- `/tmp/review139-material-lejbvym1/create/process.log` contains actual evaluated vertices.
- Real source `/tmp/review139-material-lejbvym1/procedural.blend`.

### P2 — OBJ freeform geometry is discarded before the positive Blender object check

Location: `scripts/asset_library_import.py:SourceSnapshot._obj_closure`; worker OBJ import.

OBJ preflight inventories material resources but accepts unknown geometry directives. A genuine OBJ containing a triangle and a cubic Bezier curve (`cstype bezier`, `deg 3`, `curv`, `parm`, `end`) imports successfully. Blender's OBJ importer already discards the curve before scene inspection. The runtime contains only three vertices and one triangle; the curve control geometry at x=10..13 is absent and no loss declaration identifies it.

The post-import MESH/ARMATURE/EMPTY gate cannot detect geometry discarded by the format reader itself. Enforce a documented positive OBJ directive profile before conversion, rejecting unsupported freeform/surface geometry and other unimplemented directives. Optional adapter scope does not justify silently accepting unsupported content.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/obj_curve_repro.py`
- `/tmp/hatch-fleet/review139-evidence/obj-curve-output.json`
- Real source `/tmp/review139-objcurve-ply5p2ys/surface.obj`.

### P2 — Default canonical OBJ import rotates source X/Z unexpectedly

Location: `scripts/asset_import_worker.py`, `bpy.ops.wm.obj_import(... forward_axis='Z', up_axis='Y')`.

With default `basis='canonical'` and unit scale 1, a source OBJ triangle containing vertex `(1,0,0)` is published at approximately `(-1,0,0)` after the Blender-to-glTF export. No requested root normalization accounts for this reversal. The parent confirmed canonical source XYZ preservation is the intended default.

An independent actual Blender 4.5.3 operator probe compared the current `forward_axis='Z'` with `NEGATIVE_Z` through real GLB export and decoded world coordinates. Current Z maps `(1,0,0)` to `(-1,-4.37e-8,-4.37e-8)`; NEGATIVE_Z maps it to `(1,0,0)` and retains `(0,1,0)` within floating precision. Correct the adapter convention and test actual exported vertices/bounds under default and requested normalization.

Evidence:
- `/tmp/hatch-fleet/review139-evidence/obj_axis_probe.py`
- `/tmp/hatch-fleet/review139-evidence/obj-axis-output.json`
- Actual probe files `/tmp/review139-objaxis-dde990lr`.

The parent raised the optional OBJ directive concern during read-only review; the reviewer constructed and executed the actual failure. The coordinate discrepancy was independently observed, then checked against the intended contract and actual alternative operator. The Bendy Bone case was independently identified and executed.

## Tests and evidence

```sh
source /home/complete/.cache/hatch-fleet/environment.sh
PYTHONPATH=scripts:tests /tmp/hatch139-py312/bin/python -m unittest tests.test_rig_asset_import -v
```

**16 tests PASS, no skips, 69.741 seconds**, with actual configured Blender/Khronos. Log: `/tmp/hatch-fleet/review139-evidence/r6-tests.txt`. Ten prior external reproduction scripts were replayed; nine reject as required and the eight-influence case now preserves actual weights. The three new cases above were executed independently.

The R6 pre/post receipt dictionaries match each other and the frozen reviewed Git blobs. The worktree was clean when verification completed. The supplied broader 75-test result was inspected as implementation evidence, not claimed as a reviewer rerun. No full legacy-suite or hosted exact-HEAD CI run is claimed.

## Security, decoder, CI and recommendation

Descriptor-safe source reads, case-prefix admission, fixed application worker, autoexec disabling, process/resource limits and canonical registration remain intact. The complete PNG/strict-JPEG code is unchanged from the integrated scope independently exercised during R5; its former dependency block remains resolved. The current findings are unsupported conversion semantics and coordinates, not texture admission regressions.

The new association verifier does not overclaim independent-reader approval and explicitly states its precision limits. It must remain separate from proving first-source deformation preservation. CI remains additive with pinned real tools, retained logs and pipefail; hosted combined-HEAD acceptance remains pending.

Fix the Bendy Bone and optional OBJ cases with actual regressions, then request a fresh exact-HEAD review and required CI. **Do not merge or close #139 on this verdict.** No approval transfers to later corrections.
