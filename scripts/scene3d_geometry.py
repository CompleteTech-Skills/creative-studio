"""Bounded, deterministic Y-up mesh construction for editable Scene3D assets."""
from dataclasses import dataclass
import math
import numpy as np
from scene3d_graph import require

MAX_TRIANGLES = 200000


def positive(value, name):
    require(type(value) in (int, float) and math.isfinite(value) and 0 < value <= 10000,
            name + ' must be finite, positive and at most 10000.', 'SCENE3D_GEOMETRY')
    return float(value)


def count(value, name, low=3, high=256):
    require(type(value) is int and low <= value <= high, name + ' is outside its integer budget.', 'SCENE3D_GEOMETRY')
    return value


@dataclass
class Mesh:
    vertices: list
    triangles: list

    def validate(self, budget=MAX_TRIANGLES):
        count(budget, 'triangle budget', 1, MAX_TRIANGLES)
        v = np.asarray(self.vertices, dtype=float)
        f = np.asarray(self.triangles)
        require(v.ndim == 2 and v.shape[1] == 3 and 3 <= len(v) <= 200000 and np.isfinite(v).all(),
                'Invalid or excessive mesh vertices.', 'SCENE3D_GEOMETRY')
        require(f.ndim == 2 and f.shape[1] == 3 and 1 <= len(f) <= budget and f.dtype.kind in 'iu'
                and f.min() >= 0 and f.max() < len(v), 'Invalid or excessive triangle indices.', 'SCENE3D_GEOMETRY')
        area = np.linalg.norm(np.cross(v[f[:, 1]]-v[f[:, 0]], v[f[:, 2]]-v[f[:, 0]]), axis=1)
        require((area > 1e-12).all(), 'Degenerate geometry.', 'SCENE3D_GEOMETRY')
        return self

    @property
    def bounds(self):
        self.validate()
        v = np.asarray(self.vertices)
        return dict(minimum=v.min(axis=0).tolist(), maximum=v.max(axis=0).tolist())

    def obj(self):
        """Separate vertex normals preserve hard edges; planar per-face UVs are explicit."""
        self.validate()
        lines = ['# Scene3D procedural geometry v1; right handed Y up meters']
        lines += ['v '+' '.join(format(float(x), '.17g') for x in p) for p in self.vertices]
        v = np.asarray(self.vertices)
        for a,b,c in self.triangles:
            normal = np.cross(v[b]-v[a], v[c]-v[a]); normal /= np.linalg.norm(normal)
            lines.append('vn '+' '.join(format(float(x), '.17g') for x in normal))
        lines += ['vt 0 0','vt 1 0','vt 0 1']
        lines += [f'f {a+1}/1/{i+1} {b+1}/2/{i+1} {c+1}/3/{i+1}' for i,(a,b,c) in enumerate(self.triangles)]
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
    options = {
        'box': {'width','height','depth'}, 'plane': {'width','depth'},
        'cylinder': {'radius','height','segments'}, 'cone': {'radius','height','segments'},
        'sphere': {'radius','segments','rings'}, 'capsule': {'radius','height','segments','rings'},
        'torus': {'radius','tube','segments','rings'}, 'ramp': {'width','height','depth'},
        'stairs': {'width','height','depth','steps'}, 'pipe': {'radius','inner_radius','height','segments'},
        'beam': {'width','height','depth'}, 'platform': {'width','height','depth'},
        'door': {'width','height','depth'}, 'window': {'width','height','depth'}}
    require(kind in options and not set(p)-options[kind], 'Unknown primitive or parameter.', 'SCENE3D_GEOMETRY')
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
        require(type(x) in (int,float) and type(y) in (int,float) and math.isfinite(x) and math.isfinite(y)
                and 0<=x<x+ow<=w and 0<=y<y+oh<=h,'Opening lies outside wall.')
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


def extrude(points, height=1):
    """Extrude a strictly convex XZ polygon; reject concave/self-intersecting profiles."""
    h=positive(height,'height'); p=np.asarray(points,dtype=float)
    require(p.ndim==2 and p.shape[1]==2 and 3<=len(p)<=256 and np.isfinite(p).all(),'Invalid polygon profile.')
    edges=np.roll(p,-1,axis=0)-p
    crosses=edges[:,0]*np.roll(edges,-1,axis=0)[:,1]-edges[:,1]*np.roll(edges,-1,axis=0)[:,0]
    require((crosses>1e-10).all() or (crosses< -1e-10).all(),'Profile must be strictly convex and ordered.')
    # A convex profile must place every remaining point on the same edge side.
    for i,e in enumerate(edges):
        deltas=p-p[i]
        side=e[0]*deltas[:,1]-e[1]*deltas[:,0]
        require((side>=-1e-10).all() or (side<=1e-10).all(),'Profile self-intersects.')
    if crosses[0]<0: p=p[::-1]
    n=len(p); v=[[x,y,z] for y in (0,h) for x,z in p]; f=[]
    for i in range(1,n-1): f.extend([[0,i,i+1],[n,n+i+1,n+i]])
    for i in range(n):
        j=(i+1)%n; f.extend([[i,n+i,n+j],[i,n+j,j]])
    return Mesh(v,f).validate()


def curve(points, radius=.1, segments=12):
    """Piecewise linear tube, using independent capped spans as editable solid geometry."""
    p=np.asarray(points,dtype=float); r=positive(radius,'radius'); count(segments,'segments')
    require(p.ndim==2 and p.shape[1]==3 and 2<=len(p)<=256 and np.isfinite(p).all(),'Invalid curve points.')
    parts=[]
    for a,b in zip(p,p[1:]):
        direction=b-a; length=float(np.linalg.norm(direction)); positive(length,'curve span')
        y=direction/length; reference=np.array([0.,1.,0.]) if abs(y[1])<.9 else np.array([1.,0.,0.])
        x=np.cross(y,reference); x/=np.linalg.norm(x); z=np.cross(x,y)
        mesh=primitive('cylinder',radius=r,height=length,segments=segments)
        mesh.vertices=(np.asarray(mesh.vertices)@np.column_stack([x,y,z]).T+(a+b)/2).tolist()
        parts.append((mesh,[0,0,0]))
    return combine(parts)
