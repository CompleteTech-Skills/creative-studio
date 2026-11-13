"""Explicit request binding for the canonical Blender output/pass adapter.

Clients bind before queue submission; consumers revalidate before cache reuse.
Unmarked combined-only requests retain the legacy Blender execution path.
"""
from copy import deepcopy
from pathlib import Path
import hashlib

from rig_errors import object_hash
from render3d_contracts import require, validate
from render3d_quality import output_plan

ADAPTER_SOURCES = ('render3d_quality_binding.py', 'render3d_quality.py',
                   'atomic_publish.py', 'render3d_contracts.py', 'rig_contracts.py', 'rig_errors.py')


def adapter_hash():
    root = Path(__file__).parent
    return object_hash({name: hashlib.sha256((root/name).read_bytes()).hexdigest()
                        for name in ADAPTER_SOURCES})


def quality_binding(settings):
    return dict(plan=output_plan(settings), adapter_sha256=adapter_hash())


def bind_quality_request(request):
    """Return a new canonical request; never alter an existing queue identity."""
    validate(request, 'RenderRequest')
    result = deepcopy(request)
    result['settings']['quality_sha256'] = object_hash(quality_binding(result['settings']))
    return validate(result, 'RenderRequest')


def validate_quality_settings(settings):
    """Validate before submission/reuse/execution; return None for legacy output."""
    validate(settings, 'RenderSettings')
    marker = settings.get('quality_sha256')
    if marker is None:
        require(settings['output']['passes'] == ['combined'], 'unsupported-feature',
                'Auxiliary passes require bind_quality_request before submission.')
        return None
    binding = quality_binding(settings)
    require(marker == object_hash(binding), 'stale-cache',
            'Quality adapter code or plan changed; bind a new request before submission.')
    return binding
