"""Bounded native Blender soft-body adapter; additive to rigid-body ownership.

Imports stay standard-library-only until explicit geometry or solver admission.
"""
from __future__ import annotations
from copy import deepcopy
import math
from pathlib import Path
import shutil
import tempfile
import time
import uuid
from collections.abc import Mapping
from physics_backends import (BlenderBackend,BackendError,PreparedSimulation,SimulationResult,ResourceEstimate,
    _world,_ratio,_Reader,_paths,_write,_inventory,_source_admission,_new_destination,_publish,_run_process,
    _remaining,canonical,digest,parse,require,MAX_INPUT_BYTES,MAX_CACHE_BYTES,MAX_DOCUMENT_BYTES)


def cache_schema():
    from physics_contracts import definitions
    defs=definitions()
    fields=dict(schema_version={'const':1},kind={'const':'hatch-softbody-cache'},
        world_sha256={'type':'string','pattern':'^[0-9a-f]{64}$'},
        files={'type':'array','minItems':1,'maxItems':256,'items':defs['FileRef']},
        backend=defs['BackendIdentity'],fixture={'type':'boolean'},review_status={'const':'unreviewed'},usage=defs['ResourceUsage'])
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','$id':'https://hatch-pet.local/physics-softbody-cache.schema.json',
        'title':'Native soft-body cache v1','type':'object','additionalProperties':False,'required':list(fields),'properties':fields}


def validate_manifest(value):
    from jsonschema import Draft202012Validator
    error=next(Draft202012Validator(cache_schema()).iter_errors(value),None)
    require(error is None,'PHYSICS_CACHE_CORRUPT','Invalid soft-body cache manifest: '+(str(error.message) if error else ''))


def admit(world):
    world=_world(world);solver=world['solver'];components=world['components'];budget=world['budgets']
    require(solver['backend']=='blender' and solver['adapter_version']=='1.0.0','PHYSICS_BACKEND','Select Blender1.0.0 for soft bodies')
    require(len(components)==1 and components[0]['category']=='soft-body' and set(solver['required_categories'])=={'soft-body','rigid-body'},
        'PHYSICS_CAPABILITY_UNAVAILABLE','One soft body and static rigid colliders are supported')
    require(not world['joints'] and not world['forces'] and not world['events'],'PHYSICS_CAPABILITY_UNAVAILABLE','Coupling, forces and events are unsupported')
    require(solver['reproducibility'] in ('verified-cache-replay','not-guaranteed') and not solver.get('stability'),
        'PHYSICS_CAPABILITY_UNAVAILABLE','Only saved-state replay is qualified; rigid solver stability controls do not apply')
    require(world['frame_range']['start']==0 and 2<=world['frame_range']['end']<=256 and 1<=_ratio(world['fps'])<=120,
        'PHYSICS_TIME_UNSUPPORTED','Soft-body outputs require zero origin and2..256 samples at1..120fps')
    steps=solver['iterations']*solver['substeps']
    require(1<=steps<=1000 and _ratio(solver['fixed_step'])/solver['substeps']==1/(_ratio(world['fps'])*steps),
        'PHYSICS_TIME_UNSUPPORTED','Native fixed min/max integration steps must equal iterations*substeps per output frame')
    comp=components[0]
    require(comp['parent'] is None and 4<=comp['vertex_count']<=4096 and .01<=comp['stiffness']<=.99 and .01<=comp['mass']<=1000,
        'PHYSICS_SETTINGS','Unsupported soft-body parent, mesh size, stiffness or mass')
    require(1<=len(world['bodies'])<=16 and set(comp['colliders'])=={b['id'] for b in world['bodies']},
        'PHYSICS_CAPABILITY_UNAVAILABLE','Explicit static colliders must account for all rigid bodies')
    for body in world['bodies']:
        require(body['mode']=='static' and body['parent'] is None and body['animation'] is None and len(body['colliders'])==1,
            'PHYSICS_CAPABILITY_UNAVAILABLE','Only unparented static collider meshes are supported')
        col=body['colliders'][0]
        require(col['shape']['type'] in ('box','sphere') and not col['sensor'] and col['group']==1 and col['mask']==2**32-1 and
            col['transform']==dict(translation=[0,0,0],rotation=[0,0,0,1]),'PHYSICS_CAPABILITY_UNAVAILABLE','Unsupported static collider proxy')
    for material in world['materials']:
        require(material['pbr_material'] is None and material['restitution']==0 and material['collision_margin']==0 and
            material['static_friction']==material['dynamic_friction'] and material['dynamic_friction']<=1,
            'PHYSICS_MATERIAL_UNSUPPORTED','Soft-body contacts support one friction factor; restitution/extra margin/PBR are unsupported')
    require(max(map(abs,world['gravity']))<=100 and budget['memory_bytes']>=512*1024**2 and
        budget['cache_bytes']<=MAX_CACHE_BYTES and budget['temporary_bytes']<=MAX_CACHE_BYTES,
        'PHYSICS_RESOURCE_LIMIT','Native soft-body gravity/memory/cache budget exceeded')
    used={b['asset'] for b in world['bodies']}|{comp['geometry']}
    require(used=={a['id'] for a in world['assets']},'PHYSICS_UNUSED_ASSET','Every source asset must be used by this bounded soft-body job')
    return world


def _component_meshes(root,world):
    # Load the full source closure once through the established Scene3D owner.
    body_world=deepcopy(world);ids={b['asset'] for b in world['bodies']}
    body_world['assets']=[a for a in world['assets'] if a['id'] in ids]
    bundle,bodies,_,_=_source_admission(root,body_world,'project.json')
    from physics_component_sources import admit_component_meshes
    admitted=admit_component_meshes(root,world,bundle)
    comp=world['components'][0];mesh=admitted['components'][comp['id']]
    import numpy as np
    from rig_geometry import mesh_edges
    points=np.asarray(mesh['positions'],dtype=float);triangles=np.asarray(mesh['triangles'],dtype=int)
    require(len(points)==comp['vertex_count'],'PHYSICS_GEOMETRY','Declared soft-body vertex count differs')
    _,counts=mesh_edges(triangles)
    neighbors=[set() for _ in points]
    directed={}
    for a,b,c in triangles:
        for x,y in ((a,b),(b,c),(c,a)):
            neighbors[x].add(y);neighbors[y].add(x)
            edge=(min(x,y),max(x,y));directed[edge]=directed.get(edge,0)+(1 if x<y else -1)
    reached={0};pending=[0]
    while pending:
        for vertex in neighbors[pending.pop()]-reached:reached.add(vertex);pending.append(vertex)
    require(len(reached)==len(points) and all(value==0 for value in directed.values()),
        'PHYSICS_GEOMETRY','Soft bodies require connected consistently oriented surface topology')
    require(len(counts)>0 and np.all(counts==2),'PHYSICS_GEOMETRY','Soft bodies require a closed two-manifold triangle surface')
    areas=np.linalg.norm(np.cross(points[triangles[:,1]]-points[triangles[:,0]],points[triangles[:,2]]-points[triangles[:,0]]),axis=1)
    require(np.all(areas>1e-12),'PHYSICS_GEOMETRY','Degenerate soft-body triangle')
    require(np.max(np.abs(points))<10000,'PHYSICS_RESOURCE_LIMIT','Mesh coordinates exceed native limits')
    from scene3d_math import evaluate_transforms
    transforms=evaluate_transforms(bundle['documents'][world['scene']['path']])
    from rig_math import transform_points
    for body in world['bodies']:
        bodies[body['id']]['positions']=transform_points(np.asarray(transforms[body['id']]['world_matrix']),np.asarray(bodies[body['id']]['vertices'])).tolist()
        bodies[body['id']].pop('vertices')
    bodies[comp['id']]=dict(positions=mesh['positions'],triangles=mesh['triangles'])
    return bundle,bodies


class SoftBodyBackend(BlenderBackend):
    def validate_scene(self,world):
        value=admit(world)
        return dict(status='configuration-valid',native_scene_verified=False,solver_executed=False,world_sha256=digest(canonical(value)))

    def prepare(self,world,source_payloads,*,project_manifest='project.json'):
        world=admit(world)
        require(project_manifest=='project.json','PHYSICS_CAPABILITY_UNAVAILABLE','Soft-body v1 uses the canonical project.json entry')
        require(isinstance(source_payloads,Mapping),'PHYSICS_INPUTS','Source payloads must be an immutable-byte mapping')
        source_payloads=dict(source_payloads);_paths(source_payloads)
        require(1<=len(source_payloads)<=128 and all(type(x) is bytes for x in source_payloads.values()) and
            sum(map(len,source_payloads.values()))<=MAX_INPUT_BYTES,'PHYSICS_RESOURCE_LIMIT','Invalid source byte mapping')
        identity=self._discover();root=Path(tempfile.mkdtemp(prefix='hatch-softbody-')).resolve()
        try:
            (root/'inputs').mkdir()
            for name,payload in source_payloads.items():_write(root/'inputs'/name,payload)
            bundle,meshes=_component_meshes(root/'inputs',world)
            require(set(bundle['inventory'])==set(source_payloads),'PHYSICS_INPUTS','Source mapping differs from native closure')
            vertex_count=sum(len(m['positions']) for m in meshes.values());frames=world['frame_range']['end']
            require(vertex_count*frames*128<world['budgets']['cache_bytes'],'PHYSICS_RESOURCE_LIMIT','Estimated vertex cache exceeds budget')
            estimate=ResourceEstimate(sum(map(len,source_payloads.values())),vertex_count,sum(len(m['triangles']) for m in meshes.values()),frames,
                (frames-1)*world['solver']['iterations']*world['solver']['substeps'],512*1024**2,vertex_count*frames*128,min(300.,world['budgets']['timeout_seconds']))
            worker_path=Path(__file__).with_name('physics_softbody_worker.py')
            require(worker_path.is_file() and not worker_path.is_symlink(),'PHYSICS_WORKER_TRUST','Worker must be a regular package file')
            worker=worker_path.read_bytes();_write(root/'worker.py',worker)
            _write(root/'world.json',canonical(world));token=uuid.uuid4().hex
            job=dict(protocol=1,world=world,world_sha256=digest(canonical(world)),meshes=meshes,worker_sha256=digest(worker),mode='simulate')
            _write(root/'softbody-job.json',canonical(job))
            fixture=bundle['project']['provenance']['fixture'];inventory=deepcopy(bundle['inventory'])
            prepared=PreparedSimulation(token,root,job['world_sha256'],'blender-soft-body',estimate,inventory,fixture)
            info=root.stat();self._owned[token]=dict(prepared=prepared,prepared_snapshot=deepcopy(prepared),root=root,inode=(info.st_dev,info.st_ino),
                world=world,identity=identity,job=job,worker_hash=digest(worker),inventory=inventory,project_manifest=project_manifest)
            return prepared
        except BaseException:shutil.rmtree(root);raise

    def _command(self,root):
        return [self.executable,'--background','--factory-startup','--disable-autoexec','--threads','1','--python-exit-code','2',
            '--python',str(root/'worker.py'),'--',str(root/'softbody-job.json')]

    def simulate(self,prepared,progress=None,cancel_token=None):
        require(isinstance(prepared,PreparedSimulation) and (progress is None or callable(progress)) and
            (cancel_token is None or callable(getattr(cancel_token,'is_set',None))),'PHYSICS_REQUEST','Invalid soft-body request')
        require(cancel_token is None or not cancel_token.is_set(),'PHYSICS_CANCELLED','Cancelled before native start')
        owner=self._claim_attempt(prepared);root=owner['root'];world=owner['world'];start=time.monotonic();deadline=start+prepared.estimate.timeout_seconds
        with _Reader(root) as reader:
            require(reader.read('world.json')==canonical(world) and digest(reader.read('worker.py'))==owner['worker_hash'] and
                reader.read('softbody-job.json')==canonical(owner['job']),'PHYSICS_INPUT_CHANGED','Prepared native job changed')
        with _Reader(root/'inputs') as reader:
            for path,expected in owner['inventory'].items():
                payload=reader.read(path);require(digest(payload)==expected['sha256'],'PHYSICS_INPUT_CHANGED','Prepared source changed')
        require(self._discover()==owner['identity'],'PHYSICS_BACKEND_CHANGED','Native executable changed')
        kwargs=dict(memory_bytes=world['budgets']['memory_bytes'],output_bytes=world['budgets']['temporary_bytes'],cancel_token=cancel_token,progress=progress)
        usage=_run_process(self._command(root),root,timeout=_remaining(deadline),**kwargs)
        _validate_output(root,world)
        job=deepcopy(owner['job']);job['mode']='reopen';_write(root/'softbody-job.json',canonical(job))
        replay_usage=_run_process(self._command(root),root,timeout=_remaining(deadline),label='reopen',**kwargs)
        replay=parse((root/'reopen-report.json').read_bytes())
        require(replay.get('ok') is True and replay.get('maximum_vertex_error',1)>-1 and replay['maximum_vertex_error']<=1e-5,
            'PHYSICS_CACHE_CORRUPT','Native deformation replay failed')
        _write(root/'softbody-job.json',canonical(owner['job']))
        files=tuple(_inventory(root));total=sum(f['bytes'] for f in files)
        require(total<=world['budgets']['cache_bytes'],'PHYSICS_RESOURCE_LIMIT','Final native cache exceeds budget')
        usage=dict(elapsed_seconds=time.monotonic()-start,peak_memory_bytes=max(usage['peak_memory_bytes'],replay_usage['peak_memory_bytes']),
            output_bytes=total,solver_steps=prepared.estimate.solver_steps)
        result=SimulationResult(prepared.token,root,prepared.world_sha256,owner['identity'],files,usage,replay,prepared.fixture)
        self._results[prepared.token]=deepcopy(result);return result

    def bake(self,result,destination):
        self._owner(result);require(self._results.get(result.token)==result,'PHYSICS_OWNERSHIP','Modified or foreign simulation result')
        destination=_new_destination(destination);stage=Path(tempfile.mkdtemp(prefix='.'+destination.name+'.softbody-',dir=destination.parent))
        try:
            with _Reader(result.root) as reader:
                for record in result.files:
                    payload=reader.read(record['path']);require(digest(payload)==record['sha256'],'PHYSICS_CACHE_CORRUPT','Changed native artifact')
                    _write(stage/record['path'],payload)
            manifest=dict(schema_version=1,kind='hatch-softbody-cache',world_sha256=result.world_sha256,files=list(result.files),
                backend=result.backend,fixture=result.fixture,review_status='unreviewed',usage=result.usage)
            validate_manifest(manifest);_write(stage/'cache.json',canonical(manifest));verify_cache(stage);_publish(stage,destination);return manifest
        finally:
            if stage.exists():shutil.rmtree(stage)


def _validate_output(root,world):
    report=parse((root/'simulate-report.json').read_bytes());comp=world['components'][0]
    require(report.get('ok') is True and report.get('is_baked') is True and report.get('world_sha256')==digest(canonical(world)) and
        report.get('application_version','').startswith('4.5.3'),'PHYSICS_BAKE_FAILED','Native soft-body bake missing or inconsistent')
    samples=parse((root/'deformation.json').read_bytes())
    require(set(samples)=={'component','samples'} and samples['component']==comp['id'] and len(samples['samples'])==world['frame_range']['end'],
        'PHYSICS_CACHE_CORRUPT','Invalid deformation samples')
    for frame,sample in enumerate(samples['samples']):
        require(set(sample)=={'frame','positions'} and sample['frame']==frame and len(sample['positions'])==comp['vertex_count'],
            'PHYSICS_CACHE_CORRUPT','Invalid vertex sample count')
        require(all(type(p) is list and len(p)==3 and all(type(x) in (int,float) and math.isfinite(x) and abs(x)<100000 for x in p) for p in sample['positions']),
            'PHYSICS_CACHE_CORRUPT','Invalid evaluated vertex')
    return samples


def verify_cache(root):
    root=Path(root)
    with _Reader(root) as reader:
        manifest=parse(reader.read('cache.json'));validate_manifest(manifest)
        require(manifest.get('schema_version')==1 and manifest.get('kind')=='hatch-softbody-cache','PHYSICS_CACHE_CORRUPT','Unsupported soft-body cache')
        files=manifest.get('files');require(type(files) is list and len(files)<=256,'PHYSICS_CACHE_CORRUPT','Invalid cache inventory')
        paths=[r['path'] for r in files];_paths(paths)
        require(len(paths)==len(set(paths)) and set(paths)=={r['path'] for r in _inventory(root) if r['path']!='cache.json'},
            'PHYSICS_CACHE_CORRUPT','Cache file inventory differs')
        for record in files:
            payload=reader.read(record['path']);require(len(payload)==record['bytes'] and digest(payload)==record['sha256'],'PHYSICS_CACHE_CORRUPT','Changed cache bytes')
        world=admit(parse(reader.read('world.json')))
        require(digest(canonical(world))==manifest['world_sha256'],'PHYSICS_CACHE_CORRUPT','Changed world hash')
    _component_meshes(root/'inputs',world);samples=_validate_output(root,world)
    return dict(status='hash-and-native-source-verified',sample_count=len(samples['samples']),fixture=manifest['fixture'],review_status='unreviewed')


def inspect_cache(root,frame):
    report=verify_cache(root)
    require(type(frame) is int and 0<=frame<report['sample_count'],'PHYSICS_TIME_UNSUPPORTED','Frame outside cached interval')
    return parse((Path(root)/'deformation.json').read_bytes())['samples'][frame]


def metrics(root):
    verify_cache(root)
    samples=parse((Path(root)/'deformation.json').read_bytes())['samples']
    world=parse((Path(root)/'world.json').read_bytes());comp=world['components'][0]
    initial=samples[0]['positions']
    def center(points):return [sum(p[i] for p in points)/len(points) for i in range(3)]
    def distance(a,b):return math.sqrt(sum((x-y)**2 for x,y in zip(a,b)))
    origin=center(initial);radii=[distance(p,origin) for p in initial]
    shape_change=max(abs(distance(p,center(sample['positions']))-r) for sample in samples for p,r in zip(sample['positions'],radii))
    tail=samples[-min(10,len(samples)):]
    tail_motion=max((distance(a,b) for x,y in zip(tail,tail[1:]) for a,b in zip(x['positions'],y['positions'])),default=0.)
    pin_error=max((distance(sample['positions'][index],initial[index]) for sample in samples for index in comp['pins']),default=0.)
    return dict(maximum_centered_radius_change=shape_change,initial_center=origin,final_center=center(samples[-1]['positions']),
        maximum_tail_vertex_step=tail_motion,maximum_pin_error=pin_error,volume_preservation='unsupported',pressure='unsupported',
        fixture=world['id']=='backend-engineering-fixture',visual_review='not-performed')


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--blender')
    commands=parser.add_subparsers(dest='command',required=True)
    probe=commands.add_parser('probe');probe.add_argument('--smoke',action='store_true')
    demo=commands.add_parser('demo');demo.add_argument('destination',type=Path)
    verify=commands.add_parser('verify');verify.add_argument('cache',type=Path)
    inspect=commands.add_parser('inspect');inspect.add_argument('cache',type=Path);inspect.add_argument('frame',type=int)
    args=parser.parse_args(argv)
    try:
        backend=SoftBodyBackend(args.blender)
        if args.command=='verify':report=verify_cache(args.cache)
        elif args.command=='inspect':report=inspect_cache(args.cache,args.frame)
        elif args.command=='probe' and not args.smoke:
            report=dict(backend=backend._discover(),category='soft-body',status='experimental',smoke_executed=False)
        else:
            from physics_softbody_example import build_softbody_inputs
            world,payloads=build_softbody_inputs();prepared=backend.prepare(world,payloads)
            try:
                result=backend.simulate(prepared)
                if args.command=='demo':
                    backend.bake(result,args.destination)
                    report=dict(validation=verify_cache(args.destination),metrics=metrics(args.destination),backend=result.backend,usage=result.usage)
                else:
                    with tempfile.TemporaryDirectory(prefix='hatch-softbody-smoke-') as name:
                        cache=Path(name)/'cache';backend.bake(result,cache)
                        report=dict(status='available',category='soft-body',smoke_executed=True,metrics=metrics(cache),backend=result.backend)
            finally:backend.cleanup(prepared)
        print(canonical(report).decode());return 0
    except BackendError as exc:
        print(canonical(exc.report).decode());return 2


if __name__=='__main__':raise SystemExit(main())
