"""Exact scheduling and interpolation of completed solver states.

Output ranges are half open. A fractional final sample requires the next full
solver step as an interpolation bracket; no shortened terminal step is hidden.
"""
from fractions import Fraction
import math


def ratio(value):
    return Fraction(value['numerator'], value['denominator'])


def rational(value):
    value = Fraction(value)
    return dict(numerator=value.numerator, denominator=value.denominator)


def clock(world):
    fps = ratio(world['fps'])
    step = ratio(world['solver']['fixed_step']) / world['solver']['substeps']
    start, end = (world['frame_range'][key] for key in ('start', 'end'))
    terminal = Fraction(end - 1, 1) / fps
    count = math.ceil(terminal / step)
    per_frame = 1 / (fps * step)
    return dict(output_rate=rational(fps), requested_fixed_step=world['solver']['fixed_step'],
        effective_solver_step=rational(step), solver_steps=count,
        steps_per_output=rational(per_frame), sample_count=end-start,
        first_sample_time=rational(Fraction(start, 1)/fps),
        last_sample_time=rational(terminal), computed_terminal_time=rational(count*step),
        interval_policy='half-open output range; full solver steps bracket final sample',
        animation_time='output timestamp', cache_sample_rate=rational(1/step if world['solver']['backend']=='pybullet' else fps),
        cache_sampling='every completed solver step' if world['solver']['backend']=='pybullet' else 'native embedded cache and output samples; substep extraction unsupported',
        render_rate='independent consumer; seek computed cache states',
        interpolation='linear translation and shortest-arc quaternion slerp',
        sample_brackets=[dict(frame=frame,time=rational(Fraction(frame,1)/fps),
            lower=bracket(Fraction(frame,1)/fps,step)[0],
            upper=bracket(Fraction(frame,1)/fps,step)[1],
            alpha=rational(bracket(Fraction(frame,1)/fps,step)[2]),
            source='direct solved state' if (Fraction(frame,1)/fps/step).denominator==1 else 'interpolated completed solver states')
            for frame in range(start,end)])


def bracket(timestamp, step):
    index = timestamp // step
    alpha = timestamp / step - index
    return int(index), int(index + bool(alpha)), alpha


def interpolate(left, right, alpha):
    """Interpolate states already computed by a solver, never create dynamics."""
    a = float(alpha)
    position = [x+(y-x)*a for x,y in zip(left['translation'],right['translation'])]
    q, r = left['rotation'], right['rotation']
    dot = sum(x*y for x,y in zip(q,r))
    if dot < 0:
        r = [-x for x in r]; dot = -dot
    dot = min(1., max(-1., dot))
    if dot > .9995:
        rotation = [x+(y-x)*a for x,y in zip(q,r)]
    else:
        theta=math.acos(dot)
        rotation=[(math.sin((1-a)*theta)*x+math.sin(a*theta)*y)/math.sin(theta) for x,y in zip(q,r)]
    norm=math.sqrt(sum(x*x for x in rotation))
    return dict(id=left['id'],translation=position,rotation=[x/norm for x in rotation])
