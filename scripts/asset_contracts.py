"""Small versioned contracts shared by the controller, importers and exporters."""
from __future__ import annotations

from sys import _getframe as _loaded_module_frame

_EVALUATOR_MODULE_CODE = _loaded_module_frame().f_code
del _loaded_module_frame

import hashlib
import json
import math
import platform
import re
import sys
from pathlib import Path

from PIL import Image, __version__ as PILLOW_VERSION

VERSION = '2.1.0'
RESAMPLING = {'nearest': Image.Resampling.NEAREST, 'bilinear': Image.Resampling.BILINEAR,
              'bicubic': Image.Resampling.BICUBIC, 'lanczos': Image.Resampling.LANCZOS}
MAX_SOURCE_PIXELS = 2_000_000
MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_FRAME_COUNT = 64
MAX_TOTAL_FRAMES = 1024
MAX_WORK_BYTES = 768 * 1024 * 1024
REVIEW_DIMENSIONS = ('identity', 'action', 'direction', 'loop', 'extraction', 'reduced_motion')
REFERENCE_ROLES = {'identity', 'front', 'side', 'back', 'detail', 'style', 'edit-target'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def hash_object(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def strict_keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f'{label}: expected object with supported keys {sorted(allowed)}')


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + ' must be nonempty text')
    return value


def text_list(value, label):
    if not isinstance(value, list):
        raise ValueError(label + ' must be a list')
    return [text(v, label) for v in value]


def validate_identity(value, profile):
    if value is None:
        value = {'schema_version': 1}
    strict_keys(value, {'schema_version', 'markings', 'proportions', 'palette', 'materials',
        'approved_props', 'asymmetries', 'directions', 'forbid_mirror', 'unresolved_conflicts'}, 'identity')
    result = json.loads(json.dumps(value))
    if type(result.get('schema_version')) is not int or result['schema_version'] != 1:
        raise ValueError('identity schema_version must be 1')
    for key in ('markings', 'materials', 'approved_props', 'asymmetries', 'unresolved_conflicts'):
        result[key] = text_list(result.get(key, []), 'identity.' + key)
    for key in ('proportions', 'palette', 'directions'):
        if not isinstance(result.setdefault(key, {}), dict):
            raise ValueError('identity.' + key + ' must be an object')
    for name, ratio in result['proportions'].items():
        text(name, 'proportion name')
        if type(ratio) not in (int, float) or not math.isfinite(ratio) or ratio <= 0:
            raise ValueError('identity proportions are positive finite ratios')
    for name, color in result['palette'].items():
        if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
            raise ValueError('identity palette requires #RRGGBB colors')
    specs = {s['id']: s for s in profile['states']}
    for state, direction in result['directions'].items():
        if state not in specs or direction not in ('left', 'right', 'front', 'back', 'any'):
            raise ValueError('identity directions must name a profile state and supported direction')
        if (state == 'running-right' and direction == 'left') or (state == 'running-left' and direction == 'right'):
            raise ValueError('identity direction contradicts the Codex state: ' + state)
    result.setdefault('forbid_mirror', False)
    if type(result['forbid_mirror']) is not bool:
        raise ValueError('identity.forbid_mirror must be boolean')
    if result['unresolved_conflicts']:
        raise ValueError('unresolved identity conflicts: ' + '; '.join(result['unresolved_conflicts']))
    return result


def reference_roles(value, count):
    if value is None:
        return [{'role': 'identity', 'note': 'Original required reference'} for _ in range(count)]
    if not isinstance(value, list) or len(value) != count:
        raise ValueError('reference roles must contain exactly one record per --reference, in order')
    for entry in value:
        strict_keys(entry, {'role', 'note'}, 'reference role')
        if entry.get('role') not in REFERENCE_ROLES:
            raise ValueError('unsupported reference role; choose ' + ', '.join(sorted(REFERENCE_ROLES)))
        text(entry.get('note'), 'reference role note')
    return value


def check_image(path):
    """Check header before decoding; animated imports need explicitly indexed frames."""
    if Path(path).suffix.lower() not in ('.png', '.webp', '.jpg', '.jpeg'):
        raise ValueError('unsupported source type; supported single-frame sources are PNG, WebP or JPEG')
    if Path(path).stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError('source exceeds 64 MiB encoded-file bound')
    with Image.open(path) as image:
        if image.format not in ('PNG', 'WEBP', 'JPEG') or getattr(image, 'n_frames', 1) != 1:
            raise ValueError('supported sources are single-frame PNG, WebP or JPEG; export animated sources to indexed PNG frames first')
        if image.width * image.height > MAX_SOURCE_PIXELS:
            raise ValueError(f'source exceeds {MAX_SOURCE_PIXELS} pixels; reduce resolution before decoding')
        size = image.size
        image.verify()
    return size


def check_resources(profile):
    layout = profile['layout']
    counts = [s['frames'] for s in profile['states']]
    if max(counts) > MAX_FRAME_COUNT or sum(counts) > MAX_TOTAL_FRAMES:
        raise ValueError(f'profile resource bound: at most {MAX_FRAME_COUNT} frames/state and {MAX_TOTAL_FRAMES} total')
    # Atlas normalization, two encodings, largest row, and Python source masking arrays.
    estimated = 256 * 1024 * 1024 + layout['width'] * layout['height'] * 16 + max(counts) * layout['cell_width'] * layout['cell_height'] * 16
    if estimated > MAX_WORK_BYTES:
        raise ValueError(f'profile estimated raster working set {estimated} exceeds {MAX_WORK_BYTES} bytes')
    return {'estimated_raster_work_bytes': estimated, 'source_pixel_limit': MAX_SOURCE_PIXELS,
            'source_byte_limit': MAX_SOURCE_BYTES, 'max_frames_per_state': MAX_FRAME_COUNT, 'total_frames': sum(counts)}


def generation_projection(profile, name):
    value = {'generation': profile['generation'],
             'cell': [profile['layout']['cell_width'], profile['layout']['cell_height']]}
    if name != 'base':
        state = next(s for s in profile['states'] if s['id'] == name)
        value['state'] = {k: state[k] for k in ('id', 'purpose', 'frames', 'motion', 'generation')}
    return value


def extraction_projection(profile, name):
    state = next(s for s in profile['states'] if s['id'] == name)
    return {'layout': {k: profile['layout'][k] for k in ('cell_width', 'cell_height')},
            'frames': state['frames'], 'alignment': state['alignment'], 'validation': profile['validation']}


def versions():
    scripts = Path(__file__).parent
    return {'hatch': VERSION, 'run_schema': 3, 'profile_schemas': [1, 2], 'source_schema': 1,
            'python': sys.version.split()[0], 'pillow': PILLOW_VERSION, 'platform': platform.platform(),
            'code_sha256': hash_object({p.name: sha(p) for p in sorted(scripts.glob('*.py'))}),
            'consumer_assets': {p.name: sha(p) for p in sorted((scripts.parent / 'assets').glob('*')) if p.suffix in ('.js', '.html')},
            'model_call': {'id': None, 'model': None, 'parameters': None,
                           'unavailable_reason': 'No model call is performed by the deterministic controller.'}}


def portable(root, path):
    return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()


def resolve_ref(root, value, former_root=None):
    """Resolve internal artifacts only. External origins remain opaque historical strings."""
    root = Path(root).resolve()
    value = str(value).replace('\\', '/')
    former_root = str(former_root).replace('\\', '/') if former_root else None
    if former_root and value.startswith(former_root.rstrip('/') + '/'):
        value = value[len(former_root.rstrip('/')) + 1:]
    path = Path(value)
    if path.is_absolute() and former_root:
        try:
            path = path.relative_to(Path(former_root))
        except ValueError:
            pass
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError('artifact reference escapes run: ' + str(value))
    return target
