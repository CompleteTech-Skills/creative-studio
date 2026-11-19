"""Continuous sphere versus native world triangles, without temporal sampling.

Face planes, finite edge cylinders and vertex spheres form the triangle's
Minkowski sum. Candidate analytic roots are checked against its actual nearest
point. Open meshes are two-sided surfaces; closed meshes also reject initial
solid containment. Work is bounded by the spatial owner's query budget.
"""
from fractions import Fraction
from itertools import combinations
import math
import numpy as np


def _roots(offset, velocity, radius):
    speed = math.hypot(*velocity)
    if speed == 0:
        return []
    direction = velocity / speed
    along = float(offset @ direction)
    # The half chord depends on the distance to the line, even when the
    # sweep started thousands of radii away from the vertex or edge.
    perpendicular = math.hypot(*np.cross(offset, direction))
    roundoff = 8 * np.finfo(float).eps * max(radius, perpendicular, abs(along))
    if abs(perpendicular - radius) <= roundoff:
        # A rounded distance can conceal a small positive gap, including on
        # an axis-aligned path. Resolve the chord sign from the input floats.
        origin = [Fraction(x) for x in offset]
        motion = [Fraction(x) for x in velocity]
        speed_squared = sum(x*x for x in motion)
        projection = sum(x*v for x, v in zip(origin, motion))
        perpendicular_squared = sum(x*x for x in origin) - projection**2 / speed_squared
        chord_ratio = 1 - perpendicular_squared / Fraction(radius)**2
        if chord_ratio < 0:
            return []
        half_chord = radius * math.sqrt(chord_ratio)
    else:
        if perpendicular > radius:
            return []
        half_chord = math.sqrt(radius - perpendicular) * math.sqrt(radius + perpendicular)
    if half_chord == 0:
        return [-along / speed]
    far_root = -along - math.copysign(half_chord, along)
    # Recover the other root from the product to avoid cancellation when
    # the sphere starts just outside the surface.
    distance = math.hypot(*offset)
    near_root = ((distance - radius) / far_root) * (distance + radius)
    return sorted((near_root / speed, far_root / speed))


def _local_sweep_origin(start, end, anchor, delta, radius):
    if math.hypot(*delta) <= math.ldexp(radius, 18):
        return start - anchor, None
    # Compute the nearest point on the segment to the triangle anchor before
    # rounding to floats. Small contact offsets can vanish when subtracting the
    # anchor from a distant endpoint and then traversing most of that segment.
    start_values = [Fraction(float(value)) for value in start]
    motion = [Fraction(float(value)) - initial for value, initial in zip(end, start_values)]
    origin = [initial - Fraction(float(value)) for initial, value in zip(start_values, anchor)]
    projection = sum(a * b for a, b in zip(origin, motion))
    pivot = max(0, min(1, -projection / sum(component**2 for component in motion)))
    local = np.array([float(a + pivot * b) for a, b in zip(origin, motion)])
    return local, pivot


def _triangle_hit(start, end, delta, radius, triangle):
    from scene3d_spatial import _closest_triangle, _triangle_frame
    # Keep contact validation and normals in the triangle's own frame.
    anchor = triangle[0]
    triangle = triangle - anchor
    initial_center = start - anchor
    start, pivot = _local_sweep_origin(start, end, anchor, delta, radius)
    lower, upper = (0., 1.) if pivot is None else (-float(pivot), float(1 - pivot))
    frame = _triangle_frame(triangle)
    nearest = _closest_triangle(initial_center, triangle, frame=frame)
    gap = math.hypot(*(initial_center - nearest))
    candidates = [(lower, False)] if gap <= radius else []
    a,b,c=triangle
    normal = frame[3]
    if np.any(normal):
        denominator=float(delta@normal)
        if denominator != 0:
            distance=float((start-a)@normal)
            candidates.extend(((sign*radius-distance)/denominator, True) for sign in (-1,1))
    for vertex in triangle:
        candidates.extend((t, False) for t in _roots(start-vertex,delta,radius))
    for p,q in ((a,b),(b,c),(c,a)):
        edge=q-p;length=float(edge@edge)
        if length == 0:
            continue
        offset=start-p
        perpendicular=offset-edge*float(offset@edge)/length
        velocity=delta-edge*float(delta@edge)/length
        for t in _roots(perpendicular,velocity,radius):
            along=float((offset+delta*t)@edge)/length
            if 0 <= along <= 1:
                candidates.append((t, False))
    for t, face_only in sorted(candidates):
        if not lower <= t <= upper:
            continue
        t = float(t)
        travel = delta*t
        center = start + travel
        if t == lower:
            center, time = initial_center, 0.
        elif t == upper:
            center, time = end - anchor, 1.
        else:
            time = t if pivot is None else max(0, min(1, pivot + Fraction(t)))
        point = _closest_triangle(center, triangle, frame=frame, face_only=face_only)
        if point is None:
            continue  # A plane crossing outside the triangle is never a face hit.
        separation=center-point;distance=float(np.linalg.norm(separation))
        tolerance = max(radius*1e-9, 8*np.finfo(float).eps*float(np.max(np.abs(travel))))
        if distance > radius+tolerance:
            continue
        if distance > 0:
            outward=separation/distance
        elif np.any(normal):
            outward=normal if float(delta@normal)<=0 else -normal
        elif np.linalg.norm(delta)>0:
            outward=-delta/np.linalg.norm(delta)
        else:
            outward=np.array([1.,0.,0.])
        return time, dict(fraction=float(time), normal=outward.tolist(),
                          point=(point+anchor).tolist(), center=(center+anchor).tolist())
    return None


def _solid_domain(triangles, indices, require, budget):
    # Winding containment has an unambiguous material boundary for one shell or
    # spatially separated shells. Nested/intersecting component bounds are an
    # explicit unsupported domain rather than a guessed nearest escape surface.
    edges={}
    for i,face in enumerate(indices):
        for a,b in zip(face,np.roll(face,-1)):
            edges.setdefault(tuple(sorted((int(a),int(b)))),[]).append(i)
    unseen = set(range(len(indices)))
    boxes = []
    for first in range(len(indices)):
        if first not in unseen:
            continue
        unseen.remove(first)
        component, pending = [first], [first]
        while pending:
            face=indices[pending.pop()]
            for a,b in zip(face,np.roll(face,-1)):
                for neighbor in edges[tuple(sorted((int(a),int(b))))]:
                    if neighbor in unseen:unseen.remove(neighbor);component.append(neighbor);pending.append(neighbor)
        vertices=triangles[component].reshape(-1,3)
        boxes.append((vertices.min(axis=0),vertices.max(axis=0)))
    pair_work=len(boxes)*(len(boxes)-1)//2
    require(pair_work<=budget,'Closed sweep component budget exceeded.','SCENE3D_SPATIAL_LIMIT')
    for (lo, hi), (other_lo, other_hi) in combinations(boxes, 2):
        require(not (np.all(lo < other_hi) and np.all(other_lo < hi)),
                'Sphere sweep does not support nested or interior-overlapping closed component bounds.',
                'SCENE3D_SPATIAL')
    return pair_work


def _boundary_outward(point, triangles, triangle_id, inside, require):
    a,b,c=triangles[triangle_id]
    edge_scale=float(np.max(np.abs(triangles[triangle_id]-a)))
    require(edge_scale>0,'Closed sweep has a degenerate boundary.','SCENE3D_SPATIAL')
    normal=np.cross((b-a)/edge_scale,(c-a)/edge_scale)
    length=float(np.linalg.norm(normal))
    require(length>0,'Closed sweep has a degenerate boundary.','SCENE3D_SPATIAL')
    normal/=length
    scale=max(edge_scale,float(np.max(np.abs(point))))
    for probe_point in (point,point+(np.mean(triangles[triangle_id],axis=0)-point)*1e-8):
        for factor in (64,256,1024):
            step=np.finfo(float).eps*scale*factor
            positive=inside(probe_point+normal*step,triangles)
            negative=inside(probe_point-normal*step,triangles)
            if positive!=negative:return normal if negative else -normal
    return None  # A shared internal face has material on both sides.


def sweep_sphere(index, start, end, radius, *, exclude=(), visibility='all'):
    from scene3d_spatial import (_vec,_positive,_closest_triangle,_closed,_inside,
                                _box,MAX_QUERY_TESTS)
    from scene3d_graph import require
    start,end=_vec(start,'sweep start'),_vec(end,'sweep end')
    radius=_positive(radius,'sphere radius')
    # Explicit numerical domain bounds; never return guessed clearance on overflow.
    require(radius<=1e12 and np.max(np.abs(start))<=1e12 and np.max(np.abs(end))<=1e12,
            'Sphere sweep exceeds supported 1e12 metre numeric domain.', 'SCENE3D_SPATIAL')
    delta=end-start;low=np.minimum(start,end)-radius;high=np.maximum(start,end)+radius
    best, best_order = None, (math.inf, "", -1)
    tested = 0
    for node,record in index._chosen(exclude,visibility,triangles=True):
        lo,hi=_box(record['box'])
        if np.any(high<lo) or np.any(low>hi):
            continue
        triangles=record['triangles'];tested+=len(triangles)
        require(tested<=MAX_QUERY_TESTS,'Sphere sweep query budget exceeded.','SCENE3D_SPATIAL_LIMIT')
        require(np.max(np.abs(triangles))<=1e12,
                'Sphere sweep mesh exceeds supported numeric domain.','SCENE3D_SPATIAL')
        # Closed native meshes represent solids; a camera starting inside cannot
        # escape by calling the surface sweep with an initially clear radius.
        if _closed(index.meshes[node][1]):
            tested+=_solid_domain(triangles,index.meshes[node][1],require,MAX_QUERY_TESTS-tested)
            def solid_inside(point, triangles):
                nonlocal tested
                tested+=len(triangles)
                require(tested<=MAX_QUERY_TESTS,'Sphere solid-side query budget exceeded.','SCENE3D_SPATIAL_LIMIT')
                return _inside(point,triangles)
            contained=solid_inside(start,triangles)
            tested+=len(triangles)
            require(tested<=MAX_QUERY_TESTS,'Sphere closed-nearest query budget exceeded.','SCENE3D_SPATIAL_LIMIT')
            # Compare separation before rounding contact points back into
            # world space, including the nearest-boundary ordering.
            candidates = []
            for i, triangle in enumerate(triangles):
                anchor = triangle[0]
                local_start = start - anchor
                point = _closest_triangle(local_start, triangle - anchor)
                candidates.append((math.hypot(*(point - local_start)), i, point))
            candidates.sort(key=lambda item: (item[0], item[1]))
            for length,triangle_id,point in candidates:
                if not contained and length>radius:break
                anchor = triangles[triangle_id,0]
                local_triangles = triangles - anchor
                outward=_boundary_outward(point,local_triangles,triangle_id,solid_inside,require)
                if outward is None:continue
                boundary_ulp=32*np.finfo(float).eps*float(np.max(np.abs(local_triangles[triangle_id])))
                normal = point - (start - anchor)
                normal=(normal/length*(1 if contained else -1) if length>boundary_ulp else outward)
                hit=dict(fraction=0.,normal=normal.tolist(),point=(anchor+point).tolist(),center=start.tolist(),
                         node=node,triangle=triangle_id,revision=index._revision,initial_containment=contained)
                order = (0., node, triangle_id)
                if order < best_order:
                    best, best_order = hit, order
                break
            else:
                require(not contained,'Closed sweep has no unambiguous material escape boundary.','SCENE3D_SPATIAL')
            if best_order[0] == 0:
                continue
        for triangle_id,triangle in enumerate(triangles):
            result = _triangle_hit(start, end, delta, radius, triangle)
            if result is not None:
                time, hit = result
                order = (time, node, triangle_id)
                if order < best_order:
                    hit.update(node=node, triangle=triangle_id, revision=index._revision)
                    best, best_order = hit, order
    return best
