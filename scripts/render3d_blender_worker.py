"""Fixed Blender-only renderer for sanitized Render3D staging inputs.

This entrypoint never opens a user .blend file or executes supplied code. The
application validates source closures and generates each packed GLB derivative.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import sys

import bpy
from mathutils import Matrix, Vector


def require(value,message):
    if not value:raise ValueError(message)


def safe_input(root,relative,expected):
    require(isinstance(relative,str) and '/' not in relative and '\\' not in relative,'Invalid derivative filename')
    path=root/relative
    require(path.is_file() and not path.is_symlink(),'Missing regular derivative')
    require(hashlib.sha256(path.read_bytes()).hexdigest()==expected,'Derivative hash mismatch')
    return path


def import_asset(root,entry):
    asset=entry['asset'];path=safe_input(root,asset['path'],asset['sha256'])
    previous=set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path),merge_vertices=False,import_pack_images=True,
                              bone_heuristic='BLENDER',disable_bone_shape=True)
    imported=set(bpy.data.objects)-previous
    require(imported,'Imported derivative contained no objects')
    wrapper=bpy.data.objects.new(entry['id'],None);bpy.context.scene.collection.objects.link(wrapper)
    for obj in imported:
        obj['render3d_rest_basis']=json.dumps([list(row) for row in obj.matrix_basis])
        if obj.parent not in imported:obj.parent=wrapper
    return wrapper,list(imported)


def set_clip_time(objects,clip):
    """Use each imported native NLA action at an explicit local source time.

    This is a Blender time adapter, not a second native skeletal evaluator.
    Every character has independent NLA strip time, so two actors can reuse the
    same source action without sharing a timeline offset.
    """
    matched=False
    for obj in objects:
        if 'render3d_rest_basis' in obj:obj.matrix_basis=Matrix(json.loads(obj['render3d_rest_basis']))
        if obj.type=='ARMATURE':
            for bone in obj.pose.bones:bone.matrix_basis=Matrix.Identity(4)
        owners=[obj]
        if obj.type=='MESH' and obj.data.shape_keys:
            owners.append(obj.data.shape_keys)
            for key in obj.data.shape_keys.key_blocks:key.value=0
        for owner in owners:
            if not owner.animation_data:continue
            data=owner.animation_data;data.action=None
            for track in data.nla_tracks:
                track.mute=True;track.is_solo=False
                if clip and track.name==clip['id']:
                    require(len(track.strips)==1,'Native clip requires one imported action strip')
                    strip=track.strips[0]
                    strip.use_animated_time=True
                    strip.strip_time=clip['time']*bpy.context.scene.render.fps
                    # The fixed frame is inside the strip. Actual evaluation
                    # time is supplied above and does not use scene FPS rounding.
                    strip.frame_start=0
                    strip.frame_end=max(strip.frame_end,2)
                    track.mute=False;matched=True
    require(not clip or matched,'Requested native animation clip was not imported')


def create_camera(entry):
    data=bpy.data.cameras.new(entry['id']);obj=bpy.data.objects.new(entry['id'],data)
    bpy.context.scene.collection.objects.link(obj)
    configure_camera(data,entry['camera']);return obj


def configure_camera(data,source):
    data.clip_start=source['near'];data.clip_end=source['far']
    data.sensor_fit='VERTICAL'
    data.type='PERSP' if source['projection']=='perspective' else 'ORTHO'
    if data.type=='PERSP':data.angle=source['vertical_fov']
    else:data.ortho_scale=source['orthographic_height']


def create_light(entry):
    source=entry['light'];kind={'directional':'SUN','point':'POINT','spot':'SPOT','area':'AREA'}[source['type']]
    data=bpy.data.lights.new(entry['id'],kind);data.color=source['color'];data.energy=source['intensity']
    obj=bpy.data.objects.new(entry['id'],data);bpy.context.scene.collection.objects.link(obj);return obj


def preview_camera(objects,direction):
    bounds=[]
    deps=bpy.context.evaluated_depsgraph_get()
    for obj in objects:
        if obj.type=='MESH':
            evaluated=obj.evaluated_get(deps)
            bounds.extend(evaluated.matrix_world@Vector(corner) for corner in evaluated.bound_box)
    require(bounds,'Native preview has no geometry bounds')
    low=Vector(tuple(min(v[i] for v in bounds) for i in range(3)))
    high=Vector(tuple(max(v[i] for v in bounds) for i in range(3)))
    center=(low+high)*.5;span=max((high-low).length,.1)
    data=bpy.data.cameras.new('preview-camera');camera=bpy.data.objects.new('preview-camera',data)
    bpy.context.scene.collection.objects.link(camera)
    offset={'front':(0,-1,0),'side':(1,0,0),'back':(0,1,0),'three-quarter':(2.3,-3.8,1.1)}[direction]
    camera.location=center+Vector(offset).normalized()*span*2
    camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler()
    data.type='ORTHO';data.ortho_scale=span*1.15;data.clip_end=span*20
    for name,offset,energy in [('key',(-1,-2,3),450),('fill',(2,1,1),200)]:
        light=bpy.data.lights.new(name,'AREA');light.energy=energy*span*span;light.shape='DISK';light.size=span*2
        obj=bpy.data.objects.new(name,light);bpy.context.scene.collection.objects.link(obj)
        obj.location=center+Vector(offset)*span;obj.rotation_euler=(center-obj.location).to_track_quat('-Z','Y').to_euler()
    return camera


def main():
    path=Path(sys.argv[sys.argv.index('--')+1]).resolve();root=path.parent
    require(path.stat().st_size<=32*1024**2,'Worker job exceeds32MiB')
    job=json.loads(path.read_text())
    require(set(job)-{'quality'}=={'schema_version','mode','settings','scene','output','report'} and job['schema_version']==1 and job['mode']=='render','Unexpected worker job shape')
    require(bpy.app.version[:2]==(4,5),'Supported Blender is4.5LTS')
    require(job['output']=='../frames' and job['report']=='../worker-report.json','Unexpected worker output paths')
    quality=job.get('quality')
    if quality is not None:
        sys.path.insert(0,str(Path(__file__).parent))
        from render3d_quality import render_frame
        from render3d_quality_binding import adapter_hash
        require(adapter_hash()==quality['adapter_sha256'],'Quality helper code differs from admitted bytes')
        from rig_errors import object_hash
        require(object_hash(quality)==job['settings'].get('quality_sha256'),'Quality plan differs from request binding')
    output=root.parent/'frames'
    if quality is None:output.mkdir(exist_ok=False)
    scene=bpy.context.scene;settings=job['settings'];description=job['scene']
    bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
    scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=settings['samples'];scene.cycles.seed=settings['seed']
    scene.cycles.use_denoising=settings['denoise'];scene.render.threads_mode='FIXED';scene.render.threads=2
    scene.render.resolution_x,scene.render.resolution_y=settings['resolution'];scene.render.resolution_percentage=100
    scene.render.fps=24;scene.render.fps_base=1
    scene.render.film_transparent=settings['output']['alpha']=='straight'
    scene.render.image_settings.file_format='PNG' if settings['output']['format']=='png' else 'OPEN_EXR'
    scene.render.image_settings.color_mode='RGBA' if settings['output']['alpha']=='straight' else 'RGB'
    scene.render.image_settings.color_depth=str(settings['output']['bit_depth'])
    scene.view_settings.view_transform='Standard' if settings['view_transform']=='standard' else 'AgX'
    scene.view_settings.look='None';scene.view_settings.exposure=settings['exposure'];scene.view_settings.gamma=1
    world=bpy.data.worlds.new('render3d-world');world.use_nodes=True;scene.world=world
    objects={};groups={};all_imported=[]
    for entry in description['nodes']:
        if 'asset' in entry:
            objects[entry['id']],groups[entry['id']]=import_asset(root,entry);all_imported.extend(groups[entry['id']])
        elif entry['type']=='camera':objects[entry['id']]=create_camera(entry)
        elif entry['type']=='light':objects[entry['id']]=create_light(entry)
        else:
            obj=bpy.data.objects.new(entry['id'],None);scene.collection.objects.link(obj);objects[entry['id']]=obj
    scene.frame_set(1)
    if description['native_preview']:scene.camera=preview_camera(all_imported,description['camera'])
    else:
        require(description['camera'] in objects and objects[description['camera']].type=='CAMERA','Requested camera is unavailable')
        scene.camera=objects[description['camera']]
    rendered=[];measurements=[]
    for position,frame in enumerate(description['frames']):
        for entry in description['nodes']:
            state=entry['frames'][position];obj=objects[entry['id']]
            obj.matrix_world=Matrix(state['matrix']);obj.hide_render=not state['visible']
            for child in groups.get(entry['id'],[]):
                child.hide_render=not state['visible']
                if child.type=='MESH':child.visible_shadow=entry['cast_shadow']
            if state.get('camera'):configure_camera(obj.data,state['camera'])
            if state.get('light'):
                obj.data.color=state['light']['color'];obj.data.energy=state['light']['intensity']
            if entry['id'] in groups:set_clip_time(groups[entry['id']],state['clip'])
        background=world.node_tree.nodes['Background']
        background.inputs['Color'].default_value=(*frame['world']['background'],1)
        background.inputs['Strength'].default_value=2**frame['world']['exposure']
        scene.frame_set(1);bpy.context.view_layer.update()
        if quality is None:
            scene.render.filepath=str(output/f'{frame["index"]:06d}.{settings["output"]["format"]}')
            bpy.ops.render.render(write_still=True)
        else:
            render_frame(scene,quality['plan'],root.parent,frame['index'])
        rendered.append(frame['index'])
        deps=bpy.context.evaluated_depsgraph_get();sample={}
        for obj in all_imported:
            if obj.type!='MESH':continue
            evaluated=obj.evaluated_get(deps);mesh=evaluated.to_mesh()
            sample[obj.name]=[list(evaluated.matrix_world@mesh.vertices[i].co) for i in range(min(8,len(mesh.vertices)))]
            evaluated.to_mesh_clear()
        measurements.append(dict(index=frame['index'],world_vertices=sample))
    report=dict(version='Blender '+bpy.app.version_string,engine='cycles',device='cpu',rendered=rendered,
        geometry_vertices=sum(len(o.data.vertices) for o in all_imported if o.type=='MESH'),
        geometry_triangles=sum(len(o.data.polygons) for o in all_imported if o.type=='MESH'),
        armatures=sum(o.type=='ARMATURE' for o in all_imported),samples=measurements)
    if quality is not None:
        require(adapter_hash()==quality['adapter_sha256'],'Quality helper code changed during rendering')
        report['quality']=quality
    (root.parent/'worker-report.json').write_text(json.dumps(report,allow_nan=False,indent=2))


if __name__=='__main__':main()
