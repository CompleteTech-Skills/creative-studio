"""Bounded, deterministic Y-up mesh construction for editable Scene3D assets."""
from dataclasses import dataclass
import math
import numpy as np
from scene3d_graph import require

MAX_TRIANGLES = 200000


def numeric_array(value, label):
    try:
        result=np.asarray(value,dtype=float)
    except (TypeError, ValueError, OverflowError):
        require(False,label+' must be a finite numeric array.','SCENE3D_GEOMETRY')
    require(np.isfinite(result).all(),label+' must be a finite numeric array.','SCENE3D_GEOMETRY')
    return result


def positive(value, name):
    require(type(value) in (int, float) and 0 < value <= 10000 and math.isfinite(value),
            name + ' must be finite, positive and at most 10000.', 'SCENE3D_GEOMETRY')
    return float(value)


def count(value, name, low=3, high=256):
    require(type(value) is int and low <= value <= high, name + ' is outside its integer budget.', 'SCENE3D_GEOMETRY')
    return value


@dataclass
class Mesh:
    vertices: list
    triangles: list
    texcoords: list | None = None

    def validate(self, budget=MAX_TRIANGLES):
        count(budget, 'triangle budget', 1, MAX_TRIANGLES)
        v = numeric_array(self.vertices,'vertices')
        try:
            f = np.asarray(self.triangles)
        except (TypeError,ValueError,OverflowError):
            require(False,'Triangles must be integer triples.','SCENE3D_GEOMETRY')
        require(v.ndim == 2 and v.shape[1] == 3 and 3 <= len(v) <= 200000 and np.isfinite(v).all() and (np.abs(v)<=1000000).all(),
                'Invalid or excessive mesh vertices.', 'SCENE3D_GEOMETRY')
        require(f.ndim == 2 and f.shape[1] == 3 and 1 <= len(f) <= budget and f.dtype.kind in 'iu'
                and f.min() >= 0 and f.max() < len(v), 'Invalid or excessive triangle indices.', 'SCENE3D_GEOMETRY')
        if self.texcoords is not None:
            uv = numeric_array(self.texcoords, 'texture coordinates')
            require(uv.shape == (len(v), 2),'Expected one UV pair per vertex.', 'SCENE3D_GEOMETRY')
        area = np.linalg.norm(np.cross(v[f[:, 1]]-v[f[:, 0]], v[f[:, 2]]-v[f[:, 0]]), axis=1)
        require(np.isfinite(area).all() and (area > 1e-12).all(), 'Degenerate geometry.', 'SCENE3D_GEOMETRY')
        return self

    @property
    def bounds(self):
        self.validate()
        v = numeric_array(self.vertices,'vertices')
        return dict(minimum=v.min(axis=0).tolist(), maximum=v.max(axis=0).tolist())

    def obj(self):
        """Write flat normals and explicit OBJ texture coordinates."""
        self.validate()
        lines = ['# Scene3D procedural geometry v1; right handed Y up meters']
        lines += ['v '+' '.join(format(float(x), '.17g') for x in p) for p in self.vertices]
        v = numeric_array(self.vertices,'vertices')
        for a,b,c in self.triangles:
            normal = np.cross(v[b]-v[a], v[c]-v[a]); length=float(np.linalg.norm(normal))
            require(math.isfinite(length) and length>1e-12,'Cannot emit a nonfinite or degenerate normal.')
            normal /= length
            lines.append('vn '+' '.join(format(float(x), '.17g') for x in normal))
        if self.texcoords is None:
            lines += ['vt 0 0','vt 1 0','vt 0 1']
            lines += [f'f {a+1}/1/{i+1} {b+1}/2/{i+1} {c+1}/3/{i+1}' for i,(a,b,c) in enumerate(self.triangles)]
        else:
            lines += ['vt '+' '.join(format(float(x), '.17g') for x in uv) for uv in self.texcoords]
            lines += [f'f {a+1}/{a+1}/{i+1} {b+1}/{b+1}/{i+1} {c+1}/{c+1}/{i+1}'
                      for i,(a,b,c) in enumerate(self.triangles)]
        return ('\n'.join(lines)+'\n').encode()


def combine(parts):
    vertices, triangles = [], []
    for mesh, offset in parts:
        mesh.validate()
        start = len(vertices)
        vertices.extend((np.asarray(mesh.vertices)+offset).tolist())
        triangles.extend([[start+i for i in face] for face in mesh.triangles])
    return Mesh(vertices, triangles).validate()


def box(width=1, height=1, depth=1):
    w,h,d = [positive(x,n)/2 for x,n in zip((width,height,depth),('width','height','depth'))]
    vertices = [[-w,-h,-d],[w,-h,-d],[w,h,-d],[-w,h,-d],[-w,-h,d],[w,-h,d],[w,h,d],[-w,h,d]]
    quads = [(0,3,2,1),(4,5,6,7),(0,4,7,3),(1,2,6,5),(0,1,5,4),(3,7,6,2)]
    return Mesh(vertices, [[q[0],q[1],q[2]] for q in quads]+[[q[0],q[2],q[3]] for q in quads]).validate()


def plane(width=1, depth=1):
    w,d = positive(width,'width')/2, positive(depth,'depth')/2
    return Mesh([[-w,0,-d],[-w,0,d],[w,0,d],[w,0,-d]], [[0,1,2],[0,2,3]])


def revolve(profile, segments=24):
    count(segments,'segments')
    require(len(profile) >= 2 and all(math.isfinite(r) and math.isfinite(y) and r >= 0 for r,y in profile), 'Invalid revolution profile.')
    vertices, rings, faces = [], [], []
    for radius,y in profile:
        if radius == 0:
            rings.append([len(vertices)]); vertices.append([0,y,0])
        else:
            rings.append(list(range(len(vertices),len(vertices)+segments)))
            vertices += [[radius*math.cos(2*math.pi*i/segments), y, radius*math.sin(2*math.pi*i/segments)] for i in range(segments)]
    for a,b in zip(rings,rings[1:]):
        require(len(a)>1 or len(b)>1, 'Adjacent axis points make no surface.')
        for i in range(segments):
            j=(i+1)%segments
            if len(a)==1: faces.append([a[0],b[i],b[j]])
            elif len(b)==1: faces.append([a[i],b[0],a[j]])
            else: faces.extend([[a[i],b[i],b[j]],[a[i],b[j],a[j]]])
    return Mesh(vertices,faces).validate()


def primitive(kind, **p):
    """All dimensions are meters. Unknown parameters fail instead of being ignored."""
    if kind == 'polygon':
        require('points' in p and not set(p)-{'points','holes'},'Invalid polygon parameters.')
        return polygon(**p)
    if kind == 'extrude':
        require(not set(p)-{'points','height','holes'} and 'points' in p,'Invalid extrusion parameters.')
        return extrude(**p)
    if kind == 'curve':
        require(not set(p)-{'points','radius','segments'} and 'points' in p,'Invalid curve parameters.')
        return curve(**p)
    options = {
        'box': {'width','height','depth'}, 'plane': {'width','depth'},
        'cylinder': {'radius','height','segments'}, 'cone': {'radius','height','segments'},
        'sphere': {'radius','segments','rings'}, 'capsule': {'radius','height','segments','rings'},
        'torus': {'radius','tube','segments','rings'}, 'ramp': {'width','height','depth'},
        'stairs': {'width','height','depth','steps'}, 'pipe': {'radius','inner_radius','height','segments'},
        'beam': {'width','height','depth'}, 'platform': {'width','height','depth'},
        'door': {'width','height','depth'}, 'window': {'width','height','depth'}}
    require(isinstance(kind,str) and kind in options and not set(p)-options[kind], 'Unknown primitive or parameter.', 'SCENE3D_GEOMETRY')
    if kind in ('box','beam','platform','door','window'): return box(**p)
    if kind == 'plane': return plane(**p)
    segments=count(p.get('segments',24),'segments')
    r=positive(p.get('radius',1),'radius'); h=positive(p.get('height',2),'height')
    if kind in ('cylinder','cone'):
        return revolve([(0,-h/2),(r,-h/2),(r if kind=='cylinder' else 0,h/2)]+([(0,h/2)] if kind=='cylinder' else []),segments)
    if kind in ('sphere','capsule'):
        rings=count(p.get('rings',12),'rings',4,128)
        require(kind!='capsule' or h>=2*r, 'Capsule height must include its hemispheres.')
        half=0 if kind=='sphere' else h/2-r
        profile=[(0,-half-r)]
        profile += [(r*math.sin(math.pi*i/rings), -half+r*-math.cos(math.pi*i/rings)) for i in range(1,rings//2+1)]
        if half: profile.append((r,half))
        profile += [(r*math.sin(math.pi*i/rings),half-r*math.cos(math.pi*i/rings)) for i in range(rings//2+1,rings)]
        profile += [(0,half+r)]
        return revolve(profile,segments)
    if kind in ('ramp','stairs'):
        w,h,d=[positive(p.get(k,1),k) for k in ('width','height','depth')]
        if kind=='stairs':
            steps=count(p.get('steps',8),'steps',1,128)
            return combine([(box(w,h*(i+1)/steps,d/steps),[0,h*(i+1)/steps/2,-d/2+d*(i+.5)/steps]) for i in range(steps)])
        return Mesh([[-w/2,0,-d/2],[w/2,0,-d/2],[-w/2,0,d/2],[w/2,0,d/2],[-w/2,h,d/2],[w/2,h,d/2]],
                    [[0,2,3],[0,3,1],[0,1,5],[0,5,4],[0,4,2],[1,3,5],[2,4,5],[2,5,3]]).validate()
    if kind=='pipe':
        inner=positive(p.get('inner_radius',r*.75),'inner_radius'); require(inner<r,'Pipe interior must be smaller than exterior.')
        # Closed profile gives actual hollow pipe, including annular end faces.
        return revolve([(r,-h/2),(r,h/2),(inner,h/2),(inner,-h/2),(r,-h/2)],segments)
    tube=positive(p.get('tube',.25),'tube'); require(tube<r,'Torus tube must be smaller than major radius.')
    rings=count(p.get('rings',12),'rings',3,128)
    vertices=[[ (r+tube*math.cos(2*math.pi*j/rings))*math.cos(2*math.pi*i/segments),tube*math.sin(2*math.pi*j/rings),
                (r+tube*math.cos(2*math.pi*j/rings))*math.sin(2*math.pi*i/segments)] for i in range(segments) for j in range(rings)]
    faces=[]
    for i in range(segments):
        for j in range(rings):
            a=i*rings+j;b=((i+1)%segments)*rings+j;c=((i+1)%segments)*rings+(j+1)%rings;d=i*rings+(j+1)%rings
            faces.extend([[a,c,b],[a,d,c]])
    return Mesh(vertices,faces).validate()


def wall(width, height, thickness, openings=()):
    """Tessellate solid regions around rectangular doors/windows; no hidden wall spans openings."""
    w,h,t=[positive(x,n) for x,n in zip((width,height,thickness),('width','height','thickness'))]
    require(isinstance(openings,(list,tuple)) and len(openings)<=32,'Wall supports at most 32 openings.')
    rectangles=[]
    for opening in openings:
        require(isinstance(opening,dict) and set(opening)=={'x','y','width','height'},'Opening needs x/y/width/height.')
        x,y=opening['x'],opening['y'];ow,oh=positive(opening['width'],'opening width'),positive(opening['height'],'opening height')
        require(type(x) in (int,float) and type(y) in (int,float)
                and 0<=x<=w and 0<=y<=h and math.isfinite(x) and math.isfinite(y)
                and x+ow<=w and y+oh<=h,'Opening lies outside wall.')
        rectangles.append((x,y,x+ow,y+oh))
    xs=sorted({0,w,*[v for r in rectangles for v in (r[0],r[2])]})
    ys=sorted({0,h,*[v for r in rectangles for v in (r[1],r[3])]})
    parts=[]
    for x0,x1 in zip(xs,xs[1:]):
        for y0,y1 in zip(ys,ys[1:]):
            if not any(a<=(x0+x1)/2<=c and b<=(y0+y1)/2<=d for a,b,c,d in rectangles):
                parts.append((box(x1-x0,y1-y0,t),[(x0+x1)/2-w/2,(y0+y1)/2,0]))
    require(parts,'Openings remove the whole wall.')
    return combine(parts)


_POLYGON_EPS = 1e-10


def _turn(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def _edges(ring):
    return zip(ring, ring[1:]+ring[:1])


def _on_segment(a, b, point):
    return (abs(_turn(a, b, point)) <= _POLYGON_EPS
            and all(min(x,y)-_POLYGON_EPS <= z <= max(x,y)+_POLYGON_EPS
                    for x,y,z in zip(a,b,point)))


def _intersects(a, b, c, d):
    return ((_turn(a,b,c)*_turn(a,b,d) < 0 and _turn(c,d,a)*_turn(c,d,b) < 0)
            or _on_segment(a,b,c) or _on_segment(a,b,d)
            or _on_segment(c,d,a) or _on_segment(c,d,b))


def _contains(ring, point):
    # Boundaries are already disjoint. Half-open crossings count vertices once.
    x, z = point
    return sum((a[1] > z) != (b[1] > z)
               and x < a[0] + (z-a[1])*(b[0]-a[0])/(b[1]-a[1])
               for a,b in _edges(ring)) % 2 == 1


def _signed_area2(ring):
    return math.fsum(_turn(ring[0], a, b) for a,b in _edges(ring))


def _profile_rings(points, holes):
    require(isinstance(holes, (list, tuple)) and len(holes) <= (256-3)//3,
            'Holes must be a bounded list of polygon rings.')
    sources = [points, *holes]
    require(all(isinstance(r, (list, tuple, np.ndarray)) and (not isinstance(r, np.ndarray) or r.ndim != 0)
                and 3 <= len(r) <= 256 for r in sources)
            and sum(map(len, sources)) <= 256, 'Polygon requires 3 to 256 total corners.')
    rings = []
    for source in sources:
        require(all(isinstance(point, (list, tuple, np.ndarray)) and
                    (not isinstance(point, np.ndarray) or point.ndim == 1) and
                    len(point) == 2 and all(np.isscalar(x) for x in point) for point in source),
                'Polygon corners must be XZ pairs.')
        if rings:
            require(all(isinstance(x, (int, float, np.integer, np.floating)) and
                        not isinstance(x, (bool, np.bool_)) for point in source for x in point),
                    'Polygon hole coordinates must be numbers.')
        p = numeric_array(source, 'polygon ring')
        require(p.shape == (len(source), 2) and (np.abs(p) <= 10000).all(),
                'Polygon coordinates must fit the bounded 10000 meter profile.')
        ring = p.tolist()
        edges = list(_edges(ring))
        for i,(a,b) in enumerate(edges):
            require(math.dist(a,b) > 1e-8 and abs(_turn(ring[i-1],a,b)) > _POLYGON_EPS,
                    'Polygon has a collinear or repeated corner.')
            for j in range(i+2, len(edges)):
                if i or j != len(edges)-1:
                    require(not _intersects(a,b,*edges[j]), 'Polygon ring self-intersects.')
        require(abs(_signed_area2(ring)) > _POLYGON_EPS, 'Polygon ring has no area.')
        for previous in rings:
            require(not any(_intersects(a,b,c,d) for a,b in edges for c,d in _edges(previous)),
                    'Polygon rings touch or intersect.')
        if rings:
            require(_contains(rings[0], ring[0]), 'Polygon hole lies outside its outline.')
            require(not any(_contains(hole,ring[0]) or _contains(ring,hole[0]) for hole in rings[1:]),
                    'Polygon holes may not nest.')
        rings.append(ring)
    return rings


def _in_sector(before, at, after, target):
    # The interior lies to the left of this oriented boundary. At a duplicated
    # bridge endpoint each occurrence has its own interior sector.
    incoming = _turn(before,at,target) > _POLYGON_EPS
    outgoing = _turn(at,after,target) > _POLYGON_EPS
    if _turn(before,at,after) > 0:
        return incoming and outgoing
    return incoming or outgoing


def _bridge_holes(vertices, boundaries):
    # Connect each rightmost hole vertex to a visible vertex of the joined
    # boundary. Duplicate only indices: each seam disappears in the final mesh.
    boundary = boundaries[0].copy()
    edges = [edge for ring in boundaries for edge in _edges(ring)]
    holes = []
    for ring in boundaries[1:]:
        right = max(range(len(ring)), key=lambda i: vertices[ring[i]])
        holes.append(ring[right:] + ring[:right])
    for ring in sorted(holes, key=lambda r: vertices[r[0]], reverse=True):
        tip = ring[0]
        a = vertices[tip]
        candidates = sorted(range(len(boundary)), key=lambda i: math.dist(a, vertices[boundary[i]]))
        for i in candidates:
            end = boundary[i]
            b = vertices[end]
            if not (_in_sector(vertices[ring[-1]],a,vertices[ring[1]],b)
                    and _in_sector(vertices[boundary[i-1]],b,vertices[boundary[(i+1)%len(boundary)]],a)):
                continue
            for u,v in edges:
                if u in (tip,end) or v in (tip,end):
                    # A shared endpoint is valid, an overlapping edge is not.
                    other = vertices[v if u in (tip,end) else u]
                    if _on_segment(a,b,other):
                        break
                elif _intersects(a,b,vertices[u],vertices[v]):
                    break
            else:
                boundary[i+1:i+1] = [*ring, tip, end]
                edges.append((tip,end))
                break
        else:
            require(False, 'Polygon hole cannot be connected without degeneracy.')
    return boundary


def _triangulate_profile(points, holes):
    rings = _profile_rings(points, holes)
    vertices = []
    boundaries = []
    for i,ring in enumerate(rings):
        indices = list(range(len(vertices), len(vertices)+len(ring)))
        vertices.extend(ring)
        # CCW outer outline, CW hole outlines in the native XZ plane.
        if (_signed_area2(ring) > 0) != (i == 0):
            indices.reverse()
        boundaries.append(indices)
    indices = _bridge_holes(vertices, boundaries)
    faces = []
    while len(indices) > 3:
        for k,b in enumerate(indices):
            a, c = indices[k-1], indices[(k+1)%len(indices)]
            pa, pb, pc = vertices[a], vertices[b], vertices[c]
            if _turn(pa,pb,pc) <= _POLYGON_EPS:
                continue
            if any(_turn(pa,pb,vertices[q]) >= -_POLYGON_EPS
                   and _turn(pb,pc,vertices[q]) >= -_POLYGON_EPS
                   and _turn(pc,pa,vertices[q]) >= -_POLYGON_EPS
                   for q in indices if q not in (a,b,c)):
                continue
            faces.append([a,b,c])
            indices.pop(k)
            break
        else:
            require(False, 'Polygon cannot be triangulated without degeneracy.')
    require(_turn(*(vertices[i] for i in indices)) > 1e-12,
            'Polygon cap has a degenerate final triangle.')
    faces.append(indices)
    return vertices, faces, boundaries


def polygon_triangles(points, holes=()):
    """Triangulate a bounded XZ outline with optional disjoint hole rings."""
    vertices, faces, _ = _triangulate_profile(points, holes)
    return np.asarray(vertices), faces


def polygon(points, holes=()):
    vertices, faces = polygon_triangles(points, holes)
    return Mesh([[x,0,z] for x,z in vertices], [[a,c,b] for a,b,c in faces]).validate()


def extrude(points, height=1, holes=()):
    """Extrude a bounded XZ profile with through-holes into a closed solid."""
    h = positive(height, 'height')
    points, cap, boundaries = _triangulate_profile(points, holes)
    n = len(points)
    vertices = [[x,y,z] for y in (0,h) for x,z in points]
    faces = []
    for a,b,c in cap:
        faces.extend([[a,b,c], [n+a,n+c,n+b]])
    # Keep the original edge order and triangle diagonal for legacy OBJ hashes.
    for ring in boundaries:
        forward = ring[0] < ring[-1]
        ordered = ring if forward else ring[::-1]
        for i,j in _edges(ordered):
            sides = [[i,n+i,n+j], [i,n+j,j]]
            faces.extend(sides if forward else [face[::-1] for face in sides])
    return Mesh(vertices, faces).validate()


def curve(points, radius=.1, segments=12):
    """Piecewise linear tube, using independent capped spans as editable solid geometry."""
    p=numeric_array(points,'points'); r=positive(radius,'radius'); count(segments,'segments')
    require(p.ndim==2 and p.shape[1]==3 and 2<=len(p)<=256 and np.isfinite(p).all() and (np.abs(p)<=10000).all(),'Invalid curve points.')
    parts=[]
    for a,b in zip(p,p[1:]):
        direction=b-a; length=float(np.linalg.norm(direction)); positive(length,'curve span')
        y=direction/length; reference=np.array([0.,1.,0.]) if abs(y[1])<.9 else np.array([1.,0.,0.])
        x=np.cross(y,reference); x/=np.linalg.norm(x); z=np.cross(x,y)
        mesh=primitive('cylinder',radius=r,height=length,segments=segments)
        mesh.vertices=(np.asarray(mesh.vertices)@np.column_stack([x,y,z]).T+(a+b)/2).tolist()
        parts.append((mesh,[0,0,0]))
    return combine(parts)
