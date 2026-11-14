# Shared private staging and exclusive publication

`atomic_publish` remains standard-library-only. Existing
`_rename_no_replace(source, destination)` behavior is unchanged: default path
calls retain Windows exclusive rename and the existing Linux/macOS platform
operations. Optional `source_dir_fd` and `destination_dir_fd` must be supplied
together, identify open directories, and use basenames without traversal.

New publishers use this additive API:

```python
from atomic_publish import (
    private_stage_supported, pinned_directory, private_stage, _rename_no_replace,
)

if not private_stage_supported():
    raise RuntimeError('secure private staging is unavailable on this host')
with pinned_directory(existing_destination_parent) as parent_fd:
    with private_stage(parent_fd, prefix='.delivery-') as (stage, stage_fd):
        # stage is exactly /proc/self/fd/N, pinned before any caller I/O.
        payload = stage / 'payload'
        payload.mkdir()
        # Write and independently verify all payload bytes here.
        # Run the last untrusted callback BEFORE final verification.
        _rename_no_replace('payload', output_basename,
                           source_dir_fd=stage_fd,
                           destination_dir_fd=parent_fd)
```

File publishers create their partial file beneath `stage`, independently verify
its exact final bytes, then rename its basename from `stage_fd`. Directory
publishers create a fresh inner payload and rename that inner basename; they
must never publish the publicly replaceable outer temporary-directory name.
Persistent frame-cache publishers copy/revalidate the cache into a fresh private
inner payload before publication. Holding a descriptor to the cache alone does
not authenticate a later rename of its mutable public basename.

`pinned_directory(path)` opens every existing absolute/relative path component
without following symbolic links. It yields a borrowed directory descriptor for
its context lifetime. Private staging duplicates its parent descriptor, creates
an unpredictable mode-0700 directory and opens the stage descriptor immediately,
before yielding or permitting caller writes. The opened descriptor must remain a
regular directory owned by the effective user with no group/other permission
bits; a different-owner or shared-permission creation replacement is rejected
before any caller I/O. This is not isolation from a hostile process with the
same user credentials or from privileged mount/namespace manipulation. All subsequent stage input/output
must derive from the yielded procfd path or explicit descriptor-relative calls.
Never call `Path.resolve()` or `realpath()` on this capability: resolving it back
to a physical pathname discards protection against namespace replacement. Existing
owner helpers that resolve roots need a caller-specific lexical/dirfd adapter;
this staging helper does not silently patch those owners.

A subprocess using `/proc/self/fd/N` must inherit the same descriptor number:

```python
subprocess.run(command_with_stage_path, pass_fds=(stage_fd,), check=True)
```

All workers must finish before context exit. For both passes of a two-pass
encoder, pass the descriptor to both invocations. A child must preserve the
procfd spelling and leave the inherited descriptor open until its last I/O.
Parent-PID procfd paths have different permission/lifetime constraints and are
not advertised as a tested substitute. No callback that can alter filesystem
state belongs between final-byte verification and the atomic publish operation.

The new capability is explicitly limited to the tested Linux procfs/dirfd
adapter. `private_stage_supported()` reports whether it is available;
`private_stage` and `pinned_directory` raise `OSError(ENOTSUP)` before creating
staging on unsupported systems. A caller maps that to its existing typed
unavailable/unsupported diagnostic and capability report, rather than adding a
new backend identity field or falling back to unsafe paths. This does not narrow
the platform behavior of existing default `_rename_no_replace` callers.

Cleanup uses a fresh `os.listdir('/proc/self/fd/N')` stream, because listing a
borrowed directory descriptor directly can consume or reuse its directory
offset on some filesystems. Every unlink and recursive cleanup stays relative
to the pinned original directory; recursive deletion uses Python's fd-aware,
symlink-resistant implementation. Links inside the owned private tree are
unlinked, not followed. The context owns its complete private contents: callers
must not place unrelated user data there, and no writers may remain active while
cleanup runs.

The helper **never removes the public outer basename**, even after an inode
comparison. A stat-then-rmdir check can still remove a foreign empty replacement
between the check and removal. The small empty wrapper intentionally remains;
explicit owned maintenance can inspect retained wrappers separately. Replacing
or moving that public name therefore cannot redirect cleanup to an unrelated
tree or cause deletion of an empty replacement directory.

By default, owned contents are cleaned on both success and failure, and both
stage and duplicated parent descriptors close. `cleanup=False` retains all
contents. `cleanup_on_error=False` retains diagnostic contents only when the
body raises; the backend can record `os.readlink(stage)` inside its error handler
to identify the original inode's current physical location. No retention option
leaves the borrowed descriptor open after the context. If cleanup also fails
during an existing exception, the original exception is preserved with a cleanup
note when supported by the Python runtime.

The regression suite exercises default rename compatibility, exclusive existing
destinations, source/destination namespace swaps, file and inner-directory
publication, inherited child descriptors, directory-offset cleanup, symlink and
empty-directory replacements, diagnostic retention, exceptional cleanup,
unsupported hosts, unsafe paths and invalid descriptors. These are filesystem
engineering fixtures, not renderer or artwork approval.

The lookdev `rename_no_replace_at(source_fd, source_name, destination_fd,
destination_name)` entry point is preserved as a wrapper over the same exclusive
rename authority. Required `tests/test_atomic_private_stage.py` discovery uses
portable contracts with no platform skips. Linux race fixtures live under
`tests/optional` without a package initializer and run explicitly in the dedicated
Linux descriptor CI job; they do not add unapproved skips to the required 2D
compatibility discovery. The focused workflow also executes portable contracts
on Linux, Windows and macOS without claiming private staging works off Linux.
