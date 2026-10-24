"""Read local capabilities without importing optional dependencies or calling a model."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def capabilities():
    from rig_doctor import rig_capability_description
    return {'interface_version': 1,
        'rig_3d': rig_capability_description(),
        'model3d_contracts': {'schema_version': 1, 'command': 'python scripts/model3d.py',
            'operations': ['capabilities', 'validate'],
            'payload_validation': 'Hash-verified snapshot and existing GLB/glTF/OBJ inspection.',
            'generation': False, 'provider_execution': False, 'review_approval': False},
        'sources': {'imagegen': ['image', 'strip'], 'import': ['image', 'strip', 'frames']},
        'extraction': ['conservative-slots', 'source-bound-rectangles'],
        'decomposition': {'schema_version': 1, 'sources': ['png', 'jpeg', 'webp'],
            'semantic_input': ['source-bound-masks', 'polygons', 'color-seeds', 'offline-review'],
            'semantic_model': 'Optional external image-understanding capability; none called by this controller.',
            'modes': ['faithful', 'explicit-assisted-hidden-reconstruction'],
            'reuse': ['portable-directory', 'portable-zip', 'forward-generation-import'],
            'automatic_rigging': False},
        'resampling': ['nearest', 'bilinear', 'bicubic', 'lanczos'],
        'styles': {'schema_version': 1, 'catalog': 'styles/catalog.json',
                   'discovery': ['styles', 'style', 'style-modifiers'],
                   'immutable_run_specification': True, 'thematic_modifiers': True},
        'exporters': ['frames', 'atlas', 'gif', 'webp', 'browser', 'cards', 'flipbook', 'board', 'stimuli', 'cursor', 'craft'],
        'consumers': ['browser-state-machine-v2', 'communication-board-v1', 'Xcursor-v1', 'local-cursor-v1', 'print-layout-v1', 'stimuli-csv-v1', 'craft-grid-v1'],
        'state_library': {'interface_version': 1, 'command': 'python scripts/hatch.py states',
            'discovery': ['list', 'search', 'show', 'packs', 'archetypes', 'recipes', 'preview'],
            'compilation': 'Selected definitions compile to an ordinary validated custom profile and a separate provenance manifest.',
            'generation': 'Selective installed imagegen handoffs or explicit imports; compilation performs no model calls.',
            'codex_extension': False},
        'unsupported': 'No dynamic plugins, arbitrary code hooks, remote generation clients or automatic external installation.'}


def doctor(destination=None, profile=None):
    result = {'python': sys.version.split()[0], 'python_supported': sys.version_info >= (3, 10),
              'capabilities': capabilities(), 'codecs': {}, 'errors': [], 'warnings': []}
    try:
        from PIL import features, __version__
        result['pillow'] = __version__
        result['codecs'] = {name: features.check(name) for name in ('webp', 'jpg', 'zlib', 'freetype2')}
        if tuple(map(int, __version__.split('.')[:2])) < (10, 1):
            result['errors'].append('Pillow >=10.1 is required; install Pillow in this interpreter.')
        if not result['codecs']['webp']:
            result['errors'].append('Pillow WebP codec is required for Codex packaging.')
        if not result['codecs']['zlib']:
            result['errors'].append('Pillow zlib support is required for PNG files.')
        if not result['codecs']['freetype2']:
            result['warnings'].append('Print exporters require FreeType support; non-print workflows remain available.')
        if profile:
            from profile_spec import load_profile
            from asset_contracts import check_resources
            selected = load_profile(profile)
            result['resources'] = check_resources(selected)
            if set(selected['export']['formats']) & {'cards', 'flipbook', 'craft'} and not result['codecs']['freetype2']:
                result['errors'].append('Selected profile needs FreeType for readable print labels.')
    except ImportError:
        result['errors'].append('Pillow is missing: python -m pip install Pillow')
    result['optional'] = {'playwright_python': importlib.util.find_spec('playwright') is not None,
                          'imagegen': 'External installed skill/tool; availability cannot be probed by Python.'}
    try:
        from state_catalog import load_library
        result['state_library'] = load_library().summary()
    except (ImportError, ValueError, OSError, KeyError, TypeError) as exc:
        result['errors'].append('Character state library is incomplete or invalid: ' + str(exc))
    try:
        from style_registry import load_catalog, digest
        catalog = load_catalog()
        result['style_catalog'] = {'version': catalog['catalog_version'], 'sha256': digest(catalog),
                                   'styles': len(catalog['styles']),
                                   'techniques': sum(s['kind'] == 'technique' for s in catalog['styles']),
                                   'categories': len(catalog['categories']), 'modifiers': len(catalog['modifiers'])}
    except (ImportError, ValueError, OSError, KeyError, TypeError) as exc:
        result['errors'].append('Style catalog is invalid: ' + str(exc))
    parent = Path(destination or Path.cwd()).resolve()
    while not parent.exists():
        parent = parent.parent
    result['filesystem'] = {'existing_parent': str(parent), 'free_bytes': shutil.disk_usage(parent).free}
    try:
        with tempfile.TemporaryDirectory(prefix='.hatch-doctor-', dir=parent) as td:
            a, b = Path(td) / 'first', Path(td) / 'second'
            a.write_bytes(b'atomic rename probe')
            os.replace(a, b)
            result['filesystem']['atomic_rename_probe'] = b.read_bytes() == b'atomic rename probe'
    except OSError as exc:
        result['errors'].append('Destination must support writable files and same-volume atomic rename: ' + str(exc))
    if not result['python_supported']:
        result['errors'].append('Python >=3.10 required.')
    result['ok'] = not result['errors']
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--destination'); p.add_argument('--profile')
    args = p.parse_args()
    value = doctor(args.destination, args.profile)
    print(json.dumps(value, indent=2))
    raise SystemExit(0 if value['ok'] else 1)
