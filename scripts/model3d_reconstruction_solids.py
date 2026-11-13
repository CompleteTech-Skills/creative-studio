"""Exact contour extrusion, explicit revolution and finite-thickness sampled relief.

No segmentation or hidden anatomy is inferred. Polygon loops are authored planar
observations; sidewalls/depth/symmetry are explicit hypotheses. Closed solids use
native Mesh/Geometry and validate edge and vertex manifoldness before returning.
"""
from __future__ import annotations
from fractions import Fraction
import math
import numpy as np
from model3d_reconstruction import _number,_array,_budget,_result,_seal_result
from rig_errors import require


def _cross(a,b,c):return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
def _on(a,b,p):return _cross(a,b,p)==0 and min(a[0],b[0])<=p[0]<=max(a[0],b[0]) and min(a[1],b[1])<=p[1]<=max(a[1],b[1])
def _intersects(a,b,c,d):
    x,y,z,w=_cross(a,b,c),_cross(a,b,d),_cross(c,d,a),_cross(c,d,b)
    return (x*y<0 and z*w<0) or any((v==0 and _on(p,q,r)) for v,p,q,r in [(x,a,b,c),(y,a,b,d),(z,c,d,a),(w,c,d,b)])
def _inside(point,loop):
    x,y=point;inside=False
    for a,b in zip(loop,loop[1:]+loop[:1]):
        require(not _on(a,b,point),'RECON_CONTOUR','Hole boundaries cannot touch another loop.')
        if (a[1]>y)!=(b[1]>y):
            if x < a[0]+(y-a[1])*(b[0]-a[0])/(b[1]-a[1]):inside=not inside
    return inside


def _loops(outer,holes):
    require(isinstance(outer,(list,tuple,np.ndarray)) and isinstance(holes,(list,tuple)) and len(holes)<=16,
            'RECON_CONTOUR','Supply one outer polygon and at most16nonintersecting holes.')
    loops=[];total=0
    for source in [outer,*holes]:
        require(isinstance(source,(list,tuple,np.ndarray)) and 3<=len(source)<=256,
                'RECON_CONTOUR','Closed loops require3..256distinct points; do not repeat their start.')
        total+=len(source);require(total<=256,'RECON_LIMIT','Contour set exceeds256vertices.')
        values=_array(source,(len(source),2),'contour')
        loop=[tuple(Fraction(float(v)) for v in point) for point in values]
        require(len(set(loop))==len(loop),'RECON_CONTOUR','Contour repeats a vertex.')
        edges=list(zip(loop,loop[1:]+loop[:1]))
        require(sum(a[0]*b[1]-a[1]*b[0] for a,b in edges)!=0,'RECON_CONTOUR','Contour has zero signed area.')
        for i,(a,b) in enumerate(edges):
            for j,(c,d) in enumerate(edges):
                if j<=i or j==i+1 or i==0 and j==len(edges)-1:continue
                require(not _intersects(a,b,c,d),'RECON_CONTOUR','Crossing/touching/open sketch loops need explicit repair.')
        # Adjacent collinear reversal would create an overlapping boundary.
        for i,b in enumerate(loop):
            a,c=loop[i-1],loop[(i+1)%len(loop)]
            require(not (_cross(a,b,c)==0 and (_on(a,b,c) or _on(b,c,a))), 'RECON_CONTOUR','Adjacent contour edges overlap.')
        loops.append(loop)
    for i,left in enumerate(loops):
        for right in loops[i+1:]:
            require(not any(_intersects(a,b,c,d) for a,b in zip(left,left[1:]+left[:1]) for c,d in zip(right,right[1:]+right[:1])),
                    'RECON_CONTOUR','Contours cannot cross or touch.')
    for i,hole in enumerate(loops[1:]):
        require(_inside(hole[0],loops[0]),'RECON_CONTOUR','Every hole must lie strictly inside the outer contour.')
        require(not any(_inside(hole[0],other) or _inside(other[0],hole) for other in loops[1:i+1]),
                'RECON_CONTOUR','Nested or overlapping holes need explicit region ownership.')
    return loops


def extrude_contour(outer, *, holes=(), thickness, evidence=None):
    """Extrude exact simple XY polygons/holes along Z, centered at Z=0.

    Rational slab tessellation preserves concave polygons and holes without
    bridging or filling them. Winding of supplied loops is immaterial. Crossings,
    touch points, ambiguous ownership and nonmanifold results reject explicitly.
    """
    thickness=_number(thickness,positive=True);loops=_loops(outer,holes)
    xs=sorted({point[0] for loop in loops for point in loop})
    edges=[(a,b) for loop in loops for a,b in zip(loop,loop[1:]+loop[:1]) if a[0]!=b[0]]
    slabs=[];cuts={x:set() for x in xs}
    for left,right in zip(xs,xs[1:]):
        middle=(left+right)/2
        crossed=[]
        for a,b in edges:
            if min(a[0],b[0])<middle<max(a[0],b[0]):
                def y(x):return a[1]+(x-a[0])*(b[1]-a[1])/(b[0]-a[0])
                crossed.append((y(middle),y(left),y(right)))
        crossed.sort();require(len(crossed)%2==0,'RECON_CONTOUR','Contour intersection parity is invalid.')
        for lower,upper in zip(crossed[::2],crossed[1::2]):
            assert lower[0]<upper[0]
            slab=(left,right,lower[1],lower[2],upper[1],upper[2]);slabs.append(slab)
            cuts[left].update((lower[1],upper[1]));cuts[right].update((lower[2],upper[2]))
    points=[];indices={};caps=[]
    def vertex(point):
        if point not in indices:indices[point]=len(points);points.append(point)
        return indices[point]
    for left,right,ll,lr,ul,ur in slabs:
        polygon=[(left,ll),(right,lr)]
        polygon.extend((right,y) for y in sorted(cuts[right]) if lr<y<ur)
        polygon.extend(((right,ur),(left,ul)))
        polygon.extend((left,y) for y in sorted(cuts[left],reverse=True) if ll<y<ul)
        polygon=[p for i,p in enumerate(polygon) if p!=polygon[i-1]]
        center=(sum(p[0] for p in polygon)/len(polygon),sum(p[1] for p in polygon)/len(polygon))
        ci=vertex(center)
        for a,b in zip(polygon,polygon[1:]+polygon[:1]):caps.append((ci,vertex(a),vertex(b)))
        _budget(len(points)*2,len(caps)*2+len(points)*2)
    n=len(points);positions=[[float(x),float(y),z] for z in (-thickness/2,thickness/2) for x,y in points]
    triangles=[(c,b,a) for a,b,c in caps]+[(a+n,b+n,c+n) for a,b,c in caps]
    boundary={}
    for tri in caps:
        for a,b in zip(tri,tri[1:]+tri[:1]):
            key=tuple(sorted((a,b)))
            if key in boundary:boundary[key]=None
            else:boundary[key]=(a,b)
    for edge in boundary.values():
        if edge is not None:
            a,b=edge;triangles.extend(((a,b,b+n),(a,b+n,a+n)))
    result=_result(positions,triangles,method='contour-extrusion',parameters=dict(outer=np.asarray(outer).tolist(),holes=[np.asarray(h).tolist() for h in holes],thickness_m=thickness,axis='+Z'),evidence=evidence)
    result.report['surface_regions']=[dict(faces=[0,2*len(caps)],classification='procedurally-inferred',constraint='Cap outlines are exactly constrained by supplied planar contours; Z placement and hidden cap are inferred.'),dict(faces=[2*len(caps),len(triangles)],classification='procedurally-inferred',constraint='Sidewalls connect matching contour boundaries over explicit thickness.')]
    return _seal_result(result)


def revolve_profile(profile, *, segments=32, cap_ends=True, evidence=None):
    """Revolve an explicit [radius_m,Y_m] polyline around the canonical +Y axis.

    Profile follows strictly increasing Y with nonnegative radius. It may reach
    the axis only at its ends. Rotational symmetry and hidden surfaces remain
    inferred; UV0 maps azimuth and normalized profile arc length. Seam vertices
    are split only for UVs after closed topology has been verified.
    """
    require(type(segments) is int and 3<=segments<=512 and type(cap_ends) is bool,
            'RECON_PARAMETER','Use3..512azimuth segments and explicit cap_ends boolean.')
    require(isinstance(profile,(list,tuple,np.ndarray)) and 2<=len(profile)<=512,'RECON_PROFILE','Profile requires2..512points.')
    values=_array(profile,(len(profile),2),'profile')
    require(np.all(values[:,0]>=0) and np.all(np.diff(values[:,1])>0) and np.all(values[1:-1,0]>0)
            and np.max(values[:,0])>0,'RECON_PROFILE','Profile Y must increase; radius is nonnegative and interior radii positive.')
    _budget(len(values)*(segments+1)+2,2*segments*len(values))
    positions=[];rings=[];uv=[]
    lengths=np.r_[0,np.cumsum(np.linalg.norm(np.diff(values,axis=0),axis=1))];lengths/=lengths[-1]
    for j,(radius,y) in enumerate(values):
        if radius==0:rings.append([len(positions)]);positions.append([0,y,0]);uv.append([.5,float(lengths[j])])
        else:
            ring=[]
            for i in range(segments):
                angle=2*math.pi*i/segments;ring.append(len(positions));positions.append([radius*math.cos(angle),y,radius*math.sin(angle)]);uv.append([i/segments,float(lengths[j])])
            rings.append(ring)
    triangles=[]
    for left,right in zip(rings,rings[1:]):
        for i in range(segments):
            k=(i+1)%segments
            if len(left)==1:triangles.append((left[0],right[i],right[k]))
            elif len(right)==1:triangles.append((left[i],right[0],left[k]))
            else:triangles.extend(((left[i],right[i],right[k]),(left[i],right[k],left[k])))
    if cap_ends:
        for ring,(radius,y),bottom in [(rings[0],values[0],True),(rings[-1],values[-1],False)]:
            if len(ring)==1:continue
            center=len(positions);positions.append([0,y,0]);uv.append([.5,0 if bottom else 1])
            for i in range(segments):
                k=(i+1)%segments
                triangles.append((center,ring[i],ring[k]) if bottom else (center,ring[k],ring[i]))
    closed=cap_ends or values[0,0]==values[-1,0]==0
    result=_result(positions,triangles,method='profile-revolution',parameters=dict(profile=values.tolist(),segments=segments,cap_ends=cap_ends,axis='+Y',symmetry='explicit rotational assumption'),evidence=evidence,uv=uv,closed=bool(closed))
    # UV seam splitting must not masquerade as a geometric boundary in the
    # quality receipt. Duplicate u=0 corners at u=1 only on crossing triangles.
    mesh=result.geometry.meshes[0];points=mesh.positions.tolist();coords=mesh.attributes['TEXCOORD_0'].tolist();normals=mesh.attributes['NORMAL'].tolist();seams={}
    faces=mesh.triangles.copy()
    for face in faces:
        us=[coords[v][0] for v in face]
        if max(us)-min(us)>.5:
            for j,v in enumerate(face):
                if coords[v][0]==0:
                    if int(v) not in seams:
                        seams[int(v)]=len(points);points.append(points[v]);coords.append([1,coords[v][1]]);normals.append(normals[v])
                    face[j]=seams[int(v)]
    mesh.positions=np.asarray(points);mesh.triangles=faces;mesh.attributes['NORMAL']=np.asarray(normals);mesh.attributes['TEXCOORD_0']=np.asarray(coords)
    result.report['surface_regions']=[dict(faces=[0,len(faces)],classification='procedurally-inferred',constraint='Supplied meridian profile; rotational symmetry and hidden azimuths explicitly assumed.')]
    result.report['topology_domain']='shared geometric surface before render UV seam duplication'
    result.report['uv_seam']='Render seam duplicates u=0 at u=1; geometric topology receipt refers to welded source surface.'
    return _seal_result(result)


def heightfield(heights, *, spacing, base_thickness, evidence=None):
    """Closed XZ sampled relief; heights measured above a Y=0 front datum.

    Positive base_thickness places a flat bottom at Y=-base_thickness. Supplied
    heights are metric authored/derived values, never guessed luminance-depth.
    Bottom and side topology share sampled edge vertices, including on ridges.
    """
    require(isinstance(heights,(list,tuple,np.ndarray)),'RECON_HEIGHT','Expected a bounded2Dheight array.')
    require(isinstance(heights,np.ndarray) and heights.ndim==2 or isinstance(heights,(list,tuple)) and 2<=len(heights)<=256 and all(isinstance(row,(list,tuple,np.ndarray)) and len(row)==len(heights[0]) for row in heights),'RECON_HEIGHT','Heightfield rows must have equal bounded dimensions.')
    require(2<=len(heights)<=256 and 2<=len(heights[0])<=256,'RECON_HEIGHT','Heightfield dimensions must stay in2..256.')
    values=_array(heights,(len(heights),len(heights[0])),'heightfield')
    require(values.ndim==2 and 2<=values.shape[0]<=256 and 2<=values.shape[1]<=256
            and values.dtype.kind in 'fiu','RECON_HEIGHT','Heightfield grid must be2..256per dimension and numeric.')
    require(np.isfinite(values).all() and np.all(values>=-1e6) and np.all(values<=1e6),'RECON_HEIGHT','Height samples must be finite metric values.')
    spacing=_array(spacing,(2,),'spacing');require(np.all(spacing>0),'RECON_HEIGHT','Grid spacing must be positive.')
    base=_number(base_thickness,positive=True);require(np.min(values)>-base,'RECON_HEIGHT','Front surface must stay strictly above the finite base.')
    h,w=values.shape;_budget(2*h*w,4*(h-1)*(w-1)+4*(h+w-2))
    positions=[[x*spacing[0],float(values[y,x]) if side==0 else -base,y*spacing[1]] for side in (0,1) for y in range(h) for x in range(w)]
    n=h*w;triangles=[]
    for y in range(h-1):
        for x in range(w-1):
            a=y*w+x;b=a+1;c=a+w;d=c+1
            triangles.extend(((a,c,b),(b,c,d),(a+n,b+n,c+n),(b+n,d+n,c+n)))
    loop=list(range(w))+[y*w+w-1 for y in range(1,h)]+list(range((h-1)*w+w-2,(h-1)*w-1,-1))+[y*w for y in range(h-2,0,-1)]
    for a,b in zip(loop,loop[1:]+loop[:1]):triangles.extend(((a,b,b+n),(a,b+n,a+n)))
    uv=[[x/(w-1),y/(h-1)] for side in (0,1) for y in range(h) for x in range(w)]
    result=_result(positions,triangles,method='finite-heightfield',parameters=dict(spacing_m=spacing.tolist(),base_thickness_m=base,grid_shape=[h,w],heights_m=values.tolist(),surface_axes='X+Z; height+Y'),evidence=evidence,uv=uv)
    # Grid triangles alternate front/bottom; retain exact attribution by index.
    result.report['surface_regions']=[dict(face_pattern='indices4k,4k+1for k in0..'+str((h-1)*(w-1)-1),classification='geometrically-constrained',constraint='Supplied sampled heights; linear triangle interiors are inferred.'),dict(face_pattern='remaining faces',classification='procedurally-inferred',constraint='Flat bottom and closing walls use explicit base thickness.')]
    return _seal_result(result)


def extrude_mask(mask, *, pixel_size, thickness, origin=(0,0), evidence=None):
    """Extrude exact binary pixel cells, retaining every resolved hole/component.

    Pixel X maps+X; raster Y maps-Y. Pixel corners use supplied metric origin.
    No cleanup/resampling/semantic component labels are silently applied. Diagonal
    point/edge contacts reject as nonmanifold and require an explicit mask edit.
    """
    try:values=np.asarray(mask)
    except (ValueError,TypeError,OverflowError):require(False,'RECON_MASK','Mask must be a rectangular boolean array.')
    require(values.ndim==2 and values.dtype.kind=='b' and 0<values.size<=262144 and values.any(),
            'RECON_MASK','Supply a nonempty binary mask of at most262144pixels.')
    pixel=_number(pixel_size,positive=True);depth=_number(thickness,positive=True);origin=_array(origin,(2,),'origin')
    occupied=np.argwhere(values);h,w=values.shape
    # At most2grid layers; bound before allocating output point/face records.
    edge_count=int(np.count_nonzero(values[:,1:]!=values[:,:-1])+np.count_nonzero(values[1:]!=values[:-1])+values[0].sum()+values[-1].sum()+values[:,0].sum()+values[:,-1].sum())
    _budget(min(8*len(occupied),2*(h+1)*(w+1)),4*len(occupied)+2*edge_count)
    positions=[];indices={};triangles=[]
    def v(x,y,z):
        key=(int(x),int(y),int(z))
        if key not in indices:indices[key]=len(positions);positions.append([origin[0]+x*pixel,origin[1]-y*pixel,(-.5 if z==0 else .5)*depth])
        return indices[key]
    def quad(corners):
        a,b,c,d=[v(*p) for p in corners];triangles.extend(((a,b,c),(a,c,d)))
    for y,x in occupied:
        quad([(x,y,0),(x+1,y,0),(x+1,y+1,0),(x,y+1,0)])
        quad([(x,y,1),(x,y+1,1),(x+1,y+1,1),(x+1,y,1)])
        if y==0 or not values[y-1,x]:quad([(x,y,0),(x,y,1),(x+1,y,1),(x+1,y,0)])
        if y==h-1 or not values[y+1,x]:quad([(x,y+1,0),(x+1,y+1,0),(x+1,y+1,1),(x,y+1,1)])
        if x==0 or not values[y,x-1]:quad([(x,y,0),(x,y+1,0),(x,y+1,1),(x,y,1)])
        if x==w-1 or not values[y,x+1]:quad([(x+1,y,0),(x+1,y,1),(x+1,y+1,1),(x+1,y+1,0)])
    import hashlib
    result=_result(positions,triangles,method='binary-mask-extrusion',parameters=dict(pixel_size_m=pixel,thickness_m=depth,origin_m=origin.tolist(),mask_shape=list(values.shape),mask_sha256=hashlib.sha256(values.tobytes()).hexdigest(),foreground_pixels=len(occupied),cleanup='none; exact pixel-cell silhouette'),evidence=evidence)
    result.report['surface_regions']=[dict(faces=[0,len(triangles)],classification='procedurally-inferred',constraint='XY silhouette constrained by exact observed mask cells; all depth, hidden caps and walls use explicit thickness.')]
    return _seal_result(result)
