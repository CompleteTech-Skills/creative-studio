# Genuine 3D rigging and skeletal animation

Hatch 3D v1 is an optional local Python engine for actual triangle meshes, hierarchical skeletons, normalized skin weights, poses, animation clips and editable authoring files. Its contracts are separate from the Codex sprite atlas, character decomposition and production scene formats. Existing nine-state Codex timing, `pet.json`, `spritesheet.webp`, profiles V1/V2 and the 416-state catalog retain their original contracts.

## Install and inspect

Use Python 3.12 for the tested dependency set. Install ordinary Hatch dependencies first, then the optional 3D requirements:

```bash
python -m pip install "Pillow>=10.1" -r requirements-rig.txt
python scripts/rig.py doctor
python scripts/hatch.py capabilities
python scripts/rig.py templates list
```

For independent verification and browser tests:

```bash
python -m pip install -r requirements-rig-test.txt
python -m playwright install chromium
npm ci --ignore-scripts --prefix tools/rig
export HATCH_BLENDER=/absolute/path/to/blender-4.5.3-linux-x64/blender
```

The offline review renderer vendors Three.js 0.180.0, with its MIT license and file hashes, under `assets/rig-viewer/vendor`. It makes no CDN request. The optional Khronos validator is pinned to `2.0.0-dev.3.10`. Blender 4.5.3 LTS is the tested authoring application; the checksum and reproducible installation are in [the optional tools guide](../tools/rig/README.md). A CPU is sufficient. Ordinary 2D commands neither import NumPy/jsonschema nor require Blender or WebGL.

## Coordinate, time and identity contract

| Property | Native convention |
| --- | --- |
| Handedness and axes | Right-handed; +X right, +Y up, +Z forward |
| Units | Meters; import scale is explicit and retained |
| Rotation | Radians; unit quaternions ordered X, Y, Z, W |
| Transform composition | Column vectors; local TRS; `world = parent_world × local` |
| Bone orientation | Authored bones use local +Y from head to tail; imported joint orientation is preserved |
| Matrices | Python row-indexed arrays representing column-vector transforms; glTF buffers use column-major MAT4 order |
| Animation | Seconds; per-clip integer FPS is an authoring/bake rate |
| Pose values | Absolute local TRS overrides; omitted properties use rest values |
| Pose `rig_version` | The skeleton version, so weight-only revisions do not invalidate poses |
| Blender conversion | `(x, y, z)` becomes `(x, -z, y)`; a proper +90° X rotation |

Immutable example fixtures and retained validation evidence preserve their authoritative Git bytes across checkouts, including with `core.autocrlf=true`. Source-bound test programs and the existing frozen playback/catalog compatibility files use LF on every platform, as existing script sources already do. This Git attribute policy prevents line-ending conversion from invalidating retained hashes; it does not rewrite original blobs or change historical test/review claims.

Shared JSON ingestion bounds document size in encoded bytes and nesting, rejects duplicate keys and nonfinite numbers, and reports decoder failures as structured `RIG_JSON` errors. This includes the interpreter's decimal-integer conversion limit; it does not disable or raise that resource limit. String payloads count UTF-8 bytes in bounded chunks before parsing, matching byte-payload admission without allocating a full encoded copy. Invalid raw Unicode encoding produces `RIG_JSON`; JSON escape spelling retains its prior parsing semantics. Existing specific duplicate-key, nonfinite, nesting and size diagnostic codes remain distinct. Invalid input is rejected before a CLI configuration/correction operation persists state.

Transforms must be finite and nonsingular. Rest transforms that contain shear or a reflection are rejected for the skeletal core; apply those transforms in an editor first. Static reflected geometry is baked with corrected winding. Original files are copied byte for byte and hash checked. `identity.json` locks the source identity, origin, fixture classification and import scale. An imported procedural fixture stays a fixture when exported and reimported.

The canonical definitions live in `scripts/rig_contracts.py`. Generated Draft 2020-12 schemas under `profiles/rig*.schema.json` cover rigs, templates, clips, poses, retarget profiles, corrections, reviews, jobs and export receipts. Nested definitions cover skeletons, bones, skin bindings and controllers. JSON Schema checks structure and field types; the runtime also checks reference graphs, unit quaternions, bind reconstruction, source hashes and safe local resource paths. See the [contract acceptance audit](rig-contracts-acceptance.md) for the current compatibility baseline and requirement-to-test mapping.

## Import, analyze, fit and bind

```bash
python scripts/rig.py import --model input/character.glb --run-dir output/character --id character
python scripts/rig.py analyze --run-dir output/character
python scripts/rig.py auto --run-dir output/character --template humanoid
python scripts/rig.py inspect --run-dir output/character
python scripts/rig.py weights --run-dir output/character --method auto
python scripts/rig.py validate --run-dir output/character
python scripts/rig.py test-motion --run-dir output/character
```

GLB/glTF import handles real mesh primitives and scene instances, local or embedded buffers, interleaved and sparse accessors, PNG/JPEG textures, material references, UVs, normals, tangents, morph deltas/default weights, compatible skins and animation channels. The importer preserves an existing compatible skeleton. `auto` reports preservation instead of replacing it; replacement needs `--replace`. Unsupported required extensions, inconsistent bind shapes and missing dependencies fail with actionable errors.

When static glTF node transforms are baked into canonical geometry, normals use the inverse transpose while tangent XYZ uses the forward linear transform. Base vectors are normalized, and their morph deltas use the same per-vertex normalization denominator, preserving the direction of the transformed base plus weighted deltas. Reflections retain the existing triangle-winding correction and invert tangent handedness. This assumes an authored valid normal/tangent basis; the importer does not infer UV tangents or repair artistic shading. Direction morphs require their corresponding base attribute, and zero-length direction attributes are rejected. Original source bytes remain unchanged. These attribute checks do not establish renderer or skinned shading equivalence; independent rendering remains a separate gate.

OBJ import handles groups, materials, per-corner attributes, negative indices and planar concave polygons through deterministic ear clipping. It converts bottom-left OBJ UVs to glTF texture coordinates. Self-crossing/nonplanar polygons and partial per-face UV/normal data need explicit source repair. MTL support covers diffuse color, opacity and local PNG/JPEG diffuse textures; bake advanced MTL shader/texture options in an editor. OBJ has no skin or animation data to recover.

Blender input uses a trusted fixed worker with `--background --factory-startup --disable-autoexec`. Make linked libraries local and pack external images before portable import. The original `.blend` remains the source; decoded geometry is derived data. A manifest never supplies executable Python.

Templates are versioned JSON under `rig-templates/`: humanoid, quadruped, winged-creature, robotic and custom. The winged fixture has six legs, a tail and two articulated wings. Templates compose through validated inheritance; duplicate IDs/roles and inheritance cycles fail. The custom template provides a root for explicitly authored anatomy, not a guessed complete animal.

Automatic fitting prioritizes an existing rig, approved source-bound 3D landmarks, labelled mesh region centerlines, then measured bounds. Geometry analysis reports connectivity, bounds, surface area, PCA axes, symmetry evidence and uncertainty. Bounds/PCA do not establish species or anatomical correctness. Uncertain placement remains `needs-correction` until resolved.

Provide landmarks with `auto --landmarks landmarks.json`:

```json
{
  "source_sha256": "64-character hash of the original model",
  "landmarks": {
    "head": {
      "head": [0, 1.63, 0], "tail": [0, 1.94, 0],
      "approved": true, "source": "human-landmark", "confidence": 1,
      "reference": "Reviewed 3D joint placement"
    }
  }
}
```

Semantic mesh region IDs can come from imported `hatchRegion` extras or a `weights --regions` JSON mapping of mesh ID to allowed deform-bone IDs. These are explicit 3D annotations. A 2D mask or image decomposition does not supply hidden 3D geometry, depth, a joint position or approval.

The primary weight method diffuses segment-distance seeds over actual mesh adjacency, then prunes and normalizes influences. Segmented regions constrain candidate bones. Rigid weighting serves mechanical parts. Large or unsuitable diffusion workloads use a recorded bounded distance fallback. Manual vertex locks survive regeneration. Four influences are the portable viewer profile; native data and glTF also support an explicitly configured eight-influence profile, which requires a compatible external consumer.

## Correct and version a rig

`rig.json` is the active manifest. Geometry/weights/clips live in immutable content-addressed files. Previous manifests and correction documents remain under `rig/revisions` and `rig/corrections`. Every mutation promotes the final manifest atomically after dependent files are written. A failed promotion leaves the prior active revision usable.

The correction envelope is strict and contains `rig_id`, `rig_version`, `expected_sha256`, author, reason, timestamp and ordered edits. Supported edits cover bone add/remove/update/rename/reparent, limits and IK chains, sockets, facial controllers/expressions, weights and template replacement. Bone changes invalidate active skins, animations, state mappings and reviews while preserving their old files. Weight changes retain compatible channels but invalidate deformation evidence and review.

```bash
python scripts/rig.py review-ui --run-dir output/character --destination output/review
python -m http.server 8765 --bind 127.0.0.1 --directory output/review
python scripts/rig.py review-apply --run-dir output/character --receipt rig-correction.json
python scripts/rig.py weights --run-dir output/character --method auto
```

Before a correction writes history or a revision, it validates every declared IK chain against the corrected skeleton using the same declaration checks as validation/export. Missing or repeated bones, disconnected hierarchy, non-unit orientation and evaluated rest segments at or below `1e-8` meters are rejected. Rejection preserves the active manifest, immutable assets, correction history and caller's rig. The operation dispatcher retains a failed-job diagnostic separately; it does not promote a correction. Valid skeleton edits can still invalidate skins and animation, and need not pass animation-readiness validation before saving.

The viewer downloads a correction bound to the current revision. A later edit makes that correction stale. `bone-edit --bone ID --file fields.json --author NAME --reason TEXT` creates the same bound operation from CLI fields. Use `weights-inspect --mesh ID --vertex N`; JSON weight edits can assign normalized weights, smooth, copy, explicitly mirror reviewed vertex pairs, lock or unlock vertices. Mirroring never silently invents symmetry.

## Pose, IK, face and objects

FK evaluates parent-to-child TRS without mutating the rest pose. Analytical two-bone IK uses a pole target; FABRIK handles longer chains. Both measure the actual evaluated endpoint, handle unreachable targets and obey bounded iterations. Constraints include hinges, swing/twist, ball limits, locked rotation axes, translation limits, aiming and transform copying, with dependency-cycle rejection.

Structural validation checks every declared IK chain, even when no clip or contact uses it. Chain IDs must be unique, bone references must exist, the ordered bones must follow direct parent relationships, and an optional target orientation must be a valid unit quaternion. Every evaluated rest-pose segment between consecutive joint origins must be strictly longer than `1e-8` meters, matching the existing solver threshold; positive authored bone head-to-tail lengths alone do not establish this separation. Both two-bone and FABRIK solvers also enforce that same length guard on the supplied runtime pose. GLB/glTF export runs declaration checks before writing output; Blender export uses that same validated native export boundary.

```bash
python scripts/rig.py pose --run-dir output/character --clip wave --time 0.75 --destination output/wave-pose.json
python scripts/rig.py solve-ik --run-dir output/character --chain arm.right --target 0.35 1.25 0.45 --destination output/reach.json
```

The programmatic facial layer mixes authored morph/bone expressions with explicit intensities and incompatibilities, resets to neutral, and aims existing eye joints with vergence and optional head follow. Finger controls articulate existing segmented finger bones. Tail and wing controls use bounded deterministic parameters; secondary tail motion uses an explicit seed. Missing anatomy raises an unavailable-capability error.

`facial-controls --run-dir RUN` reports availability for neutral, smile, sadness, anger, surprise, fear, squint, blink, independent winks, brows, jaw and mouth-corner controls, plus the 15-symbol viseme vocabulary `sil PP FF TH DD kk CH SS nn RR aa E ih oh ou`. Only existing labelled morphs are mapped; the report explicitly identifies unavailable controls. Author actual targets with `create_morph_target` / `edit_morph_target`, or a `morph` correction containing exact topology-matched deltas/positions. A target-count change invalidates affected weight-animation channels. Qualified expression keys such as `eye-white-left::blink` provide independent left/right control. Resetting expressions clears their controlled morphs/bones; `reset=False` applies a partial overlay. Passing `None` to `gaze_at` releases gaze back to neutral.

Sockets use actual evaluated bone transforms and local offsets. Object-relative grasping solves both endpoint position and orientation while accounting for the hand socket. Attach/detach can preserve world position. A handoff requires the current owner and measured world-space contact with the destination hand. Ownership is single-valued; a claimed contact never moves an unrelated prop implicitly.

## Clips, layering, contacts and retargeting

Animation clips contain real translation/rotation/scale/morph channels, ordered keys, duration, loop/interrupt metadata, events, contact intervals, root-motion policy and provenance. LINEAR rotations use SLERP; STEP holds the prior value; CUBICSPLINE uses Hermite tangents and normalizes quaternion output. Negative/nonfinite times, duplicate channels, invalid joints and overlapping contradictory contact targets fail.

`animate --file clip.json` imports the native contract, or accepts `{id, keyframes:[{time,pose}], fps, loop, contacts, events}` authoring data. `animation-import --source file.glb` imports compatible named channels. `animation-bake --clip NAME` evaluates controls into ordinary channels. Unconstrained STEP/cubic clips retain exact interpolation. Constrained STEP clips require explicit stepped authoring instead of silently becoming linear. Baked continuous channels are resampled at the declared FPS; comparisons measure the approximation.

The Python API supports masked pose layers, additive rest-relative deltas, morph mixing and aligned crossfades. Embedded root motion can accumulate between loops. World-space foot/paw contacts re-solve IK through stance intervals; contact and release events remain in the native clip. An exported GLB contains the resulting baked motion, not live contact solvers.

```bash
python scripts/rig.py retarget --source output/source-character --run-dir output/target-character --clip walk
python scripts/rig.py render --run-dir output/target-character --clip walk-retargeted --time 0.7 --destination output/retarget.png
```

Retarget profiles bind source/target identities and skeleton versions. Semantic roles map bones; calibrated rest orientations and measured proportions adapt motion. Root scale and limb/local translation adaptations are explicit. Compatible contact chains are re-solved on the target; missing roles are omissions only when partial retargeting was explicitly selected. Cross-archetype retargeting is not automatically considered valid. Inspect deformation and semantic suitability after numerical checks.

## Catalog and optional 2D bridge

```bash
python scripts/rig.py state-map --run-dir output/character --state walking-forward --state waving
python scripts/rig.py state-map --run-dir output/character --pack PACK_ID
python scripts/rig.py export --run-dir output/character --format frames --clip walk --destination output/walk-frames
```

Mapping calls the actual unchanged catalog resolver and anatomy/prop compatibility rules. Definitions with no implemented channels report `requires-animation`; absent anatomy reports `requires-rig-capability`; executable unreviewed recipes report `needs-review`. Priority, interruption, recovery, cancellation and reduced-motion metadata are retained separately. No 3D clip is generated merely because a catalog definition exists. The original `running` state still means task processing.

The transparent frame exporter renders actual evaluated triangles using a fixed shared camera and scene bounds, retains per-frame seconds/duration/source hashes, and provides an animated WebP preview. CPU evidence rendering shades material base colors; use Three.js or Blender for texture-complete inspection. The `frames.json` bridge declares derived origin and fixture/review status. Feed selected frames through the existing explicit image/frame import workflow and its normal profile, atlas and review gates. The 3D bridge does not change Codex geometry or automatically grant 2D artwork approval.

## Exports and independent evidence

```bash
python scripts/rig.py export --run-dir output/character --format glb --destination output/character.glb
python scripts/rig.py export-validate --file output/character.glb
python scripts/rig.py export --run-dir output/character --format blender --destination output/character.blend
python scripts/rig.py render --run-dir output/character --engine blender --clip walk --time 0.65 --destination output/blender-walk.png
python scripts/rig.py archive --run-dir output/character --destination output/character.zip
python scripts/rig.py restore --file output/character.zip --destination output/restored-character
```

GLB/glTF exports contain actual skins, JOINTS/WEIGHTS attributes, column-major inverse-bind matrices, parented joint nodes, materials/textures/morph targets and executable channels. Both GLB and embedded-buffer `.gltf` are supported. The latter is self-contained for atomic promotion. IK controls, constraint solvers and contact systems remain native authoring information; the portable export carries evaluated results.

Blender export imports the real animated GLB, retains editable armatures/vertex groups/skin modifiers/materials/shape keys and named action slots, adds target/pole controls and socket definitions, saves `.blend`, then reopens it in a separate process. The adapter compares independently evaluated joint and vertex samples and re-exports a GLB. Baked playback is the default. Enable the armature's `hatch_live_ik` property to edit live target controls. Native limit types and exact socket semantics are retained in the embedded manifest; not every native control has an equivalent Blender authoring constraint. See the explicit limitations before round-tripping authoring controls.

`render --engine blender` produces an actual Cycles CPU render of the evaluated pose through the fixed worker, with transparent alpha, a fitted camera and the imported materials. The Python API `render_blender_preview(rig, destination, clip='walk', times=[0, .65], blend_path=...)` can render multiple samples from an existing saved project with one camera fitted across all samples. It retains each frame's time/hash and the exact `.blend` hash. Dimensions, sample count, estimated scene buffers and subprocess time are bounded. Saved-project changes should be reviewed against their own recorded hash.

The independent checks have separate scopes: Khronos validates glTF semantics; Blender verifies editability and independently evaluated deformation; Three.js loads the file and evaluates real skinned vertices while browser automation exercises playback/overlays. A successful self-import alone is not independent evidence.

`profiles/rig-export.schema.json` is the strict version-1 contract for the receipts returned by `export_gltf` and `export_blender`. Native GLB/glTF receipts retain the output/rig hashes, mesh and channel counts, and `independently_verified: false`; a later independent validator has its own report. The native `joints` count includes every exported skeleton bone/node, including a nondeforming root; it can exceed the union of glTF `skins[].joints`. Blender receipts retain their existing application, armature, action, sample, command and hash fields, and add `schema_version: 1` and `format: "blend"`. The Blender `bones` count measures the actual imported armature. A reopened Blender receipt must contain the numerical comparison and round-trip file evidence. The Python `verify=False` option produces a creation-only receipt with `reopened: false` and no reopen comparison fields. `independent: true` identifies the external Blender reader and does not override that distinction. Schema conformance alone does not establish artifact integrity or artistic approval. Frame manifests and portable archive inventories keep their separate existing contracts.

## Review UI and visual acceptance

The offline viewer provides orbit/pan/zoom, clip/time/speed/pause/frame stepping, skeleton overlays and bone selection, FK controls, world-space IK target controls, wireframe/material and selected-bone weights, morph sliders, contact/socket markers, comparison GLB loading, JSON pose/correction download and viewport capture. The browser IK editor implements analytical two-bone and bounded FABRIK editing with ball/hinge limits; saved poses are rechecked by the full native constraint engine. Browser looping restarts the displayed root trajectory; the native evaluator supports accumulated root motion.

Visual receipts cover skeleton placement, bone semantics, deformation, joint motion, facial animation, contacts, continuity, identity and interchange. Every dimension needs an explicit verdict and note. Evidence paths/hashes and the core rig hash bind the review. A synthetic test receipt cannot approve the rig. A human/agent review of an engineering fixture can only approve that fixture; it cannot convert the source into production artwork.

## Python API

Add `scripts` to the Python path and import `rig_api` for the stable v1 facade. `Geometry`, `Mesh`, `SkinWeights` and `RigAsset` carry actual NumPy arrays; boundary files use the strict schemas. `RigError.report` is the structured error contract.

```python
from rig_api import load_rig, evaluate_animation, evaluate_deformation, validate_rig

rig = load_rig("examples/rig-3d/humanoid")
evaluated = evaluate_animation(rig, "walk", 0.65)
vertices = evaluate_deformation(rig, evaluated["pose"])
assert vertices["torso"]["positions"].shape[1] == 3
assert validate_rig(rig, require_animation=True)["ok"]
```

The facade exports model import/analysis, templates/skeleton generation, corrections, weights, FK, IK, deformation, animation/layers/blends/baking, retargeting, validation/range testing, rendering, GLB, Blender and portable packaging. Focused modules expose facial controls, sockets/grasps, state mapping and operations. Examples and numerical expectations are executable in `tests/test_rig_*.py`.

## Local HTTP API

```bash
python scripts/rig.py serve --run-dir output/rig-jobs --intake input/models --port 8766
```

The server binds only `127.0.0.1` and prints its local bearer token. Send `Authorization: Bearer TOKEN` and `Content-Type: application/json`. Cross-origin requests are refused, intake reads stay inside the chosen folder, exports stay inside the selected job, JSON bodies are limited to four MiB, connections time out, and two workers are available. There is no remote/public-server mode.

| Method and route | Behavior |
| --- | --- |
| `POST /v1/rig-jobs` | Import `{model, id?, scale?, fixture?, origin?, attribution?}` from intake |
| `GET /v1/rig-jobs/{id}` | Current status, versions, counts and bounded step journal |
| `POST /v1/rig-jobs/{id}/generate` | Template/landmark skeleton construction |
| `POST .../skin`, `/analyze`, `/validate`, `/test-motion` | Actual core operations |
| `POST .../animate`, `/state-map`, `/configure` | Author channels, select catalog recipes or lower/configure limits |
| `POST .../preview`, `/export` | Local inspection bundle or real mesh/Blender/frame export |
| `GET .../artifacts` | Relative paths, sizes and byte hashes |
| `GET /v1/rigs/{id}` | Strict active rig manifest |
| `PATCH /v1/rigs/{id}` | Apply a complete hash-bound correction envelope |
| `POST /v1/rigs/{id}/pose`, `/solve-ik` | Evaluate a native pose or spatial target |
| `POST .../retarget`, `/review` | Retarget from another local job or apply a bound review receipt |

Operation parameters are shared with `rig_operations.py`; unknown mutation fields fail. A stale correction returns a conflict, missing jobs return 404, unauthenticated requests 401, and occupied workers 429. Tests issue real local HTTP requests rather than stubbing route behavior.

## Resource limits, jobs and recovery

Defaults: 256 MiB encoded source, 1 GiB dependencies/archive expansion, one million vertices, two million triangles, 256 bones, four influences, 128 morph targets per mesh, 64 clips, 250,000 total authored keys, 120 seconds per clip, 60 FPS, two GiB estimated processing memory, 128 solver iterations, 300 seconds per external/render operation and three attempts for a failed step.

`configure --file limits.json` uses strict bounded overrides. `status` shows active/pending/failed/stale work. `resume` retries failed/interrupted operations within the attempt budget. An OS file lock gives one writer ownership; claims survive in the journal and lock ownership releases after a process crash. Cache identity covers the core rig, parameters, implementation files and retained artifact hashes. Changing a covered input makes prior results stale. Failed exports never count as independent acceptance.

Archive inventory covers every included file, hash and size. Restoration rejects path escapes, aliases/collisions, links, encrypted/special files, excessive expansion and unexpected/missing members, verifies all bytes in a fresh temporary directory, loads the actual rig, then promotes the directory. A restored fixture is evaluated again by the demonstration gate.

Restoration requires a new destination and rejects destination symlinks, including those with missing targets. If another writer creates that destination before publication, even as an empty directory or symlink, restoration fails, preserves the competing destination, and removes only its own temporary extraction directory.

## Troubleshooting

| Error | Action |
| --- | --- |
| `RIG_DEPENDENCY`, `RIG_BLENDER_MISSING`, `RIG_VALIDATOR_MISSING` | Run `rig doctor`; install only the optional tools needed for that operation |
| `RIG_ANATOMY_UNCERTAIN` / `needs-correction` | Inspect the mesh; provide source-bound landmarks or correct the skeleton |
| `RIG_STALE_CORRECTION`, `RIG_STALE_REVIEW` | Rebase the edit/review on the current version and regenerate covered evidence |
| `RIG_SOURCE_CHANGED`, `RIG_IDENTITY_CHANGED`, `RIG_HASH` | Restore exact retained bytes or import the intentional new source as a new identity/run |
| `RIG_BIND_SHAPE`, unsupported extension, shear/reflection | Decode/apply the source transforms in an editor; preserve the original source alongside the derived input |
| `RIG_MISSING_SKIN`, `RIG_WEIGHT_*` | Bind every mesh, correct references/weights, and regenerate only affected evidence |
| `RIG_IK_*`, unreachable/residual report | Correct the chain/pole/limits or move the target inside the actual reachable workspace |
| `RIG_REVIEW_UNCERTAINTY` | Resolve the recorded uncertainty through explicit anatomical corrections |
| `RIG_LIMIT_*`, `RIG_RENDER_BUDGET` | Reduce mesh size, key count, frame dimensions or selected scope |
| `RIG_BUSY`, `RIG_API_BUSY` | Let the current writer finish; use status/resume instead of starting duplicates |
| `RIG_ATTEMPTS` | Correct the failing input/parameters before a new attempt series |

## Reproduce the engineering demonstrations

```bash
python scripts/verify_rig_demos.py --generate --destination output/rig-demos \
  --blender --browser --validator --render --archives
HATCH_RIG_INTEROP_TEST=1 HATCH_RIG_BLENDER_TEST=1 HATCH_RIG_BROWSER_TEST=1 \
  python -m unittest discover -s tests -p 'test_rig_*.py' -v
python scripts/generate_schemas.py --check
python scripts/generate_rig_templates.py --check
```

Generation requires a new destination. Verification can be rerun without `--generate` on an existing set. The retained fixtures, source GLBs, real animation channels, pose sheets, transparent frame sequences, browser screenshots, editable Blender files, independent reports and archive receipts are indexed by `examples/rig-3d/acceptance.json`. Consult [acceptance and limitations](3d-rigging-acceptance.md) before treating any engineering fixture or gate as a production-art approval.

The public production delivery command remains scoped to all existing 2D tests by default (`--tests --test-scope 2d`), so its pinned environment does not need NumPy, pygltflib or Blender. It filters only the new `test_rig_*.py` modules before import and retains strict skip accounting. Use the focused 3D command above and the nine named CI jobs for 3D acceptance. The delivery runner also accepts `--test-scope all` when the complete 3D environment and test flags are enabled; unexpected integration skips still fail that gate.

### Authored landmark admission

`rig auto --landmarks FILE` accepts a source-bound object containing exactly
`source_sha256` and `landmarks`. Each bone-keyed landmark requires `head` and
`tail` arrays of three finite JSON numbers and `approved: true`. Numeric strings,
booleans, unknown bone IDs, missing coordinates and unsupported fields fail with
a typed diagnostic. The existing bone contract validates evidence source,
nonempty reference and confidence in `[0, 1]` before the fitter returns a status.
Landmarks take precedence over semantic regions and bounds; geometric sources
or confidence below `0.9` retain an explicit anatomy uncertainty. This validates
supplied annotations; it does not independently establish anatomy approval.

The `test_rig_landmarks.py` regression exercises the real mutation dispatcher,
valid source-bound fitting/reload and rejection with unchanged active manifest,
immutable revisions and original source bytes. It covers the landmark-admission
part of #52; broader import, template composition and native acceptance remain
separate requirements.
