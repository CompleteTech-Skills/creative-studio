"""Standard-library-only filesystem publication shared by archive workflows."""
import ctypes
import errno
import os
import sys
import stat


def _rename_no_replace(stage, destination, *, source_dir_fd=None, destination_dir_fd=None):
    """Publish atomically without replacing even a racing empty directory.

    Windows os.rename has exclusive semantics. POSIX os.rename does not, so
    use the operating system's exclusive rename and fail closed if unsupported.
    Linux: man7.org/linux/man-pages/man2/rename.2.html (RENAME_NOREPLACE).
    macOS: Apple's sys/stdio.h and renamex_np(2) (RENAME_EXCL).
    Optional directory descriptors pin both parents across path replacement;
    this mode accepts basenames only and fails closed where unsupported.
    """
    relative = source_dir_fd is not None or destination_dir_fd is not None
    if relative:
        if source_dir_fd is None or destination_dir_fd is None:
            raise ValueError('both source and destination directory descriptors are required')
        for descriptor in (source_dir_fd, destination_dir_fd):
            if type(descriptor) is not int or descriptor < 0 or not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ValueError('publication descriptor must identify an open directory')
        for name in (stage, destination):
            raw = os.fspath(name)
            if not isinstance(raw, str) or raw in ('', '.', '..') or '/' in raw or '\\' in raw or '\0' in raw:
                raise ValueError('descriptor-relative publication requires a single filename')
    if os.name == 'nt':
        if relative:
            raise OSError(errno.ENOTSUP, 'descriptor-relative publication is unavailable on Windows')
        os.rename(stage, destination)
        return
    library = ctypes.CDLL(None, use_errno=True)
    old, new = os.fsencode(stage), os.fsencode(destination)
    if sys.platform.startswith('linux') and hasattr(library, 'renameat2'):
        operation = library.renameat2
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(source_dir_fd if relative else -100, old, destination_dir_fd if relative else -100, new, 1)  # AT_FDCWD; RENAME_NOREPLACE
    elif sys.platform == 'darwin' and relative and hasattr(library, 'renameatx_np'):
        operation = library.renameatx_np
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(source_dir_fd, old, destination_dir_fd, new, 4)  # RENAME_EXCL
    elif sys.platform == 'darwin' and not relative and hasattr(library, 'renamex_np'):
        operation = library.renamex_np
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(old, new, 4)  # RENAME_EXCL
    else:
        raise OSError(errno.ENOTSUP, 'atomic exclusive directory publication is unavailable', str(destination))
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def rename_no_replace_at(source_fd, source_name, destination_fd, destination_name):
    """Compatibility entry point for existing lookdev descriptor publishers."""
    return _rename_no_replace(source_name, destination_name,
                              source_dir_fd=source_fd, destination_dir_fd=destination_fd)


def windows_private_stage_supported():
    """Whether the separate native Windows handle-pinning adapter exists."""
    from atomic_windows import supported
    return supported()


def windows_private_stage(parent, **policies):
    """Context yielding ``stage.path`` and exact ``stage.publish_to(path)``.

    Windows writers and verifiers use the ordinary path only while the context
    is active, finish all I/O, then publish the held payload through its method.
    This additive platform adapter does not alter the POSIX staging defaults.
    """
    from atomic_windows import private_stage
    return private_stage(parent, **policies)


# New staging capabilities intentionally have a narrower platform contract than
# the legacy path-based exclusive rename above. Never silently fall back to an
# unpinned temporary pathname on an unsupported host.
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import tempfile
import secrets


def private_stage_fd_supported():
    """Whether POSIX can pin, list and clean private stages without procfs."""
    return (os.name == 'posix'
            and all(hasattr(os, name) for name in ('O_DIRECTORY', 'O_NOFOLLOW'))
            and all(operation in os.supports_dir_fd
                    for operation in (os.open, os.mkdir, os.stat, os.unlink, os.rmdir))
            and os.listdir in os.supports_fd
            and bool(getattr(shutil.rmtree, 'avoids_symlink_attacks', False)))


def _clear_private_directory_fd(descriptor):
    # Open a fresh directory stream: dup() alone shares its directory offset.
    stream = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=descriptor)
    try:
        names = os.listdir(stream)
    finally:
        os.close(stream)
    for child in names:
        try:
            mode = os.stat(child, dir_fd=descriptor, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                shutil.rmtree(child, dir_fd=descriptor)
            else:
                os.unlink(child, dir_fd=descriptor)
        except FileNotFoundError:
            pass


@contextmanager
def private_stage_fd(parent_fd, *, prefix='.publish-', cleanup=True, cleanup_on_error=True):
    """Lend a private directory FD on supported POSIX systems.

    This additive adapter has no procfs requirement. All caller operations must
    use the FD, and directory publishers must publish an inner payload through
    descriptor-relative exclusive rename. Contents are cleaned relative to the
    pinned FD; the public outer basename is never removed. As with private_stage,
    same-UID hostile process isolation is outside this ownership boundary.
    The descriptor belongs to this context; callers must not close or reuse it.
    """
    if not private_stage_fd_supported():
        raise OSError(errno.ENOTSUP, 'descriptor-only private staging is unavailable')
    _validate_directory_descriptor(parent_fd)
    if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', prefix):
        raise ValueError('private-stage prefix must be one bounded portable filename component')
    if type(cleanup) is not bool or type(cleanup_on_error) is not bool:
        raise ValueError('private-stage cleanup policies must be booleans')
    parent_identity = os.fstat(parent_fd)
    parent = os.dup(parent_fd)
    descriptor = None
    identity = None
    body_error = None
    try:
        borrowed = os.fstat(parent)
        if (borrowed.st_dev, borrowed.st_ino) != (parent_identity.st_dev, parent_identity.st_ino):
            raise OSError(errno.ESTALE, 'private stage parent descriptor changed during acquisition')
        _validate_directory_descriptor(parent)
        for _ in range(64):
            name = prefix + secrets.token_hex(16)
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError(errno.EEXIST, 'private stage name collision budget exhausted')
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=parent)
        _validate_directory_descriptor(descriptor)
        identity = os.fstat(descriptor)
        if identity.st_uid != os.geteuid() or stat.S_IMODE(identity.st_mode) & 0o077:
            raise OSError(errno.EPERM, 'created private stage must be owned by this user with no group/other permissions')
        try:
            yield descriptor
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            if cleanup and (body_error is None or cleanup_on_error):
                try:
                    current = os.fstat(descriptor)
                    if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                        raise OSError(errno.ESTALE, 'private stage descriptor was closed and reused')
                    _clear_private_directory_fd(descriptor)
                except BaseException as cleanup_error:
                    if body_error is None:
                        raise
                    if hasattr(body_error, 'add_note'):
                        body_error.add_note('Pinned staging cleanup also failed: ' + str(cleanup_error))
    finally:
        if descriptor is not None:
            try:
                current = os.fstat(descriptor)
                if identity is None or (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                    os.close(descriptor)
            except OSError:
                pass
        os.close(parent)


def private_stage_supported():
    """Whether the tested Linux procfs + descriptor-relative adapter exists."""
    return (sys.platform.startswith('linux') and Path('/proc/self/fd').is_dir()
            and all(hasattr(os, name) for name in ('O_DIRECTORY','O_NOFOLLOW','O_NONBLOCK'))
            and os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd
            and bool(getattr(shutil.rmtree, 'avoids_symlink_attacks', False)))


def _private_support():
    if not private_stage_supported():
        raise OSError(errno.ENOTSUP, 'pinned private staging requires the tested Linux procfs/dirfd adapter')


def _validate_directory_descriptor(descriptor):
    if type(descriptor) is not int or descriptor < 0 or not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise ValueError('parent descriptor must identify an open directory')


@contextmanager
def _registered_directory(path):
    # The rig authority alone recognizes explicit, currently active internal
    # capabilities. Ordinary archive users need not import that subsystem.
    try:
        from rig_errors import scoped_directory_descriptor
    except ImportError:
        scoped_directory_descriptor = None
    if scoped_directory_descriptor is None:
        yield None
    else:
        with scoped_directory_descriptor(path) as descriptor:
            yield descriptor


@contextmanager
def _pinned_directory_common(path):
    """Open every existing path component without following symbolic links.

    The borrowed descriptor is valid only inside this context. Do not call
    resolve()/realpath() on a subsequent procfd capability: that discards its
    protection against namespace replacement.
    """
    with _registered_directory(path) as descriptor:
        if descriptor is not None:
            yield descriptor
            return
    path = Path(path).absolute()
    if any(part == '..' for part in path.parts):
        raise ValueError('pinned directory path must not contain parent traversal')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def pinned_directory(path):
    """Existing Linux path-adapter parent pinning contract."""
    _private_support()
    with _pinned_directory_common(path) as descriptor:
        yield descriptor


@contextmanager
def pinned_directory_fd(path):
    """Lend a parent directory FD on POSIX without requiring procfs paths.

    The single canonical no-follow component walk is shared with the existing
    Linux adapter. Callers retain descriptor-relative I/O on macOS; this does not
    expose an ordinary mutable path as a substitute for an FD capability.
    """
    if not private_stage_fd_supported():
        raise OSError(errno.ENOTSUP, 'descriptor-only parent pinning is unavailable')
    with _pinned_directory_common(path) as descriptor:
        yield descriptor


def _clear_private_directory(descriptor):
    # Use a new procfd directory stream on every cleanup. os.listdir(fd) may
    # share/consume a borrowed descriptor's directory offset on some filesystems.
    # Each destructive operation remains relative to this exact pinned parent.
    for child in os.listdir(Path('/proc/self/fd') / str(descriptor)):
        try:
            mode = os.stat(child, dir_fd=descriptor, follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                # The fd-aware stdlib implementation verifies opened directory
                # identity and refuses symlink substitution during recursion.
                shutil.rmtree(child, dir_fd=descriptor)
            else:
                os.unlink(child, dir_fd=descriptor)
        except FileNotFoundError:
            pass


@contextmanager
def private_stage(parent_fd, *, prefix='.publish-', cleanup=True, cleanup_on_error=True):
    """Yield ``(Path('/proc/self/fd/N'), N)`` for a new pinned private directory.

    Open N immediately after creating the unpredictable mode-0700 directory,
    before any caller I/O. All caller paths must derive from the yielded procfd
    path and must retain that spelling, rather than resolve it back to a mutable
    pathname. Subprocesses using /proc/self/fd/N must inherit N explicitly through
    pass_fds and finish before context exit.

    File publishers rename a basename from N. Directory publishers create a
    fresh inner payload, verify it, then rename that payload basename from N;
    they must never rename the public outer wrapper basename. No untrusted
    callback may run between final byte verification and publication.

    Cleanup clears only contents reached through N and never removes the outer
    public basename, even after an inode comparison: stat-then-rmdir has a race
    that can delete a foreign empty replacement. The tiny empty wrapper remains
    for explicit owned maintenance. cleanup=False retains all stage contents;
    cleanup_on_error=False retains diagnostic contents only when the body raises.
    The descriptor always closes. This capability owns all private contents;
    unrelated user data must not be placed inside it and writers must finish
    before cleanup. Existing exclusive-rename default behavior is unchanged.
    """
    _private_support()
    _validate_directory_descriptor(parent_fd)
    if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', prefix):
        raise ValueError('private-stage prefix must be one bounded portable filename component')
    if type(cleanup) is not bool or type(cleanup_on_error) is not bool:
        raise ValueError('private-stage cleanup policies must be booleans')
    parent = os.dup(parent_fd)
    descriptor = None
    body_error = None
    try:
        parent_path = Path('/proc/self/fd') / str(parent)
        name = Path(tempfile.mkdtemp(prefix=prefix, dir=parent_path)).name
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        _validate_directory_descriptor(descriptor)
        identity = os.fstat(descriptor)
        if identity.st_uid != os.geteuid() or stat.S_IMODE(identity.st_mode) & 0o077:
            raise OSError(errno.EPERM, 'created private stage must be owned by this user with no group/other permissions')
        try:
            yield Path('/proc/self/fd') / str(descriptor), descriptor
        except BaseException as exc:
            body_error = exc
            raise
        finally:
            if cleanup and (body_error is None or cleanup_on_error):
                try:
                    _clear_private_directory(descriptor)
                except BaseException as cleanup_error:
                    if body_error is None:
                        raise
                    if hasattr(body_error, 'add_note'):
                        body_error.add_note('Pinned staging cleanup also failed: ' + str(cleanup_error))
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)
