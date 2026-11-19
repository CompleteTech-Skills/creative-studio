"""Hash-bound Blender OCIO configuration; scene-linear shading is unchanged.

White balance is a view transform, never a rewrite of source lights/materials.
The pinned Blender backend performs its own OCIO transform exactly once.
"""
from contextlib import contextmanager
from copy import deepcopy
import math
from lookdev_contracts import content_hash
from rig_errors import require, finite_json, RigError


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
    display = scene.display_settings
    image = scene.render.image_settings
    linear = value['output'] == 'scene-linear-exr'
    # Parent enums must precede their dependent fields during apply and rollback.
    assignments = [
        (display, 'display_device', 'sRGB'),
        (settings, 'view_transform', value['view']),
        (settings, 'look', 'None'),
        (settings, 'exposure', value['exposure_stops']),
        (settings, 'gamma', 1.0),
        (settings, 'use_curve_mapping', False),
        (settings, 'use_white_balance', True),
        (settings, 'white_balance_temperature', value['temperature_kelvin']),
        (settings, 'white_balance_tint', value['tint']),
        (image, 'color_management', 'FOLLOW_SCENE'),
        (image, 'file_format', 'OPEN_EXR' if linear else 'PNG'),
        (image, 'color_depth', '32' if linear else '16'),
        (image, 'color_mode', 'RGBA'),
    ]
    if hasattr(settings, 'use_hdr_view'):
        assignments.append((settings, 'use_hdr_view', False))
    previous = [(owner, name, getattr(owner, name))
                for owner, name, _ in assignments]
    previous.append((image, 'exr_codec', image.exr_codec))
    if linear:
        assignments.append((image, 'exr_codec', 'ZIP'))
    hash_property = 'hatch_color_sha256'
    previous_hash = scene.get(hash_property)
    active_camera = scene.camera
    previous_camera_hash = active_camera.get(hash_property) if active_camera is not None else None
    try:
        for owner, name, expected in assignments:
            try:
                setattr(owner, name, expected)
            except (TypeError, ValueError) as exc:
                if name in ('display_device', 'view_transform', 'look'):
                    raise RigError('LOOKDEV_COLOR_BACKEND', 'Requested OCIO view/look unavailable.') from exc
                raise
        # A later RNA assignment can silently change an earlier field.
        for owner, name, expected in assignments:
            observed = getattr(owner, name)
            if type(expected) in (int, float):
                matches = math.isclose(observed, expected, rel_tol=2e-6,
                                       abs_tol=min(1e-7, abs(expected) * 2e-6))
            else:
                matches = observed == expected
            require(matches, 'LOOKDEV_COLOR_BACKEND', f'Backend cannot represent {name}={expected}.')
        if previous_camera_hash is not None and previous_camera_hash != value['sha256']:
            del active_camera[hash_property]
        scene[hash_property] = value['sha256']
        yield {'configuration_sha256': value['sha256'], 'view': settings.view_transform,
               'output_encoding': value['output'], 'display_transform_baked': not linear}
    except Exception:
        for owner, name, original in previous:
            if getattr(owner, name) != original:
                setattr(owner, name, original)
        if previous_camera_hash is not None and active_camera.get(hash_property) != previous_camera_hash:
            active_camera[hash_property] = previous_camera_hash
        if scene.get(hash_property) != previous_hash:
            if previous_hash is None:
                del scene[hash_property]
            else:
                scene[hash_property] = previous_hash
        raise
