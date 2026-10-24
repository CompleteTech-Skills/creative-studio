# Model3D contract and payload foundation

This is the bounded #43 foundation under #10, not a completed model-generation
system. It adds fifteen strict, independently versioned document types and a working
read-only verifier. It reuses actual rig geometry/import inspection rather than
creating another mesh representation, registry, renderer, job store or review store.

## Public operations

Install the existing isolated 3D dependencies:

```sh
python -m pip install 'Pillow>=10.1' -r requirements-rig.txt
python scripts/model3d.py capabilities
python scripts/model3d.py validate --file examples/model3d-contracts/robotic.json --root .
python scripts/model3d.py validate --file examples/model3d-contracts/robotic.json --structural-only
```

The first validation checks both original source bytes and the actual animated
GLB. The second checks only the document: it reports `payloads_verified: false`
and `geometry_inspected: false`, even when all structural checks pass. Neither
operation generates a model, calls a provider, grants approval, or publishes files.
All output is JSON on stdout; expected failures use a JSON error on stderr and
exit status 2. Argument errors use argparse's ordinary exit status 2 and help text.
Commands never overwrite the selected project. Redirect stdout explicitly to
retain a report outside a protected source directory.

Python callers import `validate_contract` from `model3d_contracts` and
`validate_document` / `validate_file` from `model3d_validation` (add `scripts` to the
module path, following the existing repository convention). The first performs no
file I/O. The latter two verify the declared closure by default, raising the shared
`RigError` for invalid contracts/payloads and ordinary `OSError` for OS failures.
A successful observation refers to the copied, hash-verified snapshot, not a lock
on mutable source files. Later source edits require a new verification.

## Versioned contracts

Canonical definitions live in `scripts/model3d_contracts.py`; generated JSON Schema
Draft 2020-12 is `profiles/model3d.schema.json`. Regenerate with
`python scripts/generate_schemas.py`; CI checks `--check`. Every document requires
`schema_version: 1` and an explicit `model3d-*` kind. No legacy document is migrated
or accepted as a 3D document.

| Kind | Meaning and important validation |
|---|---|
| `model3d-request` | Target identity, source roles, method intent, description/negative constraints, exact source/identity/style references, dimensions and scale evidence, desired formats/semantic parts, assumptions, provider selection, seed and resource ceilings. Source modalities and calibration must match the selected intent. This is not a generation receipt. |
| `model3d-source` | Exact original file, stable source version/role, fixture/origin distinction, supplied provenance and license claims, and explicitly unknown/supplied/estimated camera data. Unknown calibration cannot contain invented matrices. |
| `model3d-asset` | Exact source/runtime inventories, canonical geometry reference, measured counts/bounds, part/material annotations, version-pinned dependency manifests, uncertainties, review claims and budgets. Actual geometry, counts, bounds, mesh IDs and referenced file closure are verified. |
| `model3d-part` | Stable semantic ID, parent, local TRS, whole-mesh identities, material/source/uncertainty references. Duplicate IDs, cycles, missing parents, nonunit rotations, singular scales and multiple whole-mesh ownership are rejected. |
| `model3d-material` | Versioned glTF-compatible PBR parameter/slot annotation, texture references and color interpretation. Color textures use sRGB; normal/roughness/metallic/occlusion use linear data. This foundation does not assert equivalence between annotated material parameters and renderer output. |
| `model3d-uncertainty` | Target, observed/inferred/unknown classification, source evidence, explanation, optional measured confidence and its method. Missing confidence remains null; a number without evidence/method is rejected. |
| `model3d-job` | Request hash, target, explicit state, bounded attempts, outputs, failure details and dependencies. A complete declaration needs outputs and no active error; the request must resolve to a matching request contract. No scheduler or provider execution is implemented here. |
| `model3d-export` | Exact asset revision, requested format, declared state, actual output references, tool identities, declared losses and optional independent-inspection evidence. A complete claim cannot have an empty inventory. Output hashes and the source asset's geometry are checked; format-specific export roundtrip/Blender/archive verification is not implied. |

The additional seven document kinds are strict evidence envelopes for the existing
owners. Each has its own schema/semantic version, subject revision, explicit owner,
hash-bound native evidence and optional namespaced extension payloads. They do not
create a second lifecycle engine or authenticate an evidence author's claims.

| Kind | Metadata and semantic gates |
|---|---|
| `model3d-provider-receipt` | Named provider/model/version, exact request, execution/state/output claims, seed, usage and diagnostics. Completed claims require execution and outputs; unavailable providers cannot claim execution. Request JSON must identify the same subject asset and provider. |
| `model3d-processing-stage` | Operation, state, exact inputs/outputs, tools, usage and error. Completed stages require outputs; failed stages require diagnostics. |
| `model3d-resource-usage` | Explicit measured/partial/unavailable state, wall/CPU duration, peak memory, bytes, device and measurement method. Missing measurements remain null; unavailable is never zero usage. |
| `model3d-validation-result` | Named validator, state, contract/payload/geometry observations and typed diagnostics. Geometry inspection requires payload verification; a passing claim cannot contain an error. |
| `model3d-review-decision` | Reviewer identity/type, actual timestamp for decisions, scoped dimensions, inspected hashes and notes. Validation never turns this claim into human or production approval. |
| `model3d-repair-operation` | Exact before/after asset references, operation/state, changed semantic parts, tools and reason. Completed repairs preserve asset ID and need a different immutable output version; the subject binds the input revision. |
| `model3d-archive` | ZIP reference, exact source asset, complete declared inventory, state, preserved reviews and restoration evidence. A restored claim needs native restoration evidence; its subject must match the source asset. This verifier checks hashes and source geometry, not archive extraction or native restoration behavior. |

All source records explicitly retain original filename, pixel dimensions (null for
nonimages), acquisition method/time (unknown/null when unavailable), observed,
inferred or synthetic classification, masks and annotations. Source dimensions and
calibration image size are compared with actual decoded pixels. Known acquisition
methods require an ISO 8601 timestamp with timezone; dates are never fabricated.
Arbitrary calibrated image viewpoints are supported alongside supplemental identity,
texture, geometry and annotation references. Visual hull intent needs at least two
calibrated image observations; supplemental sources do not invalidate those views.

Requests include an optional existing asset version as an explicit nullable field,
source modality, request-level provenance/rights/fixture classification even for
text-only intent, material requirements, texture/runtime/compute budgets, LOD
requirements and fallback policy. These are generation **intent**. No generation,
fallback execution or runtime/compute reservation is supplied by this verifier.
The separately enforced snapshot/import resource ceilings remain described below.

Asset records include generation revision, identity reference, pivot, per-mesh/LOD
geometry references, counts, material slots, normal/tangent/UV availability,
topology measurements, surface attribution, provider/preview/quality references,
repair history, available formats and explicit completeness. Each declared LOD is
actually decoded and its mesh claims checked; this does not generate optimized
LODs. Aggregate declared LOD counts must fit the geometry budget. Parts carry
version/category, pivot/attachments, side, 2D component links, surface regions,
visible/inferred evidence, confidence/method, rig association, review state and edit
history. Parts may bind whole meshes or disjoint surface regions. Multiple whole-mesh
owners and overlapping regions belonging to different parts are rejected. Region
face indices and face/vertex uncertainty indices are checked against
decoded base geometry. Materials retain texture dimensions, typed procedural
parameters, projection evidence and observation class. Texture dimensions are
verified against actual image bytes. Unknown metadata uses explicit unknown/null or
empty evidence collections rather than invented measurements or approvals.

Unknown fields, versions, duplicate JSON keys, NaN/Infinity, oversized values,
unsafe relative paths and conflicting records are rejected. Schema validation
establishes structure; runtime semantic checks additionally enforce referential
integrity, geometry measurements and payload evidence. A JSON Schema-only consumer
must not claim to have run those semantic or filesystem checks.

### Coordinates, timing and annotations

Reuse `rig_contracts.COORDINATES`: right-handed, +X right, +Y up, +Z character
forward, meters, radians, XYZW unit quaternions, column-vector mathematics and
seconds. Local TRS is composed as T × R × S. Positive nonsingular scale is the
initial Model3D part contract; mirrored/negative transforms need an explicit future
version rather than an undocumented reinterpretation. Existing imported glTF
geometry remains subject to the existing importer's documented reflection rules.

Source camera matrices are serialized as 16 **row-major** values and applied to
column vectors. Their rotation must be a proper orthonormal basis, with affine
last row `[0,0,0,1]`. Perspective intrinsics are a row-major 3×3 matrix; orthographic
size is in meters. Supplied/estimated calibration needs image dimensions and a
hash-bound evidence file. Validation does not establish that a calibration was
physically measured; the source classification remains a claim requiring review.

The asset inspector measures geometry after the existing importer's canonical
world-space normalization. Part transforms are validated authoring annotations;
this foundation does not apply them as another hidden import transform or offer
an editing/rendering command. Binding names come from the actual importer, not
assumed anatomical segmentation. Material annotations, job/export state and review
claims are never promoted to observed visual, licensing or provider approval.

## Exact paths, versions and dependency closure

All references, including those inside child asset manifests, are relative to one
explicit common project root. They are **not** relative to each child manifest's
directory. Use `--root` for manifests nested below that root. Each file reference
includes portable path, exact SHA-256, positive byte count and media type.

Each asset dependency pins an ID, semantic version and hash-bound JSON manifest.
Conflicting hashes for the same immutable ID/version fail. Cycles, unknown target
versions, path aliases, case collisions and non-NFC spellings fail. Global and
per-document transitive file/byte/depth and image-decode memory ceilings are enforced, including cached
shared dependencies reached through a deeper path. Image memory admission occurs
before decoding and is enforced again for cached files under tighter child budgets.
Every decoded LOD propagates embedded fixture provenance. Cached geometry validation
is keyed by its effective inherited memory and per-file budgets; a result from a looser context
cannot waive a stricter ancestor budget. Reports retain one geometry observation per
asset revision at the tightest budget under which inspection actually succeeded.
Dependency ordering therefore cannot admit an otherwise over-budget model.

The verifier copies all declared files into a unique temporary directory while
checking actual sizes, SHA-256 and file identity. It decodes models **only from
that snapshot**. Actual buffers, materials' image resources and other importer
resources must be present in the asset's runtime inventory, even when another
provenance record happens to provide identical bytes. Missing/corrupt required
content fails; a valid manifest alone cannot pass payload verification.

Files are streamed in bounded chunks. Links and nonregular files are rejected.
On openat/O_NOFOLLOW platforms, held directory descriptors prevent parent renames
from redirecting individual file reads. The portable fallback rechecks path and
file identities (plus size/mtime; POSIX also checks change time). Windows creation
time is not used as a write fingerprint because CRT handle/path APIs expose
different precision; exact SHA-256 verification remains mandatory. This is not a sandbox against a hostile process concurrently
replacing directories; isolate untrusted writers at OS level. Neither variant
claims to make a mutable project a transactional multi-file source. SHA-bound
snapshot consistency and source-directory locking are different guarantees.

Default maximums are 256 MiB per file, 1 GiB aggregate payload, 4,096 files/parts,
1,000,000 vertices, 2,000,000 triangles, dependency depth 16, and 2 GiB estimated
geometry memory. Project limits may lower these hard ceilings. Static image inputs
are decoded and limited to 16,777,216 pixels and the snapshot's memory budget.
Multiframe images are rejected in this initial contract. Inspection selects the supported eight-influence preservation profile; it does
not imply eight-influence playback in the four-influence local viewer. Native geometry budgets
are delegated to the existing bounded importer, not disabled.

Bounds tolerance is `abs(actual-declared) <= 1e-6 * max(1, abs(actual), abs(declared))`
in meters. Quaternion norm tolerance is 1e-6. These are structural/numerical checks,
not artistic tolerances or measurements of reconstruction accuracy.

## Capability and compatibility boundaries

Implemented and executed: strict validation, exact dependency/file verification,
real GLB and external-resource glTF decoding, existing rig/animation preservation
inspection, CLI JSON failures, read-only concurrent verification, independent
restoration of the declared closure, and negative missing/corrupt/fake mesh tests.
The OBJ route reuses the existing importer; its limitations remain unchanged.

Supported only through other existing rig commands: skeletal generation, skinning,
animation, native previews, Blender authoring and rig archives. This foundation
neither reimplements nor falsely upgrades them. Current verification reports bind
to the actual existing rig implementation hash and installed software versions.

Not implemented here: text/image/multiview generation, arbitrary model-provider
adapters, topology repair/LOD, material editing/render equivalence, model job
execution, general asset-library management or a Model3D editor. No paid service,
model download, GPU probe or network request occurs. Blender-native model inputs
are deliberately excluded from this read-only canonical inspection path; use the
existing explicit trusted Blender workflow and its separate acceptance evidence.

GLB/glTF/OBJ retain the existing importer's supported primitive, attribute,
extension and local-resource policies. This is not a claim of unrestricted glTF
extension support. Sources requiring unsupported adapters fail explicitly rather
than being flattened or silently converted. Output/approval state remains distinct
from contract validity, file integrity and actual geometry inspection.

The fixed Codex atlas, 2D schemas, pixel-space transforms, audio-capable production,
state/style IDs, existing provenance and historical reviews are unchanged. The
legacy test collectors explicitly classify the **new** `test_model3d_` namespace
as optional 3D; no existing 2D test is removed, skipped or reclassified. The new
cross-platform workflow runs those new tests separately with 3D dependencies.


Integral JSON numbers such as `3.0` satisfy JSON Schema's `integer` type. Validated
file byte counts and operational resource limits are normalized to Python integers
before I/O; booleans and nonintegral values remain invalid. This normalization does
not rewrite source manifests, bytes or hashes. Per-file limits apply through the
complete dependency closure, including results first cached under a looser parent.
The stricter inherited limit is checked before opening/copying a fresh payload and
before cached reuse; complete per-document closure checks provide a second gate.
Inspection observations expose the effective source-file ceiling as well as the
memory budget; equal-memory observations retain the stricter successful file limit.
