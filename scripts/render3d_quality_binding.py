"""Explicit request binding for the canonical Blender output/pass adapter.

Clients bind before queue submission; consumers revalidate before cache reuse.
Unmarked combined-only requests retain the legacy Blender execution path.
"""
from copy import deepcopy
from pathlib import Path
import hashlib
import os

from rig_errors import object_hash
from render3d_contracts import Render3DError, require, validate
from render3d_quality import output_plan

ADAPTER_SOURCES = ('render3d_quality_binding.py', 'render3d_quality.py',
                   'atomic_publish.py', 'render3d_contracts.py', 'rig_contracts.py', 'rig_errors.py',
                   'cinematics3d_color.py', 'render3d_color_context.py', 'render3d_quality_jobs.py')


def adapter_hash():
    root = Path(__file__).parent
    return object_hash({name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                        for name in ADAPTER_SOURCES})


def color_profile(executable=None):
    from rig_blender import find_blender
    from cinematics3d_color import capture_config
    require(not os.environ.get('OCIO') and not os.environ.get('BLENDER_OCIO'), 'unsupported-feature',
            'Bound output requires the captured builtin Blender profile; custom OCIO is unsupported.')
    try:
        binary=Path(find_blender(executable)).resolve()
        root=binary.parent/'4.5'/'datafiles'/'colormanagement'
        inventory=capture_config(root)
    except (OSError, ValueError) as exc:
        raise Render3DError('unsupported-feature','Unable to admit the builtin Blender color profile: '+str(exc)) from exc
    return root,inventory


def quality_binding(settings, *, executable=None):
    _,inventory=color_profile(executable)
    return dict(plan=output_plan(settings), adapter_sha256=adapter_hash(), color_config=inventory)


def bind_quality_request(request, *, executable=None):
    """Return a new canonical request; never alter an existing queue identity."""
    validate(request, 'RenderRequest')
    result = deepcopy(request)
    result['settings']['quality_sha256'] = object_hash(quality_binding(result['settings'], executable=executable))
    return validate(result, 'RenderRequest')


def validate_quality_settings(settings, *, executable=None):
    """Validate before submission/reuse/execution; return None for legacy output."""
    validate(settings, 'RenderSettings')
    marker = settings.get('quality_sha256')
    if marker is None:
        require(settings['output']['passes'] == ['combined'], 'unsupported-feature',
                'Auxiliary passes require bind_quality_request before submission.')
        return None
    binding = quality_binding(settings, executable=executable)
    require(marker == object_hash(binding), 'stale-cache',
            'Quality adapter code or plan changed; bind a new request before submission.')
    return binding
