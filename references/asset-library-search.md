# Offline asset search

`asset_library_search.SearchIndex` is a derived SQLite FTS5/metadata index over
explicitly configured `AssetLibrary` handles. It never publishes assets or owns
library manifests. Deleting and rebuilding an index leaves original library
content unchanged. SQLite FTS5 is required; absence produces an explicit index
error and does not silently substitute an embedding model.

```python
from asset_library import AssetLibrary
from asset_library_search import SearchIndex

library = AssetLibrary('/absolute/private/library')
search = SearchIndex('/absolute/new/private/search-index',
                     {'project': library}, create=True)
report = search.rebuild()  # requires the native list_references API
page = search.search('Find a small cartoon robot that can walk.', limit=20)
```

`rebuild(locations=[{'library': 'project', 'reference': exact_ref}])` instead
indexes an explicit pinned selection. The response distinguishes that selection
from full-catalog indexing. Neither form follows mutable latest-version aliases.
A whole-library rebuild uses the storage owner's `list_references`, while every
source record is loaded through its existing `resolve` and `annotations` APIs.

Pass the existing `DependencyGraph` as `dependency_graph=graph` to verify required
cross-library closure and evaluate compatibility through
`asset_library_dependencies.check_compatibility`. The graph and search must use
exactly the same configured library handles. `search(compatibility=constraints,
native_path='native/rig.json')` uses the native owner for signatures requiring
actual rig data. Declarative renderer capabilities remain declarations; no
renderer execution is inferred.

## Consistency and filters

Rebuild reads each exact immutable manifest, payload closure and mutable
annotation revision twice before committing a new derived snapshot. Failed
payloads are omitted with exact location/error diagnostics. Every query checks
all selected records again, including source bytes, annotations, native
collections and explicit project bindings. Changes return `LIBRARY_SEARCH_STALE`
until rebuilt. A query cannot silently rank old tags or return a now-corrupt
payload. This costs bounded source verification per query; no unmeasured claim
of large-library performance is made.

The private index stores the manifest metadata and original source hashes, a
snapshot digest, lexical rows and optional vectors. It checks derived rows
against the current authoritative snapshot. An incomplete index is disposable;
corrupt original library files require the storage owner's repair workflow.
The source library's owned-private-directory trust boundary also applies to
index directories. Do not replace their roots during an operation.

`filters` accepts these strict fields:

- `kind`, `subtype`, `category`, `author`, `publisher`, `license`, `scope`
  (publication visibility), `library` (configured alias), `version`, `format`,
  `method`, `review_status`, and `rig_type`: any of the supplied exact strings.
- `tags`, `collections`, `material`, `animation`, `renderer`, `capabilities`:
  all supplied strings must match recorded metadata.
- `favorite`: boolean.
- `max_triangles`, `max_texture_pixels`: nonnegative integer budgets over the sum of recorded per-file counts. Missing
  recorded technical values fail the filter; a manifest does not prove geometry
  correctness merely by recording a count.
- `created_after`, `created_before`: explicit RFC3339 timestamps, inclusive.
- `used_by`: any supplied project ID from native project bindings.

Empty string lists impose no constraint. Filter values are never interpolated
into SQL or FTS syntax. Search text is tokenized into quoted lexical terms.

Pass `{alias: AssetVersions(library)}` as `versions` to include custom collection
revisions. Pass the native `ScopedResolver` as `usage_resolver` and a bounded
explicit `project_ids` list to include exact project bindings. Collection/usage
filters fail explicitly when these owners are not configured; invented empty
usage is not treated as an authoritative project inventory.

Sorting supports `relevance`, `name`, `created_at` and descending numeric semantic
`version`, with exact library/asset/version/hash tie-breakers. Responses include
explainable lexical, completeness, native-compatibility and optional cosine
components. Completeness is a verified admission gate, so all returned records
have that same score. Maximum page size is 100. Cursor state binds query, filters,
sort and index snapshot; changing any of those invalidates the cursor.

## Natural-language boundary

The offline parser performs documented deterministic rules, not AI inference:

- Singular/plural character, material, texture, camera, light, environment and
  prop words select the asset kind when unambiguous.
- `low-polygon`/`low poly` requires a recorded triangle count at most 10,000.
- `can walk` requires an explicit `walk` capability or animation channel.
- Remaining meaningful words use lexical matching and SQLite relevance.

For example, medieval stone village, rough metallic science-fiction materials,
low-polygon outdoor trees and indoor lighting work from corresponding metadata.
A phrase such as “compatible with this animation rig” requires explicit native
compatibility constraints; pronouns never select a rig implicitly. `small` and
`cartoon` are lexical descriptors, not unmeasured geometry/style conclusions.

## Optional embeddings

`ProviderRegistry` is empty and disabled by default. Trusted Python adapters must
report exact model ID/version, runtime availability, credential requirements and
whether metadata leaves the machine. Unknown providers, missing runtime or
credentials and unauthorized private-metadata transmission fail before encoding.
Actual encoding is separate from lexical search; `embedding_used` and the bound
model identity are returned only for that path.

`asset_search_embeddings.LocalSentenceTransformer` is an optional local-only
adapter for `sentence-transformers/paraphrase-MiniLM-L3-v2`. It requires separately
installed `sentence-transformers`, an explicitly supplied local model directory
and its exact tree SHA-256 from `model_snapshot`. It performs no download and
uses CPU, `local_files_only=True`, `trust_remote_code=False` and safetensors.
Model files must be bounded regular files without links; custom Python modules,
pickle weights and unknown model IDs are rejected. Supported loading options
follow the [Sentence Transformers API](https://sbert.net/docs/package_reference/sentence_transformer/model.html).
The adapter takes a verified private copy before loading. Each source file is
limited to the library's 64 MiB reader bound, with 128 files and 256 MiB total;
models outside those limits fail rather than bypass them.

`rebuild_embeddings(registry, name, enabled=True)` creates derived normalized
vectors for the current snapshot. Remote providers additionally require
`authorize_private_metadata=True` for that call and every query. Vectors are
bounded, finite, model/version/snapshot-bound and discarded on rebuild. A query
must select the same provider and exact model version. The adapter must enforce
its own process/runtime limits; ordinary lexical search never invokes it.

The local runtime and model were unavailable during this implementation's
validation. Provider doubles test only admission, invalid vectors, revision
binding and privacy boundaries; they do not establish real embedding quality or
execution. Offline lexical and constraint search use actual registered payloads.
The optional model path must be tested with its real runtime before deployment.

## Bounds and demonstration

Limits: 32 libraries, 4,096 selected records, 2,048 query characters, 64 lexical
terms, 128 project IDs, 100 results/page, 16 MiB captured metadata, 16 MiB vectors,
2,048 vector dimensions and a 64 MiB SQLite index. Quota failures roll back the
index transaction; existing library content is untouched.

```bash
PYTHONPATH=scripts python -m unittest discover -s tests -p test_asset_library_search.py -v
python scripts/demonstrate_asset_search.py --destination NEW_DIRECTORY
```

The demonstration registers real procedural OBJ payloads with distinct metadata,
queries them offline, verifies pagination and confirms that changed annotations
invalidate the old snapshot. It is an engineering fixture, not artwork approval.
