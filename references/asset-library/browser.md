# Offline asset browser and local management API

`asset_library_browser.py` serves an authenticated loopback browser over explicit
canonical `AssetLibrary` handles and their existing `SearchIndex`. It stores no
second authoritative asset catalog. Search results, selected versions, inventory,
provenance, dependencies and mutable annotations come from those owners.

Create/rebuild the search index with the native search API, then run:

```bash
python scripts/asset_library_browser.py --library /private/library --index /private/search-index --port 8768
```

Open the printed loopback URL and enter the printed token. The token remains in
page memory and is sent in bearer headers; it is absent from URLs, static HTML,
browser persistence and server request logs. Do not expose this process remotely.
Exact model metadata remains available when WebGL cannot start, including revision
checked tag, favorite and note repairs. Collection filtering uses the configured
native lifecycle authority and fails explicitly if it has not been connected.

## Inspection and limits

The browser uses the repository's pinned offline Three.js 0.180.0,
`GLTFLoader` and `OrbitControls`. It loads one selected, inventoried,
self-contained GLB at a time after an independent native preflight. Orbit, pan,
zoom, keyboard camera movement, mesh selection, wireframe, skeleton overlay,
named clip selection, playback, time scrubbing and material/bone/clip details
operate on the actual GLB. No rendered image sequence substitutes for animation.

Browser model limits are stricter than general native import: encoded and decoded
buffer closure 32 MiB, 100,000 vertices, 200,000 triangles, 25,000 animation keys,
32 clips and a native working-memory estimate of 128 MiB. Existing native texture,
hierarchy, accessor, skin and finite-number checks also apply. The interactive
viewer currently requires embedded buffer-view images and buffers; external glTF,
remote URLs, data-URI resources, decoder extensions and arbitrary local paths do
not pass this adapter. Convert unsupported resources using the native importer.
This browser does not modify geometry or grant visual/distribution approval.

## Python and HTTP interfaces

```python
server = AssetBrowserServer(
    {"local": library}, search_index, port=0, token=token,
    operations=native_asset_operations,  # optional AssetOperations authority
)
```

`operations` implements the native managed-operation protocol, rather than a new
browser scheduler: `submit(operation, request)`, `list_jobs()`, `job_status(id)`
and `cancel_job(id)`. Native job submitters validate their own operation schema,
revision checks, resource budgets, staging and recovery. Without this authority,
managed actions are unavailable and no success is fabricated. The standalone CLI
currently opens a read/annotation browser; construct the server from the operations
CLI/API to enable managed conversion/version/export/replace/repair jobs.

All JSON routes use POST, `Content-Type: application/json`, a bearer token, an
exact same-origin/Host check, and a maximum 64 KiB body. API access with another
origin, an unrecognized configured library alias, an unlisted resource path,
unexpected fields or stale annotation revision fails with a structured error.

| Route | Request | Authority |
|---|---|---|
| `/api/search` | query, filters, sort, limit, cursor | native SearchIndex |
| `/api/inspect` | library, exact reference | AssetLibrary |
| `/api/model` | location, inventoried path | exact read + native import preflight |
| `/api/annotations` | location, expected_revision, tags, favorite, note | AssetLibrary CAS |
| `/api/actions/{operation}` | native operation request | injected AssetOperations |
| `/api/jobs/list` | empty object | injected native jobs |
| `/api/jobs/status`, `/api/jobs/cancel` | id | injected native jobs |

The metadata mutation result reports whether the derived search index was rebuilt.
A rebuild failure preserves the successfully committed annotation and reports
`index_current: false`; use the native index-rebuild command before searching.
Metadata is rendered through DOM text nodes. CSP denies remote scripts, frames,
workers and arbitrary connections. The server serves only a fixed static-resource
allowlist; it does not mount library roots or accept uploaded files.

## Executable validation

```bash
PYTHONPATH=scripts:tests python -m unittest test_asset_library_browser -v
HATCH_ASSET_BROWSER_TEST=1 PYTHONPATH=scripts:tests python -m unittest test_asset_library_browser -v
```

The opt-in Chromium case loads a real skinned/morphed animated fixture, compares
actual vertex positions at different clip times, exercises keyboard camera
movement and controls, saves/reopens canonical metadata, verifies no remote fetch,
and repeats metadata repair with WebGL disabled. Engineering fixtures do not
establish production visual acceptance. Native managed jobs and scene placement
are tested by their existing owners; a protocol double tests only browser dispatch.
