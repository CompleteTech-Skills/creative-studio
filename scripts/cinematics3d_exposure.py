"""Measured optional exposure advice for canonical scene-linear color plans.

This module neither owns a timeline nor alters pixels. Callers supply canonical
rational source times and apply the returned total once through the color API.
"""
from __future__ import annotations
from fractions import Fraction
import math

from rig_errors import object_hash
import cinematics3d_color as color

MAX_PIXELS = 4_194_304
MAX_WORK_BYTES = 512 * 1024**2
HISTOGRAM_BINS = 256


def _need(ok, message):
    if not ok:
        raise ValueError(message)


def _number(value, low, high):
    _need(type(value) in (int, float) and low <= value <= high and math.isfinite(value), 'invalid bounded exposure number')
    return float(value)


def _time(value):
    _need(type(value) is Fraction and 0 <= value <= 86400*365 and abs(value.numerator) <= 2**53
          and value.denominator <= 2**53, 'expected bounded canonical rational source time')
    return {'numerator': value.numerator, 'denominator': value.denominator}


def policy(*, target_gray=.18, minimum_stops=-8., maximum_stops=8., adaptation_seconds=.5):
    target_gray = _number(target_gray, 1e-6, 1.)
    minimum_stops = _number(minimum_stops, -24., 0.)
    maximum_stops = _number(maximum_stops, 0., 24.)
    adaptation_seconds = _number(adaptation_seconds, .001, 3600.)
    return dict(target_gray=target_gray, minimum_stops=minimum_stops,
                maximum_stops=maximum_stops, adaptation_seconds=adaptation_seconds)


def measure(root, inventory, source, rgba, *, max_work_bytes=MAX_WORK_BYTES):
    """Measure alpha-weighted scene luminance with the profile's real XYZ basis.

    Fully transparent pixels are excluded. Negative/zero luminance is reported
    and floored at 2**-40 solely for the metering statistic; source pixels remain
    unchanged. The histogram covers log2 luminance [-40,24] with edge counts.
    """
    import numpy as np
    _need(type(max_work_bytes) is int and 1 <= max_work_bytes <= MAX_WORK_BYTES, 'invalid metering memory budget')
    _need(isinstance(rgba, np.ndarray) and rgba.ndim in (2, 3) and rgba.shape[-1] == 4
          and rgba.dtype in (np.dtype('float32'), np.dtype('float64')) and rgba.flags.c_contiguous,
          'metering needs contiguous floating RGBA')
    count = rgba.size//4
    _need(0 < count <= MAX_PIXELS and rgba.nbytes + count*4*4*3 + 65536*8*8 <= max_work_bytes,
          'metering work budget exceeded')
    plan = color.make_plan(root, inventory, source, target='scene-linear', display='', view='', output_alpha='straight')
    pixels, execution = color.transform_pixels(root, plan, rgba, max_work_bytes=max_work_bytes)
    with color._config(root, inventory) as (config, _):
        xyz = config.getColorSpace('cie_xyz_d65_interchange')
        cpu = config.getProcessor(source['working_space'], xyz.getName()).getDefaultCPUProcessor()
        weights = np.array([color._rgb(cpu, basis)[1] for basis in ([1., 0., 0.], [0., 1., 0.], [0., 0., 1.])], dtype=np.float64)
    _need(bool(np.isfinite(weights).all()) and bool((weights > 0).all()) and abs(float(weights.sum())-1.) < 5e-4,
          'unsupported XYZ luminance basis')
    histogram = np.zeros(HISTOGRAM_BINS, dtype=np.float64)
    log_sum = coverage = 0.
    transparent = nonpositive = below = above = over_one = 0
    flat = pixels.reshape(-1, 4)
    for offset in range(0, count, 65536):
        chunk = flat[offset:offset+65536]
        alpha = chunk[:, 3].astype(np.float64)
        visible = alpha > 0
        transparent += int((~visible).sum())
        luminance = chunk[:, :3].astype(np.float64) @ weights
        visible_luminance = luminance[visible]
        alpha = alpha[visible]
        nonpositive += int((visible_luminance <= 0).sum())
        log_values = np.log2(np.maximum(visible_luminance, 2.**-40))
        below += int((visible_luminance < 2.**-40).sum()); above += int((log_values > 24).sum())
        over_one += int((visible_luminance > 1).sum())
        log_sum += float(np.dot(log_values, alpha)); coverage += float(alpha.sum())
        indices = np.minimum(np.floor((np.clip(log_values, -40, 24)+40)*4).astype(np.int64), 255)
        histogram += np.bincount(indices, weights=alpha, minlength=HISTOGRAM_BINS)
    _need(math.isfinite(log_sum) and math.isfinite(coverage), 'nonfinite luminance reduction')
    value = dict(kind='cinematics-exposure-meter', schema_version=1, pixels=count,
                 transparent_pixels=transparent, nonpositive_visible_pixels=nonpositive,
                 histogram_log2_min=-40, histogram_log2_max=24,
                 histogram_alpha_weights=histogram.tolist(), below_histogram=below, above_histogram=above,
                 visible_over_one_pixels=over_one, alpha_coverage=coverage,
                 log2_mean_luminance=None if coverage == 0 else log_sum/coverage,
                 xyz_luminance_weights=weights.tolist(), color_execution=execution,
                 source_render_verified=False)
    value['meter_sha256'] = object_hash(value)
    return value


def _bounded_record(value):
    # Admission precedes hashing: a caller cannot force an unbounded JSON
    # serialization by supplying a forged state or nested execution receipt.
    remaining = 2048
    def visit(node, depth=0):
        nonlocal remaining
        remaining -= 1
        _need(remaining >= 0 and depth <= 8, 'exposure record complexity exceeded')
        if type(node) is dict:
            _need(len(node) <= 64 and all(type(k) is str and len(k) <= 128 for k in node), 'invalid exposure record mapping')
            for child in node.values():
                visit(child, depth+1)
        elif type(node) is list:
            _need(len(node) <= 256, 'exposure record list budget exceeded')
            for child in node:
                visit(child, depth+1)
        elif type(node) is str:
            _need(len(node) <= 4096, 'exposure record text budget exceeded')
        elif type(node) is int:
            _need(node.bit_length() <= 128, 'exposure record integer budget exceeded')
        elif type(node) is float:
            _need(math.isfinite(node), 'nonfinite exposure record')
        else:
            _need(node is None or type(node) is bool, 'unsupported exposure record value')
    visit(value)


def _validate_meter(value):
    _bounded_record(value)
    _need(type(value) is dict and value.get('kind') == 'cinematics-exposure-meter' and type(value.get('schema_version')) is int and value.get('schema_version') == 1,
          'invalid exposure measurement')
    _need(value.get('meter_sha256') == object_hash({k:v for k,v in value.items() if k != 'meter_sha256'}),
          'stale exposure measurement')
    coverage = _number(value.get('alpha_coverage'), 0., MAX_PIXELS)
    luminance = value.get('log2_mean_luminance')
    if coverage == 0:
        _need(luminance is None, 'empty exposure measurement has luminance')
    else:
        _number(luminance, -40., 128.)
    return luminance


def advise(measurement, source_time, *, authored_stops=0., settings=None, previous=None,
           enabled=False, reset=False, sequence_id='default'):
    """Return deterministic resumable metering advice, without changing pixels.

    A seek backwards or duplicate time requires explicit reset. A caller starts
    a new history on shot cuts; hidden random-access integration is not claimed.
    Authored exposure remains recorded separately and is never overwritten.
    """
    _need(type(enabled) is bool and type(reset) is bool, 'exposure switches must be booleans')
    _need(type(sequence_id) is str and 0 < len(sequence_id) <= 128 and sequence_id.isascii()
          and all(32 <= ord(c) < 127 for c in sequence_id), 'invalid exposure sequence identity')
    _need(settings is None or type(settings) is dict and set(settings) == set(policy()), 'invalid exposure policy fields')
    settings = policy() if settings is None else policy(**settings)
    settings_hash = object_hash(settings)
    time = _time(source_time)
    authored = _number(authored_stops, -24., 24.)
    log_luminance = _validate_meter(measurement)
    desired = 0. if log_luminance is None else max(settings['minimum_stops'], min(settings['maximum_stops'], math.log2(settings['target_gray'])-log_luminance))
    correction = desired if enabled else 0.
    previous_hash = None
    if previous is not None and not reset:
        _bounded_record(previous)
        _need(type(previous) is dict and previous.get('kind') == 'cinematics-exposure-advice'
              and type(previous.get('schema_version')) is int and previous.get('schema_version') == 1
              and previous.get('state_sha256') == object_hash({k:v for k,v in previous.items() if k != 'state_sha256'}),
              'invalid exposure history')
        _need(previous.get('policy_sha256') == settings_hash and previous.get('sequence_id') == sequence_id
              and previous.get('enabled') is enabled, 'exposure history policy changed; reset required')
        previous_time = previous.get('source_time')
        _need(type(previous_time) is dict and set(previous_time) == {'numerator','denominator'}
              and type(previous_time['numerator']) is int and type(previous_time['denominator']) is int
              and previous_time['denominator'] > 0, 'invalid exposure history time')
        prior = Fraction(**previous_time); _time(prior)
        delta = source_time-prior
        _need(0 < delta <= 86400, 'exposure history must advance; reset on seek or large gap')
        prior_correction = _number(previous.get('correction_stops'), settings['minimum_stops'], settings['maximum_stops'])
        if enabled:
            blend = -math.expm1(-float(delta)/settings['adaptation_seconds'])
            correction = prior_correction + blend*(desired-prior_correction)
        previous_hash = previous['state_sha256']
    total = authored + correction
    _number(total, -24., 24.)
    value = dict(kind='cinematics-exposure-advice', schema_version=1, source_time=time,
                 sequence_id=sequence_id, enabled=enabled, policy=settings, policy_sha256=settings_hash,
                 authored_stops=authored, desired_correction_stops=desired, correction_stops=correction,
                 total_stops=total, meter_sha256=measurement['meter_sha256'], previous_state_sha256=previous_hash,
                 pixel_transform_executed=False, exposure_applications=0)
    value['state_sha256'] = object_hash(value)
    return value
