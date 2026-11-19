"""Operation-scoped native journal directory identities, never JSON authority.

This module is shared when Hatch is imported and when its CLI runs as __main__.
Initial pins close before an operation runs; publication verifies the recorded
identity through its newly held pin before writing through that same pin.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import inspect
import os
from pathlib import Path
import threading


@dataclass(frozen=True)
class AdmittedRoot:
    path: str
    identity: tuple

    def __fspath__(self):
        return self.path


_ADMISSIONS = ContextVar('native_journal_admissions', default=())
_OWNERSHIPS = ContextVar('native_journal_ownerships', default=())


@dataclass
class _HeldOwnership:
    path: str
    process: int
    thread: int
    active: bool = True


@contextmanager
def held_ownership(root):
    """Record an already acquired Hatch kernel lock, never serialized authority."""
    witness = _HeldOwnership(str(Path(root)), os.getpid(), threading.get_ident())
    token = _OWNERSHIPS.set((*_OWNERSHIPS.get(), witness))
    try:
        yield
    finally:
        witness.active = False
        _OWNERSHIPS.reset(token)


def owns(root):
    return any(item.active and item.path == str(Path(root)) and
               item.process == os.getpid() and item.thread == threading.get_ident()
               for item in _OWNERSHIPS.get())


@contextmanager
def pinned_root(path):
    """Yield the publication parent and its native identity under one pin."""
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('native journal root requires a canonical absolute path')
    if os.name == 'nt':
        from atomic_windows import pinned_directory
        with pinned_directory(path) as pin:
            yield pin.path, pin.identity()
    else:
        from atomic_publish import pinned_directory_fd
        with pinned_directory_fd(path) as parent:
            info = os.fstat(parent)
            identity = (info.st_dev, info.st_ino)
            yield parent, identity
            with pinned_directory_fd(path) as current:
                info = os.fstat(current)
                if identity != (info.st_dev, info.st_ino):
                    raise ValueError('native journal directory changed during publication')


def _known(path):
    return next((item for item in reversed(_ADMISSIONS.get())
                 if Path(item.path) == path), None)


def _absolute_spelling(root):
    path = Path(root)
    return path if path.is_absolute() else Path.cwd() / path


def admit(root):
    """Record a fresh root, or verify and inherit an enclosing admission."""
    if isinstance(root, AdmittedRoot):
        witness = root
        path = Path(root.path)
    else:
        # Preserve symlink/../ semantics until the filesystem resolves a fresh
        # caller path. Already admitted canonical names must not resolve again.
        path = _absolute_spelling(root)
        witness = _known(path)
        if witness is None:
            path = path.resolve()
            witness = _known(path)
    with pinned_root(path) as (_, identity):
        if witness is not None and witness.identity != identity:
            raise ValueError('native journal directory differs from its original admission')
        return witness or AdmittedRoot(str(path), identity)


def admitted(root):
    """Retrieve prior admission; never admit a late lifecycle destination."""
    if isinstance(root, AdmittedRoot):
        return root
    witness = _known(_absolute_spelling(root))
    if witness is None:
        raise ValueError('native journal publication requires prior directory admission')
    return witness


@contextmanager
def pinned_admission(root):
    """Retain original directory authority through a bounded native action."""
    witness = admitted(root)
    with pinned_root(Path(witness.path)) as (parent, identity):
        if witness.identity != identity:
            raise ValueError('native journal directory differs from its original admission')
        yield parent


@contextmanager
def admission(root):
    witness = admit(root)
    token = _ADMISSIONS.set((*_ADMISSIONS.get(), witness))
    try:
        yield Path(witness.path)
    finally:
        _ADMISSIONS.reset(token)


def operation(function):
    """Keep nested calls, including contextmanager bodies, in one admission."""
    if inspect.isgeneratorfunction(function):
        @wraps(function)
        def generator(root, *args, **kwargs):
            with admission(root) as canonical:
                yield from function(canonical, *args, **kwargs)
        return generator

    @wraps(function)
    def invoke(root, *args, **kwargs):
        with admission(root) as canonical:
            return function(canonical, *args, **kwargs)
    return invoke
