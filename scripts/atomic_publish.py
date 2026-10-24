"""Standard-library-only filesystem publication shared by archive workflows."""
import ctypes
import errno
import os
import sys


def _rename_no_replace(stage, destination):
    """Publish atomically without replacing even a racing empty directory.

    Windows os.rename has exclusive semantics. POSIX os.rename does not, so
    use the operating system's exclusive rename and fail closed if unsupported.
    Linux: man7.org/linux/man-pages/man2/rename.2.html (RENAME_NOREPLACE).
    macOS: Apple's sys/stdio.h and renamex_np(2) (RENAME_EXCL).
    """
    if os.name == 'nt':
        os.rename(stage, destination)
        return
    library = ctypes.CDLL(None, use_errno=True)
    old, new = os.fsencode(stage), os.fsencode(destination)
    if sys.platform.startswith('linux') and hasattr(library, 'renameat2'):
        operation = library.renameat2
        operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(-100, old, -100, new, 1)  # AT_FDCWD; RENAME_NOREPLACE
    elif sys.platform == 'darwin' and hasattr(library, 'renamex_np'):
        operation = library.renamex_np
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        operation.restype = ctypes.c_int
        result = operation(old, new, 4)  # RENAME_EXCL
    else:
        raise OSError(errno.ENOTSUP, 'atomic exclusive directory publication is unavailable', str(destination))
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))
