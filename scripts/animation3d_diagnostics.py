"""Bounded, deterministic diagnostics of canonical Animation3D samples.

Reports describe the sampled grid, not a continuous proof or visual approval.
No curve, pose, skeleton or skin evaluator is implemented here.
"""
from copy import deepcopy
from fractions import Fraction
import math
from types import SimpleNamespace

import numpy as np

from animation3d_contracts import KINDS, validate_contract
from animation3d_math import euler_xyz, quaternion_log
from animation3d_registry import sample_track
from animation3d_time import frame_position, rate, TICKS_PER_FRAME
from rig_errors import RigError, require, object_hash
from rig_math import qinverse, qmul, qmatrix, quaternion

DEFAULT_POLICY = dict(samples_per_segment=16, max_samples=4096,
    position_jump_m=1.0, rotation_jump_rad=math.pi/2, scale_jump=.5,
    jump_window_frames=1.0, maximum_velocity=100.0, maximum_acceleration=1000.0,
    overshoot_tolerance=1e-8, loop_value_tolerance=1e-6,
    loop_velocity_tolerance=1e-4, flicker_window_frames=2.0)


def _policy(value):
    require(value is None or isinstance(value, dict), 'ANIMATION_DIAGNOSTICS', 'Policy must be an object.')
    require(not value or set(value) <= set(DEFAULT_POLICY), 'ANIMATION_DIAGNOSTICS', 'Unknown diagnostic policy field.')
    out = dict(DEFAULT_POLICY, **(value or {}))
    for key in ('samples_per_segment', 'max_samples'):
        maximum = 64 if key == 'samples_per_segment' else 4096
        require(type(out[key]) is int and 1 <= out[key] <= maximum,
                'ANIMATION_DIAGNOSTICS', 'Sample limits must be bounded integers.')
    for key in set(out)-{'samples_per_segment', 'max_samples'}:
        item = out[key]
        require(type(item) in (int, float) and 0 <= item <= 1e12 and math.isfinite(item),
                'ANIMATION_DIAGNOSTICS', 'Thresholds must be bounded nonnegative finite numbers.')
    return out


def _time(position):
    ticks = position * TICKS_PER_FRAME
    require(ticks.denominator == 1, 'ANIMATION_DIAGNOSTICS', 'Diagnostic time is outside the exact clock.')
    frame, sub = divmod(ticks.numerator, TICKS_PER_FRAME)
    return dict(frame=frame, subframe_ticks=sub)


def _grid(keys, duration, config):
    positions = [frame_position(k['time']) for k in keys]
    require(len(positions) <= 512, 'ANIMATION_DIAGNOSTICS', 'At most 512 keys per diagnostic track.')
    count = max(0, len(positions)-1)*config['samples_per_segment'] + len(positions) + 2
    require(count <= config['max_samples'], 'ANIMATION_DIAGNOSTICS', 'Requested sample grid exceeds its budget.')
    result = {Fraction(0), Fraction(duration), *positions}
    for a, b in zip(positions, positions[1:]):
        for i in range(1, config['samples_per_segment']):
            raw = (a+(b-a)*Fraction(i, config['samples_per_segment']))*TICKS_PER_FRAME
            tick = (2*raw.numerator+raw.denominator)//(2*raw.denominator)
            result.add(Fraction(tick, TICKS_PER_FRAME))
    require(len(result) <= config['max_samples'], 'ANIMATION_DIAGNOSTICS', 'Sample grid exceeds its budget.')
    return sorted(result)


def _distance(a, b, kind):
    if kind == 'quaternion':
        return 2*math.acos(min(1., abs(float(np.dot(quaternion(a), quaternion(b))))))
    return float(np.linalg.norm(np.asarray(b, dtype=float)-np.asarray(a, dtype=float)))


def _velocity(a, b, kind, seconds):
    if kind == 'quaternion':
        first, last = quaternion(a), quaternion(b)
        if np.dot(first, last) < 0:
            last = -last
        return 2*quaternion_log(qmul(last, qinverse(first)))/seconds
    return (np.asarray(b, dtype=float)-np.asarray(a, dtype=float))/seconds


def diagnose_track(track, registry, *, fps, duration_frames, loop=False,
                   intentional_cuts=(), policy=None):
    """Inspect canonical sampled values, using a single admitted target binding.

    A step interpolation is an intentional discontinuity. Named cut times also
    suppress derivative/jump/loop-seam warnings at that boundary. Visibility
    pulse diagnostics require explicit cuts to acknowledge rapid on/off pulses.
    """
    require(isinstance(track, dict), 'ANIMATION_DIAGNOSTICS', 'Track must be an object.')
    config = _policy(policy)
    clock = rate(fps)
    require(type(duration_frames) is int and 0 <= duration_frames <= 10_000_000,
            'ANIMATION_DIAGNOSTICS', 'Duration must be a bounded integer frame count.')
    require(type(loop) is bool, 'ANIMATION_DIAGNOSTICS', 'Loop flag must be Boolean.')
    require(isinstance(intentional_cuts, (tuple, list)) and len(intentional_cuts) <= 512,
            'ANIMATION_DIAGNOSTICS', 'Cut list must be bounded.')
    cuts = {frame_position(t) for t in intentional_cuts}
    require(all(t <= duration_frames for t in cuts), 'ANIMATION_DIAGNOSTICS', 'Cut exceeds timeline duration.')
    pinned = deepcopy(track)
    findings = []
    def emit(code, position, observed=None, limit=None, **details):
        findings.append(dict(code=code, time=_time(position), observed=observed, limit=limit, **details))
    try:
        validate_contract(dict(schema_version=1, kind=KINDS['track'], value=pinned))
        entry = deepcopy(registry._validated_track_entry(pinned, duration_frames))
    except RigError as exc:
        extrapolation = any(pinned.get(k, 'hold') != 'hold' for k in ('extrapolation_before', 'extrapolation_after'))
        emit('invalid-extrapolation' if extrapolation else 'invalid-track', Fraction(0), error=deepcopy(exc.report))
        return dict(source_valid=False, findings=findings, samples=[], coverage='not sampled: invalid source')
    # The binding above is validated by the supplied authoritative registry.
    # Sampling still delegates all interpolation and post-domain checks to it.
    frozen = SimpleNamespace(_validated_track_entry=lambda requested: deepcopy(entry))
    keys = pinned['keys'] if pinned['enabled'] else []
    points = _grid(keys, duration_frames, config)
    kind = pinned['value_type']
    point_indices = {position: index for index, position in enumerate(points)}
    samples = []
    for position in points:
        try:
            value = sample_track(pinned, _time(position), frozen)
        except RigError as exc:
            emit('property-domain-violation', position, error=deepcopy(exc.report))
            continue
        samples.append(dict(time=_time(position), value=deepcopy(value)))
        if kind not in ('boolean', 'quaternion'):
            interval = next((i for i in range(len(keys)-1)
                             if frame_position(keys[i]['time']) < position < frame_position(keys[i+1]['time'])), None)
            if interval is not None:
                a, b = [np.asarray(k['value'], dtype=float) for k in keys[interval:interval+2]]
                v = np.asarray(value, dtype=float)
                excess = float(np.maximum(np.minimum(a, b)-v, v-np.maximum(a, b)).max())
                if excess > config['overshoot_tolerance']:
                    emit('spline-overshoot', position, excess, config['overshoot_tolerance'])
    modes = [k.get('interpolation', 'step' if kind == 'boolean' else
                   'quaternion-slerp' if kind == 'quaternion' else 'linear') for k in keys]
    step_intervals = [(frame_position(a['time']), frame_position(b['time']))
                      for a, b, mode in zip(keys, keys[1:], modes) if mode == 'step']
    jump_code = 'rotation-jump' if kind == 'quaternion' else 'scale-discontinuity' if pinned['target']['property'] == 'scale' else 'position-jump'
    jump_limit = config['rotation_jump_rad'] if kind == 'quaternion' else config['scale_jump'] if pinned['target']['property'] == 'scale' else config['position_jump_m']
    for a, b, mode in zip(keys, keys[1:], modes):
        t0, t1 = frame_position(a['time']), frame_position(b['time'])
        if kind == 'quaternion' and float(np.dot(a['value'], b['value'])) < 0:
            emit('quaternion-sign', t1, float(np.dot(a['value'], b['value'])), 0)
        distance = _distance(a['value'], b['value'], kind)
        if kind != 'boolean' and mode != 'step' and t1 not in cuts and t1-t0 <= config['jump_window_frames'] and distance > jump_limit:
            emit(jump_code, t1, distance, jump_limit)
    velocities = []
    for first, last in zip(samples, samples[1:]):
        a, b = frame_position(first['time']), frame_position(last['time'])
        # Missing/invalid samples never bridge into an invented valid interval.
        if point_indices[b]-point_indices[a] != 1 or kind == 'boolean':
            continue
        if any(a < cut <= b for cut in cuts) or any(start < b and a < end for start, end in step_intervals):
            continue
        velocity = _velocity(first['value'], last['value'], kind, float((b-a)/clock))
        speed = float(np.linalg.norm(velocity))
        if speed > config['maximum_velocity']:
            emit('velocity-spike', b, speed, config['maximum_velocity'])
        velocities.append((a, b, velocity))
    for previous, current in zip(velocities, velocities[1:]):
        if previous[1] != current[0]:
            continue
        dt = float(((current[1]-current[0])+(previous[1]-previous[0]))/(2*clock))
        acceleration = float(np.linalg.norm(current[2]-previous[2]))/dt
        if acceleration > config['maximum_acceleration']:
            emit('extreme-acceleration', current[0], acceleration, config['maximum_acceleration'])
    if kind == 'boolean':
        changes = []
        for key in keys:
            if not changes or key['value'] != changes[-1]['value']:
                changes.append(key)
        for a, b, c in zip(changes, changes[1:], changes[2:]):
            start, end = frame_position(b['time']), frame_position(c['time'])
            if a['value'] == c['value'] != b['value'] and end-start <= config['flicker_window_frames'] and not {start, end} & cuts:
                emit('visibility-flicker', start, float(end-start), config['flicker_window_frames'])
    if loop and samples and len(samples) == len(points) and not {Fraction(0), Fraction(duration_frames)} & cuts:
        seam = _distance(samples[-1]['value'], samples[0]['value'], kind)
        if seam > config['loop_value_tolerance']:
            emit('broken-loop-seam', Fraction(duration_frames), seam, config['loop_value_tolerance'])
        if len(velocities) >= 2 and velocities[0][0] == 0 and velocities[-1][1] == duration_frames:
            delta = float(np.linalg.norm(velocities[-1][2]-velocities[0][2]))
            if delta > config['loop_velocity_tolerance']:
                emit('broken-loop-velocity', Fraction(duration_frames), delta, config['loop_velocity_tolerance'])
    return dict(source_valid=True, sample_complete=len(samples) == len(points),
        track_id=pinned['id'], track_sha256=object_hash(pinned), target=deepcopy(pinned['target']),
        target_binding=deepcopy(entry.get('source')), findings=findings, samples=samples,
        policy=config, coverage='bounded canonical sample grid; extrema between samples may be missed',
        approved=False, units='meters / radians / scale, derivatives per second')


def diagnose_clip(clip, registry, *, intentional_cuts=(), policy=None):
    """Inspect a clip without changing it or running playback state."""
    clip = deepcopy(clip)
    validate_contract(clip)
    require(clip['kind'] == KINDS['clip'], 'ANIMATION_DIAGNOSTICS', 'Expected a clip.')
    config = _policy(policy)
    require(len(clip['tracks']) <= 128, 'ANIMATION_DIAGNOSTICS', 'At most 128 diagnostic tracks.')
    predicted = sum(max(0, len(t['keys'])-1)*config['samples_per_segment'] + len(t['keys']) + 2 for t in clip['tracks'])
    require(predicted <= 16384, 'ANIMATION_DIAGNOSTICS', 'Aggregate clip diagnostic sample budget exceeded.')
    return dict(clip_id=clip['id'], tracks=[diagnose_track(t, registry, fps=clip['fps'],
        duration_frames=clip['duration_frames'], loop=clip['loop_mode'] == 'loop',
        intentional_cuts=intentional_cuts, policy=config) for t in clip['tracks']], approved=False)


def diagnose_euler_conversion(quaternions, converted_xyz, *, tolerance_rad=1e-6):
    """Check actual caller-provided XYZ conversion against canonical quaternions.

    Euler triples are radians, X then Y then Z. These are conversion diagnostics,
    not another Euler animation channel or a promise to remove gimbal lock.
    """
    require(isinstance(quaternions, (list, tuple)) and isinstance(converted_xyz, (list, tuple))
            and 0 < len(quaternions) == len(converted_xyz) <= 4096,
            'ANIMATION_DIAGNOSTICS', 'Conversion samples must be equally sized bounded lists.')
    require(type(tolerance_rad) in (int, float) and 0 < tolerance_rad <= 1 and math.isfinite(tolerance_rad),
            'ANIMATION_DIAGNOSTICS', 'Angular tolerance must be finite and positive.')
    findings = []
    previous = None
    for index, (raw, xyz) in enumerate(zip(quaternions, converted_xyz)):
        for value, size in ((raw, 4), (xyz, 3)):
            require(isinstance(value, (list, tuple)) and len(value) == size
                    and all(type(v) in (int, float) and -1e12 <= v <= 1e12 and math.isfinite(v) for v in value),
                    'ANIMATION_DIAGNOSTICS', 'Conversion components must be bounded finite numbers of the correct dimension.')
        q = quaternion(raw)
        rebuilt = euler_xyz(xyz)
        error = _distance(q, rebuilt, 'quaternion')
        if error > tolerance_rad:
            findings.append(dict(code='euler-conversion-error', sample=index, observed=error, limit=tolerance_rad))
        if abs(abs(float(qmatrix(q)[2, 0]))-1) <= tolerance_rad:
            findings.append(dict(code='euler-conversion-singularity', sample=index))
        if previous is not None:
            rotation = _distance(previous[0], q, 'quaternion')
            coordinate = float(np.linalg.norm(np.asarray(xyz)-np.asarray(previous[1])))
            if coordinate > math.pi and rotation < .1:
                findings.append(dict(code='euler-coordinate-jump', sample=index, observed=coordinate, physical_rotation_rad=rotation))
        previous = (q, deepcopy(xyz))
    return dict(findings=findings, sample_count=len(quaternions), approved=False)
