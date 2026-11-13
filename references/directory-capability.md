# Internal pinned-directory I/O

The additive, standard-library-only `rig_errors.directory_capability(fd)` context
is for trusted publisher/worker code that already holds an open directory. It is
not an uploaded-manifest field, a public path syntax or a global procfd exception.

```python
with directory_capability(stage_fd) as capability:
    root = capability.path
    with open_payload(root / 'payload', 'manifest.json') as (stream, info):
        payload = stream.read(bound + 1)
```

Entering the context validates a live directory descriptor and owns one duplicate.
The original descriptor remains the caller's property. `capability.fd`, `.path`
and `.identity` expose the live owned descriptor, its lexical procfd path and its
`(device, inode)` identity. Do not close `capability.fd` directly. The context
closes its own duplicate and removes its authority at exit. Nested capabilities
remain active concurrently, so a pinned source cache can be read while a separate
publication directory is built and verified.

`normalize_root(root)` retains a lexical anchor only when the current context
explicitly carries its matching validated capability. Outside that scope it uses
ordinary path resolution. `safe_path` and `open_payload` retain this authority;
the latter duplicates the owned root descriptor and opens relative directory/file
components without following symlinks. It retains regular-file, root-identity and
before/after payload identity checks. Both helpers reject parent traversal and
unsafe descendants. A root normalizer is not a substitute for secure creation or
publication: those operations still belong to the pinned publisher authority.

The explicitly supplied original descriptor is also a scoped alias, to support
worker job/config paths prepared before a worker duplicates its inherited FD.
Every use of that alias checks that its descriptor is still open and names the
same directory. It is rebased to the context-owned duplicate; a closed/reused alias
fails. Unrelated procfd strings do not acquire authority from another active
context. Contexts do not survive process `exec`: pass only the intended descriptor
with `pass_fds`, then have the fixed trusted worker explicitly enter its own
`directory_capability(received_fd)` scope. Do not decode arbitrary descriptor
numbers from imported asset metadata.

Protected consumers must replace anchor-stripping root `.resolve()` calls with
`normalize_root()` while their explicit capability scope is alive, and preserve
the scope through capture, loading, decoding and validation. Consumer/publisher
tests must prove that replacing an outer stage cannot redirect verified bytes or
publication. Passing helper tests alone does not certify a consumer transaction.

This explicit descriptor flow currently requires Linux procfd and directory-relative
nofollow support and fails with `RIG_DIRECTORY_UNAVAILABLE` elsewhere. Ordinary
path APIs and their platform behavior remain available and unchanged. No arbitrary
code sandbox, hostile same-process FD manipulation protection, concurrent metadata
authorization or power-loss guarantee is claimed.
