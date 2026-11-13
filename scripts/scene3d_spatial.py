"""Bounded geometric queries on the authoritative SceneGraph base pose.

Triangle data are explicit node-local geometry, never substituted from a bounding
box. All public distances, rays and plane equations use canonical world metres.
Queries snapshot a revision and return stable node-id/triangle-index ordering.
Animated/deformed callers must supply their evaluated vertices explicitly.
"""
from __future__ import annotations

from itertools import product
import math
import numpy as np

from scene3d_graph import require, _affine

MAX_VERTICES = 1_000_000
MAX_TRIANGLES = 1_000_000
MAX_QUERY_TESTS = 2_000_000
EPS = 1e-10


def _vec(value, label='vector'):
    try:
        result = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError):
        require(False, 'Invalid '+label, 'SCENE3D_SPATIAL')
    require(result.shape == (3,) and np.isfinite(result).all(),
            'Expected a finite three-vector for '+label, 'SCENE3D_SPATIAL')
    return result


def _positive(value, label, *, zero=False):
    require(not isinstance(value, bool) and isinstance(value, (int, float)) and
            math.isfinite(value) and (value >= 0 if zero else value > 0),
            'Invalid '+label, 'SCENE3D_SPATIAL')
    return float(value)


def _box(value):
    require(isinstance(value, dict) and set(value) == {'minimum', 'maximum'},
            'Expected minimum/maximum bounds.', 'SCENE3D_SPATIAL')
    low, high = _vec(value['minimum']), _vec(value['maximum'])
    require(np.all(low <= high), 'Inverted spatial bounds.', 'SCENE3D_SPATIAL')
    return low, high


def _bounds(points):
    return dict(minimum=points.min(axis=0).tolist(), maximum=points.max(axis=0).tolist())


def _overlap(a, b, clearance=0):
    return bool(np.all(a[0] <= b[1]+clearance) and np.all(b[0] <= a[1]+clearance))


def _ray_box(origin, direction, low, high, limit):
    near, far = 0., limit
    for i in range(3):
        if abs(direction[i]) < EPS:
            if origin[i] < low[i] or origin[i] > high[i]:
                return False
        else:
            a, b = sorted(((low[i]-origin[i])/direction[i], (high[i]-origin[i])/direction[i]))
            near, far = max(near, a), min(far, b)
            if near > far:
                return False
    return True


def _closest_triangle(point, triangle):
    # Closest point in triangle Voronoi regions, including degenerate edges.
    a, b, c = triangle
    if np.linalg.norm(np.cross(b-a,c-a)) < EPS:
        candidates = []
        for u,v in ((a,b),(b,c),(c,a)):
            edge = v-u
            denom = np.dot(edge,edge)
            candidates.append(u if denom < EPS else u+np.clip(np.dot(point-u,edge)/denom,0,1)*edge)
        return min(candidates,key=lambda p:np.dot(p-point,p-point))
    ab, ac, ap = b-a, c-a, point-a
    d1, d2 = np.dot(ab, ap), np.dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return a
    bp = point-b
    d3, d4 = np.dot(ab, bp), np.dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return b
    vc = d1*d4-d3*d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        return a+(d1/(d1-d3))*ab
    cp = point-c
    d5, d6 = np.dot(ab, cp), np.dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return c
    vb = d5*d2-d1*d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        return a+(d2/(d2-d6))*ac
    va = d3*d6-d5*d4
    if va <= 0 and d4-d3 >= 0 and d5-d6 >= 0:
        return b+((d4-d3)/((d4-d3)+(d5-d6)))*(c-b)
    total = va+vb+vc
    if abs(total) < EPS:
        candidates = []
        for u, v in ((a,b),(b,c),(c,a)):
            edge = v-u
            denom = np.dot(edge,edge)
            candidates.append(u if denom < EPS else u+np.clip(np.dot(point-u,edge)/denom,0,1)*edge)
        return min(candidates, key=lambda p: np.dot(p-point,p-point))
    return a+ab*(vb/total)+ac*(vc/total)


class SpatialIndex:
    """Revision-aware, exact-triangle query adapter over a SceneGraph.

    ``meshes`` maps node IDs to {vertices: Nx3, triangles: Mx3 integer indices}.
    Inputs are copied; unknown nodes and unbounded arrays are rejected. AABB
    queries are deliberately broad phase. Ray/nearest/ground queries require
    triangles and never report a box as a mesh hit.
    """
    def __init__(self, graph, meshes=None):
        self.graph = graph
        require(isinstance(meshes, dict) or meshes is None, 'Expected mesh mapping.', 'SCENE3D_SPATIAL')
        ids = {n['id'] for n in graph.document()['nodes']}
        self.meshes = {}
        total_vertices = total_triangles = 0
        for identifier, mesh in (meshes or {}).items():
            require(identifier in ids and isinstance(mesh, dict) and set(mesh) == {'vertices','triangles'},
                    'Invalid mesh node/fields.', 'SCENE3D_SPATIAL')
            require(isinstance(mesh['vertices'], (list,tuple,np.ndarray)) and
                    isinstance(mesh['triangles'], (list,tuple,np.ndarray)), 'Invalid mesh arrays.', 'SCENE3D_SPATIAL')
            total_vertices += len(mesh['vertices'])
            total_triangles += len(mesh['triangles'])
            require(total_vertices <= MAX_VERTICES and total_triangles <= MAX_TRIANGLES,
                    'Spatial geometry budget exceeded.', 'SCENE3D_SPATIAL_LIMIT')
            try:
                vertices = np.asarray(mesh['vertices'], dtype=float)
                raw_indices = np.asarray(mesh['triangles'])
            except (TypeError, ValueError, OverflowError):
                require(False, 'Invalid mesh arrays.', 'SCENE3D_SPATIAL')
            require(vertices.ndim == 2 and vertices.shape[1] == 3 and len(vertices) > 0 and
                    np.isfinite(vertices).all(), 'Invalid mesh vertices.', 'SCENE3D_SPATIAL')
            require(raw_indices.ndim == 2 and raw_indices.shape[1] == 3 and len(raw_indices) > 0 and
                    raw_indices.dtype.kind in 'iu' and np.all(raw_indices >= 0) and
                    np.all(raw_indices < len(vertices)), 'Invalid triangle indices.', 'SCENE3D_SPATIAL')
            self.meshes[identifier] = (vertices.copy(), raw_indices.astype(np.int64,copy=True))
        self._revision = None
        self._records = {}

    def _refresh(self):
        # Isolate flags can change without revision, so always sample visibility.
        values = self.graph.evaluate_base()
        if self._revision != self.graph.revision:
            nodes = {n['id']: n for n in self.graph.document()['nodes']}
            records = {}
            for identifier, node in nodes.items():
                matrix = _affine(values[identifier]['world_matrix'])
                triangles = None
                if identifier in self.meshes:
                    vertices, indices = self.meshes[identifier]
                    world = vertices @ matrix[:3,:3].T + matrix[:3,3]
                    require(np.isfinite(world).all(), 'World mesh overflow.', 'SCENE3D_SPATIAL')
                    triangles = world[indices]
                    box = _bounds(world)
                    local = _bounds(vertices)
                else:
                    local = node['bounds']
                    if local is None and 'asset' in node:
                        local = self.graph._asset_bounds.get(node['asset']['id']+'@'+node['asset']['version'])
                    if local is None:
                        continue
                    low, high = _box(local)
                    corners = np.asarray(list(product(*zip(low,high))))
                    box = _bounds(corners @ matrix[:3,:3].T + matrix[:3,3])
                records[identifier] = dict(matrix=matrix, local=local, box=box, triangles=triangles)
            self._records, self._revision = records, self.graph.revision
        self._values = values

    def _chosen(self, exclude=(), visibility='all', *, triangles=False):
        self._refresh()
        require(isinstance(exclude,(tuple,list,set,frozenset)) and all(isinstance(x,str) for x in exclude),
                'Invalid excluded nodes.', 'SCENE3D_SPATIAL')
        require(visibility in ('all','editor','render'), 'Invalid visibility policy.', 'SCENE3D_SPATIAL')
        return [(i,r) for i,r in sorted(self._records.items()) if i not in exclude and
                (visibility == 'all' or self._values[i][visibility+'_visible']) and
                (not triangles or r['triangles'] is not None)]

    def bounds(self, identifier):
        self._refresh()
        require(identifier in self._records, 'Node has no geometric bounds.', 'SCENE3D_SPATIAL')
        return {k:list(v) for k,v in self._records[identifier]['box'].items()}

    def obb(self, identifier):
        """Return exact transformed local bounds as a parallelepiped (shear retained)."""
        self._refresh()
        require(identifier in self._records, 'Node has no geometric bounds.', 'SCENE3D_SPATIAL')
        record = self._records[identifier]
        low, high = _box(record['local'])
        matrix = record['matrix']
        return dict(center=(matrix[:3,:3] @ ((low+high)/2)+matrix[:3,3]).tolist(),
                    half_axes=(matrix[:3,:3] @ np.diag((high-low)/2)).T.tolist())

    def overlap(self, bounds, *, clearance=0, exclude=(), visibility='all'):
        """Conservative world AABB overlap candidates, sorted by node ID."""
        query = _box(bounds)
        clearance = _positive(clearance, 'clearance', zero=True)
        return [i for i,r in self._chosen(exclude,visibility) if _overlap(query,_box(r['box']),clearance)]

    def oriented_overlap(self, identifier, *, exclude=(), visibility='all'):
        """Exact SAT overlap of transformed local boxes, including shear.

        Geometry boxes remain conservative with respect to non-box meshes.
        Degenerate (flat/line) bounds use world AABB broad-phase candidates.
        """
        source = self.obb(identifier)
        center = np.asarray(source['center'])
        axes = np.asarray(source['half_axes'])
        low,high = _box(self.bounds(identifier))
        result = []
        for other,record in self._chosen(exclude,visibility):
            if other == identifier or not _overlap((low,high),_box(record['box'])):
                continue
            target = self.obb(other)
            other_axes = np.asarray(target['half_axes'])
            delta = np.asarray(target['center'])-center
            # Face normals from each box plus all nine edge cross products.
            candidates = [np.cross(a,b) for a,b in ((axes[0],axes[1]),(axes[1],axes[2]),(axes[2],axes[0]),
                                                    (other_axes[0],other_axes[1]),(other_axes[1],other_axes[2]),(other_axes[2],other_axes[0]))]
            candidates += [np.cross(a,b) for a in axes for b in other_axes]
            separated = False
            for axis in candidates:
                length = np.linalg.norm(axis)
                if length < EPS:
                    continue
                axis = axis/length
                radius = np.abs(axes@axis).sum()+np.abs(other_axes@axis).sum()
                if abs(np.dot(delta,axis)) > radius+EPS:
                    separated = True
                    break
            if not separated:
                result.append(other)
        return result

    def raycast(self, origin, direction, *, max_distance=1e9, exclude=(), visibility='all', first=True):
        origin, direction = _vec(origin,'ray origin'), _vec(direction,'ray direction')
        length = np.linalg.norm(direction)
        require(length > EPS, 'Ray direction is zero.', 'SCENE3D_SPATIAL')
        direction = direction/length
        limit = _positive(max_distance, 'ray distance', zero=True)
        require(type(first) is bool, 'Invalid first-hit flag.', 'SCENE3D_SPATIAL')
        hits, tested = [], 0
        for identifier, record in self._chosen(exclude,visibility,triangles=True):
            low, high = _box(record['box'])
            if not _ray_box(origin,direction,low,high,limit):
                continue
            triangles = record['triangles']
            tested += len(triangles)
            require(tested <= MAX_QUERY_TESTS, 'Ray query budget exceeded.', 'SCENE3D_SPATIAL_LIMIT')
            for index, (a,b,c) in enumerate(triangles):
                e1, e2 = b-a, c-a
                p = np.cross(direction,e2)
                det = np.dot(e1,p)
                if abs(det) < EPS:
                    continue
                inverse = 1./det
                offset = origin-a
                u = np.dot(offset,p)*inverse
                q = np.cross(offset,e1)
                v = np.dot(direction,q)*inverse
                distance = np.dot(e2,q)*inverse
                if u >= -EPS and v >= -EPS and u+v <= 1+EPS and -EPS <= distance <= limit:
                    normal = np.cross(e1,e2)
                    normal /= np.linalg.norm(normal)
                    distance = max(0.,float(distance))
                    hits.append(dict(node=identifier, triangle=index, distance=distance,
                                     point=(origin+direction*distance).tolist(), normal=normal.tolist(),
                                     barycentric=[float(1-u-v),float(u),float(v)], revision=self._revision))
        hits.sort(key=lambda h:(h['distance'],h['node'],h['triangle']))
        return (hits[0] if hits else None) if first else hits

    def nearest(self, point, *, max_distance=1e9, exclude=(), visibility='all'):
        point = _vec(point)
        limit = _positive(max_distance, 'nearest distance', zero=True)
        best = None
        tested = 0
        for identifier, record in self._chosen(exclude,visibility,triangles=True):
            tested += len(record['triangles'])
            require(tested <= MAX_QUERY_TESTS, 'Nearest query budget exceeded.', 'SCENE3D_SPATIAL_LIMIT')
            for index, triangle in enumerate(record['triangles']):
                closest = _closest_triangle(point,triangle)
                distance = float(np.linalg.norm(closest-point))
                if distance <= limit and (best is None or (distance,identifier,index) <
                                          (best['distance'],best['node'],best['triangle'])):
                    best = dict(node=identifier,triangle=index,distance=distance,point=closest.tolist(),revision=self._revision)
        return best

    def frustum(self, planes, *, exclude=(), visibility='all'):
        """AABB candidates inside inward world planes [nx, ny, nz, offset]."""
        require(isinstance(planes,(list,tuple,np.ndarray)) and len(planes) == 6,
                'Frustum requires six planes.', 'SCENE3D_SPATIAL')
        planes = np.asarray(planes,dtype=float)
        require(planes.shape == (6,4) and np.isfinite(planes).all() and
                np.all(np.linalg.norm(planes[:,:3],axis=1)>EPS), 'Invalid frustum planes.', 'SCENE3D_SPATIAL')
        result = []
        for identifier,record in self._chosen(exclude,visibility):
            low,high = _box(record['box'])
            if all(np.dot(normal,np.where(normal>=0,high,low))+offset >= 0 for normal,offset in
                   ((p[:3],p[3]) for p in planes)):
                result.append(identifier)
        return result

    def ground(self, point, *, max_distance=1e9, exclude=(), min_up_dot=0.5, visibility='all'):
        require(isinstance(min_up_dot,(int,float)) and not isinstance(min_up_dot,bool) and
                math.isfinite(min_up_dot) and 0 <= min_up_dot <= 1, 'Invalid support slope.', 'SCENE3D_SPATIAL')
        hits = self.raycast(point,[0,-1,0],max_distance=max_distance,exclude=exclude,visibility=visibility,first=False)
        # Ground accepts upward facing authored triangles; undersides are not support.
        return next((h for h in hits if h['normal'][1] >= min_up_dot),None)


def snap_grid(value, step, *, origin=(0,0,0)):
    """Nearest world/local grid point, halves away from zero deterministically."""
    step = _positive(step,'grid step')
    origin = _vec(origin,'grid origin')
    scaled = (_vec(value)-origin)/step
    require(np.isfinite(scaled).all(), 'Grid computation overflow.', 'SCENE3D_SPATIAL')
    return (origin+np.sign(scaled)*np.floor(np.abs(scaled)+0.5)*step).tolist()


def snap_scale(value, step):
    result = snap_grid(value,step)
    require(all(x > 0 for x in result), 'Scale snap would collapse or reflect an axis.', 'SCENE3D_SPATIAL')
    return result
