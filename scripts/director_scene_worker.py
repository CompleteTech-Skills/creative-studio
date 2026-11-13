"""Fixed Blender adapter for native Director scene assets; no asset-supplied code."""
import json
import math
import re
import sys
from pathlib import Path
import bpy
from mathutils import Matrix


def canonical(point):
    return [float(point.x),float(point.z),float(-point.y)]


def import_asset(path):
    before=set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path,merge_vertices=False,import_pack_images=True,bone_heuristic='BLENDER',disable_bone_shape=True)
    return set(bpy.data.objects)-before


def create(job):
    bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
    scene=bpy.context.scene;scene.render.fps=24;scene.frame_start=0;scene.frame_end=119
    for actor,data in job['actors'].items():
        objects=import_asset(data['source'])
        world=bpy.data.objects.new(actor+'-world',None);scene.collection.objects.link(world)
        world.matrix_world=Matrix(data['matrix'])
        for obj in objects:
            if obj.parent not in objects:obj.parent=world
            obj['director_actor']=actor
            if obj.type=='MESH':
                original=re.sub(r'\.\d{3}$','',obj.name)
                if original not in data['meshes']:raise ValueError('Unknown imported mesh '+original)
                obj['director_mesh']=original
            if obj.animation_data and obj.animation_data.nla_tracks:
                track=next((t for t in obj.animation_data.nla_tracks if t.name==actor+'-investigation'),None)
                if track is None:track=next((t for t in obj.animation_data.nla_tracks if t.strips and actor+'-investigation' in t.strips[0].action.name),None)
                if track is None:raise ValueError('Missing native actor action')
                obj.animation_data.action=track.strips[0].action
                obj.animation_data.action_slot=track.strips[0].action_slot
                for item in obj.animation_data.nla_tracks:item.mute=True
    import_asset(job['laboratory'])
    key_objects=import_asset(job['key'])
    key_world=bpy.data.objects.new('access-key-world',None);scene.collection.objects.link(key_world)
    for obj in key_objects:
        if obj.parent not in key_objects:obj.parent=key_world
        obj['director_prop']='access-key'
    key_world.rotation_mode='QUATERNION'
    for key in job['key_frames']:
        key_world.matrix_world=Matrix(key['matrix'])
        key_world.keyframe_insert(data_path='location',frame=key['frame'])
        key_world.keyframe_insert(data_path='rotation_quaternion',frame=key['frame'])
        key_world.keyframe_insert(data_path='scale',frame=key['frame'])
    for name,data in job['cameras'].items():
        camera=bpy.data.objects.new(name,bpy.data.cameras.new(name));scene.collection.objects.link(camera)
        camera.matrix_world=Matrix(data['matrix']);camera.data.sensor_fit='VERTICAL';camera.data.sensor_height=24.;camera.data.lens=12./math.tan(data['fov']/2);camera.data.clip_start=.05;camera.data.clip_end=100
    scene.camera=bpy.data.objects['wide']
    for frame,name in [(0,'wide'),(60,'investigation')]:
        marker=scene.timeline_markers.new(name,frame=frame);marker.camera=bpy.data.objects[name]
    for name,data in job['lights'].items():
        light=bpy.data.objects.new(name,bpy.data.lights.new(name,'AREA'));scene.collection.objects.link(light)
        light.location=data['position'];light.data.energy=data['energy'];light.data.shape='DISK';light.data.size=4
    scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=16
    scene.render.threads_mode='FIXED';scene.render.threads=2
    scene.render.resolution_x=640;scene.render.resolution_y=360;scene.render.resolution_percentage=100
    scene.world=bpy.data.worlds.new('Laboratory world');scene.world.use_nodes=True
    scene.world.node_tree.nodes['Background'].inputs['Strength'].default_value=.15
    scene.frame_set(0)
    bpy.ops.wm.save_as_mainfile(filepath=job['output'],compress=True)


def inspect(job):
    scene=bpy.context.scene; samples=[]
    for frame in job['samples']:
        scene.frame_set(frame);bpy.context.view_layer.update();depsgraph=bpy.context.evaluated_depsgraph_get()
        sample=dict(frame=frame,actors={},key=[],camera=scene.camera.name)
        for obj in scene.objects:
            if obj.type!='MESH':continue
            evaluated=obj.evaluated_get(depsgraph);mesh=evaluated.to_mesh()
            try:
                if obj.get('director_actor'):
                    sample['actors'].setdefault(obj['director_actor'],{})[obj['director_mesh']]=[canonical(evaluated.matrix_world@v.co) for v in mesh.vertices]
                if obj.get('director_prop'):sample['key'].extend(canonical(evaluated.matrix_world@v.co) for v in mesh.vertices)
            finally:evaluated.to_mesh_clear()
        samples.append(sample)
    return dict(blender_version=bpy.app.version_string,samples=samples,meshes=sum(o.type=='MESH' for o in scene.objects),
        armatures=sum(o.type=='ARMATURE' for o in scene.objects),actions=len(bpy.data.actions),
        cameras=sum(o.type=='CAMERA' for o in scene.objects),lights=sum(o.type=='LIGHT' for o in scene.objects))


job=json.loads(Path(sys.argv[sys.argv.index('--')+1]).read_text())
if job['mode']=='create':create(job)
elif job['mode']=='reopen':bpy.ops.wm.open_mainfile(filepath=job['input'],load_ui=False,use_scripts=False)
else:raise ValueError('Unknown worker mode')
Path(job['report']).write_text(json.dumps(inspect(job)))
