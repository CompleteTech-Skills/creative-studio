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
For full-catalog rebuilds and queries, a native durable library checks that its
SQLite version inventory agrees with its immutable commits. An interrupted
publication can leave a committed version outside SQLite. In that state the
library raises `LIBRARY_DURABILITY`; the existing search index and source are
preserved. Call the library's `recover()` explicitly before rebuilding search.
SQLite-only legacy libraries continue to enumerate their existing records.

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

## Optional owner admission and native collections

Supplied collection owners must provide callable `collections(reference)`.
The retained `versions` argument maps library aliases to those owners.
Current `AssetVersions` supplies `collections` and `set_collections` through the
canonical event store; pass `versions={'local': AssetVersions(library)}` to index
that metadata. A native owner must refer to the library configured for its alias.
A separately opened handle for the same library root is valid. A portable copy
has its own mutable collection history even when its exact asset references
match. Custom collection adapters remain responsible for the alias binding.
The original bounded history/promotion API from PR #460 alone
did not supply collections. Collection filtering requires an owner for every
configured library. Changes invalidate the search snapshot until rebuild.
A usage owner must provide callable `bindings(project_id)`; only explicitly
named projects are inspected. Project-binding integration remains deferred.

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

## Preservation and validation

Sources: original PR #312 at `a10709b30a276a622ccfdc6c017e4c5c8c5da980`,
with corrected search/parser, bounded model scanning and Windows SQLite fixture
cleanup preserved from PR #406 at
`f1a09cfd40ad4395d194c7f93c1491ae0a798ef8`. The preparation base is
`4b89ab1ce3e76ebef69a825212ab207d746c3112`. Historical execution evidence
applies only to its recorded revision; it is not a result for this change.

The exact preservation source is PR #501 at
`53706b555a135c5512f4f20236e34db8836d90fe`, refreshed onto main
`094e4c5f04c255178be6a09cb10d25280201fe92`. Both implementation modules and
all 26 source test methods remain verbatim, including the 22 corrected historical
methods and four optional-owner refusal methods. One new native collections test
covers canonical event persistence, reopening, stale snapshot/cursor refusal,
rebuild and preservation of original manifests and payload bytes. The guide now
reflects the collection owner available on this main revision.

On this base, the unchanged 26 source tests passed locally with no skips before
adding the native collections test. On executable source
`8796e4e0ecfce969ea7bd93b03964ecef05a3bd7`, Linux x86_64 with Python 3.12.14,
the final search suite passed 27 tests in 5.261 seconds and the canonical
foundation/storage/version suites passed 77 tests in 3.462 seconds. Both
commands returned exit status 0 with no skips. Exact commands were:

```bash
PYTHONPATH=scripts:tests python -m unittest -v test_asset_library_search
PYTHONPATH=scripts:tests python -m unittest -v test_asset_library test_asset_library_storage test_asset_library_versions
```

These are local Linux synthetic-fixture checks. Provider doubles prove
dispatch/admission controls only; no live provider or model was called.
Historical results remain tied to their original revisions. The shared gates
require separate execution on the integrated candidate:

```bash
python scripts/test_rig_gate.py operations-archive
python scripts/test_rig_gate.py compatibility
```

The compatibility collector discovers this test module automatically. Root
integration also adds it to the operations-archive group; shared gate files are
outside this preservation slice. No historical demonstration script is copied.

Search bounds: 32 libraries, 4,096 selected records, 2,048 query characters,
64 lexical terms, 128 project IDs, 100 results/page, 16 MiB captured metadata,
16 MiB vectors, 2,048 vector dimensions and a 64 MiB derived SQLite index.
Full dependency, project usage, browser and parent #132/#128 acceptance remain
open. This search slice does not complete dependency closure/locks/replacement
acceptance in #143 or the broader #144 search integration requirements.

Native collection-owner admission also verifies both handles still name their original private directories before comparing ownership. Reopening the same root is supported; a reused pathname after directory replacement cannot admit a stale handle, create a derived index, or turn an ownership failure into a successful empty rebuild. The owner identity check is read-only and runs before capture or optional provider encoding.

### Current owner, lexical integrity and adapter declarations

Every configured native library handle must still name its original directory
before index creation, rebuild or query, including explicit selections without
collection adapters. A missing native owner is an error, not an incomplete asset.
Queries check FTS5 postings as well as displayed text against the verified source
snapshot. An inconsistent derived index raises `LIBRARY_SEARCH_INDEX`; explicit
rebuild replaces the derived lexical table transactionally from captured native
records. The SQLite FTS5 integrity-check command is documented at
<https://www.sqlite.org/fts5.html#the_integrity_check_command>.

Provider declarations are checked again when selected and immediately before
encoding. Missing or nonboolean runtime, credential and transmission declarations
refuse dispatch. Local/provider test doubles do not establish real inference.
The documented `compatible with this animation rig` clause (also `this rig`) is
query syntax requiring explicit compatibility requirements and an authority;
other words remain lexical terms. A structured filter that contradicts a parsed
constraint raises `LIBRARY_SEARCH_REQUEST`, so explanations never claim a
discarded constraint matched.
