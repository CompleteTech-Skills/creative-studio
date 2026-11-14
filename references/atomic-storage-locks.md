# Cooperative storage writer locks

Use `with atomic_publish.writer_lock_fd(parent_fd, name=...)` on POSIX around
aggregate quota admission, copy, and exclusive publication. Every cooperating
writer must use the same reserved name. The parent must be owned by the current
user without group/other write permission. The lock is an owned mode-0600,
single-link regular file; acquisition and release check its named inode against
the held descriptor. Namespace changes to the public parent cannot redirect the
critical section. The lock inode is permanent: do not unlink or replace it.
Another writer receives `BlockingIOError`; process exit releases the OS lock.
This is a storage critical section, not an execution queue or claim registry.

On Windows use `with atomic_publish.windows_writer_lock(root_or_pin, name=...) as
pin`. Ordinary roots are pinned through the canonical namespace adapter; an
existing live registered pin is accepted directly. The lock file has protected
process-user/LocalSystem permissions, one directory link, and deny-delete
sharing. A nonblocking one-byte CRT lock serializes writers before any quota or
copy operation. It is acquired even when the file is empty, before initialization
reads/writes. The fixed lock byte is retained and its inode is never replaced.
See Microsoft's [CRT locking contract](https://learn.microsoft.com/en-us/cpp/c-runtime-library/reference/locking)
for nonblocking locks and locking beyond the current end of file.

These APIs provide cooperative serialization. Callers still own their quota
policy, bounded inventory, immutable history, process lifecycle and recovery.
Do not substitute before/after pathname checks for a pinned authority. Existing
rig writer locks and all frozen publication defaults retain their contracts.

Windows byte installers also use canonical `require_directory_pin`, incremental
`with pin.entries()` yielding `(name, kind)`, and `with pin.pin_file(name)` yielding
`(binary_stream, stat_result)`. Readers reject reparse points and directory
leaves, deny concurrent writes/deletion, and check their held file after reading.
`stage.publish_file(name, destination, sha256=..., bytes=..., max_bytes=...)`
checks the exact held single-link file before exclusive HANDLE publication. It
publishes a real file, including ZIP bytes, rather than a directory with a file
extension. These primitives supply byte identity, not archive or project meaning.
