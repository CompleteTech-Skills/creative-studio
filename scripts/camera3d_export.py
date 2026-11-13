"""Camera-only glTF interchange with canonical state and explicit loss records.

The renderer, animation evaluator and native scene exporter remain owned by
their existing subsystems. This module exports one already evaluated camera.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import math
import shutil
import tempfile

from atomic_publish import _rename_no_replace
from camera3d_contracts import canonical_bytes
from camera3d_math import _matrix, field_of_view, optical_gate
from rig_errors import atomic_json, object_hash, sha256


def export_plan(optics, render, world_matrix, *, allow_loss=False, exposure=0.0,
                motion_blur=False, shutter_frames=0.5):
    """Return a camera-only glTF document and authoritative canonical sidecar.

    Core glTF cannot represent principal-point shift, physical DOF, exposure or
    motion blur. Active unsupported state fails unless loss is explicitly
    allowed. Physical authoring controls are always retained in the canonical
    snapshot; a glTF consumer is never claimed to preserve those controls.
    """
    if not isinstance(allow_loss, bool):
        raise ValueError('camera3d allow_loss must be boolean')
    # glTF's supported subset differs from Blender's. Validate canonical
    # documents without imposing Blender-only aperture/orthographic DOF limits;
    # unsupported active effects are handled explicitly below by the loss policy.
    width, height = optical_gate(optics, render)
    for value, low, high, label in ((exposure, -32, 32, 'exposure'),
                                   (shutter_frames, 0, 1, 'shutter_frames')):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not low <= value <= high):
            raise ValueError(f'camera3d {label} must be finite in [{low},{high}]')
    if not isinstance(motion_blur, bool):
        raise ValueError('camera3d motion_blur must be boolean')
    world = _matrix(world_matrix, rigid=True)
    projection = optics['projection']
    losses = []
    for enabled, code, description in (
        (projection['shift_x'] != 0 or projection['shift_y'] != 0,
         'principal-point-shift', 'Core glTF uses a centered projection.'),
        (optics['depth_of_field']['enabled'], 'physical-depth-of-field',
         'Core glTF does not store physical focus or aperture.'),
        (exposure != 0, 'exposure', 'Core glTF does not store camera exposure.'),
        (motion_blur, 'motion-blur', 'Core glTF does not store shutter or motion blur.'),
    ):
        if enabled:
            losses.append(dict(code=code, effect=description))
    if losses and not allow_loss:
        raise ValueError('camera3d glTF export requires explicit allow_loss for: ' +
                         ', '.join(item['code'] for item in losses))
    if projection['type'] == 'perspective':
        camera = dict(type='perspective', perspective=dict(
            yfov=field_of_view(optics, render)[1], aspectRatio=width / height,
            znear=projection['near_clip_m'], zfar=projection['far_clip_m']))
    else:
        camera = dict(type='orthographic', orthographic=dict(
            xmag=width * render['overscan'] / 2,
            ymag=height * render['overscan'] / 2,
            znear=projection['near_clip_m'], zfar=projection['far_clip_m']))
    # Camera3D and glTF share Y-up world coordinates and camera-local -Z view,
    # +Y up. The node stores a column-major matrix, without a second basis turn.
    document = dict(asset=dict(version='2.0', generator='Creative Studio Camera3D'),
                    cameras=[camera], nodes=[dict(name=optics['id'], camera=0,
                    matrix=world.T.reshape(-1).tolist())],
                    scenes=[dict(nodes=[0])], scene=0)
    canonical = dict(schema_version=1, kind='camera3d-export-snapshot',
                     optics=deepcopy(optics), render=deepcopy(render),
                     world_matrix=world.tolist(), exposure=float(exposure),
                     motion_blur=motion_blur, shutter_frames=float(shutter_frames))
    # Admission and hashing use the existing finite canonical JSON authority.
    canonical_bytes(canonical)
    report = dict(schema_version=1, kind='camera3d-gltf-loss-report',
                  scope='one evaluated camera; no scene geometry or animation',
                  exact_core_projection=not any(item['code'] == 'principal-point-shift' for item in losses),
                  active_effects_preserved=not losses, losses=losses,
                  canonical_sha256=object_hash(canonical),
                  authoring_controls='Physical sensor/lens, focus and shutter remain in canonical.json; core glTF stores only projection and pose.',
                  input_sha256=dict(optics=object_hash(optics), render=object_hash(render),
                                    world_matrix=object_hash(world.tolist())),
                  specification='https://registry.khronos.org/glTF/specs/2.0/glTF-2.0.html#cameras')
    return dict(gltf=document, canonical=canonical, loss_report=report)


def export_gltf(destination, optics, render, world_matrix, *, validate=True, **options):
    """Publish a new camera interchange directory after independent validation.

    The optional validator flag is only for explicitly unverified preparation.
    Returned receipts distinguish that state; existing destinations are never
    replaced, including racing empty directories.
    """
    if not isinstance(validate, bool):
        raise ValueError('camera3d validate must be boolean')
    plan = export_plan(optics, render, world_matrix, **options)
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('camera3d export destination must be new')
    if not destination.parent.is_dir():
        raise ValueError('camera3d export parent directory must exist')
    stage = Path(tempfile.mkdtemp(prefix='.camera-export-', dir=destination.parent))
    try:
        for name, value in (('camera.gltf', plan['gltf']), ('canonical.json', plan['canonical']),
                            ('losses.json', plan['loss_report'])):
            atomic_json(stage / name, value)
        validation = None
        if validate:
            from rig_interop import validate_gltf
            validation = validate_gltf(stage / 'camera.gltf')
            if not validation['ok']:
                raise ValueError('camera3d independent Khronos validation rejected export')
            atomic_json(stage / 'khronos-validation.json', validation)
        receipt = dict(schema_version=1, kind='camera3d-export-receipt',
                       independently_validated=validation is not None,
                       loss_report_sha256=sha256(stage / 'losses.json'),
                       files={path.name: sha256(path) for path in stage.iterdir()},
                       canonical_sha256=plan['loss_report']['canonical_sha256'])
        atomic_json(stage / 'receipt.json', receipt)
        _rename_no_replace(stage, destination)
        return receipt
    finally:
        if stage.exists():
            shutil.rmtree(stage)
