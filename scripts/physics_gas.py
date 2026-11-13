"""Bounded real Mantaflow gas adapter using native source and process owners."""
from __future__ import annotations
import argparse
from copy import deepcopy
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import uuid

import physics_backends as core


DEFAULT_SETTINGS = dict(schema_version=1,kind='hatch-3d-gas-settings',reference_density=1.225,
    ambient_temperature=273.15,temperature_scale=100.,fuel=1.,emission_end_frame=12,
    dissipation_enabled=True,dissipation_frames=12,vorticity=.1,burning_rate=.75,
    density_buoyancy=-.001,heat_buoyancy=1.,collision_distance=1.5,cfl=2.,minimum_substeps=1,
    maximum_substeps=4,render_frames=[0,12,23],render_size=128,render_samples=8,render_density=3.)
OBSERVED_SETTINGS=('dissipation_enabled','dissipation_frames','vorticity','burning_rate','density_buoyancy',
    'heat_buoyancy','collision_distance','cfl','minimum_substeps','maximum_substeps')


def schemas():
    properties={key:{'type':'number'} for key in DEFAULT_SETTINGS}
    bounds=dict(reference_density=(.01,100),ambient_temperature=(100,1000),temperature_scale=(1,1000),fuel=(0,5),
        vorticity=(0,4),burning_rate=(.01,4),density_buoyancy=(-5,5),heat_buoyancy=(-5,5),collision_distance=(.001,10),
        cfl=(.1,10),render_density=(.01,100))
    for key,(low,high) in bounds.items():properties[key].update(minimum=low,maximum=high)
    for key,low,high in [('emission_end_frame',1,47),('dissipation_frames',1,250),('minimum_substeps',1,16),
        ('maximum_substeps',1,16),('render_size',64,256),('render_samples',1,16)]:
        properties[key]=dict(type='integer',minimum=low,maximum=high)
    properties.update(schema_version={'const':1},kind={'const':'hatch-3d-gas-settings'},
        dissipation_enabled={'type':'boolean'},render_frames=dict(type='array',minItems=1,maxItems=8,uniqueItems=True,
            items=dict(type='integer',minimum=0,maximum=47)))
    return {'physics-gas-settings.schema.json':dict(
        **{'$schema':'https://json-schema.org/draft/2020-12/schema'},title='Hatch native gas settings',
        type='object',additionalProperties=False,properties=properties,required=list(DEFAULT_SETTINGS))}


def validate_settings(value, frames):
    value=core.parse(core.canonical(value))
    core.require(type(value) is dict and set(value)==set(DEFAULT_SETTINGS) and type(value['schema_version']) is int and value['schema_version']==1
        and value['kind']=='hatch-3d-gas-settings','PHYSICS_GAS_SETTINGS','Unknown gas settings schema.')
    ranges=dict(reference_density=(.01,100),ambient_temperature=(100,1000),temperature_scale=(1,1000),
        fuel=(0,5),vorticity=(0,4),burning_rate=(.01,4),density_buoyancy=(-5,5),heat_buoyancy=(-5,5),
        collision_distance=(.001,10),cfl=(.1,10),render_density=(.01,100))
    for key,(low,high) in ranges.items():
        v=value[key]
        core.require(type(v) in (int,float) and math.isfinite(v) and low<=v<=high,
            'PHYSICS_GAS_SETTINGS','Gas setting outside native admission: '+key)
    for key,low,high in [('emission_end_frame',1,frames-1),('dissipation_frames',1,250),
        ('minimum_substeps',1,16),('maximum_substeps',1,16),('render_size',64,256),('render_samples',1,16)]:
        core.require(type(value[key]) is int and low<=value[key]<=high,'PHYSICS_GAS_SETTINGS','Invalid integer setting: '+key)
    core.require(type(value['dissipation_enabled']) is bool and value['minimum_substeps']<=value['maximum_substeps'],
        'PHYSICS_GAS_SETTINGS','Invalid dissipation or substep settings.')
    samples=value['render_frames']
    core.require(type(samples) is list and 1<=len(samples)<=8
        and all(type(x) is int and 0<=x<frames for x in samples) and len(set(samples))==len(samples),
        'PHYSICS_GAS_SETTINGS','Invalid render frame selection.')
    return value


def admit(world,settings):
    world=core._world(world)
    frames=world['frame_range']['end']
    core.require(world['frame_range']['start']==0 and 2<=frames<=48 and world['solver']['backend']=='blender'
        and set(world['solver']['required_categories'])=={'rigid-body','gas'},'PHYSICS_GAS_UNSUPPORTED','Gas jobs require 2..48 zero-origin frames and static body/gas categories.')
    core.require(len(world['components'])==1 and world['components'][0]['category']=='gas' and not world['joints']
        and not world['events'],'PHYSICS_GAS_UNSUPPORTED','This adapter supports one gas domain without rigid joints/events.')
    domain=world['components'][0]
    core.require(domain['transform']['rotation']==[0,0,0,1] and len(set(domain['size']))==1
        and len(set(domain['resolution']))==1 and 8<=domain['resolution'][0]<=64 and domain['viscosity']==0,
        'PHYSICS_GAS_UNSUPPORTED','Gas domain must be an unrotated cube at 8..64 cells/axis; viscosity control unsupported.')
    core.require(.1<=domain['size'][0]<=100 and all(abs(v)<=1000 for v in domain['transform']['translation']),
        'PHYSICS_GAS_UNSUPPORTED','Gas domain size/position exceeds supported range.')
    from fractions import Fraction
    core.require(Fraction(world['solver']['fixed_step']['numerator'],world['solver']['fixed_step']['denominator']) ==
        Fraction(world['fps']['denominator'],world['fps']['numerator']) and world['solver']['substeps']==1 and world['solver']['iterations']==1,
        'PHYSICS_GAS_UNSUPPORTED','Gas timing requires one output interval; adaptive native CFL controls belong in GasSettings.')
    ids={b['id'] for b in world['bodies']}
    roles=domain['emitters']+domain['colliders']
    core.require(1<=len(ids)<=16 and set(roles)==ids and len(roles)==len(set(roles)),
        'PHYSICS_GAS_UNSUPPORTED','Each static source body must have one emitter or collider role.')
    for body in world['bodies']:
        core.require(body['mode']=='static' and body['authority']=='static' and body['parent'] is None
            and body['linear_velocity']==[0,0,0] and body['angular_velocity']==[0,0,0]
            and body['animation'] is None,'PHYSICS_GAS_UNSUPPORTED','Animated/dynamic gas inputs require another adapter.')
        core.require(len(body['colliders'])==1 and body['colliders'][0]['shape']['type'] in ('sphere','box'),
            'PHYSICS_GAS_UNSUPPORTED','Gas input geometry must use admitted native sphere/box meshes.')
        collider=body['colliders'][0]
        core.require(collider['transform']==dict(translation=[0,0,0],rotation=[0,0,0,1]) and
            not collider['sensor'] and collider['group']==1 and collider['mask']==2**32-1,
            'PHYSICS_GAS_UNSUPPORTED','Gas inputs require identity unfiltered nonsensor collider transforms.')
    for force in world['forces']:
        core.require(force['type'] in ('wind','turbulence') and force['bodies']==[domain['id']]
            and force['categories']==['gas'] and force['frame_range']==world['frame_range']
            and (force['type']!='wind' or abs(sum(x*x for x in force['vector'])-1)<1e-6),
            'PHYSICS_GAS_UNSUPPORTED','Only constant domain wind/turbulence forces are supported.')
    core.require('stability' not in world['solver'],'PHYSICS_GAS_UNSUPPORTED','Rigid stability settings do not map to Mantaflow.')
    settings=validate_settings(settings,frames)
    ratio=domain['density']/settings['reference_density']
    temperature=(domain['temperature']-settings['ambient_temperature'])/settings['temperature_scale']
    core.require(0<ratio<=10 and -10<=temperature<=10,'PHYSICS_GAS_SETTINGS','Density/temperature conversion exceeds native range.')
    return world,settings


class GasBackend(core.BlenderBackend):
    """Static mesh emitters/colliders, native gas evolution and independent VDB validation."""
    backend_id='blender'

    def __init__(self, executable=None, reader_python=None):
        super().__init__(executable)
        value=reader_python or os.environ.get('HATCH_OPENVDB_PYTHON')
        self.reader_python=str(Path(value).absolute()) if value else None
        self._gas_lock=threading.Lock()

    def _discover(self,*,timeout=10):
        identity=deepcopy(super()._discover(timeout=timeout))
        identity.update(solver_name='Blender Mantaflow gas',solver_version='not exposed separately by Blender')
        self._identity=identity
        return identity

    def _reader_identity(self,*,timeout=10):
        core.require(self.reader_python and Path(self.reader_python).is_file(),'PHYSICS_BACKEND_UNAVAILABLE',
            'Set HATCH_OPENVDB_PYTHON to an isolated interpreter with OpenVDB 13.')
        with tempfile.TemporaryDirectory(prefix='hatch-vdb-probe-') as directory:
            root=Path(directory)
            core._write(root/'reader.py',Path(__file__).with_name('physics_gas_vdb.py').read_bytes())
            try:
                core._run_process([self.reader_python,'-I',str(root/'reader.py'),'--probe'],root,
                    timeout=timeout,memory_bytes=1024**3,output_bytes=1024**2,label='probe')
            except core.BackendError as exc:
                raise core.BackendError('PHYSICS_BACKEND_UNAVAILABLE','The isolated OpenVDB reader failed its module probe.') from exc
            identity=core.parse((root/'probe-stdout.log').read_bytes())
        core.require(identity.get('reader')=='OpenVDB' and identity.get('version',[None])[0]==13,
            'PHYSICS_BACKEND_VERSION_UNSUPPORTED','Only independently tested OpenVDB 13 readers are admitted.')
        identity['interpreter_sha256']=core.digest(Path(self.reader_python).read_bytes())
        return identity

    def probe(self,*,smoke=False):
        core.require(type(smoke) is bool,'PHYSICS_REQUEST','Gas smoke probe requires an explicit boolean.')
        categories={name:core.FeatureCapability('unsupported',False,'This adapter only simulates gas; static meshes supply sources and obstacles.') for name in core.ALL_CATEGORIES}
        evidence=None
        try:
            identity=self._discover();reader=self._reader_identity()
            state='experimental'
            categories['gas']=core.FeatureCapability(state,False,'Blender and OpenVDB discovered; simulation was not executed.')
            if smoke:
                from physics_gas_example import build_gas_inputs
                evidence=[]
                for fire in (False,True):
                    world,payloads,settings=build_gas_inputs(fire=fire)
                    prepared=self.prepare(world,payloads,settings=settings)
                    try:
                        result=self.simulate(prepared)
                        evidence.append(dict(combustion=fire,world_sha256=result.world_sha256,verification=result.verification))
                    finally:self.cleanup(prepared)
                state='available'
                categories['gas']=core.FeatureCapability(state,True,'Actual smoke and fire native bakes, VDB field decode and independent reopen passed.')
            return core.CapabilityReport('blender-gas',core.ADAPTER_VERSION,state,self.executable,identity['application_version'],
                identity['executable_sha256'],True,smoke,categories,('sphere','box'),(),('OpenVDB',),('native-gas-cache',),
                core.hardware(),('Static emitters/obstacles only; native adaptive CFL; no arbitrary solver continuation.',),
                dict(reader=reader,execution=evidence))
        except core.BackendError as exc:
            state=core._failure_state(exc.report['code'])
            categories['gas']=core.FeatureCapability(state,False,str(exc))
            return core.CapabilityReport('blender-gas',core.ADAPTER_VERSION,state,self.executable,None,None,False,False,
                categories,(),(),(),(),core.hardware(),('No readiness without actual smoke/fire runs.',),exc.report)

    def validate_scene(self,world,settings=None):
        world,settings=admit(world,DEFAULT_SETTINGS if settings is None else settings)
        return dict(status='configuration-valid',world_sha256=core.digest(core.canonical(world)),
            settings_sha256=core.digest(core.canonical(settings)),solver_executed=False,category='gas',
            numerical_clock='native adaptive CFL; no claim of fixed rigid-body substeps')

    def estimate_resources(self,world,settings=None,*,source_bytes=0,mesh_vertices=0,mesh_triangles=0):
        world,settings=admit(world,DEFAULT_SETTINGS if settings is None else settings)
        cells=math.prod(world['components'][0]['resolution']);frames=world['frame_range']['end']
        memory=512*1024**2+cells*1024+source_bytes*8
        output=16*1024**2+cells*frames*32+source_bytes*2
        budget=world['budgets']
        core.require(memory<=budget['memory_bytes'] and output<=min(budget['cache_bytes'],budget['temporary_bytes'],core.MAX_CACHE_BYTES)
            and output<=budget['bake_file_bytes'],'PHYSICS_RESOURCE_LIMIT','Gas allocation estimate exceeds admitted resources.')
        return core.ResourceEstimate(source_bytes,mesh_vertices,mesh_triangles,frames,frames*settings['maximum_substeps'],memory,output,min(budget['timeout_seconds'],300.))

    def prepare(self,world,source_payloads,*,settings=None,project_manifest='project.json'):
        world,settings=admit(world,DEFAULT_SETTINGS if settings is None else settings)
        core.require(os.name=='posix','PHYSICS_PLATFORM_UNSUPPORTED','Gas adapter requires POSIX process/descriptor ownership.')
        core.require(isinstance(source_payloads,dict) and 1<=len(source_payloads)<=128 and
            all(type(x) is bytes for x in source_payloads.values()),'PHYSICS_INPUTS','Supply complete immutable native source bytes.')
        core._paths(source_payloads);core.path_name(project_manifest)
        total=sum(map(len,source_payloads.values()))
        core.require(total<=core.MAX_INPUT_BYTES,'PHYSICS_RESOURCE_LIMIT','Gas source inputs exceed limit.')
        estimate=self.estimate_resources(world,settings,source_bytes=total)
        identity=self._discover()
        reader_identity=self._reader_identity()
        root=Path(tempfile.mkdtemp(prefix='hatch-gas-')).resolve()
        try:
            for path,payload in source_payloads.items():core._write(root/'inputs'/path,payload)
            bundle,meshes,vertices,triangles=core._source_admission(root/'inputs',world,project_manifest)
            core.require(set(bundle['inventory'])==set(source_payloads),'PHYSICS_INPUTS','Gas inputs differ from native dependency closure.')
            estimate=self.estimate_resources(world,settings,source_bytes=total,mesh_vertices=vertices,mesh_triangles=triangles)
            worker=Path(__file__).with_name('physics_gas_worker.py').read_bytes()
            reader=Path(__file__).with_name('physics_gas_vdb.py').read_bytes()
            world_bytes=core.canonical(world);setting_bytes=core.canonical(settings)
            for name,data in [('world.json',world_bytes),('gas-settings.json',setting_bytes),('worker.py',worker),('reader.py',reader)]:core._write(root/name,data)
            job=dict(protocol=1,mode='simulate',expected_blend_sha256=None,world=world,settings=settings,meshes=meshes,worker_sha256=core.digest(worker),
                world_sha256=core.digest(world_bytes),settings_sha256=core.digest(setting_bytes))
            core._write(root/'gas-job.json',core.canonical(job))
            core._write(root/'vdb-job.json',core.canonical(dict(frames=world['frame_range']['end'],maximum_cells=math.prod(world['components'][0]['resolution']))))
            token=uuid.uuid4().hex;info=root.stat()
            prepared=core.PreparedSimulation(token,root,core.digest(world_bytes),self.backend_id,estimate,deepcopy(bundle['inventory']),bool(bundle['project']['provenance']['fixture']))
            self._owned[token]=dict(root=root,inode=(info.st_dev,info.st_ino),world=world,settings=settings,identity=identity,
                prepared=prepared,prepared_snapshot=deepcopy(prepared),inventory=deepcopy(bundle['inventory']),project_manifest=project_manifest,
                reader_identity=reader_identity,
                immutable={p.relative_to(root).as_posix():core.digest(p.read_bytes()) for p in root.rglob('*') if p.is_file()})
            return prepared
        except BaseException:
            shutil.rmtree(root);raise

    def simulate(self,prepared,progress=None,cancel_token=None,**unused):
        start=time.monotonic()
        core.require(not unused and (progress is None or callable(progress)) and
            (cancel_token is None or callable(getattr(cancel_token,'is_set',None))),'PHYSICS_REQUEST','Invalid gas execution request.')
        core.require(cancel_token is None or not cancel_token.is_set(),'PHYSICS_CANCELLED','Gas attempt cancelled.')
        with self._gas_lock:
            owner=self._owner(prepared)
            core.require(prepared.token not in self._attempted,'PHYSICS_STATE','Gas attempts execute once.')
            self._attempted.add(prepared.token)
        root=owner['root'];world=owner['world'];settings=owner['settings']
        deadline=start+prepared.estimate.timeout_seconds
        def remaining():
            core.require(cancel_token is None or not cancel_token.is_set(),'PHYSICS_CANCELLED','Gas attempt cancelled.')
            return core._remaining(deadline)
        def verify_inputs(overrides=None):
            expected={**owner['immutable'],**(overrides or {})}
            with core._Reader(root) as reader:
                for path,digest in expected.items():
                    remaining()
                    core.require(core.digest(reader.read(path,core.MAX_INPUT_BYTES))==digest,'PHYSICS_INPUT_CHANGED','Prepared gas input changed.')
        verify_inputs()
        core.require(self._discover(timeout=min(10,remaining()))==owner['identity'],'PHYSICS_BACKEND_CHANGED','Blender changed after gas preparation.')
        core.require(self._reader_identity(timeout=min(10,remaining()))==owner['reader_identity'],'PHYSICS_BACKEND_CHANGED','OpenVDB reader changed after gas preparation.')
        verify_inputs()
        usage=core._run_process([self.executable,'--background','--factory-startup','--disable-autoexec','--threads','2',
            '--python-exit-code','2','--python',str(root/'worker.py'),'--',str(root/'gas-job.json')],root,
            timeout=remaining(),memory_bytes=world['budgets']['memory_bytes'],
            output_bytes=min(world['budgets']['temporary_bytes'],core.MAX_CACHE_BYTES),cancel_token=cancel_token,progress=progress)
        verify_inputs()
        reader_usage=core._run_process([self.reader_python,'-I',str(root/'reader.py'),str(root)],root,timeout=remaining(),
            memory_bytes=world['budgets']['memory_bytes'],output_bytes=min(world['budgets']['temporary_bytes'],core.MAX_CACHE_BYTES),
            cancel_token=cancel_token,progress=progress,label='vdb-reader')
        report=core.parse((root/'gas-report.json').read_bytes())
        fields=core.parse((root/'volume-fields.json').read_bytes())
        validate_fields(fields,world)
        core.require(fields['version']==owner['reader_identity']['version'] and fields['module_sha256']==owner['reader_identity']['module_sha256'],
            'PHYSICS_BACKEND_CHANGED','Actual VDB reader differs from admitted reader identity.')
        core.require(report['native_bake'] is True and report['world_sha256']==prepared.world_sha256
            and report['settings_sha256']==core.digest(core.canonical(settings)),'PHYSICS_OUTPUT_MISSING','Native gas receipt differs from prepared request.')
        core.blend_container((root/'scene.blend').read_bytes())
        core.require(set(report['observed'])==set(OBSERVED_SETTINGS),'PHYSICS_OUTPUT_MISSING','Missing native setting readback.')
        for key,value in report['observed'].items():
            core.require(key in settings and (value==settings[key] if isinstance(value,bool) else abs(value-settings[key])<=1e-5),
                'PHYSICS_OUTPUT_MISSING','Native gas setting was not honored: '+key)
        job=core.parse((root/'gas-job.json').read_bytes())
        job.update(mode='reopen',expected_blend_sha256=core.digest((root/'scene.blend').read_bytes()))
        (root/'gas-job.json').write_bytes(core.canonical(job))
        verify_inputs({'gas-job.json':core.digest(core.canonical(job))})
        replay_usage=core._run_process([self.executable,'--background','--factory-startup','--disable-autoexec','--threads','2',
            '--python-exit-code','2','--python',str(root/'worker.py'),'--',str(root/'gas-job.json')],root,timeout=remaining(),
            memory_bytes=world['budgets']['memory_bytes'],output_bytes=min(world['budgets']['temporary_bytes'],core.MAX_CACHE_BYTES),
            cancel_token=cancel_token,progress=progress,label='replay')
        from PIL import Image
        for frame in settings['render_frames']:
            with Image.open(root/f'frame-{frame:04d}.png') as image:
                core.require(image.size==(settings['render_size'],settings['render_size']),'PHYSICS_OUTPUT_MISSING','Gas render size differs.')
                image.load()
                with Image.open(root/f'replay-frame-{frame:04d}.png') as replay:
                    replay.load()
                    core.require(image.mode==replay.mode and image.size==replay.size and image.tobytes()==replay.tobytes(),
                        'PHYSICS_CACHE_CORRUPT','Native gas replay changed rendered pixels.')
        files=core._inventory(root)
        core.require(sum(item['bytes'] for item in files)<=world['budgets']['cache_bytes'],
            'PHYSICS_RESOURCE_LIMIT','Gas cache exceeds its admitted byte budget.')
        usage.update(elapsed_seconds=time.monotonic()-start,
            peak_memory_bytes=max(usage['peak_memory_bytes'],reader_usage['peak_memory_bytes'],replay_usage['peak_memory_bytes']),
            measured_processes=['simulation','vdb-reader','replay'],output_bytes=sum(item['bytes'] for item in files))
        result=core.SimulationResult(prepared.token,root,prepared.world_sha256,owner['identity'],tuple(files),usage,
            dict(native_bake=True,independent_openvdb=True,fixture=prepared.fixture),prepared.fixture)
        owner['result']=result
        self._results[prepared.token]=deepcopy(result)
        return result

    def bake(self,result,destination):
        owner=self._owner(result)
        core.require(owner.get('result') is result and self._results.get(result.token)==result,
            'PHYSICS_STATE','Only the unchanged owned adapter result may publish.')
        destination=core._new_destination(destination)
        stage=Path(tempfile.mkdtemp(prefix='.hatch-gas-publish-',dir=destination.parent))
        try:
            with core._Reader(result.root) as reader:
                for item in result.files:
                    payload=reader.read(item['path'],core.MAX_CACHE_BYTES)
                    core.require(len(payload)==item['bytes'] and core.digest(payload)==item['sha256'],'PHYSICS_CACHE_CORRUPT','Gas output changed before publication.')
                    core._write(stage/item['path'],payload)
            manifest=dict(schema_version=1,kind='hatch-3d-gas-cache',world_sha256=result.world_sha256,
                settings_sha256=core.digest(core.canonical(owner['settings'])),source_manifest=owner['project_manifest'],
                files=list(result.files),backend=result.backend,usage=result.usage,fixture=result.fixture)
            core._write(stage/'gas-cache.json',core.canonical(manifest))
            verify_cache(stage)
            core._publish(stage,destination)
        except BaseException:
            shutil.rmtree(stage,ignore_errors=True);raise
        return verify_cache(destination)


def validate_fields(report,world):
    samples=report.get('samples')
    core.require(report.get('protocol')==1 and report.get('reader')=='OpenVDB' and type(samples) is list
        and len(samples)==world['frame_range']['end'],'PHYSICS_CACHE_CORRUPT','Incomplete independent volume samples.')
    maximum=math.prod(world['components'][0]['resolution'])
    for frame,sample in enumerate(samples):
        core.require(sample['frame']==frame and {'density','temperature','velocity'}<=set(sample['fields']),
            'PHYSICS_CACHE_CORRUPT','Missing or misordered gas fields.')
        for field in sample['fields'].values():
            core.require(0<=field['active_voxels']<=maximum and math.prod(field['resolution'])<=maximum
                and field['resolution']==world['components'][0]['resolution']
                and all(math.isfinite(field[k]) for k in ('minimum','maximum','total')),'PHYSICS_CACHE_CORRUPT','Invalid volume field bounds/values.')
    core.require(any(s['fields']['density']['maximum']>0 for s in samples),'PHYSICS_OUTPUT_MISSING','Native gas never produced density.')
    if world['components'][0]['combustion']:
        core.require(any(s['fields'].get('flame',{}).get('maximum',0)>0 for s in samples),'PHYSICS_OUTPUT_MISSING','Native fire never produced flame.')


def verify_cache(root):
    try:
        return _verify_cache(root)
    except core.BackendError:
        raise
    except (KeyError,TypeError,ValueError,OverflowError,OSError) as exc:
        raise core.BackendError('PHYSICS_CACHE_CORRUPT','Malformed or incomplete gas cache: '+str(exc)) from exc


def _verify_cache(root):
    root=Path(root)
    with core._Reader(root) as reader:
        manifest=core.parse(reader.read('gas-cache.json',core.MAX_DOCUMENT_BYTES))
        core.require(manifest.get('schema_version')==1 and manifest.get('kind')=='hatch-3d-gas-cache','PHYSICS_CACHE_CORRUPT','Unknown gas cache schema.')
        core.require(set(manifest)=={'schema_version','kind','world_sha256','settings_sha256','source_manifest','files','backend','usage','fixture'},
            'PHYSICS_CACHE_CORRUPT','Unknown gas cache fields.')
        files=manifest['files'];core._paths([x['path'] for x in files])
        core.require(len(files)<=core.MAX_FILES and len(files)==len({x['path'] for x in files}),'PHYSICS_CACHE_CORRUPT','Duplicate gas cache inventory.')
        core.require(reader.names()=={x['path'] for x in files}|{'gas-cache.json'},'PHYSICS_CACHE_CORRUPT','Gas cache inventory differs from actual files.')
        total=0
        for item in files:
            core.require(set(item)=={'path','bytes','sha256'} and type(item['bytes']) is int and 0<=item['bytes']<=core.MAX_CACHE_BYTES-total,
                'PHYSICS_CACHE_CORRUPT','Invalid gas cache file size.')
            total+=item['bytes']
            data=reader.read(item['path'],item['bytes'])
            core.require(len(data)==item['bytes'] and core.digest(data)==item['sha256'],'PHYSICS_CACHE_CORRUPT','Gas cache hash mismatch: '+item['path'])
        world=core.parse(reader.read('world.json',core.MAX_DOCUMENT_BYTES));settings=core.parse(reader.read('gas-settings.json',core.MAX_DOCUMENT_BYTES))
        world,settings=admit(world,settings)
        core.require(core.digest(core.canonical(world))==manifest['world_sha256'] and core.digest(core.canonical(settings))==manifest['settings_sha256'],
            'PHYSICS_CACHE_CORRUPT','Gas request identity mismatch.')
        required={'world.json','gas-settings.json','scene.blend','gas-report.json','volume-fields.json','replay-report.json',
            'worker.py','reader.py','gas-job.json','vdb-job.json'}
        required|={f'{prefix}frame-{frame:04d}.png' for frame in settings['render_frames'] for prefix in ('','replay-')}
        core.require(required<={item['path'] for item in files},'PHYSICS_CACHE_CORRUPT','Missing mandatory native gas artifacts.')
        native=core.parse(reader.read('gas-report.json',core.MAX_DOCUMENT_BYTES))
        replay=core.parse(reader.read('replay-report.json',core.MAX_DOCUMENT_BYTES))
        core.require(native['backend']=='Blender Mantaflow' and native['version']=='4.5.3 LTS' and native['build']==core.SUPPORTED_BLENDER_BUILD
            and native['native_bake'] is True and native['frame_count']==world['frame_range']['end'] and native['cache_format']=='OPENVDB'
            and native['world_sha256']==manifest['world_sha256'] and native['settings_sha256']==manifest['settings_sha256']
            and native['requested_resolution']==world['components'][0]['resolution'] and native['render_frames']==settings['render_frames'],
            'PHYSICS_CACHE_CORRUPT','Native gas receipt contradicts source or settings.')
        blend=reader.read('scene.blend',core.MAX_CACHE_BYTES)
        core.blend_container(blend)
        core.require(replay['mode']=='reopen' and replay['blend_sha256']==core.digest(blend) and replay['frames']==settings['render_frames'],
            'PHYSICS_CACHE_CORRUPT','Native replay does not bind the retained Blender project.')
        core.require(set(native['observed'])==set(OBSERVED_SETTINGS),'PHYSICS_CACHE_CORRUPT','Missing native setting observations.')
        for key,value in native['observed'].items():
            core.require(key in settings and abs(value-settings[key])<=1e-5,'PHYSICS_CACHE_CORRUPT','Native gas setting differs from request.')
        from PIL import Image
        for frame in settings['render_frames']:
            with (Image.open(io.BytesIO(reader.read(f'frame-{frame:04d}.png',4*1024**2))) as image,
                  Image.open(io.BytesIO(reader.read(f'replay-frame-{frame:04d}.png',4*1024**2))) as replay_image):
                core.require(image.size==replay_image.size==(settings['render_size'],settings['render_size']) and image.mode==replay_image.mode,
                    'PHYSICS_CACHE_CORRUPT','Gas replay render dimensions differ.')
                image.load();replay_image.load()
                core.require(image.tobytes()==replay_image.tobytes(),'PHYSICS_CACHE_CORRUPT','Gas replay render pixels differ.')
        # Source owners inspect paths; give them a private snapshot populated
        # only from the verified descriptor-relative bytes, not mutable input.
        with tempfile.TemporaryDirectory(prefix='hatch-gas-verify-') as folder:
            source=Path(folder)
            for item in files:
                if item['path'].startswith('inputs/'):
                    core._write(source/item['path'][7:],reader.read(item['path'],item['bytes']))
            bundle,_,_,_=core._source_admission(source,world,manifest['source_manifest'])
            core.require(type(manifest['fixture']) is bool and manifest['fixture']==bundle['project']['provenance']['fixture'],
                'PHYSICS_CACHE_CORRUPT','Gas fixture status contradicts preserved source provenance.')
        fields=core.parse(reader.read('volume-fields.json',core.MAX_DOCUMENT_BYTES));validate_fields(fields,world)
        for sample in fields['samples']:
            path=f'cache/data/fluid_data_{sample["frame"]+1:04d}.vdb'
            core.require(core.digest(reader.read(path,core.MAX_CACHE_BYTES))==sample['sha256'],'PHYSICS_CACHE_CORRUPT','VDB sample digest differs.')
    return dict(status='hash-and-source-verified',category='gas',sample_count=len(fields['samples']),
        world_sha256=manifest['world_sha256'],fixture=manifest['fixture'],native_reexecution=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['validate','simulate','verify'])
    parser.add_argument('path');parser.add_argument('--settings');parser.add_argument('--source-root');parser.add_argument('--destination')
    args=parser.parse_args()
    if args.command=='verify':result=verify_cache(args.path)
    else:
        def document(path):
            path=Path(path)
            with core._Reader(path.parent) as reader:return core.parse(reader.read(path.name,core.MAX_DOCUMENT_BYTES))
        world=document(args.path);settings=document(args.settings) if args.settings else DEFAULT_SETTINGS
        backend=GasBackend()
        if args.command=='validate':result=backend.validate_scene(world,settings)
        else:
            core.require(args.source_root and args.destination,'PHYSICS_REQUEST','Simulation needs source root and new destination.')
            source=Path(args.source_root)
            with core._Reader(source) as reader:
                names=reader.names();payloads={};total=0
                for name in sorted(names):
                    payload=reader.read(name,core.MAX_INPUT_BYTES-total);total+=len(payload);payloads[name]=payload
            prepared=backend.prepare(world,payloads,settings=settings)
            try:result=backend.bake(backend.simulate(prepared),args.destination)
            finally:backend.cleanup(prepared)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
