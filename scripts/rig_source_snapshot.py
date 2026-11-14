"""Exact-byte source capture around the native GLB/glTF/OBJ importers.

Only the existing native parsers discover dependencies. Every parser read is
served from one private no-follow snapshot, never from mutable original bytes.
The returned context owns its temporary closure until closed.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import math
import tempfile
import unicodedata
from typing import Any

from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError, require


def import_limits(values):
    result = dict(DEFAULT_LIMITS)
    require(values is None or isinstance(values, dict), 'RIG_LIMITS', 'Limits must be an object.')
    for key, value in (values or {}).items():
        require(key in result and type(value) in (int, float) and 0 < value <= 2**63-1
                and (type(value) is int or math.isfinite(value) and value.is_integer()),
                'RIG_LIMITS', 'Limits must be bounded positive whole numbers with known names.', asset=str(key))
        result[key] = int(value)
    return result


@dataclass
class SourceSnapshot:
    model: Any
    source_records: list[dict]
    provenance: dict
    root: Path
    _temporary: Any = field(repr=False)

    def close(self):
        self._temporary.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def import_snapshot(path, *, scale=1., limits=None, blender=None):
    """Import one frozen local closure, preserving source bytes and identities.

    Explicit scale is meters per source unit. Rig/skin/morph/clip conversion
    remains entirely within the existing native importer. This API never starts
    Blender unless an explicitly selected .blend input requires the existing
    packed-project adapter.
    """
    from rig_import import load_character_model
    from rig_errors import open_payload
    limits = import_limits(limits)
    require(type(scale) in (int, float) and 0 < scale <= 10000
            and (type(scale) is int or math.isfinite(scale)),
            'RIG_SCALE', 'Import scale must be finite and positive, at most 10000.')
    entry = Path(path).absolute()
    require(entry.suffix.lower() in ('.glb', '.gltf', '.obj', '.blend'),
            'RIG_FORMAT', 'Source snapshots support GLB, glTF, OBJ and packed Blender files.')
    source_root = entry.parent.resolve()
    entry = source_root / entry.name
    source_info = source_root.stat()
    root_identity = (source_info.st_dev, source_info.st_ino)
    temporary = tempfile.TemporaryDirectory(prefix='rig-source-')
    root = Path(temporary.name).resolve()
    records, aliases, directories = {}, {}, {}
    total = 0
    read_bytes = 0

    def capture(source, maximum):
        nonlocal total, read_bytes
        source = Path(source).absolute()
        try:
            name = source.relative_to(source_root).as_posix()
        except ValueError as exc:
            raise RigError('RIG_PATH', 'Source dependency escapes the entry directory.') from exc
        alias = unicodedata.normalize('NFC', name).casefold()
        require(alias not in aliases or aliases[alias] == name,
                'RIG_PATH', 'Source closure contains colliding portable paths.', asset=name)
        require(alias not in directories, 'RIG_PATH', 'Source file aliases a resource directory.', asset=name)
        parts = name.split('/')
        prefixes = ['/'.join(parts[:depth]) for depth in range(1, len(parts))]
        for directory in prefixes:
            folded = unicodedata.normalize('NFC', directory).casefold()
            require(folded not in aliases and (folded not in directories or directories[folded] == directory),
                    'RIG_PATH', 'Source directory aliases another portable path.', asset=name)
        target = root/name
        if name not in records:
            require(len(records) < limits['archive_files'], 'RIG_LIMIT_FILES', 'Source closure exceeds its file budget.')
            with open_payload(source_root, name, root_identity=root_identity) as (stream, info):
                ceiling = min(maximum, limits['source_bytes'], limits['dependency_bytes']-total,
                              limits['memory_bytes']//3)
                require(0 < info.st_size <= ceiling, 'RIG_LIMIT_SOURCE', 'Source exceeds its encoded-byte budget.', asset=name)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(); count = 0
                with target.open('xb') as output:
                    while chunk := stream.read(min(1024**2, info.st_size-count+1)):
                        count += len(chunk)
                        require(count <= info.st_size, 'RIG_SOURCE_CHANGED', 'Source grew during capture.', asset=name)
                        digest.update(chunk);output.write(chunk)
                require(count == info.st_size, 'RIG_SOURCE_CHANGED', 'Source size changed during capture.', asset=name)
            total += count
            aliases[alias] = name
            for directory in prefixes:
                directories[unicodedata.normalize('NFC', directory).casefold()] = directory
            records[name] = dict(path=name, bytes=count, sha256=digest.hexdigest())
        require(records[name]['bytes'] <= maximum, 'RIG_LIMIT_SOURCE', 'Repeated source exceeds this parser read budget.', asset=name)
        read_bytes += records[name]['bytes']
        require(read_bytes * 3 <= limits['memory_bytes'], 'RIG_LIMIT_MEMORY', 'Aggregate parser source reads exceed their encoded memory budget.')
        return target.read_bytes()

    try:
        if entry.suffix.lower() == '.blend':
            # The existing Blender owner requires packed textures and no linked
            # libraries; only the immutable source copy reaches that worker.
            capture(entry, limits['source_bytes'])
        else:
            discovered = load_character_model(entry, scale=scale, limits=limits, source_reader=capture)
            names = {p.absolute().relative_to(source_root).as_posix() for p in discovered.source_files}
            require(names == set(records), 'RIG_SOURCE_INVENTORY', 'Decoded source closure and captured files disagree.')
            del discovered
        # Reopening the private closure proves it is independently loadable and
        # does not rely on live source handles or parser callbacks.
        model = load_character_model(root/entry.name, scale=scale, limits=limits, blender=blender)
        actual = {p.relative_to(root).as_posix() for p in model.source_files}
        require(actual == set(records), 'RIG_SOURCE_INVENTORY', 'Frozen closure dependencies changed during reopening.')
        return SourceSnapshot(model, list(records.values()),
                              dict(authority='rig_import.load_character_model', source_entry=entry.name,
                                   scale_conversion=scale, source_consistency='private exact-byte snapshot'),
                              root, temporary)
    except RigError:
        temporary.cleanup()
        raise
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        temporary.cleanup()
        raise RigError('RIG_SOURCE_SNAPSHOT', 'Native source capture or decoding failed.', asset=entry.name) from exc
    except BaseException:
        temporary.cleanup()
        raise
