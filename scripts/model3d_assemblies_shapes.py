"""Bounded assembly forms over the existing editable polygon authority."""
from __future__ import annotations
import math
import numpy as np
from model3d_primitives import primitive, editable_mesh
from model3d_coordinates import transform_editable
from rig_errors import require

BASIC = {'box', 'plane', 'sphere', 'cylinder', 'cone', 'torus'}
FORMS = BASIC | {'ellipsoid', 'capsule', 'tube', 'curve', 'extrusion'}


def _number(value, label, minimum=1e-4, maximum=1000.):
    require(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum,
            'MODEL3D_ASSEMBLY_PARAMETER', f'{label} must be finite within [{minimum}, {maximum}].')
    return float(value)


def parameters(form, values):
    """Normalize parameters and compute conservative allocation bounds, without meshes."""
    require(form in FORMS and isinstance(values, dict), 'MODEL3D_ASSEMBLY_FORM', 'Unknown assembly form or parameters.')
    fields = {'box': {'width':1.,'height':1.,'depth':1.}, 'plane': {'width':1.,'depth':1.},
              'sphere': {'radius':.5,'segments':16,'rings':8},
              'cylinder': {'radius':.5,'height':1.,'segments':16},
              'cone': {'radius':.5,'height':1.,'segments':16},
              'torus': {'major_radius':1.,'minor_radius':.25,'segments':16,'rings':8},
              'ellipsoid': {'radii':[.5,.5,.5],'segments':16,'rings':8},
              'capsule': {'radius':.25,'height':1.,'segments':16,'rings':8},
              'tube': {'radius':.5,'inner_radius':.4,'height':1.,'segments':16},
              'curve': {'points':None,'radius':.05,'segments':8},
              'extrusion': {'polygon':None,'height':1.}}
    require(not set(values)-set(fields[form]), 'MODEL3D_ASSEMBLY_PARAMETER', 'Unknown form parameter.')
    p = dict(fields[form], **values)
    for key, value in p.items():
        if key in ('segments','rings'):
            minimum = 3 if key == 'segments' or form == 'torus' else 2
            require(type(value) is int and minimum <= value <= 64,
                    'MODEL3D_ASSEMBLY_PARAMETER', f'{key} must be an integer in [{minimum},64].')
        elif key == 'radii':
            require(isinstance(value,list) and len(value)==3, 'MODEL3D_ASSEMBLY_PARAMETER', 'Ellipsoid needs three radii.')
            p[key]=[_number(v,key) for v in value]
        elif key in ('points','polygon'):
            width=3 if key=='points' else 2
            minimum=2 if key=='points' else 3
            require(isinstance(value,list) and minimum<=len(value)<=64,
                    'MODEL3D_ASSEMBLY_PARAMETER', f'{key} needs {minimum}..64 coordinates.')
            require(all(isinstance(row,list) and len(row)==width for row in value),
                    'MODEL3D_ASSEMBLY_PARAMETER', 'Invalid point dimension.')
            p[key]=[[_number(v,key,-1000,1000) for v in row] for row in value]
        else:p[key]=_number(value,key)
    if form in ('torus','tube'):
        require(p['major_radius']>p['minor_radius'] if form=='torus' else p['radius']>p['inner_radius'],
                'MODEL3D_ASSEMBLY_PARAMETER', 'Outer/major radius must exceed inner/minor radius.')
    if form=='capsule':require(p['height']>=2*p['radius'], 'MODEL3D_ASSEMBLY_PARAMETER', 'Capsule height must include both hemispheres.')
    n=p.get('segments',4);r=p.get('rings',4)
    if form=='box':v,t=8,12
    elif form=='plane':v,t=4,2
    elif form in ('sphere','ellipsoid'):v,t=2+n*(r-1),2*n*(r-1)
    elif form=='capsule':v,t=2+2*r*n,4*r*n
    elif form=='cylinder':v,t=2*n,4*n-4
    elif form=='cone':v,t=n+1,2*n-2
    elif form=='torus':v,t=n*r,2*n*r
    elif form=='tube':v,t=4*n,8*n
    elif form=='curve':v,t=n*len(p['points']),2*n*len(p['points'])
    else:v,t=2*len(p['polygon']),4*len(p['polygon'])-4
    return p,(v,t)


def build_shape(form, values, *, name, limits):
    p,_=parameters(form,values)
    if form in BASIC:return primitive(form,p,name=name,limits=limits)
    if form=='ellipsoid':
        item=primitive('sphere',dict(radius=1.,segments=p['segments'],rings=p['rings']),name=name,limits=limits)
        return transform_editable(item,np.diag([*p['radii'],1.]),limits=limits)
    points=[];faces=[]
    n=p.get('segments',8)
    if form=='tube':
        for y, radius in [(-p['height']/2,p['radius']),(p['height']/2,p['radius']),
                          (-p['height']/2,p['inner_radius']),(p['height']/2,p['inner_radius'])]:
            points.extend([[radius*math.cos(2*math.pi*i/n),y,radius*math.sin(2*math.pi*i/n)] for i in range(n)])
        for i in range(n):
            j=(i+1)%n
            faces.extend([[i,n+i,n+j,j],[2*n+j,3*n+j,3*n+i,2*n+i],
                          [i,j,2*n+j,2*n+i],[n+j,n+i,3*n+i,3*n+j]])
    elif form=='capsule':
        radius=p['radius'];half=p['height']/2-radius;r=p['rings']
        profile=[]
        for j in range(1,r+1):
            angle=-math.pi/2+j*math.pi/(2*r)
            profile.append((radius*math.cos(angle),-half+radius*math.sin(angle)))
        if half>0:profile.append((radius,half))
        for j in range(1,r):
            angle=j*math.pi/(2*r)
            profile.append((radius*math.cos(angle),half+radius*math.sin(angle)))
        points=[[0,-p['height']/2,0]]
        for radius,y in profile:points.extend([[radius*math.cos(2*math.pi*i/n),y,radius*math.sin(2*math.pi*i/n)] for i in range(n)])
        top=len(points);points.append([0,p['height']/2,0])
        faces=[[0,1+i,1+(i+1)%n] for i in range(n)]
        for row in range(len(profile)-1):
            a=1+row*n;b=a+n
            faces.extend([[a+i,b+i,b+(i+1)%n,a+(i+1)%n] for i in range(n)])
        a=1+(len(profile)-1)*n;faces.extend([[a+i,top,a+(i+1)%n] for i in range(n)])
    elif form=='extrusion':
        polygon=p['polygon'];count=len(polygon)
        points=[[x,y,z] for y in (-p['height']/2,p['height']/2) for x,z in polygon]
        # Canonicalize the 2D winding; the native triangulator rejects crossings.
        area=sum(polygon[i][0]*polygon[(i+1)%count][1]-polygon[(i+1)%count][0]*polygon[i][1] for i in range(count))
        require(abs(area)>1e-12,'MODEL3D_ASSEMBLY_PARAMETER','Extrusion polygon has no signed area.')
        loop=list(range(count)) if area>0 else list(reversed(range(count)))
        faces=[loop,[count+i for i in reversed(loop)]]
        faces.extend([[i,count+i,count+j,j] for i,j in zip(loop,loop[1:]+loop[:1])])
    else:
        centers=np.asarray(p['points']);delta=np.diff(centers,axis=0)
        lengths=np.linalg.norm(delta,axis=1)
        require(np.all(lengths>2*p['radius']), 'MODEL3D_ASSEMBLY_PARAMETER', 'Curve segments must exceed twice their tube radius.')
        directions=delta/lengths[:,None]
        tangents=np.concatenate((directions[:1],directions[:-1]+directions[1:],directions[-1:]))
        norms=np.linalg.norm(tangents,axis=1)
        require(np.all(norms>1e-6),'MODEL3D_ASSEMBLY_PARAMETER','Curve cannot reverse at a joint.')
        tangents/=norms[:,None]
        # Parallel transport a stable ring frame along the polyline.
        u=np.cross(tangents[0],[0,1,0] if abs(tangents[0,1])<.9 else [1,0,0]);u/=np.linalg.norm(u)
        for center,tangent in zip(centers,tangents):
            u=u-tangent*np.dot(u,tangent)
            if np.linalg.norm(u)<1e-6:u=np.cross(tangent,[0,1,0] if abs(tangent[1])<.9 else [1,0,0])
            u/=np.linalg.norm(u);v=np.cross(tangent,u)
            points.extend([(center+p['radius']*(math.cos(2*math.pi*i/n)*u+math.sin(2*math.pi*i/n)*v)).tolist() for i in range(n)])
        faces=[list(reversed(range(n))),list(range((len(centers)-1)*n,len(centers)*n))]
        for row in range(len(centers)-1):
            a=row*n;b=a+n
            # Bent adjacent rings form ruled strips, whose quads need not be
            # planar. Retain their explicit triangle topology for native import.
            for i in range(n):
                j=(i+1)%n
                faces.extend([[a+i,a+j,b+j],[a+i,b+j,b+i]])
    return editable_mesh(points,faces,name=name,limits=limits)
