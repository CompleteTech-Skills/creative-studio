# Asset version lifecycle

`asset_library_versions.AssetVersions` extends the existing `AssetLibrary` and
its durable event journal. It does not create a second registry. Published
manifests, source files and prior versions remain immutable and addressable by
`{id, version, sha256}`. The store rechecks the complete payload closure when a
version is resolved.

```python
from asset_library import AssetLibrary
from asset_library_versions import AssetVersions, ScopedResolver

library = AssetLibrary('/private/library')
versions = AssetVersions(library)
plan = versions.promotion_plan(reference, consumers=[
    {'id': 'actor', 'requirements': {'formats': ['glb']}}
])
versions.promote(reference, expected_revision=plan['expected_revision'],
                 consumers=[{'id': 'actor', 'requirements': {'formats': ['glb']}}])
```

Create a new version with `create(manifest, sources, previous=exact_reference)`.
It must retain its parent, stable identity, author, license and original license
evidence; allowed uses cannot broaden. An existing identity requires an explicit
parent. Creation supplies `expected_identity_refs` to the registry; the complete
identity-history snapshot is compared under its writer lock before publication.
A concurrent direct registry write therefore invalidates lifecycle admission
instead of bypassing parent and attribution checks. Existing direct registration
callers retain their prior behavior unless they request this optional guard.
A major version is required for coordinate/unit or rig/skeleton signature
changes, removed capabilities, or removed required files. Added capabilities
require a minor version; remaining corrections require a patch version. These
rules inspect declared interfaces rather than trusting the version number.
Native import/validation remains responsible for deriving truthful interfaces.

`promote`, `rollback` and `deprecate` append immutable revision-checked records.
Promotion is a local default selection, not technical, artistic, licensing or
public distribution approval. Its plan lists the explicitly supplied consumers
and affected binding/preview/render categories. It does not discover unregistered
projects or execute rebuilds. Consumer signatures must come from native
inspection. Deprecated versions remain readable; the current default cannot be
deprecated until replaced. Rollback selects a retained prior default and checks
its supplied consumer requirements again. Concurrent stale revisions fail.

Collections use `set_collections` and `collections`; tags, favorites and notes use
`AssetLibrary.update_annotations`. Both preserve the immutable manifest and
require the current metadata revision. Histories are limited to 4096 events;
exhaustion fails before publication rather than making history unreadable.

`ScopedResolver` takes an ordered list of explicitly named stores. The first
store containing an identity shadows later stores; a missing default or broken
exact reference fails instead of falling through. `select` returns a named store
and exact reference. `bind` persists that pair in an explicitly configured
project store with revision checking. `load` always follows the pair, so later
shared default changes cannot modify existing project snapshots. Named stores
can be reopened at relocated filesystem roots without rewriting bindings.

`LegacyMigration.preview` supports native legacy pet profiles, casts, clips and
rig documents using their existing validators. It hashes the complete supplied
inventory, requires retained original source provenance and verifies hash-bound
native file references. `apply` repeats preview checks and then registers the
exact originals. Source bytes and native IDs are not rewritten. Unsupported
kinds, missing or changed files and stale plans fail. This is preserved native
payload verification, not geometric conversion or production approval. Full
native clip semantics still belong to the owning cast and native validator.

Validation: `PYTHONPATH=scripts:tests python -m unittest discover -s tests -p
 test_asset_library_versions.py -v` exercises real immutable payloads, concurrent
promotion, rollback, restrictions, deterministic scope, relocated stores and
migration. Storage publication and recovery remain covered by the storage suite.

## Duplicate as a new asset

`AssetVersions(library).duplicate(reference, identity=new_identity,
version='1.0.0')` publishes an independent identity from an exact registered
version in the same canonical library. Supply the complete new `AssetIdentity`,
including a different ID and explicit creation time. Kind, subtype and original
attribution must remain unchanged; licensing, allowed uses, provider/source
receipts and fixture status are retained. This is copying, not generation.

Every inventoried file is copied byte for byte through the registry's ordinary
hash-checked registration path. Content-addressed blobs are deduplicated. Exact
dependency, style and historical parent references retain their verified closure
in that same library; the source exact reference is additionally retained as a
provenance parent. This operation does not export a separate library or rewrite
native IDs inside files. Portable cross-library packages remain the packaging
owner's responsibility.

The duplicate has no inherited selection, tags, favorites, previews or external
integration claim. Technical payload verification does not grant artistic,
licensing or distribution approval. Existing identities and concurrent creation
of the requested identity fail through the ordinary identity-history guard.
An interrupted canonical commit uses normal `AssetLibrary.recover()`; inspect the
recovered exact reference before retrying. Retrying an already recovered identity
fails rather than publishing a second version.
