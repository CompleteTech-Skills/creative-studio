# Exact asset version history, local promotion and collections

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
references and replayed decisions for an ID. Replay checks each distinct exact
reference against its canonical manifest. `compare()` verifies both exact
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

Collections are mutable metadata for one exact version. Use
`versions.collections(reference)` to read `{revision, collections}` and
`versions.set_collections(reference, expected_revision=revision, collections=[...])`
to append a replacement. Names are unique canonical identifiers, sorted for
storage, with at most 128 entries. Empty lists clear membership. Writes require
the observed revision; a concurrent winner makes stale writes fail at the
canonical event append. Reopening and index recovery replay the durable stream.
Malformed documents, mismatched exact references and invalid names fail closed.

Collection streams are separate from local selection and the existing
`AssetLibrary.annotations/update_annotations` owner for tags, favorites and notes.
They preserve published manifests and payloads. The optional collection adapter
for offline search can use this owner directly; callers still rebuild derived
indexes after metadata changes.

This slice preserves #460 at `6212d2d8373e12618bd28eb039e05bbfa0327912`
and adds the collections API from #310 at
`80c827e6e4e68adb6cb4c1dff81caf9abb50d9f6`. Checked creation, scoped
defaults and pins, and legacy migration remain follow-up work under open #142.

Local validation on 18 November 2026 used Linux x86_64 and Python 3.12.14.
The latest tested integration source is
`1fd7b8cf1b47a9b36a1652b4961991c689d61616`, which includes main
`69304deb3e55a35aa3717e1e6fcd949502c8bb99` in the preserved slice. Its
version implementation and tests are unchanged from
`d1c0f060e11dbcb10e885303df3f83bd01bbd429`. Source comparison confirms all 13
original #460 test methods remain verbatim, alongside five collections tests.

Executed commands and observed results:

```bash
PYTHONPATH=scripts:tests python -m unittest discover -s tests -p test_asset_library_versions.py -v
PYTHONPATH=scripts:tests python -m unittest discover -s tests -p 'test_asset_library*.py' -v
python scripts/test_rig_gate.py compatibility
```

On that integration source, the focused version suite passed all 18 tests in
2.744 seconds. The canonical asset-library, storage and version suites passed
all 77 tests in 7.293 seconds, with no skips.

Before the latest main update, the same focused suites passed 18 tests in
1.793 seconds and 77 tests in 5.339 seconds on integration source
`7316e2d616268f8543696f22f0da0ce772f69d20`, which includes main
`8a88bdffae26d672bdb033daa7793e927ce7d7c4`. The full compatibility command
completed on that earlier source with exit status 0: 1,312 tests in 674.766
seconds, `OK (skipped=5)`. The five skips cover the opt-in decomposition browser
test, unavailable Chromium for export/playback/production-preview tests, and
unavailable Krita interoperability. Playwright also emitted pending-task and
`TargetClosedError` cleanup diagnostics after the unittest summary. This run
does not establish browser or Krita success. The full compatibility gate was not
rerun after the latest main update; hosted checks must cover the combined head.

The asset tests use disposable synthetic fixtures and canonical registration
and events. They establish local persistence, replay, concurrency and immutable
payload behavior for this slice. They do not establish cross-platform execution,
native consumer activation, production review or complete #142 acceptance.
