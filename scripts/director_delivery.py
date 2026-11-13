"""Evidence checks for the Director's real local 3D production slice.

Native scene, rig, renderer, and silent-video owners retain their formats and
execution. A Director receipt reports what was measured; it is not visual approval.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from rig_errors import RigError, object_hash, require, sha256

SAMPLE_FRAMES = (0, 29, 59, 60, 89, 119)


def _require(condition, message):
    require(condition, 'DIRECTOR_DELIVERY', message, stage='director-delivery')


def measure_motion(rigs, scene):
    """Sample actual native skinning; image differences alone cannot prove motion."""
    import numpy as np
    from director_scene import evaluate_frame
    _require(set(rigs) == {'aster', 'moss'}, 'Exactly the two persistent robot rigs are required.')
    samples = {frame: evaluate_frame(rigs, scene, frame) for frame in SAMPLE_FRAMES}
    measured = {}
    for actor in ('aster', 'moss'):
        initial = samples[0]['actors'][actor]['vertices']
        _require(initial, 'Robot has no decoded vertex data: ' + actor)
        maximum = 0.
        for frame in SAMPLE_FRAMES[1:]:
            actual = samples[frame]['actors'][actor]['vertices']
            _require(set(initial) == set(actual), 'Mesh identity changed while sampling: ' + actor)
            for mesh in initial:
                before, after = np.asarray(initial[mesh]), np.asarray(actual[mesh])
                _require(before.ndim == 2 and before.shape[1] == 3 and before.shape == after.shape and len(before),
                         'Invalid decoded geometry while sampling: ' + actor)
                _require(np.isfinite(before).all() and np.isfinite(after).all(), 'Nonfinite evaluated geometry.')
                maximum = max(maximum, float(np.linalg.norm(after - before, axis=1).max()))
        _require(maximum > .02, 'Required independent robot articulation is absent: ' + actor)
        measured[actor] = {'maximum_vertex_displacement_m': maximum,
                           'meshes': sorted(initial), 'vertices': sum(len(v) for v in initial.values())}
    key = [np.asarray(samples[frame]['key_matrix']) for frame in SAMPLE_FRAMES]
    _require(all(matrix.shape == (4, 4) and np.isfinite(matrix).all() for matrix in key), 'Invalid evaluated prop transform.')
    displacement = max(float(np.linalg.norm(matrix[:3, 3] - key[0][:3, 3])) for matrix in key[1:])
    _require(displacement > .02, 'Required moving prop is absent.')
    _require(samples[0]['key_owner'] is None and samples[119]['key_owner'] == 'aster', 'Prop ownership boundary is absent.')
    _require(samples[0]['camera'] == 'wide' and samples[119]['camera'] == 'investigation', 'Two-shot camera sequence is absent.')
    # Repeat a nonsequential order using the same native data; no hidden stepping state.
    for frame in reversed(SAMPLE_FRAMES):
        _require(evaluate_frame(rigs, scene, frame) == samples[frame], 'Native frame evaluation is order-dependent.')
    return {'kind': 'director-measured-motion', 'schema_version': 1, 'sample_frames': list(SAMPLE_FRAMES),
            'actors': measured, 'key_displacement_m': displacement,
            'sample_sha256': {str(frame): object_hash(sample) for frame, sample in samples.items()},
            'visual_review': 'not-performed'}


def _performance_input_hashes(root):
    from rig_errors import read_json, safe_path
    paths = {'scene.json'}
    for actor in ('aster', 'moss'):
        prefix = 'assets/rig-' + actor + '/'
        paths.update({prefix + 'rig.json', prefix + 'identity.json'})
        manifest = read_json(safe_path(root, prefix + 'rig.json', exists=True))
        pending = [manifest]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if isinstance(value.get('path'), str) and isinstance(value.get('sha256'), str):
                    paths.add(prefix + value['path'])
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
    return {relative: sha256(safe_path(root, relative, exists=True)) for relative in sorted(paths)}


def verify_shot_clips(rigs):
    """Bind rendered shot animation to the measured full performance clock."""
    import numpy as np
    from rig_animation import evaluate_animation
    from rig_skeleton import evaluate_fk
    for actor, rig in rigs.items():
        for suffix, offset in [('establish', 0), ('discovery', 60)]:
            for local in range(61):
                full = evaluate_animation(rig, actor+'-investigation', (offset+local)/24,
                                          contacts=False, constraints=False)
                shot = evaluate_animation(rig, actor+'-'+suffix, local/24,
                                          contacts=False, constraints=False)
                expected = evaluate_fk(rig.skeleton, full['pose'])
                observed = evaluate_fk(rig.skeleton, shot['pose'])
                _require(set(expected) == set(observed) and all(np.allclose(expected[bone], observed[bone],
                         atol=1e-8, rtol=0) for bone in expected),
                         'Rendered shot clip differs from measured performance: '+actor+'-'+suffix)
    return {'boundaries_per_shot': 61, 'actors': sorted(rigs), 'verified': True}


def verify_performance_motion(performance_root):
    """Load the actual native rig geometry/weights/animation and verify motion."""
    from rig_models import load_rig
    from rig_errors import read_json, safe_path
    root = Path(performance_root)
    before = _performance_input_hashes(root)
    scene = read_json(safe_path(root, 'scene.json', exists=True))
    rigs = {actor: load_rig(root / 'assets' / ('rig-' + actor)) for actor in ('aster', 'moss')}
    shot_clips = verify_shot_clips(rigs)
    result = measure_motion(rigs, scene)
    result['shot_clips'] = shot_clips
    after = _performance_input_hashes(root)
    _require(before == after, 'Performance inputs changed during verification.')
    result['input_sha256'] = before
    return result


def verify_no_motion_negative(performance_root):
    """Execute the absent-motion negative using valid static native animation."""
    from director_scene import scene_document
    from rig_animation import clip_from_poses
    from rig_models import load_rig
    from rig_skeleton import neutral_pose
    root = Path(performance_root)
    rigs = {actor: load_rig(root / 'assets' / ('rig-' + actor)) for actor in ('aster', 'moss')}
    for actor, rig in rigs.items():
        original = rig.clips[actor + '-investigation']
        clip = clip_from_poses(rig, actor + '-investigation', [0., 5.],
                               [neutral_pose(rig.skeleton), neutral_pose(rig.skeleton)], fps=24,
                               provenance=deepcopy(original['provenance']))
        rig.clips = {clip['id']: clip}
    try:
        measure_motion(rigs, scene_document())
    except RigError as exc:
        _require('articulation is absent' in str(exc), 'Absent-motion control failed for an unrelated reason.')
        return {'kind': 'director-static-motion-negative', 'schema_version': 1,
                'expected_rejection': exc.report, 'source_mutated': False}
    raise RigError('DIRECTOR_DELIVERY', 'Static native animations incorrectly passed motion verification.')


def encode_rendered_mvp(root, shots, destination, *, ffmpeg=None, ffprobe=None):
    """Encode only an exact, verified two-shot PNG inventory through its owner.

    ``shots`` supplies each request, result, result-directory relative to root,
    snapshot root, and the backend identity observed by the execution supervisor.
    The native renderer/verifier remains responsible for actual scene execution.
    This adapter adds exact MVP timing/order checks and independent media decoding.
    """
    from fractions import Fraction
    from render3d_contracts import validate
    from render3d_verify import RenderArtifactVerifier
    from silent_video import source_from_blender_manifest, make_request, encode, verify_export
    root = Path(root)
    _require(isinstance(shots, list) and len(shots) == 2, 'Exactly two rendered shots are required.')
    frames = []
    expected_start = 0
    snapshot_hash = None
    cameras, shot_ids = set(), set()
    for item in shots:
        _require(isinstance(item, dict) and set(item) == {'request', 'result', 'directory', 'snapshot_root', 'expected_backend'}, 'Invalid rendered shot binding.')
        request, result = item['request'], item['result']
        validate(request, 'RenderRequest')
        validate(result, 'RenderResult')
        cameras.add(request['camera'])
        shot_ids.add((request['shot']['id'], request['shot']['version']))
        start, end = request['frame_range']['start'], request['frame_range']['end']
        _require(start == expected_start and end > start, 'Shot frame ranges have gaps or overlaps.')
        expected_start = end
        _require(request['fps'] == {'numerator': 24, 'denominator': 1}, 'MVP rendering must use 24 fps.')
        _require(request['settings']['resolution'] == [640, 360], 'MVP rendering must use 640x360.')
        _require(request['settings']['engine'] == 'cycles' and request['settings']['device_policy'] == 'cpu', 'MVP requires genuine Blender Cycles CPU rendering.')
        _require(request['settings']['output'] == {'format': 'png', 'bit_depth': 8, 'passes': ['combined'], 'alpha': 'opaque', 'color_space': 'srgb'}, 'MVP requires opaque 8-bit PNG masters.')
        _require(result['status'] == 'complete' and result['successful'] == list(range(start, end)), 'Shot does not contain all successful frames.')
        _require(result['backend']['device'] == 'cpu', 'MVP render did not execute on CPU.')
        snapshot_hash = snapshot_hash or result['snapshot_sha256']
        _require(result['snapshot_sha256'] == snapshot_hash, 'Shots do not use the same immutable source snapshot.')
        directory = item['directory']
        from rig_errors import safe_path
        output_root = safe_path(root, directory)
        _require(output_root.is_dir(), 'Rendered shot directory is missing.')
        # An earlier verified flag does not substitute for decoding current bytes.
        RenderArtifactVerifier(item['snapshot_root'], item['expected_backend']).verify(request, result, output_root)
        images = sorted(result['images'], key=lambda image: image['frame'])
        _require([image['frame'] for image in images] == list(range(start, end)), 'Shot image order/count differs from the requested interval.')
        for image in images:
            _require(image['format'] == 'png' and image['pass_name'] == 'combined', 'Unexpected image output.')
            record = image['file']
            frames.append({'index': image['frame'], 'path': directory.rstrip('/') + '/' + record['path'],
                           'sha256': record['sha256'], 'bytes': record['bytes']})
    _require(expected_start == 120 and len(frames) == 120, 'MVP requires exactly 120 frames.')
    _require(len(cameras) == 2 and len(shot_ids) == 2, 'MVP requires two distinct shots and actual cameras.')
    source = source_from_blender_manifest(root, {'schema_version': 1, 'kind': 'blender-frame-manifest',
        'engine': 'CYCLES', 'fps': {'numerator': 24, 'denominator': 1}, 'frames': frames})
    request = make_request(source, 'h264-mp4')
    receipt = encode(request, root, destination, ffmpeg=ffmpeg, ffprobe=ffprobe)
    verification = verify_export(destination, request, root=root, ffmpeg=ffmpeg, ffprobe=ffprobe)
    _require(verification['frame_count'] == 120 and verification['audio_stream_count'] == 0,
             'Actual video does not satisfy frame or zero-audio requirements.')
    duration = verification['duration']
    tolerance = verification['timebase_tolerance']
    _require(abs(Fraction(duration['numerator'], duration['denominator']) - 5) <=
             Fraction(tolerance['numerator'], tolerance['denominator']), 'Actual video duration differs from five seconds.')
    return {'schema_version': 1, 'kind': 'director-mvp-media', 'snapshot_sha256': snapshot_hash,
            'frame_source': source, 'video_request': request, 'encode_receipt': receipt,
            'verification': verification, 'visual_review': 'not-performed'}


def render_mvp(performance_root, destination, *, blender=None, ffmpeg=None, ffprobe=None):
    """Execute both canonical shots and encode independently verified masters.

    Use a new destination. Failed output remains private for diagnosis; a final
    receipt appears only after native motion, image and media checks all pass.
    The caller must serialize heavy native execution with the shared host lock.
    """
    import tempfile
    import hashlib
    from atomic_publish import _rename_no_replace
    from rig_errors import atomic_json
    from render3d_snapshot import Scene3DAdapter, create_snapshot
    from render3d_blender import BlenderBackend
    from render3d_acceptance import inspect_render
    from scene3d_contracts import load_project
    from render3d_contracts import validate
    root = Path(performance_root).absolute()
    destination = Path(destination).absolute()
    _require(not destination.exists() and not destination.is_symlink(), 'Use a new delivery destination.')
    _require(destination.parent.is_dir(), 'Delivery parent does not exist.')
    motion = verify_performance_motion(root)
    static = verify_no_motion_negative(root)
    bundle = load_project(root / 'scene-project')
    stage = Path(tempfile.mkdtemp(prefix='.director-delivery-', dir=destination.parent))
    try:
        # Rebuild the render binding from the exact measured native inputs.
        # An independently valid, unrelated static project must never inherit
        # the animated source's motion evidence.
        from director_scene_binding import bind_scene_project
        from director_scene import evaluate_frame
        from rig_models import load_rig
        from rig_errors import read_json
        rigs = {actor: load_rig(root / 'assets' / ('rig-' + actor)) for actor in ('aster', 'moss')}
        source_scene = read_json(root / 'scene.json')
        key_frames = [dict(frame=frame, matrix=evaluate_frame(rigs, source_scene, frame,
                      include_vertices=False)['key_matrix']) for frame in range(121)]
        bound_root = stage / 'bound-scene-project'
        bind_scene_project(root, bound_root, key_frames)
        rebuilt = load_project(bound_root)
        _require(rebuilt['inventory'] == bundle['inventory'], 'Rendered Scene3D project differs from measured native performance.')
        _require(_performance_input_hashes(root) == motion['input_sha256'], 'Native inputs changed during render binding.')
        snapshot_root = stage / 'snapshot'
        snapshot = create_snapshot(bound_root, snapshot_root, Scene3DAdapter())
        snapshot_bytes = (snapshot_root / 'snapshot.json').read_bytes()
        backend = BlenderBackend(blender)
        identity = backend.inspect_version()
        shots = []
        inspections = []
        for identifier, camera, start, end in [('establish', 'wide', 0, 60), ('discovery', 'investigation', 60, 120)]:
            request = dict(schema_version=1, kind='render3d-request', id='director-' + identifier,
                project={key: bundle['project'][key] for key in ('id', 'version')},
                scene={'id': 'director-laboratory', 'version': '1.0.0'},
                shot={'id': identifier, 'version': '1.0.0'}, mode='3d',
                snapshot={'path': 'snapshot.json', 'sha256': hashlib.sha256(snapshot_bytes).hexdigest(), 'bytes': len(snapshot_bytes)},
                fps={'numerator': 24, 'denominator': 1}, frame_range={'start': start, 'end': end},
                source_start={'numerator': 0, 'denominator': 1}, playback_rate={'numerator': 1, 'denominator': 1}, camera=camera,
                settings=dict(engine='cycles', device_policy='cpu', device=None, allow_cpu_fallback=False,
                    resolution=[640, 360], samples=16, seed=0, denoise=False,
                    output={'format': 'png', 'bit_depth': 8, 'passes': ['combined'], 'alpha': 'opaque', 'color_space': 'srgb'},
                    exposure=0, view_transform='standard', resources=dict(memory_bytes=4*1024**3,
                        storage_bytes=2*1024**3, timeout_seconds=1800, workers=1, max_frames=60)),
                priority=0, max_attempts=1, cache_policy='verify-reuse', destination={'path': identifier, 'owner_token': 'director-mvp'})
            # Use the exact canonical scene identity rather than an assumed filename.
            request['scene'] = {key: bundle['project']['scenes'][0][key] for key in ('id', 'version')}
            validate(request, 'RenderRequest')
            atomic_json(stage / (identifier + '-request.json'), request)
            result = backend.render_frames(request, snapshot_root, stage / identifier)
            shot = dict(request=request, result=result, directory=identifier,
                        snapshot_root=snapshot_root, expected_backend=identity)
            shots.append(shot)
            inspections.append(inspect_render(request, result, stage / identifier, snapshot_root,
                                               stage / (identifier + '-inspection'), expected_backend=identity))
        media = encode_rendered_mvp(stage, shots, stage / 'laboratory.mp4', ffmpeg=ffmpeg, ffprobe=ffprobe)
        negatives = verify_media_negatives(stage, shots, media, ffmpeg=ffmpeg, ffprobe=ffprobe)
        receipt = dict(schema_version=1, kind='director-mvp-delivery', motion=motion,
            static_motion_negative=static, snapshot=snapshot, backend=identity,
            shots=[dict(request=item['request'], result=item['result'], directory=item['directory']) for item in shots],
            inspections=inspections, media=media, negatives=negatives, visual_review='unreviewed')
        atomic_json(stage / 'delivery.json', receipt)
        _rename_no_replace(stage, destination)
        return receipt
    except Exception as exc:
        # Failure evidence is useful and must never look like a published delivery.
        atomic_json(stage / 'failure.json', {'error': str(exc), 'published': False})
        raise


def verify_media_negatives(root, shots, media, *, ffmpeg=None, ffprobe=None):
    """Execute missing/corrupt master and audio controls against retained bytes."""
    import subprocess
    from render3d_verify import RenderArtifactVerifier
    from silent_video import verify_export, _binary
    root = Path(root)
    first = shots[0]
    record = first['result']['images'][0]['file']
    path = root / first['directory'] / record['path']
    original = path.read_bytes()
    outcomes = {}
    for name, content in [('missing_frame', None), ('corrupt_frame', b'invalid PNG')]:
        try:
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
            try:
                RenderArtifactVerifier(first['snapshot_root'], first['expected_backend']).verify(
                    first['request'], first['result'], root / first['directory'])
            except Exception as exc:
                outcomes[name] = {'rejected': True, 'error': str(exc)}
            else:
                raise ValueError(name + ' negative unexpectedly accepted')
        finally:
            path.write_bytes(original)
        _require(sha256(path) == record['sha256'], 'Negative control changed retained master.')
    audio = root / 'audio-negative.mp4'
    command = [_binary('ffmpeg', ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin',
        '-i', str(root / 'laboratory.mp4'), '-f', 'lavfi', '-i', 'anullsrc=r=48000:cl=mono',
        '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'copy', '-c:a', 'aac', '-t', '5', '-threads', '1', str(audio)]
    executed = subprocess.run(command, capture_output=True, timeout=120, check=False)
    _require(executed.returncode == 0, 'Could not create actual audio negative: ' + executed.stderr.decode(errors='replace')[-1024:])
    try:
        verify_export(audio, media['video_request'], root=root, ffmpeg=ffmpeg, ffprobe=ffprobe)
    except ValueError as exc:
        _require('audio' in str(exc).lower(), 'Audio negative failed for an unrelated reason.')
        outcomes['audio_present'] = {'rejected': True, 'error': str(exc), 'sha256': sha256(audio)}
    else:
        raise ValueError('Audio negative unexpectedly accepted')
    return outcomes


if __name__ == '__main__':
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Render the actual Director two-shot MVP and independently verify media.')
    parser.add_argument('performance_root', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--blender')
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    args = parser.parse_args()
    result = render_mvp(args.performance_root, args.destination, blender=args.blender,
                        ffmpeg=args.ffmpeg, ffprobe=args.ffprobe)
    print(json.dumps({'destination': str(args.destination), 'frame_count': result['media']['verification']['frame_count'],
                      'visual_review': result['visual_review']}))
