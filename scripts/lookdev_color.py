"""Hash-bound Blender OCIO configuration; scene-linear shading is unchanged.

White balance is a view transform, never a rewrite of source lights/materials.
The pinned Blender backend performs its own OCIO transform exactly once.
"""
from contextlib import contextmanager
from copy import deepcopy
import math
from lookdev_contracts import content_hash
from rig_errors import require, finite_json, RigError


COLOR_HASH = 'hatch_color_sha256'
CAMERA_COLOR_OWNER = 'hatch_color_scene'


def camera_is_shared(scene, camera):
    if not hasattr(camera, 'users_scene'):
        return False
    if any(other != scene for other in camera.users_scene):
        return True
    # Blender also permits a scene to select a camera outside its collection.
    import bpy
    return any(other != scene and other.camera == camera for other in bpy.data.scenes)


def release_camera_color(scene, camera):
    """Release this scene's object tag, including tags saved before owner tracking."""
    if camera is None or COLOR_HASH not in camera:
        return
    owner = camera.get(CAMERA_COLOR_OWNER)
    if owner is not None and owner != scene:
        return
    if owner is None and camera_is_shared(scene, camera):
        # A legacy object tag has no owner identity. A matching digest cannot
        # distinguish a camera activation from a scene-only configuration.
        require(camera[COLOR_HASH] != scene.get(COLOR_HASH), 'LOOKDEV_COLOR_BACKEND',
                'Ambiguous legacy shared camera binding; use an exclusive camera.')
        return
    del camera[COLOR_HASH]
    if owner is not None:
        del camera[CAMERA_COLOR_OWNER]


def restore_property(owner, key, previous):
    if owner.get(key) != previous:
        if previous is None:
            del owner[key]
        else:
            owner[key] = previous
        require(owner.get(key) == previous, 'LOOKDEV_COLOR_BACKEND', f'Backend failed to restore {key}.')


def color_state(scene):
    """Snapshot native settings in the order needed to restore dependent enums."""
    view = scene.view_settings
    fields = [(scene.display_settings, ('display_device',)),
              (view, ('view_transform', 'look', 'exposure', 'gamma', 'use_curve_mapping',
                      'use_white_balance', 'white_balance_temperature', 'white_balance_tint')),
              (scene.render.image_settings, ('color_management', 'file_format', 'color_depth',
                                            'color_mode', 'exr_codec'))]
    if hasattr(view, 'use_hdr_view'):
        fields.append((view, ('use_hdr_view',)))
    return [(owner, name, getattr(owner, name)) for owner, names in fields for name in names]


def verify_color_state(state, *, allow_float32=False):
    for owner, name, expected in state:
        observed = getattr(owner, name)
        if allow_float32 and type(expected) in (int, float):
            matches = math.isclose(observed, expected, rel_tol=2e-6,
                                   abs_tol=min(1e-7, abs(expected) * 2e-6))
        else:
            matches = observed == expected
        require(matches, 'LOOKDEV_COLOR_BACKEND', f'Backend cannot represent {name}={expected}.')


def restore_color_state(state):
    for owner, name, original in state:
        if getattr(owner, name) != original:
            setattr(owner, name, original)
    verify_color_state(state)


def configuration(*, view='AgX', exposure=0., temperature_kelvin=6500., tint=10., output='srgb-png'):
    result={'schema_version':1,'working_space':'linear-srgb','view':view,'exposure_stops':exposure,
            'temperature_kelvin':temperature_kelvin,'tint':tint,'output':output}
    _validate_content(result)
    result['sha256']=content_hash(result)
    return validate_configuration(result)


def _validate_content(value):
    finite_json(value)
    require(type(value['schema_version']) is int and value['schema_version']==1 and value['working_space']=='linear-srgb',
            'LOOKDEV_COLOR','Unsupported color configuration.')
    require(value['view'] in ('Standard','AgX','Filmic'),'LOOKDEV_COLOR','Unsupported OCIO view; query backend availability.')
    for key,low,high in [('exposure_stops',-32,32),('temperature_kelvin',1800,100000),('tint',-500,500)]:
        number=value[key]
        require(type(number) in (int,float) and low<=number<=high and math.isfinite(number),
                'LOOKDEV_COLOR','Invalid '+key)
    require(value['output'] in ('srgb-png','scene-linear-exr'),'LOOKDEV_COLOR','Unsupported output encoding.')


def validate_configuration(value):
    require(isinstance(value,dict) and set(value)=={'schema_version','working_space','view','exposure_stops',
            'temperature_kelvin','tint','output','sha256'},'LOOKDEV_COLOR','Invalid color configuration fields.')
    _validate_content(value)
    require(value['sha256']==content_hash({k:v for k,v in value.items() if k!='sha256'}),
            'LOOKDEV_COLOR_HASH','Color configuration changed.')
    return deepcopy(value)


def apply_configuration(scene, value):
    """Apply shared native color settings and return their receipt."""
    with configured_color(scene, value) as receipt:
        return receipt


@contextmanager
def configured_color(scene, value):
    """Configure actual Blender OCIO and file encoding, rejecting unknown views.

    EXR remains scene-linear: display exposure/white balance/view metadata is
    retained in the scene but is not baked into the HDR payload by Blender.
    Restore the original scene settings if the native assignments or the
    caller's dependent setup raises.
    """
    value=validate_configuration(value)
    settings=scene.view_settings
    require(hasattr(settings,'use_white_balance') and hasattr(settings,'white_balance_temperature') and hasattr(settings,'white_balance_tint'),
            'LOOKDEV_COLOR_BACKEND','Backend lacks explicit view white balance; require pinned Blender4.5.')
    for name,requested in [('exposure',value['exposure_stops']),('white_balance_temperature',value['temperature_kelvin']),('white_balance_tint',value['tint'])]:
        prop=settings.bl_rna.properties[name]
        require(prop.hard_min<=requested<=prop.hard_max,'LOOKDEV_COLOR_BACKEND','Color setting exceeds actual backend bounds.')
    linear = value['output'] == 'scene-linear-exr'
    previous = color_state(scene)
    target = dict(display_device='sRGB', view_transform=value['view'], look='None',
                  exposure=value['exposure_stops'], gamma=1.0, use_curve_mapping=False,
                  use_white_balance=True, white_balance_temperature=value['temperature_kelvin'],
                  white_balance_tint=value['tint'], color_management='FOLLOW_SCENE',
                  file_format='OPEN_EXR' if linear else 'PNG', color_depth='32' if linear else '16',
                  color_mode='RGBA', use_hdr_view=False)
    if linear:
        target['exr_codec'] = 'ZIP'
    assignments = [(owner, name, target[name]) for owner, name, _ in previous if name in target]
    previous_hash = scene.get(COLOR_HASH)
    active_camera = scene.camera
    previous_binding = [] if active_camera is None else [
        (key, active_camera.get(key)) for key in (COLOR_HASH, CAMERA_COLOR_OWNER)]
    try:
        for owner, name, expected in assignments:
            try:
                setattr(owner, name, expected)
            except (TypeError, ValueError) as exc:
                if name in ('display_device', 'view_transform', 'look'):
                    raise RigError('LOOKDEV_COLOR_BACKEND', 'Requested OCIO view/look unavailable.') from exc
                raise
        # A later RNA assignment can silently change an earlier field.
        if active_camera is not None and active_camera.get(COLOR_HASH) != value['sha256']:
            release_camera_color(scene, active_camera)
        scene[COLOR_HASH] = value['sha256']
        verify_color_state(assignments, allow_float32=True)
        yield {'configuration_sha256': value['sha256'], 'view': settings.view_transform,
               'output_encoding': value['output'], 'display_transform_baked': not linear}
    except Exception:
        for key, original in previous_binding:
            restore_property(active_camera, key, original)
        restore_property(scene, COLOR_HASH, previous_hash)
        restore_color_state(previous)
        raise
