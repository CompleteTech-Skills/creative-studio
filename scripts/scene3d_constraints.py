"""Transactional placement and attachment helpers for SceneGraph.

Graph's existing parent, copy-transform and look-at evaluators remain the single
constraint authority. These helpers author validated changes into that graph;
no second persistent constraint dialect is introduced.
"""
from copy import deepcopy
import math
import numpy as np

from rig_math import compose, matrix_quaternion, quaternion
from scene3d_graph import require, transform_for_matrix, _inverse
from scene3d_spatial import _vec, _positive, _box, snap_grid, snap_scale


def snap_rotation(rotation, step_radians, *, axis=(0,1,0)):
    """Snap a rotation about a declared local axis; reject other-axis rotation."""
    q = np.asarray(quaternion(rotation,strict=True),dtype=float)
    # q and -q must quantize to the same physical orientation.
    if q[3] < 0 or (q[3] == 0 and next((x for x in q[:3] if x != 0),1) < 0):
        q = -q
    axis = _vec(axis)
    magnitude = np.max(np.abs(axis))
    require(magnitude > 0, 'Rotation snap axis is zero.', 'SCENE3D_SPATIAL')
    axis = axis/magnitude
    axis /= np.linalg.norm(axis)
    step = _positive(step_radians,'rotation step')
    parallel = np.dot(q[:3],axis)
    require(np.linalg.norm(q[:3]-parallel*axis) < 1e-8,
            'Rotation is not about the declared snap axis.', 'SCENE3D_SPATIAL')
    angle = 2*math.atan2(parallel,q[3])
    angle = math.copysign(math.floor(abs(angle)/step+0.5)*step,angle)
    return [*(axis*math.sin(angle/2)).tolist(), math.cos(angle/2)]


def snap_transform(graph, identifier, *, space, grid=None, origin=(0,0,0), scale_step=None,
                   rotation_step=None, rotation_axis=(0,1,0), expected_revision=None):
    require(space in ('world','local'), 'Declare local or world snap space.', 'SCENE3D_SPATIAL')
    transform = graph.transform(identifier,space=space)
    if space == 'world':
        transform = transform_for_matrix(transform)
    if grid is not None:
        transform['translation'] = snap_grid(transform['translation'],grid,origin=origin)
    if scale_step is not None:
        transform['scale'] = snap_scale(transform['scale'],scale_step)
    if rotation_step is not None:
        transform['rotation'] = snap_rotation(transform['rotation'],rotation_step,axis=rotation_axis)
    value = compose(**{k:transform[k] for k in ('translation','rotation','scale')}) if space == 'world' else transform
    return graph.set_transform(identifier,value,space=space,expected_revision=expected_revision)


def attach(graph, identifier, owner, socket, *, preserve_world=True, expected_revision=None):
    """Attach to a named authored socket; preserve exact world pose by default."""
    require(type(preserve_world) is bool,'Invalid preserve-world flag.','SCENE3D_SPATIAL')
    node = graph._editable(identifier)
    nodes = graph._nodes()
    require(owner in nodes and socket in nodes and nodes[socket]['type']=='socket' and
            nodes[socket]['parent']==owner, 'Socket does not belong to owner.', 'SCENE3D_SPATIAL')
    require(not node['constraints'], 'Resolve constraints before attaching.', 'SCENE3D_SPATIAL')
    world = np.asarray(graph.evaluate_base(identifier)['world_matrix'])
    socket_world = np.asarray(graph.evaluate_base(socket)['world_matrix'])
    candidate = graph.document()
    target = next(n for n in candidate['nodes'] if n['id']==identifier)
    # Identity node local pose; the offset owns the attachment relationship.
    target['transform'] = dict(translation=[0,0,0],rotation=[0,0,0,1],scale=[1,1,1])
    offset = transform_for_matrix(_inverse(socket_world)@world) if preserve_world else deepcopy(target['transform'])
    target['attachment'] = dict(owner=owner,socket=socket,offset=offset)
    return graph._commit(candidate,expected_revision=expected_revision)


def detach(graph, identifier, *, preserve_world=True, expected_revision=None):
    require(type(preserve_world) is bool,'Invalid preserve-world flag.','SCENE3D_SPATIAL')
    node = graph._editable(identifier)
    require('attachment' in node, 'Node is not attached.', 'SCENE3D_SPATIAL')
    require(not node['constraints'], 'Resolve constraints before detaching.', 'SCENE3D_SPATIAL')
    world = np.asarray(graph.evaluate_base(identifier)['world_matrix'])
    parent = np.asarray(graph.evaluate_base(node['parent'])['world_matrix']) if node['parent'] else np.eye(4)
    candidate = graph.document()
    target = next(n for n in candidate['nodes'] if n['id']==identifier)
    del target['attachment']
    if preserve_world:
        target['transform'] = transform_for_matrix(_inverse(parent)@world)
    return graph._commit(candidate,expected_revision=expected_revision)


def set_constraint(graph, identifier, constraint, *, expected_revision=None):
    """Use the existing validated copy-transform/look-at/socket constraint model."""
    graph._editable(identifier)
    candidate = graph.document()
    target = next(n for n in candidate['nodes'] if n['id']==identifier)
    require(isinstance(constraint,dict) and 'id' in constraint,'Constraint requires ID.','SCENE3D_SPATIAL')
    target['constraints'] = [c for c in target['constraints'] if c['id']!=constraint['id']]+[deepcopy(constraint)]
    return graph._commit(candidate,expected_revision=expected_revision)


def sample_path(points, fraction):
    """Arc-length linear path in explicit world coordinates (no extrapolation)."""
    from scene3d_path import path_lengths
    require(isinstance(points,(list,tuple)) and 2 <= len(points) <= 10000,
            'Path requires 2..10000 points.','SCENE3D_SPATIAL')
    require(isinstance(fraction,(int,float)) and not isinstance(fraction,bool) and
            math.isfinite(fraction) and 0<=fraction<=1, 'Path fraction is outside [0,1].','SCENE3D_SPATIAL')
    points = np.array([_vec(p) for p in points])
    lengths,total = path_lengths(points.tolist())
    distance = fraction*total
    for a,b,length in zip(points,points[1:],lengths):
        if length>1e-10 and distance<=length:
            return (a+(b-a)*distance/length).tolist()
        distance -= length
    return points[-1].tolist()


def place(graph, index, identifier, candidates, *, clearance=0, expected_revision=None):
    """Try bounded world positions; reject conservative AABB collisions atomically.

    This is a deterministic broad-phase placement policy, not a mesh collision
    solver. A failed search leaves the authored graph and revision untouched.
    """
    node = graph._editable(identifier)
    require(not node['constraints'] and 'attachment' not in node,
            'Resolve dynamic relationships before placement.','SCENE3D_SPATIAL')
    require(isinstance(candidates,(list,tuple)) and 1<=len(candidates)<=1000,
            'Placement requires 1..1000 candidates.','SCENE3D_SPATIAL_LIMIT')
    candidates = [_vec(p) for p in candidates]
    clearance = _positive(clearance,'placement clearance',zero=True)
    transform = transform_for_matrix(graph.transform(identifier,space='world'))
    current = np.asarray(transform['translation'])
    # Whole subtree moves with the owner and must not collide against itself.
    from scene3d_graph import _subtree
    excluded = _subtree(graph._nodes(),identifier)
    nodes = graph._nodes()
    require(all(not nodes[i]['constraints'] and 'attachment' not in nodes[i] for i in excluded),
            'Resolve driven descendants before rigid-subtree placement.', 'SCENE3D_SPATIAL')
    index._refresh()
    boxes = [_box(index.bounds(i)) for i in excluded if i in index._records]
    require(bool(boxes), 'Placement subtree has no geometry.', 'SCENE3D_PLACEMENT')
    low = np.min([b[0] for b in boxes],axis=0)
    high = np.max([b[1] for b in boxes],axis=0)
    for candidate in candidates:
        delta = candidate-current
        box = dict(minimum=(low+delta).tolist(),maximum=(high+delta).tolist())
        if not index.overlap(box,clearance=clearance,exclude=excluded):
            transform['translation'] = candidate.tolist()
            graph.set_transform(identifier,compose(**{k:transform[k] for k in ('translation','rotation','scale')}),space='world',expected_revision=expected_revision)
            return dict(position=candidate.tolist(),policy='world-aabb',revision=graph.revision)
    require(False,'No collision-free candidate in bounded placement search.','SCENE3D_PLACEMENT')


def snap_surface(graph,index,identifier,origin,*,max_distance=1e6,offset=0,align=True,expected_revision=None):
    """Place the node origin on the first upward triangle, with world normal offset."""
    graph._editable(identifier)
    require(type(align) is bool,'Invalid alignment flag.','SCENE3D_SPATIAL')
    require(isinstance(offset,(int,float)) and not isinstance(offset,bool) and math.isfinite(offset),
            'Invalid surface offset.','SCENE3D_SPATIAL')
    from scene3d_graph import _subtree
    hit = index.ground(origin,max_distance=max_distance,exclude=_subtree(graph._nodes(),identifier),min_up_dot=0)
    require(hit is not None,'No upward support surface beneath origin.','SCENE3D_PLACEMENT')
    transform = transform_for_matrix(graph.transform(identifier,space='world'))
    normal = np.asarray(hit['normal'])
    transform['translation'] = (np.asarray(hit['point'])+normal*offset).tolist()
    if align:
        # Preserve heading by projecting current local X into the tangent plane.
        matrix = compose(**{k:transform[k] for k in ('translation','rotation','scale')})
        x = matrix[:3,0]-normal*np.dot(matrix[:3,0],normal)
        if np.linalg.norm(x)<1e-8:
            x = np.cross(normal,[0,0,1] if abs(normal[2])<.9 else [1,0,0])
        x /= np.linalg.norm(x)
        z = np.cross(x,normal)
        transform['rotation'] = matrix_quaternion(np.column_stack([x,normal,z])).tolist()
    graph.set_transform(identifier,compose(**{k:transform[k] for k in ('translation','rotation','scale')}),space='world',expected_revision=expected_revision)
    return hit


def snap_bounds(graph,index,identifier,target,*,axis,side='after',gap=0,expected_revision=None):
    """Translate a world AABB face to a target face along a declared XYZ axis."""
    require(type(axis) is int and axis in (0,1,2), 'Bounds snap axis must be 0, 1 or 2.', 'SCENE3D_SPATIAL')
    require(side in ('before','after'), 'Bounds snap side must be before or after.', 'SCENE3D_SPATIAL')
    gap = _positive(gap,'bounds gap',zero=True)
    require(identifier!=target, 'Cannot snap bounds to self.', 'SCENE3D_SPATIAL')
    from scene3d_graph import _dependencies
    nodes = graph._nodes()
    require(target in nodes, 'Unknown bounds target.', 'SCENE3D_SPATIAL')
    visited, pending = set(), [target]
    while pending:
        dependency = pending.pop()
        require(dependency != identifier, 'Bounds target moves with the owner.', 'SCENE3D_SPATIAL')
        if dependency not in visited:
            visited.add(dependency)
            pending.extend(_dependencies(nodes[dependency]))
    low,high = _box(index.bounds(identifier)); target_low,target_high = _box(index.bounds(target))
    delta = target_high[axis]+gap-low[axis] if side=='after' else target_low[axis]-gap-high[axis]
    matrix = np.asarray(graph.transform(identifier,space='world'),dtype=float)
    matrix[axis,3] += delta
    return graph.set_transform(identifier,matrix,space='world',expected_revision=expected_revision)


def follow_path(graph,identifier,points,fraction,*,expected_revision=None):
    """Author one explicit world-space path sample using the graph revision gate.

    Time sampling/persistence remains the animation owner's responsibility. This
    function does not claim a static node will continue moving after it returns.
    """
    matrix = np.asarray(graph.transform(identifier,space='world'),dtype=float)
    matrix[:3,3] = sample_path(points,fraction)
    return graph.set_transform(identifier,matrix,space='world',expected_revision=expected_revision)
