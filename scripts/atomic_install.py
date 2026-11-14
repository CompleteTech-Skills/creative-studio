"""Bounded byte-certified installation through canonical directory authorities.

No archive interpretation or semantic acceptance occurs here. Callers finish all
workers before entry and retain authorities until this synchronous call returns.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
import re
import stat

from atomic_publish import private_stage_fd, rename_no_replace_at
from portable_paths import existing_relative

_CHUNK = 128 * 1024


@dataclass(frozen=True)
class Limits:
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    max_depth: int

    def __post_init__(self):
        for field in ('max_files', 'max_file_bytes', 'max_total_bytes', 'max_depth'):
            value = getattr(self, field)
            if type(value) is not int or value < (1 if field in ('max_files', 'max_depth') else 0):
                raise ValueError('installation limits must be explicit nonnegative integers')
        if self.max_depth > 64:
            raise ValueError('installation depth limit cannot exceed 64')


def _limits(limits):
    if type(limits) is not Limits:
        raise ValueError('an explicit Limits value is required')
    # Validate even a value reconstructed without the dataclass constructor.
    limits.__post_init__()


def _name(name):
    if type(name) is not str:
        raise ValueError('filenames must be plain strings')
    existing_relative(name)
    if '/' in name:
        raise ValueError('publication requires one portable filename component')
    return name


def _entry(path, digest, size, limits):
    if type(path) is not str:
        raise ValueError('inventory paths must be plain strings')
    existing_relative(path)
    if len(path.split('/')) > limits.max_depth:
        raise ValueError('inventory path exceeds depth limit')
    if type(digest) is not str or re.fullmatch('[0-9a-f]{64}', digest) is None:
        raise ValueError('inventory requires lowercase SHA-256')
    if type(size) is not int or not 0 <= size <= limits.max_file_bytes:
        raise ValueError('inventory file size exceeds limit or is invalid')
    return path, digest, size


def _capture(inventory, directories, limits):
    _limits(limits)
    files = []
    total = 0
    paths = set()
    for item in inventory:
        if len(files) >= limits.max_files:
            raise ValueError('inventory exceeds file limit')
        if type(item) is not dict or set(item) != {'path', 'sha256', 'bytes'}:
            raise ValueError('inventory entries require exactly path, sha256 and bytes')
        entry = _entry(item['path'], item['sha256'], item['bytes'], limits)
        if entry[0].casefold() in paths:
            raise ValueError('duplicate or case-aliased inventory path')
        paths.add(entry[0].casefold())
        total += entry[2]
        if total > limits.max_total_bytes:
            raise ValueError('inventory exceeds total byte limit')
        files.append(entry)
    dirs = set()
    for index, name in enumerate(directories):
        if index >= limits.max_files:
            raise ValueError('explicit directories exceed entry limit')
        if type(name) is not str:
            raise ValueError('directories must be plain strings')
        existing_relative(name)
        if len(name.split('/')) > limits.max_depth:
            raise ValueError('directory exceeds depth limit')
        if name in dirs:
            raise ValueError('duplicate directory')
        dirs.add(name)
    for name in [entry[0] for entry in files] + list(dirs):
        parts = name.split('/')
        dirs.update('/'.join(parts[:index]) for index in range(1, len(parts)))
    folded = set(paths)
    for name in dirs:
        if name.casefold() in folded:
            raise ValueError('file/directory conflict or case alias')
        folded.add(name.casefold())
    return tuple(sorted(files)), tuple(sorted(dirs, key=lambda name: (name.count('/'), name)))


def _fingerprint(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            None if os.name == 'nt' else info.st_ctime_ns)


class _PosixDirectory:
    def __init__(self, fd):
        self.fd = fd

    @contextmanager
    def child(self, name, *, create=False):
        _name(name)
        if create:
            os.mkdir(name, 0o700, dir_fd=self.fd)
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            before = os.fstat(fd)
            yield _PosixDirectory(fd)
            current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if (not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)):
                raise ValueError('directory descendant changed during use')
        finally:
            os.close(fd)

    @contextmanager
    def reader(self, name):
        fd = os.open(_name(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('source entry is not a regular file')
            yield stream, before
            after = os.fstat(stream.fileno())
            current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if _fingerprint(before) != _fingerprint(after) or _fingerprint(before) != _fingerprint(current):
                raise ValueError('source file changed while reading')

    @contextmanager
    def writer(self, name):
        fd = os.open(_name(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        with os.fdopen(fd, 'wb') as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())

    @contextmanager
    def entries(self):
        # A fresh open has its own directory offset; dup() shares that offset.
        fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            with os.scandir(fd) as entries:
                def classified():
                    for entry in entries:
                        info = entry.stat(follow_symlinks=False)
                        kind = 'directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'unsafe'
                        yield entry.name, kind
                yield classified()
        finally:
            os.close(fd)

    def sync(self):
        os.fsync(self.fd)


class _WindowsDirectory:
    def __init__(self, pin):
        self.pin = pin

    @contextmanager
    def child(self, name, *, create=False):
        with self.pin.pin_child(_name(name), create=create) as pin:
            yield _WindowsDirectory(pin)

    def reader(self, name):
        return self.pin.pin_file(_name(name))

    @contextmanager
    def writer(self, name):
        with (self.pin.path / _name(name)).open('xb') as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())

    def entries(self):
        return self.pin.entries()

    def sync(self):
        # File contents are fsynced; Windows does not expose POSIX directory fsync.
        pass


@contextmanager
def _authority(value):
    if os.name == 'nt':
        import atomic_windows
        pin = atomic_windows.require_directory_pin(value)
        yield _WindowsDirectory(pin)
    else:
        if type(value) is not int or value < 0:
            raise ValueError('POSIX installation requires an open directory descriptor')
        before = os.fstat(value)
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError('installation authority is not a directory')
        fd = os.dup(value)
        try:
            current = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
                raise ValueError('installation authority changed during acquisition')
            yield _PosixDirectory(fd)
        finally:
            os.close(fd)


@contextmanager
def _descendant(root, path, *, create=False):
    parts = path.split('/') if path else []
    if not parts:
        yield root
    else:
        with root.child(parts[0], create=create) as child:
            with _descendant(child, '/'.join(parts[1:]), create=create) as result:
                yield result


def _copy(source, source_name, output, output_name, digest, size):
    with source.reader(source_name) as (reader, info):
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError('source file type or size differs from inventory')
        hashed = hashlib.sha256()
        count = 0
        with output.writer(output_name) as writer:
            while True:
                data = reader.read(min(_CHUNK, size - count + 1))
                if not data:
                    break
                count += len(data)
                if count > size:
                    raise ValueError('source exceeds certified byte size')
                hashed.update(data)
                writer.write(data)
        if count != size or hashed.hexdigest() != digest:
            raise ValueError('source bytes differ from inventory')


def _scan(root, files, directories, limits):
    expected = {path: (digest, size) for path, digest, size in files}
    expected_dirs = set(directories)
    seen_files = set()
    seen_dirs = set()
    count = 0
    total = 0

    def visit(node, prefix='', depth=0):
        nonlocal count, total
        with node.entries() as entries:
            for name, kind in entries:
                count += 1
                # Bound scanning incrementally, including unexpected directories.
                if count > len(expected) + len(expected_dirs):
                    raise ValueError('filesystem inventory exceeds entry budget')
                _name(name)
                path = prefix + name
                if depth + 1 > limits.max_depth:
                    raise ValueError('filesystem exceeds depth budget')
                if kind == 'directory':
                    if path not in expected_dirs:
                        raise ValueError('unexpected source/payload directory: ' + path)
                    seen_dirs.add(path)
                    with node.child(name) as child:
                        visit(child, path + '/', depth + 1)
                elif kind == 'file':
                    if path not in expected:
                        raise ValueError('unexpected source/payload file: ' + path)
                    digest, size = expected[path]
                    with node.reader(name) as (stream, info):
                        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                            raise ValueError('filesystem file type/size differs: ' + path)
                        hashed = hashlib.sha256()
                        length = 0
                        while True:
                            data = stream.read(min(_CHUNK, size - length + 1))
                            if not data:
                                break
                            length += len(data)
                            total += len(data)
                            if length > size or total > limits.max_total_bytes:
                                raise ValueError('filesystem byte budget exceeded')
                            hashed.update(data)
                        if length != size or hashed.hexdigest() != digest:
                            raise ValueError('filesystem hash differs: ' + path)
                    seen_files.add(path)
                else:
                    raise ValueError('filesystem contains a link or nonregular entry: ' + path)
    visit(root)
    if seen_files != set(expected) or seen_dirs != expected_dirs:
        raise ValueError('filesystem inventory is incomplete')


def _reject_source_destination_overlap(source, destination):
    if os.name == 'nt':
        if destination.pin.path.is_relative_to(source.pin.path):
            raise ValueError('directory destination parent lies inside source inventory')
        return
    original = os.fstat(source.fd)
    identity = original.st_dev, original.st_ino
    fd = os.dup(destination.fd)
    try:
        # Bound even abnormal mount/namespace ancestry without resolving paths.
        for _ in range(1024):
            info = os.fstat(fd)
            current = info.st_dev, info.st_ino
            if current == identity:
                raise ValueError('directory destination parent lies inside source inventory')
            parent = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                upper = os.fstat(parent)
            except BaseException:
                os.close(parent)
                raise
            os.close(fd)
            fd = parent
            if (upper.st_dev, upper.st_ino) == current:
                return
        raise ValueError('destination ancestry exceeds acquisition budget')
    finally:
        os.close(fd)


@contextmanager
def _stage(parent):
    if os.name == 'nt':
        import atomic_windows
        with atomic_windows.private_stage(parent.pin.path) as stage:
            yield _WindowsDirectory(stage), stage
    else:
        with private_stage_fd(parent.fd, prefix='.install-') as fd:
            yield _PosixDirectory(fd), None


def install_directory(source_authority, destination_parent_authority, destination_name,
                      inventory, *, limits, directories=()):
    """Copy a closed regular-file inventory and exclusively commit a directory.

    Parent directories of listed files/directories are implicit. Other empty
    directories must be listed explicitly. Inventory is captured before I/O.
    """
    files, dirs = _capture(inventory, directories, limits)
    _name(destination_name)
    with _authority(source_authority) as source, _authority(destination_parent_authority) as parent:
        _reject_source_destination_overlap(source, parent)
        _scan(source, files, dirs, limits)
        with _stage(parent) as (outer, windows_stage):
            @contextmanager
            def payload_context():
                if windows_stage is not None:
                    yield outer
                else:
                    with outer.child('payload', create=True) as payload:
                        yield payload
            with payload_context() as payload:
                for path in dirs:
                    ancestor, _, name = path.rpartition('/')
                    with _descendant(payload, ancestor) as directory:
                        with directory.child(name, create=True):
                            pass
                        directory.sync()
                for path, digest, size in files:
                    ancestor, _, name = path.rpartition('/')
                    with _descendant(source, ancestor) as original, _descendant(payload, ancestor) as output:
                        _copy(original, name, output, name, digest, size)
                        output.sync()
                _scan(source, files, dirs, limits)
                _scan(payload, files, dirs, limits)
                payload.sync()
            if windows_stage is not None:
                windows_stage.publish_to(parent.pin.path / destination_name)
            else:
                rename_no_replace_at(outer.fd, 'payload', parent.fd, destination_name)


def install_file(source_authority, source_name, destination_parent_authority,
                 destination_name, *, sha256, bytes, limits):
    """Copy and exclusively commit one real regular file (including ZIP bytes)."""
    _limits(limits)
    source_name = _name(source_name)
    destination_name = _name(destination_name)
    _, digest, size = _entry(source_name, sha256, bytes, limits)
    if size > limits.max_total_bytes:
        raise ValueError('file exceeds total byte limit')
    with _authority(source_authority) as source, _authority(destination_parent_authority) as parent:
        with _stage(parent) as (stage, windows_stage):
            _copy(source, source_name, stage, 'payload-file', digest, size)
            _scan(stage, (('payload-file', digest, size),), (), limits)
            stage.sync()
            if windows_stage is not None:
                windows_stage.publish_file('payload-file', parent.pin.path / destination_name,
                                           sha256=digest, bytes=size, max_bytes=limits.max_file_bytes)
            else:
                rename_no_replace_at(stage.fd, 'payload-file', parent.fd, destination_name)
