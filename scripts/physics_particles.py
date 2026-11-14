"""Versioned CPU particle solver with exact inputs, real state and bounded caches.

Pure numerical integration uses a package-owned subprocess. Scene3D owns source
closure and transforms; rig_import owns mesh decoding. This adapter does not
claim fluid dynamics, a GPU backend, or production visual approval.
"""
from dataclasses import dataclass
from fractions import Fraction
import argparse
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

VERSION='1.0.0'
MAX_PARTICLES=1024
MAX_FRAMES=120


def schemas():
    number=dict(type='number',minimum=0,maximum=10000)
    rate=dict(type='object',additionalProperties=False,required=['numerator','denominator'],properties={
        'numerator':dict(type='integer',minimum=0,maximum=1000000),
        'denominator':dict(type='integer',minimum=1,maximum=1000000)})
    emitter=dict(type='object',additionalProperties=False,required=['component','shape','rate','burst','start','end','spread','drag','restitution','collision'],properties={
        'component':dict(type='string',pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
        'shape':dict(enum=['point','box','sphere','mesh-surface']),
        'rate':rate,'start':rate,'end':rate,'burst':dict(type='integer',minimum=0,maximum=MAX_PARTICLES),
        'spread':dict(type='number',minimum=0,maximum=math.pi),'drag':number,
        'restitution':dict(type='number',minimum=0,maximum=1),'collision':dict(enum=['none','bounce','remove'])})
    return {'physics-particle-settings.schema.json':dict(
        **{'$schema':'https://json-schema.org/draft/2020-12/schema'},
        title='Particle settings bound to exact PhysicsWorld v1',type='object',additionalProperties=False,
        required=['schema_version','kind','world_sha256','emitters'],properties={
            'schema_version':dict(const=1),'kind':dict(const='hatch-physics-particle-settings'),
            'world_sha256':dict(type='string',pattern=r'^[0-9a-f]{64}$'),
            'emitters':dict(type='array',minItems=1,maxItems=64,items=emitter)})}


def admit(world,settings):
    from jsonschema import Draft202012Validator
    world=core._world(world)
    settings=core.parse(core.canonical(settings))
    errors=list(Draft202012Validator(next(iter(schemas().values()))).iter_errors(settings))
    core.require(not errors,'PARTICLE_SETTINGS',str(errors[0].message) if errors else 'Invalid settings.')
    core.require(settings['world_sha256']==core.digest(core.canonical(world)),'PARTICLE_SOURCE_CHANGED','Settings bind a different physical world.')
    core.require(world['solver']['backend']=='particles-native' and world['solver']['adapter_version']==VERSION and
        set(world['solver']['required_categories'])==({'particles','rigid-body'} if world['bodies'] else {'particles'}),'PARTICLE_BACKEND','Select the versioned particles-native backend.')
    core.require('stability' not in world['solver'] and not world['joints'] and not world['events'],
        'PARTICLE_UNSUPPORTED','Rigid stability, joints and interaction events are not particle settings.')
    core.require(world['frame_range']['start']==0 and 2<=world['frame_range']['end']<=MAX_FRAMES,
        'PARTICLE_LIMIT','Particle jobs require 2..120 zero-origin frames.')
    core.require(0<core._ratio(world['fps'])<=240 and Fraction(1,10000)<=core._ratio(world['solver']['fixed_step'])/world['solver']['substeps']<=Fraction(1,10),
        'PARTICLE_LIMIT','Output rate or integration step exceeds particle bounds.')
    core.require(all(c['category']=='particles' for c in world['components']) and 1<=len(world['components'])<=64,
        'PARTICLE_UNSUPPORTED','This solver accepts particle components only.')
    components={c['id']:c for c in world['components']}
    core.require(len(settings['emitters'])==len(components) and {e['component'] for e in settings['emitters']}==set(components),
        'PARTICLE_SETTINGS','Exactly one emitter setting is required per component.')
    for c in components.values():
        core.require(c['radius']<=100 and c['mass']<=1e6 and max(map(abs,c['initial_velocity']))<=10000,
            'PARTICLE_LIMIT','Particle radius, mass or velocity exceeds the admitted bound.')
        core.require(set(c['colliders'])<= {b['id'] for b in world['bodies']},'PARTICLE_COLLISION_UNSUPPORTED','Collider reference is outside the admitted bodies.')
    for emitter in settings['emitters']:
        for field in ('rate','start','end'):
            emitter[field]={k:int(v) for k,v in emitter[field].items()}
        emitter['burst']=int(emitter['burst'])
        core.require(core._ratio(emitter['start'])<core._ratio(emitter['end']), 'PARTICLE_TIME','Emission end must follow start.')
        core.require((emitter['collision']=='none') == (not components[emitter['component']]['colliders']),
            'PARTICLE_COLLISION_UNSUPPORTED','Collision mode and explicit collider list must agree.')
    for body in world['bodies']:
        core.require(body['mode']=='static' and len(body['colliders'])==1 and not body['colliders'][0]['sensor'] and
            body['colliders'][0]['group']==1 and body['colliders'][0]['mask']==2**32-1 and
            body['parent'] is None and body['animation'] is None,
            'PARTICLE_UNSUPPORTED','Only static unfiltered nonsensor colliders are supported; coupling is one way.')
    core.require(len(world['bodies'])<=32,'PARTICLE_LIMIT','At most 32 static collider bodies are supported.')
    for force in world['forces']:
        core.require(force['type'] in ('wind','force','drag','turbulence') and set(force['bodies'])<=set(components) and
            force['categories']==['particles'],'PARTICLE_UNSUPPORTED','Only particle-directed force, wind, drag and turbulence are supported.')
    count=sum(c['count'] for c in components.values())
    frames=world['frame_range']['end']
    core.require(count<=min(MAX_PARTICLES,world['budgets']['particles']) and count*frames<=100000,
        'PARTICLE_LIMIT','Particle/frame budget exceeds the documented bounded solver.')
    estimate=1024*1024+count*frames*1024
    core.require(estimate<=min(world['budgets']['cache_bytes'],world['budgets']['temporary_bytes']) and
        world['budgets']['memory_bytes']>=64*1024*1024+estimate*4,
        'PARTICLE_LIMIT','Particle output or memory estimate exceeds the world budget.')
    return world,settings


@dataclass(frozen=True)
class PreparedParticles:
    token:str
    root:Path
    world_sha256:str
    settings_sha256:str


class ParticleBackend:
    def __init__(self,interpreter=None):
        self.interpreter=str(Path(interpreter or sys.executable).absolute())
        self._owned={};self._attempts=set();self._lock=threading.Lock()

    def capabilities(self):
        return dict(backend='particles-native',adapter_version=VERSION,solver='analytic force/linear-drag CPU particle integrator',
            device='cpu',gpu=False,emitter_shapes=['point','box','sphere','mesh-surface'],max_particles=MAX_PARTICLES,
            max_frames=MAX_FRAMES,collision='optional PyBullet 3.2.7 discrete sphere/static contact; one-way bounce or removal; no swept/CCD',
            readiness='configuration only; execute a real job to establish readiness',
            representation='point masses with separately exported octahedral display geometry')

    def prepare(self,world,settings,source_payloads,*,project_manifest='project.json'):
        from physics_component_sources import admit_component_meshes
        world,settings=admit(world,settings)
        source_payloads=dict(source_payloads)
        core.require(1<=len(source_payloads)<=64 and all(type(v) is bytes for v in source_payloads.values()),'PARTICLE_SOURCE','Supply a bounded exact-byte native source closure.')
        core._paths(source_payloads);core.path_name(project_manifest)
        core.require(sum(map(len,source_payloads.values()))<=core.MAX_INPUT_BYTES,'PARTICLE_LIMIT','Source byte budget exceeded.')
        root=Path(tempfile.mkdtemp(prefix='hatch-particles-'))
        try:
            sources=root/'inputs';sources.mkdir()
            for name,data in source_payloads.items():core._write(sources/name,data)
            # The canonical body reader loads the complete native bundle once;
            # component-only refs are subsequently admitted by the shared helper.
            body_world=core.parse(core.canonical(world))
            body_ids={b['asset'] for b in world['bodies']}
            body_world['assets']=[a for a in world['assets'] if a['id'] in body_ids]
            bundle,body_meshes,vertices,triangles=core._source_admission(sources,body_world,project_manifest)
            core.require(set(bundle['inventory'])==set(source_payloads),'PARTICLE_SOURCE','Source mapping must equal its complete native closure.')
            geometry=admit_component_meshes(sources,world,bundle)
            core.require(vertices+geometry['vertices']<=world['budgets']['vertices'] and triangles+geometry['triangles']<=world['budgets']['triangles'],
                'PARTICLE_LIMIT','Combined source meshes exceed world budgets.')
            used=body_ids|{c['geometry'] for c in world['components']}
            core.require(used=={a['id'] for a in world['assets']},'PARTICLE_SOURCE','Every declared source must be used by an admitted body or emitter.')
            emitters={k:dict(vertices=v['positions'],triangles=v['triangles'],origin=[v['world_matrix'][i][3] for i in range(3)])
                for k,v in geometry['components'].items()}
            worker=Path(__file__).with_name('physics_particle_worker.py').read_bytes()
            referenced={body for component in world['components'] for body in component['colliders']}
            job=dict(world=world,settings=settings,emitters=emitters,colliders=[body for body in world['bodies'] if body['id'] in referenced])
            if any(e['collision']!='none' for e in settings['emitters']):
                helper=Path(__file__).with_name('physics_collision_native.py').read_bytes()
                core._write(root/'physics_collision_native.py',helper)
            for name,data in [('world.json',core.canonical(world)),('settings.json',core.canonical(settings)),('worker.py',worker),('worker-job.json',core.canonical(job))]:core._write(root/name,data)
            prepared=PreparedParticles(uuid.uuid4().hex,root,core.digest(core.canonical(world)),core.digest(core.canonical(settings)))
            inventory={n:core.digest((root/n).read_bytes()) for n in ('world.json','settings.json','worker.py','worker-job.json')}
            if (root/'physics_collision_native.py').exists():inventory['physics_collision_native.py']=core.digest((root/'physics_collision_native.py').read_bytes())
            self._owned[prepared.token]=dict(prepared=prepared,world=world,settings=settings,inventory=inventory,
                sources={n:core.digest(d) for n,d in source_payloads.items()},fixture=geometry['fixture'],project_manifest=project_manifest)
            return prepared
        except BaseException:
            shutil.rmtree(root);raise

    def simulate(self,prepared,*,cancel_token=None,progress=None):
        core.require((progress is None or callable(progress)) and (cancel_token is None or callable(getattr(cancel_token,'is_set',None))),
            'PARTICLE_REQUEST','Invalid callback or cancellation token.')
        core.require(cancel_token is None or not cancel_token.is_set(),'PHYSICS_CANCELLED','Particle request was already cancelled.')
        with self._lock:
            owner=self._owned.get(getattr(prepared,'token',None))
            core.require(owner is not None and owner['prepared']==prepared,'PARTICLE_OWNERSHIP','Unknown or altered prepared attempt.')
            core.require(prepared.token not in self._attempts,'PARTICLE_STATE','Prepared particle attempts execute once.')
            self._attempts.add(prepared.token)
        with core._Reader(prepared.root) as reader:
            for name,expected in owner['inventory'].items():core.require(core.digest(reader.read(name))==expected,'PARTICLE_SOURCE_CHANGED','Prepared bytes changed.')
        with core._Reader(prepared.root/'inputs') as reader:
            for name,expected in owner['sources'].items():core.require(core.digest(reader.read(name))==expected,'PARTICLE_SOURCE_CHANGED','Prepared source bytes changed.')
        budget=owner['world']['budgets']
        usage=core._run_process([self.interpreter,'-I',str(prepared.root/'worker.py'),str(prepared.root/'worker-job.json')],
            prepared.root,timeout=budget['timeout_seconds'],memory_bytes=budget['memory_bytes'],output_bytes=budget['temporary_bytes'],
            cancel_token=cancel_token,progress=progress)
        particles,events,report=_read_results(prepared.root,owner['world'])
        owner['result']=dict(usage=usage,particles=particles,events=events,report=report)
        owner['completed_inventory']={p.relative_to(prepared.root).as_posix():core.digest(p.read_bytes()) for p in prepared.root.rglob('*') if p.is_file()}
        return prepared

    def bake(self,result,destination):
        from atomic_publish import private_stage_fd,rename_no_replace_at
        owner=self._owned.get(getattr(result,'token',None))
        core.require(owner is not None and owner['prepared']==result and 'result' in owner,'PARTICLE_STATE','Only completed owned results can be baked.')
        destination=core._new_destination(destination)
        with core._Reader(destination.parent) as parent, private_stage_fd(parent.fd,prefix='.'+destination.name+'.particle-') as stage_fd:
            os.mkdir('payload',mode=0o700,dir_fd=stage_fd)
            payload_fd=os.open('payload',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=stage_fd)
            try:
                with core._Reader(result.root) as reader:
                    names=reader.names()
                    core.require(names==set(owner['completed_inventory']),'PARTICLE_SOURCE_CHANGED','Completed particle inventory changed.')
                    for name in sorted(names):
                        data=reader.read(name)
                        core.require(owner['completed_inventory'][name]==core.digest(data),'PARTICLE_SOURCE_CHANGED','Completed particle result changed before publication.')
                        core._write_relative(payload_fd,name,data)
                from physics_particle_worker import export_obj
                for sample in owner['result']['particles']['samples']:
                    core._write_relative(payload_fd,'geometry/frame-%04d.obj'%sample['frame'],export_obj(sample))
                files=core._inventory_relative(payload_fd)
                manifest=dict(schema_version=1,kind='hatch-physics-particle-cache',world_sha256=result.world_sha256,
                    settings_sha256=result.settings_sha256,files=files,fixture=owner['fixture'],review_status='unreviewed',
                    project_manifest=owner['project_manifest'],backend=self.capabilities(),execution=owner['result']['report'],usage=owner['result']['usage'])
                core._write_relative(payload_fd,'particle-cache.json',core.canonical(manifest))
                report=_verify_at(payload_fd)
                try:rename_no_replace_at(stage_fd,'payload',parent.fd,destination.name)
                except FileExistsError as exc:
                    raise core.BackendError('PHYSICS_DESTINATION_EXISTS','A destination created during staging was preserved.',category='invalid-input') from exc
                except OSError as exc:
                    raise core.BackendError('PHYSICS_PUBLICATION_UNAVAILABLE','Exclusive output publication failed safely: '+str(exc)) from exc
                return report
            finally:os.close(payload_fd)

    def cleanup(self,prepared):
        owner=self._owned.get(getattr(prepared,'token',None))
        core.require(owner is not None and owner['prepared']==prepared,'PARTICLE_OWNERSHIP','Unknown particle workspace.')
        shutil.rmtree(prepared.root);del self._owned[prepared.token]


def _read_results(root,world):
    with core._Reader(root) as reader:
        particles=core.parse(reader.read('particles.json'));events=core.parse(reader.read('events.json'));report=core.parse(reader.read('report.json'))
    core.require(particles.get('kind')=='hatch-particle-states' and particles.get('fps')==world['fps'] and
        particles.get('frame_range')==world['frame_range'] and len(particles.get('samples',[]))==world['frame_range']['end'],
        'PARTICLE_OUTPUT','Missing or mismatched particle samples.')
    components={c['id']:c for c in world['components']}
    for frame,sample in enumerate(particles['samples']):
        core.require(sample.get('frame')==frame and sample.get('time')==dict(numerator=(Fraction(frame)/core._ratio(world['fps'])).numerator,
            denominator=(Fraction(frame)/core._ratio(world['fps'])).denominator),'PARTICLE_OUTPUT','Invalid exact sample timestamp.')
        seen=set()
        core.require(len(sample.get('particles',[]))<=sum(c['count'] for c in components.values()),'PARTICLE_OUTPUT','Particle sample exceeds count bounds.')
        for particle in sample['particles']:
            core.require(set(particle)=={'id','component','position','velocity','age','radius','mass','birth_time'} and particle['id'] not in seen and
                particle['component'] in components,'PARTICLE_OUTPUT','Particle record fields or identity are invalid.')
            seen.add(particle['id']);c=components[particle['component']]
            core.require(all(type(x) in (int,float) and math.isfinite(x) and abs(x)<=1e8 for k in ('position','velocity') for x in particle[k]) and
                len(particle['position'])==len(particle['velocity'])==3 and 0<=particle['age']<c['lifetime_seconds']+1e-8 and
                particle['radius']==c['radius'] and particle['mass']==c['mass'],'PARTICLE_OUTPUT','Particle numerical state is invalid.')
    core.require(events.get('kind')=='hatch-particle-events' and report.get('solver')=='hatch-particles-cpu' and
        report.get('version')==VERSION and report.get('device')=='cpu','PARTICLE_OUTPUT','Invalid solver/event report.')
    return particles,events,report


def _verify_snapshot(root):
    root=Path(root)
    with core._Reader(root) as reader:
        manifest_bytes=reader.read('particle-cache.json',1024*1024)
        manifest=core.parse(manifest_bytes)
        core.require(set(manifest)=={'schema_version','kind','world_sha256','settings_sha256','files','fixture','review_status','project_manifest','backend','execution','usage'} and
            manifest['schema_version']==1 and manifest['kind']=='hatch-physics-particle-cache' and manifest['review_status']=='unreviewed',
            'PARTICLE_CACHE','Invalid particle manifest.')
        names=[f['path'] for f in manifest['files']];core._paths(names)
        core.require(len(set(names))==len(names) and len(names)<=256,'PARTICLE_CACHE','Invalid cache inventory.')
        actual={p.relative_to(root).as_posix() for p in root.rglob('*') if not p.is_dir()}
        core.require(actual==set(names)|{'particle-cache.json'},'PARTICLE_CACHE','Cache files differ from inventory.')
        for item in manifest['files']:
            core.require(set(item)=={'path','bytes','sha256'},'PARTICLE_CACHE','Invalid file record.')
            data=reader.read(item['path'])
            core.require(len(data)==item['bytes'] and core.digest(data)==item['sha256'],'PARTICLE_CACHE','Cache bytes differ from inventory.')
        world_data=reader.read('world.json');settings_data=reader.read('settings.json')
        core.require(core.digest(world_data)==manifest['world_sha256'] and core.digest(settings_data)==manifest['settings_sha256'],
            'PARTICLE_CACHE','World/settings identity differs.')
        world,settings=admit(core.parse(world_data),core.parse(settings_data))
        budget=world['budgets']
        core.require(sum(item['bytes'] for item in manifest['files'])+len(manifest_bytes)<=
            min(budget['cache_bytes'],budget['temporary_bytes'],core.MAX_CACHE_BYTES) and
            len(manifest_bytes)<=budget['bake_file_bytes'] and
            all(item['bytes']<=budget['bake_file_bytes'] for item in manifest['files']),
            'PARTICLE_LIMIT','Published particle cache exceeds retained world file or aggregate budgets.')
    from physics_component_sources import admit_component_meshes
    body_world=core.parse(core.canonical(world))
    body_ids={body['asset'] for body in world['bodies']}
    body_world['assets']=[item for item in world['assets'] if item['id'] in body_ids]
    bundle,_,_,_=core._source_admission(root/'inputs',body_world,manifest['project_manifest'])
    geometry=admit_component_meshes(root/'inputs',world,bundle)
    core.require(geometry['fixture']==manifest['fixture'],'PARTICLE_CACHE','Fixture provenance differs.')
    particles,events,report=_read_results(root,world)
    from physics_particle_worker import export_obj
    with core._Reader(root) as reader:
        for sample in particles['samples']:
            core.require(reader.read('geometry/frame-%04d.obj'%sample['frame'])==export_obj(sample),
                'PARTICLE_CACHE','Display geometry differs from evaluated particle states.')
        job=core.parse(reader.read('worker-job.json'))
        expected_emitters={key:dict(vertices=value['positions'],triangles=value['triangles'],
            origin=[value['world_matrix'][i][3] for i in range(3)]) for key,value in geometry['components'].items()}
        references={body for component in world['components'] for body in component['colliders']}
        core.require(job==dict(world=world,settings=settings,emitters=expected_emitters,
            colliders=[body for body in world['bodies'] if body['id'] in references]),
            'PARTICLE_CACHE','Worker job differs from exact native inputs.')
    core.require(manifest['backend']==ParticleBackend().capabilities(),'PARTICLE_CACHE','Backend capability identity differs.')
    core.require(report==manifest['execution'],'PARTICLE_CACHE','Solver execution report differs.')
    return dict(status='hash-and-state-verified',backend='particles-native',sample_count=len(particles['samples']),
        world_sha256=manifest['world_sha256'],settings_sha256=manifest['settings_sha256'],fixture=manifest['fixture'],review_status='unreviewed',
        evidence_authentication='unsigned consistency evidence, not authenticated provenance')


def verify(root):
    """Verify a detached exact-byte snapshot, never a changing caller directory."""
    return _verify_cache(root)


def _verify_at(directory_fd):
    return _verify_cache(None,directory_fd=directory_fd)


def _verify_cache(root,*,directory_fd=None):
    with (core._Reader(root) if directory_fd is None else core._Reader.from_descriptor(directory_fd)) as reader:
        manifest_bytes=reader.read('particle-cache.json',1024*1024)
        manifest=core.parse(manifest_bytes)
        core.require(type(manifest) is dict and type(manifest.get('files')) is list and len(manifest['files'])<=256,
            'PARTICLE_CACHE','Invalid bounded particle inventory.')
        names=[]
        for item in manifest['files']:
            core.require(type(item) is dict and set(item)=={'path','bytes','sha256'} and type(item['bytes']) is int and
                0<=item['bytes']<=core.MAX_CACHE_BYTES,'PARTICLE_CACHE','Invalid cache file record.')
            names.append(item['path'])
        core._paths(names)
        core.require(len(set(names))==len(names) and sum(item['bytes'] for item in manifest['files'])<=core.MAX_CACHE_BYTES,
            'PARTICLE_CACHE','Duplicate files or excessive cache bytes.')
        actual=reader.names()
        core.require(actual==set(names)|{'particle-cache.json'},'PARTICLE_CACHE','Unexpected or missing cache content.')
        # Admit the small configuration before copying bulk payloads into a snapshot.
        entries={item['path']:item for item in manifest['files']};configuration={}
        core.require({'world.json','settings.json'}<=set(entries),'PARTICLE_CACHE','Missing particle configuration.')
        for name in ('world.json','settings.json'):
            entry=entries[name];data=reader.read(name,min(entry['bytes'],1024*1024))
            core.require(len(data)==entry['bytes'] and core.digest(data)==entry['sha256'],
                'PARTICLE_CACHE','Changed or corrupt particle configuration.')
            configuration[name]=data
        world,_=admit(core.parse(configuration['world.json']),core.parse(configuration['settings.json']))
        budget=world['budgets']
        core.require(sum(item['bytes'] for item in manifest['files'])+len(manifest_bytes)<=
            min(budget['cache_bytes'],budget['temporary_bytes'],core.MAX_CACHE_BYTES) and
            len(manifest_bytes)<=budget['bake_file_bytes'] and
            all(item['bytes']<=budget['bake_file_bytes'] for item in manifest['files']),
            'PARTICLE_LIMIT','Published particle cache exceeds retained world file or aggregate budgets.')
        with tempfile.TemporaryDirectory(prefix='hatch-particle-verify-') as directory:
            snapshot=Path(directory)
            core._write(snapshot/'particle-cache.json',manifest_bytes)
            for item in manifest['files']:
                data=configuration.get(item['path'])
                if data is None:data=reader.read(item['path'],item['bytes'])
                core.require(len(data)==item['bytes'] and core.digest(data)==item['sha256'],'PARTICLE_CACHE','Changed or corrupt particle cache payload.')
                core._write(snapshot/item['path'],data)
            core.require(reader.names()==set(names)|{'particle-cache.json'},'PARTICLE_CACHE','Particle inventory changed during snapshot reads.')
            return _verify_snapshot(snapshot)


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('capabilities')
    p=sub.add_parser('verify');p.add_argument('cache')
    p=sub.add_parser('run');p.add_argument('world');p.add_argument('settings');p.add_argument('source');p.add_argument('destination')
    args=parser.parse_args()
    if args.command=='capabilities':result=ParticleBackend().capabilities()
    elif args.command=='verify':result=verify(args.cache)
    else:
        source=Path(args.source);payloads={p.relative_to(source).as_posix():p.read_bytes() for p in source.rglob('*') if p.is_file()}
        backend=ParticleBackend();prepared=backend.prepare(core.parse(Path(args.world).read_bytes()),core.parse(Path(args.settings).read_bytes()),payloads)
        try:result=backend.bake(backend.simulate(prepared),args.destination)
        finally:backend.cleanup(prepared)
    print(json.dumps(result,sort_keys=True,indent=2))

if __name__=='__main__':main()
