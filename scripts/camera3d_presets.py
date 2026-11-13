"""Deterministic native Camera3D presets and explicit spatial storyboard plans."""
from copy import deepcopy
import math

from camera3d_contracts import validate, canonical_bytes
from camera3d_math import look_at


CAMERA_PRESETS = {
    'standard-50mm': dict(focal_length_mm=50),
    'wide-24mm': dict(focal_length_mm=24),
    'cinematic-35mm': dict(focal_length_mm=35),
    'portrait-85mm': dict(focal_length_mm=85),
    'telephoto-135mm': dict(focal_length_mm=135),
    'shallow-depth-portrait': dict(focal_length_mm=85, f_stop=1.4, dof=True),
    'deep-focus-environment': dict(focal_length_mm=24, f_stop=16, dof=True),
    'orthographic-technical': dict(orthographic_height_m=5),
    'isometric': dict(orthographic_height_m=5, position=[4, 4, 4]),
    'top-down': dict(orthographic_height_m=5, position=[0, 5, 0], up=[0, 0, -1]),
}
MOVEMENT_PRESETS = {
    'slow-dolly-in': ('dolly', dict(distance_m=2)),
    'slow-dolly-out': ('dolly', dict(distance_m=-2)),
    'horizontal-tracking': ('truck', dict(distance_m=2)),
    'vertical-pedestal': ('pedestal', dict(distance_m=2)),
    'crane-reveal': ('crane', dict(delta_m=[0, 3, 2])),
    'half-orbit': ('orbit', dict(angle_rad=math.pi)),
    'full-orbit': ('orbit', dict(angle_rad=2 * math.pi)),
    'character-follow': ('follow', dict(offset_m=[0, 2, 5])),
    'over-the-shoulder-tracking': ('follow', dict(offset_m=[1, 1.6, 2])),
    'handheld': ('shake', dict(amplitude_m=.02, frequency_hz=2, seed=0)),
    'static-tripod': ('static', {}),
    'dolly-zoom': ('dolly-zoom', dict(distance_m=2)),
}
SHOT_PRESETS = {
    'establishing-shot': ('wide-24mm', 'static-tripod', .25),
    'two-shot': ('cinematic-35mm', 'static-tripod', .6),
    'close-up': ('portrait-85mm', 'static-tripod', .9),
    'medium-shot': ('standard-50mm', 'static-tripod', .65),
    'full-body-shot': ('standard-50mm', 'static-tripod', .8),
    'over-the-shoulder': ('standard-50mm', 'over-the-shoulder-tracking', .65),
    'hero-shot': ('wide-24mm', 'slow-dolly-in', .85),
    'product-showcase': ('portrait-85mm', 'half-orbit', .8),
    'character-entrance': ('cinematic-35mm', 'static-tripod', .7),
    'character-exit': ('cinematic-35mm', 'static-tripod', .7),
    'action-tracking': ('wide-24mm', 'character-follow', .6),
    'cinematic-reveal': ('cinematic-35mm', 'crane-reveal', .5),
}


def camera_preset(name, *, version='1.0.0', identifier=None, focus_distance_m=5):
    """Return a real optics document and a camera-local transform suggestion."""
    if name not in CAMERA_PRESETS:
        raise ValueError('camera3d unknown camera preset')
    source = CAMERA_PRESETS[name]
    projection = dict(type='perspective', focal_length_mm=source.get('focal_length_mm', 50),
                      sensor_width_mm=36, sensor_height_mm=24, sensor_fit='horizontal',
                      near_clip_m=.1, far_clip_m=1000, shift_x=0, shift_y=0)
    if 'orthographic_height_m' in source:
        projection = dict(type='orthographic', orthographic_height_m=source['orthographic_height_m'],
                          near_clip_m=.1, far_clip_m=1000, shift_x=0, shift_y=0)
    optics = dict(schema_version=1, kind='camera3d-optics', id=identifier or name,
                  version=version, projection=projection,
                  depth_of_field=dict(enabled=source.get('dof', False),
                      focus_distance_m=focus_distance_m, f_stop=source.get('f_stop', 2.8),
                      aperture_blades=7, aperture_rotation_rad=0, anamorphic_ratio=1))
    validate(optics, 'optics')
    position = source.get('position', [0, 0, 5])
    transform = dict(translation=list(position), rotation=look_at(position, [0, 0, 0], source.get('up', [0, 1, 0])).tolist(), scale=[1, 1, 1])
    return dict(optics=optics, transform=transform, preset=name, preset_version='1.0.0')


def movement_preset(name, *, overrides=None):
    if name not in MOVEMENT_PRESETS:
        raise ValueError('camera3d unknown movement preset')
    motion, defaults = MOVEMENT_PRESETS[name]
    parameters = deepcopy(defaults)
    if overrides is not None:
        if not isinstance(overrides, dict) or set(overrides) - set(defaults):
            raise ValueError('camera3d unsupported movement preset override')
        parameters.update(deepcopy(overrides))
    canonical_bytes(parameters)
    for key, value in parameters.items():
        if key in ('delta_m', 'offset_m'):
            from rig_math import vector
            vector(value)
        elif key == 'seed':
            if type(value) is not int or not 0 <= value < 2**32:
                raise ValueError('camera3d seed must be a bounded integer')
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e6:
            raise ValueError('camera3d preset parameters must be bounded finite numbers')
        elif key in ('amplitude_m', 'frequency_hz') and value < 0:
            raise ValueError('camera3d amplitude and frequency cannot be negative')
    return dict(preset=name, preset_version='1.0.0', motion=motion, parameters=parameters)


def shot_preset(name):
    if name not in SHOT_PRESETS:
        raise ValueError('camera3d unknown shot preset')
    camera, movement, fill = SHOT_PRESETS[name]
    return dict(preset=name, preset_version='1.0.0', camera=camera_preset(camera),
                movement=movement_preset(movement), subject_frame_fraction=fill)


def duplicate_preset(preset, identifier, *, version='1.0.0', overrides=None):
    """Duplicate a native camera preset, retaining its exact source camera ref."""
    validate(preset, 'preset')
    result = deepcopy(preset)
    if identifier == preset['id']:
        raise ValueError('camera3d preset duplication requires a new stable ID')
    result.update(id=identifier, version=version)
    if overrides is not None:
        result['overrides'] = deepcopy(overrides)
    validate(result, 'preset')
    return result


def import_preset(path):
    from asset_library import read_source_bytes
    from camera3d_contracts import MAX_BYTES
    from rig_errors import decode_json
    value = decode_json(read_source_bytes(path, MAX_BYTES))
    validate(value, 'preset')
    return value


def native_camera_preset(name, camera_reference, *, identifier=None, optics_path=None):
    """Build a standard camera3d-preset and the exact optics it references.

    Callers retain/export both returned documents. The source camera reference
    stays exact; the preset supplies independent, versioned optical overrides.
    """
    import hashlib
    result = camera_preset(name, identifier=identifier or name)
    optics = result['optics']
    reference = dict(id=optics['id'], version=optics['version'], kind=optics['kind'],
                     path=optics_path or optics['id'] + '.json',
                     sha256=hashlib.sha256(canonical_bytes(optics)).hexdigest())
    preset = dict(schema_version=1, kind='camera3d-preset', id=identifier or name, version='1.0.0',
                  camera=deepcopy(camera_reference), overrides=dict(transform=result['transform'], optics=reference),
                  metadata=dict(name=name, description='Versioned native optical and transform preset',
                  provenance=dict(origin='authored', fixture=False,
                                  attribution='Creative Studio native preset', license='CC0-1.0', inputs=[])))
    validate(preset, 'preset')
    return dict(preset=preset, optics=optics)


def plan_storyboard_panel(panel, *, subject_bounds, aspect_ratio=16/9):
    """Map explicit world-space subject bounds to an inspectable camera plan.

    An image alone is never treated as measured geometry. Inputs retain the
    source panel verbatim; guessed side, lens and composition are disclosed.
    Bounds must be supplied by the scene owner, not reconstructed from pixels.
    """
    required = {'id', 'subject_ids', 'shot_preset', 'duration_frames', 'notes'}
    optional = {'source_image', 'camera_side', 'lens_mm', 'movement_preset',
                'scene_id', 'shot_id', 'narrative_purpose', 'shot_size', 'camera_angle',
                'composition', 'camera_target', 'transition', 'framing_reference'}
    if not isinstance(panel, dict) or required - panel.keys() or panel.keys() - required - optional:
        raise ValueError('camera3d storyboard panel has missing or unknown fields')
    if (not isinstance(panel['id'], str) or not panel['id'] or len(panel['id']) > 64
            or not isinstance(panel['notes'], str) or len(panel['notes']) > 4096
            or type(panel['duration_frames']) is not int or not 1 <= panel['duration_frames'] <= 10_000_000):
        raise ValueError('camera3d invalid storyboard identity, notes or exact duration')
    subjects = panel['subject_ids']
    if (not isinstance(subjects, list) or not 1 <= len(subjects) <= 128
            or any(not isinstance(value, str) or value not in subject_bounds for value in subjects)
            or len(set(subjects)) != len(subjects)):
        raise ValueError('camera3d storyboard requires unique available world-space subjects')
    for key in ('source_image', 'scene_id', 'shot_id', 'narrative_purpose', 'framing_reference'):
        if key in panel and (not isinstance(panel[key], str) or not panel[key] or len(panel[key]) > 4096):
            raise ValueError('camera3d storyboard text metadata must be bounded nonempty text')
    if 'camera_target' in panel and panel['camera_target'] not in subjects:
        raise ValueError('camera3d storyboard target must be a supplied subject')
    if panel.get('composition', 'center') not in ('center', 'third-left', 'third-right'):
        raise ValueError('camera3d unsupported storyboard composition')
    if panel.get('camera_angle', 'eye-level') not in ('eye-level', 'high', 'low', 'top-down'):
        raise ValueError('camera3d unsupported storyboard camera angle')
    if panel.get('shot_size', 'preset') not in ('preset', 'close-up', 'medium', 'full', 'wide'):
        raise ValueError('camera3d unsupported storyboard shot size')
    if 'transition' in panel:
        validate(panel['transition'], 'transition')
    if isinstance(aspect_ratio, bool) or not isinstance(aspect_ratio, (int, float)) or not math.isfinite(aspect_ratio) or not .1 <= aspect_ratio <= 10:
        raise ValueError('camera3d storyboard aspect ratio is out of bounds')
    import numpy as np
    from rig_math import vector, unit
    minima, maxima = [], []
    for subject in subjects:
        bounds = subject_bounds[subject]
        if not isinstance(bounds, dict) or set(bounds) != {'minimum', 'maximum'}:
            raise ValueError('camera3d subject bounds require minimum and maximum')
        minimum, maximum = vector(bounds['minimum']), vector(bounds['maximum'])
        if np.any(minimum > maximum):
            raise ValueError('camera3d subject bounds are inverted')
        minima.append(minimum); maxima.append(maximum)
    minimum = np.min(minima, axis=0); maximum = np.max(maxima, axis=0)
    center = (minimum + maximum) / 2
    if 'camera_target' in panel:
        target_bounds = subject_bounds[panel['camera_target']]
        center = (vector(target_bounds['minimum']) + vector(target_bounds['maximum'])) / 2
    extent = np.maximum(maximum - minimum, .001)
    selected = shot_preset(panel['shot_preset'])
    optics = selected['camera']['optics']
    uncertain = ['World bounds do not establish occlusion, acting quality or artistic approval.']
    if 'camera_side' not in panel:
        angle = panel.get('camera_angle', 'eye-level')
        uncertain.append(f'Camera side derives from the {angle} angle preset; the storyboard image does not establish depth.')
    side = unit(panel.get('camera_side', {'eye-level':[0,0,1], 'high':[0,1,1],
                    'low':[0,-.5,1], 'top-down':[0,1,0]}[panel.get('camera_angle','eye-level')]))
    if abs(float(np.dot(side, [0, 1, 0]))) > .999:
        up = [0, 0, -1]
    else:
        up = [0, 1, 0]
    if 'lens_mm' in panel:
        if optics['projection']['type'] != 'perspective':
            raise ValueError('camera3d lens preference cannot modify orthographic framing')
        optics['projection']['focal_length_mm'] = panel['lens_mm']
        validate(optics, 'optics')
    else:
        uncertain.append('Lens derives from the selected versioned preset and remains editable.')
    fill = selected['subject_frame_fraction']
    fill = {'preset':fill, 'close-up':.9, 'medium':.65, 'full':.8, 'wide':.25}[panel.get('shot_size','preset')]
    shift = {'center':0, 'third-left':-1/6, 'third-right':1/6}[panel.get('composition','center')]
    optics['projection']['shift_x'] = shift
    # A composed target has less room on the nearer edge; preserve full bounds.
    fill *= 1 - 2 * abs(shift)
    if optics['projection']['type'] == 'perspective':
        focal = optics['projection']['focal_length_mm']
        half_height = 18 / (focal * aspect_ratio)
        # Bounding sphere guarantees coverage for arbitrary authored camera side.
        radius = max(float(np.linalg.norm(corner-center)) for corner in
                     (np.array([x,y,z]) for x in (minimum[0],maximum[0])
                      for y in (minimum[1],maximum[1]) for z in (minimum[2],maximum[2])))
        distance = radius / (math.sin(math.atan(min(18 / focal, half_height))) * fill)
    else:
        optics['projection']['orthographic_height_m'] = float(np.linalg.norm(extent) / fill)
        distance = float(np.linalg.norm(extent) * 2)
    position = center + side * max(distance, .1)
    rotation = look_at(position, center, up)
    transform = dict(translation=position.tolist(), rotation=rotation.tolist(), scale=[1, 1, 1])
    validate(dict(schema_version=1, kind='camera3d-transform', id='panel-transform', version='1.0.0',
                  local=transform, parent=None), 'transform')
    optics['depth_of_field']['focus_distance_m'] = float(np.linalg.norm(position - center))
    validate(optics, 'optics')
    movement = movement_preset(panel.get('movement_preset', selected['movement']['preset']))
    return dict(source_panel=deepcopy(panel), subject_bounds=deepcopy(subject_bounds),
                transform=transform,
                optics=optics, movement=movement, target_m=center.tolist(),
                duration_frames=panel['duration_frames'], uncertainty=uncertain,
                requires_render_review=True)
