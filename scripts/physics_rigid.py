"""Bounded physical controls shared by admission and native source compilation.

This module never imports or runs a solver. Native Scene3D owns authored tracks;
rigid-body motion is an explicit animation authority, never a dynamic trajectory.
"""
from copy import deepcopy
import math
from fractions import Fraction


def volume(shape):
    if shape['type'] == 'sphere':
        return 4 * math.pi * shape['radius']**3 / 3
    if shape['type'] == 'box':
        return math.prod(shape['size'])
    raise ValueError('Density mass currently requires a primitive sphere or box.')


def density_mass(body, density):
    """Return an explicit mass/inertia assignment; never mutate caller data."""
    if type(density) not in (int, float) or not math.isfinite(density) or not 0 < density <= 1e12:
        raise ValueError('Density must be finite and positive.')
    if body['mode'] != 'dynamic' or len(body['colliders']) != 1:
        raise ValueError('Density assignment needs one dynamic primitive.')
    result = deepcopy(body)
    shape = body['colliders'][0]['shape']
    mass = density * volume(shape)
    if not math.isfinite(mass) or not .001 <= mass <= 1e6:
        raise ValueError('Derived mass exceeds native admission.')
    if shape['type'] == 'sphere':
        diagonal = [.4 * mass * shape['radius']**2] * 3
    else:
        x,y,z = shape['size']
        diagonal = [mass*(y*y+z*z)/12, mass*(x*x+z*z)/12, mass*(x*x+y*y)/12]
    result['mass'].update(kilograms=mass, inertia=[[diagonal[i] if i == j else 0 for j in range(3)] for i in range(3)])
    return result


def sleep_settings(body):
    return body.get('sleep', dict(enabled=False, initially_sleeping=False,
        linear_threshold=.4, angular_threshold=.5))


def sample_translation(motion, time):
    """Evaluate the bounded native linear/step translation track on exact time."""
    coordinate = time * Fraction(motion['fps']['numerator'], motion['fps']['denominator'])
    keys = motion['keys']
    if coordinate <= keys[0]['frame']:
        return list(keys[0]['value'])
    for left,right in zip(keys,keys[1:]):
        if coordinate <= right['frame']:
            if motion['interpolation'] == 'step':
                return list(right['value'] if coordinate == right['frame'] else left['value'])
            alpha = float((coordinate-left['frame'])/(right['frame']-left['frame']))
            return [a+(b-a)*alpha for a,b in zip(left['value'],right['value'])]
    return list(keys[-1]['value'])


def compile_motion(body, clip, world, fail):
    """Admit the actual Scene3D animation document, not untyped key metadata."""
    fail(clip['kind'] == 'scene3d-animation' and clip['id'] == body['animation'], 'Kinematic animation identity differs.')
    fail(not clip['bindings'] and not clip['dependencies'] and len(clip['tracks']) == 1,
         'This native kinematic profile needs one independent translation track.')
    track = clip['tracks'][0]
    step=Fraction(world['solver']['fixed_step']['numerator'],world['solver']['fixed_step']['denominator'])/world['solver']['substeps']
    solver_steps=math.ceil(Fraction((world['frame_range']['end']-1)*world['fps']['denominator'],world['fps']['numerator'])/step)
    fail(len(track['keys'])*solver_steps <= 5_000_000, 'Kinematic track evaluation exceeds bounded operation budget.')
    fail(track['target'] == body['id'] and track['property'] == 'translation' and
         track['interpolation'] in ('linear','step'), 'Kinematic track must own this exact body translation.')
    duration = Fraction(clip['duration_frames']*clip['fps']['denominator'], clip['fps']['numerator'])
    last = Fraction((world['frame_range']['end']-1)*world['fps']['denominator'], world['fps']['numerator'])
    fail(duration >= last and track['keys'][0]['frame'] == 0 and
         track['keys'][-1]['frame'] >= last*Fraction(clip['fps']['numerator'],clip['fps']['denominator']),
         'Kinematic native track must cover the complete requested time range.')
    fail(track['keys'][0]['value'] == body['transform']['translation'], 'Kinematic initial pose differs from native scene.')
    fail(all(max(map(abs,key['value'])) <= 10000 for key in track['keys']), 'Kinematic motion exceeds world bounds.')
    return dict(fps=deepcopy(clip['fps']), keys=deepcopy(track['keys']), interpolation=track['interpolation'])
