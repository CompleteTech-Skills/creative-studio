"""Thin Camera3D binding to the shared LookDev color-management authority.

No white-balance formula, color schema or image filter is defined here.
"""
from camera3d_blender import apply_camera, optical_plan
from camera3d_math import _matrix


def validate_color_binding(optics, render, world_matrix, color_configuration, *,
                           motion_blur=False, shutter_frames=0.5):
    """Resolve shared hash-bound color settings and reject output disagreements.

    This host-side admission performs no Blender mutation. The shared color
    configuration remains the single exposure and white-balance authority.
    """
    from lookdev_color import validate_configuration
    color = validate_configuration(color_configuration)
    optical_plan(optics, render, exposure=color['exposure_stops'],
                 motion_blur=motion_blur, shutter_frames=shutter_frames)
    _matrix(world_matrix, rigid=True)
    expected = {'srgb-png': ('png', 'srgb'),
                'scene-linear-exr': ('exr', 'linear-srgb')}[color['output']]
    if (render['output'], render['color_space']) != expected:
        raise ValueError('camera3d render output/color_space disagrees with shared color configuration')
    return color


def apply_camera_with_color(scene, optics, render, *, world_matrix,
                            color_configuration, name='Camera3D', camera_object=None,
                            motion_blur=False, shutter_frames=0.5):
    """Apply one validated optical state and one shared native color transform.

    Returns (actual_camera_object, shared_color_receipt). Exposure is supplied
    once by the shared configuration, so the camera and color owners cannot
    choose conflicting values. Native backend availability is enforced by the
    respective owners, including the pinned physical Cycles camera capability.
    """
    from lookdev_color import apply_configuration
    color = validate_color_binding(optics, render, world_matrix, color_configuration,
                                   motion_blur=motion_blur, shutter_frames=shutter_frames)
    camera = apply_camera(scene, optics, render, world_matrix=world_matrix,
                          name=name, camera_object=camera_object,
                          exposure=color['exposure_stops'], motion_blur=motion_blur,
                          shutter_frames=shutter_frames)
    receipt = apply_configuration(scene, color)
    camera['hatch_color_sha256'] = color['sha256']
    return camera, receipt
