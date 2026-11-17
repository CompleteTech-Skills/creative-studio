"""Real rig-socket carry/release input for the ordinary native physics adapter.

The engineering prop's geometry is actual OBJ data. Carried placement is
explicitly authored kinematics; all post-release movement must be computed by
the chosen native rigid-body adapter. This is not a persistent ownership demo.
"""
from copy import deepcopy
import numpy as np

from physics_backend_example import build_inputs
from physics_backends import canonical, digest, parse
from physics_grabbing import (attach_kinematic, attachment_pose, compile_release,
                              release_state, rigid_transform)
from rig_attachments import socket_world
from rig_math import decompose
from rig_skeleton import neutral_pose


def build_release_inputs(rig, *, socket_id='grip.right', impulse=(0, 0, 0),
                         angular_impulse=(0, 0, 0), frames=49):
    """Use the actual rig socket, then release a sphere with a measured velocity."""
    world, source = build_inputs(backend='pybullet', frames=frames)
    pose = neutral_pose(rig.skeleton)
    socket = socket_world(rig, pose, socket_id)
    socket_transform = decompose(socket)
    root = rigid_transform(dict(translation=(np.array([0., 3., 0.])-socket[:3, 3]).tolist(),
                                rotation=[0, 0, 0, 1]))
    prop = rigid_transform(dict(translation=[0., 3., 0.], rotation=socket_transform['rotation']))
    # Author the engineering asset's named socket before publishing any source.
    # Preserve the source mesh bytes, and update all exact native document refs.
    asset = parse(source['ball-asset.json'])
    asset['sockets'] = [dict(id='handle', transform=rigid_transform(dict(translation=[0, 0, 0], rotation=[0, 0, 0, 1])))]
    source['ball-asset.json'] = canonical(asset)
    scene = parse(source['physics-scene.json'])
    old_asset = deepcopy(scene['nodes'][0]['asset'])
    asset_ref = {**old_asset, 'sha256': digest(source['ball-asset.json'])}
    scene['nodes'][0]['asset'] = asset_ref
    scene['dependencies'] = [asset_ref if ref == old_asset else ref for ref in scene['dependencies']]
    source['physics-scene.json'] = canonical(scene)
    old_scene = {**world['scene'], 'kind': 'scene3d-scene'}
    scene_ref = {**old_scene, 'sha256': digest(source['physics-scene.json'])}
    world['scene'] = {**scene_ref, 'kind': 'scene'}
    project = parse(source['project.json'])
    for field in ('assets', 'scenes', 'dependency_lock'):
        project[field] = [asset_ref if ref == old_asset else scene_ref if ref == old_scene else ref
                          for ref in project[field]]
    source['project.json'] = canonical(project)
    attached = attach_kinematic(rig, pose, socket_id, root, asset, prop, 'handle')
    current_root = deepcopy(root);current_root['translation'][0] += .1
    carried = attachment_pose(rig, pose, attached['attachment'], current_root)
    release = release_state(dict(time=dict(numerator=0, denominator=1), transform=attached['world_transform']),
                            dict(time=dict(numerator=1, denominator=10), transform=carried),
                            impulse=impulse, angular_impulse=angular_impulse)
    released, payloads, receipt = compile_release(world, source, 'ball', release)
    return released, payloads, dict(compilation=receipt, attachment=attached, release=release,
        character_id=rig.manifest['character']['id'], rig_version=rig.manifest['version'],
        skeleton_version=rig.skeleton['version'], socket_id=socket_id,
        status='uncommitted authored carry and native release inputs; persistent ownership unavailable')
