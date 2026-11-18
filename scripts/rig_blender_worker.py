"""Trusted Blender-only worker. Input JSON selects data operations, never code."""
from __future__ import annotations
import json
import math
from pathlib import Path
import sys
import bpy
from mathutils import Vector, Matrix


def canonical(point):return [float(point.x),float(point.z),float(-point.y)]
def blender(point):return Vector((point[0],-point[2],point[1]))


def activate_clip(name):
    matched=[]
    for obj in bpy.data.objects:
        if obj.get('hatch_control'):continue
        for owner in (obj,getattr(obj.data,'shape_keys',None) if obj.type=='MESH' else None):
            if owner is None or owner.animation_data is None:continue
            data=owner.animation_data;data.action=None
            for track in data.nla_tracks:
                track.is_solo=False;track.mute=True
                if track.name==name and track.strips:
                    data.action=track.strips[0].action;data.action_slot=track.strips[0].action_slot
                    matched.append(obj.name)
        if obj.get('hatch_rest_basis'):obj.matrix_basis=Matrix(json.loads(obj['hatch_rest_basis']))
        if obj.type=='ARMATURE':
            for bone in obj.pose.bones:bone.matrix_basis=Matrix.Identity(4)
        if obj.type=='MESH' and obj.data.shape_keys:
            for key in obj.data.shape_keys.key_blocks:key.value=0.
    return matched


def inspect_scene(job):
    armatures=[o for o in bpy.data.objects if o.type=='ARMATURE']
    meshes=[o for o in bpy.data.objects if o.type=='MESH']
    report=dict(blender_version=bpy.app.version_string,meshes=len(meshes),vertices=sum(len(o.data.vertices) for o in meshes),
        armatures=len(armatures),bones=sum(len(o.data.bones) for o in armatures),
        armature_modifiers=sum(sum(m.type=='ARMATURE' for m in o.modifiers) for o in meshes),
        vertex_groups=sum(len(o.vertex_groups) for o in meshes),
        shape_keys={o.name:[k.name for k in o.data.shape_keys.key_blocks] for o in meshes if o.data.shape_keys},
        actions=[dict(name=a.name,slots=[s.identifier for s in a.slots]) for a in bpy.data.actions],
        nla={o.name:[t.name for t in o.animation_data.nla_tracks] for o in bpy.data.objects if o.animation_data},
        controls=[o.name for o in bpy.data.objects if o.get('hatch_control')],
        constraints=sum(len(b.constraints) for a in armatures for b in a.pose.bones),samples=[])
    for request in job.get('samples',[]):
        matched=activate_clip(request['clip'])
        frame=request['time']*bpy.context.scene.render.fps
        bpy.context.scene.frame_set(math.floor(frame),subframe=frame%1)
        bpy.context.view_layer.update();depsgraph=bpy.context.evaluated_depsgraph_get()
        sample=dict(clip=request['clip'],time=request['time'],matched=matched,meshes={},bones={})
        for obj in meshes:
            evaluated=obj.evaluated_get(depsgraph);mesh=evaluated.to_mesh()
            indices=request.get('vertices',{}).get(obj.name)
            if indices is not None:
                sample['meshes'][obj.name]=[canonical(evaluated.matrix_world@mesh.vertices[i].co) for i in indices]
            evaluated.to_mesh_clear()
        for arm in armatures:
            for name in request.get('bones',[]):
                if name in arm.pose.bones:sample['bones'][name]=canonical(arm.matrix_world@arm.pose.bones[name].head)
        report['samples'].append(sample)
    return report


def add_authoring_controls(manifest):
    armatures=[o for o in bpy.data.objects if o.type=='ARMATURE']
    if not armatures:raise ValueError('Imported scene has no editable armature')
    arm=armatures[0];arm.show_in_front=True;arm.data.display_type='OCTAHEDRAL'
    arm['hatch_live_ik']=False
    arm['hatch_control_help']='Baked animation is active by default. Enable hatch_live_ik to edit target/pole empties; disable it for exact exported playback.'
    for source in manifest['skeleton']['bones']:
        if source['id'] in arm.data.bones:
            bone=arm.data.bones[source['id']];bone['hatch_role']=source['role'];bone['hatch_bone_id']=source['id'];bone.use_deform=source['deform']
    collection=bpy.data.collections.new('Hatch editable controls');bpy.context.scene.collection.children.link(collection)
    by_id={b['id']:b for b in manifest['skeleton']['bones']}
    for chain in manifest['controllers']['ik']:
        target=bpy.data.objects.new('IK '+chain['id'],None);target.empty_display_type='CUBE';target.empty_display_size=.045
        target.location=blender(by_id[chain['bones'][-1]]['head']);target['hatch_control']='ik-target';collection.objects.link(target)
        pole=bpy.data.objects.new('Pole '+chain['id'],None);pole.empty_display_type='SPHERE';pole.empty_display_size=.025
        pole.location=blender(chain['pole']);pole['hatch_control']='ik-pole';collection.objects.link(pole)
        bone=arm.pose.bones.get(chain['bones'][-2])
        if bone is None:continue
        constraint=bone.constraints.new('IK');constraint.name='Hatch '+chain['id'];constraint.target=target;constraint.pole_target=pole
        constraint.chain_count=len(chain['bones'])-1;constraint.iterations=chain['iterations'];constraint.use_stretch=False;constraint.influence=0.
        curve=constraint.driver_add('influence');driver=curve.driver;driver.type='AVERAGE'
        variable=driver.variables.new();variable.name='live';variable.type='SINGLE_PROP'
        variable.targets[0].id=arm;variable.targets[0].data_path='["hatch_live_ik"]'
    for socket in manifest['attachments']:
        obj=bpy.data.objects.new('Socket '+socket['id'],None);obj.empty_display_type='ARROWS';obj.empty_display_size=.055
        obj['hatch_control']='attachment';obj['hatch_owner']=socket['owner'];obj['hatch_definition']=json.dumps(socket)
        collection.objects.link(obj)
        # Bone parenting exposes an editable, owned socket. Exact canonical TRS remains in the manifest.
        obj.parent=arm;obj.parent_type='BONE';obj.parent_bone=socket['bone']
        obj.location=Vector(socket['transform']['translation'])
    text=bpy.data.texts.new('HATCH_RIG_MANIFEST.json');text.write(json.dumps(manifest,indent=2))
    info=bpy.data.texts.new('HATCH_README.txt')
    info.write('This is a real editable armature with vertex groups, skin modifiers, shape keys and named NLA actions.\n'
        'Play a named NLA track; the default controls are disabled to preserve baked motion. Enable armature hatch_live_ik to edit IK targets.\n'
        'Native constraints, contact intervals, canonical socket transforms and provenance are retained in HATCH_RIG_MANIFEST.json.\n'
        'Native constraint types that are not translated to Blender must be edited through Hatch, then rebaked.\n')


def export_glb(path,*,animations=True):
    bpy.ops.export_scene.gltf(filepath=str(path),export_format='GLB',export_yup=True,export_skins=True,
        export_morph=True,export_morph_animation=True,export_animations=animations,export_animation_mode='NLA_TRACKS',
        export_frame_range=False,export_frame_step=1,export_optimize_animation_size=False,export_extras=True,
        export_force_sampling=True,export_all_influences=True)


def render_scene(job):
    scene=bpy.context.scene
    scene.render.engine='CYCLES';scene.cycles.device='CPU';scene.cycles.samples=16
    scene.render.threads_mode='FIXED';scene.render.threads=2
    scene.render.resolution_x=job['width'];scene.render.resolution_y=job['height'];scene.render.resolution_percentage=100
    scene.render.image_settings.file_format='PNG';scene.render.image_settings.color_mode='RGBA';scene.render.film_transparent=True
    scene.world=bpy.data.worlds.new('Hatch preview world');scene.world.use_nodes=True
    scene.world.node_tree.nodes['Background'].inputs['Color'].default_value=(.55,.65,.8,1)
    scene.world.node_tree.nodes['Background'].inputs['Strength'].default_value=.35
    if not activate_clip(job['clip']):raise ValueError('Requested render action is unavailable')
    bounds=[]
    for request in job['frames']:
        frame=request['time']*scene.render.fps;scene.frame_set(math.floor(frame),subframe=frame%1)
        depsgraph=bpy.context.evaluated_depsgraph_get()
        for obj in scene.objects:
            if obj.type=='MESH':
                evaluated=obj.evaluated_get(depsgraph)
                bounds.extend(evaluated.matrix_world@Vector(corner) for corner in evaluated.bound_box)
    if not bounds:raise ValueError('Rendering requires evaluated mesh bounds')
    low=Vector(tuple(min(v[i] for v in bounds) for i in range(3)));high=Vector(tuple(max(v[i] for v in bounds) for i in range(3)))
    center=(low+high)*.5;span=max((high-low).length,.1)
    camera=bpy.data.objects.new('Hatch preview camera',bpy.data.cameras.new('Hatch preview camera'))
    scene.collection.objects.link(camera);scene.camera=camera
    direction={'front':(0,-1,0),'side':(1,0,0),'back':(0,1,0),'three-quarter':(2.3,-3.8,1.1)}[job['camera']]
    camera.location=center+Vector(direction).normalized()*span*2
    camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler()
    camera.data.type='ORTHO';camera.data.ortho_scale=span*1.15;camera.data.clip_end=span*20
    for name,offset,energy in [('Key',(-1,-2,3),450),('Fill',(2,1,1),200)]:
        light=bpy.data.objects.new('Hatch '+name,bpy.data.lights.new('Hatch '+name,'AREA'))
        scene.collection.objects.link(light);light.location=center+Vector(offset)*span
        light.rotation_euler=(center-light.location).to_track_quat('-Z','Y').to_euler()
        light.data.energy=energy*span*span;light.data.shape='DISK';light.data.size=span*2
    for request in job['frames']:
        frame=request['time']*scene.render.fps;scene.frame_set(math.floor(frame),subframe=frame%1)
        scene.render.filepath=request['path'];bpy.ops.render.render(write_still=True)
    return dict(engine='Cycles CPU',samples=16,frames=job['frames'],camera=job['camera'],transparent=True,
                width=job['width'],height=job['height'])


def main():
    job_path=Path(sys.argv[sys.argv.index('--')+1]);job=json.loads(job_path.read_text())
    if bpy.app.version[:2]!=(4,5):raise ValueError('Supported Blender version is 4.5 LTS')
    mode=job['mode']
    if mode=='create':
        bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
        bpy.context.scene.render.fps=job.get('fps',24);bpy.context.scene.frame_start=0
        bpy.ops.import_scene.gltf(filepath=job['input'],merge_vertices=False,import_pack_images=True,bone_heuristic='BLENDER',disable_bone_shape=True)
        for obj in bpy.data.objects:obj['hatch_rest_basis']=json.dumps([list(row) for row in obj.matrix_basis])
        manifest=json.loads(Path(job['manifest']).read_text());add_authoring_controls(manifest)
        bpy.context.scene.frame_start=0;bpy.context.scene.frame_end=round(job.get('duration',2)*bpy.context.scene.render.fps)
        activate_clip('neutral');bpy.context.scene.frame_set(0)
        bpy.ops.wm.save_as_mainfile(filepath=job['output'],compress=True)
    elif mode in ('reopen','import','render'):
        bpy.ops.wm.open_mainfile(filepath=job['input'],load_ui=False,use_scripts=False)
        if mode=='import':
            if bpy.data.libraries:raise ValueError('Make linked Blender libraries local before portable import.')
            if any(image.source=='FILE' and not image.packed_file for image in bpy.data.images):
                raise ValueError('Pack all external texture images into the Blender file before portable import.')
    else:raise ValueError('Unsupported fixed worker mode')
    report=inspect_scene(job)
    if mode=='render':report['render']=render_scene(job)
    if job.get('export'):
        # Export one track group per clip; all tracks must be enabled for the exporter inventory.
        for obj in bpy.data.objects:
            for owner in (obj,getattr(obj.data,'shape_keys',None) if obj.type=='MESH' else None):
                if owner and owner.animation_data:
                    owner.animation_data.action=None
                    for track in owner.animation_data.nla_tracks:track.mute=False;track.is_solo=False
        export_glb(job['export'])
    report['ok']=report['armatures']>0 and report['armature_modifiers']>0 and report['vertex_groups']>0
    if mode=='import':report['ok']=report['meshes']>0
    Path(job['report']).write_text(json.dumps(report,indent=2))


if __name__=='__main__':main()
