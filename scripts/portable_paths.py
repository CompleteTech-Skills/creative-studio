"""Shared standard-library-only portable path spelling policy.

Transport and native workers use this single authority; importing it does not
load image, geometry or optional runtime dependencies.
"""
from pathlib import PurePosixPath

_RESERVED = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$',
             *(prefix + str(index) for prefix in ('com', 'lpt') for index in range(1, 10))}


def existing_relative(name):
    """Require one portable spelling; reject traversal, aliases and Windows ADS."""
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('unsafe decomposition package path: ' + str(name))
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or path.as_posix() != name or name == '.':
        raise ValueError('unsafe decomposition package path: ' + name)
    if any(part != part.rstrip(' .') or part.split('.', 1)[0].casefold() in _RESERVED or
           any(character in '<>"|?*' or ord(character) < 32 or ord(character) == 127 or 0xD800 <= ord(character) <= 0xDFFF for character in part)
           for part in path.parts):
        raise ValueError('unsafe or nonportable decomposition package path: ' + name)
    return name

