# Durable asset storage and recovery

`AssetLibrary` remains the canonical asset registry. New libraries add immutable
records under `records/blobs` and `records/commits`; `library.sqlite3` is their
query projection. Existing SQLite-only libraries remain readable. Call
`upgrade_storage()` explicitly before using durable events, recovery, backups or
garbage collection on an old library. Migration preserves manifest and payload
bytes, exact references, and the latest annotation revision. A legacy annotation
checkpoint identifies the actual retained revision without inventing lost history.

## Publication and failure boundaries

Registration verifies the source files and semantic manifest through the existing
owner. A SQLite `BEGIN IMMEDIATE` transaction serializes all writers. New blob
bytes are staged in a private directory, flushed, exclusively renamed and read
back. A content-addressed immutable commit then binds the manifest. Only after
that durable commit does the SQLite transaction finish. Metadata and event writes
use the same ordering. POSIX directory entries are fsynced; Windows uses flushed
files and its exclusive rename semantics, without claiming unsupported directory
fsync.

These are two explicit commit boundaries, not a claim of cross-filesystem/database
atomicity. A process killed between them leaves durable history ahead of the
index. `recover()` verifies all records and referenced blobs, then replaces index
rows within one SQLite transaction. A failed recovery rolls that transaction back.
Conflicting retries fail until recovery; they cannot silently overwrite the
durable revision. Partial files in `records/staging` are never interpreted as
committed assets or deleted by garbage collection.

If the SQLite file is lost, `AssetLibrary.rebuild_index(root)` builds a new index
in a temporary file from verified canonical records and publishes it exclusively.
It rejects an existing database, including a racing empty destination. Restore a
damaged but usable index with `recover()`. A damaged canonical blob is an error;
an intact cache does not make it ready. `recover_blob(digest, payload,
expected_bytes=...)` can repair only an already referenced missing/corrupt index
blob when the canonical bytes remain intact. It cannot introduce new assets or
replace canonical evidence.

## APIs and bounds

```python
library = AssetLibrary(private_root)
library.storage_status()                 # exact record snapshot and byte usage
library.recover()                        # verified index replay
library.backup(new_private_directory)    # portable, independently rebuilt copy
plan = library.analyze_gc()              # inspect exact immutable plan
library.apply_gc(plan)                   # fails if records/candidates changed
```

`backup()` copies only verified committed records, rebuilds the destination index,
and publishes to a new directory. Move it elsewhere and reopen it with
`AssetLibrary`: no absolute source path or original library is required. Original
source files are never changed. All registered versions, dependencies, source and
license evidence, previews, annotation history and durable events remain protected.
The conservative GC only deletes verified content-addressed blobs unreachable from
**every** committed version. There is no implicit retirement or old-version deletion;
project pins, shared users, rollback retention and active jobs cannot lose a
registered version through this collector. Staging data is reported and retained.

Limits retain the foundation's 64 MiB/file, 256 MiB/version and 4,096 versions.
Canonical payloads plus commit records fit within 1 GiB; each commit is at most
2 MiB, and history is at most 65,536 commits. Index reconstruction verifies its
bounded closure before changing indexed records. The implementation materializes
that bounded closure in memory; it does not claim arbitrary-size streaming.

`inspect(reference, verify_payloads=True)` and `read_own_file(reference, path)`
verify only the exact version's own content. Dependency-aware consumers must
perform their own closure admission before considering a production ready.
`list_references(limit=4096)` returns bounded exact references in stable ID/version
order. These APIs never resolve `latest` or silently substitute another library.

`append_event(stream, expected_revision=0, document={...})` appends a durable
consumer-owned event and returns `{stream, revision, document}`. `events(stream)`
verifies the index against immutable history. Streams use 1–96 lowercase ASCII
letters/digits/dots/hyphens; documents are JSON mappings up to 64 KiB. The first
event has revision 1, writes use optimistic revision checks, and history must be
contiguous. Consumers own the semantic event contracts; this generic store does
not confer lifecycle approval, asset readiness, licensing or review decisions.

The existing private owned-directory trust boundary remains required, including
equivalent private ACLs on Windows. Symlink/reparse and hardlink record aliases
fail closed. The storage does not provide a hostile same-user process sandbox.

## Executed acceptance

`tests/test_asset_library_storage.py` executes a real child-process exit after the
canonical commit and before SQLite commit; total index loss and relocation;
annotation/event replay; canonical corruption; exact cache repair; legacy
checkpoint migration; safe backup; stale/tampered GC plans; real symlink/hardlink
rejection; and protected source/dependency/staging retention. Existing
`test_asset_library.py` covers concurrent registration, annotation CAS, quota
rollback and legacy source preservation. Run:

```sh
PYTHONPATH=scripts:tests python -m unittest discover -s tests -p 'test_asset_library*.py' -v
```

Local Linux tests are implementation evidence, not hosted cross-platform results.
The broader job scheduler, library search, lifecycle policy and importer remain
their respective owners' responsibilities.

Existing handles refresh the canonical-storage mode after acquiring the SQLite writer lock. A legacy handle that waits for another handle to migrate therefore publishes its subsequent versions and annotation changes to canonical history. Successful migration is idempotent across handles. Staged recovered SQLite indexes are synchronized through a writable descriptor, as required on Windows.
