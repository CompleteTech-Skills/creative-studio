"""Bounded exact-rational triangle contact diagnostics for native mesh geometry.

Every admitted float is treated as its exact binary rational value. The report
checks pairs without shared indexed vertices; adjacent-pair foldover and
volumetric solid interpretation remain unavailable. Shared positions across
independent indices are contacts, not automatically welded topology.
"""
from fractions import Fraction
import numpy as np
from model3d_topology import _check,_degenerate
from rig_errors import require


def _sub(a,b):return tuple(x-y for x,y in zip(a,b))
def _cross(a,b):return (a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0])
def _dot(a,b):return sum(x*y for x,y in zip(a,b))
def _orient2(a,b,p):return (b[0]-a[0])*(p[1]-a[1])-(b[1]-a[1])*(p[0]-a[0])
def _project(p,axis):return tuple(x for i,x in enumerate(p) if i!=axis)


def _inside(p,t):
    signs=[_orient2(t[i],t[(i+1)%3],p) for i in range(3)]
    return all(x>=0 for x in signs) or all(x<=0 for x in signs)


def _segment2(a,b,c,d):
    ab_c,ab_d=_orient2(a,b,c),_orient2(a,b,d)
    cd_a,cd_b=_orient2(c,d,a),_orient2(c,d,b)
    if ab_c==ab_d==cd_a==cd_b==0:
        return all(max(min(a[i],b[i]),min(c[i],d[i]))<=min(max(a[i],b[i]),max(c[i],d[i])) for i in (0,1))
    return ab_c*ab_d<=0 and cd_a*cd_b<=0


def _coplanar(a,b,normal):
    axis=max(range(3),key=lambda i:abs(normal[i]))
    a=[_project(p,axis) for p in a];b=[_project(p,axis) for p in b]
    return (_inside(a[0],b) or _inside(b[0],a) or any(
        _segment2(a[i],a[(i+1)%3],b[j],b[(j+1)%3]) for i in range(3) for j in range(3)))


def _edge_triangle(p,q,triangle,normal):
    left=_dot(normal,_sub(p,triangle[0]));right=_dot(normal,_sub(q,triangle[0]))
    if left*right>0:return False
    axis=max(range(3),key=lambda i:abs(normal[i]));t=[_project(v,axis) for v in triangle]
    if left==right==0:
        p,q=_project(p,axis),_project(q,axis)
        return _inside(p,t) or _inside(q,t) or any(_segment2(p,q,t[i],t[(i+1)%3]) for i in range(3))
    factor=left/(left-right)
    point=tuple(x+factor*(y-x) for x,y in zip(p,q))
    return _inside(_project(point,axis),t)


def _contact(a,b):
    na=_cross(_sub(a[1],a[0]),_sub(a[2],a[0]));nb=_cross(_sub(b[1],b[0]),_sub(b[2],b[0]))
    sides=[_dot(na,_sub(p,a[0])) for p in b]
    if all(x>0 for x in sides) or all(x<0 for x in sides):return None
    if all(x==0 for x in sides):return 'coplanar' if _coplanar(a,b,na) else None
    if any(_edge_triangle(a[i],a[(i+1)%3],b,nb) or _edge_triangle(b[i],b[(i+1)%3],a,na) for i in range(3)):
        return 'noncoplanar'
    return None


def triangle_contacts(mesh,*,max_triangles=4096,max_pairs=100_000,limits=None):
    """Return exact nonadjacent triangle-contact pairs, including tangency.

    A deterministic X-axis AABB sweep rejects separated pairs before exact
    predicates. All sweep comparisons (including adjacent pairs) consume the
    explicit pair budget. Budget exhaustion rejects without a partial success
    report. Input geometry is never modified and degeneracies reject explicitly.
    """
    budget=_check(mesh,limits)
    require(mesh.positions.dtype.kind!='f' or mesh.positions.dtype.itemsize<=8,
            'MODEL3D_INTERSECTION_NUMERIC','Exact float predicates support binary floating types up to float64.')
    require(type(max_triangles) is int and 1<=max_triangles<=20_000
            and type(max_pairs) is int and 1<=max_pairs<=200_000,
            'MODEL3D_INTERSECTION_BUDGET','Intersection limits must be bounded positive native integers.')
    require(len(mesh.triangles)<=max_triangles,'MODEL3D_INTERSECTION_BUDGET','Triangle count exceeds exact diagnostic budget.')
    # Conservative allowance covers retained fractions/sets/sweep arrays plus
    # bounded exact-predicate temporaries. Each reported pair, including later
    # geometry-wrapper part metadata, reserves another 8192 bytes.
    working=len(mesh.positions)*4096+len(mesh.triangles)*4096+131072
    require(working<=budget['memory_bytes'],'MODEL3D_INTERSECTION_BUDGET',
            'Exact contact working memory exceeds the configured budget before allocation.')
    contact_budget=min(max_pairs,(budget['memory_bytes']-working)//8192)
    require(not _degenerate(mesh).any(),'MODEL3D_INTERSECTION_DEGENERATE','Repair degenerate triangles explicitly before intersection measurement.')
    # Preserve integer coordinates without an intervening float conversion;
    # float16/32/64 inputs retain their exact binary ratios.
    used=np.unique(mesh.triangles)
    exact={int(i):tuple(Fraction(int(x)) if mesh.positions.dtype.kind in 'iu' else Fraction(float(x))
                        for x in mesh.positions[i]) for i in used}
    triangles=mesh.positions[mesh.triangles]
    low,high=triangles.min(axis=1),triangles.max(axis=1)
    order=sorted(range(len(triangles)),key=lambda i:(low[i,0],i))
    active=[];comparisons=adjacent=separated=exact_pairs=0;contacts=[]
    vertex_sets=[set(map(int,t)) for t in mesh.triangles]
    for i in order:
        active=[j for j in active if high[j,0]>=low[i,0]]
        for j in active:
            comparisons+=1
            require(comparisons<=max_pairs,'MODEL3D_INTERSECTION_BUDGET','Candidate pair work exceeded the exact diagnostic budget.')
            if vertex_sets[i]&vertex_sets[j]:adjacent+=1;continue
            if np.any(high[i]<low[j]) or np.any(high[j]<low[i]):separated+=1;continue
            exact_pairs+=1
            a=tuple(exact[int(k)] for k in mesh.triangles[i]);b=tuple(exact[int(k)] for k in mesh.triangles[j])
            kind=_contact(a,b)
            if kind:
                require(len(contacts)<contact_budget,'MODEL3D_INTERSECTION_BUDGET',
                        'Exact contact result exceeds the configured memory budget.')
                contacts.append(dict(triangles=sorted([i,j]),kind=kind))
        active.append(i)
    contacts.sort(key=lambda x:x['triangles'])
    return dict(schema_version=1,kind='hatch-model3d-triangle-contacts',mesh=mesh.id,
                triangles=len(triangles),contacts=contacts,contact_count=len(contacts),
                conservative_working_bytes=working,contact_result_budget=contact_budget,
                sweep_pair_comparisons=comparisons,adjacent_pairs_excluded=adjacent,
                aabb_pairs_separated=separated,exact_pairs_checked=exact_pairs,
                numeric_policy='exact binary-rational predicates; no geometric epsilon',
                limitations=['Pairs sharing any indexed vertex are excluded; adjacent foldover remains unmeasured.',
                             'Contacts include tangency and coincident disconnected surfaces, not just penetrating intersections.',
                             'No solid-volume, inside/outside, clearance or artistic quality interpretation.'])


def geometry_contacts(geometry,*,max_triangles=4096,max_pairs=100_000,limits=None):
    """Measure both within-part and cross-part nonadjacent surface contacts.

    Mesh identity is retained in each contact. Distinct parts use independent
    vertex indices even where their positions coincide; intended attachment
    contacts therefore remain visible for caller policy and review.
    """
    from model3d_topology import _limits
    from rig_models import Mesh,validate_geometry
    budget=_limits(limits);validate_geometry(geometry,budget)
    require(type(max_triangles) is int and 1<=max_triangles<=20_000
            and geometry.triangle_count<=max_triangles,'MODEL3D_INTERSECTION_BUDGET','Aggregate triangle count exceeds diagnostic limit.')
    require(geometry.vertex_count*96+geometry.triangle_count*256<=budget['memory_bytes'],
            'MODEL3D_INTERSECTION_BUDGET','Aggregate contact working memory exceeds budget.')
    dtype=np.result_type(*(mesh.positions.dtype for mesh in geometry.meshes))
    if dtype.kind=='f':
        require(dtype.itemsize<=8 and all(mesh.positions.dtype.kind=='f' or
                (-2**53<=int(mesh.positions.min()) and int(mesh.positions.max())<=2**53)
                for mesh in geometry.meshes),'MODEL3D_INTERSECTION_NUMERIC',
                'Mixed floating/integer parts must preserve exact integer coordinates within float64 precision.')
    positions=[];triangles=[];mapping=[];offset=0
    for mesh in geometry.meshes:
        positions.append(mesh.positions);triangles.append(mesh.triangles.astype(np.int64)+offset)
        mapping.extend((mesh.id,i) for i in range(len(mesh.triangles)));offset+=len(mesh.positions)
    merged=Mesh('contact-measurement',np.concatenate(positions),np.concatenate(triangles))
    result=triangle_contacts(merged,max_triangles=max_triangles,max_pairs=max_pairs,limits=budget)
    result['kind']='hatch-model3d-geometry-contacts';result.pop('mesh')
    result['meshes']=[mesh.id for mesh in geometry.meshes]
    for contact in result['contacts']:
        contact['parts']=[dict(mesh=mapping[i][0],triangle=mapping[i][1]) for i in contact['triangles']]
    result['limitations'].append('Intended attachments and cross-part contacts require explicit semantic interpretation.')
    return result


def main():
    import argparse,json
    from pathlib import Path
    from rig_source_snapshot import import_snapshot
    from rig_errors import RigError
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--input',required=True,type=Path)
    p.add_argument('--max-triangles',type=int,default=4096);p.add_argument('--max-pairs',type=int,default=100_000)
    a=p.parse_args()
    try:
        with import_snapshot(a.input) as snapshot:
            report=geometry_contacts(snapshot.model.geometry,max_triangles=a.max_triangles,max_pairs=a.max_pairs)
        print(json.dumps(report,sort_keys=True));return 0
    except RigError as error:print(json.dumps(error.report,sort_keys=True));return 1
    except OSError as error:print(json.dumps(dict(code='MODEL3D_INTERSECTION_IO',message=str(error))));return 1

if __name__=='__main__':raise SystemExit(main())
