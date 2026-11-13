"""Bounded native Mantaflow liquid execution and hash-checked cache inspection.

This separate profile does not advertise liquid through the rigid-body adapter.
It executes one water domain; neither mass conservation nor two-way coupling is
certified. Cache hashes provide integrity, not authenticated provenance.
"""
from pathlib import Path
import io
import shutil
import tempfile
import time
from types import MappingProxyType

import physics_backends as core
from physics_liquid_settings import admit
from physics_liquid_validation import verify_geometry


def _verify_cache_snapshot(root, *, expected_world_sha256=None):
    """Return the public report and immutable bytes from its verified snapshot."""
    with core._Reader(root) as reader:
        manifest_bytes=reader.read('liquid-cache.json',1024*1024)
        manifest=core.parse(manifest_bytes)
        core.require(type(manifest) is dict and set(manifest)=={'protocol','kind','world_sha256','settings_sha256','files','backend','resources','project_manifest'} and
            manifest['protocol']==1 and manifest['kind']=='hatch-native-liquid-cache',
            'PHYSICS_CACHE_CORRUPT','Invalid liquid cache manifest.')
        files=manifest['files']
        core.require(type(files) is list and 1<=len(files)<=core.MAX_FILES,'PHYSICS_CACHE_CORRUPT','Invalid liquid inventory.')
        names=set();total=0;payloads={};entries={}
        for entry in files:
            core.require(type(entry) is dict and set(entry)=={'path','bytes','sha256'} and type(entry['bytes']) is int and
                0<=entry['bytes']<=core.MAX_CACHE_BYTES,'PHYSICS_CACHE_CORRUPT','Invalid inventory entry.')
            name=core.path_name(entry['path'])
            core.require(name not in names and name!='liquid-cache.json','PHYSICS_CACHE_CORRUPT','Duplicate inventory entry.')
            names.add(name);total+=entry['bytes'];entries[name]=entry
            core.require(total+len(manifest_bytes)<=core.MAX_CACHE_BYTES,'PHYSICS_CACHE_CORRUPT','Liquid cache exceeds bounded profile.')
        core.require(reader.names()==names|{'liquid-cache.json'},'PHYSICS_CACHE_CORRUPT','Liquid inventory is incomplete.')
        core.require({'world.json','liquid-settings.json'}<=names,'PHYSICS_CACHE_CORRUPT','Liquid configuration is missing.')
        # Admit the small configuration before reading bulk native artifacts.
        for name in ('world.json','liquid-settings.json'):
            entry=entries[name];payload=reader.read(name,min(entry['bytes'],1024*1024))
            core.require(len(payload)==entry['bytes'] and core.digest(payload)==entry['sha256'],
                'PHYSICS_CACHE_CORRUPT','Liquid configuration changed.')
            payloads[name]=payload
        world,settings=admit(core.parse(payloads['world.json']),core.parse(payloads['liquid-settings.json']))
        budgets=world['budgets']
        core.require(total+len(manifest_bytes)<=min(budgets['cache_bytes'],budgets['temporary_bytes']) and
            len(manifest_bytes)<=budgets['bake_file_bytes'] and
            all(entry['bytes']<=budgets['bake_file_bytes'] for entry in files),
            'PHYSICS_CACHE_LIMIT','Liquid cache exceeds retained world storage limits, including its manifest.')
        for name,entry in entries.items():
            if name in payloads:continue
            payload=reader.read(name,entry['bytes'])
            core.require(len(payload)==entry['bytes'] and core.digest(payload)==entry['sha256'],
                'PHYSICS_CACHE_CORRUPT','Liquid artifact changed.')
            payloads[name]=payload
        core.require(reader.names()==names|{'liquid-cache.json'},'PHYSICS_CACHE_CORRUPT','Liquid inventory changed during snapshot reads.')
        class Snapshot:
            def read(self,name,limit=core.MAX_CACHE_BYTES):
                value=payloads.get(name)
                core.require(value is not None and len(value)<=limit,'PHYSICS_CACHE_CORRUPT','Missing or excessive liquid artifact.')
                return value
        reader=Snapshot()
        core.require(core.digest(core.canonical(world))==manifest['world_sha256'] and
            core.digest(core.canonical(settings))==manifest['settings_sha256'] and
            (expected_world_sha256 is None or expected_world_sha256==manifest['world_sha256']),
            'PHYSICS_CACHE_CORRUPT','Liquid world or settings identity differs.')
        inputs={name[len('inputs/'):]:value for name,value in payloads.items() if name.startswith('inputs/')}
        core.require(inputs and sum(map(len,inputs.values()))<=core.MAX_INPUT_BYTES,'PHYSICS_CACHE_CORRUPT','Invalid liquid source inventory.')
        with tempfile.TemporaryDirectory(prefix='hatch-liquid-source-') as temporary:
            private=Path(temporary)
            for name,value in inputs.items():core._write(private/name,value)
            bundle,meshes,_,_=core._source_admission(private,world,core.path_name(manifest['project_manifest']))
            core.require(set(inputs)==set(bundle['inventory']),'PHYSICS_CACHE_CORRUPT','Liquid source closure differs.')
        worker=Path(__file__).with_name('physics_liquid_worker.py').read_bytes()
        job=core.parse(reader.read('liquid-job.json',32*1024**2))
        expected=dict(protocol=1,mode='reopen',expected_blend_sha256=core.digest(reader.read('scene.blend')),
            world=world,settings=settings,meshes=meshes,worker_sha256=core.digest(worker),
            world_sha256=manifest['world_sha256'],settings_sha256=manifest['settings_sha256'])
        core.require(job==expected and reader.read('worker.py')==worker,'PHYSICS_CACHE_CORRUPT','Liquid worker or source job differs.')
        replay=core.parse(reader.read('replay-report.json',1024*1024))
        core.require(type(replay) is dict and replay.get('mode')=='reopen' and
            replay.get('blend_sha256')==job['expected_blend_sha256'] and replay.get('frames')==list(range(world['frame_range']['end'])),
            'PHYSICS_CACHE_CORRUPT','Liquid replay binding differs.')
        report=core.parse(reader.read('liquid-report.json',1024*1024))
        core.require(report['world_sha256']==manifest['world_sha256'] and report['settings_sha256']==manifest['settings_sha256'] and
            report['native_bake'] is True and 'Blender '+report['version']==manifest['backend']['application_version'],
            'PHYSICS_CACHE_CORRUPT','Liquid worker report identity differs.')
        d=world['components'][0]
        observed=dict(fps_numerator=world['fps']['numerator'],fps_base=world['fps']['denominator'],
            resolution=d['resolution'][0],mesh_scale=settings['mesh_scale'],particle_radius=settings['particle_radius'],
            viscosity_enabled=d['viscosity']>0,viscosity=d['viscosity'],cfl=settings['cfl'],
            minimum_substeps=settings['minimum_substeps'],maximum_substeps=settings['maximum_substeps'],boundaries=settings['boundaries'])
        actual=report.get('observed')
        core.require(type(actual) is dict and set(actual)==set(observed),'PHYSICS_CACHE_CORRUPT','Liquid observed settings are incomplete.')
        for key,value in observed.items():
            same=abs(actual[key]-value)<=1e-6*max(1,abs(value)) if type(value) is float and type(actual[key]) in (int,float) else actual[key]==value
            core.require(same,'PHYSICS_CACHE_CORRUPT','Liquid native setting differs: '+key)
        core.require(report.get('frame_count')==world['frame_range']['end'] and report.get('render_frames')==settings['render_frames'] and
            report.get('cache_format')=='OPENVDB+BOBJECT' and report.get('build')==core.SUPPORTED_BLENDER_BUILD and
            replay.get('version')==report['version'] and replay.get('build')==report['build'] and
            replay.get('clock')==dict(fps_numerator=world['fps']['numerator'],fps_base=world['fps']['denominator']),
            'PHYSICS_CACHE_CORRUPT','Liquid native report timing, cache or build differs.')
        from PIL import Image
        for frame in settings['render_frames']:
            for prefix in ['', 'replay-']:
                payload=reader.read(f'{prefix}frame-{frame:04d}.png',4*1024**2)
                try:
                    with Image.open(io.BytesIO(payload)) as rendered:
                        core.require(rendered.format=='PNG' and rendered.size==(settings['render_size'],)*2,
                            'PHYSICS_CACHE_CORRUPT','Native liquid render has incorrect format or dimensions.')
                        rendered.load()
                        core.require(any(low!=high for low,high in rendered.convert('RGB').getextrema()),
                            'PHYSICS_CACHE_CORRUPT','Native liquid render is blank or has incorrect dimensions.')
                except (OSError,ValueError,Image.DecompressionBombError) as exc:
                    raise core.BackendError('PHYSICS_CACHE_CORRUPT','Invalid native liquid PNG.') from exc
        geometry=verify_geometry(reader,world)
        result=dict(status='verified',world_sha256=manifest['world_sha256'],geometry=geometry,
            backend=manifest['backend'],resources=manifest['resources'],review_status='unreviewed',
            limitations=['mass conservation unverified','no two-way coupling','no secondary particles','no solver continuation'])
        # Consumers must use these same bounded, verified bytes, rather than
        # reopening mutable caller paths after verification. Values are bytes.
        payloads['liquid-cache.json']=manifest_bytes
        return result, MappingProxyType(payloads)


def verify_cache(root, *, expected_world_sha256=None):
    report, _ = _verify_cache_snapshot(root, expected_world_sha256=expected_world_sha256)
    return report


def simulate(world, source_payloads, destination, *, settings, executable=None, project_manifest='project.json', cancel_token=None):
    world,settings=admit(world,settings)
    destination=core._new_destination(destination)
    core.require(type(source_payloads) is dict and all(type(v) is bytes for v in source_payloads.values()) and sum(len(v) for v in source_payloads.values())<=core.MAX_INPUT_BYTES,
        'PHYSICS_INPUTS','Liquid sources exceed input bound.')
    stage=Path(tempfile.mkdtemp(prefix='.'+destination.name+'.liquid-',dir=destination.parent))
    try:
        backend=core.BlenderBackend(executable);identity=backend._discover()
        identity={**identity,'solver_name':'Blender Mantaflow liquid','solver_version':'bundled with Blender'}
        for name,payload in source_payloads.items():
            core.path_name(name);core.require(type(payload) is bytes,'PHYSICS_INPUTS','Source payload must be bytes.')
            core._write(stage/'inputs'/name,payload)
        bundle,meshes,_,_=core._source_admission(stage/'inputs',world,project_manifest)
        core.require(set(bundle['inventory'])==set(source_payloads),'PHYSICS_INPUTS','Source mapping must equal native dependency closure.')
        worker=Path(__file__).with_name('physics_liquid_worker.py').read_bytes()
        core._write(stage/'worker.py',worker)
        core._write(stage/'world.json',core.canonical(world));core._write(stage/'liquid-settings.json',core.canonical(settings))
        job=dict(protocol=1,mode='simulate',expected_blend_sha256=None,world=world,settings=settings,meshes=meshes,
            worker_sha256=core.digest(worker),world_sha256=core.digest(core.canonical(world)),settings_sha256=core.digest(core.canonical(settings)))
        core._write(stage/'liquid-job.json',core.canonical(job))
        command=[backend.executable,'--background','--factory-startup','--disable-autoexec','--threads',str(min(2,world['budgets']['cpu_workers'])),
            '--python-exit-code','2','--python',str(stage/'worker.py'),'--',str(stage/'liquid-job.json')]
        deadline=time.monotonic()+min(180,world['budgets']['timeout_seconds'])
        resources=[]
        for mode in ['simulate','reopen']:
            if mode=='reopen':
                job.update(mode=mode,expected_blend_sha256=core.digest((stage/'scene.blend').read_bytes()))
                (stage/'liquid-job.json').unlink();core._write(stage/'liquid-job.json',core.canonical(job))
            resources.append(core._run_process(command,stage,timeout=core._remaining(deadline),
                memory_bytes=min(2*1024**3,world['budgets']['memory_bytes']),
                output_bytes=min(256*1024**2,world['budgets']['cache_bytes'],world['budgets']['temporary_bytes']),cancel_token=cancel_token,label=mode))
        with core._Reader(stage) as reader:verify_geometry(reader,world)
        manifest=dict(protocol=1,kind='hatch-native-liquid-cache',world_sha256=job['world_sha256'],
            settings_sha256=job['settings_sha256'],files=core._inventory(stage),backend=identity,resources=resources,project_manifest=project_manifest)
        manifest_bytes=core.canonical(manifest)
        core.require(sum(x['bytes'] for x in manifest['files'])+len(manifest_bytes)<=min(core.MAX_CACHE_BYTES,world['budgets']['cache_bytes'],world['budgets']['temporary_bytes']) and
            len(manifest_bytes)<=world['budgets']['bake_file_bytes'] and all(x['bytes']<=world['budgets']['bake_file_bytes'] for x in manifest['files']),
            'PHYSICS_RESOURCE_LIMIT','Liquid output exceeds admitted storage limits.')
        core._write(stage/'liquid-cache.json',manifest_bytes)
        result=verify_cache(stage,expected_world_sha256=job['world_sha256'])
        core._publish(stage,destination)
        return result
    finally:
        if stage.exists():shutil.rmtree(stage)


def main(argv=None):
    import argparse
    import json
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    demo=commands.add_parser('demo');demo.add_argument('destination');demo.add_argument('--blender');demo.add_argument('--moving',action='store_true')
    verify=commands.add_parser('verify');verify.add_argument('cache')
    args=parser.parse_args(argv)
    if args.command=='demo':
        from physics_liquid_example import build_liquid_inputs
        world,payloads,settings=build_liquid_inputs(moving=args.moving)
        result=simulate(world,payloads,args.destination,settings=settings,executable=args.blender)
    else:result=verify_cache(args.cache)
    print(json.dumps(result,sort_keys=True,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
