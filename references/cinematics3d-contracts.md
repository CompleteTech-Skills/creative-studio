# Cinematic authoring contracts v1

This is the contract/audit package for [#242](https://github.com/CompleteTech-Skills/creative-studio/issues/242), under [#238](https://github.com/CompleteTech-Skills/creative-studio/issues/238). It implements executable authoring validation, an effect registry, immutable reference closure, effect inheritance and capability admission. It does **not** implement a cinematic renderer. None of the registry's effects is automatically claimed supported. The full parent remains incomplete until real before/after renders, the multi-shot film and independently decoded zero-audio delivery pass.

## Baseline and ownership

The original inspected default head is `d2744d91082f7ce7fe54f2bb13c5985825adadc2`. No repository or ancestor `AGENTS.md` was present. `README.md`, `SKILL.md`, `DELIVERY_STATUS.md`, production contracts/rendering/operations, the retained 2D compositor and exporters, rig contracts/math, Model3D, Scene3D, Camera3D, LookDev and their tests/workflows were inspected. The original environment's plain `python` lacked Pillow; generation failed before any checks ran. The existing isolated Pillow 13 environment then ran the checks listed in the evidence receipt. That environment is not represented as matching the old Pillow 12.3 test pin.

| Owner | Consumed interface or boundary |
| --- | --- |
| #12 Scene3D | `scene3d_contracts.load_project`: exact source closure and scene/shot identities. Cinematic scenes reference owner documents; they do not own geometry or the scene graph. |
| #13 Animation3D | Exact frame intervals and reference handoff only. Keyframe sampling, constraints and animated effect interpolation remain the animation owner and #254. |
| #14 Camera3D | Exact `camera3d-optics` reference for DOF; focal length, f-stop, focus distance, aperture and projection remain in that document. No second optics model. |
| #15 LookDev | Material, lighting and texture execution remain native. This layer reuses portable path and canonical JSON serialization policies. |
| #16 Render3D | `render3d-snapshot` reference; RenderBackend `capabilities/probe/validate/prepare/render_frames/cancel/inspect_version` and ArtifactVerifier `verify(request,result,root)` retain job/pixel ownership. No alternate worker or cache is introduced. |
| #17 Editor3D | Consume schemas, registry defaults/units, immutable preset versions and admission diagnostics. No functioning artist UI is implied. |
| #18 Director | Plan references to native scene/shot, cinematic stack and render request. Task scheduling, claims, budget reservations and publication remain native. |
| #223 Silent exporter | Cinematic render requests require `audio_streams: 0`. Actual encoder mapping, full decode, zero-audio ffprobe enforcement and publication belong to #223/#227/#228 and #257. Schema validity does not verify media. |
| Legacy 2D | `production_contracts.py`, `production_performance.py`, `production_render.py`, `export_assets.py`, `asset_exporters.py` remain unchanged. Pixel coordinates, millisecond atlas timing and audio-capable review films are not reinterpreted. |

Coordinates remain right-handed +Y up, +Z actor forward, meters and XYZW unit quaternions. Camera-local viewing remains Camera3D's -Z axis. This layer introduces no spatial transforms. External transforms must use the canonical `rig_math.BLENDER_FROM_CANONICAL`/Scene3D adapter authorities; never relabel raw Blender Z-up data as canonical. Frames use a positive rational FPS and one million integer ticks per frame. Intervals are half-open; `end` must exceed `start`. No float seconds or legacy millisecond timing is inferred.

## Authoring API and semantics

`cinematics3d_contracts.schemas()` generates seven closed Draft 2020-12 schemas: effect, stack, shot, scene, preset, render and graph. `validate_document` validates the same structural definitions and relational rules. Duplicate JSON keys, nonfinite values, oversized/deep input, unknown fields/versions/effects/parameters, numeric bounds, invalid identities, unsafe portable paths, duplicate IDs, graph cycles and incompatible ports fail. JSON Schema alone cannot prove path NFC normalization, cross-document hashes, reference closure, ordering or cycles; runtime validation is mandatory before use.

`effect_registry('1.0.0')` returns a copy of the immutable v1 definitions, including typed parameter schemas, explicit default values and units, supported scopes, execution stage, required passes, backend feature name and fallback policy. Defaults are an explicit authoring operation (`default_effect`); validation never silently inserts parameters. DOF, LUT and Cryptomatte selectors require exactly one typed immutable input. The DOF input is the native optics document. Other effects obtain existing scene and renderer pass inputs from their owners. No LUT file is decoded in this contract package.

The registry includes depth of field, motion blur, exposure, tone mapping, grading, LUT, bloom, glow, glare, halation, vignette, distortion/dispersion, seeded grain, volumetric fog, depth atmosphere, AO, contact shadows, reflection, refraction, alpha compositing, chroma key and Cryptomatte selection. A listed effect is a defined contract, **not an executed feature**. `default_status` is `unavailable` for every entry.

`Registry([(relative_path, original_bytes), ...], external={path: original_bytes})` admits at most 1024 documents, 2 MiB per document/32 MiB aggregate JSON, 4096 external files/64 MiB external bytes, and rejects portable path aliases, missing or changed bytes, identity mismatches and cyclic references. It performs no filesystem discovery. External bytes are hash-bound opaque owner inputs; callers must invoke native semantic validators and freeze the bytes before execution. `get`, `reference` and `bytes` return copies. Relocating exact bytes preserves identity. Native external IDs retain their uppercase/dot/colon alphabet; authored cinematic IDs use the existing lowercase camera ID policy.

`effective_stack(reference)` resolves at most 64 inheritance levels in scene → shot → object order. Children may replace an inherited effect ID with an explicit new version, or bypass an inherited ID. Unknown bypasses fail. Effects must match their stack's scope. Disabled effects remain preserved in source but do not execute. Enabled effects sort by scene, lens, linear-composite, grade, display, delivery stage and explicit order; ambiguous duplicate stage/order slots fail rather than silently depend on JSON order. Scene, shot and object bindings check their stack scopes.

Preset versions retain the same identity, a strictly increasing semantic version and an exact previous-version reference. Existing bytes are not overwritten by this API. `migrate(value, 1)` verifies and returns a copied v1 document; unknown source/target versions fail. There is no invented legacy v0 conversion. Future semantic changes require a new registry/schema version and an explicit migration preserving prior bytes.

`admission(effects, capabilities)` returns individual `supported`, `approximate` or `unavailable` states. Omitting a capability means unavailable. Approximation requires the effect's explicit permission; unavailable always rejects enabled effects. Disabled effects need no backend support. Capability values come from the caller and are labeled **not independently verified**. Even an admitted result always has `executed: false`: the renderer must still validate source bytes, probe hardware/method/pass support, execute and verify output. This function cannot create evidence or approval.

The v1 graph is a typed routing contract with bounded input/effect/output nodes, named ports, exactly one link per required input, matching RGBA/scalar/depth/vector/normal/matte/Cryptomatte types and no cycles. It does not execute nodes, infer conversion between color/data passes, or claim the Blender compositor mapping required by #252. `pass_contracts()` fixes depth as Euclidean camera distance in meters, motion vectors as pixels per frame (+X right/+Y up at document FPS), normals as canonical-world unit vectors and mattes as [0,1] coverage. Adapters must convert native passes explicitly or reject them; names alone cannot establish parity. Working color is explicitly scene-linear sRGB, alpha is explicit straight/premultiplied, and display encoding is separate. EXR requests reject PQ display encoding; actual color transformation remains backend work.

## Dependency graph and requirement-to-test map

The executable integration sequence remains:

`#242 → #243 → (#244…#251) → (#252, #253, #254) → (#255, #256) → #257 → #258`

#246/#247 and #251 supply color/passes for #252. #257 consumes the #223 silent-export owner; #258 additionally needs native scene, animation, lookdev and rendering programs. Parallel feature development must not create a second scene, camera, material, timeline, geometry or publication authority.

| Requirement/package | Current support and executable check |
| --- | --- |
| #242 strict seven families / A1 | `test_all_families_schema_runtime_roundtrip`, schema generation `--check`, defaults/bounds/types/unknown-field/path/ID tests. |
| #242 immutable closure/history / A3 | Hash, missing/conflicting/alias bytes, identity, inheritance, bypass, scope, ordering and history roundtrip tests. `cinematics3d_example.py` relocates exact bytes and invokes the actual Scene3D loader. |
| #242 capability distinction / A3 | Admission tests cover unavailable, supported, explicit approximate and disabled states; every result retains `executed: false`. |
| #242 baseline/compatibility / A2 | Evidence records original main, environment, schema/math, legacy production tests, unchanged legacy files and failed first environment attempt. |
| #243 backend | Unavailable here; requires real process probing and trusted native worker integration. |
| #244–#245 optics/motion | Registry/reference admission only; no blur or animated focus render proof. |
| #246–#247 HDR/color | Explicit contracts/units; no EXR decoder, OCIO/LUT execution or pixel proof. |
| #248–#250 lens, volumes, materials | Typed requirements only; real scene/render integration and before/after pixel evidence remain open. |
| #251–#253 passes/graph/mattes | Pass names and typed acyclic routing only; no multilayer EXR, Cryptomatte decoder or Blender compositor execution. |
| #254 animated effects/transitions | Exact time representation only; interpolation and continuity execution remain open. |
| #255 review/presets | Immutable preset data only; no functioning UI, preview or production approval. |
| #256 operations | Bounded config admission only; no render scheduler, cancellation, checkpoints or recovery claims. |
| #257 silent media | Zero-audio request invariant only; encoder/ffprobe/full-decode acceptance remains open. |
| #258 complete film/release | No rendered film or visual approval; cannot close the parent from this foundation. |

## Reproduce

```bash
python -m pip install -r requirements-rig-test.txt
python scripts/generate_schemas.py --check
python scripts/cinematics3d_contracts.py --check
python scripts/cinematics3d_contracts.py --registry
python scripts/cinematics3d_contracts.py --validate examples/cinematics3d/exposure.json
python scripts/cinematics3d_example.py
python -m unittest discover -s tests -p test_rig_cinematics3d_contracts.py -v
python scripts/test_rig_gate.py schema-math
```

The contract module itself imports no Pillow, NumPy, Blender, browser, encoder or network client. JSON Schema is imported only when validation is requested. The native gate keeps the cinematic tests visible without adding optional packages to legacy 2D discovery.
