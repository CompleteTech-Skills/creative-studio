"""Causal render controls over native jobs, source evaluation and decoded pixels.

The numerical helpers are not execution attestations. ``verify_control_pair``
requires the existing native delivery authority and independently verified job
publications; arbitrary image/result JSON cannot establish native execution.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import math
from pathlib import Path
import re

from rig_errors import RigError, finite_json, object_hash, safe_path, sha256
from rig_contracts import COORDINATES

EFFECTS = ('geometry', 'camera', 'lighting')
DEFAULT_THRESHOLDS = dict(min_mean_abs=1/255, min_changed_fraction=.005,
                          pixel_epsilon=1/65535, noise_gain=5.,
                          min_geometry_delta_m=1e-5, min_camera_translation_m=1e-5,
                          min_camera_rotation_rad=1e-5, min_light_intensity_delta=1e-5,
                          state_tolerance=1e-9, max_vertices=500000,
                          max_pixels=16*1024**2, memory_bytes=512*1024**2)


def _need(ok, code, message):
    if not ok:
        raise RigError('DIRECTOR_EFFECT_'+code, message, stage='render-effect-control')


def _index(value):
    _need(type(value) in (int, float) and 0 <= value <= 2_000_000_000 and value == int(value),
          'FRAME', 'Use a bounded exact frame index.')
    return int(value)


def _names(values, label):
    _need(isinstance(values, (tuple, list)) and len(values) <= 256 and
          all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value)
              for value in values) and len(set(values)) == len(values),
          'INPUT', label+' must be unique bounded node identities.')
    return tuple(values)


def _thresholds(value):
    _need(value is None or isinstance(value, dict) and set(value) <= set(DEFAULT_THRESHOLDS),
          'LIMIT', 'Unknown render control threshold.')
    result = {**DEFAULT_THRESHOLDS, **(value or {})}
    for key in ('max_vertices', 'max_pixels', 'memory_bytes'):
        maximum = {'max_vertices': 2_000_000, 'max_pixels': 32*1024**2,
                   'memory_bytes': 2*1024**3}[key]
        _need(type(result[key]) is int and 1 <= result[key] <= maximum,
              'LIMIT', 'Invalid '+key+'.')
    for key, amount in result.items():
        if key in ('max_vertices', 'max_pixels', 'memory_bytes'):
            continue
        _need(type(amount) in (int, float) and 0 < amount <= 1e6 and math.isfinite(amount),
              'LIMIT', 'Thresholds must be finite, positive and bounded.')
    _need(result['min_mean_abs'] >= 1e-6 and result['min_changed_fraction'] <= 1 and
          result['noise_gain'] >= 1 and result['state_tolerance'] <= 1e-6,
          'LIMIT', 'Control thresholds cannot admit zero signal or unbounded state drift.')
    return result


def _array(value, columns, *, limit):
    import numpy as np
    _need(isinstance(value, (list, tuple)) and 0 < len(value) <= limit and
          all(isinstance(row, (list, tuple)) and len(row) == columns and
              all(type(number) in (int, float) and abs(number) <= 1e12 and math.isfinite(number)
                  for number in row) for row in value),
          'STATE', 'Expected bounded finite native numerical arrays.')
    return np.asarray(value, dtype=np.float64)


def _matrix(value):
    import numpy as np
    matrix = _array(value, 4, limit=4)
    _need(matrix.shape == (4, 4) and np.allclose(matrix[3], [0, 0, 0, 1], rtol=0, atol=1e-12),
          'STATE', 'Expected a finite affine native world matrix.')
    return matrix


def _close(left, right, tolerance):
    if type(left) in (int, float) and type(right) in (int, float):
        return abs(left-right) <= tolerance
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(_close(left[key], right[key], tolerance) for key in left)
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return len(left) == len(right) and all(_close(a, b, tolerance) for a, b in zip(left, right))
    return type(left) is type(right) and left == right


def _projection(evaluation, settings, thresholds):
    """Project actual owner evaluation; never create or mutate a scene."""
    import numpy as np
    from scene3d_evaluate import camera_matrices
    finite_json(evaluation)
    _need(isinstance(evaluation, dict) and evaluation.get('coordinate_system') == COORDINATES and
          evaluation.get('native_geometry_sampled') is True and evaluation.get('authored_mutated') is False,
          'STATE', 'Use actual unmutated canonical Scene3D geometry evaluation.')
    nodes, geometry = evaluation.get('nodes'), evaluation.get('native_geometry')
    _need(isinstance(nodes, dict) and 0 < len(nodes) <= 4096 and
          isinstance(geometry, dict) and 0 < len(geometry) <= 256,
          'STATE', 'Native scene and sampled geometry are required.')
    _need(isinstance(evaluation.get('world'), dict) and evaluation.get('camera') in nodes and
          all(isinstance(state, dict) and 'type' in state and 'world_matrix' in state
              for state in nodes.values()), 'STATE', 'Malformed native scene state.')
    _need(isinstance(settings, dict) and isinstance(settings.get('resolution'), (list, tuple)) and
          len(settings['resolution']) == 2, 'STATE', 'A native raster is required.')
    arrays, count = {}, 0
    for actor, meshes in geometry.items():
        _need(actor in nodes and isinstance(meshes, dict) and 0 < len(meshes) <= 1024,
              'STATE', 'Sampled geometry must bind actual scene nodes and meshes.')
        world = _matrix(nodes[actor]['world_matrix'])
        _need(abs(float(np.linalg.det(world[:3, :3]))) > 1e-15,
              'STATE', 'Actor world matrix must be invertible for local deformation controls.')
        inverse = np.linalg.inv(world)
        arrays[actor] = {}
        for mesh, positions in meshes.items():
            remaining = thresholds['max_vertices']-count
            values = _array(positions, 3, limit=remaining)
            count += len(values)
            local = np.column_stack((values, np.ones(len(values)))) @ inverse.T
            arrays[actor][mesh] = dict(world=values.tolist(), local=local[:, :3].tolist())
    camera = camera_matrices(evaluation, dict(width=settings['resolution'][0], height=settings['resolution'][1]))
    lights, static, matrices = {}, {}, {}
    # Compare physical world transforms below. Their redundant local matrices
    # can legitimately change for an explicitly controlled moving prop.
    derived = {'world_bounds', 'local_matrix', 'native_clip', 'native_animation_evaluated'}
    for identifier, state in nodes.items():
        _need(isinstance(state, dict) and 'type' in state, 'STATE', 'Malformed native node state.')
        world = _matrix(state['world_matrix']).tolist()
        if state['type'] == 'camera':
            continue  # Only the selected camera contributes to this frame.
        if state['type'] == 'light':
            lights[identifier] = dict(world_matrix=world,
                state={key: deepcopy(value) for key, value in state.items()
                       if key not in derived and key != 'world_matrix'})
            continue
        matrices[identifier] = world
        static[identifier] = {key: deepcopy(value) for key, value in state.items()
                              if key not in derived and key != 'world_matrix'}
    return dict(geometry=arrays, vertices=count, camera=camera, lights=lights,
                world=deepcopy(evaluation['world']), static_nodes=static, matrices=matrices)


def compare_evaluations(before, after, settings, *, effect, actor_ids=(), moving_nodes=(),
                        light_ids=(), thresholds=None):
    """Numerical control helper only; this does not attest rendered execution."""
    import numpy as np
    _need(effect in (*EFFECTS, 'noise'), 'INPUT', 'Unknown native render effect.')
    limits = _thresholds(thresholds)
    actors = _names(actor_ids, 'Actor identities')
    moving = _names(moving_nodes, 'Moving geometry nodes')
    lights = _names(light_ids, 'Controlled light identities')
    left, right = _projection(before, settings, limits), _projection(after, settings, limits)
    tolerance = limits['state_tolerance']
    _need(_close(left['world'], right['world'], tolerance), 'CONFOUNDED', 'World/exposure changed outside the selected control.')
    _need(_close(left['static_nodes'], right['static_nodes'], tolerance),
          'CONFOUNDED', 'Geometry identities, materials or visibility changed outside the selected control.')
    _need(set(left['geometry']) == set(right['geometry']) and set(left['matrices']) == set(right['matrices']),
          'CONFOUNDED', 'Native geometry/node identities differ.')
    _need(effect == 'geometry' or not moving, 'INPUT', 'Only geometry controls may declare moving scene nodes.')
    _need(effect == 'geometry' or not actors, 'INPUT', 'Only geometry controls may declare deforming actors.')
    _need(set(moving) <= set(left['matrices']) and not set(moving) & set(left['geometry']),
          'INPUT', 'Actor roots cannot be treated as independent moving props.')
    for node in left['matrices']:
        if effect == 'geometry' and node in moving:
            continue
        _need(_close(left['matrices'][node], right['matrices'][node], tolerance),
              'CONFOUNDED', 'An undeclared geometry/root transform changed: '+node)
    geometry_delta = 0.
    changed_actors = []
    for actor in left['geometry']:
        _need(set(left['geometry'][actor]) == set(right['geometry'][actor]), 'CONFOUNDED', 'Native mesh identities differ.')
        delta = 0.
        for mesh in left['geometry'][actor]:
            a = left['geometry'][actor][mesh]
            b = right['geometry'][actor][mesh]
            _need(len(a['local']) == len(b['local']), 'CONFOUNDED', 'Native vertex topology differs.')
            if effect == 'geometry':
                delta = max(delta, float(np.max(np.linalg.norm(np.asarray(a['local'])-np.asarray(b['local']), axis=1))))
            else:
                _need(_close(a['world'], b['world'], tolerance), 'CONFOUNDED', 'Geometry changed in a non-geometry control.')
        if delta > limits['min_geometry_delta_m']:
            changed_actors.append(actor)
        geometry_delta = max(geometry_delta, delta)
    if effect == 'geometry':
        _need(actors and set(actors) <= set(left['geometry']), 'INPUT', 'Select actual sampled actor identities.')
        _need(set(changed_actors) <= set(actors) and bool(changed_actors), 'NO_EFFECT', 'No selected actor has real local-space deformation.')
    camera_translation = camera_rotation = 0.
    if effect == 'camera':
        a, b = _matrix(left['camera']['world_matrix']), _matrix(right['camera']['world_matrix'])
        camera_translation = float(np.linalg.norm(a[:3, 3]-b[:3, 3]))
        cosine = min(1., max(-1., float((np.trace(a[:3, :3].T @ b[:3, :3])-1)/2)))
        camera_rotation = math.acos(cosine)
        _need(camera_translation > limits['min_camera_translation_m'] or camera_rotation > limits['min_camera_rotation_rad'],
              'NO_EFFECT', 'A lens-only change does not establish actual camera motion.')
    else:
        _need(_close(left['camera'], right['camera'], tolerance), 'CONFOUNDED', 'The selected camera changed in a non-camera control.')
    intensity_deltas = {}
    _need(set(left['lights']) == set(right['lights']), 'CONFOUNDED', 'Native light identities differ.')
    if effect == 'lighting':
        _need(lights and set(lights) <= set(left['lights']), 'INPUT', 'Select actual native light identities.')
        lcopy, rcopy = deepcopy(left['lights']), deepcopy(right['lights'])
        for node in lights:
            for item in (lcopy[node], rcopy[node]):
                _need(isinstance(item['state'].get('light'), dict) and 'intensity' in item['state']['light'],
                      'STATE', 'Expected an explicit native light intensity.')
            a = lcopy[node]['state']['light'].pop('intensity')
            b = rcopy[node]['state']['light'].pop('intensity')
            _need(type(a) in (int, float) and type(b) in (int, float) and 0 <= a <= 1e12 and 0 <= b <= 1e12,
                  'STATE', 'Light intensity must be finite and bounded.')
            intensity_deltas[node] = abs(a-b)
        _need(_close(lcopy, rcopy, tolerance), 'CONFOUNDED', 'Lighting control changed something other than selected intensities.')
        _need(max(intensity_deltas.values()) > limits['min_light_intensity_delta'], 'NO_EFFECT', 'Native light intensity did not change.')
    else:
        _need(not lights and _close(left['lights'], right['lights'], tolerance),
              'CONFOUNDED', 'Light state changed in a non-lighting control.')
    return dict(effect=effect, numerical_control_verified=True, native_execution_verified=False,
                vertices=left['vertices'], changed_actors=changed_actors,
                max_local_geometry_delta_m=geometry_delta, camera_translation_m=camera_translation,
                camera_rotation_rad=camera_rotation, light_intensity_deltas=intensity_deltas,
                before_state_sha256=object_hash(left), after_state_sha256=object_hash(right))


def image_metrics(before_path, after_path, settings, *, thresholds=None):
    """Independently decode actual files; image metrics alone prove no 3D work."""
    import numpy as np
    from render3d_verify import decode_image
    limits = _thresholds(thresholds)
    _need(isinstance(settings, dict) and isinstance(settings.get('resolution'), (list, tuple)) and
          len(settings['resolution']) == 2 and isinstance(settings.get('output'), dict),
          'INPUT', 'Use native raster and output settings.')
    width, height = settings['resolution']
    _need(type(width) is int and type(height) is int and width > 0 and height > 0
          and width*height <= limits['max_pixels'], 'LIMIT', 'Pixel comparison exceeds its raster budget.')
    _need(width*height*3*8*8 <= limits['memory_bytes'], 'LIMIT', 'Pixel arrays exceed their memory admission.')
    decoded = []
    for path in (before_path, after_path):
        metadata, channels = decode_image(path, output=settings['output'], resolution=settings['resolution'],
                                           pass_name='combined', max_bytes=limits['memory_bytes'],
                                           memory_bytes=limits['memory_bytes'])
        _need({'R', 'G', 'B'} <= set(channels), 'PIXELS', 'Effect controls need actual RGB combined pixels.')
        pixels = np.stack([channels[name] for name in ('R', 'G', 'B')], axis=-1)
        _need(pixels.shape == (height, width, 3) and np.all(np.isfinite(pixels)), 'PIXELS', 'Malformed decoded RGB arrays.')
        decoded.append((metadata, pixels))
    difference = np.abs(decoded[0][1]-decoded[1][1])
    return dict(before=decoded[0][0], after=decoded[1][0],
                mean_abs=float(np.mean(difference)), rmse=float(np.sqrt(np.mean(difference*difference))),
                max_abs=float(np.max(difference)),
                changed_fraction=float(np.mean(np.any(difference > limits['pixel_epsilon'], axis=-1))),
                comparison_space=settings['output']['color_space'], native_execution_verified=False)


def _materialized(store, root, binding):
    from render3d_jobs import JobStore
    _need(isinstance(store, JobStore), 'AUTHORITY', 'Use the authoritative native JobStore, never supplied result dictionaries.')
    try:
        from director_final_delivery import verify_native_materialization
    except ImportError as exc:
        raise RigError('DIRECTOR_EFFECT_ADAPTER_UNAVAILABLE', 'Install the canonical native delivery verification adapter.', stage='render-effect-control') from exc
    proof = verify_native_materialization(store, root, binding, required_scope='scene3d')
    _need(proof['backend']['id'] == 'blender', 'AUTHORITY', 'Effect acceptance requires the actual Blender backend.')
    return proof


def _sample(proof, frame, limits):
    from render3d_contracts import make_frame_spec
    from scene3d_contracts import load_project
    from scene3d_evaluate import evaluate_scene
    request = proof['request']
    frame = _index(frame)
    _need(request['frame_range']['start'] <= frame < request['frame_range']['end'], 'FRAME', 'Selected frame is outside the verified native job.')
    rows = [row for row in proof['state']['frames'] if row['frame'] == frame]
    _need(len(rows) == 1 and rows[0]['state'] == 'complete', 'AUTHORITY', 'Native frame completion is missing.')
    images = [image for image in proof['result']['images'] if image['frame'] == frame and image['pass_name'] == 'combined']
    _need(len(images) == 1, 'PIXELS', 'Select exactly one verified native combined image.')
    spec = make_frame_spec(request, frame, proof['snapshot_root']/'snapshot.json')
    bundle = load_project(proof['snapshot_root'])
    shots = [ref['path'] for ref in bundle['project']['shots']
             if ref['id'] == request['shot']['id'] and ref['version'] == request['shot']['version']]
    scenes = [ref['path'] for ref in bundle['project']['scenes']
              if ref['id'] == request['scene']['id'] and ref['version'] == request['scene']['version']]
    _need(len(shots) == len(scenes) == 1, 'STATE', 'Native scene/shot binding is ambiguous.')
    source_frame = Fraction(**spec['source_time'])*Fraction(**bundle['documents'][shots[0]]['fps'])
    evaluation = evaluate_scene(bundle, scenes[0], shots[0], source_frame,
                                source_root=proof['snapshot_root'], sample_geometry=True,
                                max_geometry_vertices=limits['max_vertices'])
    return dict(frame=frame, frame_spec=spec, cache_key=rows[0]['cache_key'], evaluation=evaluation,
                fixture=bundle['project']['provenance']['fixture'],
                image=images[0]['file'], image_path=safe_path(proof['output_root'], images[0]['file']['path'], exists=True))


def _request_control(left, right):
    _need(left['backend'] == right['backend'], 'CONFOUNDED', 'Backend/device/runtime identity changed across the control.')
    metadata = {'id', 'shot', 'frame_range', 'source_start', 'camera', 'destination'}
    a = {key: value for key, value in left['request'].items() if key not in metadata}
    b = {key: value for key, value in right['request'].items() if key not in metadata}
    _need(a == b, 'CONFOUNDED', 'Render settings/seed/source snapshot changed outside the control.')
    return sorted(key for key in metadata if left['request'][key] != right['request'][key])


def verify_control_pair(store, root, before_binding, after_binding, *, effect,
                        before_frame, after_frame, actor_ids=(), moving_nodes=(), light_ids=(),
                        noise_binding=None, noise_frame=None, thresholds=None):
    """Verify actual native publications, domain changes and observed pixel effect.

    A measured repeat, when supplied, must use a distinct native cache entry yet
    the same physical state. Without a repeat, fixed seed/backend/settings and
    exact non-target-domain controls remain mandatory; repeat drift is explicitly
    unmeasured and no statistical confidence is claimed.
    """
    from render3d_contracts import make_frame_spec
    import scene3d_evaluate
    import render3d_verify
    _need(effect in EFFECTS, 'INPUT', 'Select geometry, camera or lighting.')
    limits = _thresholds(thresholds)
    root = Path(root)
    before = _materialized(store, root, before_binding)
    after = before if before_binding == after_binding else _materialized(store, root, after_binding)
    differences = _request_control(before, after)
    left, right = _sample(before, before_frame, limits), _sample(after, after_frame, limits)
    rechecks = [(before_binding, before, left), (after_binding, after, right)]
    numeric = compare_evaluations(left['evaluation'], right['evaluation'], before['request']['settings'],
        effect=effect, actor_ids=actor_ids, moving_nodes=moving_nodes, light_ids=light_ids, thresholds=limits)
    pixels = image_metrics(left['image_path'], right['image_path'], before['request']['settings'], thresholds=limits)
    _need(pixels['before']['sha256'] == left['image']['sha256'] and pixels['after']['sha256'] == right['image']['sha256'],
          'CHANGED', 'Decoded images differ from the exact native publication.')
    noise = dict(measured=False, policy='matched fixed seed/backend/settings and numerical non-target controls',
                 statistical_confidence=None)
    noise_mean = 0.
    if noise_binding is not None:
        _need(noise_frame is not None, 'FRAME', 'Select the independent repeat frame.')
        repeat = _materialized(store, root, noise_binding)
        _request_control(before, repeat)
        repeated = _sample(repeat, noise_frame, limits)
        rechecks.append((noise_binding, repeat, repeated))
        _need(repeated['cache_key'] != left['cache_key'], 'AUTHORITY', 'A reused cache entry cannot measure independent render drift.')
        compare_evaluations(left['evaluation'], repeated['evaluation'], before['request']['settings'], effect='noise', thresholds=limits)
        measured = image_metrics(left['image_path'], repeated['image_path'], before['request']['settings'], thresholds=limits)
        _need(measured['after']['sha256'] == repeated['image']['sha256'], 'CHANGED', 'Noise repeat bytes changed.')
        noise_mean = measured['mean_abs']
        noise = dict(measured=True, binding=deepcopy(noise_binding), frame=_index(noise_frame),
                     cache_key=repeated['cache_key'], metrics=measured, statistical_confidence=None)
    minimum = max(limits['min_mean_abs'], noise_mean*limits['noise_gain'])
    _need(pixels['mean_abs'] >= minimum and pixels['changed_fraction'] >= limits['min_changed_fraction'],
          'NO_PIXEL_EFFECT', 'Decoded pixel change is absent, too small or explained by measured repeat drift.')
    # Recheck the live native revisions and request-bound source closure after all
    # evaluation/decoding. Never return an old green result after a state change.
    for binding, proof, sample in rechecks:
        state = store.status(binding['job'], event_limit=0)
        _need(state['state'] == 'complete' and state['revision'] == binding['job_revision'] and
              object_hash(state['request']) == binding['request_sha256'] and object_hash(state['backend']) == binding['backend_sha256'],
              'CHANGED', 'Native job changed during effect verification.')
        _need(make_frame_spec(proof['request'], sample['frame'], proof['snapshot_root']/'snapshot.json') == sample['frame_spec'],
              'CHANGED', 'Native source changed during effect verification.')
    result = dict(schema_version=1, kind='director-render-effect-control', effect=effect, verified=True,
                  native_execution_verified=True,
                  before=dict(binding=deepcopy(before_binding), frame=left['frame'], cache_key=left['cache_key'],
                              frame_spec_sha256=object_hash(left['frame_spec']), image=left['image']),
                  after=dict(binding=deepcopy(after_binding), frame=right['frame'], cache_key=right['cache_key'],
                             frame_spec_sha256=object_hash(right['frame_spec']), image=right['image']),
                  backend=deepcopy(before['backend']), allowed_request_differences=differences,
                  numeric=numeric, pixels=pixels, noise=noise, minimum_required_mean_abs=minimum,
                  thresholds=limits, fixture=left['fixture'] or right['fixture'],
                  verifier_sources={'scene_evaluator': sha256(Path(scene3d_evaluate.__file__)),
                                    'pixel_verifier': sha256(Path(render3d_verify.__file__)),
                                    'effect_verifier': sha256(Path(__file__))},
                  production_art_approved=False)
    result['sha256'] = object_hash(result)
    return result
