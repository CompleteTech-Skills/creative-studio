"""Bounded geometric queries on the authoritative SceneGraph base pose.

Triangle data are explicit node-local geometry, never substituted from a bounding
box. All public distances, rays and plane equations use canonical world metres.
Queries snapshot a revision and return stable node-id/triangle-index ordering.
Animated/deformed callers must supply their evaluated vertices explicitly.
"""
from __future__ import annotations

from fractions import Fraction
from itertools import product
import math
import sys
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
            -sys.float_info.max <= value <= sys.float_info.max and math.isfinite(value) and (value >= 0 if zero else value > 0),
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


def _fraction_vector(values):
    return tuple(Fraction(float(value)) for value in values)


def _subtract3(left, right):
    return tuple(a - b for a, b in zip(left, right))


def _cross3(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot3(left, right):
    return sum(a * b for a, b in zip(left, right))


def _nearest_vertex(point, triangle):
    return min(triangle, key=lambda vertex: math.dist(point, vertex))


def _oriented_bounds(record):
    low, high = map(_fraction_vector, _box(record['local']))
    midpoint = tuple((a + b) / 2 for a, b in zip(low, high))
    widths = tuple((b - a) / 2 for a, b in zip(low, high))
    rows = tuple(map(_fraction_vector, record['matrix'][:3]))
    center = tuple(_dot3(row[:3], midpoint) + row[3] for row in rows)
    axes = tuple(tuple(row[i] * width for row in rows) for i, width in enumerate(widths))
    return center, axes


def _overlap(a, b, clearance=0):
    return bool(np.all(a[0] <= b[1]+clearance) and np.all(b[0] <= a[1]+clearance))


def _ray_box(origin, direction, low, high, direction_squared, limit_squared):
    near, far = Fraction(0), None
    for position, component, minimum, maximum in zip(
            origin, direction, _fraction_vector(low), _fraction_vector(high)):
        if not component:
            if not minimum <= position <= maximum:
                return False
            continue
        entry, exit = sorted(((minimum - position) / component, (maximum - position) / component))
        near = max(near, entry)
        far = exit if far is None else min(far, exit)
        if near > far:
            return False
    return near**2 * direction_squared <= limit_squared


def _exact_ray_triangle(origin, direction, triangle, direction_squared, limit_squared):
    """Keep the actual ray parameter for ordering and reconstruct the world point."""
    a, b, c = map(_fraction_vector, triangle)
    e1, e2, offset = _subtract3(b, a), _subtract3(c, a), _subtract3(origin, a)
    p = _cross3(direction, e2)
    det = _dot3(e1, p)
    if not det:
        return None
    q = _cross3(offset, e1)
    u, v = _dot3(offset, p) / det, _dot3(direction, q) / det
    w = 1 - u - v
    if min(w, u, v) < 0:
        return None
    t = _dot3(e2, q) / det
    if t < 0 or t**2 * direction_squared > limit_squared:
        return None
    travel = tuple(t * component for component in direction)
    point = [float(position + component) for position, component in zip(origin, travel)]
    normal = _cross3(e1, e2)
    magnitude = max(map(abs, normal))
    unit = [float(component / magnitude) for component in normal]
    length = math.hypot(*unit)
    return t, math.hypot(*map(float, travel)), point, [float(w), float(u), float(v)], [
        value / length for value in unit]


def _triangle_frame(triangle):
    # Scaling each edge by a power of two preserves its direction exactly.
    # Crossing the two shorter edges avoids subtracting nearly equal long edges.
    edges = np.roll(triangle, -1, axis=0) - triangle
    scales = np.max(np.abs(edges), axis=1)
    _, exponents = np.frexp(scales)
    scaled_edges = np.ldexp(edges, -exponents[:, None])
    longest = int(np.argmax(scales))
    normal = np.cross(scaled_edges[(longest+1) % 3], scaled_edges[(longest+2) % 3])
    normal_length = math.hypot(*normal)
    if normal_length:
        normal /= normal_length
    return edges, scaled_edges, exponents, normal


def _exact_closest_triangle(point, triangle, face_only):
    point = _fraction_vector(point)
    vertices = tuple(map(_fraction_vector, triangle))
    edges = tuple(_subtract3(vertices[(i + 1) % 3], v) for i, v in enumerate(vertices))
    normal = _cross3(edges[0], edges[1])
    normal_squared = _dot3(normal, normal)
    if normal_squared:
        plane = _dot3(_subtract3(point, vertices[0]), normal) / normal_squared
        projected = tuple(value - plane * axis for value, axis in zip(point, normal))
        if all(_dot3(_cross3(edge, _subtract3(projected, vertex)), normal) >= 0
               for vertex, edge in zip(vertices, edges)):
            return projected, normal_squared * plane**2
    if face_only:
        return None

    nearest = None
    for vertex, edge in zip(vertices, edges):
        edge_squared = _dot3(edge, edge)
        along = max(0, min(1, _dot3(_subtract3(point, vertex), edge) / edge_squared)) if edge_squared else 0
        candidate = tuple(value + along * delta for value, delta in zip(vertex, edge))
        delta = _subtract3(point, candidate)
        squared = _dot3(delta, delta)
        if nearest is None or squared < nearest[0]:
            nearest = squared, candidate
    return nearest[1], nearest[0]


def _closest_triangle(point, triangle, *, frame=None, face_only=False):
    # Project onto the face, then test its three edge half-planes in the
    # dominant coordinate plane. Gram determinants can lose the entire area
    # of a long, thin triangle when they subtract nearly equal dot products.
    edges, scaled_edges, exponents, normal = _triangle_frame(triangle) if frame is None else frame
    outside_face = False
    if np.any(normal):
        projected = point - ((point - triangle[0]) @ normal) * normal
        axis = int(np.argmax(np.abs(normal)))
        i, j = (axis+1) % 3, (axis+2) % 3
        offset = projected - triangle
        sides = np.sign(normal[axis]) * (scaled_edges[:, i]*offset[:, j] - scaled_edges[:, j]*offset[:, i])
        # Multiplication and vertex subtraction can both conceal a small gap
        # along a long edge. Ambiguous signs and their nearest edge use the
        # actual binary coordinates, with no geometric tolerance added.
        error = 32 * np.finfo(float).eps * (
            np.abs(scaled_edges[:, i]) * (abs(projected[j]) + np.abs(triangle[:, j])) +
            np.abs(scaled_edges[:, j]) * (abs(projected[i]) + np.abs(triangle[:, i])))
        if np.all(sides > error):
            return projected
        outside_face = np.any(sides < -error)
    if not outside_face:
        exact = _exact_closest_triangle(point, triangle, face_only)
        return None if exact is None else np.asarray(exact[0], dtype=float)
    if face_only:
        return None

    # Outside the face, or on a degenerate triangle, select the nearest
    # point on its three finite segments. A zero-length segment is a vertex.
    candidates = []
    for vertex, edge, direction, exponent in zip(triangle, edges, scaled_edges, exponents):
        length_squared = direction @ direction
        if length_squared:
            along = np.ldexp((point - vertex) @ direction, -exponent) / length_squared
            candidates.append(vertex + np.clip(along, 0, 1)*edge)
        else:
            candidates.append(vertex)
    closest = candidates[0]
    for candidate in candidates[1:]:
        difference = candidate - closest
        # Comparing on the bisector avoids subtracting two long, nearly equal distances.
        if (point - closest - .5*difference) @ difference > 0:
            closest = candidate
    return closest


def _triangle_axes(left, right):
    def edges(vertices):
        return [tuple(b - a for a, b in zip(vertex, vertices[(index + 1) % 3]))
                for index, vertex in enumerate(vertices)]

    left_edges, right_edges = edges(left), edges(right)
    left_normal = _cross3(left_edges[0], left_edges[1])
    right_normal = _cross3(right_edges[0], right_edges[1])
    # Coplanar triangles also need the in-plane normals of their edges.
    return ([left_normal, right_normal] +
            [_cross3(a, b) for a in left_edges for b in right_edges] +
            [_cross3(left_normal, edge) for edge in left_edges] +
            [_cross3(right_normal, edge) for edge in right_edges])


def _exact_triangle_overlap(left, right):
    # Binary float denominators are powers of two. Scale all six vertices to
    # one integer lattice so normals, axes and projection signs stay exact.
    ratios = [[float(value).as_integer_ratio() for value in point] for point in (*left, *right)]
    unit = max(denominator for point in ratios for _, denominator in point)
    vertices = [tuple(numerator * (unit // denominator) for numerator, denominator in point)
                for point in ratios]
    left, right = vertices[:3], vertices[3:]
    for axis in _triangle_axes(left, right):
        if any(axis):
            projected = [sum(a * b for a, b in zip(point, axis)) for point in vertices]
            a, b = projected[:3], projected[3:]
            if max(a) < min(b) or max(b) < min(a):
                return False
    return True


def _triangle_intersects(left, right):
    """Separating-axis triangle intersection, including coplanar triangles."""
    anchor = left[0]
    with np.errstate(over='ignore'):
        relative_left, relative_right = left - anchor, right - anchor
    scale = max(float(np.max(np.abs(relative_left))), float(np.max(np.abs(relative_right))))
    if scale == 0 or not math.isfinite(scale):
        return _exact_triangle_overlap(left, right)
    normalized_left, normalized_right = relative_left / scale, relative_right / scale
    uncertain = False
    for axis in _triangle_axes(normalized_left, normalized_right):
        magnitude = max(map(abs, axis))
        if magnitude == 0:
            uncertain = True
            continue
        axis = np.asarray(axis) / magnitude
        a, b = normalized_left @ axis, normalized_right @ axis
        tolerance = 32 * np.finfo(float).eps * max(1., float(np.max(np.abs(a))), float(np.max(np.abs(b))))
        gap = max(float(b.min() - a.max()), float(a.min() - b.max()))
        if gap > tolerance:
            return False
        if gap >= -tolerance:
            uncertain = True
    return _exact_triangle_overlap(left, right) if uncertain else True


def _closed(indices):
    """Require an oriented two-manifold edge inventory before solid containment."""
    edges = {}
    for triangle in indices:
        for a,b in zip(triangle,np.roll(triangle,-1)):
            key = tuple(sorted((int(a),int(b))))
            edges.setdefault(key,[]).append((int(a),int(b)))
    return all(len(uses)==2 and uses[0]==tuple(reversed(uses[1])) for uses in edges.values())


def _component_representatives(indices):
    """One triangle offset per index-connected component, in deterministic order."""
    parents = {}
    def find(i):
        parents.setdefault(i,i)
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    for triangle in indices:
        a = find(int(triangle[0]))
        for vertex in triangle[1:]:
            b = find(int(vertex))
            if a != b:
                parents[b] = a
    chosen = {}
    for i,triangle in enumerate(indices):
        chosen.setdefault(find(int(triangle[0])),i)
    return sorted(chosen.values())


def _inside(point, triangles):
    # Oriented solid angle, independent of ray-edge coincidences.
    vectors = triangles-point
    scale = float(np.max(np.abs(vectors)))
    if scale == 0:
        return True
    vectors = vectors/scale
    lengths = np.linalg.norm(vectors,axis=2)
    if np.any(lengths == 0):
        return True
    a,b,c = vectors[:,0],vectors[:,1],vectors[:,2]
    numerator = np.einsum('ij,ij->i',a,np.cross(b,c))
    denominator = lengths.prod(axis=1)+np.einsum('ij,ij->i',a,b)*lengths[:,2]+np.einsum('ij,ij->i',b,c)*lengths[:,0]+np.einsum('ij,ij->i',c,a)*lengths[:,1]
    angle = float((2*np.arctan2(numerator,denominator)).sum())
    return abs(angle) > 2*math.pi


class _QueryBudget:
    """Shared counter across nested/repeated exact queries in one operation."""
    def __init__(self):
        self.used = 0

    def consume(self, amount):
        self.used += amount
        require(self.used <= MAX_QUERY_TESTS, 'Exact geometry operation budget exceeded.', 'SCENE3D_SPATIAL_LIMIT')


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
        nodes = {n['id']: n for n in graph.document()['nodes']}
        ids = set(nodes)
        self._bindings = {}
        self.meshes = {}
        total_vertices = total_triangles = 0
        for identifier, mesh in (meshes or {}).items():
            require(identifier in ids and isinstance(mesh, dict) and set(mesh) == {'vertices','triangles'},
                    'Invalid mesh node/fields.', 'SCENE3D_SPATIAL')
            require(isinstance(mesh['vertices'], (list,tuple,np.ndarray)) and
                    isinstance(mesh['triangles'], (list,tuple,np.ndarray)), 'Invalid mesh arrays.', 'SCENE3D_SPATIAL')
            require(not isinstance(mesh['vertices'],np.ndarray) or mesh['vertices'].ndim>0,
                    'Invalid vertex array shape.', 'SCENE3D_SPATIAL')
            require(not isinstance(mesh['triangles'],np.ndarray) or mesh['triangles'].ndim>0,
                    'Invalid triangle array shape.', 'SCENE3D_SPATIAL')
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
            self._bindings[identifier] = (nodes[identifier].get('asset'),nodes[identifier]['type'],
                                          nodes[identifier]['metadata'].get('source_component'),nodes[identifier]['metadata'].get('source_node'))
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
                    binding = (node.get('asset'),node['type'],node['metadata'].get('source_component'),node['metadata'].get('source_node'))
                    require(binding == self._bindings[identifier], 'Geometry identity changed; rebuild the spatial index.', 'SCENE3D_SPATIAL_STALE')
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
        center, half_axes = _oriented_bounds(self._records[identifier])
        try:
            return dict(center=list(map(float, center)), half_axes=[list(map(float, axis)) for axis in half_axes])
        except OverflowError:
            require(False, 'Oriented bounds exceed finite world coordinates.', 'SCENE3D_SPATIAL')

    def overlap(self, bounds, *, clearance=0, exclude=(), visibility='all'):
        """Conservative world AABB overlap candidates, sorted by node ID."""
        query = _box(bounds)
        clearance = _positive(clearance, 'clearance', zero=True)
        return [i for i,r in self._chosen(exclude,visibility) if _overlap(query,_box(r['box']),clearance)]

    def oriented_overlap(self, identifier, *, exclude=(), visibility='all'):
        """Exact SAT overlap of transformed local boxes, including shear.

        Geometry boxes remain conservative with respect to non-box meshes.
        Degenerate bounds use the world AABB and available separating axes conservatively.
        """
        records = self._chosen(exclude, visibility)
        require(identifier in self._records, 'Node has no geometric bounds.', 'SCENE3D_SPATIAL')
        source = self._records[identifier]
        center, axes = _oriented_bounds(source)
        source_extent = tuple(sum(map(abs, column)) for column in zip(*axes))
        result = []
        for other, record in records:
            if other == identifier:
                continue
            other_center, other_axes = _oriented_bounds(record)
            delta = _subtract3(other_center, center)
            other_extent = (sum(map(abs, column)) for column in zip(*other_axes))
            # A transformed local box may include empty corners beyond its mesh AABB.
            if any(abs(component) > left + right
                   for component, left, right in zip(delta, source_extent, other_extent)):
                continue
            # Exact binary projections preserve every positive gap and inclusive contact.
            # Zero axes carry no separating information, including for flat boxes.
            candidates = [_cross3(box[i], box[(i + 1) % 3])
                          for box in (axes, other_axes) for i in range(3)]
            candidates.extend(_cross3(a, b) for a in axes for b in other_axes)
            if all(abs(_dot3(delta, axis)) <= sum(abs(_dot3(edge, axis)) for edge in (*axes, *other_axes))
                   for axis in candidates if any(axis)):
                result.append(other)
        return result

    def mesh_overlap(self, identifier, *, exclude=(), visibility='all', solid=False, _budget=None):
        """Exact triangle intersections, optionally including closed-solid containment.

        Touching surfaces count as overlap. Solid mode requires oriented closed
        two-manifold input meshes. A bounded pair budget fails explicitly rather
        than substituting box collisions. Degenerate triangles are rejected.
        """
        self._refresh()
        require(type(solid) is bool, 'Invalid solid overlap flag.', 'SCENE3D_SPATIAL')
        require(identifier in self._records and self._records[identifier]['triangles'] is not None,
                'Exact overlap requires source triangles.', 'SCENE3D_SPATIAL')
        source = self._records[identifier]
        left = source['triangles']
        require(_budget is None or isinstance(_budget,_QueryBudget), 'Invalid exact geometry budget.', 'SCENE3D_SPATIAL')
        budget = _QueryBudget() if _budget is None else _budget
        result = []
        def valid_triangles(triangles):
            with np.errstate(over='ignore', invalid='ignore', divide='ignore'):
                edges = triangles[:, 1:] - triangles[:, :1]
                scale = np.max(np.abs(edges), axis=(1, 2))
                normalized = edges / scale[:, None, None]
                normals = np.cross(normalized[:, 0], normalized[:, 1])
            uncertain = ~np.isfinite(normals).all(axis=1) | ~np.any(normals, axis=1)
            for triangle in triangles[uncertain]:
                a, b, c = map(_fraction_vector, triangle)
                require(any(_cross3(_subtract3(b, a), _subtract3(c, a))),
                        'Exact overlap rejects degenerate triangles.', 'SCENE3D_SPATIAL')
        valid_triangles(left)
        if solid:
            require(_closed(self.meshes[identifier][1]), 'Solid overlap requires oriented closed source mesh.', 'SCENE3D_SPATIAL')
        left_low,left_high = left.min(axis=1),left.max(axis=1)
        for other,record in self._chosen(exclude,visibility,triangles=True):
            if other == identifier or not _overlap(_box(source['box']),_box(record['box'])):
                continue
            right = record['triangles']
            valid_triangles(right)
            if solid:
                require(_closed(self.meshes[other][1]), 'Solid overlap requires oriented closed target mesh.', 'SCENE3D_SPATIAL')
            # Count possible work before allocating pair candidates.
            budget.consume(len(left)*len(right))
            found = None
            right_low,right_high = right.min(axis=1),right.max(axis=1)
            for i,triangle in enumerate(left):
                candidates = np.flatnonzero(np.all(left_low[i]<=right_high,axis=1)&np.all(right_low<=left_high[i],axis=1))
                for j in candidates:
                    if _triangle_intersects(triangle,right[j]):
                        found = dict(node=other,relation='surface',source_triangle=i,target_triangle=int(j),revision=self._revision)
                        break
                if found is not None:
                    break
            if found is None and solid:
                left_reps = _component_representatives(self.meshes[identifier][1])
                right_reps = _component_representatives(self.meshes[other][1])
                budget.consume(len(left_reps)*len(right)+len(right_reps)*len(left))
                if any(_inside(left[i,0],right) for i in left_reps) or any(_inside(right[i,0],left) for i in right_reps):
                    found = dict(node=other,relation='containment',source_triangle=None,target_triangle=None,revision=self._revision)
            if found is not None:
                result.append(found)
        return result

    def raycast(self, origin, direction, *, max_distance=1e9, exclude=(), visibility='all', first=True):
        origin, direction = _vec(origin,'ray origin'), _vec(direction,'ray direction')
        exact_origin, exact_direction = _fraction_vector(origin), _fraction_vector(direction)
        direction_squared = _dot3(exact_direction, exact_direction)
        magnitude = np.max(np.abs(direction))
        require(magnitude > 0, 'Ray direction is zero.', 'SCENE3D_SPATIAL')
        direction = direction/magnitude
        direction = direction/np.linalg.norm(direction)
        limit = _positive(max_distance, 'ray distance', zero=True)
        limit_squared = Fraction(limit)**2
        require(type(first) is bool, 'Invalid first-hit flag.', 'SCENE3D_SPATIAL')
        hits, tested = [], 0
        for identifier, record in self._chosen(exclude,visibility,triangles=True):
            low, high = _box(record['box'])
            if not _ray_box(exact_origin, exact_direction, low, high, direction_squared, limit_squared):
                continue
            triangles = record['triangles']
            tested += len(triangles)
            require(tested <= MAX_QUERY_TESTS, 'Ray query budget exceeded.', 'SCENE3D_SPATIAL_LIMIT')
            for index, triangle in enumerate(triangles):
                with np.errstate(over='ignore', invalid='ignore'):
                    a, b, c = triangle
                    e1, e2 = b - a, c - a
                    scale = max(float(np.max(np.abs(e1))), float(np.max(np.abs(e2))))
                    if scale == 0:
                        continue
                    e1, e2 = e1 / scale, e2 / scale
                    p = _cross3(direction, e2)
                    det = _dot3(e1, p)
                    offset = (origin - a) / scale
                    u = _dot3(offset, p)
                    v = _dot3(direction, _cross3(offset, e1))
                    w = det - u - v
                # Reject reliable misses cheaply. Resolve every surviving
                # parameter from the original coordinates before ordering hits.
                roundoff = 64 * np.finfo(float).eps * max(1., float(np.max(np.abs(offset))))
                minimum = min(w, u, v) if det > 0 else min(-w, -u, -v)
                if abs(det) > roundoff and minimum < -roundoff:
                    continue
                hit = _exact_ray_triangle(exact_origin, exact_direction, triangle, direction_squared, limit_squared)
                if hit is None:
                    continue
                parameter, distance, point, barycentric, normal = hit
                result = dict(node=identifier, triangle=index, distance=min(distance, limit),
                              point=point, normal=normal, barycentric=barycentric, revision=self._revision)
                if first:
                    if not hits or parameter < hits[0][0]:
                        hits[:] = [(parameter, result)]
                else:
                    hits.append((parameter, result))
        if first:
            return hits[0][1] if hits else None
        hits.sort(key=lambda hit: (hit[0], hit[1]['node'], hit[1]['triangle']))
        return [result for _, result in hits]

    def sweep_sphere(self, start, end, radius, *, exclude=(), visibility='all'):
        """First continuous sphere contact against real triangles (closed solids
        also detect initial containment). Touching counts as contact.
        """
        from scene3d_sweep import sweep_sphere
        return sweep_sphere(self,start,end,radius,exclude=exclude,visibility=visibility)

    def nearest(self, point, *, max_distance=1e9, exclude=(), visibility='all'):
        point = _vec(point)
        limit = _positive(max_distance, 'nearest distance', zero=True)
        limit_squared = Fraction(limit)**2
        best = best_triangle = best_squared = None
        best_error = 0.
        tested = 0
        for identifier, record in self._chosen(exclude, visibility, triangles=True):
            tested += len(record['triangles'])
            require(tested <= MAX_QUERY_TESTS, 'Nearest query budget exceeded.', 'SCENE3D_SPATIAL_LIMIT')
            for index, triangle in enumerate(record['triangles']):
                anchor = _nearest_vertex(point, triangle)
                with np.errstate(over='ignore', invalid='ignore'):
                    local_point, local_triangle = point - anchor, triangle - anchor
                    scale = max(float(np.max(np.abs(local_point))), float(np.max(np.abs(local_triangle))))
                    closest = _closest_triangle(local_point, local_triangle) if math.isfinite(scale) else None
                    distance = math.hypot(*(closest - local_point)) if closest is not None else math.inf
                    world_point = (anchor + closest).tolist() if closest is not None else None
                threshold = limit if best is None else best['distance']
                error = 64 * np.finfo(float).eps * scale
                squared = None
                if not math.isfinite(distance) or abs(distance - threshold) <= max(error, best_error):
                    exact_point, squared = _exact_closest_triangle(point, triangle, False)
                    if squared > limit_squared:
                        continue
                    if best is not None:
                        if best_squared is None:
                            _, best_squared = _exact_closest_triangle(point, best_triangle, False)
                        # Candidates are traversed in node and triangle order.
                        if squared >= best_squared:
                            continue
                    displacement = _subtract3(_fraction_vector(point), exact_point)
                    distance = min(limit, math.hypot(*map(float, displacement)))
                    world_point = list(map(float, exact_point))
                elif distance > threshold:
                    continue
                best = dict(node=identifier, triangle=index, distance=distance,
                            point=world_point, revision=self._revision)
                best_triangle, best_squared, best_error = triangle, squared, error
        return best

    def frustum(self, planes, *, exclude=(), visibility='all'):
        """AABB candidates inside inward world planes [nx, ny, nz, offset]."""
        require(isinstance(planes,(list,tuple,np.ndarray)) and
                (not isinstance(planes,np.ndarray) or planes.ndim>0) and len(planes) == 6,
                'Frustum requires six planes.', 'SCENE3D_SPATIAL')
        try:
            planes = np.asarray(planes,dtype=float)
        except (TypeError,ValueError,OverflowError):
            require(False, 'Invalid frustum plane values.', 'SCENE3D_SPATIAL')
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
                0 <= min_up_dot <= 1 and math.isfinite(min_up_dot), 'Invalid support slope.', 'SCENE3D_SPATIAL')
        hits = self.raycast(point,[0,-1,0],max_distance=max_distance,exclude=exclude,visibility=visibility,first=False)
        # Ground accepts upward facing authored triangles; undersides are not support.
        return next((h for h in hits if h['normal'][1] >= min_up_dot),None)


def snap_grid(value, step, *, origin=(0,0,0)):
    """Nearest world/local grid point, halves away from zero deterministically."""
    spacing = Fraction(_positive(step, 'grid step'))
    half = Fraction(1, 2)
    snapped = []
    for position, base in zip(_vec(value), _vec(origin, 'grid origin')):
        anchor = Fraction(float(base))
        offset = (Fraction(float(position)) - anchor) / spacing
        nearest = math.floor(abs(offset) + half)
        if offset < 0:
            nearest = -nearest
        try:
            snapped.append(float(anchor + nearest * spacing))
        except OverflowError:
            require(False, 'Grid computation overflow.', 'SCENE3D_SPATIAL')
    return snapped


def snap_scale(value, step):
    result = snap_grid(value,step)
    require(all(x > 0 for x in result), 'Scale snap would collapse or reflect an axis.', 'SCENE3D_SPATIAL')
    return result
