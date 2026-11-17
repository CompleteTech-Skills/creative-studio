"""Renderer-owned observations of a pinned OCIO profile and actual scene state.

This module declares no independent color registry or grading transform.
"""
from pathlib import Path
import os

from render3d_contracts import require


def color_context(scene, profile_root, inventory, plan):
    from cinematics3d_color import verify_config_bytes
    root = Path(profile_root).absolute()
    expected = root/inventory['config_path']
    require(os.environ.get('OCIO') == str(expected), 'verification-failed',
            'Worker did not start with the captured OCIO config pinned explicitly.')
    closure = verify_config_bytes(root, inventory)
    image = scene.render.image_settings
    management = getattr(image, 'color_management', None)
    require(management == 'FOLLOW_SCENE', 'unsupported-feature',
            'Bound output does not support an image color-management override.')
    view = scene.view_settings
    return dict(kind='render3d-color-context', schema_version=1,
                config_source='builtin-pinned', config_path=inventory['config_path'],
                config_sha256=inventory['config_sha256'], closure_sha256=closure,
                declared_roles=inventory['declared_roles'],
                scene_linear_role=inventory['declared_roles']['scene_linear'],
                numeric_primaries=None, numeric_white_point=None,
                chromaticity_status='not-established-by-renderer-receipt',
                display=scene.display_settings.display_device,
                view=view.view_transform, look=view.look,
                exposure=view.exposure, gamma=view.gamma,
                image_color_management=management,
                file_format=image.file_format, color_mode=image.color_mode,
                color_depth=image.color_depth,
                film_transparent=scene.render.film_transparent,
                requested_alpha=plan['alpha'], alpha_association='unverified',
                passes=[dict(layer=layer.name, depth=layer.use_pass_z,
                             normal=layer.use_pass_normal,
                             albedo=layer.use_pass_diffuse_color,
                             alpha_threshold=layer.pass_alpha_threshold)
                        for layer in scene.view_layers])
