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
    _structural(settings, encoding_schema(), 'silent encoding controls')
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
    if quality not in QUALITY.get(profile.get('codec'), {}):
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
