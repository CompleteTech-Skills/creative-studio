"""Independent actual liquid geometry checks shared by run and cache admission."""
import math

import physics_backends as core
from physics_liquid_mesh import decode_bobj,canonical_vertices,measurements,LiquidMeshError


def validate_samples(value,world,*,reverse=False):
    frames=world['frame_range']['end']
    core.require(type(value) is dict and set(value)=={'samples'} and type(value['samples']) is list and len(value['samples'])==frames,
        'PHYSICS_CACHE_CORRUPT','Incomplete native liquid mesh samples.')
    order=list(reversed(range(frames))) if reverse else list(range(frames))
    result={}
    for expected,sample in zip(order,value['samples']):
        core.require(type(sample) is dict and set(sample)=={'frame','vertices','triangles'} and type(sample['frame']) is int and sample['frame']==expected,
            'PHYSICS_CACHE_CORRUPT','Native liquid samples are misordered or malformed.')
        vertices=sample['vertices'];triangles=sample['triangles']
        core.require(type(vertices) is list and type(triangles) is list and len(vertices)<=min(100000,world['budgets']['vertices']) and
            len(triangles)<=min(200000,world['budgets']['triangles']),'PHYSICS_CACHE_CORRUPT','Liquid sample geometry count exceeds admission.')
        core.require(all(type(v) is list and len(v)==3 and all(type(x) in (int,float) and math.isfinite(x) and abs(x)<=2000 for x in v) for v in vertices),
            'PHYSICS_CACHE_CORRUPT','Nonfinite or unbounded liquid mesh positions.')
        core.require(all(type(t) is list and len(t)==3 and all(type(x) is int and 0<=x<len(vertices) for x in t) and len(set(t))==3 for t in triangles),
            'PHYSICS_CACHE_CORRUPT','Invalid liquid mesh topology.')
        result[expected]=sample
    return result


def verify_geometry(reader,world,*,require_replay=True):
    native=validate_samples(core.parse(reader.read('mesh-samples.json',64*1024**2)),world)
    replay=validate_samples(core.parse(reader.read('replay-mesh-samples.json',64*1024**2)),world,reverse=True) if require_replay else None
    d=world['components'][0];reports=[]
    for frame,sample in native.items():
        path=f'cache/mesh/fluid_mesh_{frame+1:04d}.bobj.gz'
        payload=reader.read(path,16*1024**2)
        try:
            mesh=decode_bobj(payload,max_vertices=min(100000,world['budgets']['vertices']),max_triangles=min(200000,world['budgets']['triangles']))
        except LiquidMeshError as exc:
            raise core.BackendError('PHYSICS_CACHE_CORRUPT','Invalid native liquid mesh: '+str(exc)) from exc
        vertices=canonical_vertices(mesh,d)
        core.require(mesh['triangles']==sample['triangles'] and len(vertices)==len(sample['vertices']),
            'PHYSICS_CACHE_CORRUPT','Native cache and evaluated liquid mesh topology disagree.')
        error=max((abs(a-b) for left,right in zip(vertices,sample['vertices']) for a,b in zip(left,right)),default=0.)
        core.require(error<=2e-5,'PHYSICS_CACHE_CORRUPT','Native liquid cache and evaluated coordinates disagree.')
        if replay is not None:
            core.require(replay[frame]==sample,'PHYSICS_CACHE_CORRUPT','Reopening the native cache changed liquid geometry.')
        data=reader.read(f'cache/data/fluid_data_{frame+1:04d}.vdb',core.MAX_CACHE_BYTES)
        core.require(len(data)>=64 and data[:8]==b' BDV\x00\x00\x00\x00','PHYSICS_CACHE_CORRUPT','Missing or malformed native liquid data container.')
        reports.append(dict(frame=frame,mesh_sha256=core.digest(payload),native_vertex_error=error,**measurements(vertices,mesh['triangles'])))
    core.require(any(r['vertex_count']>8 for r in reports),'PHYSICS_OUTPUT_MISSING','Native liquid never produced a nontrivial surface.')
    core.require(len({r['mesh_sha256'] for r in reports})>1,'PHYSICS_OUTPUT_MISSING','Native liquid mesh did not evolve.')
    return dict(protocol=1,independent_decoder='gzip-BOBJECT',reopened_geometry_verified=bool(require_replay),samples=reports,
        maximum_native_vertex_error=max(r['native_vertex_error'] for r in reports),mass_conservation='not established')
