"""Fixed trusted Blender soft-body worker; never executes asset-supplied code."""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import sys
import bpy
from mathutils import Vector


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def load_job():
    root=Path(__file__).resolve().parent
    path=Path(sys.argv[sys.argv.index('--')+1])
    if path.name not in ('softbody-job.json','softbody-reopen-job.json') or path.is_symlink() or path.resolve().parent!=root:
        raise ValueError('Expected owned softbody-job.json beside worker')
    if path.stat().st_size>32*1024*1024:raise ValueError('Oversized job')
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate job key')
            result[key]=value
        return result
    job=json.loads(path.read_bytes(),object_pairs_hook=pairs)
    if set(job)!={'protocol','world','world_sha256','meshes','worker_sha256','mode'} or job['protocol']!=1:
        raise ValueError('Invalid worker protocol')
    if hashlib.sha256(canonical(job['world'])).hexdigest()!=job['world_sha256']:
        raise ValueError('Changed world')
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=job['worker_sha256']:
        raise ValueError('Changed trusted worker')
    if job['mode'] not in ('simulate','reopen'):raise ValueError('Invalid operation')
    if (path.name=='softbody-reopen-job.json') != (job['mode']=='reopen'):raise ValueError('Job mode and filename disagree')
    return root,job


def native(point):
    return point[0],-point[2],point[1]


def canonical_point(point):
    return [float(point[0]),float(point[2]),float(-point[1])]


def mesh_object(name,mesh):
    data=bpy.data.meshes.new(name)
    data.from_pydata([native(p) for p in mesh['positions']],[],mesh['triangles']);data.update()
    obj=bpy.data.objects.new(name,data);bpy.context.scene.collection.objects.link(obj)
    return obj


def state(scene,obj,frame):
    scene.frame_set(frame+1)
    evaluated=obj.evaluated_get(bpy.context.evaluated_depsgraph_get());mesh=evaluated.to_mesh()
    try:points=[canonical_point(evaluated.matrix_world@v.co) for v in mesh.vertices]
    finally:evaluated.to_mesh_clear()
    if not all(math.isfinite(x) and abs(x)<100000 for p in points for x in p):
        raise ValueError('Nonfinite or unbounded evaluated soft-body vertices')
    return {'frame':frame,'positions':points}


def execute(root,job):
    world=job['world'];component=world['components'][0];count=world['frame_range']['end'];identity=component['id']
    scene=bpy.context.scene
    if job['mode']=='simulate':
        bpy.ops.wm.read_factory_settings(use_empty=True);scene=bpy.context.scene
        bpy.context.preferences.filepaths.save_version=0
        scene.frame_start=1;scene.frame_end=count
        scene.render.fps=world['fps']['numerator'];scene.render.fps_base=world['fps']['denominator']
        scene.gravity=native(world['gravity'])
        for body in world['bodies']:
            floor=mesh_object(body['id'],job['meshes'][body['id']]);floor.modifiers.new('physics-collider','COLLISION')
            material=next(m for m in world['materials'] if m['id']==body['colliders'][0]['material'])
            floor.collision.thickness_outer=.01;floor.collision.thickness_inner=.01
            floor.collision.damping_factor=0.;floor.collision.friction_factor=material['dynamic_friction']
        obj=mesh_object(identity,job['meshes'][identity]);modifier=obj.modifiers.new('soft-body','SOFT_BODY');settings=modifier.settings
        settings.mass=component['mass']/component['vertex_count']
        settings.friction=0.;settings.use_edges=True;settings.pull=component['stiffness'];settings.push=component['stiffness']
        settings.damping=component['damping']*50;settings.bend=1.;settings.use_self_collision=False
        steps=world['solver']['iterations']*world['solver']['substeps'];settings.step_min=steps;settings.step_max=steps;settings.use_auto_step=False
        settings.use_goal=bool(component['pins'])
        if component['pins']:
            group=obj.vertex_groups.new(name='source-pins');group.add(component['pins'],1.,'REPLACE')
            settings.vertex_group_goal=group.name;settings.goal_default=1.;settings.goal_min=0.;settings.goal_max=1.;settings.goal_spring=.999;settings.goal_friction=0.
        cache=modifier.point_cache;cache.frame_start=1;cache.frame_end=count
        bpy.ops.wm.save_as_mainfile(filepath=str(root/'softbody.blend'))
        cache.use_disk_cache=True
        with bpy.context.temp_override(scene=scene,object=obj,active_object=obj,point_cache=cache):
            outcome=bpy.ops.ptcache.bake(bake=True)
        if outcome!={'FINISHED'} or not cache.is_baked:raise ValueError('Native soft-body bake failed')
        samples=[state(scene,obj,f) for f in range(count)]
        (root/'deformation.json').write_bytes(canonical({'component':identity,'samples':samples}))
        # Render actual evaluated geometry at beginning, impact and settled end.
        scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=4
        scene.render.resolution_x=160;scene.render.resolution_y=160;scene.render.resolution_percentage=100
        camera_data=bpy.data.cameras.new('diagnostic-camera');camera=bpy.data.objects.new('diagnostic-camera',camera_data)
        scene.collection.objects.link(camera);camera.location=(4,-6,3)
        camera.rotation_euler=(Vector((0,0,1))-camera.location).to_track_quat('-Z','Y').to_euler();scene.camera=camera
        light_data=bpy.data.lights.new('diagnostic-light','AREA');light=bpy.data.objects.new('diagnostic-light',light_data)
        scene.collection.objects.link(light);light.location=(2,-3,6);light_data.energy=800;light_data.size=4
        scene.world=bpy.data.worlds.new('diagnostic-world');scene.world.color=(.15,.15,.15)
        center=lambda points:[sum(p[i] for p in points)/len(points) for i in range(3)]
        def deviation(sample):
            points=sample['positions'];c=center(points)
            return max(abs(math.sqrt(sum((p[i]-c[i])**2 for i in range(3)))-.5) for p in points)
        impact=max(samples,key=deviation)['frame']
        for name,frame in [('initial',0),('impact',impact),('final',count-1)]:
            scene.frame_set(frame+1);scene.render.filepath=str(root/(name+'.png'));bpy.ops.render.render(write_still=True)
        bpy.ops.wm.save_as_mainfile(filepath=str(root/'softbody.blend'))
    else:
        bpy.ops.wm.open_mainfile(filepath=str(root/'softbody.blend'),load_ui=False,use_scripts=False)
        scene=bpy.context.scene;obj=bpy.data.objects[identity];modifier=next(m for m in obj.modifiers if m.type=='SOFT_BODY');settings=modifier.settings;cache=modifier.point_cache
        reference=json.loads((root/'deformation.json').read_bytes())['samples']
        # Baked cache consumption is tested under changed gravity and reverse seeks.
        scene.gravity=Vector((0,0,98.1))
        samples=[state(scene,obj,f) for f in reversed(range(count))]
        error=max(abs(a-b) for sample in samples for pa,pb in zip(sample['positions'],reference[sample['frame']]['positions']) for a,b in zip(pa,pb))
        if error>1e-5:raise ValueError('Reopened native bake disagrees with retained vertices')
    report={'ok':True,'operation':job['mode'],'world_sha256':job['world_sha256'],'sample_count':count,'vertex_count':len(obj.data.vertices),
        'fps':scene.render.fps,'fps_base':scene.render.fps_base,'time_scale':settings.speed,'disk_cache':cache.use_disk_cache,
        'application_version':bpy.app.version_string,'application_build':bpy.app.build_hash.decode(),'is_baked':cache.is_baked,'is_outdated':cache.is_outdated,
        'native_settings':{key:getattr(settings,key) for key in ('mass','friction','pull','push','damping','step_min','step_max','use_auto_step','use_goal')},
        'pin_settings':({'default':settings.goal_default,'minimum':settings.goal_min,'maximum':settings.goal_max,'spring':settings.goal_spring,'friction':settings.goal_friction,'weights':[next((g.weight for g in v.groups if g.group==obj.vertex_groups[settings.vertex_group_goal].index),0.) for v in obj.data.vertices]} if component['pins'] else None),
        'capabilities':{'surface_deformation':True,'volume_preservation':False,'pressure':False,'two_way_rigid_coupling':False}}
    if job['mode']=='reopen':report.update(maximum_vertex_error=error,changed_gravity_cache_consumption=True)
    (root/(job['mode']+'-report.json')).write_bytes(canonical(report))


if __name__=='__main__':
    root,job=load_job();execute(root,job)
