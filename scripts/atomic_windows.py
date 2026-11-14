"""Windows namespace-pinned private directory publication using OS handles.

The adapters pin every ancestor before caller I/O, deny delete sharing for the
entire context, reject reparse points, and rename the held payload by handle.
No pathname stat-then-rename is used as proof of source identity. This does not
isolate callers from hostile processes running as the same Windows user.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import ctypes
import errno
from functools import lru_cache
import os
from pathlib import Path, PureWindowsPath
import re
import secrets


_READ_ATTRIBUTES = 0x80
_TRAVERSE = 0x20
_DELETE = 0x10000
_DIRECTORY = 0x10
_REPARSE = 0x400
_INVALID = ctypes.c_void_p(-1).value
_DWORD = ctypes.c_uint32
_HANDLE = ctypes.c_void_p
_PINS = ContextVar('atomic_windows_directory_pins', default=())


class _Attributes(ctypes.Structure):
    _fields_ = [('attributes', _DWORD), ('reparse_tag', _DWORD)]


class _RenameInfo(ctypes.Structure):
    _fields_ = [('replace', ctypes.c_ubyte), ('root', _HANDLE),
                ('name_bytes', _DWORD), ('name', ctypes.c_wchar * 1)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('length', _DWORD), ('descriptor', ctypes.c_void_p),
                ('inherit_handle', ctypes.c_int)]


def supported():
    return os.name == 'nt' and callable(getattr(ctypes, 'WinDLL', None))


@lru_cache(maxsize=1)
def _api():
    if not supported():
        raise OSError(errno.ENOTSUP, 'Windows handle-pinned publication is unavailable')
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [ctypes.c_wchar_p, _DWORD, _DWORD,
                               ctypes.c_void_p, _DWORD, _DWORD, _HANDLE]
    api.CreateFileW.restype = _HANDLE
    api.CloseHandle.argtypes = [_HANDLE]
    api.CloseHandle.restype = ctypes.c_int
    api.GetFileInformationByHandleEx.argtypes = [_HANDLE, ctypes.c_int,
                                                ctypes.c_void_p, _DWORD]
    api.GetFileInformationByHandleEx.restype = ctypes.c_int
    api.SetFileInformationByHandle.argtypes = [_HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, _DWORD]
    api.SetFileInformationByHandle.restype = ctypes.c_int
    api.CreateDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(_SecurityAttributes)]
    api.CreateDirectoryW.restype = ctypes.c_int
    api.LocalFree.argtypes = [ctypes.c_void_p]
    api.LocalFree.restype = ctypes.c_void_p
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p, _DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    api._security = security
    return api


def _error(path):
    error = ctypes.get_last_error()
    if error in (80, 183):
        return FileExistsError(errno.EEXIST, 'exclusive publication target exists', str(path))
    return ctypes.WinError(error)


def _close(handle):
    if not _api().CloseHandle(handle):
        raise _error('directory handle')


def _attributes(handle):
    result = _Attributes()
    if not _api().GetFileInformationByHandleEx(handle, 9, ctypes.byref(result),
                                              ctypes.sizeof(result)):
        raise _error('directory handle attributes')
    return result.attributes


def _open(path, *, delete=False, traverse=True):
    # FILE_SHARE_READ|FILE_SHARE_WRITE deliberately omits FILE_SHARE_DELETE.
    # OPEN_REPARSE_POINT inspects the link itself rather than following it.
    handle = _api().CreateFileW(str(path), _READ_ATTRIBUTES | (_TRAVERSE if traverse else 0) | (_DELETE if delete else 0),
                               3, None, 3, 0x02000000 | 0x00200000, None)
    if handle == _INVALID:
        raise _error(path)
    return handle


def _mkdir_private(path):
    # Protected DACL: only this object's owner and LocalSystem, inherited by
    # files and subdirectories. A Python mkdir(mode=0700) alone is not private
    # on Windows, where the numeric mode does not establish this ACL.
    descriptor = ctypes.c_void_p()
    api = _api()
    if not api._security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            'D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)', 1,
            ctypes.byref(descriptor), None):
        raise _error(path)
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
        if not api.CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise _error(path)
    finally:
        api.LocalFree(descriptor)


def _component(name):
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or any(character in name for character in ('/', '\\', ':', '\0'))
            or name[-1] in (' ', '.') or any(ord(character) < 32 for character in name)
            or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name)):
        raise ValueError('Windows publication names must be ordinary single filename components')
    return name


def _path(path):
    raw = os.fspath(path)
    if not isinstance(raw, str) or '\0' in raw:
        raise ValueError('Windows publication paths must be strings')
    original = PureWindowsPath(raw)
    if '..' in original.parts or original.drive and not original.root:
        raise ValueError('Windows publication paths cannot traverse parents or use drive-relative paths')
    result = Path(raw).absolute()
    parsed = PureWindowsPath(str(result))
    if not re.fullmatch(r'[A-Za-z]:', parsed.drive) or parsed.root != '\\':
        raise ValueError('Windows handle publication requires an ordinary local drive path')
    for part in parsed.parts[1:]:
        _component(part)
    return result


class _DirectoryPin:
    def __init__(self, path, handle, stage=None):
        self.path = path
        self._handle = handle
        self._active = True
        self._stage = stage

    @contextmanager
    def pin_child(self, name, *, create=False):
        """Pin a physical direct child without reopening the held ancestors."""
        if not self._active or self._stage is not None and self._stage.published:
            raise ValueError('directory pin is inactive or has already been published')
        _component(name)
        if type(create) is not bool:
            raise ValueError('child creation policy must be a boolean')
        child = self.path / name
        if create:
            child.mkdir(mode=0o700)
        with pinned_directory(child) as pin:
            yield pin


@contextmanager
def pinned_directory(path):
    """Lend a stable ordinary Path while all its directory ancestors are pinned."""
    _api()  # Unsupported hosts fail before inspecting or touching caller paths.
    path = _path(path)
    handles = []
    registration = None
    result = None
    owner = None
    counted = False
    try:
        parent = None
        suffix = None
        for candidate in sorted(_PINS.get(), key=lambda pin: len(pin.path.parts), reverse=True):
            if not candidate._active or candidate._stage is not None and candidate._stage.published:
                continue
            try:
                relative = path.relative_to(candidate.path)
            except ValueError:
                continue
            parent = candidate
            suffix = relative.parts
            break
        if parent is not None:
            current = parent.path
            handle = parent._handle
            owner = parent._stage
            attributes = _attributes(handle)
            if not attributes & _DIRECTORY or attributes & _REPARSE:
                raise ValueError('registered Windows directory is no longer valid')
            parts = suffix
        else:
            current = Path(path.anchor)
            parts = (None, *path.parts[1:])
        for part in parts:
            if part is not None:
                current /= part
            handle = _open(current)
            handles.append(handle)
            attributes = _attributes(handle)
            if not attributes & _DIRECTORY or attributes & _REPARSE:
                raise ValueError('Windows publication ancestors must be physical directories without reparse points')
        result = _DirectoryPin(path, handle, owner)
        if owner is not None and handles:
            owner._children += 1
            counted = True
        registration = _PINS.set((*_PINS.get(), result))
        yield result
    finally:
        if registration is not None:
            _PINS.reset(registration)
        if result is not None:
            result._active = False
        for handle in reversed(handles):
            _close(handle)
        if counted:
            owner._children -= 1


def _rename_handle(handle, destination):
    # All destination ancestors are already held without delete sharing. The
    # fully qualified spelling is therefore stable for this operation. Use the
    # documented NULL-RootDirectory form: the Win32 adapter on supported runners
    # rejects a non-NULL relative root with ERROR_INVALID_PARAMETER.
    encoded = str(destination).encode('utf-16-le')
    offset = _RenameInfo.name.offset
    size = max(ctypes.sizeof(_RenameInfo), offset + len(encoded) + 2)
    buffer = ctypes.create_string_buffer(size)
    header = _RenameInfo.from_buffer(buffer)
    header.replace = 0
    header.root = None
    header.name_bytes = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded, len(encoded))
    if not _api().SetFileInformationByHandle(handle, 3, buffer, size):
        raise _error(destination)


def _delete_handle(handle):
    delete = ctypes.c_ubyte(1)
    if not _api().SetFileInformationByHandle(handle, 4, ctypes.byref(delete), 1):
        raise _error('owned private entry')


def _clear_owned(path, *, depth=0, count=None):
    if count is None:
        count = [0]
    if depth > 64:
        raise ValueError('private stage cleanup exceeds directory depth budget')
    # Every directory being scanned is already held with delete sharing denied.
    with os.scandir(path) as entries:
        for entry in entries:
            count[0] += 1
            if count[0] > 100000:
                raise ValueError('private stage cleanup exceeds entry budget')
            handle = _open(entry.path, delete=True, traverse=False)
            try:
                attributes = _attributes(handle)
                if attributes & _DIRECTORY and not attributes & _REPARSE:
                    _clear_owned(Path(entry.path), depth=depth + 1, count=count)
                # Delete the exact held object, including a reparse point itself.
                _delete_handle(handle)
            finally:
                _close(handle)


class PrivateStage(_DirectoryPin):
    """A pinned Path for writers plus exclusive publication of the exact payload."""
    def __init__(self, path, handle):
        super().__init__(path, handle)
        self._stage = self
        self._children = 0
        self.published = False

    def publish_to(self, destination):
        """Rename the held payload by handle; never replace a destination."""
        if not self._active or self.published:
            raise ValueError('private stage is inactive or has already been published')
        if self._children:
            raise ValueError('close child directory pin contexts before publication')
        destination = _path(destination)
        _component(destination.name)
        with pinned_directory(destination.parent) as parent:
            _rename_handle(self._handle, parent.path / destination.name)
        self.published = True


@contextmanager
def private_stage(parent, *, prefix='.publish-', cleanup=True, cleanup_on_error=True):
    """Yield a Windows Path stage pinned before all caller writes and checks.

    Call ``stage.publish_to(destination)`` after verifying the complete payload.
    Writers must finish before publication or exit. The empty private wrapper is
    retained; cleanup never removes its public basename. Native sharing modes
    keep ancestors and payload names stable until their handles close.
    """
    _api()
    if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', prefix):
        raise ValueError('private stage prefix must be one bounded portable component')
    if type(cleanup) is not bool or type(cleanup_on_error) is not bool:
        raise ValueError('private stage cleanup policies must be booleans')
    with pinned_directory(parent) as parent_pin:
        for _ in range(64):
            wrapper = parent_pin.path / (prefix + secrets.token_hex(16))
            try:
                _mkdir_private(wrapper)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError(errno.EEXIST, 'private stage name collision budget exhausted')
        wrapper_handle = _open(wrapper)
        payload_handle = None
        stage = None
        body_error = None
        try:
            attributes = _attributes(wrapper_handle)
            if not attributes & _DIRECTORY or attributes & _REPARSE:
                raise ValueError('private wrapper changed before acquisition')
            payload = wrapper / 'payload'
            payload.mkdir(mode=0o700)
            payload_handle = _open(payload, delete=True)
            attributes = _attributes(payload_handle)
            if not attributes & _DIRECTORY or attributes & _REPARSE:
                raise ValueError('private payload changed before acquisition')
            stage = PrivateStage(payload, payload_handle)
            registration = _PINS.set((*_PINS.get(), stage))
            try:
                yield stage
            except BaseException as exc:
                body_error = exc
                raise
            finally:
                _PINS.reset(registration)
                stage._active = False
                if not stage.published and cleanup and (body_error is None or cleanup_on_error):
                    try:
                        _clear_owned(payload)
                        _delete_handle(payload_handle)
                    except BaseException as cleanup_error:
                        if body_error is None:
                            raise
                        if hasattr(body_error, 'add_note'):
                            body_error.add_note('Windows private cleanup also failed: ' + str(cleanup_error))
        finally:
            if payload_handle is not None:
                _close(payload_handle)
            _close(wrapper_handle)
