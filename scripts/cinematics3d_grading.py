"""Non-destructive scene-linear grades; canonical color owns final transforms.

This module operates on admitted pixels. It does not authenticate rendered input,
create a timeline, decode media, or infer source color spaces.
"""
from copy import deepcopy
import math
from pathlib import Path
import tempfile

import cinematics3d_color as color
from rig_errors import object_hash

MAX_OPERATIONS = 32
KINDS = {'exposure', 'lift-gamma-gain', 'contrast', 'saturation', 'vibrance',
         'temperature-tint', 'hue', 'channel-matrix', 'tonal-balance', 'curves', 'lut'}


def _need(ok, message):
    if not ok:
        raise ValueError(message)


def _number(value, low, high):
    _need(type(value) in (int, float) and low <= value <= high and math.isfinite(value), 'invalid grading number')


def _vector(value, length, low, high):
    _need(type(value) is list and len(value) == length, 'invalid grading vector')
    for item in value:
        _number(item, low, high)


def import_cube(root, path, *, working_space, interpolation='linear'):
    """Admit exact CUBE bytes for an explicit same-space transform."""
    color._name(working_space)
    _need(interpolation == 'linear', 'only explicit linear LUT interpolation is supported')
    _need(Path(path).suffix.lower() == '.cube', 'only CUBE LUTs are supported')
    payload = color._bytes(root, path, color.MAX_FILE_BYTES)
    color._lut(payload, '.cube')
    headers = {}
    for line in payload.decode('utf-8').splitlines():
        tokens = line.split('#', 1)[0].strip().split()
        if tokens and tokens[0] in ('DOMAIN_MIN', 'DOMAIN_MAX', 'LUT_1D_SIZE', 'LUT_3D_SIZE'):
            headers[tokens[0]] = tokens[1:]
    dimensions = 1 if 'LUT_1D_SIZE' in headers else 3
    value = {'kind': 'cinematics-grade-lut', 'schema_version': 1, 'path': color._path(path),
             'bytes': len(payload), 'sha256': color._hash(payload), 'input_space': working_space,
             'output_space': working_space, 'dimensions': dimensions,
             'grid_size': int(headers['LUT_1D_SIZE' if dimensions == 1 else 'LUT_3D_SIZE'][0]),
             'domain_min': [float(x) for x in headers.get('DOMAIN_MIN', ['0']*3)],
             'domain_max': [float(x) for x in headers.get('DOMAIN_MAX', ['1']*3)],
             'interpolation': interpolation}
    value['lut_sha256'] = object_hash(value)
    return value


def _validate_operation(operation):
    color._exact(operation, ['id', 'enabled', 'kind', 'parameters'], 'grade operation')
    color._name(operation['id'])
    _need(type(operation['enabled']) is bool and operation['kind'] in KINDS, 'invalid grade kind/enabled')
    p = operation['parameters']; kind = operation['kind']
    if kind == 'exposure':
        color._exact(p, ['stops'], kind); _number(p['stops'], -24, 24)
    elif kind == 'lift-gamma-gain':
        color._exact(p, ['lift', 'gamma', 'gain'], kind)
        _vector(p['lift'], 3, -16, 16); _vector(p['gamma'], 3, .05, 20); _vector(p['gain'], 3, 0, 32)
    elif kind == 'contrast':
        color._exact(p, ['contrast', 'pivot'], kind); _number(p['contrast'], 0, 8); _number(p['pivot'], .0001, 16)
    elif kind in ('saturation', 'vibrance'):
        color._exact(p, ['amount'], kind); _number(p['amount'], 0, 4)
    elif kind == 'temperature-tint':
        color._exact(p, ['temperature', 'tint'], kind)
        _number(p['temperature'], -2, 2); _number(p['tint'], -2, 2)
    elif kind == 'hue':
        color._exact(p, ['degrees'], kind); _number(p['degrees'], -180, 180)
    elif kind == 'channel-matrix':
        color._exact(p, ['matrix', 'offset'], kind)
        _need(type(p['matrix']) is list and len(p['matrix']) == 3, 'invalid channel matrix')
        for row in p['matrix']: _vector(row, 3, -8, 8)
        _vector(p['offset'], 3, -16, 16)
    elif kind == 'tonal-balance':
        color._exact(p, ['shadows', 'midtones', 'highlights'], kind)
        for vector in p.values(): _vector(vector, 3, -4, 4)
    elif kind == 'curves':
        color._exact(p, ['rgb'], kind)
        _need(type(p['rgb']) is list and len(p['rgb']) == 3, 'invalid channel curves')
        for points in p['rgb']:
            _need(type(points) is list and 2 <= len(points) <= 64, 'curve point budget')
            for pair in points: _vector(pair, 2, -64, 64)
            _need(all(a[0] < b[0] for a, b in zip(points, points[1:])), 'curve inputs must increase')
    elif kind == 'lut':
        color._exact(p, ['inventory'], kind)
        value = p['inventory']
        color._exact(value, ['kind', 'schema_version', 'path', 'bytes', 'sha256', 'input_space', 'output_space',
                            'dimensions', 'grid_size', 'domain_min', 'domain_max', 'interpolation', 'lut_sha256'], 'LUT')
        _need(value['kind'] == 'cinematics-grade-lut' and type(value['schema_version']) is int and value['schema_version'] == 1,
              'invalid LUT version')
        color._path(value['path']); color._sha(value['sha256'])
        _need(type(value['bytes']) is int and 0 < value['bytes'] <= color.MAX_FILE_BYTES, 'invalid LUT byte budget')
        _need(type(value['dimensions']) is int and value['dimensions'] in (1, 3), 'invalid LUT dimensions')
        _need(type(value['grid_size']) is int and 2 <= value['grid_size'] <= (65536 if value['dimensions'] == 1 else 65), 'invalid LUT grid')
        _vector(value['domain_min'], 3, -1e12, 1e12); _vector(value['domain_max'], 3, -1e12, 1e12)
        _need(all(a < b for a, b in zip(value['domain_min'], value['domain_max'])), 'invalid LUT domain')
        color._name(value['input_space']); color._name(value['output_space'])
        _need(value['interpolation'] == 'linear', 'unsupported LUT interpolation')
        color._sha(value['lut_sha256'])
        _need(object_hash({k:v for k,v in value.items() if k != 'lut_sha256'}) == value['lut_sha256'], 'stale LUT identity')
    return operation


def make_grade(*, working_space, operations, scope='shot', source_id, intensity=1.):
    """Capture authored operations; timeline owners supply sampled intensity."""
    color._name(working_space); color._name(source_id)
    _need(scope in ('shot', 'scene'), 'grade scope must be shot or scene')
    _number(intensity, 0, 1)
    _need(type(operations) is list and len(operations) <= MAX_OPERATIONS, 'operation budget exceeded')
    ids = set()
    for operation in operations:
        _validate_operation(operation)
        _need(operation['id'] not in ids, 'duplicate grade operation id'); ids.add(operation['id'])
        if operation['kind'] == 'lut':
            lut = operation['parameters']['inventory']
            _need(lut['input_space'] == lut['output_space'] == working_space, 'incompatible LUT color spaces')
    value = {'kind': 'cinematics-grade', 'schema_version': 1, 'working_space': working_space,
             'scope': scope, 'source_id': source_id, 'intensity': intensity, 'operations': deepcopy(operations)}
    value['grade_sha256'] = object_hash(value)
    return value


def _lut_pixels(root, inventory, rgb):
    current = import_cube(root, inventory['path'], working_space=inventory['input_space'], interpolation=inventory['interpolation'])
    _need(current == inventory, 'LUT bytes or admission changed')
    payload = color._bytes(root, inventory['path'], color.MAX_FILE_BYTES)
    _need(color._hash(payload) == inventory['sha256'], 'LUT changed during snapshot')
    ocio = color._ocio()
    with tempfile.TemporaryDirectory(prefix='grade-lut-') as temporary:
        path = Path(temporary)/'grade.cube'; path.write_bytes(payload)
        transform = ocio.FileTransform(str(path), interpolation=ocio.INTERP_LINEAR)
        processor = ocio.Config.CreateRaw().getProcessor(transform).getDefaultCPUProcessor()
        processor.applyRGB(rgb)


def _operation(rgb, operation, root):
    import numpy as np
    p = operation['parameters']; kind = operation['kind']
    luma = rgb @ np.array([.2126, .7152, .0722])
    if kind == 'exposure': return rgb*2.**p['stops']
    if kind == 'lift-gamma-gain':
        lifted = rgb + np.asarray(p['lift'])*(1-rgb)
        return np.sign(lifted)*np.abs(lifted)**(1/np.asarray(p['gamma']))*np.asarray(p['gain'])
    if kind == 'contrast': return (rgb-p['pivot'])*p['contrast']+p['pivot']
    if kind == 'saturation': return luma[:, None]+(rgb-luma[:, None])*p['amount']
    if kind == 'vibrance':
        chroma = rgb.max(axis=1)-rgb.min(axis=1)
        saturation = chroma/np.maximum(np.abs(rgb).max(axis=1), 1e-8)
        amount = 1+(p['amount']-1)*(1-np.minimum(saturation, 1))
        return luma[:, None]+(rgb-luma[:, None])*amount[:, None]
    if kind == 'temperature-tint':
        # Artist-facing chromatic balance in stops, explicitly not a Kelvin CAT.
        return rgb*2.**np.array([p['temperature']-p['tint']/2, p['tint'], -p['temperature']-p['tint']/2])
    if kind == 'hue':
        # Rodrigues rotation about the neutral RGB axis preserves exact grays.
        angle = math.radians(p['degrees']); axis = np.ones(3)/math.sqrt(3)
        return rgb*math.cos(angle)+np.cross(axis, rgb)*math.sin(angle)+(rgb@axis)[:, None]*axis*(1-math.cos(angle))
    if kind == 'channel-matrix': return rgb@np.asarray(p['matrix']).T+np.asarray(p['offset'])
    if kind == 'tonal-balance':
        t = np.clip(luma, 0, 1); shadow = (1-t)**2; high = t*t; mid = 1-shadow-high
        return rgb*2.**(shadow[:, None]*p['shadows']+mid[:, None]*p['midtones']+high[:, None]*p['highlights'])
    if kind == 'curves':
        result = rgb.copy()
        for channel, points in enumerate(p['rgb']):
            x, y = np.asarray(points).T
            # Outside the control domain preserve HDR detail with unit slope.
            values = rgb[:, channel]
            result[:, channel] = np.interp(values, x, y)
            result[:, channel] = np.where(values < x[0], values+y[0]-x[0], result[:, channel])
            result[:, channel] = np.where(values > x[-1], values+y[-1]-x[-1], result[:, channel])
        return result
    if kind == 'lut':
        result = np.ascontiguousarray(rgb, dtype=np.float32); _lut_pixels(root, p['inventory'], result); return result
    raise ValueError('unsupported grading operation')


def diagnostics(before, after):
    """Measured range/neutral/histogram diagnostics, not artistic approval."""
    import numpy as np
    result = {}
    for name, pixels in (('before', before), ('after', after)):
        rgb = pixels.reshape(-1, 4)[:, :3]; luma = rgb@np.array([.2126, .7152, .0722])
        histogram, edges = np.histogram(luma, bins=16, range=(0, 1))
        result[name] = {'min': float(rgb.min()), 'max': float(rgb.max()), 'below_zero': int((rgb < 0).sum()),
                        'above_one': int((rgb > 1).sum()), 'mean_luminance': float(luma.mean()),
                        'histogram_0_1': histogram.tolist(), 'histogram_edges': edges.tolist()}
    source = before.reshape(-1, 4)[:, :3]; target = after.reshape(-1, 4)[:, :3]
    neutral = source.max(axis=1)-source.min(axis=1) <= 1e-6
    result['neutral_pixels'] = int(neutral.sum())
    result['neutral_max_channel_spread'] = float(np.ptp(target[neutral], axis=1).max()) if neutral.any() else None
    result['mean_absolute_difference'] = float(np.abs(target-source).mean())
    return result


def apply_grade(root, grade, rgba, *, source_context, mask=None, max_work_bytes=color.MAX_WORK_BYTES):
    """Apply enabled operations to straight scene-linear pixels without mutation."""
    import numpy as np
    color._exact(grade, ['kind', 'schema_version', 'working_space', 'scope', 'source_id', 'intensity', 'operations', 'grade_sha256'], 'grade')
    _need(grade['kind'] == 'cinematics-grade' and type(grade['schema_version']) is int and grade['schema_version'] == 1, 'unsupported grade')
    expected = make_grade(working_space=grade['working_space'], operations=grade['operations'], scope=grade['scope'],
                          source_id=grade['source_id'], intensity=grade['intensity'])
    _need(grade == expected, 'stale grade identity')
    _need(isinstance(rgba, np.ndarray) and rgba.dtype.kind == 'f' and rgba.dtype.isnative
          and rgba.dtype.itemsize in (4, 8) and rgba.ndim in (2, 3) and rgba.shape[-1] == 4 and rgba.flags.c_contiguous,
          'expected native contiguous float32/64 straight RGBA')
    source_context = color.validate_source(source_context)
    _need(source_context['working_space'] == grade['working_space']
          and source_context['primaries'] == color.REC709_D65
          and source_context['alpha_mode'] == 'straight',
          'grades require an explicitly declared straight scene-linear Rec.709 source')
    count = math.prod(rgba.shape[:-1])
    lut_bytes = sum(op['parameters']['inventory']['bytes'] for op in grade['operations'] if op['enabled'] and op['kind'] == 'lut')
    _need(type(max_work_bytes) is int and 0 < max_work_bytes <= color.MAX_WORK_BYTES
          and 0 < count <= color.MAX_PIXELS and rgba.nbytes+count*256+lut_bytes*8 <= max_work_bytes, 'grading work budget exceeded')
    _need(bool(np.isfinite(rgba).all()) and bool(((rgba[..., 3] >= 0) & (rgba[..., 3] <= 1)).all()), 'invalid grade pixels')
    if mask is not None:
        _need(isinstance(mask, np.ndarray) and mask.shape == rgba.shape[:-1] and mask.dtype.kind == 'f'
              and bool(np.isfinite(mask).all()) and bool(((mask >= 0) & (mask <= 1)).all()), 'invalid secondary mask')
    output = np.array(rgba, dtype=np.float64, copy=True, order='C'); rgb = output.reshape(-1, 4)[:, :3].copy()
    with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
        for operation in grade['operations']:
            if operation['enabled']:
                rgb = _operation(rgb, operation, root)
                _need(bool(np.isfinite(rgb).all()), 'grade operation overflow')
        weight = grade['intensity'] if mask is None else grade['intensity']*mask.reshape(-1, 1)
        source = rgba.reshape(-1, 4)[:, :3]
        rgb = source+(rgb-source)*weight
    _need(bool(np.isfinite(rgb).all()) and bool((np.abs(rgb) <= np.finfo(np.float32).max).all()), 'grading precision overflow')
    output.reshape(-1, 4)[:, :3] = rgb
    output = output.astype(np.float32)
    receipt = {'kind': 'cinematics-grade-execution', 'schema_version': 1, 'grade_sha256': grade['grade_sha256'],
               'input_pixels_sha256': color._hash(memoryview(rgba).cast('B')),
               'output_pixels_sha256': color._hash(memoryview(output).cast('B')),
               'mask_sha256': None if mask is None else color._hash(mask.tobytes()), 'shape': list(rgba.shape),
               'alpha_mode': 'straight', 'working_space': grade['working_space'],
               'source_context_sha256': object_hash(source_context),
               'source_render_verified': False, 'grade_executed': True, 'diagnostics': diagnostics(rgba, output)}
    receipt['execution_sha256'] = object_hash(receipt)
    return output, receipt
