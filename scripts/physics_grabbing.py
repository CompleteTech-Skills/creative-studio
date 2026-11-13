"""Physical prop attachment consumers of the existing rig and physics owners.

This module evaluates spatial grips and compiles a release into an ordinary
PhysicsWorld. It does not persist ownership, schedule a simulation, or implement
a solver. Persistent grabs/handoffs require the continuity registry transaction
owner; fixed/spring attachment execution requires the joint adapter.
"""
from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import math

import numpy as np

from rig_attachments import attach_prop, detach_prop, prop_world, socket_world
from rig_errors import RigError, require, object_hash
from rig_math import decompose, qinverse, qmatrix, qmul, transform_matrix


def _number(value, label, *, positive=False, maximum=1e6):
    require(type(value) in (int, float), 'PHYSICS_GRIP_NUMBER', label+' must be a number.')
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise RigError('PHYSICS_GRIP_NUMBER', label+' exceeds finite numeric bounds.') from exc
    require(math.isfinite(number) and abs(number) <= maximum and (not positive or number > 0),
            'PHYSICS_GRIP_NUMBER', label+' exceeds finite numeric bounds.')
    return number


def _vector(value, count, label, *, maximum=1e6):
    require(isinstance(value, (list, tuple)) and len(value) == count,
            'PHYSICS_GRIP_VECTOR', label+' has the wrong dimensions.')
    return np.asarray([_number(x, label, maximum=maximum) for x in value], dtype=float)


def rigid_transform(value):
    """Admit canonical rigid TRS without coercing strings, booleans or scaling."""
    require(isinstance(value, dict) and {'translation', 'rotation'} <= set(value) and
            set(value) <= {'translation', 'rotation', 'scale'},
            'PHYSICS_GRIP_TRANSFORM', 'Expected canonical rigid translation/rotation.')
    translation = _vector(value['translation'], 3, 'translation', maximum=10000)
    rotation = _vector(value['rotation'], 4, 'rotation', maximum=1)
    require(abs(float(np.linalg.norm(rotation))-1) <= 1e-6,
            'PHYSICS_GRIP_TRANSFORM', 'Rotation must be a normalized XYZW quaternion.')
    if 'scale' in value:
        scale = _vector(value['scale'], 3, 'scale')
        require(np.array_equal(scale, np.ones(3)), 'PHYSICS_GRIP_SCALE',
                'Bake actor/prop scale through the geometry owner before physical attachment.')
    return dict(translation=translation.tolist(), rotation=(rotation/np.linalg.norm(rotation)).tolist(),
                scale=[1., 1., 1.])


def _rigid_matrix(matrix):
    value = decompose(matrix)
    require(np.allclose(value['scale'], [1., 1., 1.], rtol=0, atol=1e-10),
            'PHYSICS_GRIP_SCALE', 'Evaluated socket or parent has nonunit scale.')
    value['scale'] = [1., 1., 1.]
    return rigid_transform(value)


def _validate_pose(rig, pose):
    from rig_contracts import validate_contract
    validate_contract(pose, 'Pose')
    require(pose['rig_version'] == rig.skeleton['version'], 'PHYSICS_GRIP_RIG_VERSION',
            'Pose and authoritative rig skeleton versions differ.')
    # Guard numeric conversion before the existing FK owner receives overrides.
    for override in pose['bones'].values():
        for key, size in (('translation', 3), ('rotation', 4), ('scale', 3)):
            if key in override:
                _vector(override[key], size, 'pose '+key)


def grip_world(asset, prop_transform, grip_id):
    """Use a named Scene3D asset socket as the prop's local grasp anchor."""
    from scene3d_contracts import validate_document
    validate_document(asset, 'asset')
    require(type(grip_id) is str, 'PHYSICS_GRIP_REFERENCE', 'Grip identifier must be a string.')
    matches = [entry for entry in asset['sockets'] if entry['id'] == grip_id]
    require(len(matches) == 1, 'PHYSICS_GRIP_REFERENCE', 'Named prop grip is unavailable.')
    return transform_matrix(rigid_transform(prop_transform)) @ transform_matrix(rigid_transform(matches[0]['transform']))


def socket_in_world(rig, pose, socket_id, actor_transform):
    """Apply the authoritative actor root to the canonical rig socket FK."""
    _validate_pose(rig, pose)
    matrix = transform_matrix(rigid_transform(actor_transform)) @ socket_world(rig, pose, socket_id)
    _rigid_matrix(matrix)
    return matrix


def reach_report(rig, pose, socket_id, actor_transform, asset, prop_transform, grip_id,
                 *, max_distance=.1, max_angle_radians=math.pi/6):
    """Measure current anchor proximity, not collision reach or an IK guarantee."""
    distance_limit = _number(max_distance, 'reach distance', positive=True, maximum=10)
    angle_limit = _number(max_angle_radians, 'reach angle', positive=True, maximum=math.pi)
    hand = socket_in_world(rig, pose, socket_id, actor_transform)
    grip = grip_world(asset, prop_transform, grip_id)
    distance = float(np.linalg.norm(hand[:3, 3]-grip[:3, 3]))
    relative = decompose(np.linalg.inv(hand) @ grip)['rotation']
    angle = 2*math.acos(min(1., abs(relative[3])))
    return dict(eligible=distance <= distance_limit and angle <= angle_limit,
                distance_m=distance, angle_radians=angle,
                max_distance_m=distance_limit, max_angle_radians=angle_limit,
                hand_world=_rigid_matrix(hand), grip_world=_rigid_matrix(grip),
                scope='present named-anchor proximity; no occlusion or IK-reach claim')


def attach_kinematic(rig, pose, socket_id, actor_transform, asset, prop_transform, grip_id,
                     *, max_distance=.1, max_angle_radians=math.pi/6):
    """Compute a keep-world rig attachment; committing ownership is separate."""
    report = reach_report(rig, pose, socket_id, actor_transform, asset, prop_transform, grip_id,
                          max_distance=max_distance, max_angle_radians=max_angle_radians)
    require(report['eligible'], 'PHYSICS_GRIP_OUT_OF_REACH', 'Named grip and socket are not aligned.')
    root = transform_matrix(rigid_transform(actor_transform))
    local = _rigid_matrix(np.linalg.inv(root) @ transform_matrix(rigid_transform(prop_transform)))
    result = attach_prop(rig, pose, dict(owner=None, socket=None, transform=local), socket_id, keep_world=True)
    # The canonical owner decomposes a product of admitted rigid matrices.
    # Normalize only computed roundoff; authored nonunit scales fail above.
    result['transform'] = _rigid_matrix(transform_matrix(result['transform']))
    observed = root @ prop_world(rig, pose, result)
    return dict(mode='kinematic', attachment=result, world_transform=_rigid_matrix(observed),
                reach=report, ownership_status='uncommitted; continuity transaction required')


def attachment_pose(rig, pose, attachment, actor_transform):
    """Evaluate the same owner-bound attachment under a later rig/root pose."""
    _validate_pose(rig, pose)
    rigid_transform(attachment['transform'])
    return _rigid_matrix(transform_matrix(rigid_transform(actor_transform)) @ prop_world(rig, pose, attachment))


def detach_kinematic(rig, pose, attachment, actor_transform):
    _validate_pose(rig, pose)
    rigid_transform(attachment['transform'])
    detached = detach_prop(rig, pose, attachment)
    return _rigid_matrix(transform_matrix(rigid_transform(actor_transform)) @ transform_matrix(detached['transform']))


def handoff_kinematic(source_rig, source_pose, source_attachment, source_root,
                      target_rig, target_pose, target_socket, target_root,
                      asset, grip_id, *, max_distance=.1, max_angle_radians=math.pi/6):
    """Prepare an aligned keep-world handoff without changing persistent owners.

    Actor-instance identity and compare-and-swap are deliberately left to the
    continuity transaction owner. A rig character ID alone cannot distinguish
    two scene instances of the same character.
    """
    world = detach_kinematic(source_rig, source_pose, source_attachment, source_root)
    result = attach_kinematic(target_rig, target_pose, target_socket, target_root,
                             asset, world, grip_id, max_distance=max_distance,
                             max_angle_radians=max_angle_radians)
    result['ownership_status'] = 'uncommitted handoff; actor-instance CAS transaction required'
    return result


def _time(value):
    require(isinstance(value, dict) and set(value) == {'numerator', 'denominator'} and
            type(value['numerator']) is int and type(value['denominator']) is int and
            0 <= value['numerator'] <= 2**53 and 1 <= value['denominator'] <= 2**31,
            'PHYSICS_GRIP_TIME', 'Use bounded exact nonnegative rational seconds.')
    return Fraction(value['numerator'], value['denominator'])


def release_state(previous, current, *, center_of_mass=(0., 0., 0.), impulse=(0., 0., 0.),
                  angular_impulse=(0., 0., 0.)):
    """Recover world-space velocity, retaining authored versus physical provenance.

    Quaternion differencing uses the shortest arc between two observed poses;
    rotations of more than pi between samples cannot be recovered unambiguously.
    The returned linear velocity is at the COM, as required by Bullet's API.
    Explicit impulses remain forces in the native world, not velocity changes.
    """
    require(isinstance(previous, dict) and isinstance(current, dict) and
            set(previous) == set(current) == {'time', 'transform'},
            'PHYSICS_GRIP_SAMPLE', 'Release requires two exact-time transform samples.')
    dt = _time(current['time'])-_time(previous['time'])
    require(0 < dt <= 1 and dt.denominator <= 2**31, 'PHYSICS_GRIP_TIME',
            'Release samples need a bounded positive exact interval of at most one second.')
    before, after = rigid_transform(previous['transform']), rigid_transform(current['transform'])
    origin_velocity = (np.asarray(after['translation'])-before['translation'])/float(dt)
    delta = qmul(after['rotation'], qinverse(before['rotation']))
    if delta[3] < 0:
        delta = -delta
    sine = float(np.linalg.norm(delta[:3]))
    angular = np.zeros(3) if sine < 1e-12 else delta[:3]/sine*(2*math.atan2(sine, delta[3])/float(dt))
    com = _vector(center_of_mass, 3, 'center of mass', maximum=1000)
    arm = qmatrix(after['rotation']) @ com
    linear = origin_velocity + np.cross(angular, arm)
    require(np.isfinite(linear).all() and np.isfinite(angular).all() and
            np.linalg.norm(linear) <= 50 and np.linalg.norm(angular) <= 100,
            'PHYSICS_GRIP_SPEED', 'Recovered release velocity exceeds the native safety profile.')
    return dict(transform=after, origin_linear_velocity=origin_velocity.tolist(),
                linear_velocity=linear.tolist(), angular_velocity=angular.tolist(),
                center_of_mass=com.tolist(),
                impulse=_vector(impulse, 3, 'impulse', maximum=1000).tolist(),
                angular_impulse=_vector(angular_impulse, 3, 'angular impulse', maximum=1000).tolist(),
                sample_interval=dict(numerator=dt.numerator, denominator=dt.denominator),
                release_time=deepcopy(current['time']),
                velocity_source='shortest-arc finite difference of authoritative carried world poses',
                collision_status='requested: compile preserves the supplied original collision profile')


def compile_release(world, payloads, body_id, release):
    """Build a native release input through existing PhysicsWorld/Scene3D owners.

    The returned world starts at release time zero. Story time is retained in the
    release record; no dynamic output poses are authored or predicted here.
    Only an existing single-root primitive body may be released in this profile.
    """
    from physics_backends import canonical, digest, parse
    from physics_contracts import validate_contract
    from scene3d_contracts import validate_document
    from scene3d_graph import SceneGraph
    validate_contract(world)
    require(isinstance(payloads, dict) and all(type(k) is str and type(v) is bytes for k, v in payloads.items()),
            'PHYSICS_GRIP_SOURCE', 'Release consumes immutable native source payload bytes.')
    result, sources = deepcopy(world), dict(payloads)
    matches = [body for body in result['bodies'] if body['id'] == body_id]
    require(len(matches) == 1, 'PHYSICS_GRIP_REFERENCE', 'Released body is unavailable.')
    body = matches[0]
    require(body['category'] == 'rigid-body' and body['mass']['kilograms'] > 0 and
            body['parent'] is None and len(body['colliders']) == 1 and
            body['colliders'][0]['shape']['type'] in ('sphere', 'box') and not result['joints'],
            'PHYSICS_GRIP_UNSUPPORTED', 'Release profile requires an independent massive sphere/box and no joints.')
    require(isinstance(release, dict) and set(release) == {
        'transform', 'origin_linear_velocity', 'linear_velocity', 'angular_velocity', 'impulse',
        'angular_impulse', 'center_of_mass', 'sample_interval', 'release_time', 'velocity_source', 'collision_status'},
        'PHYSICS_GRIP_SAMPLE', 'Use a complete release state from two authoritative samples.')
    require(0 < _time(release['sample_interval']) <= 1, 'PHYSICS_GRIP_TIME', 'Invalid release sample interval.')
    _time(release['release_time'])
    _vector(release['origin_linear_velocity'], 3, 'origin velocity', maximum=1e6)
    require(_vector(release['center_of_mass'], 3, 'center of mass', maximum=1000).tolist() ==
            body['mass']['center_of_mass'], 'PHYSICS_GRIP_COM',
            'Release velocity must be recovered for this exact physical center of mass.')
    transform = rigid_transform(release['transform'])
    body.update(mode='dynamic', authority='physics', animation=None,
                transform={key: transform[key] for key in ('translation', 'rotation')},
                linear_velocity=_vector(release['linear_velocity'], 3, 'release velocity', maximum=50).tolist(),
                angular_velocity=_vector(release['angular_velocity'], 3, 'release angular velocity', maximum=100).tolist())
    if 'sleep' in body:
        body['sleep']['initially_sleeping'] = False
    # Never silently delete pre-existing force programs or pending events.
    require(not result['forces'] and not result['events'], 'PHYSICS_GRIP_UNSUPPORTED',
            'Compose other force/event programs through their canonical owner before release.')
    for field, kind in (('impulse', 'impulse'), ('angular_impulse', 'angular-impulse')):
        vector = _vector(release[field], 3, field, maximum=1000).tolist()
        if any(vector):
            result['forces'].append(dict(id='release-'+kind, type=kind, bodies=[body_id], vector=vector,
                point=[0, 0, 0], application='center', frame_range=dict(start=0, end=1), strength=1,
                seed=0, categories=['rigid-body']))
    reference = result['scene']
    require(reference['path'] in sources and digest(sources[reference['path']]) == reference['sha256'],
            'PHYSICS_GRIP_SOURCE', 'Source Scene3D does not match the world binding.')
    scene = parse(sources[reference['path']]);validate_document(scene, 'scene')
    nodes = [node for node in scene['nodes'] if node['id'] == body_id]
    require(len(nodes) == 1 and nodes[0]['parent'] is None and not nodes[0].get('attachment') and
            not nodes[0]['constraints'] and not nodes[0]['animation_bindings'] and
            not any(nodes[0]['transform'].get('pivot', [0, 0, 0])), 'PHYSICS_GRIP_UNSUPPORTED',
            'Release requires one unparented Scene3D node with no other transform writers.')
    # Use the authoritative editor and its lock/revision checks. The caller's
    # original documents/bytes are untouched; this is a new immutable snapshot.
    graph = SceneGraph(scene)
    graph.set_transform(body_id, transform, expected_revision=scene['revision'])
    scene = graph.document()
    validate_document(scene, 'scene')
    sources[reference['path']] = canonical(scene)
    new_reference = {**reference, 'sha256': digest(sources[reference['path']])}
    result['scene'] = new_reference
    require('project.json' in sources, 'PHYSICS_GRIP_SOURCE', 'This profile requires the canonical project manifest.')
    project = parse(sources['project.json']);validate_document(project, 'project')
    native_reference = {**new_reference, 'kind': 'scene3d-scene'}
    old_reference = {**reference, 'kind': 'scene3d-scene'}
    require(project['scenes'].count(old_reference) == 1 and project['dependency_lock'].count(old_reference) == 1,
            'PHYSICS_GRIP_SOURCE', 'Project must bind the exact source scene once in scenes and dependency lock.')
    for field in ('scenes', 'dependency_lock'):
        project[field] = [native_reference if item == old_reference else item
                          for item in project[field]]
    validate_document(project, 'project');sources['project.json'] = canonical(project)
    validate_contract(result)
    return result, sources, dict(release_sha256=object_hash(release), source_world_sha256=object_hash(world),
                                released_world_sha256=object_hash(result),
                                status='compiled; native execution and ownership transaction not performed')


def capabilities():
    return dict(kinematic_attachment='spatial evaluator; ownership commit unavailable until registry provider',
                release='existing native PhysicsWorld rigid-body execution',
                fixed_attachment='unsupported; joint owner integration required',
                spring_attachment='unsupported; joint owner integration required',
                persistent_ownership='unavailable; continuity registry transaction provider required')
