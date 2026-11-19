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


def _sqrt_fraction(value):
    """A bounded 128-bit square root; exact signs never depend on this rounding."""
    numerator, denominator = value.numerator, value.denominator
    nroot, droot = math.isqrt(numerator), math.isqrt(denominator)
    if nroot*nroot == numerator and droot*droot == denominator:
        return Fraction(nroot, droot)
    # Scale before integer sqrt, so neither huge nor subnormal coefficients
    # need conversion to float. The result has at least 128 significant bits.
    shift = 128 - (numerator.bit_length() - denominator.bit_length()) // 2
    if shift >= 0:
        return Fraction(math.isqrt((numerator << (2*shift)) // denominator), 1 << shift)
    return Fraction(math.isqrt(numerator // (denominator << (-2*shift))) << -shift)


def _roots(a, b, c, lower, upper):
    """Roots of exact squared-distance coefficients, bounded before conversion."""
    if a == 0:
        return [] if b == 0 else [t for t in (-c / b,) if lower <= t <= upper]
    discriminant = b*b - 4*a*c
    if discriminant < 0:
        return []
    if discriminant == 0:
        roots = [-b / (2*a)]
    else:
        chord = _sqrt_fraction(discriminant)
        far = (-b - (chord if b >= 0 else -chord)) / 2
        # Product recovery retains a positive near-contact residual even when
        # the directly subtracted square root would round it away.
        roots = [far / a, c / far]
    return sorted(t for t in roots if lower <= t <= upper)


def _feature_candidates(origin, motion, vertices, radius, lower, upper):
    from scene3d_spatial import _cross3, _dot3, _subtract3
    radius_squared = Fraction(radius)**2
    speed_squared = _dot3(motion, motion)
    a, b, c = vertices
    normal = _cross3(_subtract3(b, a), _subtract3(c, a))
    normal_squared = _dot3(normal, normal)
    if normal_squared:
        distance = _dot3(_subtract3(origin, a), normal)
        speed = _dot3(motion, normal)
        for t in _roots(speed*speed, 2*distance*speed,
                        distance*distance - radius_squared*normal_squared, lower, upper):
            yield t, True
    for vertex in vertices:
        offset = _subtract3(origin, vertex)
        for t in _roots(speed_squared, 2*_dot3(offset, motion),
                        _dot3(offset, offset) - radius_squared, lower, upper):
            yield t, False
    for p, q in ((a, b), (b, c), (c, a)):
        edge = _subtract3(q, p)
        length = _dot3(edge, edge)
        if not length:
            continue
        offset = _subtract3(origin, p)
        # Cross products avoid projecting away the very gap being measured.
        perpendicular = _cross3(offset, edge)
        velocity = _cross3(motion, edge)
        for t in _roots(_dot3(velocity, velocity), 2*_dot3(perpendicular, velocity),
                        _dot3(perpendicular, perpendicular) - radius_squared*length,
                        lower, upper):
            along = _dot3(offset, edge) + t*_dot3(motion, edge)
            if 0 <= along <= length:
                yield t, False


def _sweep_pivot(origin, motion, radius):
    if math.hypot(*map(float, motion)) <= math.ldexp(radius, 18):
        return Fraction(0)
    # Recenter long sweeps before finding roots: tiny contact offsets survive
    # even when their public fraction rounds to the same float as the pivot.
    projection = sum(a*b for a, b in zip(origin, motion))
    return max(Fraction(0), min(Fraction(1), -projection / sum(v*v for v in motion)))


def _within_triangle_radius(distance, radius, center, triangle):
    if abs(distance - radius) > 8 * math.ulp(max(distance, radius)):
        return distance < radius
    from scene3d_spatial import _exact_closest_triangle
    _, squared = _exact_closest_triangle(center, triangle, False)
    return squared <= Fraction(radius)**2


def _triangle_hit(start, end, delta, radius, triangle):
    from scene3d_spatial import (_closest_triangle, _nearest_vertex, _triangle_frame,
                                 _fraction_vector, _subtract3)
    # Center near the swept segment so a distant authored vertex cannot
    # erase a small positive gap during contact validation.
    start_world, triangle_world = start, triangle
    exact_start = _fraction_vector(start)
    exact_motion = _subtract3(_fraction_vector(end), exact_start)
    anchor = _nearest_vertex(start + .5 * delta, triangle)
    triangle = triangle - anchor
    initial_center = start - anchor
    exact_anchor = _fraction_vector(anchor)
    pivot = _sweep_pivot(_subtract3(exact_start, exact_anchor), exact_motion, radius)
    lower, upper = -pivot, 1 - pivot
    exact_origin = tuple(s + pivot*v - a for s, v, a in zip(exact_start, exact_motion, exact_anchor))
    exact_vertices = [_subtract3(_fraction_vector(vertex), exact_anchor) for vertex in triangle_world]
    frame = _triangle_frame(triangle)
    nearest = _closest_triangle(initial_center, triangle, frame=frame)
    gap = math.hypot(*(initial_center - nearest))
    initially_touching = _within_triangle_radius(gap, radius, start_world, triangle_world)
    candidates = [(lower, False)] if initially_touching else []
    normal = frame[3]
    candidates.extend(_feature_candidates(exact_origin, exact_motion, exact_vertices,
                                          radius, lower, upper))
    for t, face_only in sorted(candidates):
        if not lower <= t <= upper:
            continue
        travel = delta*float(t)
        center = np.array([float(s + t*v) for s, v in zip(exact_origin, exact_motion)])
        if t == lower:
            if not initially_touching:
                continue
            center, time, world_center = initial_center, 0., start_world
        elif t == upper:
            center, time, world_center = end - anchor, 1., end
        else:
            time = pivot + t
            world_center = np.array([float(s + time*v) for s, v in zip(exact_start, exact_motion)])
        point = _closest_triangle(center, triangle, frame=frame, face_only=face_only)
        if point is None:
            continue  # A plane crossing outside the triangle is never a face hit.
        separation = center - point
        distance = math.hypot(*separation)
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
                          point=(point+anchor).tolist(), center=world_center.tolist())
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
    from scene3d_spatial import (_vec, _positive, _closest_triangle, _closed, _inside,
                                 _box, _nearest_vertex, MAX_QUERY_TESTS)
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
                anchor = _nearest_vertex(start, triangle)
                local_start = start - anchor
                point = _closest_triangle(local_start, triangle - anchor)
                candidates.append((math.hypot(*(point - local_start)), i, point, anchor))
            candidates.sort(key=lambda item: (item[0], item[1]))
            for length, triangle_id, point, anchor in candidates:
                if not contained:
                    if length - radius > 8 * math.ulp(max(length, radius)):
                        break
                    if not _within_triangle_radius(length, radius, start, triangles[triangle_id]):
                        continue
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
