"""Versioned diagnostics and bounded I/O for the optional 3D subsystem."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Iterator

VERSION = '1.0.0'
MAX_JSON_BYTES = 32 * 1024 * 1024


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


def decode_json(payload: bytes | str) -> Any:
    require(len(payload) <= MAX_JSON_BYTES, 'RIG_LIMIT_JSON', 'JSON exceeds the 32 MiB bound.')
    if isinstance(payload, str):
        # A character count is only a lower bound for UTF-8. Count bounded
        # chunks so admission never materializes a second full encoded input.
        encoded_size = 0
        try:
            for offset in range(0, len(payload), 8192):
                encoded_size += len(payload[offset:offset+8192].encode('utf-8'))
                require(encoded_size <= MAX_JSON_BYTES, 'RIG_LIMIT_JSON', 'JSON exceeds the 32 MiB bound.')
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
    from decomposition_package import _relative
    try:
        return _relative(name)
    except ValueError as exc:
        raise RigError('RIG_PATH', str(exc), asset=str(name)) from exc


def safe_path(root: str | Path, name: str, *, exists: bool = False) -> Path:
    root = Path(root).resolve()
    target = root / portable_name(name)
    require(target.resolve().is_relative_to(root), 'RIG_PATH', 'Path escapes the asset directory.', asset=name)
    cursor = root
    for part in PurePosixPath(name).parts:
        cursor = cursor / part
        require(not cursor.is_symlink(), 'RIG_SYMLINK', 'Symbolic links are not portable rig assets.', asset=name)
    if exists:
        require(target.is_file(), 'RIG_MISSING_ASSET', 'Referenced regular file is missing.', asset=name)
    return target


def slug(text: str) -> str:
    value = re.sub('[^a-z0-9._-]+', '-', str(text).lower()).strip('-._')[:100]
    return value or 'character'


@contextmanager
def job_lock(root: str | Path) -> Iterator[None]:
    """OS locks release on process exit; the lock inode is never replaced."""
    root = Path(root).resolve()
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
