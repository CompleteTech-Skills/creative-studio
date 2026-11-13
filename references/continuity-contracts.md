# Continuity contracts and identity revision API

This additive foundation implements [task #113](https://github.com/CompleteTech-Skills/hatch-pet/issues/113).
It consumes immutable native asset and evidence pointers; it does not implement
cross-shot event replay, geometry inspection, continuity detection, rendered
comparison or a review-authority service. Those are separate tasks under #107.
The existing production-v1 five schemas, pixel coordinates, frame/audio behavior,
source transports and historical reviews are untouched.

## Contract authority and coordinates

`scripts/continuity_contracts.py` owns 23 strict root models corresponding to
parent section 24.1: identity, revision, native canonical asset, rig compatibility,
costume, world entity, character/prop/environment/camera/animation/narrative state,
event, snapshot, link, rule, exception, issue, comparison, review, dependency,
impact and repair. Generated schemas live at `profiles/continuity-*.schema.json`.
Imports and schema generation require no geometry/render packages. Explicit
validation lazily uses the already-declared `jsonschema` dependency.

Every document has independent `schema_version: "1.0.0"` and a fixed
`kind: "continuity-..."`. Unknown versions/properties fail; there is no open
metadata escape hatch. Native pointers are exact `{id, version, path, sha256,
kind}` records. File-byte hashes and canonical-JSON hashes are different evidence.
`canonical_bytes` sorts keys, uses compact UTF-8 JSON and rejects nonfinite values,
non-JSON values, nesting beyond 64 levels and documents above 8 MiB. Native asset
bytes are never normalized or rewritten.

The canonical rig/scene convention remains right-handed +Y-up, actor +Z-forward,
meters/radians, XYZW unit quaternions and positive nonsingular TRS scales. No
pixel-to-meter reinterpretation occurs. Native Blender/glTF conversions remain
with their existing owners. Time coordinates are reduced signed rational seconds
with positive denominators and integers bounded to the exact JSON range. Queries
include exact project revision, world, timeline, branch, distinct story and
presentation times, and `before`, `after` or `terminal` side. Scopes are nonempty
half-open intervals. A continuity link must consume an explicit terminal outgoing
query; last displayed frame N−1 cannot silently replace terminal boundary N.
JSON Schema integer-equivalent numbers such as `1.0` are normalized to Python
integers before rational arithmetic, preserving exact values and structured
errors. These contracts do not evaluate a timeline.

Logical `identity_id`, immutable `revision_id`/`semantic_version`, persistent
`entity_id`, `instance_id`, and renderer IDs have separate fields. Character
entities must bind the matching logical identity and exact immutable revision.
Native asset identity belongs to the asset library/model/rig owner; this registry
never copies their payloads or mints native IDs. World state stores evaluated
source references and unavailable fields instead of claiming authored labels
prove actual evaluated motion, contacts or visual identity.

## Validate real fixture pointers

The retained `examples/continuity` OBJ and one-pixel PPM are explicitly synthetic
transport fixtures. The OBJ is a genuine independently readable triangle, not
production character geometry. The PPM is a transport/pixel fixture, not an
approved rendered reference. No retained review or approval is supplied.

```bash
python scripts/continuity_contracts.py --generate-schemas profiles --check
python scripts/continuity_contracts.py --validate examples/continuity/revision.json
PYTHONPATH=scripts python -c "from continuity_contracts import load_document,verify_references; print(verify_references('examples/continuity',load_document('examples/continuity/revision.json')))"
```

`load_document` and retained receipt reads share one bounded nofollow/nonblocking
descriptor reader with type/size and replacement checks; they never use a
file-check followed by a separate blocking path open. `verify_references` checks
actual regular-file bytes, case/path alias conflicts,
portable relative paths, no symlinks, bounded total reads and observed
open-handle/path replacement. The default total bound is 128 MiB. It deliberately
reports `geometry_inspected: false` and `visual_approval: false`. Matching bytes,
counts or names do not establish geometry suitability or identity consistency.
`geometry_statistics.status=unverified` requires null counts/measurement source;
bounds may also be explicitly null. A measured record instead requires complete
counts, bounds and an inspection-evidence pointer. The retained fixture honestly
leaves these fields unavailable. A mesh pointer cannot masquerade as a measured
inspection receipt. Native format/version/rig compatibility inspection belongs to task #114 and
capability-tested adapters. The local root directory is trusted against arbitrary
hostile concurrent ancestor renames; this API is not an OS sandbox.

## Identity registry operations

`IdentityRegistry(database_path, asset_root=...)` is a data-only SQLite continuity
sidecar, with explicit format/application identity, foreign keys, bounded growth,
full synchronous rollback-journal transactions and five-second lock timeout.
Its parent must already exist. The constructor owns the native SQLite connection
until every initialization step succeeds. Application/format rejection, PRAGMA
failure and schema-transaction failure close that connection explicitly; rollback
or a SQLite context manager alone does not close it. A successful constructor
transfers ownership to the registry until `close` or its own context exit. Existing unrelated databases, leaf symlinks,
nonregular files and symlink SQLite sidecars are rejected. Existing parent aliases
are resolved without following the requested database leaf. Native files and
legacy project manifests remain external and unchanged.

```python
from continuity_contracts import load_document
from continuity_registry import IdentityRegistry

with IdentityRegistry("output/continuity.sqlite", asset_root="examples/continuity") as store:
    store.create_identity(load_document("examples/continuity/identity.json"))
    draft_hash = store.create_draft("working", load_document("examples/continuity/revision.json"))
    draft = store.draft("working")  # isolated, unapproved; does not select a version
```

`create_identity` is idempotent for identical bytes and rejects an ID collision
with changed bytes. Isolated mutable drafts use exact expected-hash compare and
swap. `update_draft` cannot move a draft to a different identity. Unknown parent
revisions, cross-identity lineage and non-increasing derived semantic versions
fail before persistence.

`promote(draft_id, expected_sha256=..., review=..., select=False,
expected_selected_revision_id=None)` freezes an
explicitly supplied approved decision. It verifies the draft hash, exact retained
receipt bytes, source fixture flag, and actual evidence files. Required inspected
evidence includes the exact geometry pointer and every reference-render pointer.
The retained receipt is opened once with a bounded nofollow/nonblocking descriptor
and verified against its pinned bytes before decoding; it cannot block on a FIFO
substituted after an earlier verification. The retained receipt is the continuity-review record without its own
`native_receipt` pointer, avoiding a circular self-hash. It remains a separate
owner-retained file. Synthetic-test decisions cannot approve. Tests construct
clearly labelled simulated assistant decisions solely to exercise admission; they
are not actual visual-review evidence.

This method is not a reviewer, credential/signature verifier, geometry inspector,
or production approval authority. It records an existing caller-supplied decision
and always returns `production_artwork_approved: false`. Automated receipts cannot
establish human signoff. Later review integrations must adapt and verify the
actual native review authority rather than infer an approval from a name or tag.

An immutable revision ID or per-identity semantic version cannot be reused with
different canonical bytes. Promotion rechecks draft CAS inside the write lock,
atomically inserts the immutable document/review binding, optionally selects it
only if the current selection still equals the explicit expected selected ID
(default `None` requires an unselected identity),
removes the draft and appends history. Any failure rolls the whole transaction
back. Documents deduplicate by canonical hash; asset bytes are never copied.

`revision(id)` rechecks schema, document hash, review binding and all external
reference bytes by default. `identity(id)` returns immutable identity specification
plus its independently mutable selected revision/pin. Selection never rewrites an
approved revision. `select(identity, revision, expected_revision_id=...)` supports
pinning and rollback with a CAS precondition. `compare(a,b)` produces exact field
differences and explicitly does not claim geometric/visual comparison.
`deprecate(id, expected_sha256=..., reason=...)` retains historical bytes and
existing project bindings; deprecated versions cannot be newly selected/migrated.

## Selected-version migration boundary

`register_bindings` rechecks target deprecation and all native/project references
inside the write transaction, then stores a separate versioned continuity sidecar with exact
project pointer and shot-instance/entity/revision pins. It does not edit native
scene documents. `migrate_selected(project_id, instance_ids, target_revision_id,
expected_sha256=...)` checks every selected ID, identity, version/hash and target
approval/deprecation status, then atomically changes only selected pins. Unknown,
duplicate, stale or cross-identity selections fail with no changed rows/history.
Unselected entries remain byte-for-byte equal as JSON values. Binding reads and
migration reverify the actual exact project file; stale project bytes cannot be
migrated merely because the sidecar hash is unchanged.

Until a real geometry/rig compatibility adapter is available, migration permits
only newer metadata revisions retaining identical native geometry, component
structure, authoritative asset/dependency closure, materials/textures,
rig/skin/morph/attachment/rest references, bounds
and coordinates. Changed structural payloads fail `CONTINUITY_ADAPTER_REQUIRED`;
they are not declared compatible from identity labels. This conservative initial
admission boundary enables proven same-payload version migration while keeping
actual changed-asset/editor scene migration and render/review invalidation gates
explicitly pending their respective tasks.

## Requirement-to-test evidence

`tests/test_continuity_contracts.py` executes all 23 schema/runtime roundtrips,
unknown properties/versions, unsafe/aliased/missing/changed payloads, exact rational
times, unit quaternions, cyclic component refs, immutable collision rejection,
real database reopen, two-connection stale drafts, supplied review binding,
rollback/pinning/deprecation, selective migration and injected SQLite transaction
failure. No positive test relies only on file existence. Schema generation is
checked from runtime definitions; the standalone command avoids imports of other
in-flight contract families. Tests are automatically discovered in the ordinary
2D-compatible suite because this foundation requires no optional 3D packages.

Existing `test_production_contracts` exercises actual v1 project loading and
`test_rig_compatibility` protects frozen atlas/catalog/contracts and optional
package isolation. The dated validation record distinguishes those observed
executions from future cross-platform CI, geometry inspection, rendering,
production visual approval and the full continuity mission.

## Cross-platform receipt bytes

The shared decoder/provenance policy pins recursive `tests/results/**` and
`examples/**` as exact bytes, all `tests/*.py` to LF, and the existing frozen
playback/catalog files to LF. Scoped decoder rules remain in place. A real
`core.autocrlf=true` checkout proves active CRLF conversion for unpinned README
while all 114 continuity receipt-bound sources and nested evidence retain exact
Git bytes. The retained final-provenance report distinguishes this Git conversion
check on Linux from still-required native Windows/macOS CI.
