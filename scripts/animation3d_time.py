"""Exact animation clocks. Terminal frame N is a boundary, never output N.

Subframes use 1,000,000 ticks/frame. Float conversion is confined to the
existing rig evaluator boundary, never repeated to accumulate time.
"""
from fractions import Fraction
from rig_errors import require

TICKS_PER_FRAME = 1_000_000
MAX_FRAMES = 10_000_000


def rate(fps):
    require(isinstance(fps, dict) and set(fps) == {'numerator', 'denominator'},
            'ANIMATION_RATE', 'Expected numerator/denominator frame rate.')
    n, d = fps['numerator'], fps['denominator']
    require(type(n) is int and type(d) is int and 0 < n <= 1_000_000 and 0 < d <= 1_000_000,
            'ANIMATION_RATE', 'Frame rate terms must be bounded positive integers.')
    result = Fraction(n, d)
    require(Fraction(1, 1000) <= result <= 1000, 'ANIMATION_RATE', 'Frame rate outside [0.001,1000].')
    return result


def frame_position(value):
    require(isinstance(value, dict) and set(value) <= {'frame', 'subframe_ticks'} and 'frame' in value,
            'ANIMATION_TIME', 'Expected frame and optional subframe_ticks.')
    frame, ticks = value['frame'], value.get('subframe_ticks', 0)
    require(type(frame) is int and 0 <= frame <= MAX_FRAMES and type(ticks) is int and 0 <= ticks < TICKS_PER_FRAME,
            'ANIMATION_TIME', 'Frame/subframe outside clock bounds.')
    return Fraction(frame) + Fraction(ticks, TICKS_PER_FRAME)


def seconds(value, fps):
    return frame_position(value) / rate(fps)


def at_seconds(value, fps):
    require(isinstance(value, (int, Fraction)) and not isinstance(value, bool) and value >= 0,
            'ANIMATION_TIME', 'Time must be a nonnegative exact integer or Fraction.')
    ticks = value * rate(fps) * TICKS_PER_FRAME
    require(ticks.denominator == 1, 'ANIMATION_TIME', 'Time is not representable at configured tick precision.')
    frame, subframe = divmod(ticks.numerator, TICKS_PER_FRAME)
    result = {'frame': frame, 'subframe_ticks': subframe}
    frame_position(result)
    return result


def sample_time(index, total_frames, fps):
    require(type(index) is int and type(total_frames) is int and 0 <= index < total_frames <= MAX_FRAMES,
            'ANIMATION_FRAME', 'Rendered sample must be inside [0,total_frames).')
    return seconds({'frame': index}, fps)


def shot_time(global_time, start_frame, duration_frames, fps, *, terminal=False):
    position = frame_position(global_time)
    require(type(start_frame) is int and type(duration_frames) is int and start_frame >= 0 and duration_frames > 0,
            'ANIMATION_SHOT', 'Invalid shot interval.')
    local = position - start_frame
    require(0 <= local < duration_frames or terminal and local == duration_frames,
            'ANIMATION_SHOT', 'Time is outside the shot half-open interval.')
    return local / rate(fps)
