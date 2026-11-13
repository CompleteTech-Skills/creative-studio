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
