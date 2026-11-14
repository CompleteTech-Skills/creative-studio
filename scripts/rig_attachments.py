"""Real spatial sockets, object-relative grasps and single-owner handoffs."""
from __future__ import annotations

from copy import deepcopy
import math
import numpy as np
from rig_errors import RigError, require
from rig_ik import solve_ik
from rig_math import decompose, qangle, qinverse, qmatrix, qmul, quaternion, transform_matrix
from rig_skeleton import evaluate_fk


def socket_world(rig, pose, socket_id: str) -> np.ndarray:
    sockets = {s['id']: s for s in rig.manifest['attachments']}
    require(socket_id in sockets, 'RIG_SOCKET', 'Attachment socket is unavailable: '+socket_id)
    socket = sockets[socket_id];world = evaluate_fk(rig.skeleton, pose)
    require(socket['bone'] in world, 'RIG_SOCKET_BONE', 'Socket references a missing bone.')
    return world[socket['bone']] @ transform_matrix(socket['transform'])


def prop_world(rig, pose, prop: dict) -> np.ndarray:
    if prop.get('owner') is None:return transform_matrix(prop['transform'])
    require(prop['owner'] == rig.manifest['character']['id'], 'RIG_PROP_OWNER', 'Prop belongs to a different character.')
    return socket_world(rig, pose, prop['socket']) @ transform_matrix(prop['transform'])


def attach_prop(rig, pose, prop: dict, socket_id: str, *, expected_owner=None, keep_world=True) -> dict:
    require(prop.get('owner') == expected_owner, 'RIG_PROP_OWNER', 'Prop owner changed; an explicit handoff is required.')
    socket = socket_world(rig, pose, socket_id)
    current = prop_world(rig, pose, prop) if prop.get('owner') is not None else transform_matrix(prop['transform'])
    result = deepcopy(prop)
    result.update(owner=rig.manifest['character']['id'], socket=socket_id,
                  transform=decompose(np.linalg.inv(socket) @ current) if keep_world else decompose(np.eye(4)))
    return result


def detach_prop(rig, pose, prop: dict) -> dict:
    matrix = prop_world(rig, pose, prop);result = deepcopy(prop)
    result.update(owner=None, socket=None, transform=decompose(matrix))
    return result


def _prop_grip_matrix(grip):
    require(isinstance(grip, dict) and set(grip) == {'translation', 'rotation', 'scale'},
            'RIG_HANDOFF_GRIP', 'A prop-local grip requires complete translation, rotation and scale.')
    for field, size in [('translation', 3), ('rotation', 4), ('scale', 3)]:
        values = grip[field]
        require(isinstance(values, (list, tuple)) and len(values) == size
                and all(type(value) in (int, float) and abs(value) <= 1e9
                        and math.isfinite(value) for value in values),
                'RIG_HANDOFF_GRIP', 'Prop-local grip '+field+' must be a finite native numeric vector.')
    try:
        # The canonical math owner checks finite vectors and positive,
        # nonsingular scale. Grip quaternions follow the strict native contract.
        quaternion(grip['rotation'], strict=True)
        return transform_matrix(grip)
    except RigError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise RigError('RIG_HANDOFF_GRIP', 'Malformed prop-local grip transform.') from exc


def transfer_prop(source, source_pose, target, target_pose, prop: dict, target_socket: str, *, tolerance=.01,
                  source_grip=None, target_grip=None, orientation_tolerance=.01) -> dict:
    """Transfer one owner without moving the prop.

    Without explicit grips the established coincident-socket contact check is
    retained. Paired grips are complete object-local TRS transforms: each hand
    must meet its own prop grip within ``tolerance`` meters and
    ``orientation_tolerance`` radians. The hands need not coincide.
    """
    require(prop.get('owner') == source.manifest['character']['id'], 'RIG_PROP_OWNER', 'Source character does not own this prop.')
    require((source_grip is None) == (target_grip is None), 'RIG_HANDOFF_GRIP',
            'Supply both prop-local grips together, or neither for legacy contact.')
    current_socket = socket_world(source, source_pose, prop['socket']);destination = socket_world(target, target_pose, target_socket)
    if source_grip is None:
        error = float(np.linalg.norm(current_socket[:3, 3]-destination[:3, 3]))
        require(error <= tolerance, 'RIG_HANDOFF_CONTACT', 'Handoff hands do not meet in world space.', expected=tolerance, observed=error)
        world = prop_world(source, source_pose, prop)
    else:
        for name, limit in [('position', tolerance), ('orientation', orientation_tolerance)]:
            require(type(limit) in (int, float) and 0 <= limit <= 1e9 and math.isfinite(limit),
                    'RIG_HANDOFF_TOLERANCE', 'Paired-grip '+name+' tolerance must be finite and within [0, 1e9].')
        world = prop_world(source, source_pose, prop)
        for side, actual, grip in [('source', current_socket, source_grip), ('target', destination, target_grip)]:
            expected = world @ _prop_grip_matrix(grip)
            position_error = float(np.linalg.norm(actual[:3, 3]-expected[:3, 3]))
            angle_error = qangle(decompose(actual)['rotation'], decompose(expected)['rotation'])
            require(position_error <= tolerance and angle_error <= orientation_tolerance,
                    'RIG_HANDOFF_CONTACT', 'Handoff '+side+' hand does not meet its prop-local grip.',
                    expected=dict(position_m=tolerance, orientation_rad=orientation_tolerance),
                    observed=dict(position_m=position_error, orientation_rad=angle_error))
    result = deepcopy(prop)
    result.update(owner=target.manifest['character']['id'], socket=target_socket,
                  transform=decompose(np.linalg.inv(destination) @ world))
    return result


def solve_grasp(rig, pose, chain_id: str, socket_id: str, object_transform: dict, grip_transform: dict):
    sockets = {s['id']: s for s in rig.manifest['attachments']}
    chains = {c['id']: c for c in rig.manifest['controllers']['ik']}
    require(socket_id in sockets and chain_id in chains, 'RIG_GRASP_REFERENCE', 'Grasp needs a real hand socket and IK chain.')
    socket = sockets[socket_id];chain = chains[chain_id]
    require(chain['bones'][-1] == socket['bone'], 'RIG_GRASP_REFERENCE', 'Socket must belong to the IK endpoint.')
    grip = transform_matrix(object_transform) @ transform_matrix(grip_transform)
    desired_q = qmul(decompose(grip)['rotation'], qinverse(socket['transform']['rotation']))
    hand_target = grip[:3, 3]-qmatrix(desired_q) @ np.asarray(socket['transform']['translation'])
    result = solve_ik(rig.skeleton, pose, chain, hand_target, orientation=desired_q,
                      constraints=rig.manifest['controllers']['constraints'])
    actual = socket_world(rig, result.pose, socket_id)
    return dict(pose=result.pose, residual_m=float(np.linalg.norm(actual[:3, 3]-grip[:3, 3])),
                converged=result.converged, reachable=result.reachable, grip_world=grip.tolist(), hand_world=actual.tolist())
