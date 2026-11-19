# Character decomposition

Turn a single existing character image into observed visual components, source-space masks, editable semantic metadata and reusable generation references. The original encoded image remains unchanged. This format is separate from the fixed nine-state Codex pet package.

## Contents

- [Capabilities and evidence](#capabilities-and-evidence)
- [Start a decomposition](#start-a-decomposition)
- [Supply semantic annotations](#supply-semantic-annotations)
- [Coordinates, hierarchy and joints](#coordinates-hierarchy-and-joints)
- [Inspect and correct](#inspect-and-correct)
- [Recomposition and quality gates](#recomposition-and-quality-gates)
- [Explicit assisted reconstruction](#explicit-assisted-reconstruction)
- [Export, reload and forward reuse](#export-reload-and-forward-reuse)
- [Package structure and limits](#package-structure-and-limits)
- [Acceptance evidence and real-image protocol](#acceptance-evidence-and-real-image-protocol)

## Capabilities and evidence

| Capability | Implemented route | Meaning of its evidence |
| --- | --- | --- |
| Foreground detection | Original alpha, edge-connected solid-color removal, explicit foreground masks, rectangular character selection | Deterministic geometry; complex backgrounds remain unresolved until a mask is supplied or corrected |
| Semantic components | Source-bound labeled masks; polygons, painted masks, or explicitly labeled color/seed selection | A supplied label expresses a user's or analyzer's interpretation; it still requires review |
| Extraction | Binary ownership masks applied to original RGBA pixels once | Exact observed appearance and transparency inside each visible mask |
| Hidden anatomy | Explicit assisted reconstruction handoff and verified returned artwork | Inferred content is separately identified; original observed component assets remain available |
| Rig metadata | Hierarchy, anatomical side, source-space pivots/attachments and low-confidence pivot suggestions | Partial cutout metadata; no claim that one image provides a complete rig |
| Reuse | Strict identity prepopulation and ordinary forward-run preparation | Required original/component references retain source hashes, component metadata and review status |

The installed local pipeline does **not** contain a general-purpose semantic vision model. Running `decompose` without annotations creates a faithful, reviewable foreground with unassigned semantic regions. It never labels connected regions as heads, limbs or accessories merely because of their position. `analysis-request.json` records the actual normalized reference and expected annotation contract for optional external assistance; it does not represent a completed model call.

An optional [resumable image-analysis controller](decomposition-analysis.md) can now execute the real local OpenCV human face/eye detector or ingest attributed external proposals. Its coarse photographic-region support is not a general-purpose anatomy model; it preserves pending review and unassigned pixels.

An optional [foreground instance stage](decomposition-foreground.md) runs the pinned local Torchvision COCO model for source-sized person/animal/object masks and explicit multi-subject selection. It does not identify anatomy, replaces no source pixels, and leaves selected components semantically unassigned.

The complete manual route is available offline and requires Pillow only. A separate analyzer may inspect the source and return the documented annotations. Treat its output as proposed annotations with real author/tool attribution, confidence and rationale. Do not fabricate a provider, API, model call or semantic success receipt. Semantic labels remain flexible: humanoids, quadrupeds, wings, robot housings, masks, symbols and abstract designs all use the same data-driven schema.

## Start a decomposition

```bash
python scripts/hatch.py doctor
python scripts/hatch.py decompose \
  --source ./character.png \
  --run-dir ./output/character-decomposition \
  --origin "User-supplied character artwork; creator and permission recorded by the user"
python scripts/hatch.py decompose-review-ui \
  --run-dir ./output/character-decomposition \
  --destination ./output/character-review
```

Open the exported `review.html` locally. The starting manifest's semantic review is pending. Identify applicable components there or supply a full annotation file at creation.

Single-frame PNG, JPEG and WebP inputs are accepted. Animated files, SVG, GIF, video, empty images and files beyond the documented limits fail clearly. The importer archives original compressed bytes at `source/original.EXT` and records their SHA-256, then writes decoded RGBA `original.png`. These hashes serve different purposes: annotation identity binds the **original file bytes**, while normalized/raster hashes protect managed output pixels. Metadata includes the original format, dimensions, mode, attribution and EXIF orientation. EXIF orientation is recorded without silently rotating source-space pixels; inspect oriented JPEGs before assigning anatomical sides.

`--background auto` uses existing alpha as foreground evidence when at least **1% of source pixels are fully transparent**. A few damaged or fractional-alpha pixels do not establish a background boundary. Otherwise, a near-uniform edge can support a proposal for edge-connected background removal. The operation preserves source RGB rather than repainting or decontaminating edges. A complex background is retained with an explicit unresolved warning until supplied evidence distinguishes foreground membership. Use `--foreground-mask ./mask.png` to provide a full-canvas binary mask, `--background alpha` to use alpha explicitly, or `--background keep` to retain the complete image deliberately. An opaque retained image still needs foreground review.

Use `--pixel-art` for pixel artwork. Extraction never resizes the source or individual parts. Pixel previews and newly prepared forward profiles use nearest-neighbor handling, so hard pixel boundaries and palette entries are not smoothed. Fractional source alpha in other artwork is retained exactly; a segmentation mask is membership, not a second opacity layer.

Disconnected large regions cause a **multiple-character suspicion**, not a semantic assertion. They may be two figures, a detached hand, lettering or floating details. Select a character using half-open source bounds:

```bash
python scripts/hatch.py decompose \
  --source ./two-characters.png \
  --run-dir ./output/first-character \
  --select "0,0,400,600"
```

Run another decomposition with the second selection for separate output packages. Each keeps the original full canvas, bytes and provenance; selected-out regions remain in the archived original. Explicit component annotations can group disconnected geometry into one logical character instead. Overlapping figures sharing a single silhouette require a semantic foreground mask; connected-component statistics cannot identify that boundary.

### A complete runnable annotated example

The repository includes a hand-authored creature and exact masks at [examples/decomposition/creature](../examples/decomposition/creature). Its source hash is already bound in `annotation.json`:

```bash
python scripts/hatch.py decompose \
  --source examples/decomposition/creature/source.png \
  --annotations examples/decomposition/creature/annotation.json \
  --fixture-mode \
  --run-dir output/decomposed-creature
```

For decomposition, portable export/reload and preparation of a normal forward run in one deterministic exercise:

```bash
python examples/decomposition/build_fixture.py \
  --case creature --output-dir output/decomposition-creature
python examples/decomposition/build_fixture.py \
  --all --output-dir output/decomposition-fixtures
```

Choose a new output directory. The exercise extracts real transparent files and leaves component approval and new animation artwork pending. `acceptance.json` explicitly records fixture origin, zero model calls, zero generated animation frames and the exact inspection paths. Synthetic authored masks prove the mechanics of this route, not arbitrary image understanding.

## Supply semantic annotations

`profiles/decomposition-annotations.schema.json` defines the strict version-1 input. `schema_version`, original `source_sha256`, `author`, `note`, and `components` are required. Optional `orientation`, `foreground_mask`, and `extensions` complete the envelope. Compute an original file's hash with:

```bash
python -c "import hashlib,pathlib; print(hashlib.sha256(pathlib.Path('character.png').read_bytes()).hexdigest())"
```

The following component fragment belongs inside `components`; use only parts actually supported by the artwork:

```json
{
  "id": "anatomical-left-eye",
  "name": "Anatomical left eye",
  "category": "face/eye",
  "parent_id": "head",
  "z_index": 30,
  "side": "left",
  "mask": {"path": "masks/left-eye.png"},
  "pivot": [72, 38],
  "attachments": [],
  "confidence": 0.94,
  "occlusion": {
    "status": "visible",
    "occluded_by": [],
    "note": "The complete visible eye is present; its rear surface is not observable."
  },
  "notes": ["Front-facing character: anatomical left appears on the viewer's right."]
}
```

Paths resolve relative to the annotation file. A component requires `id`, `name`, `category`, and `mask`; the remaining fields have conservative defaults. IDs are portable lowercase slugs, while categories are extensible text such as `anatomy/tail`, `face/eyelid`, `clothing/hat`, `accessory/tool`, or `detail/marking`. Categories are not a mandatory anatomy checklist. Do not add nonexistent parts to fill a taxonomy.

Every mask uses full original-canvas coordinates and one representation:

| Representation | Contract |
| --- | --- |
| `{"path":"part.png"}` | A lossless full-canvas PNG in mode `1` or `L`, with membership values exactly 0/255; fractional masks and implicit scaling are rejected |
| `{"runs":[[y,x0,x1],...]}` | Sorted, nonoverlapping horizontal spans with exclusive `x1`; suitable for saved brush edits and portable annotations |
| `{"polygons":[[[x,y],...],...]}` | Union of bounded source-space polygons; useful for drawing irregular outlines or multiple regions |
| `{"colors":{"values":["#AABBCC"],"tolerance":0,...}}` | Explicitly labeled deterministic color selection, optionally limited by seed points and `within` polygons; no semantic name is inferred from the color |

Use irregular masks for irregular parts. A bounding box is placement metadata, not sufficient segmentation. Rectangular component masks remain possible for genuine rectangular pixel/robot features, but produce an inspection warning. A mask can include several disconnected regions: both eye highlights or several spots may be one logical detail. Conversely, one connected face/body silhouette can contain many individually named masks.

At overlap, highest `z_index` owns visible source pixels; IDs deterministically break ties. The manifest records proposed overlap count, affected occlusion metadata and uncertainty. Effective observed masks are disjoint so every foreground pixel is composited exactly once. Low-confidence or overlapping proposals remain pending for review. Entirely hidden components cannot masquerade as extracted assets: a component with no supported visible pixels is rejected.

Any unassigned foreground is retained in a separately labeled `unassigned` component. This protects pixel coverage and makes missing semantic work visible. Such a component cannot receive semantic approval until identified or its mask reassigned. Strict validation rejects unknown top-level fields, invalid geometry, duplicate IDs, impossible references and hierarchy cycles. Add application-specific data beneath `extensions`; extensions are preserved data and are never executed.

## Coordinates, hierarchy and joints

| Field | Coordinate convention |
| --- | --- |
| Canvas | Original decoded width and height; neither cropping nor normalization changes this coordinate system |
| `bbox` | `[x0,y0,x1,y1]`, integer source bounds, exclusive right/bottom; transparent component PNG dimensions are `x1-x0` by `y1-y0` |
| `transform.position` / `local_origin` | Source-canvas position of the cropped asset's local `[0,0]` |
| `transform.rotation` / `scale` | Faithful extraction starts at `0` degrees and `[1,1]`; alternate edited previews must not replace source-fidelity evidence |
| `pivot` | Explicit source-canvas point or `null`; convert to crop-local coordinates by subtracting the bounding-box origin |
| `attachments[].point` | Source-canvas point with optional existing `target_id` and confidence |
| `parent_id` / `z_index` | Logical relationship / independent back-to-front drawing order |

Use anatomical left/right from the character's perspective, and record its orientation. If anatomy or facing direction is ambiguous, use `unknown` and describe the uncertainty. Do not map viewer-left to anatomical-left silently. A shared visual detail can use `center` or `unknown`; it need not be split merely to obtain paired labels.

Plausible pivot suggestions are intentionally weak evidence: the current geometric heuristic uses proximity to a parent's bounds and reports low confidence. It does not detect anatomical joints. The review desk can set inspected pivots and attachment points, but `rig.animation_ready` remains false. A complete animation rig would additionally require validated separation, hidden geometry, usable joint behavior and target-specific motion tests; those are not established by static cutouts.

## Inspect and correct

The decomposition review desk extends the same offline snapshot-and-receipt workflow used for generated states:

```bash
python scripts/hatch.py decompose-review-ui \
  --run-dir output/decomposed-creature --destination output/creature-review
```

Inspect the original beside the layered view and select, hide or isolate components. Zoom the source, overlay masks, paint or erase membership, split a mistaken grouping, and merge regions that form one logical detail. Edit names, categories, parents, anatomical side, front-to-back order, pivots and attachment points. Mark unresolved or inferred geometry and approve/reject each component with evidence notes. Recompose after changes and check newly exposed gaps rather than guessing the hidden body behind a moved layer.

Use the desk's undo controls while editing. Download the correction/review receipt, then apply that exact file:

```bash
python scripts/hatch.py decompose-review \
  --run-dir output/decomposed-creature --receipt output/creature-review-receipt.json
python scripts/hatch.py decompose-validate --run-dir output/decomposed-creature
```

Downloading alone does not mutate or approve the run. Receipts bind the current manifest and the original source hash; stale receipts fail. Revisions retain accepted annotations, original asset evidence and correction history. Both mask corrections and metadata-only reviews stage their related manifest, identity, validation and provenance updates before publication; interrupted replacement uses the same checked recovery path. Changed component content/metadata invalidates its approval; unchanged component signatures can retain their verdicts. Rejection is repairable locally by correcting that component and submitting a fresh receipt, without another whole-image extraction or model call.

Restore an earlier revision through the controller:

```bash
python scripts/hatch.py decompose-restore \
  --run-dir output/decomposed-creature --review-revision 1 \
  --author "Character reviewer" --note "Restore the previously inspected component boundaries."
```

Restoration creates a new revision preserving history rather than deleting later evidence. A label, confidence number or checkbox is an attestation; the UI cannot prove that someone inspected the image. Numerical QA never supplies a semantic pass.

## Recomposition and quality gates

`decompose-validate` checks current files, source/asset hashes, strict schema, mask ownership, placement, review signatures and faithful reconstruction. `decompose-recompose` writes an explicit flattened result:

```bash
python scripts/hatch.py decompose-recompose \
  --run-dir output/decomposed-creature --destination output/creature-recomposition
```

The default faithful tolerance is exact: maximum channel error **0**, source-pixel coverage **1.0**, and unexpected effective overlap **0 pixels**. Alpha must match exactly. Source foreground membership is separately inferred or supplied; exact reconstruction of a poorly chosen foreground mask does not validate that mask's semantics.

`validation.json` reports maximum visible channel error, RMSE, differing pixel count, foreground coverage, unassigned regions, gaps, unexpected overlap, alpha preservation, component clipping, edge discrepancies and inferred-content pixel counts. Proposed overlap diagnostics are separately recorded before visible ownership resolution. Source-quality warnings cover low resolution, boundary contact, low contrast, partial alpha and unresolved backgrounds; they are diagnostic heuristics, not a certified perceptual image-quality score. Quality data uses a strict typed schema, and validation recomputes both quality and rig metadata from the retained source and components. Editing those claims independently cannot produce a valid saved or loaded manifest.

Previews include original/foreground/reconstructed comparison, transparent foreground, overlay, amplified pixel differences, component contact sheet, source-space boxes and color-coded mask coverage. Full-resolution rasters remain available even when comparison panels are bounded for display. A rejected semantic component may still pass pixel fidelity: matching the source and identifying the correct parts are independent judgments.

## Explicit assisted reconstruction

Faithful mode never invents missing torso, hair, limbs or hidden surfaces. To request inferred missing pixels, create the run explicitly in assisted mode using `--decomposition-mode assisted`. Existing observed component assets remain authoritative and unchanged. Request one component at a time:

```bash
python scripts/hatch.py decompose-reconstruction-request \
  --run-dir output/assisted-character --component torso \
  --note "Reconstruct the torso hidden behind the observed arm; retain all visible torso pixels." \
  --hidden-mask work/torso-hidden-mask.png \
  --allow-reconstruction
```

Supply a full-canvas binary hidden mask, or record valid `occluded_by` relationships for the component. Only pixels behind existing opaque front layers are permitted; the current conservative route does not invent new silhouette beyond the original foreground. Without an eligible hidden region, the request fails. The handoff describes required images, component identity, visible mask, source geometry, preservation constraints and a receipt. It does not perform a model call. Read the **installed imagegen skill** before fulfilling it. Its preferred interface is the actual built-in `image_gen` tool; it does not require an API key. Inspect local targets with the tool's supported image-view operation first. Attach every required image using supported `referenced_image_paths` when every input has a local path, or the supported recent-image mechanism when necessary. Never supply both mechanisms, invent an SDK segmentation endpoint, or silently fall back to paid CLI inference.

Ask for genuine transparency and source-aligned geometry. Treat the source character, observed component and masks according to their stated roles. Preserve visible colors, proportions, markings and constraints; infer only the explicitly requested occluded region. Copy the actual selected output into the intended workspace through the installed skill's supported flow. Record genuine tool/provider evidence in the handoff receipt; a missing call ID or model identifier requires a nonempty `unavailable_reason`. The component's stored call metadata and inspection note remain bound to that original receipt. Never substitute a local job token for a provider call ID.

```bash
python scripts/hatch.py decompose-reconstruction-ingest \
  --run-dir output/assisted-character --component torso \
  --source work/selected-torso-reconstruction.png \
  --receipt work/torso-reconstruction-receipt.json \
  --allow-reconstruction
```

The controller validates the actual returned raster and stores reconstructed artwork and its inferred mask separately under component provenance. The observed crop is retained; newly generated pixels are never converted into source evidence. Review the inferred shape independently. Use `decompose-recompose --include-inferred` only for an explicitly labeled inferred preview. Default faithful validation continues to compare the original visible extraction. The project does not claim a successful model call merely because a handoff was prepared or a synthetic receipt was tested.

Reissuing a request for the same active component returns its existing token. Resolve it by ingestion or `decompose-reconstruction-fail --token TOKEN --note "Actual failure reason"` before retrying or starting another component. Default bounds are three claims per component, one active claim and sixteen total claims. These limits count local handoffs; they do not measure provider costs or secretly initiate inference.

## Export, reload and forward reuse

```bash
python scripts/hatch.py decompose-export \
  --run-dir output/decomposed-creature --destination output/creature-package
python scripts/hatch.py decompose-export \
  --run-dir output/decomposed-creature --destination output/creature-package.zip
python scripts/hatch.py decompose-import \
  --source output/creature-package.zip --run-dir output/reloaded-creature
python scripts/hatch.py decompose-validate --run-dir output/reloaded-creature
```

Exports are self-contained validated snapshots with a file inventory and hashes. Evaluation exports can remain unapproved and say so. Use `--require-approved` when an accepted semantic export is required. Reload verifies package entries, source evidence, masks, placements and approval signatures before creating a new editable run. It preserves provenance and review metadata; archive transport never promotes a pending decomposition to approved. The package receipt explicitly says that it is not a Codex pet package.

Prepare ordinary forward-generation work from the decomposed character:

```bash
python scripts/hatch.py decompose-prepare \
  --run-dir output/reloaded-creature --destination output/creature-waving \
  --operation wave --component near-wing
python scripts/hatch.py base-review \
  --run-dir output/creature-waving \
  --note "Inspected imported canonical foreground, proportions, palette and visible wing reference."
python scripts/hatch.py claim --run-dir output/creature-waving --job wave
```

The first command imports the observed foreground as the normal canonical base, prepopulates strict `identity.json`, snapshots decomposition provenance, and makes selected component images explicit required references. Preparation does not create animation frames, spend model calls, approve the base, or erase pending semantic status. Complete the claimed state with the installed imagegen workflow, then use the existing validation, visual review and export gates. Fixture labels persist across the boundary. An explicit `--profile profiles/codex-v1.json` is required when the destination must satisfy Codex's fixed package; otherwise reuse creates a generic asset profile appropriate to the selected operation.

To reuse a [compiled character-state selection](character-states.md), pass its profile with `--profile selected-profile.json` and its compilation evidence with `--state-selection selected-profile.selection.json` to `decompose-prepare`. Both the original state selection and the selected component references remain bound to the forward run through retries. Compilation does not change imported pixels or supply missing animation artwork.

Identity prepopulation includes measured palette entries and component/canvas proportions, named visible markings and accessories, anatomical side constraints and explicit identity extensions. These are observed-view measurements and instructions, not a recovery of unknowable body proportions. Mirroring is forbidden by default because one view cannot establish opposing-side symmetry. Add carefully reviewed identity fields beneath annotation `extensions.identity` when needed.

| Request | Operation classification | Practical implication |
| --- | --- | --- |
| Animate, wave, walk, new pose, changed expression | `animate`, `wave`, `walk`, `pose`, `expression`: new artwork | Static components serve as identity references; actual new poses/frames still need production artwork |
| Replace a hat or preserve everything except one part | `replace`, `preserve-except`: new artwork with explicit component targets and operation note | The selected components are change targets; other identity constraints remain required |
| Recolor, hide, transform or reorder an existing layer | `recolor`, `hide`, `transform`, `reorder`: layer-edit intent | Use `decompose-edit` to execute a deterministic visible-pixel variant; `decompose-prepare` only records intent when these operation names are used |

Use `--operation-note` for the requested expression, pose or targeted edit, and repeat `--component` for explicit change/reference targets. Do not label a hidden-area gap as repaired just because a layer was moved or hidden.

### Perform an actual static layer edit

```bash
python scripts/hatch.py decompose-edit \
  --run-dir output/decomposed-creature --destination output/creature-red-wing \
  --component near-wing --operation recolor --color "#CC2244"
python scripts/hatch.py decompose-edit \
  --run-dir output/decomposed-creature --destination output/creature-moved-wing \
  --component near-wing --operation transform --offset "3,0" --rotation 5 --scale 1
```

The original decomposition stays unchanged. The variant includes `edited.png`, individual `layers/`, compatible `identity.json`, `edit.json`, an inventory receipt and a normal `base-source.json` import descriptor. Its review is pending and it is labeled as an edit, not a faithful reconstruction of the original source.

`recolor` replaces selected RGB with one explicit flat color while preserving alpha and geometry; it deliberately replaces texture and is not a texture-preserving material recolor. `hide` omits selected layers without filling the resulting holes. `transform` moves/scales/rotates each selected component around its declared pivot or visible bounding-box center, with clockwise degrees; pixel art uses nearest-neighbor resampling. Children and joints are not solved automatically. Actual clipped pixels cause rejection. `reorder --z-index N` sets drawing order; disjoint faithful masks may produce no visible difference until layers move or inferred variants are used.

Import the edited variant through the existing generation interface:

```bash
python scripts/hatch.py prepare \
  --run-dir output/edited-creature-forward --profile examples/game-npc.json \
  --reference output/creature-red-wing/edited.png \
  --identity output/creature-red-wing/identity.json --pet-name "Edited creature" \
  --fixture-mode
python scripts/hatch.py import-source \
  --run-dir output/edited-creature-forward --job base \
  --receipt output/creature-red-wing/base-source.json \
  --note "Inspected the explicitly labeled synthetic layer-edit fixture."
```

The example retains `--fixture-mode` because it starts from the bundled synthetic creature. Production artwork must keep its own non-fixture label. The next steps remain ordinary `base-review`, actual state acquisition, validation and visual review.

## Package structure and limits

```text
character-decomposition/
├── source/original.EXT
├── original.png
├── foreground.png
├── decomposition.json
├── identity.json
├── components/<category>/<component>-<content-hash>.png
├── masks/
├── annotations/
├── previews/
├── review/
├── analysis-request.json
├── validation.json
└── provenance.json
```

The exact original and normalized file hashes, attribution and fixture status are distinct. Component asset/mask hashes, source-space placement, hierarchy, uncertainty and review are strictly validated by `profiles/decomposition.schema.json` and the shared runtime definitions. Content-addressed component filenames allow independent reuse without silently overwriting old artwork. Review/history and explicit reconstruction files are added as the workflow progresses.

Image input limits reuse the existing infrastructure: at most **2,000,000 source pixels** and **64 MiB encoded image bytes**. Component annotations permit at most **128 components**, with an additional expanded mask working-set bound of **64,000,000 component-canvas pixels**. A residual unassigned component also consumes capacity. The combined canonical horizontal-run count for all component masks plus the foreground mask is limited to **250,000 runs**, so extremely fragmented masks can fail even when their canvas fits. Mask spans/polygons, attachment counts and strings are bounded. Portable archives additionally bound total files and uncompressed bytes and reject traversal, symlinks, malformed inventories and case-colliding paths. Resources are checked before expensive decoding/allocation wherever the necessary information is available.

No mandatory paid inference service, external account or network dependency is introduced. New run/export destinations must be empty. Local corrections use staged replacement and recovery records; imported originals and prior component evidence are preserved. A model-assisted operation remains an explicit external handoff, so Hatch's controller must never silently initiate a retry or duplicate provider call.

## Acceptance evidence and real-image protocol

`tests/decomposition_fixtures.py` authors eight representative test designs: cartoon, overlapping arm, multiple accessories, winged quadruped, pixel robot, fractional-alpha details, complex background and two characters. Its visible masks come from known authored layers, not a segmentation model. [test_decomposition.py](../tests/test_decomposition.py) checks real raster files and exact reconstruction against independently rendered source artwork. Separate review and portability/reuse tests cover actual corrections, browser interaction, archive verification and the normal generation boundary. A bundled Pip frame additionally exercises real existing artwork import and pixel fidelity while explicitly retaining pending semantic status.

| Requirement | Implementation and evidence |
| --- | --- |
| 1. Existing architecture | `hatch.py` command dispatch, `asset_contracts.py` limits/hashes/identity, existing `source_interface.py` imports, review-desk routing and normal forward controller |
| 2. Image input | `create_decomposition`, `decomposition_masks.py`; PNG/JPEG/WebP, alpha/solid/complex backgrounds, selection, source byte hashes and invalid/oversized tests |
| 3. Semantic analysis | Flexible annotation categories, optional external analysis request, confidence/uncertainty, honest `manual-pending` and `annotation-assisted` labels; no fabricated vision backend |
| 4. Actual extraction | Cropped source RGBA plus full-canvas masks; connected face/body separation, disconnected highlights/spots and exact asset/hash/bbox tests |
| 5. Occlusion/reconstruction | Visible ownership, explicit partial/unknown metadata, separately ingested inferred artwork and preserved original crops; ambiguity/overlap/assisted provenance tests |
| 6. Hierarchy/rigging | Parent validation, anatomical sides, transforms, pivots/attachments and conservative rig status; cycles, nonexistent parent and source-coordinate tests |
| 7. Quality validation | Exact reconstruction, visible error and alpha, source coverage, gaps/overlap/clipping, unassigned regions and diagnostic previews; similarity does not grant semantic approval |
| 8. Review/correction | Offline component desk, mask edits, split/merge, metadata, verdicts, source/manifest-bound receipts, independent invalidation and revision restore tests |
| 9. Definition/schema | Generated strict manifest and annotation schemas, runtime/schema agreement, namespaced extensions and existing strict identity validation |
| 10. Forward reuse | `decomposition_reuse.py`, source snapshot, selected reference receipts, ordinary imported base, state prompts and retained fixture/review semantics |
| 11. Portability | `decomposition_package.py`, directory/ZIP inventory, exact export/import round-trip and tamper/traversal rejection |
| 12. Operations | Bounded deterministic masks and geometry; no automatic network inference; fail-closed staging, source preservation and individually repairable artifacts |
| 13. Automated acceptance | Synthetic and existing real raster cases, regression suite, meaningful failure tests, real-image semantic protocol below |
| 14. End-to-end example | `examples/decomposition/build_fixture.py` extracts, validates, exports, reloads and prepares ordinary forward work with zero calls and no fabricated approvals |

### Real-image semantic acceptance is a separate gate

Before describing an optional analyzer as generally capable of automatic character decomposition, test it on a held-out, rights-cleared set that was not used to draw fixture masks or tune prompts. Include at least the following: a continuous-tone illustrated mascot; a clothed humanlike figure with several accessories; a quadruped/fantasy creature; a robot/abstract design; pixel art; fractional-alpha edges; a complex photographic/illustrated background; disconnected details; overlapping anatomy; and ambiguous or multiple characters. Retain original files, origin/license evidence and hashes.

Have a qualified reviewer annotate visible part boundaries, semantic names/hierarchy, anatomical orientation, uncertain areas and all genuinely occluded regions. Run the real optional analyzer through its documented interface, recording actual prompts, attached-image hashes, tool/model identity where available, settings, cost estimates if explicitly provided, outputs, elapsed time and failures. Make separate records for fully automatic proposals and any human-assisted corrections; do not give an automatic score to masks corrected by a person.

Assess semantic correctness component by component: omitted or invented parts, inappropriate humanoid assumptions, merged/split labels, wrong left/right, misplaced accessory attachments and incorrect foreground membership. Compare predicted visible masks with the reviewed reference using coverage and boundary/region overlap, and report those alongside the existing reconstruction metrics. Choose and document practical boundary tolerances for the actual image resolution and intended use before measuring the held-out set. Pixel-perfect reconstruction alone receives no semantic credit: assigning every pixel to `unassigned` reproduces pixels while identifying no anatomy.

For assisted hidden reconstruction, separately review anatomical plausibility, visible-pixel preservation, identity drift and the disclosed inferred mask. Hidden geometry has no direct source-pixel ground truth. Require a second reviewer for ambiguous parts or production-critical acceptance, retain disagreements and failed cases, and state the tested distribution and actual remaining limitations. The repository's offline tests and fixture examples do not claim that this live semantic evaluation has been run.

Run the complete existing and decomposition tests with the documented test dependencies:

```bash
python -m unittest discover -s tests -v
```

Browser review tests use the optional local Playwright/Chromium setup described in the README. Enable the decomposition desk's actual browser-editor exercise explicitly; otherwise that test is skipped:

```bash
HATCH_DECOMPOSITION_BROWSER_TEST=1 python -m unittest discover -s tests -p test_decomposition_review.py -v
```

In PowerShell, set `$env:HATCH_DECOMPOSITION_BROWSER_TEST = "1"` before running the same Python command without the leading shell assignment. Set this environment variable for the complete suite as well when browser-editor acceptance is required. Deterministic fixture tests never require paid inference.

### Implementation verification

See [current integration validation](decomposition-integration-validation.md) for the repaired merge with the character-state library and the latest complete regression. The following counts describe the earlier feature-branch run.

The complete development suite passed **512 tests with no skips** with the decomposition browser test enabled: **109 decomposition tests** plus **403 existing regression tests**, including the merged style catalog. This includes real Chromium editing and receipt application, staged recovery at each promotion phase, exact source/alpha reconstruction, inferred-art provenance, directory/ZIP round trips, actual static layer edits, and ordinary forward import/validation/review/package/archive restoration. A dedicated catalog integration test verifies imported pixels and style authority, nearest-neighbor profile precedence, and frozen style/component provenance through forward handoff and archive/restore. Generated schemas matched their source declarations, and the installed skill metadata validator passed.

Running the eight-case example produced **114 transparent component assets**. Every case had maximum channel error **0**, selected-foreground coverage **100%**, effective overlap and gaps **0 pixels**, and exact alpha preservation. Every case exported, reloaded and prepared an ordinary forward run. The example retained explicit fixture provenance and pending semantic review, with **zero model calls and zero newly generated animation frames**. These results verify the implemented mechanics; the held-out real-image semantic evaluation above remains a separate, unperformed gate.
