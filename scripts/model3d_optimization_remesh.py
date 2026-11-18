"""Explicit component-local grid remeshing with protected open boundaries.

This is a bounded smooth, position-only profile, not a general surface repairer.
UVs, authored normals/hard edges, tangents, colors and morphs require a richer
remesher and are rejected. Source evidence is retained as immutable JSON bytes;
caller-provided source_version is a label, not authenticated registry approval.
"""
from __future__ import annotations
from dataclasses import dataclass
import copy
import hashlib
import json
import math
import numpy as np
from rig_contracts import DEFAULT_LIMITS
from rig_errors import require
from rig_geometry import connected_components
from model3d_topology import _degenerate, _face_vectors, _normals
from rig_models import Mesh

@dataclass(frozen=True)
class RemeshResult:
    mesh: Mesh
    report: dict
    source_bytes: bytes


def _edges(faces):
    edges=np.concatenate([faces[:,[0,1]],faces[:,[1,2]],faces[:,[2,0]]])
    unique,inverse,counts=np.unique(np.sort(edges,axis=1),axis=0,return_inverse=True,return_counts=True)
    signs=np.where(edges[:,0]<edges[:,1],1,-1)
    sums=np.bincount(inverse,weights=signs,minlength=len(unique))
    require(np.all(counts<=2) and np.all(sums[counts==2]==0), 'MODEL3D_REMESH_TOPOLOGY',
            'Remeshing requires manifold consistently wound input/output.')
    return unique[counts==1]



def _vertex_links(faces):
    links={}
    for a,b,c in faces:
        for vertex,left,right in [(a,b,c),(b,c,a),(c,a,b)]:
            graph=links.setdefault(int(vertex),{})
            graph.setdefault(int(left),set()).add(int(right))
            graph.setdefault(int(right),set()).add(int(left))
    for graph in links.values():
        degrees=[len(neighbors) for neighbors in graph.values()]
        require(max(degrees)<=2 and sum(d==1 for d in degrees) in (0,2),
                'MODEL3D_REMESH_TOPOLOGY','Vertex links must be simple manifold cycles or boundary paths.')
        pending=[next(iter(graph))];seen=set()
        while pending:
            vertex=pending.pop()
            if vertex not in seen:seen.add(vertex);pending.extend(graph[vertex]-seen)
        require(len(seen)==len(graph),'MODEL3D_REMESH_TOPOLOGY','Pinched/disconnected vertex fans are unsupported.')


def vertex_cluster_remesh(mesh: Mesh, *, cell_size, source_version, limits=None, protected_vertices=()) -> RemeshResult:
    """Cluster vertices into explicit Cartesian cells within each source component.

    All open-boundary vertices remain fixed and distinct. Rejects component loss,
    topology defects, flipped surviving faces and changed boundary edges. Reports
    vertex correspondence displacement, not Hausdorff or silhouette certification.
    A coarse grid may be rejected; retrying with a smaller explicit cell is safe.
    """
    budget=dict(DEFAULT_LIMITS)
    require(limits is None or isinstance(limits,dict),'MODEL3D_REMESH_LIMIT','Limits must be a mapping.')
    for key,value in (limits or {}).items():
        require(key in budget and type(value) in (int,float) and 0<value<=2**53-1
                and (isinstance(value,int) or math.isfinite(value)) and value==int(value),
                'MODEL3D_REMESH_LIMIT','Limits must be positive bounded whole JSON numbers.')
        budget[key]=int(value)
    require(type(cell_size) in (int,float) and 1e-100<=cell_size<=1e100 and math.isfinite(cell_size),
            'MODEL3D_REMESH_PARAMETER','cell_size must be finite within [1e-100,1e100].')
    require(isinstance(source_version,str) and 0<len(source_version)<=128 and source_version.strip()==source_version
            and all(ord(c)>=32 and not 0xD800<=ord(c)<=0xDFFF for c in source_version),'MODEL3D_REMESH_SOURCE','An explicit bounded source version label is required.')
    require(isinstance(mesh,Mesh),'MODEL3D_REMESH_INPUT','Expected native Mesh.')
    require(all(isinstance(value,str) for value in [mesh.id,mesh.name,mesh.region])
            and all(len(value)<=4096 and not any(0xD800<=ord(c)<=0xDFFF for c in value) for value in [mesh.id,mesh.name,mesh.region])
            and type(mesh.material) is int and 0<=mesh.material<4096 and type(mesh.source_node) is int and -1<=mesh.source_node<=2**53-1,
            'MODEL3D_REMESH_INPUT','Mesh identity and material/source indices must use native scalar types.')
    v,f=np.asarray(mesh.positions),np.asarray(mesh.triangles)
    require(v.ndim==2 and v.shape[1]==3 and len(v)>0 and v.dtype.kind in 'fiu' and np.isfinite(v).all(),
            'MODEL3D_REMESH_INPUT','Positions must be finite nonempty Nx3 numbers.')
    require(v.dtype.kind=='f' or (v.min()>=-2**53 and v.max()<=2**53),
            'MODEL3D_REMESH_INPUT','Integer positions must be exactly representable as float64.')
    require(f.ndim==2 and f.shape[1]==3 and len(f)>0 and f.dtype.kind in 'iu'
            and f.min()>=0 and f.max()<len(v),'MODEL3D_REMESH_INPUT','Expected valid indexed triangles.')
    require(len(v)<=budget['vertices'] and len(f)<=budget['triangles'], 'MODEL3D_REMESH_LIMIT','Geometry exceeds vertex/triangle limits.')
    require((len(v)*2048+len(f)*4096)<=budget['memory_bytes'], 'RIG_LIMIT_MEMORY','Remeshing working estimate exceeds memory budget before allocation.')
    require(not mesh.attributes and not mesh.morphs and not mesh.morph_defaults,
            'MODEL3D_REMESH_ATTRIBUTE','Position-only smooth remeshing cannot preserve authored attributes or morphs.')
    # Normalize numerical geometry for stable degeneracy/face-direction checks.
    v=np.array(v,dtype=np.float64,copy=True);f=np.array(f,dtype=np.int64,copy=True)
    source_mesh=Mesh(mesh.id,v,f)
    require(not _degenerate(source_mesh).any(), 'MODEL3D_REMESH_TOPOLOGY',
            'Degenerate input triangles must be repaired explicitly first.')
    face_cross,_=_face_vectors(source_mesh)
    face_scale=np.max(np.abs(face_cross),axis=1)
    require(np.all(face_scale>0),'MODEL3D_REMESH_TOPOLOGY','Input face directions exceed numerical precision.')
    face_cross/=face_scale[:,None]
    require(len(np.unique(np.sort(f,axis=1),axis=0))==len(f),'MODEL3D_REMESH_TOPOLOGY','Duplicate input triangles must be repaired explicitly first.')
    original_boundary=_edges(f)
    _vertex_links(f)
    components=connected_components(Mesh(mesh.id,v,f))
    component_id=np.empty(len(v),dtype=np.int64)
    for i,indices in enumerate(components):component_id[indices]=i
    # Isolated source vertices are intentionally preserved, never discarded.
    require(isinstance(protected_vertices,(list,tuple)) and len(protected_vertices)<=len(v)
            and all(type(i) is int and 0<=i<len(v) for i in protected_vertices),
            'MODEL3D_REMESH_POLICY','Protected vertices must be bounded native vertex indices.')
    protected=set(int(i) for i in original_boundary.ravel())
    protected.update(protected_vertices)
    isolated=set(range(len(v)))-set(int(i) for i in f.ravel());protected.update(isolated)
    coordinates=np.empty_like(v)
    for indices in components:
        origin=v[indices].min(axis=0)
        with np.errstate(over='ignore',invalid='ignore',divide='ignore'):
            local=v[indices]-origin
            coordinates[indices]=local/float(cell_size)
    require(np.isfinite(coordinates).all() and np.max(coordinates)<=2**52,
            'MODEL3D_REMESH_PARAMETER','Grid coordinates exceed stable integer precision; choose a larger cell.')
    grid=np.floor(coordinates).astype(np.int64)
    buckets={};mapping=np.empty(len(v),dtype=np.int64);members=[]
    for i,cell in enumerate(grid):
        key=('protected',i) if i in protected else (int(component_id[i]),*map(int,cell))
        if key not in buckets:buckets[key]=len(members);members.append([])
        j=buckets[key];mapping[i]=j;members[j].append(i)
    # Singletons (including protected boundaries) are copied bit-for-bit. Other
    # centroids use local offsets, retaining precision far from the world origin.
    positions=[]
    for indices in members:
        if len(indices)==1:
            positions.append(v[indices[0]].copy())
        else:
            anchor=v[indices[0]]
            with np.errstate(over='ignore',invalid='ignore'):
                offsets=v[indices]-anchor
            require(np.isfinite(offsets).all(),'MODEL3D_REMESH_PARAMETER','Cluster extent exceeds finite precision.')
            # Divide before summing so a large finite cluster cannot overflow.
            center=anchor+np.sum(offsets/len(indices),axis=0)
            positions.append(center)
    positions=np.array(positions)
    require(np.isfinite(positions).all(),'MODEL3D_REMESH_PARAMETER','Cluster positions overflowed.')
    remapped=mapping[f]
    surviving=np.array([len(set(map(int,row)))==3 for row in remapped])
    triangles=remapped[surviving]
    require(len(triangles)>0,'MODEL3D_REMESH_TOPOLOGY','Grid would remove all triangles.')
    require(len(np.unique(np.sort(triangles,axis=1),axis=0))==len(triangles),
            'MODEL3D_REMESH_TOPOLOGY','Grid would create overlapping duplicate triangles.')
    candidate=Mesh(mesh.id,positions,triangles)
    new_cross,_=_face_vectors(candidate)
    new_scale=np.max(np.abs(new_cross),axis=1)
    require(not _degenerate(candidate).any() and np.all(new_scale>0),
            'MODEL3D_REMESH_TOPOLOGY','Grid would create degenerate or numerically undefined triangles.')
    new_cross/=new_scale[:,None]
    require(np.all(np.sum(new_cross*face_cross[surviving],axis=1)>0),
            'MODEL3D_REMESH_TOPOLOGY','Grid would flip surviving triangles.')
    boundary=_edges(triangles)
    _vertex_links(triangles)
    expected={tuple(sorted(map(int,e))) for e in mapping[original_boundary]}
    require({tuple(map(int,e)) for e in boundary}==expected,'MODEL3D_REMESH_TOPOLOGY','Grid would change intentional open boundaries.')
    output=copy.deepcopy(mesh);output.positions=positions;output.triangles=triangles
    require(len(connected_components(output))==len(components),'MODEL3D_REMESH_TOPOLOGY','Grid would change disconnected component count.')
    # Recomputed smooth normals are explicit output derivatives, not source normals.
    output.attributes={'NORMAL':_normals(output)}
    source=dict(id=mesh.id,name=mesh.name,region=mesh.region,source_node=mesh.source_node,material=mesh.material,
                positions=v.tolist(),triangles=f.tolist(),source_version=source_version)
    payload=json.dumps(source,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    require(len(payload)<=min(budget['source_bytes'],budget['dependency_bytes']),
            'MODEL3D_REMESH_LIMIT','Immutable source evidence exceeds encoded byte limits.')
    with np.errstate(over='ignore',invalid='ignore'):
        delta=v-positions[mapping]
    distance=np.hypot.reduce(delta,axis=1)
    require(np.isfinite(distance).all(),'MODEL3D_REMESH_PARAMETER','Displacement measurement exceeds finite range.')
    report=dict(schema_version=1,kind='hatch-model3d-grid-remesh',algorithm='component-local-vertex-clustering',
        source_version=source_version,source_sha256=hashlib.sha256(payload).hexdigest(),cell_size=float(cell_size),
        source_vertices=len(v),vertices=len(positions),source_triangles=len(f),triangles=len(triangles),
        source_bounds=[v.min(axis=0).tolist(),v.max(axis=0).tolist()],
        bounds=[positions.min(axis=0).tolist(),positions.max(axis=0).tolist()],
        components=len(components),protected_boundary_vertices=len(set(map(int,original_boundary.ravel()))),explicit_protected_vertices=len(set(protected_vertices)),
        max_vertex_displacement=float(distance.max()),rms_vertex_displacement=float(np.hypot.reduce(distance/math.sqrt(len(distance)))),
        boundary_policy='fixed vertices and identical boundary edges',
        limitations=['Position-only smooth profile; no UVs, authored normals, hard edges or morphs.',
                     'Correspondence displacement is not a surface Hausdorff bound or visual approval.',
                     'No robust self-intersection solver; independent geometric review remains required.',
                     'Source version is caller-declared; source bytes/hash retained, registry publication remains external.'])
    return RemeshResult(output,report,payload)
