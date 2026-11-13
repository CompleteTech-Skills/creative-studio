"""Application-owned Blender worker. Input is JSON, never user Python."""
import json
import math
import re
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def fail(message):
    raise RuntimeError(message)


def source_fixture():
    def marked(value, depth=0):
        if depth>16:return False
        if hasattr(value,'items'):
            return any((k=='fixture' and v is True) or (k=='hatchAssetSourceFixture' and bool(v)) or marked(v,depth+1) for k,v in value.items())
        return False
    owners=[bpy.context.scene,*bpy.data.objects,*bpy.data.meshes,*bpy.data.materials]
    return any(marked(owner) for owner in owners)


def action_curves(action):
    result={}
    for curve in action.fcurves:result[curve.as_pointer()]=curve
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                for curve in bag.fcurves:result[curve.as_pointer()]=curve
    return list(result.values())


def animation_profile():
    allowed=re.compile(r'^(?:location|rotation_quaternion|rotation_euler|scale|pose\.bones\["(?:[^"\\]|\\.)+"\]\.(?:location|rotation_quaternion|rotation_euler|scale)|key_blocks\["(?:[^"\\]|\\.)+"\]\.value)$')
    paths=set();keyframes=0
    for action in bpy.data.actions:
        for curve in action_curves(action):
            if not allowed.fullmatch(curve.data_path):fail('Unsupported native animation channel: '+curve.data_path)
            if curve.modifiers or curve.sampled_points:fail('Native curve modifiers/sampled points require a verified baking adapter.')
            paths.add(curve.data_path);keyframes+=len(curve.keyframe_points)
            if keyframes>1000000:fail('Native keyframe budget exceeded.')
            if any(not math.isfinite(float(p.co[0])) or not math.isfinite(float(p.co[1])) or abs(p.co[0])>24000 for p in curve.keyframe_points):fail('Native animation time exceeds admission.')
    return sorted(paths)


def owner_animation(owner,kind):
    data=getattr(owner,'animation_data',None)
    if data is None:return
    if data.drivers:fail('Native animation drivers are unsupported.')
    actions=[]
    if data.action:actions.append(data.action)
    for track in data.nla_tracks:
        for strip in track.strips:
            if strip.type!='CLIP' or not strip.action:fail('Unsupported nested/native NLA strip.')
            actions.append(strip.action)
    for action in actions:
        for curve in action_curves(action):
            path=curve.data_path
            if kind=='shape':accepted=path.startswith('key_blocks[') and path.endswith('.value')
            elif kind=='object':accepted=path in ('location','rotation_quaternion','rotation_euler','scale') or (getattr(owner,'type',None)=='ARMATURE' and path.startswith('pose.bones['))
            else:accepted=False
            if not accepted:fail('Unsupported native animation owner/path: '+kind+'/'+path)
            try:resolved=owner.path_resolve(path)
            except (ValueError,TypeError):fail('Native action is not bound to its admitted owner path.')
            width=len(resolved) if hasattr(resolved,'__len__') else 1
            if not 0<=curve.array_index<width:fail('Native action component is outside its admitted owner path.')


def skin_bindings():
    result=[]
    for obj in bpy.context.scene.objects:
        modifiers=[m for m in obj.modifiers if m.type=='ARMATURE']
        if len(modifiers)>1:fail('Multiple armature modifiers require a verified deformation adapter.')
        for modifier in modifiers:
            if obj.type!='MESH' or not modifier.object or modifier.object.type!='ARMATURE' or modifier.object.name not in bpy.context.scene.objects:
                fail('Armature modifier requires a local scene mesh/armature binding.')
            if not modifier.use_vertex_groups or modifier.use_bone_envelopes or modifier.use_deform_preserve_volume or modifier.use_multi_modifier or modifier.vertex_group or not modifier.show_viewport or not modifier.show_render:
                fail('Unsupported armature deformation options: only unmasked linear vertex-group skinning is admitted.')
            if modifier.object.data.pose_position!='POSE':fail('Native rest-display armatures require a verified pose adapter.')
            for bone in modifier.object.data.bones:
                if bone.bbone_segments!=1 or bone.use_envelope_multiply or bone.inherit_scale!='FULL' or not bone.use_inherit_rotation or not bone.use_local_location:
                    fail('Unsupported native bone deformation: only unsegmented conventional inherited linear bones are admitted.')
            groups={g.index:g.name for g in obj.vertex_groups}
            for vertex in obj.data.vertices:
                for item in vertex.groups:
                    bone=modifier.object.data.bones.get(groups[item.group])
                    if item.weight>0 and bone is not None and not bone.use_deform:fail('Weighted non-deforming native bones require a verified adapter.')
            result.append(dict(mesh=obj.name,armature=modifier.object.name))
    return result


def enforce_conversion_profile():
    # glTF cannot retain arbitrary Blender shader graphs or simulation state.
    # Refuse those sources before export instead of publishing plain fallback
    # materials while reporting empty loss lists.
    if len(bpy.context.scene.objects)>4096 or len(bpy.data.actions)>4096:fail('Native object/action budget exceeded.')
    animation_profile()
    skin_bindings()
    for obj in bpy.context.scene.objects:
        if obj.type not in ('MESH','ARMATURE','EMPTY'):fail('Unsupported native object type: '+obj.type)
        if obj.instance_type!='NONE':fail('Native collection/particle instancing requires a verified realization adapter.')
        owner_animation(obj,'object')
        if obj.data:owner_animation(obj.data,'unsupported-data')
        if obj.type=='MESH' and obj.data.shape_keys:
            if not obj.data.shape_keys.use_relative:
                fail('Absolute shape keys require a verified evaluated-geometry baking adapter.')
            if any(key.vertex_group for key in obj.data.shape_keys.key_blocks):
                fail('Masked shape keys require a verified evaluated-geometry baking adapter.')
            owner_animation(obj.data.shape_keys,'shape')
        if obj.rigid_body or obj.rigid_body_constraint or (obj.type=='ARMATURE' and any(b.constraints for b in obj.pose.bones)):
            fail('Native physics/pose constraints require a separately verified baking adapter.')
        if obj.constraints or any(m.type!='ARMATURE' for m in obj.modifiers):
            fail('Unsupported native constraints/modifiers require a separately verified baking adapter.')
    owner_animation(bpy.context.scene,'unsupported-scene')
    for world in bpy.data.worlds:owner_animation(world,'unsupported-world')
    for material in list(bpy.data.materials):
        if material.animation_data:fail('Animated native materials are unsupported by this conversion profile.')
        if not material.use_nodes:continue
        nodes=list(material.node_tree.nodes)
        if len(nodes)!=2 or {n.bl_idname for n in nodes}!={'ShaderNodeBsdfPrincipled','ShaderNodeOutputMaterial'}:
            fail('Unsupported native shader graph: only a simple Principled-to-Output surface is admitted.')
        links=list(material.node_tree.links)
        if len(links)!=1 or links[0].from_node.bl_idname!='ShaderNodeBsdfPrincipled' or links[0].from_socket.name!='BSDF' or links[0].to_socket.name!='Surface':
            fail('Unsupported native shader links require a separately verified baking adapter.')
        principled=next(n for n in nodes if n.bl_idname=='ShaderNodeBsdfPrincipled')
        reference_material=bpy.data.materials.new('HatchAdmissionDefaults');reference_material.use_nodes=True
        reference=reference_material.node_tree.nodes['Principled BSDF']
        try:
            for socket in principled.inputs:
                if socket.name in ('Base Color','Metallic','Roughness','Alpha','IOR'):continue
                expected=next((v for v in reference.inputs if v.identifier==socket.identifier),None)
                if expected is None:fail('Unsupported native Principled socket: '+socket.identifier)
                value=socket.default_value;baseline=expected.default_value
                try:equal=tuple(value)==tuple(baseline)
                except TypeError:equal=value==baseline
                if not equal:fail('Unsupported native Principled parameter: '+socket.name)
        finally:bpy.data.materials.remove(reference_material)
        if material.animation_data or material.node_tree.animation_data:
            fail('Animated native materials are unsupported by this conversion profile.')


def inspect_scene():
    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    return dict(fixture=source_fixture(),scene_compositing=bool(bpy.context.scene.use_nodes),skin_bindings=skin_bindings(),objects=[dict(name=o.name, type=o.type, parent=o.parent.name if o.parent else None,
                             vertices=len(o.data.vertices) if o.type == 'MESH' else 0,
                             triangles=sum(len(p.vertices)-2 for p in o.data.polygons) if o.type == 'MESH' else 0)
                         for o in bpy.context.scene.objects],
                mesh_objects=len(meshes), vertices=sum(len(o.data.vertices) for o in meshes),
                armatures=sum(o.type == 'ARMATURE' for o in bpy.context.scene.objects),
                shape_keys=sum(max(0, len(o.data.shape_keys.key_blocks)-1) if o.data.shape_keys else 0 for o in meshes),
                materials=len(bpy.data.materials), actions=[a.name for a in bpy.data.actions],
                animation_paths=sorted({f.data_path for a in bpy.data.actions for f in action_curves(a)}))


def ensure_packed():
    if bpy.data.libraries or bpy.data.texts or any(o.animation_data and o.animation_data.drivers for o in bpy.data.objects):
        fail('Trusted blend profile forbids linked libraries, text blocks and expression drivers.')
    owners=[bpy.context.scene,*bpy.data.meshes,*bpy.data.armatures,*bpy.data.shape_keys,*bpy.data.materials,*bpy.data.worlds]
    if any(getattr(o,'animation_data',None) and o.animation_data.drivers for o in owners):fail('Native datablock drivers are unsupported.')
    if bpy.data.movieclips or any(not sound.packed_file for sound in bpy.data.sounds):fail('Unpacked native movie/audio media is outside the packed mesh profile.')
    if bpy.context.scene.sequence_editor and bpy.context.scene.sequence_editor.strips:fail('Native sequencer media requires a separate verified adapter.')
    for image in bpy.data.images:
        if image.type not in ('RENDER_RESULT', 'COMPOSITING') and image.source == 'FILE' and not image.packed_file:
            fail('Trusted blend profile requires packed images.')


def preview(path):
    scene = bpy.context.scene
    # Application-owned diagnostics never execute source compositor file-output
    # nodes or replace geometry with source sequencer footage.
    scene.render.use_compositing=False;scene.render.use_sequencer=False
    world=bpy.data.worlds.new('HatchImportInspectionWorld');world.use_nodes=True
    world.node_tree.nodes['Background'].inputs['Color'].default_value=(.05,.05,.05,1.)
    world.node_tree.nodes['Background'].inputs['Strength'].default_value=1.
    scene.world=world
    meshes = [o for o in scene.objects if o.type == 'MESH']
    points = [o.matrix_world @ Vector(corner) for o in meshes for corner in o.bound_box]
    if not points:
        fail('No real mesh for preview.')
    minimum = Vector(tuple(min(p[i] for p in points) for i in range(3)))
    maximum = Vector(tuple(max(p[i] for p in points) for i in range(3)))
    center = (minimum + maximum) / 2
    radius = max((maximum-minimum).length, .1)
    data = bpy.data.cameras.new('HatchImportInspectionCamera')
    camera = bpy.data.objects.new('HatchImportInspectionCamera', data)
    scene.collection.objects.link(camera)
    camera.location = center + Vector((radius, -radius*1.6, radius*.8))
    camera.rotation_euler = (center-camera.location).to_track_quat('-Z', 'Y').to_euler()
    data.type = 'ORTHO'; data.ortho_scale = radius*1.3
    scene.camera = camera
    light_data = bpy.data.lights.new('HatchImportInspectionLight', 'AREA')
    light = bpy.data.objects.new('HatchImportInspectionLight', light_data)
    scene.collection.objects.link(light)
    light.location = center + Vector((radius, -radius, radius*2))
    light.rotation_euler = (center-light.location).to_track_quat('-Z', 'Y').to_euler()
    light_data.energy = 500; light_data.shape = 'DISK'; light_data.size = radius
    scene.render.engine = 'CYCLES'; scene.cycles.device = 'CPU'; scene.cycles.samples = 4
    scene.render.threads_mode = 'FIXED'; scene.render.threads = 2
    scene.render.resolution_x = scene.render.resolution_y = 128
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = 'PNG'; scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(camera, do_unlink=True)
    bpy.data.objects.remove(light, do_unlink=True)


def main(job):
    mode = job['mode']; source = job.get('input')
    if mode in ('gltf', 'obj'):
        bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
    if mode == 'gltf':
        bpy.ops.import_scene.gltf(filepath=source)
    elif mode == 'obj':
        bpy.ops.wm.obj_import(filepath=source, forward_axis='NEGATIVE_Z', up_axis='Y')
    elif mode == 'blend':
        bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
        ensure_packed()
    elif mode == 'generate':
        bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(use_global=False)
        p = job['parameters']
        if p['primitive'] == 'box':
            bpy.ops.mesh.primitive_cube_add(size=1)
            bpy.context.object.scale = p['dimensions']
        elif p['primitive'] == 'sphere':
            bpy.ops.mesh.primitive_uv_sphere_add(segments=16, ring_count=8, radius=p['dimensions'][0]/2)
        else: fail('Unsupported procedural primitive.')
        obj = bpy.context.object; obj.name = 'ProceduralAsset'
        material = bpy.data.materials.new('ProceduralSurface'); material.diffuse_color = tuple(p['color'])
        material.use_nodes = True
        material.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = tuple(p['color'])
        obj.data.materials.append(material)
        if p['animated']:
            obj.location.x = 0; obj.keyframe_insert(data_path='location', frame=0)
            obj.location.x = 1; obj.keyframe_insert(data_path='location', frame=24)
            bpy.context.scene.frame_start = 0; bpy.context.scene.frame_end = 24
            bpy.context.scene.render.fps = 24; bpy.context.scene.frame_set(0)
    elif mode == 'reopen':
        bpy.ops.wm.open_mainfile(filepath=source, load_ui=False, use_scripts=False)
        ensure_packed()
        result = inspect_scene()
        Path(job['report']).write_text(json.dumps(dict(result, blender_version=bpy.app.version_string)), encoding='utf-8')
        return
    else: fail('Unknown fixed worker mode.')
    if mode in ('blend','obj'):enforce_conversion_profile()
    if job.get('fixture'):bpy.context.scene['hatchAssetSourceFixture']=True
    result = inspect_scene()
    if result['vertices'] <= 0: fail('Import produced no actual geometry.')
    bpy.ops.file.pack_all()
    bpy.ops.wm.save_as_mainfile(filepath=job['blend'])
    bpy.ops.export_scene.gltf(filepath=job['glb'], export_format='GLB', export_extras=True,
                             export_animations=True, export_force_sampling=True, export_morph=True, export_skins=True, export_all_influences=True, export_cameras=True,
                             export_lights=True, export_frame_range=False)
    preview(job['preview'])
    Path(job['report']).write_text(json.dumps(dict(result, blender_version=bpy.app.version_string)), encoding='utf-8')


if __name__ == '__main__':
    args = sys.argv[sys.argv.index('--')+1:]
    if len(args) != 1: fail('Expected one application job file.')
    with open(args[0], encoding='utf-8') as stream: request = json.load(stream)
    main(request)
