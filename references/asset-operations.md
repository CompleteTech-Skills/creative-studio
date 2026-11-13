# Local asset operations v1

`python scripts/assets.py capabilities` discovers installed adapters without
loading optional 3D dependencies. `python scripts/hatch.py assets ...` delegates
to the same CLI. Every command and subcommand supports `--help`; JSON results go
to stdout, structured failures to stderr. Exit 0 means success, 2 means a request
or explicit-apply error, and 1 means an operation/environment failure.

Create a private library explicitly:

```sh
python scripts/assets.py library create --library /owned/new-library --apply
python scripts/assets.py library inspect --library /owned/new-library
python scripts/assets.py import --library /owned/new-library --request import.json --apply
python scripts/assets.py jobs list --library /owned/new-library
python scripts/assets.py jobs run --library /owned/new-library --job JOB_ID --apply
python scripts/assets.py jobs status --library /owned/new-library --job JOB_ID
```

Mutation requests without `--apply` fail. `--dry-run` reports the request and
performs no action; it does not claim conversion or full semantic preflight.
`--request` accepts a bounded 64 KiB JSON object. Exact reads can use
`--asset ID --version X.Y.Z --sha256 MANIFEST_DIGEST`. `--output` writes a new
exclusive report file. Existing files are preserved. `--library`, `--project`,
`--jobs-root` and all destinations are explicit; parent traversal and symlink
paths are rejected. No model call, remote service, installation or background
server starts implicitly.

`AssetOperations(library, jobs_root=None, job_limits=None)` is the local typed
facade. `execute(operation, parameters, apply=False, dry_run=False)` returns a
versioned result. It delegates manifest registration, exact payload resolution,
annotations, versions/collections/bindings, dependencies, search, previews,
exports, packages, validation and storage to their canonical modules. Optional
or missing capabilities return `LIBRARY_OPERATIONS_UNAVAILABLE`; a listed CLI
verb alone does not imply its adapter is available. Semantic providers require
the canonical search API's configured trusted provider; arbitrary provider code
is not accepted in JSON. Canonical unbind and remote publication remain unavailable. Bounded native
box/sphere generation is documented in [managed generation](asset-managed-generation.md). Metadata writes remain bounded synchronous native transactions; version,
replacement, repair, conversion, preview, export, index and package work use
managed jobs.

Managed `submit(operation, parameters)` returns a durable queued job, not a
finished asset. `run_job`, `resume_job`, `cancel_job`, `job_status`, and `list_jobs`
use the [shared managed job journal](asset-managed-jobs.md). Import requests use
the canonical importer signature (`source`, `identity`, `version`, `metadata`,
`license_file`, and explicit optional fixture/basis/unit/Blender policies).
Source closure and license hashes are pinned at admission and compared with
the actual retained import evidence before final publication. Imports convert
in a private canonical library, then register unchanged verified bytes in the
final canonical library. Recovery recognizes the same exact immutable digest;
conflicting versions fail. Index/preview/export/package outputs retain verified
staging and use a sibling copy plus the existing exclusive rename helper, so
process exit after publication cannot cause a second publication or erase a
racing destination. Interrupted verified staging is retained for explicit resume. Index idempotency
includes exact library references and annotation revisions. Rebuilt indexes use an
explicit new generation destination; an existing generation is preserved, and
searching a stale generation reports the canonical stale-cache diagnostic. Jobs
with uncertain verified publication must recover before cancellation can claim
that no publication occurred.

Resource pool ceilings and runtime/attempt/staging budgets are persisted in the
job store. `--limits limits.json` configures lower ceilings for a new store;
conflicting reopen configuration fails. CPU, Blender, preview, archive, I/O,
hashing and indexing have separate process-shared pools. Canonical owners enforce
geometry, texture, dependency, expanded archive, memory and renderer budgets.
The journal bounds request size, staging files/bytes, retries and cooperative
runtime checks; it cannot preempt arbitrary trusted Python callback code. Native
subprocess owners enforce their own timeouts. No GPU-memory measurement is
claimed. Copies and immutable staged evidence consume additional disk space;
no implicit cleanup deletes originals or transaction evidence.

For HTTP, inject `ScopedAssetOperations(owner, intake_root=..., output_root=...)`
in the canonical browser host. It rejects parent traversal, paths outside the
configured roots and executable overrides; the jobs store must lie beneath the
output root. Use its `supported_operations`. Authentication, origin checks and
bounded HTTP transport remain browser-host responsibilities. Never inject the
unrestricted local filesystem facade directly into an HTTP request dispatcher.

The operations tests exercise actual canonical registry/index transactions,
process exit after publication, cancellation/resume, scope escape rejection,
exclusive reports, and native import/original preservation. Synthetic engineering
fixtures establish operational behavior, not artistic or production approval.

## Managed version and repair operations

The management extension uses the same job journal for `version`, `replace` and
`repair`. Version requests select an explicit `action` (`create`, `promote`, or
`rollback`); `assets version create/promote/rollback` selects that action through
the CLI. Requests capture native CAS revisions and immutable source identities.
They never select an implicit latest version. Replacement requests contain an
exact canonical replacement `plan` and explicit consumer `versions`. Repair
requests contain an authorized exact-byte `source`, `digest`, and
`expected_bytes`; repair cannot authorize arbitrary blob substitution.

Management stages use the existing canonical library, lifecycle and dependency
owners. Each publisher has an authoritative recovery probe for the same token
and exact output; native CAS and immutable registration reject concurrent
conflicts. Replacing several immutable consumer versions is recoverable per
version and does not claim a multi-library atomic transaction or automatically
activate native project payloads. User-owned originals remain read-only.

`assets browser --library LIBRARY --index INDEX --port 8768` starts the real
canonical loopback browser server explicitly. Add all three `--jobs-root`,
`--intake-root`, and `--output-root` flags to enable scoped managed operations.
Starting the server does not execute a queued operation or install any asset.
