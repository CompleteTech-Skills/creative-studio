"""Shared standard-library-only portable path spelling policy.

Transport and native workers use this single authority; importing it does not
load image, geometry or optional runtime dependencies.
"""

from sys import _getframe as _loaded_module_frame

_EVALUATOR_MODULE_CODE = _loaded_module_frame().f_code
del _loaded_module_frame
from pathlib import PurePosixPath
import re
import unicodedata

_RESERVED = {'con', 'prn', 'aux', 'nul', 'conin$', 'conout$',
             *(prefix + str(index) for prefix in ('com', 'lpt') for index in range(1, 10))}


def _scene3d_relative(name):
    # Scene3D manifests already define this spelling and its diagnostics.
    if not isinstance(name, str) or not 0 < len(name) <= 1024:
        raise ValueError('Invalid path.')
    try:
        name.encode('utf-8')
    except UnicodeError as exc:
        raise ValueError('Scene3D strings must contain UTF-8 encodable Unicode scalar values.') from exc
    if unicodedata.normalize('NFC', name) != name:
        raise ValueError('Paths must use NFC Unicode.')
    if re.search(r'[\\:\x00-\x1f<>"|?*]', name):
        raise ValueError('Path contains a nonportable character.')
    for part in name.split('/'):
        if part in ('', '.', '..') or part.startswith(' ') or part.endswith((' ', '.')):
            raise ValueError('Noncanonical path component.')
        if re.match(r'^(CON|CONIN[$]|CONOUT[$]|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)', part, re.I):
            raise ValueError('Windows device path is unsupported.')
    return name


def existing_relative(name, *, profile='default'):
    """Require one portable spelling; reject traversal, aliases and Windows ADS."""
    if profile == 'scene3d':
        return _scene3d_relative(name)
    if profile != 'default':
        raise ValueError('Unknown portable path profile.')
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
