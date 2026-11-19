"""Continuous sphere versus native world triangles, without temporal sampling.

Face planes, finite edge cylinders and vertex spheres form the triangle's
Minkowski sum. Candidate analytic roots are checked against its actual nearest
point. Open meshes are two-sided surfaces; closed meshes also reject initial
solid containment. Work is bounded by the spatial owner's query budget.
"""
from fractions import Fraction
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
    if abs(perpendicular - radius) <= roundoff and abs(along) > radius and np.count_nonzero(velocity) > 1:
        # On a long oblique tangent, even the cross product can subtract
        # nearly equal terms. Resolve the sign using the exact input floats.
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
        tolerance = 8 * np.finfo(float).eps * max(radius, perpendicular)
        if perpendicular > radius + tolerance:
            return []
        half_chord = math.sqrt(max(0., radius - perpendicular)) * math.sqrt(radius + perpendicular)
    if half_chord == 0:
        return [-along / speed]
    far_root = -along - math.copysign(half_chord, along)
    # Recover the other root from the product to avoid cancellation when
    # the sphere starts just outside the surface.
    distance = math.hypot(*offset)
    near_root = ((distance - radius) / far_root) * (distance + radius)
    return sorted((near_root / speed, far_root / speed))


def _triangle_hit(start, delta, radius, triangle):
    from scene3d_spatial import _closest_triangle, _triangle_frame
    # Keep contact validation and normals in the triangle's own frame.
    # Adding a world translation to a computed point can round away a gap.
    anchor = triangle[0]
    triangle = triangle - anchor
    start = start - anchor
    frame = _triangle_frame(triangle)
    nearest = _closest_triangle(start, triangle, frame=frame)
    gap=float(np.linalg.norm(start-nearest))
    candidates = [(0., False)] if gap <= radius else []
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
        if not 0 <= t <= 1:
            continue
        t=min(1.,max(0.,float(t)))
        travel = delta*t
        center = start + travel
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
        return dict(fraction=t,normal=outward.tolist(),point=(point+anchor).tolist(),center=(center+anchor).tolist())
    return None


def _solid_domain(triangles, indices, require, budget):
    # Winding containment has an unambiguous material boundary for one shell or
    # spatially separated shells. Nested/intersecting component bounds are an
    # explicit unsupported domain rather than a guessed nearest escape surface.
    edges={}
    for i,face in enumerate(indices):
        for a,b in zip(face,np.roll(face,-1)):
            edges.setdefault(tuple(sorted((int(a),int(b)))),[]).append(i)
    unseen=set(range(len(indices)));boxes=[]
    while unseen:
        first=min(unseen);unseen.remove(first);component=[first];pending=[first]
        while pending:
            face=indices[pending.pop()]
            for a,b in zip(face,np.roll(face,-1)):
                for neighbor in edges[tuple(sorted((int(a),int(b))))]:
                    if neighbor in unseen:unseen.remove(neighbor);component.append(neighbor);pending.append(neighbor)
        vertices=triangles[component].reshape(-1,3)
        boxes.append((vertices.min(axis=0),vertices.max(axis=0)))
    pair_work=len(boxes)*(len(boxes)-1)//2
    require(pair_work<=budget,'Closed sweep component budget exceeded.','SCENE3D_SPATIAL_LIMIT')
    for i,(lo,hi) in enumerate(boxes):
        for other_lo,other_hi in boxes[i+1:]:
            require(not (np.all(lo<other_hi) and np.all(other_lo<hi)),
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
    best=None;tested=0
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
            candidates=[(i,_closest_triangle(start,triangle)) for i,triangle in enumerate(triangles)]
            candidates.sort(key=lambda item:(float(np.linalg.norm(item[1]-start)),item[0]))
            for triangle_id,point in candidates:
                normal=point-start;length=float(np.linalg.norm(normal))
                if not contained and length>radius:break
                outward=_boundary_outward(point,triangles,triangle_id,solid_inside,require)
                if outward is None:continue
                boundary_ulp=32*np.finfo(float).eps*max(float(np.max(np.abs(point))),float(np.max(np.abs(start))),float(np.max(np.abs(triangles[triangle_id]-triangles[triangle_id,0]))))
                normal=(normal/length*(1 if contained else -1) if length>boundary_ulp else outward)
                hit=dict(fraction=0.,normal=normal.tolist(),point=point.tolist(),center=start.tolist(),
                         node=node,triangle=triangle_id,revision=index._revision,initial_containment=contained)
                if best is None or (hit['fraction'],node,triangle_id)<(best['fraction'],best['node'],best['triangle']):
                    best=hit
                break
            else:
                require(not contained,'Closed sweep has no unambiguous material escape boundary.','SCENE3D_SPATIAL')
            if best is not None and best['fraction']==0:continue
        for triangle_id,triangle in enumerate(triangles):
            hit=_triangle_hit(start,delta,radius,triangle)
            if hit is not None:
                hit.update(node=node,triangle=triangle_id,revision=index._revision)
                if best is None or (hit['fraction'],node,triangle_id)<(best['fraction'],best['node'],best['triangle']):
                    best=hit
    return best
