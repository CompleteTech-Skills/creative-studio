# Animation3D foundation

Issue [#67](https://github.com/CompleteTech-Skills/creative-studio/issues/67) adds a separately versioned document family. It does not complete the timeline, cinematic renderer or parent [#13](https://github.com/CompleteTech-Skills/creative-studio/issues/13).

## Run and inspect

Install the optional `requirements-rig.txt` environment, then:

```sh
python scripts/animation3d.py capabilities
python scripts/animation3d.py validate --file examples/animation3d-contracts/clip.json
python scripts/generate_schemas.py --check
python scripts/test_rig_gate.py schema-math
```

The example files are synthetic contract fixtures. They contain placeholder references and are not complete projects or evidence of rendered geometry. `validate` checks shape and local relationships; it explicitly returns `source_ready: false`. No provider, model, network request, renderer or download runs implicitly.

## Documents and identities

The 13 `profiles/animation3d-*.schema.json` schemas are generated from `animation3d_contracts.schemas()`: project, clip, track, target, layer, state-machine, retarget, constraint, interaction, scene, sequence, render and registry. Root documents require `schema_version: 1` and their specific `animation_*_3d` kind. Standalone target/track/layer/constraint documents wrap the nested record in `value`. Other documents use named nested records. All object fields are closed except bounded, explicitly declared scalar metadata maps. Semantic versions are exact `major.minor.patch`; no mutable `latest` binding exists.

Asset references contain `id`, `version`, portable relative `path`, lowercase SHA-256 and `kind`. Runtime path validation rejects traversal, absolute paths, unsafe platform names and non-NFC spellings. JSON loading uses the shared duplicate-key/nonfinite/UTF-8 diagnostic boundary. The schema describes syntax; runtime additionally checks chronology, unique channels, relationships, normalized rotations, target types, unsafe paths, interval constraints and unsupported operations. An individual reference is not proof that its source exists or that another owner executed it.

Projects retain separate scene/character/rig/clip/state/retarget/sequence references, fixture origin and budgets. Clips retain immutable dependencies, source provenance, events, contacts, root-motion intent and interruption policy. Layers reference source clips instead of rewriting them. State conditions are typed declarative data, never Python or JavaScript. Registry entries name exact target, owner and source plus optional scalar bounds. Scene, render and interaction documents describe integration requests; they confer no native execution or review approval.

## Safe target registry and actual sampling

`TargetRegistry.from_rig(saved_rig_directory, actor_id)` invokes the native `load_rig` authority and creates entries only for actual skeleton bones and represented mesh morphs. Persist its `.document` with the existing `atomic_json` writer. `TargetRegistry(root, document)` takes a detached copy. Every resolution checks portable path/symlink policy, exact `rig.json` hash, native rig source integrity, identity/version and actual bone/morph existence. Caller mutations cannot change registered entries. Explicit `scene3d`, `camera3d` and `lookdev` registry entries resolve saved native project/registry/resource closures through `animation3d_owners.resolve_property`. Scene node transforms/visibility, camera transforms/persisted optics fields, material surface fields and light transforms/color/intensity/spot cone are admitted. Unrepresented properties (including camera exposure/FOV in the current Camera3D owner), environment targets and unknown domains fail explicitly. Admission returns a property value; it does not execute scene animation.

```python
from animation3d_registry import TargetRegistry, sample_track
registry = TargetRegistry.from_rig('output/rig', 'actor')
value = sample_track(track, {'frame': 12, 'subframe_ticks': 0}, registry)
```

`track` is a nested track record matching a real registered target. `sample_track` delegates interpolation to `rig_animation.evaluate_track`; it returns one value, not a composed scene or solved pose. Its output may be assigned through the existing rig pose API, then `evaluate_fk`/`rig_skinning.evaluate_deformation` operate on real vertices. The independent tests do this and measure expected displacement. This adapter does not introduce a second skinning, skeletal, camera or render evaluator.

For a persisted standalone track and registry:

```sh
python scripts/animation3d.py sample --file track.json --registry registry.json --root output/rig --frame 12
```

## Clock, coordinates and interpolation

Frame rates are positive rational integers within `[0.001,1000]` fps, with each term at most 1,000,000. Frames are bounded at 10,000,000 and subframes use exactly 1,000,000 ticks per frame. `seconds` and `sample_time` return `Fraction`; `at_seconds` rejects unrepresentable precision. Samples are `[0,N)`; frame N is available as a terminal key/boundary. `shot_time` requires an explicit terminal flag for the closing boundary. No floating-point accumulation is used. Sampling converts only a local segment duration/offset at the native interpolation boundary.

Canonical transforms reuse `rig_math`: right-handed Y-up meters, XYZW unit rotations, column-vector composition `T @ R @ S`. `animation3d_math` adds bounded curve and clock-adjacent helpers, inverse/world-local conversion, log/exp and intrinsic XYZ Euler composition `Rz @ Ry @ Rx`. Reflections and shear retain the rig owner's rejection behavior. Matrices use row-major Python storage; do not silently reinterpret glTF column-major arrays.

Executed channel interpolation is step, linear, Hermite and quaternion SLERP. Boolean tracks are step-only; rotations are step or SLERP. Hermite tangents are values per frame: an incoming tangent belongs to the preceding Hermite segment, an outgoing tangent to the following one. Tangents unused by either segment reject. Only hold extrapolation currently executes. Other declared interpolation kinds, unused controls and unsupported value combinations reject explicitly. Core standalone Bézier/Catmull-Rom mathematics does not make corresponding track modes available. Key and sampled scale/morph bounds are checked, including Hermite overshoot and narrower registered scalar ranges. Camera optics and LookDev material/light values additionally use their canonical native field schemas. Native object invariants such as OPAQUE surface coverage and spot inner/outer cone ordering also constrain defaults, keys and sampled values; caller bounds can only narrow these domains.

## Exact project admission

```sh
python scripts/animation3d.py admit-project --root saved/project --project animation-project.json
```

The CLI returns the exact inventory, byte total and admission scope as JSON. A failure returns a typed diagnostic and exit status 2.

```python
from animation3d_assets import load_project
receipt = load_project('saved/project', 'animation-project.json')
print(receipt['inventory'], receipt['bytes'], receipt['scope'])
```

This API snapshots exact hash-bound dependencies into a private temporary directory before invoking native owners. Paths, identities, cycles, rational rates, source trims, scene/actor/layer references and native target existence must agree. A standalone skeletal clip needs `rig_binding`; a placed clip can inherit the scene actor's real rig. A clip with its own binding must agree with the actor. Bone masks, root-motion bones, constraint chains, morph targets and retarget bone/signature references are checked against actual admitted rigs. Skeleton signatures are the canonical `rig_errors.object_hash(rig.skeleton)` value. Named clip/layer poses, animated layer/placement weight tracks, modifiers, anchor, blend-tree and environment domains without an explicit owner fail rather than receiving source readiness. Animated weights require a canonical scalar weight-controller target; a skeletal translation or arbitrary represented property is not treated as that controller.

Constraint target actors resolve through their own real rig, target objects resolve actual scene nodes, and chains must follow native parent adjacency. Constraint dependency cycles include native skeletal/node parent edges and reject before readiness. Every typed nested frame coordinate and placement end is bounded by the project frame budget, including auxiliary keys, interaction/ownership ranges and transition durations.

Project admission enforces file/work bytes, track/key/actor/frame counts, per-rig bones, aggregate native rig vertices/morph targets, owner-admitted scene asset vertex inventories and native scene node counts. Scene asset resource counts remain the Scene3D owner’s manifest inventory, not an independent geometry import or geometric approval. Memory admission accounts encoded dependencies and three times expanded NPZ sizes, checks array headers before native NumPy allocation and caps individual JSON documents at 16 MiB. This is a deterministic data admission budget, not an operating-system RSS reservation; callers needing a process memory ceiling must run in a bounded worker. Native owners retain their own independent limits and checks. A source receipt does not survive changed hashes at a later execution boundary.

## Remaining scope

Clips/layers/state execution, retarget solving, constraints/contacts, ownership transfer, camera/light/material evaluation, overlap/dissolve sequencing, editor persistence, exports and rendering remain downstream work. Native project and supported property admission are implemented; unsupported domains remain explicit failures. No complete evaluated scene is produced by this API. No source approval, rendered production acceptance, hosted CI result or human visual signoff is inferred from a local test pass. Keep #67 open until its remaining integration acceptance is reconciled; keep #13 open until its full A01–A42 and demonstrations are verified.
