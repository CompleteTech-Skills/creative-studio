"""Deterministic local joint limits and world-space aim/copy constraints."""
from __future__ import annotations

from copy import deepcopy
import math
import numpy as np
from rig_contracts import validate_contract
from rig_errors import require
from rig_math import (IDENTITY_Q, axis_angle, decompose, qangle, qbetween, qinverse, qmul,
                      quaternion, signed_twist, slerp, swing_twist, transform_points, unit, vector)
from rig_skeleton import evaluate_fk, hierarchy_order


def constraint_order(skeleton: dict, constraints: list[dict]) -> list[dict]:
    bones = {b['id']: b for b in skeleton['bones']}
    graph = {name: ([b['parent']] if b['parent'] else []) for name, b in bones.items()}
    ids = set()
    for c in constraints:
        validate_contract(c, 'JointConstraint')
        require(c['id'] not in ids, 'RIG_CONSTRAINT_ID', 'Constraint IDs must be unique.')
        ids.add(c['id'])
        require(c['bone'] in bones, 'RIG_CONSTRAINT_REFERENCE', 'Constraint bone is missing.')
        if c['type'] in ('hinge', 'swing-twist', 'translation'):
            require('minimum' in c and 'maximum' in c and c['minimum'] <= c['maximum'],
                    'RIG_CONSTRAINT_RANGE', 'Constraint limits must be ordered.')
        if c['type'] in ('hinge', 'swing-twist', 'aim'):
            unit(c.get('axis', [0, 1, 0]))
        if c['type'] in ('ball', 'swing-twist'):
            require(0 < c.get('swing', math.pi) <= math.pi, 'RIG_CONSTRAINT_RANGE', 'Swing limit must be within pi radians.')
        if c['type'] in ('copy-position', 'copy-rotation') or (c['type'] == 'aim' and 'target' in c):
            require(c.get('target') in bones, 'RIG_CONSTRAINT_REFERENCE', 'Constraint target is missing.')
            graph[c['bone']].append(c['target'])
        if c['type'] == 'aim':
            require('target' in c or 'point' in c, 'RIG_CONSTRAINT_REFERENCE', 'Aim needs a real spatial target.')
    active, done, order = set(), set(), []
    def visit(name):
        require(name not in active, 'RIG_CONSTRAINT_CYCLE', 'Constraint dependency graph has a cycle.', asset=name)
        if name in done:return
        active.add(name)
        for dependency in graph[name]:visit(dependency)
        active.remove(name);done.add(name);order.append(name)
    for name in hierarchy_order(skeleton):visit(name)
    priority = {name: i for i, name in enumerate(order)}
    return sorted(constraints, key=lambda c: (priority[c['bone']], c['type'] in ('hinge', 'ball', 'swing-twist', 'locked'), c['id']))


def apply_joint_limits(skeleton: dict, pose: dict, constraints: list[dict]) -> tuple[dict, list[dict]]:
    """Project hard local limits without replaying fractional world controls.

    A blend between legal endpoint rotations can cross a forbidden joint range.
    Use the same native limit implementations after such a blend; aim and copy
    controls retain their already evaluated influence instead of running twice.
    """
    limits = [item for item in constraint_order(skeleton, constraints)
              if item['type'] in ('hinge', 'ball', 'swing-twist', 'locked', 'translation')]
    return apply_constraints(skeleton, pose, limits, ordered=True)


def apply_constraints(skeleton: dict, pose: dict, constraints: list[dict], *, ordered=False) -> tuple[dict, list[dict]]:
    result = deepcopy(pose)
    bones = {b['id']: b for b in skeleton['bones']}
    changes = []
    for c in constraints if ordered else constraint_order(skeleton, constraints):
        bone = bones[c['bone']]
        values = result['bones'].setdefault(bone['id'], {})
        before = dict(values)
        rest_q = bone['rest']['rotation']
        q = values.get('rotation', rest_q)
        delta = quaternion(qmul(qinverse(rest_q), q))
        axis = c.get('axis', [0, 1, 0])
        kind = c['type']
        if kind in ('hinge', 'swing-twist'):
            swing, twist = swing_twist(delta, axis)
            angle = np.clip(signed_twist(twist, axis), c['minimum'], c['maximum'])
            if kind == 'hinge':swing = np.array(IDENTITY_Q)
            elif qangle(swing) > c.get('swing', math.pi):
                swing = slerp(IDENTITY_Q, swing, c['swing']/qangle(swing))
            values['rotation'] = quaternion(qmul(rest_q, qmul(swing, axis_angle(axis, float(angle))))).tolist()
        elif kind == 'ball':
            angle = qangle(delta);maximum = c.get('swing', math.pi)
            if angle > maximum:values['rotation'] = quaternion(qmul(rest_q, slerp(IDENTITY_Q, delta, maximum/angle))).tolist()
        elif kind == 'locked':
            if delta[3] < 0:delta = -delta
            angle = qangle(delta)
            v = unit(delta[:3])*angle if np.linalg.norm(delta[:3]) > 1e-9 else np.zeros(3)
            v[c.get('axes', [0, 1, 2])] = 0
            limited = axis_angle(v, float(np.linalg.norm(v))) if np.linalg.norm(v) > 1e-9 else IDENTITY_Q
            values['rotation'] = quaternion(qmul(rest_q, limited)).tolist()
        elif kind == 'translation':
            v = vector(values.get('translation', bone['rest']['translation']))-vector(bone['rest']['translation'])
            axes = c.get('axes', [0, 1, 2]);v[axes] = np.clip(v[axes], c['minimum'], c['maximum'])
            values['translation'] = (v+vector(bone['rest']['translation'])).tolist()
        else:
            world = evaluate_fk(skeleton, result)
            parent_world = world[bone['parent']] if bone['parent'] else np.eye(4)
            current = decompose(world[bone['id']])
            factor = c.get('influence', 1.)
            if kind == 'copy-position':
                target = world[c['target']][:3, 3]
                location = current['translation']*(1-factor) + target*factor if isinstance(current['translation'], np.ndarray) else np.array(current['translation'])*(1-factor)+target*factor
                values['translation'] = transform_points(np.linalg.inv(parent_world), np.array([location]))[0].tolist()
            else:
                if kind == 'copy-rotation':desired = decompose(world[c['target']])['rotation']
                else:
                    target = world[c['target']][:3, 3] if 'target' in c else vector(c['point'])
                    direction = target-world[bone['id']][:3, 3]
                    require(np.linalg.norm(direction) > 1e-8, 'RIG_AIM_TARGET', 'Aim target coincides with the bone head.')
                    current_axis = world[bone['id']][:3, :3] @ unit(axis)
                    desired = qmul(qbetween(current_axis, direction), current['rotation'])
                desired = slerp(current['rotation'], desired, factor)
                parent_q = decompose(parent_world)['rotation']
                values['rotation'] = quaternion(qmul(qinverse(parent_q), desired)).tolist()
        changed = any(not np.allclose(value, before.get(key, bone['rest'][key]), atol=1e-8)
                      for key, value in values.items())
        if changed:changes.append(dict(constraint=c['id'], bone=bone['id'], type=kind))
    return result, changes
