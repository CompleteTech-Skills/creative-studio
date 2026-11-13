# Exact dependency graphs and replacement

The dependency layer uses `AssetLibrary` as the byte and version authority. It
adds no storage database. Every configured library handle is explicit permission
to read that library; metadata cannot introduce a filesystem path or remote store.
The dependency schemas and normal graph operations need no optional 3D package.

```python
from asset_library import AssetLibrary
from asset_library_dependencies import DependencyGraph, locator

graph = DependencyGraph({"project": AssetLibrary("/private/project-library")})
asset = locator("project", exact_asset_ref)
closure = graph.closure([asset])
lock = graph.lock([asset])
graph.verify_lock(lock)
usage = graph.reverse(asset)
```

`direct` reads explicit native library dependencies plus retained provenance and
style references. `closure` traverses iteratively with node/edge limits and checks
actual stored manifest/payload bytes. It reports required missing/corrupt content,
cycles, duplicate edges and conflicting active versions. Historical provenance
versions can coexist. Optional absent dependencies are reported separately;
`include_optional=False` records excluded edges instead of making them look
resolved. A required path always remains required even when an optional path was
visited first. Duplicate roots and excessive limits fail.

`catalog` uses the bounded owner inventory; `reverse` reports direct and transitive
consumers. `dot` emits safe Graphviz text with generated node IDs and JSON-escaped
labels. The JSON graph itself is also directly usable by an editor. It is a
snapshot: callers must revalidate its lock before using a cached result.

A lock binds the selected source-library alias, stable library identity, exact
asset ID/version/manifest digest and every inventoried file digest/size. A changed
file, missing file, altered lock or changed source-library selection invalidates
it. There is no `latest` resolution. `verify_lock` re-executes the closure; a lock
is not proof that future mutable state will remain valid.

## Technical compatibility

`check_compatibility(graph, asset, requirements, native_path=...)` compares exact
coordinate/unit requirements and required material, texture, format, animation,
renderer and extension capabilities. Declared renderer names are capabilities,
not evidence that a renderer was executed. Every report sets
`renderer_execution_verified` to false.

For skeleton/rest/skin/morph/material fingerprints and root-motion conventions,
the caller must supply an exact inventoried native entry. `native_signature`
reconstructs a private byte snapshot and invokes the existing rig/geometry owners.
It supports full `rig.json` closures and actual GLB/glTF/OBJ inputs. Native checks
use the optional rig dependencies; they never substitute schema validation for
geometry decoding. Bone names alone cannot establish compatibility: hierarchy,
roles, deformation flags, rest transforms, head/tail positions, inverse binds,
joint mappings, weights, morph targets, material values and root-motion modes are
covered separately. Native rig files also expose their authored archetype;
interchange inputs with no authoritative archetype are classified as imported.

`normalize_static_units` is an explicit deterministic conversion for static
canonical-coordinate meshes. It loads the real source, applies its declared unit
scale through the native importer, exports a GLB, independently reopens the GLB
and compares coordinates/topology. It registers a new derived immutable version,
retaining original files and exact parent/conversion provenance. The result names
the new native entry. Skins, skeletons and animation reject this narrow adapter;
they require a real owner-provided retarget/conversion instead of silently losing
channels. No provider, rendered preview or production approval is claimed.

## Targeted replacement and recovery

`replacement_plan(graph, old, new, consumers=[...], requirements={...})` names
exact old/new locators and specific dependency purposes for each consumer. It
checks compatibility and reports transitive invalidation. Callers should pass
the required native fingerprints when replacing rigs or animation bindings.
`apply_replacement(graph, plan, versions={...})` rechecks both locks and the complete
proposed consumer closure before any publication. It copies the original exact
files and creates new derived consumer manifests through `AssetLibrary.register`.
Only selected dependency slots change; old versions and unrelated consumers stay
unchanged. Receipts retain explicit rollback references.

Each consumer publication is one owner transaction. A multi-consumer request is
not represented as an atomic cross-library transaction; earlier inactive versions
remain available if a later publication fails. The layer does not overwrite
native project files or activate a replacement in an editor. Project activation,
undo and render-cache updates remain with those existing owners. `invalidated`
is a precise dependency impact report, not a claim that rendering happened.

The current canonical dependency records contain exact asset refs but no source
alias. Replacement therefore verifies that the selected old/new aliases are the
ones that the consumer would actually resolve. A cross-library binding that would
lose that selection fails explicitly and needs the scoped consumer adapter; it
is never silently rewritten to a same-named local asset.

`recover(asset, path, donors=[...])` obtains only the exact expected size and hash
from explicitly supplied, verified donor versions. The storage owner repairs the
missing/corrupt referenced blob transactionally. Unrelated filenames, different
hashes, unavailable manifests and unverified donors do not repair anything.
Original immutable manifests and old version records are retained.

## Validation

```sh
python scripts/generate_asset_dependency_schemas.py --check
PYTHONPATH=scripts:tests python -m unittest discover -s tests -p test_asset_dependencies.py -v
PYTHONPATH=scripts:tests python -m unittest discover -s tests -p test_rig_asset_dependencies.py -v
```

The first suite uses real SQLite libraries and actual OBJ bytes. It exercises
transitive/reverse use, immutable locks, optional/required missing content,
version conflicts, exact donor recovery, targeted replacement, source-library
mismatch and rejection before publishing a newly conflicting closure. The native
suite decodes real animated/skinned GLBs with the same bone names but different
rest poses, tests morph/skin compatibility, and executes static-unit conversion
and reopen. Engineering fixtures are not production artwork approval.
