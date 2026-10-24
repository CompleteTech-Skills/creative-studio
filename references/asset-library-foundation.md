# Asset Library foundation (issue #138)

This additive foundation follows the merged [baseline audit](asset-library-audit.md).
It supplies the canonical new library contract and real-file registration/resolution
facade. It does not complete #129 or #128, implement 3D import/technical validation
(#139/#140), or establish the later multi-scene rendering acceptance.

## Ownership and representation

`asset_library_contracts.py` defines the eleven version-1 conceptual schemas in
`profiles/asset-library-*.schema.json`: identity, version, file, dependency,
metadata, compatibility, preview, provenance, publication, annotations and exact
reference. `generate_asset_library_schemas.py --check` checks those definitions;
the schema drift test also runs through existing test discovery. Existing
schemas and native manifests are unchanged. Runtime validation uses the same
JSON Schema definitions, followed by semantic evidence/inventory/reference checks.
Timestamps are checked with a built-in RFC3339 spelling/calendar validator,
including standalone identity/provenance contracts, without optional format packages.
Unknown fields/versions, duplicate JSON keys, nonfinite numbers, excessive
nesting/manifest sizes, unsafe paths and invalid hashes fail with a `RigError`
containing a machine-readable `LIBRARY_*` diagnostic and affected field/path.

File contracts can retain format versions and bounded typed technical metadata.
Optional truthful source records retain the original filename, URI, acquisition
timestamp, author and known copyright holder. Providers can retain actual IDs and
bounded scalar/list generation parameters. Typed conversion and human/automated
edit histories retain exact input/output/evidence files and timestamps. Missing
historical fields stay absent; registration never invents them.

The existing `rig_errors` owner supplies bounded JSON decoding, canonical object
hashes and diagnostics. Optional NumPy, Blender and glTF tool imports are absent
from library discovery, registration and retrieval. JSON Schema validation uses
the already declared `jsonschema` dependency. `legacy_roundtrip` delegates profile,
cast, clip and rig document validation to their respective existing owners and
returns the original bytes. Clip document validation is structural: cast-bound
semantic evaluation remains `production_contracts.validate_clip`. Rig document
validation does not imply geometry, weights or deformation verification.

Library IDs use the established lowercase production identifier spelling and
exact `major.minor.patch` versions. Library identity is separate from IDs inside
native records: adapters do not rewrite valid uppercase/extended native rig IDs.
A version cannot change the stable library identity's namespace, owning library
or kind, or rewrite its original creation timestamp. `AssetRef` is `{id, version, sha256}`, with `sha256` the immutable manifest
object digest (excluding its own digest field). `scene_reference(ref, path)`
returns `{id, version, path, sha256, kind}` with a **file** digest and library
category kind; a scene consumer must map that category to its own typed asset
manifest kind. It must not equate those two digest or kind meanings.

## Storage and APIs

`AssetLibrary(new_directory, create=True)` creates one private SQLite database.
`AssetLibrary(existing_directory)` reopens it. No network or external service is
used. The database holds exact-byte SHA-256 blobs, immutable manifests and
separate mutable annotations. One SQLite transaction publishes all three or
rolls them all back; it does not coordinate an additional filesystem blob store.
Identical bytes share a blob while distinct manifests retain their own provenance.
The registry is the library authority, not a competing native rig or production
version authority. There is no separate search index in this slice.

- `read_source_bytes(path, limit=64_MiB)` exposes the bounded no-follow source
  reader for integrations; limits must be positive integers no larger than 64 MiB.
- `file_record(source, logical_path, role, required)` reads a bounded regular file,
  hashes its bytes and recognizes its header. Recognition does not decode geometry
  or certify format validity. Unsupported headers remain explicit binary payloads.
- `seal(manifest)` computes and validates the immutable manifest digest.
- `register(manifest, {logical_path: source_path})` re-reads every declared file,
  verifies size/hash/header, checks retained source/license/provider/permission
  evidence against exact inventory entries, verifies existing exact dependencies
  and their payload closure, then commits. Existing versions are preserved and
  duplicate publication fails. No provider or license receipt is manufactured.
- `resolve(ref)` verifies the manifest, every inventoried payload and exact
  dependency closure. No implicit `latest` or version range resolution exists.
- `read_file(ref, logical_path)` returns verified original bytes, without a
  filesystem export or an installation side effect.
- `annotations(ref)` and `update_annotations(ref, expected_revision=..., tags=...,
  favorite=..., note=...)` use compare-and-swap revisions. A stale writer fails;
  annotations cannot alter a version digest or source/provenance content.

This initial strict store requires every inventoried file and exact dependency,
including entries marked optional. Later policy can distinguish optional loading
without claiming absent inventoried bytes are present. Limits are 64 MiB/file,
256 MiB/version, 1 MiB/manifest, 256 file/dependency entries, 4096 versions and
1 GiB unique library payload. SQLite rollback, duplicate-writer serialization,
restart and relocation are tested. Full storage lifecycle, crash injection,
project/shared-root precedence, migration, backups, indexing and GC remain
#141/#142; this slice supplies no destructive cleanup operation.

## Safety and evidence boundary

The owned private library directory is the write trust boundary: on POSIX it must
be owned by the current user and mode 0700, with a private regular database file.
Do not move or replace it during operations or grant another principal permission
to modify it. Windows callers must maintain equivalent private directory ACLs.
SQLite owns its transaction journals; external changes to its files are outside
that trust boundary. Existing source files are never modified or deleted.

Source reads reject symbolic links/reparse points and nonregular/empty files.
POSIX uses retained directory descriptors with no-follow traversal, preventing
ancestor swaps from redirecting reads. Windows verifies the normalized final
kernel-handle path before reading: canonical long source paths are required;
unverifiable/redirected paths fail closed. System temporary-directory aliases
should be resolved by the caller before selecting source paths. Local execution
covered Linux; Windows/macOS execution remains a CI gate, not an invented result.

Library metadata remains `unreviewed` in this foundation; native review bytes may
be retained without promoting them into a library review decision.

Publication state is deliberately `payload-verified`, with private/project-local
visibility and unevaluated/restricted distribution. Registration cannot produce
public publication, human approval or geometry-ready status. License evidence is
retained; its existence does not prove redistribution rights. Existing review
status inside imported documents remains its original, independently scoped
record. Preview schemas bind exact inventoried settings, camera and lighting files to
their declared hashes, leaving those native payloads under their existing owners.
Their source version must be an exact retained dependency or parent. This
foundation does not render previews or upgrade them into production approval.

## Reproduce the real-byte demonstration

From the repository with `requirements-delivery.txt` installed:

```bash
python scripts/generate_asset_library_schemas.py --check
python scripts/demonstrate_asset_library.py --destination NEW_PRIVATE_DIRECTORY
PYTHONPATH=scripts python -m unittest discover -s tests -p test_asset_library.py -v
PYTHONPATH=scripts python -m unittest discover -s tests -p test_rig_compatibility.py -v
```

The checked-in example contains an actual editable triangle OBJ and its explicit
engineering-fixture license text. It stores, reopens and retrieves those exact
bytes. The report labels geometry validation and production approval false.
Observed source OBJ SHA-256:
`830e41de3e98d4841fcc666158ba5f55c2e542968e0dbb0c82a360909b992431`.
Observed manifest digest:
`197f615d708528669ae66912fa07709116822ceeb0ad2f5d79725d04bb0db5d0`.

## Acceptance traceability

| #138 requirement | Executed test/evidence |
| --- | --- |
| All conceptual schemas, strict versions/unknown keys and parity | `test_every_conceptual_contract_roundtrips_and_rejects_unknown_fields`, `test_schema_structural_parity_and_drift` |
| Real registration/retrieval, restart/relocation | `test_actual_geometry_registration_restart_relocation_and_exact_bytes`; real OBJ demonstration |
| Exact hashes, missing/corrupt data, inconsistent versions | `test_wrong_hash_missing_content_and_header_lie_leave_no_version`, `test_corrupt_deleted_payload_and_manifest_are_not_ready`, `test_immutable_versions_and_false_latest_are_rejected` |
| Independent mutable metadata and revision conflict | `test_annotation_cas_is_separate_and_concurrent_writers_cannot_lose_edits` |
| Provenance/license/inventory/path/finite/size validation | `test_semantic_negatives_paths_aliases_origin_inventory_evidence`, `test_symlink_source_database_and_replaced_root_fail_closed`, `test_replaced_private_root_is_rejected_before_storage_access`, `test_public_source_reader_rejects_invalid_bounds_and_nonregular_files`, `test_quota_failure_rolls_back_blobs_and_registration` |
| Dependency closure and concurrent publication | `test_exact_dependency_closure_is_verified_after_restart`, `test_duplicate_registration_writers_publish_exactly_one_version` |
| Review regressions: strict trailing-newline rejection, nonadjacent ancestor aliases, path schema parity, native configuration/source evidence and immutable creation time | `test_review_regression_trailing_newline_identifiers_and_digests_reject_registration`, `test_review_regression_nonadjacent_file_directory_alias_rejects_registration`, `test_review_regression_portable_paths_have_schema_runtime_parity`, `test_review_source_fields_and_preview_configs_retain_bound_native_data`, `test_original_creation_time_is_stable_across_versions` |
| Existing native owners and original bytes | `test_native_documents_use_original_owners_and_preserve_ids`, `test_legacy_profile_roundtrip_preserves_exact_bytes_and_2d_contract`; four existing rig compatibility cases |

The local focused result is 21 passes, zero failures/skips; the separate existing
compatibility suite passes four cases. A separate 60-case profiles, production
contracts and schema/legacy example suite passes with zero failures/skips. Exact
logs, runtime versions, source hashes and the demonstration report are retained in
[the validation receipt](../tests/results/asset-library-foundation-20261023/validation.json).
These results do not establish the whole
asset-library mission, Blender interoperability, browser inspection, geometric
validation, public distribution or human visual approval.
