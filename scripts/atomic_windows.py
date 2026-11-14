"""Windows namespace-pinned private directory publication using OS handles.

The adapters pin every ancestor before caller I/O, deny delete sharing for the
entire context, reject reparse points, and rename the held payload by handle.
No pathname stat-then-rename is used as proof of source identity. This does not
isolate callers from hostile processes running as the same Windows user.
"""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import ctypes
import errno
from functools import lru_cache
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import hashlib
import stat


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


class _Acl(ctypes.Structure):
    _fields_ = [('revision', ctypes.c_ubyte), ('reserved', ctypes.c_ubyte),
                ('size', ctypes.c_uint16), ('count', ctypes.c_uint16),
                ('reserved2', ctypes.c_uint16)]


class _AllowedAce(ctypes.Structure):
    _fields_ = [('kind', ctypes.c_ubyte), ('flags', ctypes.c_ubyte),
                ('size', ctypes.c_uint16), ('mask', _DWORD)]


class _FileTime(ctypes.Structure):
    _fields_ = [('low', _DWORD), ('high', _DWORD)]


class _Information(ctypes.Structure):
    _fields_ = [('attributes', _DWORD), ('created', _FileTime),
                ('accessed', _FileTime), ('modified', _FileTime),
                ('volume', _DWORD), ('size_high', _DWORD), ('size_low', _DWORD),
                ('links', _DWORD), ('index_high', _DWORD), ('index_low', _DWORD)]


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
    api.GetCurrentProcess.argtypes = []
    api.GetCurrentProcess.restype = _HANDLE
    api.GetFileInformationByHandle.argtypes = [_HANDLE, ctypes.POINTER(_Information)]
    api.GetFileInformationByHandle.restype = ctypes.c_int
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        ctypes.c_wchar_p, _DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    security.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = ctypes.c_int
    security.OpenProcessToken.argtypes = [_HANDLE, _DWORD, ctypes.POINTER(_HANDLE)]
    security.OpenProcessToken.restype = ctypes.c_int
    security.GetTokenInformation.argtypes = [_HANDLE, ctypes.c_int, ctypes.c_void_p,
                                             _DWORD, ctypes.POINTER(_DWORD)]
    security.GetTokenInformation.restype = ctypes.c_int
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    security.ConvertSidToStringSidW.restype = ctypes.c_int
    security.GetSecurityInfo.argtypes = [_HANDLE, ctypes.c_int, _DWORD,
                                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                        ctypes.POINTER(ctypes.c_void_p)]
    security.GetSecurityInfo.restype = _DWORD
    security.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p,
                                                      ctypes.POINTER(ctypes.c_uint16),
                                                      ctypes.POINTER(_DWORD)]
    security.GetSecurityDescriptorControl.restype = ctypes.c_int
    security.GetAce.argtypes = [ctypes.c_void_p, _DWORD, ctypes.POINTER(ctypes.c_void_p)]
    security.GetAce.restype = ctypes.c_int
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


def _open(path, *, delete=False, traverse=True, inspect_security=False, read=False, deny_write=False):
    # FILE_SHARE_READ|FILE_SHARE_WRITE deliberately omits FILE_SHARE_DELETE.
    # OPEN_REPARSE_POINT inspects the link itself rather than following it.
    access = (_READ_ATTRIBUTES | (_TRAVERSE if traverse else 0)
              | (_DELETE if delete else 0) | (0x20000 if inspect_security else 0)
              | (0x80000000 if read else 0))
    handle = _api().CreateFileW(str(path), access,
                               1 if deny_write else 3, None, 3, 0x02000000 | 0x00200000, None)
    if handle == _INVALID:
        raise _error(path)
    return handle


def _information(handle):
    information = _Information()
    if not _api().GetFileInformationByHandle(handle, ctypes.byref(information)):
        raise _error('held file identity')
    return information


def _identity(handle):
    information = _information(handle)
    return information.volume, information.index_high, information.index_low


def _sid_string(sid):
    if not sid:
        raise ValueError('private stage security descriptor has no owner or trustee')
    result = ctypes.c_void_p()
    api = _api()
    if not api._security.ConvertSidToStringSidW(sid, ctypes.byref(result)):
        raise _error('security identifier')
    try:
        return ctypes.wstring_at(result)
    finally:
        api.LocalFree(result)


@lru_cache(maxsize=1)
def _current_user_sid():
    api = _api(); token = _HANDLE()
    if not api._security.OpenProcessToken(api.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise _error('process token')
    try:
        required = _DWORD()
        api._security.GetTokenInformation(token, 1, None, 0, ctypes.byref(required))
        if not ctypes.sizeof(ctypes.c_void_p) <= required.value <= 65536:
            raise ValueError('process token user exceeds bounded security metadata')
        buffer = ctypes.create_string_buffer(required.value)
        if not api._security.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(required)):
            raise _error('process token user')
        # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first member is PSID.
        return _sid_string(ctypes.c_void_p.from_buffer(buffer).value)
    finally:
        _close(token)


def _validate_private_wrapper(handle, *, inheritance_flags=3):
    """Validate ownership and exact protected ACL through the held object."""
    api = _api(); owner = ctypes.c_void_p(); dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    error = api._security.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None,
                                          ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        user = _current_user_sid()
        if _sid_string(owner) != user or not dacl:
            raise ValueError('private stage owner or DACL changed before acquisition')
        control = ctypes.c_uint16(); revision = _DWORD()
        if not api._security.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise _error('private stage DACL control')
        if not control.value & 0x1000 or _Acl.from_address(dacl.value).count != 2:
            raise ValueError('private stage DACL must remain protected and owner/System only')
        trustees = set()
        for index in range(2):
            pointer = ctypes.c_void_p()
            if not api._security.GetAce(dacl, index, ctypes.byref(pointer)):
                raise _error('private stage access rule')
            rule = _AllowedAce.from_address(pointer.value)
            if rule.kind != 0 or rule.flags != inheritance_flags or rule.size < 16 or rule.mask != 0x1f01ff:
                raise ValueError('private stage access rule changed before acquisition')
            trustees.add(_sid_string(pointer.value + ctypes.sizeof(_AllowedAce)))
        if trustees != {user, 'S-1-5-18'}:
            raise ValueError('private stage grants access outside its owner and LocalSystem')
    finally:
        api.LocalFree(descriptor)


@contextmanager
def _private_attributes(*, inherit=True):
    # Protected DACL: only this process user and LocalSystem, inherited by
    # files and subdirectories. A Python mkdir(mode=0700) alone is not private
    # on Windows, where the numeric mode does not establish this ACL.
    descriptor = ctypes.c_void_p()
    api = _api()
    user = _current_user_sid()
    flags = 'OICI' if inherit else ''
    if not api._security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            f'O:{user}D:P(A;{flags};FA;;;{user})(A;{flags};FA;;;SY)', 1,
            ctypes.byref(descriptor), None):
        raise _error('private security descriptor')
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, 0)
        yield attributes
    finally:
        api.LocalFree(descriptor)


def _mkdir_private(path):
    with _private_attributes() as attributes:
        if not _api().CreateDirectoryW(str(path), ctypes.byref(attributes)):
            raise _error(path)


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
        self._identity = _identity(handle)

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
    def entries(self):
        """Incrementally enumerate names/types; never follow a reparse point."""
        require_directory_pin(self)
        with os.scandir(self.path) as entries:
            def bounded_iterator():
                for entry in entries:
                    require_directory_pin(self)
                    information = entry.stat(follow_symlinks=False)
                    if getattr(information, 'st_file_attributes', 0) & _REPARSE:
                        kind = 'unsafe'
                    elif stat.S_ISDIR(information.st_mode):
                        kind = 'directory'
                    elif stat.S_ISREG(information.st_mode):
                        kind = 'file'
                    else:
                        kind = 'unsafe'
                    yield entry.name, kind
            yield bounded_iterator()

    @contextmanager
    def pin_file(self, name):
        """Lend a no-follow, read-only binary stream and its initial stat."""
        with _file_stream(self, name) as value:
            yield value


def require_directory_pin(value):
    """Accept only a live registered canonical directory capability."""
    if (not isinstance(value, _DirectoryPin) or not value._active
            or value._stage is not None and value._stage.published
            or not any(pin is value for pin in _PINS.get())):
        raise ValueError('an active registered Windows directory pin is required')
    if _identity(value._handle) != value._identity:
        raise ValueError('Windows directory pin identity changed')
    attributes = _attributes(value._handle)
    if not attributes & _DIRECTORY or attributes & _REPARSE:
        raise ValueError('Windows directory pin is not a physical directory')
    return value


@contextmanager
def _file_stream(parent, name, *, delete=False):
    require_directory_pin(parent)
    _component(name)
    handle = _open(parent.path / name, delete=delete, traverse=False, read=True, deny_write=True)
    descriptor = None
    stream = None
    counted = False
    try:
        attributes = _attributes(handle)
        if attributes & (_DIRECTORY | _REPARSE):
            raise ValueError('file reader requires a physical regular file')
        import msvcrt
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        handle = None  # Ownership transferred to the CRT descriptor.
        stream = os.fdopen(descriptor, 'rb', buffering=0)
        descriptor = None
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('file reader requires a regular disk file')
        if parent._stage is not None:
            parent._stage._children += 1
            counted = True
        yield stream, before
        after = os.fstat(stream.fileno())
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_nlink')
        if any(getattr(before, field) != getattr(after, field) for field in fields):
            raise ValueError('held file changed while reading')
        require_directory_pin(parent)
    finally:
        try:
            if stream is not None:
                stream.close()
            elif descriptor is not None:
                os.close(descriptor)
            elif handle is not None:
                _close(handle)
        finally:
            if counted:
                parent._stage._children -= 1


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

    def publish_file(self, name, destination, *, sha256, bytes, max_bytes):
        """Hash and exclusively install the exact held regular FILE handle."""
        require_directory_pin(self)
        if (not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256)
                or type(bytes) is not int or bytes < 0 or type(max_bytes) is not int
                or max_bytes < 0 or bytes > max_bytes):
            raise ValueError('file publication requires bounded exact bytes and SHA-256')
        destination = _path(destination)
        _component(destination.name)
        with _file_stream(self, name, delete=True) as (stream, before):
            import msvcrt
            information = _information(msvcrt.get_osfhandle(stream.fileno()))
            if before.st_size != bytes or information.links != 1:
                raise ValueError('file publication size or link ownership differs')
            digest = hashlib.sha256()
            remaining = bytes
            while remaining:
                block = stream.read(min(128 * 1024, remaining))
                if not block:
                    raise ValueError('file publication ended before expected bytes')
                remaining -= len(block)
                digest.update(block)
            if stream.read(1) or digest.hexdigest() != sha256:
                raise ValueError('file publication hash or byte count differs')
            with pinned_directory(destination.parent) as parent:
                import msvcrt
                _rename_handle(msvcrt.get_osfhandle(stream.fileno()), parent.path / destination.name)


@contextmanager
def writer_lock(root, *, name='.execution-retention.lock'):
    """Nonblocking cooperative storage lock; pin namespace and never replace it."""
    _api()
    _component(name)
    if len(name) > 128:
        raise ValueError('writer lock filename exceeds 128 characters')
    if isinstance(root, _DirectoryPin):
        manager = nullcontext(require_directory_pin(root))
    else:
        manager = pinned_directory(root)
    with manager as parent:
        handle = None; descriptor = None; stream = None; acquired = False
        counted = False
        try:
            with _private_attributes(inherit=False) as attributes:
                handle = _api().CreateFileW(str(parent.path / name),
                    0x80000000 | 0x40000000 | _READ_ATTRIBUTES | 0x20000,
                    3, ctypes.byref(attributes), 4, 0x00200000 | 0x02000000, None)
            if handle == _INVALID:
                handle = None
                raise _error(name)
            if _attributes(handle) & (_DIRECTORY | _REPARSE):
                raise ValueError('writer lock must be a physical regular file')
            _validate_private_wrapper(handle, inheritance_flags=0)
            if _information(handle).links != 1:
                raise ValueError('writer lock must have exactly one directory link')
            import msvcrt
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            handle = None
            stream = os.fdopen(descriptor, 'r+b', buffering=0)
            descriptor = None
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('writer lock must be a regular disk file')
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError(errno.EWOULDBLOCK, 'another storage writer holds this lock') from exc
            acquired = True
            existing = stream.read(2)
            if existing not in (b'', b'0'):
                raise ValueError('reserved writer lock has unrelated existing contents')
            if not existing:
                stream.write(b'0'); os.fsync(stream.fileno())
            if parent._stage is not None:
                parent._stage._children += 1
                counted = True
            require_directory_pin(parent)
            yield parent
            require_directory_pin(parent)
            if os.fstat(stream.fileno()).st_size != 1:
                raise ValueError('reserved writer lock changed during the critical section')
        finally:
            try:
                if stream is not None:
                    try:
                        if acquired:
                            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    finally:
                        stream.close()
                elif descriptor is not None:
                    os.close(descriptor)
                elif handle is not None:
                    _close(handle)
            finally:
                if counted:
                    parent._stage._children -= 1


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
        wrapper_handle = _open(wrapper, inspect_security=True)
        payload_handle = None
        stage = None
        body_error = None
        try:
            attributes = _attributes(wrapper_handle)
            if not attributes & _DIRECTORY or attributes & _REPARSE:
                raise ValueError('private wrapper changed before acquisition')
            _validate_private_wrapper(wrapper_handle)
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
