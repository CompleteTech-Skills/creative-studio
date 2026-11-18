# Offline asset search

`asset_library_search.SearchIndex` maintains a disposable SQLite FTS5/metadata
index over explicit canonical `AssetLibrary` handles. It verifies exact asset
payloads and mutable annotations through the existing owner; it cannot publish
assets or grant geometry, artistic, licensing or production approval. SQLite
FTS5 is required. Ordinary searches invoke no embedding provider.

```python
from asset_library import AssetLibrary
from asset_library_search import SearchIndex

library = AssetLibrary('private-library')
search = SearchIndex('private-search-index', {'local': library}, create=True)
report = search.rebuild()
page = search.search('Find a small cartoon robot that can walk.', limit=20)
```

The directories must satisfy the existing owned-private-root contract. The search
destination must be new when `create=True`. Current canonical APIs are
`list_references`, `resolve` and `annotations`; no historical registry or
storage replacement is required. Resolution verifies the local dependency
payload closure. `rebuild(locations=[{'library': 'local', 'reference': exact_ref}])`
indexes an explicit pinned selection. A full-catalog index becomes stale when
the catalog changes; unrelated additions do not invalidate an explicit selection.

## Consistency, filtering and ranking

Rebuild captures source state twice before publishing. Every query rechecks
selected bytes, annotations and any configured collection/usage state. Changed
state raises `LIBRARY_SEARCH_STALE` until rebuild. Corrupt or missing payloads
are excluded with exact location/error diagnostics; metadata alone never
establishes completeness. Derived rows are checked against the source snapshot.

Filters accept exact lists for kind, subtype, category, author, publisher,
license, scope (publication visibility), library alias, version, format, method,
review status and rig type. Tags, collections, material, animation, renderer and
capabilities require all requested values. Favorite is boolean; creation dates
are inclusive RFC3339 bounds. Project usage requires explicit project IDs and
the native binding owner. Filter values never become SQL or FTS syntax.

Triangle/texture budgets sum recorded per-file measurements and fail closed
when any relevant file lacks a measurement. Recorded technical metadata does
not independently prove geometry correctness. Sorting supports relevance, name,
creation instant and descending numeric semantic version with exact
library/asset/version/hash tie-breakers. Ranking explains lexical, constraint,
completeness, compatibility and optional cosine components. Cursors bind the
query, filters, sort, index snapshot, vector cache and exact query vector.

The deterministic parser recognizes singular/plural asset kinds, `low-polygon`
or `low poly` as at most 10,000 recorded triangles, and `can walk` as the
explicit walk capability. Remaining terms use lexical matching. A phrase such
as "compatible with this animation rig" requires explicit compatibility
requirements and an authority; it never infers a rig from a pronoun.
Keyword retrieval is not embedding inference.

## Optional owner admission and deferred integration

Supplied collection owners must provide callable `collections(reference)`.
The retained `versions` argument maps library aliases to those owners.
The bounded `AssetVersions` history/promotion API from PR #460 does not supply
collections and must not be passed as that authority. Collection filtering
requires an owner for every configured library. A usage owner must provide
callable `bindings(project_id)`; only explicitly named projects are inspected.
These collection and project-binding implementations are deferred.

A dependency graph must expose the same explicit `libraries` mapping and
callable `closure`/`inspect` methods. It also requires the native module
`asset_library_dependencies.check_compatibility`, absent from the preservation
base. Its integration and native compatibility evidence remain deferred. An
explicit callable compatibility adapter remains supported for existing native
consumers; adapters retain responsibility for genuine compatibility checks.

Unsupported supplied owners fail with a typed capability diagnostic before
index creation or rebuild mutation. Optional owner capabilities are rechecked
before queries and rebuilds. Omitting them retains offline local search;
unavailable collection or usage filtering fails explicitly. Empty placeholders
are not a substitute for authority. This slice does not make the historical
asset browser ready: its collection read/write integration remains outstanding.

## Explicit semantic provider gates

`ProviderRegistry` starts empty and providers are disabled by default.
Unknown providers/models, unavailable runtime, missing required credentials and
unauthorized private-metadata transmission fail before encoding. Adapters
declare exact model/version and transmission behavior. Each remote call needs
`authorize_private_metadata=True`; enabling a provider alone grants no such
authorization. Vectors are finite, bounded, normalized and rebuildable; model,
snapshot, vector-cache or query-vector changes invalidate stale cursors.

`asset_search_embeddings.LocalSentenceTransformer` accepts only the explicitly
pinned local `sentence-transformers/paraphrase-MiniLM-L3-v2` safetensors closure.
It performs no download and uses CPU, `local_files_only=True`,
`trust_remote_code=False` and a verified private copy. Unknown model IDs,
custom modules, pickle weights, symlinks/reparse points and changed pins fail
admission. Bounds are 512 directory entries, 128 directory levels, 128 files,
64 MiB per file and 256 MiB total. Actual model/runtime execution and quality
acceptance remain separate and unverified for this preservation change.

## Preservation and pending validation

Sources: original PR #312 at `a10709b30a276a622ccfdc6c017e4c5c8c5da980`,
with corrected search/parser, bounded model scanning and Windows SQLite fixture
cleanup preserved from PR #406 at
`f1a09cfd40ad4395d194c7f93c1491ae0a798ef8`. The preparation base is
`4b89ab1ce3e76ebef69a825212ab207d746c3112`. Historical execution evidence
applies only to its recorded revision; it is not a result for this change.

All 22 preserved test methods remain, with additional optional-owner refusal
tests. Provider doubles prove dispatch/admission controls only. No Python
executor was available during preparation, so neither these tests nor the
integration gates were run. Pending commands are:

```bash
PYTHONPATH=scripts python -m unittest discover -s tests -p test_asset_library_search.py -v
python scripts/test_rig_gate.py operations-archive
python scripts/test_rig_gate.py compatibility
```

The compatibility collector discovers this test module automatically. Root
integration also adds it to the operations-archive group; shared gate files are
outside this preservation slice. No historical demonstration script is copied.

Search bounds: 32 libraries, 4,096 selected records, 2,048 query characters,
64 lexical terms, 128 project IDs, 100 results/page, 16 MiB captured metadata,
16 MiB vectors, 2,048 vector dimensions and a 64 MiB derived SQLite index.
Full dependency, collection, project usage, browser and parent #132/#128
acceptance remain open.
