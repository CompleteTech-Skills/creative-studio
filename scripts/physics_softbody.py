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
    _remaining,canonical,digest,parse,require,MAX_INPUT_BYTES,MAX_CACHE_BYTES,MAX_DOCUMENT_BYTES,CapabilityReport,FeatureCapability,ALL_CATEGORIES,hardware,_failure_state)


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


def _native_time_scale(world):
    # Blender4.5.3 softbody.cc scales gravity by0.001 and integrates in
    # frame units. speed*sqrt(0.001) maps one native frame to SI seconds.
    return math.sqrt(1000.)/float(_ratio(world['fps']))


def admit(world):
    world=_world(world);solver=world['solver'];components=world['components'];budget=world['budgets']
    require(solver['backend']=='blender' and solver['adapter_version']=='1.0.0','PHYSICS_BACKEND','Select Blender1.0.0 for soft bodies')
    require(len(components)==1 and components[0]['category']=='soft-body' and set(solver['required_categories'])=={'soft-body','rigid-body'},
        'PHYSICS_CAPABILITY_UNAVAILABLE','One soft body and static rigid colliders are supported')
    require(not world['joints'] and not world['forces'] and not world['events'],'PHYSICS_CAPABILITY_UNAVAILABLE','Coupling, forces and events are unsupported')
    require(solver['reproducibility'] in ('verified-cache-replay','not-guaranteed') and not solver.get('stability'),
        'PHYSICS_CAPABILITY_UNAVAILABLE','Only saved-state replay is qualified; rigid solver stability controls do not apply')
    require(world['frame_range']['start']==0 and 2<=world['frame_range']['end']<=128 and 1<=_ratio(world['fps'])<=120 and world['fps']['numerator']<=32767,
        'PHYSICS_TIME_UNSUPPORTED','Soft-body outputs require zero origin and2..128 samples at1..120fps with a Blender-representable numerator')
    steps=solver['iterations']*solver['substeps']
    require(1<=steps<=1000 and _ratio(solver['fixed_step'])/solver['substeps']==1/(_ratio(world['fps'])*steps),
        'PHYSICS_TIME_UNSUPPORTED','Native fixed min/max integration steps must equal iterations*substeps per output frame')
    comp=components[0]
    comp['vertex_count']=int(comp['vertex_count']);comp['pins']=[int(v) for v in comp['pins']]
    require(comp['parent'] is None and 4<=comp['vertex_count']<=4096 and .01<=comp['stiffness']<=.99 and .01<=comp['mass']<=1000,
        'PHYSICS_SETTINGS','Unsupported soft-body parent, mesh size, stiffness or mass')
    require(comp['mass']/comp['vertex_count']>=.01,'PHYSICS_MASS_UNSUPPORTED',
            'Native soft bodies require at least0.01kg per vertex; lower masses are internally clamped')
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
    node=next(n for n in bundle['documents'][world['scene']['path']]['nodes'] if n['id']==comp['id'])
    require(not node.get('animation_bindings') and not node.get('constraints') and not node.get('attachment') and not node.get('material_overrides'),
            'PHYSICS_CAPABILITY_UNAVAILABLE','Animated, constrained, attached or material-overridden soft-body source nodes are unsupported')
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
    def _discover(self, **kwargs):
        identity = super()._discover(**kwargs)
        identity['solver_name'] = 'Blender native soft-body surface spring solver'
        return identity

    def estimate_resources(self,world,*,source_bytes=0,mesh_vertices=0,mesh_triangles=0):
        world=admit(world)
        require(all(type(v) is int and v>=0 for v in (source_bytes,mesh_vertices,mesh_triangles)) and source_bytes<=MAX_INPUT_BYTES,
                'PHYSICS_RESOURCE_LIMIT','Invalid geometry resource counts')
        frames=world['frame_range']['end']
        require(mesh_vertices<=world['budgets']['vertices'] and mesh_triangles<=world['budgets']['triangles'],
                'PHYSICS_RESOURCE_LIMIT','Aggregate admitted native mesh exceeds geometry budgets')
        memory=512*1024**2+source_bytes*8+mesh_vertices*128
        output=8*1024**2+source_bytes*2+mesh_vertices*frames*128
        require(memory<=world['budgets']['memory_bytes'] and output<=min(world['budgets']['cache_bytes'],world['budgets']['temporary_bytes'],world['budgets']['bake_file_bytes']),
                'PHYSICS_RESOURCE_LIMIT','Native soft-body resource estimate exceeds admission')
        return ResourceEstimate(source_bytes,mesh_vertices,mesh_triangles,frames,
            (frames-1)*world['solver']['iterations']*world['solver']['substeps'],memory,output,min(300.,world['budgets']['timeout_seconds']))

    def verify(self,cache,*,expected_world_sha256=None):
        return verify_cache(cache,expected_world_sha256=expected_world_sha256)

    def validate_cache(self,cache,*,expected_world_sha256=None):
        return self.verify(cache,expected_world_sha256=expected_world_sha256)

    def inspect(self,cache,frame):
        return inspect_cache(cache,frame)

    def export_animation(self,cache,destination,*,format='native-transform-json'):
        raise BackendError('PHYSICS_CAPABILITY_UNAVAILABLE','Soft-body animation export is unsupported; inspect evaluated mesh samples instead')

    def probe(self,*,smoke=False):
        require(type(smoke) is bool,'PHYSICS_REQUEST','smoke must be boolean')
        categories={name:FeatureCapability('unsupported',False,'No soft-body adapter implementation for this category') for name in ALL_CATEGORIES}
        limitations=('Closed connected triangulated surface spring mesh and static sphere/box collider meshes only.',
                     'No pressure, volume preservation, self-collision, moving colliders, two-way coupling or animation export.',
                     'Cache verification checks bytes and recorded observations; it is not fresh simulation or authenticated provenance.')
        identity=None;evidence=None;verified=False
        try:
            identity=self._discover();state='experimental'
            categories['soft-body']=FeatureCapability(state,False,'Executable discovered; native smoke not executed')
            if smoke:
                from physics_softbody_example import build_softbody_inputs
                world,payloads=build_softbody_inputs();prepared=self.prepare(world,payloads)
                try:
                    result=self.simulate(prepared)
                    with tempfile.TemporaryDirectory(prefix='hatch-softbody-smoke-') as name:
                        cache=Path(name)/'cache';self.bake(result,cache)
                        evidence=dict(verification=self.verify(cache),metrics=metrics(cache),usage=result.usage)
                        m=evidence['metrics']
                        require(m['maximum_centered_radius_change']>.02 and .2<m['final_center'][1]<1 and abs(m['final_center'][0])<.1 and abs(m['final_center'][2])<.1 and
                                m['maximum_tail_vertex_step']<.05,'PHYSICS_SMOKE_FAILED','Native fixture failed deformation, bounded floor response or settling checks')
                finally:self.cleanup(prepared)
                state='available';verified=True
                categories['soft-body']=FeatureCapability(state,True,'Actual deformation and native reopened bake verified')
        except BackendError as exc:
            state=_failure_state(exc.report['code']);evidence=exc.report
            categories['soft-body']=FeatureCapability(state,False,str(exc))
        return CapabilityReport('blender','1.0.0',state,self.executable,identity.get('application_version') if identity else None,
            identity.get('executable_sha256') if identity else None,identity is not None,verified,categories,(),(),
            ('native-softbody-point-cache',) if verified else (),(),hardware(),limitations,evidence)

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
            estimate=self.estimate_resources(world,source_bytes=sum(map(len,source_payloads.values())),mesh_vertices=vertex_count,mesh_triangles=sum(len(m['triangles']) for m in meshes.values()))
            worker_path=Path(__file__).with_name('physics_softbody_worker.py')
            require(worker_path.is_file() and not worker_path.is_symlink(),'PHYSICS_WORKER_TRUST','Worker must be a regular package file')
            worker=worker_path.read_bytes();_write(root/'worker.py',worker)
            _write(root/'world.json',canonical(world));token=uuid.uuid4().hex
            job=dict(protocol=2,native_time_scale=_native_time_scale(world),world=world,world_sha256=digest(canonical(world)),meshes=meshes,worker_sha256=digest(worker),mode='simulate')
            _write(root/'softbody-job.json',canonical(job))
            replay_job=deepcopy(job);replay_job['mode']='reopen'
            _write(root/'softbody-reopen-job.json',canonical(replay_job))
            fixture=bundle['project']['provenance']['fixture'];inventory=deepcopy(bundle['inventory'])
            prepared=PreparedSimulation(token,root,job['world_sha256'],'blender-soft-body',estimate,inventory,fixture)
            info=root.stat();self._owned[token]=dict(prepared=prepared,prepared_snapshot=deepcopy(prepared),root=root,inode=(info.st_dev,info.st_ino),
                world=world,identity=identity,job=job,worker_hash=digest(worker),inventory=inventory,project_manifest=project_manifest)
            return prepared
        except BaseException:shutil.rmtree(root);raise

    def _command(self,root,*,reopen=False):
        return [self.executable,'--background','--factory-startup','--disable-autoexec','--threads','1','--python-exit-code','2',
            '--python',str(root/'worker.py'),'--',str(root/('softbody-reopen-job.json' if reopen else 'softbody-job.json'))]

    def simulate(self,prepared,progress=None,cancel_token=None):
        require(isinstance(prepared,PreparedSimulation) and (progress is None or callable(progress)) and
            (cancel_token is None or callable(getattr(cancel_token,'is_set',None))),'PHYSICS_REQUEST','Invalid soft-body request')
        require(cancel_token is None or not cancel_token.is_set(),'PHYSICS_CANCELLED','Cancelled before native start')
        owner=self._claim_attempt(prepared);root=owner['root'];world=owner['world'];start=time.monotonic();deadline=start+prepared.estimate.timeout_seconds
        with _Reader(root) as reader:
            require(reader.read('world.json')==canonical(world) and digest(reader.read('worker.py'))==owner['worker_hash'] and
                reader.read('softbody-job.json')==canonical(owner['job']) and
                reader.read('softbody-reopen-job.json')==canonical(dict(owner['job'],mode='reopen')),'PHYSICS_INPUT_CHANGED','Prepared native job changed')
        with _Reader(root/'inputs') as reader:
            for path,expected in owner['inventory'].items():
                payload=reader.read(path);require(digest(payload)==expected['sha256'],'PHYSICS_INPUT_CHANGED','Prepared source changed')
        require(self._discover()==owner['identity'],'PHYSICS_BACKEND_CHANGED','Native executable changed')
        kwargs=dict(memory_bytes=world['budgets']['memory_bytes'],output_bytes=world['budgets']['temporary_bytes'],cancel_token=cancel_token,progress=progress)
        usage=_run_process(self._command(root),root,timeout=_remaining(deadline),**kwargs)
        _validate_output(root,world)
        replay_usage=_run_process(self._command(root,reopen=True),root,timeout=_remaining(deadline),label='reopen',**kwargs)
        replay=parse((root/'reopen-report.json').read_bytes())
        require(replay.get('ok') is True and replay.get('maximum_vertex_error',1)>-1 and replay['maximum_vertex_error']<=1e-5,
            'PHYSICS_CACHE_CORRUPT','Native deformation replay failed')
        files=tuple(_inventory(root));total=sum(f['bytes'] for f in files)
        require(total<=min(world['budgets']['cache_bytes'],world['budgets']['bake_file_bytes']),'PHYSICS_RESOURCE_LIMIT','Final native cache exceeds budget')
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


def _validate_output(root,world,payloads=None):
    if payloads is None:
        with _Reader(root) as reader:
            payloads={name:reader.read(name) for name in ('simulate-report.json','deformation.json')}
    report=parse(payloads['simulate-report.json']);samples=parse(payloads['deformation.json'])
    comp=world['components'][0]
    require(report.get('ok') is True and report.get('is_baked') is True and report.get('world_sha256')==digest(canonical(world)) and
        report.get('application_version')=='4.5.3 LTS' and report.get('application_build')=='67807e1800cc' and
        report.get('capabilities')==dict(surface_deformation=True,volume_preservation=False,pressure=False,two_way_rigid_coupling=False) and
        report.get('operation')=='simulate' and report.get('sample_count')==world['frame_range']['end'] and report.get('vertex_count')==comp['vertex_count'],'PHYSICS_BAKE_FAILED','Native soft-body bake missing or inconsistent')
    require(report.get('fps')==world['fps']['numerator'] and report.get('fps_base')==world['fps']['denominator'] and type(report.get('time_scale')) in (int,float) and abs(report['time_scale']-_native_time_scale(world))<=1e-5 and report.get('disk_cache') is True,
            'PHYSICS_CACHE_CORRUPT','Native clock or disk cache differs from requested settings')
    steps=world['solver']['iterations']*world['solver']['substeps']
    settings=report.get('native_settings',{})
    expected=dict(mass=comp['mass']/comp['vertex_count'],friction=0.,pull=comp['stiffness'],push=comp['stiffness'],
                  damping=comp['damping']*50,step_min=steps,step_max=steps,use_auto_step=False,use_goal=bool(comp['pins']))
    require(set(settings)==set(expected) and all(type(settings[k]) is bool and settings[k]==v if type(v) is bool
            else type(settings[k]) in (int,float) and math.isfinite(settings[k]) and abs(settings[k]-v)<=1e-5 for k,v in expected.items()),
            'PHYSICS_CACHE_CORRUPT','Native soft-body properties differ from requested settings')
    pins=report.get('pin_settings')
    expected_pins=dict(default=1.,minimum=0.,maximum=1.,spring=.999,friction=0.,weights=[1. if i in comp['pins'] else 0. for i in range(comp['vertex_count'])]) if comp['pins'] else None
    require(pins is None if not comp['pins'] else isinstance(pins,dict) and set(pins)==set(expected_pins) and isinstance(pins.get('weights'),list) and len(pins['weights'])==comp['vertex_count'] and all(type(x) in (int,float) and math.isfinite(x) and x==y for x,y in zip(pins['weights'],expected_pins['weights'])) and all(type(pins[k]) in (int,float) and math.isfinite(pins[k]) and abs(pins[k]-expected_pins[k])<1e-5 for k in expected_pins if k!='weights'),'PHYSICS_CACHE_CORRUPT','Native source pin weights or goal settings disagree')
    require(set(samples)=={'component','samples'} and samples['component']==comp['id'] and len(samples['samples'])==world['frame_range']['end'],
        'PHYSICS_CACHE_CORRUPT','Invalid deformation samples')
    for frame,sample in enumerate(samples['samples']):
        require(set(sample)=={'frame','positions'} and sample['frame']==frame and len(sample['positions'])==comp['vertex_count'],
            'PHYSICS_CACHE_CORRUPT','Invalid vertex sample count')
        require(all(type(p) is list and len(p)==3 and all(type(x) in (int,float) and math.isfinite(x) and abs(x)<100000 for x in p) for p in sample['positions']),
            'PHYSICS_CACHE_CORRUPT','Invalid evaluated vertex')
    return samples


def _read_verified_cache(root,*,expected_world_sha256=None):
    root=Path(root)
    with _Reader(root) as reader:
        manifest_bytes=reader.read('cache.json',MAX_DOCUMENT_BYTES)
        manifest=parse(manifest_bytes);validate_manifest(manifest)
        require(manifest.get('schema_version')==1 and manifest.get('kind')=='hatch-softbody-cache','PHYSICS_CACHE_CORRUPT','Unsupported soft-body cache')
        require(expected_world_sha256 is None or manifest['world_sha256']==expected_world_sha256,'PHYSICS_STALE_CACHE','Cache belongs to another world')
        files=manifest.get('files');require(type(files) is list and len(files)<=256,'PHYSICS_CACHE_CORRUPT','Invalid cache inventory')
        paths=[r['path'] for r in files];_paths(paths)
        require(len(paths)==len(set(paths)) and set(paths)==reader.names()-{'cache.json'},
            'PHYSICS_CACHE_CORRUPT','Cache file inventory differs')
        # Admit the retained world before retaining native payloads. Both the
        # private verification copy and the published cache include the manifest.
        records={record['path']:record for record in files}
        require('world.json' in records,'PHYSICS_CACHE_CORRUPT','Missing retained world')
        world_record=records['world.json']
        require(world_record['bytes']<=MAX_DOCUMENT_BYTES,'PHYSICS_RESOURCE_LIMIT','Retained world document exceeds limit')
        world_bytes=reader.read('world.json',int(world_record['bytes']))
        require(len(world_bytes)==world_record['bytes'] and digest(world_bytes)==world_record['sha256'],
                'PHYSICS_CACHE_CORRUPT','Changed retained world bytes')
        world=admit(parse(world_bytes));budget=world['budgets']
        require(digest(canonical(world))==manifest['world_sha256'],'PHYSICS_CACHE_CORRUPT','Changed world hash')
        declared=sum(record['bytes'] for record in files)
        retained=declared+len(manifest_bytes)
        require(declared==manifest['usage']['output_bytes'] and retained<=min(MAX_CACHE_BYTES,budget['cache_bytes'],budget['temporary_bytes']),
                'PHYSICS_RESOURCE_LIMIT','Retained cache and verification copy exceed byte budgets')
        require(len(manifest_bytes)<=budget['bake_file_bytes'] and all(record['bytes']<=budget['bake_file_bytes'] for record in files),
                'PHYSICS_RESOURCE_LIMIT','Retained cache file exceeds per-file budget')
        payloads={'world.json':world_bytes};total=len(world_bytes)
        for record in files:
            if record['path']=='world.json':continue
            payload=reader.read(record['path'],int(record['bytes']));total+=len(payload);payloads[record['path']]=payload
            require(len(payload)==record['bytes'] and digest(payload)==record['sha256'],'PHYSICS_CACHE_CORRUPT','Changed cache bytes')
        require(total==declared and set(paths)==reader.names()-{'cache.json'},
                'PHYSICS_CACHE_CORRUPT','Cache inventory changed during verification')
        required={'world.json','worker.py','softbody-job.json','softbody-reopen-job.json','softbody.blend','deformation.json',
                  'simulate-report.json','reopen-report.json','initial.png','impact.png','final.png'}
        require(required<=set(paths),'PHYSICS_CACHE_CORRUPT','Missing required native soft-body artifacts')
        require(any(p.endswith('.bphys') and p.startswith('blendcache_') for p in paths),
                'PHYSICS_CACHE_CORRUPT','Missing native deformation point cache')
        replay=parse(payloads['reopen-report.json'])
        require(replay.get('ok') is True and replay.get('operation')=='reopen' and
                replay.get('world_sha256')==manifest['world_sha256'] and replay.get('is_baked') is True and
                replay.get('changed_gravity_cache_consumption') is True and
                type(replay.get('maximum_vertex_error')) in (int,float) and
                0<=replay['maximum_vertex_error']<=1e-5,
                'PHYSICS_CACHE_CORRUPT','Missing or inconsistent native replay evidence')
    with tempfile.TemporaryDirectory(prefix='hatch-softbody-verify-') as name:
        source_root=Path(name)
        for path,payload in payloads.items():
            if path.startswith('inputs/'):_write(source_root/path[7:],payload)
        bundle,meshes=_component_meshes(source_root,world)
    from physics_backend_evidence import blend_container,EvidenceError
    try:blend_container(payloads['softbody.blend'])
    except EvidenceError as exc:raise BackendError('PHYSICS_CACHE_CORRUPT',str(exc)) from exc
    worker=Path(__file__).with_name('physics_softbody_worker.py').read_bytes()
    require(payloads['worker.py']==worker,'PHYSICS_CACHE_CORRUPT','Native worker differs from installed adapter version')
    job=dict(protocol=2,native_time_scale=_native_time_scale(world),world=world,world_sha256=manifest['world_sha256'],meshes=meshes,worker_sha256=digest(worker),mode='simulate')
    require(payloads['softbody-job.json']==canonical(job) and payloads['softbody-reopen-job.json']==canonical(dict(job,mode='reopen')),
            'PHYSICS_CACHE_CORRUPT','Native jobs differ from admitted source geometry/world/worker')
    backend=manifest['backend']
    require(backend['id']=='blender' and backend['adapter_version']=='1.0.0' and backend['application_version']=='Blender 4.5.3 LTS' and
            backend['solver_name']=='Blender native soft-body surface spring solver' and backend['device']=='cpu' and backend['precision']=='mixed',
            'PHYSICS_CACHE_CORRUPT','Native backend identity is inconsistent')
    import io
    from PIL import Image
    for name in ('initial.png','impact.png','final.png'):
        try:
            with Image.open(io.BytesIO(payloads[name])) as image:
                require(image.format=='PNG' and image.size==(160,160),'PHYSICS_CACHE_CORRUPT','Unexpected diagnostic image format or dimensions')
                image.verify()
            with Image.open(io.BytesIO(payloads[name])) as image:image.load()
        except (OSError,ValueError,SyntaxError) as exc:raise BackendError('PHYSICS_CACHE_CORRUPT','Invalid diagnostic PNG') from exc
    require(manifest['fixture']==bundle['project']['provenance']['fixture'],
            'PHYSICS_CACHE_CORRUPT','Fixture status differs from admitted source')
    samples=_validate_output(root,world,payloads)
    recorded=parse(payloads['simulate-report.json'])
    fields=('application_version','application_build','sample_count','vertex_count','native_settings','fps','fps_base','time_scale','disk_cache','capabilities','pin_settings')
    require(all(replay.get(field)==recorded.get(field) for field in fields),'PHYSICS_CACHE_CORRUPT','Reopened native settings and identity differ from bake')
    require(manifest['usage']['solver_steps']==(world['frame_range']['end']-1)*world['solver']['iterations']*world['solver']['substeps'] and
            manifest['usage']['peak_memory_bytes']<=world['budgets']['memory_bytes'] and manifest['usage']['elapsed_seconds']<=world['budgets']['timeout_seconds'],
            'PHYSICS_CACHE_CORRUPT','Recorded execution usage exceeds admitted limits or differs from clock')
    import struct
    component=world['components'][0];count=component['vertex_count']
    prefix='blendcache_softbody/'+component['id'].encode().hex().upper()
    expected_caches={prefix+'_%06d_00.bphys'%(frame+1) for frame in range(len(samples['samples']))}
    require({p for p in paths if p.endswith('.bphys')}==expected_caches,'PHYSICS_CACHE_CORRUPT','Native point-cache frame inventory differs')
    for frame,sample in enumerate(samples['samples']):
        payload=payloads[prefix+'_%06d_00.bphys'%(frame+1)]
        require(len(payload)==20+count*24 and payload[:8]==b'BPHYSICS' and struct.unpack_from('<3I',payload,8)==(0,count,6),
                'PHYSICS_CACHE_CORRUPT','Malformed or unsupported native soft-body point cache')
        for index,point in enumerate(sample['positions']):
            state=struct.unpack_from('<6f',payload,20+index*24)
            require(all(math.isfinite(v) for v in state),'PHYSICS_CACHE_CORRUPT','Nonfinite native soft-body state')
            native=state[:3];observed=(native[0],native[2],-native[1])
            require(all(math.isfinite(a) and abs(a-b)<=1e-5 for a,b in zip(observed,point)),
                    'PHYSICS_CACHE_CORRUPT','Native deformation cache differs from evaluated mesh samples')
    initial=samples['samples'][0]['positions'];source=meshes[world['components'][0]['id']]['positions']
    require(len(initial)==len(source) and max(abs(a-b) for pa,pb in zip(initial,source) for a,b in zip(pa,pb))<=1e-5,
            'PHYSICS_CACHE_CORRUPT','Initial evaluated vertices differ from admitted geometry')
    return dict(status='hash-and-native-source-verified',sample_count=len(samples['samples']),fixture=manifest['fixture'],review_status='unreviewed'),samples,world


def _load_verified_cache(root,*,expected_world_sha256=None):
    try:return _read_verified_cache(root,expected_world_sha256=expected_world_sha256)
    except BackendError:raise
    except (KeyError,TypeError,ValueError,AttributeError,OSError,OverflowError,RecursionError) as exc:
        raise BackendError('PHYSICS_CACHE_CORRUPT','Malformed soft-body cache: '+str(exc)) from exc


def verify_cache(root,*,expected_world_sha256=None):
    return _load_verified_cache(root,expected_world_sha256=expected_world_sha256)[0]


def inspect_cache(root,frame):
    report,samples,_=_load_verified_cache(root)
    require(type(frame) is int and 0<=frame<report['sample_count'],'PHYSICS_TIME_UNSUPPORTED','Frame outside cached interval')
    return samples['samples'][frame]


def metrics(root):
    verified,data,world=_load_verified_cache(root)
    samples=data['samples'];comp=world['components'][0]
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
        fixture=verified['fixture'],visual_review='not-performed')


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
        elif args.command=='probe':report=backend.probe(smoke=args.smoke).as_dict()
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
