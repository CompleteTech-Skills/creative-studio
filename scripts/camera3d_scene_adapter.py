"""Read-only camera targets over native Scene3D and rig evaluation.

No scene hierarchy, animation solver, skeleton, or asset importer is duplicated.
A snapshot binds one exact scene document and one evaluated state. Static native
scenes use scene3d_math; edited/animated scenes must supply their native owner.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from itertools import product

import numpy as np

from camera3d_contracts import validate as validate_camera
from rig_errors import decode_json
from rig_math import transform_points, vector
from scene3d_contracts import canonical_bytes, validate_document


class TargetError(ValueError):
    """A requested camera target cannot be resolved from its exact scene."""


def _affine(value):
    try:
        result = np.asarray(value, dtype=float)
    except (ValueError, TypeError, OverflowError) as exc:
        raise TargetError('Expected finite affine target transform.') from exc
    if (result.shape != (4, 4) or not np.isfinite(result).all()
            or not np.allclose(result[3], [0, 0, 0, 1], atol=1e-10, rtol=0)):
        raise TargetError('Expected finite affine target transform.')
    return result


class SceneTargets:
    """Exact-reference target resolver, retaining native evaluated matrices.

    ``rigs`` maps scene node IDs to ``(RigAsset, evaluated_pose)``; bone and socket
    transforms are evaluated by rig_skeleton and rig_attachments. A missing rig,
    bone, or socket is an error, never a node-origin fallback.
    """

    def __init__(self, scene, states, *, scene_path, scene_bytes=None, rigs=None):
        scene = validate_document(deepcopy(scene), 'scene')
        raw = canonical_bytes(scene) if scene_bytes is None else scene_bytes
        if not isinstance(raw, bytes):
            raise TargetError('Scene bytes must be immutable bytes.')
        try:
            same = validate_document(decode_json(raw), 'scene') == scene
        except ValueError as exc:
            raise TargetError('Scene bytes are not a valid native scene.') from exc
        if not same:
            raise TargetError('Scene bytes do not describe the supplied scene.')
        from camera3d_contracts import portable_path
        portable_path(scene_path)
        self.reference = dict(id=scene['id'], version=scene['version'], path=scene_path,
                              sha256=hashlib.sha256(raw).hexdigest(), kind='scene3d-scene')
        self.scene = deepcopy(scene)
        self.nodes = {node['id']: node for node in scene['nodes']}
        if not isinstance(states, dict) or set(states) != set(self.nodes):
            raise TargetError('Evaluated state must cover each native scene node exactly once.')
        self.states = deepcopy(states)
        for state in self.states.values():
            _affine(state['world_matrix'])
        self.rigs = dict(rigs or {})
        if not set(self.rigs) <= set(self.nodes):
            raise TargetError('Rig bindings contain an unknown scene node.')

    @classmethod
    def from_scene(cls, scene, *, scene_path='scene.json', scene_bytes=None, rigs=None):
        from scene3d_math import evaluate_transforms
        states = evaluate_transforms(scene)  # rejects unevaluated dynamic declarations
        for node in scene['nodes']:
            bounds = node['bounds']
            if bounds is not None:
                corners = np.array(list(product(*zip(bounds['minimum'], bounds['maximum']))))
                points = transform_points(np.asarray(states[node['id']]['world_matrix']), corners)
                states[node['id']]['world_bounds'] = dict(minimum=points.min(axis=0).tolist(),
                                                          maximum=points.max(axis=0).tolist())
            else:
                states[node['id']]['world_bounds'] = None
        return cls(scene, states, scene_path=scene_path, scene_bytes=scene_bytes, rigs=rigs)

    @classmethod
    def from_graph(cls, graph, *, scene_path='scene.json', scene_bytes=None, rigs=None):
        """Use SceneGraph's canonical base evaluator, without claiming animation."""
        from scene3d_graph import SceneGraph
        if not isinstance(graph, SceneGraph):
            raise TargetError('Expected the native SceneGraph owner.')
        return cls(graph.document(), graph.evaluate_base(), scene_path=scene_path,
                   scene_bytes=scene_bytes, rigs=rigs)

    def resolve(self, target):
        target = validate_camera(deepcopy(target), 'target')
        if target['scene'] != self.reference:
            raise TargetError('Target scene reference differs from the exact evaluated scene.')
        identifier = target['node']
        if identifier not in self.nodes:
            raise TargetError('Missing camera target node: ' + identifier)
        world = _affine(self.states[identifier]['world_matrix'])
        attachment = target['attachment']
        space = target['space']
        if space in ('bone', 'socket'):
            if identifier not in self.rigs:
                raise TargetError('Camera target requires a bound native rig: ' + identifier)
            rig, pose = self.rigs[identifier]
            from rig_skeleton import evaluate_fk, validate_skeleton
            validate_skeleton(rig.skeleton, rig.manifest.get('limits'))
            if space == 'bone':
                bone_world = evaluate_fk(rig.skeleton, pose)
                if attachment not in bone_world:
                    raise TargetError('Missing camera target bone: ' + str(attachment))
                world = world @ bone_world[attachment]
            else:
                from rig_attachments import socket_world
                if attachment not in {s['id'] for s in rig.manifest['attachments']}:
                    raise TargetError('Missing camera target socket: ' + str(attachment))
                world = world @ socket_world(rig, pose, attachment)
        offset = vector(target['offset_m'])
        point = world[:3, 3] + offset if space == 'world' else transform_points(world, offset)
        if not np.isfinite(point).all():
            raise TargetError('Camera target transform overflowed.')
        bounds = self.states[identifier].get('world_bounds')
        return dict(id=target['id'], node=identifier, position=point.tolist(),
                    world_matrix=world.tolist(), bounds=deepcopy(bounds),
                    attachment=attachment, space=space)

    def __call__(self, target):
        return self.resolve(target)
