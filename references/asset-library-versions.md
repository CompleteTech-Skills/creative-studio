# Exact asset version history and local promotion

Use `AssetVersions` with the canonical `AssetLibrary`. Register manifests and
their sources through `AssetLibrary.register()`. Each reference carries the
asset ID, exact version and SHA-256 of its manifest.

```python
from asset_library import AssetLibrary
from asset_library_versions import AssetVersions

library = AssetLibrary(private_root)
versions = AssetVersions(library)
history = versions.history(asset_id)
changes = versions.compare(previous_reference, candidate_reference)
plan = versions.promotion_plan(candidate_reference, consumers=consumers)
if plan["compatible"]:
    event = versions.promote(
        candidate_reference, expected_revision=plan["expected_revision"],
        consumers=consumers,
    )
```

`history()` lists registered references in numeric version order. `state()`
returns the current lifecycle revision, promoted reference, deprecated
references and replayed decisions for an ID. `compare()` verifies both exact
references and the canonical payload and dependency closure before reporting
changes in file hashes, lengths, roles, required status and declared
compatibility. The stable identity must match.

Comparison uses the manifest contract's numeric `major.minor.patch` form. A major
increment covers changes to coordinate system, unit scale, rig or skeleton
signatures, lost capabilities or removal of a required file from the required
inventory. Added capabilities require a minor increment. Other inventory changes
require at least a patch increment. The report includes the minimum and actual
increment and whether the version labels satisfy the policy. The comparison is
advisory for local selection of registered assets.

A promotion plan identifies the previous exact reference, observed revision,
affected consumers and invalidated binding, preview and render work. Each optional
consumer mapping supplies an `id` and explicit `requirements` using compatibility
keys. Required lists must be subsets of declared capabilities. Scalars must match.
Promotion rechecks those requirements and commits through the registry's
revision-checked event append. Selection leaves review, licensing and
distribution decisions with their existing owners.

`rollback(asset_id, expected_revision=..., consumers=...)` restores the most
recent different, retained, non-deprecated prior selection. It uses the revision
observed while finding that prior selection. `deprecate(reference,
expected_revision=..., reason=...)` excludes the version from later selection
while retaining its exact payload and history. Select a replacement before
deprecating the current version.

Legacy SQLite-only roots support registration, history, comparison and planning.
The canonical owner requires durable storage for lifecycle writes. See
[durable storage and recovery](asset-library-storage.md) for explicit upgrade and
recovery. If an interruption leaves an event committed only in durable history,
recover through the canonical owner and reload the observed revision before
retrying.

This implementation covers local lifecycle decisions on already-registered exact
versions. Checked creation, annotation extensions, scoped defaults and pins, and
legacy migration are follow-up work under issue #142.
