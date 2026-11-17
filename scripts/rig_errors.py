"""Versioned diagnostics and bounded I/O for the optional 3D subsystem."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Any, Iterator

VERSION = '1.0.0'
MAX_JSON_BYTES = 32 * 1024 * 1024
_DIRECTORY_CAPABILITIES = ContextVar('rig_internal_directory_capabilities', default=())


class RigError(ValueError):
    """An actionable, machine-readable failure; never implies output acceptance."""

    def __init__(self, code: str, message: str, *, stage: str = 'rig',
                 asset: str | None = None, expected: Any = None, observed: Any = None,
                 retryable: bool = False, correction: str = 'Correct the indicated input and retry.',
                 preserved: str | None = None):
        super().__init__(message)
        self.report = dict(code=code, stage=stage, message=message, asset=asset,
                           expected=expected, observed=observed, retryable=retryable,
                           correction=correction, preserved=preserved)


def require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise RigError(code, message, **details)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def sha256(path: str | Path) -> str:
    with Path(path).open('rb') as stream:
        digest = hashlib.sha256()
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(data)
        return digest.hexdigest()


def object_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


def finite_json(value: Any, depth: int = 0) -> None:
    require(depth <= 64, 'RIG_JSON_DEPTH', 'JSON nesting exceeds 64 levels.')
    if isinstance(value, float):
        require(math.isfinite(value), 'RIG_NONFINITE', 'Numbers must be finite.')
    elif isinstance(value, dict):
        for key, child in value.items():
            require(isinstance(key, str), 'RIG_SCHEMA', 'Object keys must be strings.')
            finite_json(child, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            finite_json(child, depth + 1)


def decode_json(payload: bytes | str, *, max_bytes: int | None = None) -> Any:
    if max_bytes is None:
        max_bytes = MAX_JSON_BYTES
    require(len(payload) <= max_bytes, 'RIG_LIMIT_JSON', f'JSON exceeds the {max_bytes}-byte bound.')
    if isinstance(payload, str):
        # A character count is only a lower bound for UTF-8. Count bounded
        # chunks so admission never materializes a second full encoded input.
        encoded_size = 0
        try:
            for offset in range(0, len(payload), 8192):
                encoded_size += len(payload[offset:offset+8192].encode('utf-8'))
                require(encoded_size <= max_bytes, 'RIG_LIMIT_JSON', f'JSON exceeds the {max_bytes}-byte bound.')
        except UnicodeError as exc:
            raise RigError('RIG_JSON', 'Invalid JSON: ' + str(exc)) from exc

    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, 'RIG_JSON_DUPLICATE', 'Duplicate JSON key: ' + key)
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(
                               RigError('RIG_NONFINITE', 'JSON numbers must be finite.')))
    except RigError:
        # Duplicate-key/nonfinite hooks already carry their specific diagnosis.
        raise
    except (ValueError, RecursionError) as exc:
        # JSONDecodeError, UnicodeError and integer-conversion limits all reach
        # this decoder boundary as ValueError; preserve the shared report shape.
        raise RigError('RIG_JSON', 'Invalid JSON: ' + str(exc)) from exc
    finite_json(value)
    return value


def read_json(path: str | Path) -> Any:
    path = Path(path)
    require(path.stat().st_size <= MAX_JSON_BYTES, 'RIG_LIMIT_JSON', 'JSON exceeds 32 MiB.', asset=path.name)
    return decode_json(path.read_bytes())


def atomic_bytes(path: str | Path, payload: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_json(path: str | Path, value: Any) -> None:
    finite_json(value)
    atomic_bytes(path, (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8'))


def portable_name(name: str) -> str:
    # Same portable spelling policy as the existing decomposition transport.
    from portable_paths import existing_relative
    try:
        return existing_relative(name)
    except ValueError as exc:
        raise RigError('RIG_PATH', str(exc), asset=str(name)) from exc


class _DirectoryCapability:
    """Internal context-owned descriptor; construction never comes from metadata."""
    def __init__(self, owned_fd, borrowed_fd, identity):
        self._fd = owned_fd
        self._borrowed_fd = borrowed_fd
        self.identity = identity
        self._closed = False

    def _check(self, alias_fd=None):
        require(not self._closed, 'RIG_DIRECTORY_CLOSED', 'Directory capability has expired.')
        try:
            info = os.fstat(self._fd)
            require(stat.S_ISDIR(info.st_mode) and (info.st_dev,info.st_ino) == self.identity,
                    'RIG_DIRECTORY_CHANGED', 'Owned directory descriptor changed.')
            if alias_fd is not None and alias_fd != self._fd:
                alias = os.fstat(alias_fd)
                require(stat.S_ISDIR(alias.st_mode) and (alias.st_dev,alias.st_ino) == self.identity,
                        'RIG_DIRECTORY_CHANGED', 'Borrowed directory descriptor was reused.')
        except OSError as exc:
            raise RigError('RIG_DIRECTORY_CLOSED', 'Directory descriptor is no longer open.') from exc

    @property
    def fd(self):
        self._check()
        return self._fd

    @property
    def path(self):
        self._check()
        return Path('/proc/self/fd')/str(self._fd)


@contextmanager
def directory_capability(descriptor: int):
    """Explicitly activate an internal open-directory authority for this context.

    The caller retains ownership of descriptor. This scope owns one duplicate,
    keeps nested outer capabilities active, and never admits unrelated procfd
    strings. Workers must explicitly inherit a descriptor and enter a new scope.
    This additive internal facility is Linux/procfd-only; ordinary paths retain
    their previous behavior on every platform.
    """
    require(type(descriptor) is int and descriptor >= 0, 'RIG_DIRECTORY_FD', 'Expected an open directory descriptor.')
    require(os.name == 'posix' and os.open in os.supports_dir_fd
            and hasattr(os,'O_NOFOLLOW') and hasattr(os,'O_DIRECTORY')
            and Path('/proc/self/fd').is_dir(),
            'RIG_DIRECTORY_UNAVAILABLE', 'Descriptor capabilities require Linux directory-relative I/O and procfd.')
    owned = None; cap = None; token = None
    try:
        try:
            original = os.fstat(descriptor)
            require(stat.S_ISDIR(original.st_mode), 'RIG_DIRECTORY_FD', 'Descriptor is not a directory.')
            owned = os.dup(descriptor)
        except OSError as exc:
            raise RigError('RIG_DIRECTORY_FD', 'Expected a live directory descriptor.') from exc
        cap = _DirectoryCapability(owned,descriptor,(original.st_dev,original.st_ino))
        cap._check(descriptor)
        token = _DIRECTORY_CAPABILITIES.set((*_DIRECTORY_CAPABILITIES.get(),cap))
        yield cap
    finally:
        if token is not None:_DIRECTORY_CAPABILITIES.reset(token)
        if cap is not None:cap._closed = True
        if owned is not None:
            try:
                current = os.fstat(owned)
                # An internal caller must not close cap.fd. If it did and reused
                # that number for another object, never close the foreign object.
                # Before registration/yield, this duplicate has never been
                # exposed to callers: even a rejected borrowed-FD race leaves
                # it exclusively ours and it must be closed.
                if token is None or cap is None or (current.st_dev,current.st_ino) == cap.identity:os.close(owned)
            except OSError:
                pass


def _directory_anchor(root):
    """Match only explicitly active capabilities, with no ambient proc whitelist."""
    root = Path(root).absolute()
    parts = root.parts
    for cap in reversed(_DIRECTORY_CAPABILITIES.get()):
        for alias in (cap._fd,cap._borrowed_fd):
            prefix = (Path('/proc/self/fd')/str(alias)).parts
            if parts[:len(prefix)] != prefix:continue
            cap._check(alias)
            suffix = parts[len(prefix):]
            require(all(part not in ('.','..') and '\x00' not in part for part in suffix),
                    'RIG_PATH', 'Directory-capability descendants cannot traverse parents.')
            return cap, cap.path.joinpath(*suffix), suffix
    return None, root, ()



@contextmanager
def scoped_directory_descriptor(root: str | Path):
    """Borrow an owned directory FD only from an explicitly active capability.

    Yield None for ordinary or unregistered roots, so callers retain their
    existing strict path-opening policy. Every descendant is opened nofollow;
    this context owns and closes all duplicates, not the original capability.
    """
    cap,_,suffix = _directory_anchor(root)
    if cap is None:
        yield None
        return
    opened = []
    try:
        try:
            opened.append(os.dup(cap.fd))
            for part in suffix:
                opened.append(os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=opened[-1]))
        except OSError as exc:
            raise RigError('RIG_DIRECTORY_PATH','Capability directory descendant is missing or unsafe.') from exc
        yield opened[-1]
    finally:
        for fd in reversed(opened):os.close(fd)


def normalize_root(root: str | Path) -> Path:
    """Retain explicit scoped directory authority; otherwise use ordinary resolve."""
    cap,path,suffix = _directory_anchor(root)
    if cap is None:return path.resolve()
    opened = []
    try:
        opened.append(os.dup(cap.fd))
        for part in suffix:
            try:
                opened.append(os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=opened[-1]))
            except FileNotFoundError:
                # Like ordinary Path.resolve(), permit not-yet-created suffixes.
                # Secure creation/publication remains the caller's FD authority.
                break
    except OSError as exc:
        raise RigError('RIG_DIRECTORY_PATH','Capability root descendant is unsafe.') from exc
    finally:
        for fd in reversed(opened):os.close(fd)
    return path


def safe_path(root: str | Path, name: str, *, exists: bool = False) -> Path:
    cap,anchored,suffix = _directory_anchor(root)
    root = anchored if cap is not None else Path(root).resolve()
    target = root / portable_name(name)
    if cap is None:
        require(target.resolve().is_relative_to(root), 'RIG_PATH', 'Path escapes the asset directory.', asset=name)
    else:
        # Root descendants must also be ordinary directories, not a symlink
        # smuggled in before the separately validated payload-relative name.
        opened = []
        try:
            opened.append(os.dup(cap.fd))
            for part in suffix:
                opened.append(os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=opened[-1]))
        except OSError as exc:
            raise RigError('RIG_DIRECTORY_PATH','Capability root descendant is missing or unsafe.',asset=name) from exc
        finally:
            for fd in reversed(opened):os.close(fd)
    cursor = root
    for part in PurePosixPath(name).parts:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            continue
        require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                'RIG_SYMLINK', 'Links and reparse points are not portable rig assets.', asset=name)
    if exists:
        require(target.is_file(), 'RIG_MISSING_ASSET', 'Referenced regular file is missing.', asset=name)
    return target



@contextmanager
def open_payload(root: str | Path, name: str, *, root_identity=None):
    """Open an admitted regular payload without following replaceable parents.

    Reads remain the caller's responsibility and must be bounded. Descriptor and
    path identity are rechecked after the caller finishes. POSIX retains each
    directory descriptor; Windows checks the opened handle's canonical path.
    """
    capability,anchored,root_parts = _directory_anchor(root)
    root = anchored if capability is not None else Path(root).absolute()
    safe_path(root, name, exists=True)
    # Keep the caller's spelling for the Windows handle comparison. Resolving
    # it here would silently admit an untrusted root alias.
    target = root / portable_name(name)
    def check_root_path():
        if capability is None:
            for component in (root, *root.parents):
                info = component.lstat()
                require(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                        'RIG_SYMLINK', 'Source directory paths cannot contain links or reparse points.', asset=name)
    def check_root(info):
        require(root_identity is None or (info.st_dev,info.st_ino) == root_identity,
                'RIG_PAYLOAD_CHANGED','Selected source directory changed.',asset=name)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptors = []
    try:
        check_root(root.stat())
        check_root_path()
        if os.open in os.supports_dir_fd and hasattr(os, 'O_NOFOLLOW'):
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            if capability is not None:
                descriptors.append(os.dup(capability.fd))
                directory_parts = root_parts
            else:
                # Ordinary untrusted path behavior is deliberately unchanged.
                descriptors.append(os.open(root.anchor, directory_flags))
                directory_parts = root.parts[1:]
            for part in directory_parts:
                descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
            check_root(os.fstat(descriptors[-1]))
            parts = PurePosixPath(name).parts
            for part in parts[:-1]:
                descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
            descriptor = os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=descriptors[-1])
        else:
            descriptor = os.open(target, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode), 'RIG_PAYLOAD_TYPE',
                    'Payload must be a regular file.', asset=name)
            if os.name == 'nt':
                import ctypes
                from ctypes import wintypes
                import msvcrt
                operation = ctypes.WinDLL('kernel32', use_last_error=True).GetFinalPathNameByHandleW
                operation.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
                operation.restype = wintypes.DWORD
                buffer = ctypes.create_unicode_buffer(32768)
                length = operation(wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno())), buffer, len(buffer), 0)
                require(0 < length < len(buffer), 'RIG_PAYLOAD_PATH',
                        'Cannot verify the opened source location.', asset=name)
                actual = buffer.value
                if actual.startswith('\\\\?\\UNC\\'):
                    actual = '\\\\' + actual[8:]
                elif actual.startswith('\\\\?\\'):
                    actual = actual[4:]
                require(os.path.normcase(actual) == os.path.normcase(os.path.abspath(target)),
                        'RIG_PAYLOAD_PATH', 'Opened source was redirected.', asset=name)
            current = safe_path(root, name, exists=True).stat()
            check_root_path()
            check_root(root.stat())
            require((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino),
                    'RIG_PAYLOAD_CHANGED', 'Payload changed while opening.', asset=name)
            yield stream, before
            after = os.fstat(stream.fileno())
            check_root_path()
            check_root(root.stat())
            current = safe_path(root, name, exists=True).stat()
            def fingerprint(info):
                fields = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                return fields if os.name == 'nt' else fields + (info.st_ctime_ns,)
            require(fingerprint(before) == fingerprint(after) == fingerprint(current),
                    'RIG_PAYLOAD_CHANGED', 'Payload changed during reading.', asset=name)
    except OSError as exc:
        raise RigError('RIG_PAYLOAD_IO', 'Cannot safely read the payload: ' + str(exc), asset=name) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)

def slug(text: str) -> str:
    value = re.sub('[^a-z0-9._-]+', '-', str(text).lower()).strip('-._')[:100]
    return value or 'character'


@contextmanager
def job_lock(root: str | Path) -> Iterator[None]:
    """OS locks release on process exit; the lock inode is never replaced."""
    capability,_,suffix = _directory_anchor(root)
    require(capability is None or bool(suffix), 'RIG_DIRECTORY_LOCK',
            'A writer needs a descendant root so its sibling lock stays inside directory authority. '
            'Activate the parent directory capability when writing an existing rig root.')
    root = normalize_root(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    lock = root.parent / ('.' + root.name + '.rig.lock')
    require(not lock.is_symlink(), 'RIG_SYMLINK', 'Lock path cannot be a symbolic link.')
    with lock.open('a+b') as stream:
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            if lock.stat().st_size == 0:
                stream.write(b'0'); stream.flush(); stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RigError('RIG_BUSY', 'Another writer owns this rig.', retryable=True) from exc
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RigError('RIG_BUSY', 'Another writer owns this rig.', retryable=True) from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
