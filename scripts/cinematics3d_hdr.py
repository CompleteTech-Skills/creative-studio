"""Explicit absolute-light HDR extension of the canonical OCIO color authority.

No encoding, mastering metadata, tone mapping, renderer or global OCIO state.
The source profile and measured working primaries are verified by the existing
color module. HDR display transforms are discovered from pinned OCIO itself.
"""
from copy import deepcopy
import hashlib
import math

import cinematics3d_color as color
from rig_errors import object_hash

REC2020_D65 = {'r': [.708, .292], 'g': [.170, .797], 'b': [.131, .046], 'w': [.3127, .3290]}
_STYLES = {
    'hdr-pq': 'DISPLAY - CIE-XYZ-D65_to_REC.2100-PQ',
    'hdr-hlg-1000': 'DISPLAY - CIE-XYZ-D65_to_REC.2100-HLG-1000nit',
}


def _absolute_nits(value, high, *, minimum=0):
    # Bound arbitrary JSON integers before the pinned base's float conversion.
    color._need(type(value) in (int, float) and minimum <= value <= high,
                'invalid bounded absolute luminance')
    color._number(value, minimum, high)


def capabilities():
    ocio = color._ocio()
    styles = set(ocio.BuiltinTransformRegistry())
    return dict(kind='cinematics-hdr-capabilities', schema_version=1,
                ocio_version=ocio.GetVersion(), targets={target: dict(
                    supported=style in styles, style=style, description=None,
                    unsupported_reason=None if style in styles else 'Pinned OCIO builtin is unavailable')
                    for target, style in _STYLES.items()},
                mastering_metadata_supported=False, encoded_video_verified=False)


def _cpu(config, ocio, working_space, target, reference_white_nits):
    group = ocio.GroupTransform()
    source = ocio.ColorSpaceTransform()
    source.setSrc(working_space)
    source.setDst(config.getColorSpace('cie_xyz_d65_interchange').getName())
    group.appendTransform(source)
    # OCIO's display builtins interpret CIE XYZ Y=1 as 100 cd/m².
    # The caller explicitly defines the absolute luminance of source RGB white.
    gain = reference_white_nits / 100.
    scale = ocio.MatrixTransform()
    scale.setMatrix([gain, 0, 0, 0, 0, gain, 0, 0, 0, 0, gain, 0, 0, 0, 0, 1])
    group.appendTransform(scale)
    transform = ocio.BuiltinTransform()
    transform.setStyle(_STYLES[target])
    group.appendTransform(transform)
    return config.getProcessor(group).getDefaultCPUProcessor()


def pq_reference(nits):
    """Independent BT.2100/ST2084 absolute luminance oracle for calibration."""
    _absolute_nits(nits, 10000)
    m1, m2 = 2610/16384, 2523/32
    c1, c2, c3 = 3424/4096, 2413/128, 2392/128
    value = (nits / 10000.) ** m1
    return ((c1+c2*value)/(1+c3*value)) ** m2


def hlg_reference(nits):
    """Neutral-axis BT.2100 HLG oracle: 1000-nit display, gamma 1.2."""
    _absolute_nits(nits, 1000)
    scene = (nits / 1000.) ** (1/1.2)
    a, b, c = .17883277, .28466892, .55991073
    return math.sqrt(3*scene) if scene <= 1/12 else a*math.log(12*scene-b)+c


def _xyz_to_2020():
    # Analysis matrix derived from declared BT.2020/D65 chromaticities; actual
    # output conversion remains OCIO's discovered display builtin.
    import numpy as np
    columns = []
    for key in ('r', 'g', 'b'):
        x, y = REC2020_D65[key]
        columns.append([x/y, 1., (1-x-y)/y])
    basis = np.array(columns, dtype=np.float64).T
    x, y = REC2020_D65['w']
    scale = np.linalg.solve(basis, np.array([x/y, 1., (1-x-y)/y]))
    return np.linalg.inv(basis*scale)


def make_plan(root, inventory, source, *, target, reference_white_nits):
    color._need(type(target) is str and target in _STYLES, 'unsupported HDR target')
    _absolute_nits(reference_white_nits, 10000, minimum=.001)
    supported = capabilities()['targets'][target]
    color._need(supported['supported'], supported['unsupported_reason'])
    # Reuse canonical role/closure/linear-source and primaries calibration.
    linear = color.make_plan(root, inventory, source, target='scene-linear', display='', view='')
    peak = 10000 if target == 'hdr-pq' else 1000
    with color._config(root, linear['config']) as (config, ocio):
        cpu = _cpu(config, ocio, source['working_space'], target, reference_white_nits)
        oracle = pq_reference if target == 'hdr-pq' else hlg_reference
        errors = []
        for nits in (0., .1, 1., 18., 100., 203., 1000.):
            actual = color._rgb(cpu, [nits/reference_white_nits]*3)
            errors.extend(abs(value-oracle(nits)) for value in actual)
        import numpy as np
        xyz_cpu = config.getProcessor(source['working_space'], config.getColorSpace('cie_xyz_d65_interchange').getName()).getDefaultCPUProcessor()
        to2020 = _xyz_to_2020()
        for patch in ([.5, .1, .03], [.1, .5, .1], [.1, .1, .5]):
            # Calibrate below the supported reference peak at any explicit white.
            vector = [v*min(1., peak/reference_white_nits) for v in patch]
            nits = to2020 @ np.array(color._rgb(xyz_cpu, vector))*reference_white_nits
            color._need(bool((nits >= 0).all()) and bool((nits <= peak).all()), 'HDR calibration patch outside output gamut')
            if target == 'hdr-pq':
                expected = [pq_reference(float(v)) for v in nits]
            else:
                luminance = float(nits @ np.array([.2627, .6780, .0593]))
                scene_y = (luminance/1000.)**(1/1.2)
                scene_rgb = nits/(1000*scene_y**.2)
                expected = [math.sqrt(3*v) if v <= 1/12 else .17883277*math.log(12*v-.28466892)+.55991073 for v in scene_rgb]
            errors.extend(abs(a-b) for a,b in zip(color._rgb(cpu, vector), expected))
        error = max(errors)
        color._need(error < 2e-4, 'OCIO HDR display fails independent absolute-light calibration')
    value = dict(kind='cinematics-hdr-plan', schema_version=1, linear_plan=linear,
                 target=target, reference_white_nits=reference_white_nits,
                 reference_peak_nits=peak, builtin=supported, calibration_max_error=error,
                 output_primaries=deepcopy(REC2020_D65), output_transfer='smpte2084' if target=='hdr-pq' else 'arib-std-b67',
                 output_matrix='rgb', output_range='full', output_alpha='straight',
                 mastering_metadata=None, encoded_video_verified=False)
    value['plan_sha256'] = object_hash(value)
    return value


def transform_pixels(root, plan, rgba, *, max_work_bytes=color.MAX_WORK_BYTES):
    """Execute bounded actual OCIO; report all out-of-domain component clipping."""
    import numpy as np
    color._need(type(plan) is dict and plan.get('kind')=='cinematics-hdr-plan', 'invalid HDR plan')
    source_plan = plan.get('linear_plan')
    color._need(type(source_plan) is dict, 'missing canonical linear plan')
    color._need(isinstance(rgba, np.ndarray) and rgba.ndim in (2, 3) and rgba.shape[-1]==4, 'expected RGBA pixels')
    count = math.prod(rgba.shape[:-1])
    color._need(0 < count <= color.MAX_PIXELS, 'HDR pixel count exceeds budget')
    inventory = color.validate_inventory(source_plan.get('config'))
    color._need(type(max_work_bytes) is int and 0 < max_work_bytes <= color.MAX_WORK_BYTES,
                'invalid HDR work budget')
    color._need(rgba.nbytes + count*128 + 2*sum(f['bytes'] for f in inventory['files']) <= max_work_bytes,
                'HDR work byte budget exceeded')
    expected = make_plan(root, source_plan.get('config'), source_plan.get('source'),
                         target=plan.get('target'), reference_white_nits=plan.get('reference_white_nits'))
    # Equal Python values can still encode different typed, hash-bound records.
    color._need(plan == expected and object_hash(plan) == object_hash(expected), 'stale/forged HDR plan')
    linear, source_receipt = color.transform_pixels(root, source_plan, rgba, max_work_bytes=max_work_bytes)
    output = linear.copy()
    flat = output.reshape(-1, 4)
    clipped_low = clipped_high = below_zero = above_peak = 0
    with color._config(root, source_plan['config']) as (config, ocio):
        cpu = _cpu(config, ocio, source_plan['source']['working_space'], plan['target'], plan['reference_white_nits'])
        xyz_cpu = config.getProcessor(source_plan['source']['working_space'],
            config.getColorSpace('cie_xyz_d65_interchange').getName()).getDefaultCPUProcessor()
        xyz_to_2020 = _xyz_to_2020()
        for start in range(0, count, 65536):
            block = flat[start:start+65536]
            rgb = np.ascontiguousarray(block[:, :3])
            xyz = rgb.copy()
            xyz_cpu.applyRGB(xyz)
            absolute_rgb = (xyz @ xyz_to_2020.T) * plan['reference_white_nits']
            below_zero += int((absolute_rgb < 0).sum())
            above_peak += int((absolute_rgb > plan['reference_peak_nits']).sum())
            cpu.applyRGB(rgb)
            color._need(bool(np.isfinite(rgb).all()), 'HDR processor produced nonfinite pixels')
            clipped_low += int((rgb < 0).sum())
            clipped_high += int((rgb > 1).sum())
            block[:, :3] = np.clip(rgb, 0, 1)
    receipt = dict(kind='cinematics-hdr-execution', schema_version=1, plan_sha256=plan['plan_sha256'],
                   source=source_receipt, output_pixels_sha256=hashlib.sha256(output.tobytes()).hexdigest(),
                   output_transfer=plan['output_transfer'], output_primaries=deepcopy(REC2020_D65),
                   reference_white_nits=plan['reference_white_nits'], reference_peak_nits=plan['reference_peak_nits'],
                   clipped_encoded_components_low=clipped_low, clipped_encoded_components_high=clipped_high,
                   input_bt2020_components_below_zero=below_zero, input_bt2020_components_above_reference_peak=above_peak,
                   gamut_mapping='none; OCIO transfer boundary behavior plus explicit encoded [0,1] clamp',
                   tone_mapping='none',

                   hdr_processor_cache_id=cpu.getCacheID(), ocio_version=color.OCIO_VERSION,
                   pixel_transform_executed=True, mastering_metadata=None, encoded_video_verified=False,
                   source_render_verified=False)
    return output, receipt
