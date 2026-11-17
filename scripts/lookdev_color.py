"""Hash-bound Blender OCIO configuration; scene-linear shading is unchanged.

White balance is a view transform, never a rewrite of source lights/materials.
The pinned Blender backend performs its own OCIO transform exactly once.
"""
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
    """Configure actual Blender OCIO and file encoding, rejecting unknown views.

    EXR remains scene-linear: display exposure/white balance/view metadata is
    retained in the scene but is not baked into the HDR payload by Blender.
    No compositor nodes are introduced, avoiding a second transform.
    """
    value=validate_configuration(value)
    settings=scene.view_settings
    require(hasattr(settings,'use_white_balance') and hasattr(settings,'white_balance_temperature') and hasattr(settings,'white_balance_tint'),
            'LOOKDEV_COLOR_BACKEND','Backend lacks explicit view white balance; require pinned Blender4.5.')
    for name,requested in [('exposure',value['exposure_stops']),('white_balance_temperature',value['temperature_kelvin']),('white_balance_tint',value['tint'])]:
        prop=settings.bl_rna.properties[name]
        require(prop.hard_min<=requested<=prop.hard_max,'LOOKDEV_COLOR_BACKEND','Color setting exceeds actual backend bounds.')
    try:
        settings.view_transform=value['view']
        scene.display_settings.display_device='sRGB'
        settings.look='None'
    except (TypeError,ValueError) as exc:
        raise RigError('LOOKDEV_COLOR_BACKEND','Requested OCIO view/look unavailable.') from exc
    settings.exposure=value['exposure_stops'];settings.gamma=1.
    settings.use_curve_mapping=False
    if hasattr(settings,'use_hdr_view'):settings.use_hdr_view=False
    settings.use_white_balance=True
    settings.white_balance_temperature=value['temperature_kelvin'];settings.white_balance_tint=value['tint']
    image=scene.render.image_settings
    image.color_management='FOLLOW_SCENE'
    if value['output']=='scene-linear-exr':
        image.file_format='OPEN_EXR';image.color_depth='32';image.exr_codec='ZIP'
    else:
        image.file_format='PNG';image.color_depth='16'
    # Blender filters the color-mode enum by the current file format.
    image.color_mode='RGBA'
    scene['hatch_color_sha256']=value['sha256']
    return {'configuration_sha256':value['sha256'],'view':settings.view_transform,
            'output_encoding':value['output'],'display_transform_baked':value['output']=='srgb-png'}
