"""Codec-specific, bounded software encoder controls for silent media.

Quality scales are deliberately separate: equal numeric H.264 CRF, VP9 CQ and
AV1 CRF values do not denote equal visual quality or rate.
"""
from __future__ import annotations
import copy
from production_contracts import _structural
from production_render import fps_fraction

PRESETS = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium', 'slow', 'slower', 'veryslow')
QUALITY = {
    'h264': {'draft': (28, 'ultrafast'), 'preview': (24, 'veryfast'), 'standard': (20, 'medium'),
             'high': (17, 'slow'), 'master': (14, 'slow')},
    'vp9': {'draft': (42, 8), 'preview': (36, 6), 'standard': (30, 4), 'high': (24, 2), 'master': (18, 1)},
    'av1': {'draft': (44, 8), 'preview': (38, 7), 'standard': (32, 6), 'high': (26, 4), 'master': (20, 2)},
}


def _obj(properties):
    return {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': list(properties)}


def _int(low, high):
    return {'type': 'integer', 'minimum': low, 'maximum': high}


def encoding_schema():
    quality = _obj({'mode': {'const': 'quality'}, 'value': _int(0, 63)})
    bitrate = _obj({'mode': {'const': 'bitrate'}, 'bitrate': _int(1000, 10**9)})
    constrained = _obj({'mode': {'const': 'constrained'}, 'bitrate': _int(1000, 10**9),
                        'maxrate': _int(1000, 10**9), 'bufsize': _int(1000, 2*10**9)})
    two_pass = _obj({'mode': {'const': 'two-pass'}, 'bitrate': _int(1000, 10**9)})
    return _obj({'speed': {'anyOf': [{'enum': list(PRESETS)}, _int(0, 8)]},
                 'threads': _int(1, 2), 'gop': _int(1, 600),
                 'rate_control': {'anyOf': [quality, bitrate, constrained, two_pass]}})


def validate_encoding(profile, settings):
    if (not isinstance(profile, dict) or not isinstance(profile.get('codec'), str) or
            profile['codec'] not in QUALITY):
        raise ValueError('unsupported codec controls')
    try:
        _structural(settings, encoding_schema(), 'silent encoding controls')
    except (OverflowError, RecursionError) as exc:
        raise ValueError('encoding controls exceed numeric/nesting limits') from exc
    codec = profile['codec']
    if codec not in QUALITY:
        raise ValueError('unsupported codec controls')
    speed = settings['speed']
    if codec == 'h264':
        if not isinstance(speed, str) or speed not in PRESETS:
            raise ValueError('H.264 speed must select an x264 software preset')
    elif type(speed) is not int:
        raise ValueError('VP9/AV1 speed must select a bounded codec-specific integer')
    control = settings['rate_control']
    if control['mode'] == 'quality' and codec == 'h264' and control['value'] > 51:
        raise ValueError('H.264 CRF must lie in 0..51')
    if control['mode'] == 'constrained' and control['maxrate'] < control['bitrate']:
        raise ValueError('maximum rate must cover average bitrate')
    return copy.deepcopy(settings)


def quality_settings(profile, quality='standard', *, fps=None, threads=1):
    if (not isinstance(profile, dict) or not isinstance(profile.get('codec'), str) or
            not isinstance(quality, str) or quality not in QUALITY.get(profile.get('codec'), {})):
        raise ValueError('unsupported codec quality profile')
    value, speed = QUALITY[profile['codec']][quality]
    rate = fps_fraction({'numerator': 24, 'denominator': 1} if fps is None else fps)
    return validate_encoding(profile, {'speed': speed, 'threads': threads,
        'gop': max(1, min(600, round(rate * 2))), 'rate_control': {'mode': 'quality', 'value': value}})


def arguments(profile, settings):
    settings = validate_encoding(profile, settings)
    result = ['-threads', str(settings['threads']), '-g', str(settings['gop'])]
    if profile['codec'] == 'h264':
        result += ['-preset', settings['speed']]
    elif profile['codec'] == 'vp9':
        result += ['-deadline', 'good', '-cpu-used', str(settings['speed'])]
    else:
        result += ['-cpu-used', str(settings['speed']), '-row-mt', '1']
    control = settings['rate_control']
    if control['mode'] == 'quality':
        result += ['-crf', str(control['value'])]
        if profile['codec'] != 'h264':
            result += ['-b:v', '0']
    else:
        result += ['-b:v', str(control['bitrate'])]
        if control['mode'] == 'constrained':
            result += ['-maxrate', str(control['maxrate']), '-bufsize', str(control['bufsize'])]
    return result


QUALITY_PROFILE_VERSION = 1
SOFTWARE_ENCODERS = {'h264': 'libx264', 'vp9': 'libvpx-vp9', 'av1': 'libaom-av1'}


def _profile_digest(value):
    import hashlib
    import json
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     allow_nan=False).encode('utf-8')).hexdigest()


def _validate_rate(rate):
    try:
        _structural(rate, _obj({'numerator': _int(1, 240000), 'denominator': _int(1, 10000)}),
                    'named profile frame rate')
    except (OverflowError, RecursionError) as exc:
        raise ValueError('frame rate exceeds numeric/nesting limits') from exc


def capture_quality_profile(profile, quality='standard', *, fps=None, threads=1):
    """Capture a versioned, immutable-by-value named software profile.

    Callers retain this manifest beside the request and pass its ``encoding``
    field to make_request. Existing request schemas and defaults stay compatible.
    The digest binds the exact named profile, frame rate and resolved controls.
    """
    if (not isinstance(profile, dict) or not isinstance(profile.get('codec'), str) or
            not isinstance(profile.get('encoder'), str) or not isinstance(quality, str)):
        raise ValueError('invalid named quality profile selection')
    codec = profile.get('codec')
    if codec not in SOFTWARE_ENCODERS or profile.get('encoder') != SOFTWARE_ENCODERS[codec]:
        raise ValueError('named quality profiles require a supported software encoder')
    rate = {'numerator': 24, 'denominator': 1} if fps is None else copy.deepcopy(fps)
    _validate_rate(rate)
    settings = quality_settings(profile, quality, fps=rate, threads=threads)
    value = {'schema_version': QUALITY_PROFILE_VERSION, 'kind': 'silent-quality-profile',
             'codec': codec, 'encoder': profile['encoder'], 'name': quality,
             'fps': rate, 'encoding': settings}
    return {**value, 'sha256': _profile_digest(value)}


def validate_quality_profile(value):
    """Reject changed, stale, unknown-version or mislabeled named profiles."""
    if not isinstance(value, dict) or set(value) != {
            'schema_version', 'kind', 'codec', 'encoder', 'name', 'fps', 'encoding', 'sha256'}:
        raise ValueError('invalid named quality profile manifest')
    if type(value['schema_version']) is not int or value['schema_version'] != QUALITY_PROFILE_VERSION:
        raise ValueError('unsupported named quality profile version')
    if value['kind'] != 'silent-quality-profile' or not isinstance(value['encoding'], dict):
        raise ValueError('invalid named quality profile manifest')
    expected = capture_quality_profile({'codec': value['codec'], 'encoder': value['encoder']},
        value['name'], fps=value['fps'], threads=value['encoding'].get('threads'))
    if value != expected:
        raise ValueError('named quality profile controls or digest changed')
    return copy.deepcopy(value)


def estimate_storage(profile, settings, *, frame_count, fps, max_output_bytes=512 * 1024**2):
    """Return a bounded planning estimate, never a quality-mode size promise.

    Average bitrate gives an exact rational *target* for elementary video bytes;
    codec rate control and container overhead can change actual size. The broad
    range is deliberately the enforced export byte cap for either control mode.
    No sample encode, hardware probe or measured size is invented here.
    """
    settings = validate_encoding(profile, settings)
    if type(frame_count) is not int or not 1 <= frame_count <= 18000:
        raise ValueError('frame count must be a bounded positive integer')
    if type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= 2 * 1024**3:
        raise ValueError('output byte budget must lie in 1024..2 GiB')
    from fractions import Fraction
    _validate_rate(fps)
    duration = Fraction(frame_count, 1) / fps_fraction(fps)
    control = settings['rate_control']
    target = None
    if control['mode'] != 'quality':
        size = duration * control['bitrate'] / 8
        target = (size.numerator + size.denominator - 1) // size.denominator
    return {'schema_version': 1, 'kind': 'silent-storage-estimate',
            'codec': profile['codec'], 'rate_control': control['mode'],
            'duration': {'numerator': duration.numerator, 'denominator': duration.denominator},
            'nominal_video_bytes': target, 'output_byte_range': {'minimum': 0, 'maximum': max_output_bytes},
            'nominal_target_exceeds_budget': target is not None and target > max_output_bytes,
            'uncertainty': 'Source-dependent codec size and container overhead; range is the enforced output cap, not a confidence interval.',
            'measured_sample': None}
