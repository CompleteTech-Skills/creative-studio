"""Construct, independently reopen and render a generated Scene3D environment.

Run heavy validation through the shared native-validation flock in coordinated runs.
The inspection image explicitly hides ceilings to expose real interior openings.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from scene3d_contracts import load_project
from scene3d_environment import create_environment, load_meshes
from scene3d_graph import SceneGraph

BLENDER_SCRIPT = r'''
import bpy, json, sys, math
from pathlib import Path
from mathutils import Matrix, Vector
root=Path(sys.argv[sys.argv.index('--')+1])
data=json.loads((root/'input.json').read_text())
bpy.ops.wm.read_factory_settings(use_empty=True)
for item in data['objects']:
    mesh=bpy.data.meshes.new(item['id']);mesh.from_pydata(item['vertices'],[],item['triangles']);mesh.update()
    obj=bpy.data.objects.new(item['id'],mesh);bpy.context.collection.objects.link(obj)
    obj.matrix_world=Matrix(item['matrix'])
    mat=bpy.data.materials.new('clay-'+item['id']);mat.diffuse_color=(.4,.58,.65,1) if 'floor' in item['id'] else (.7,.58,.4,1)
    obj.data.materials.append(mat)
# Export complete actual geometry, including ceilings, before cutaway inspection.
bpy.ops.export_scene.gltf(filepath=str(root/'environment.glb'),export_format='GLB')
expected=len(data['objects'])
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=str(root/'environment.glb'))
meshes=[o for o in bpy.context.scene.objects if o.type=='MESH']
assert len(meshes)==expected,(len(meshes),expected)
for obj in meshes:
    assert len(obj.data.polygons)>0
    if 'ceiling' in obj.name:obj.hide_render=True
camera_data=bpy.data.cameras.new('inspection');camera=bpy.data.objects.new('inspection',camera_data)
bpy.context.collection.objects.link(camera);camera.location=(13,12,16)
target=Vector((3,1,0));camera.rotation_euler=(target-camera.location).to_track_quat('-Z','Y').to_euler()
camera.data.type='ORTHO';camera.data.ortho_scale=20;bpy.context.scene.camera=camera
light_data=bpy.data.lights.new('key','AREA');light=bpy.data.objects.new('key',light_data);bpy.context.collection.objects.link(light)
light.location=(3,10,8);light.rotation_euler=(target-light.location).to_track_quat('-Z','Y').to_euler();light.data.energy=1800;light.data.size=10
scene=bpy.context.scene;scene.render.engine='CYCLES';scene.cycles.samples=16;scene.cycles.device='CPU';scene.render.threads_mode='FIXED';scene.render.threads=2
scene.world=bpy.data.worlds.new('inspection-world');scene.world.color=(.15,.15,.15);scene.render.resolution_x=640;scene.render.resolution_y=480;scene.render.resolution_percentage=100
scene.render.image_settings.file_format='PNG';scene.render.filepath=str(root/'inspection.png');bpy.ops.render.render(write_still=True)
(root/'native.json').write_text(json.dumps(dict(blender=bpy.app.version_string,meshes=len(meshes),vertices=sum(len(o.data.vertices) for o in meshes),triangles=sum(len(o.data.polygons) for o in meshes),ceiling_cutaway=True),sort_keys=True))
'''


def demonstrate(destination,blender):
    root=Path(destination).absolute();root.mkdir(parents=True,exist_ok=False)
    recipe=dict(id='workshop',version=1,seed=42,generator='workshop',parameters=dict(rooms=2,windows=True),budget=100)
    create_environment(root/'project',recipe)
    bundle=load_project(root/'project');scene_path=bundle['project']['scenes'][-1]['path']
    scene=bundle['documents'][scene_path];graph=SceneGraph(scene);meshes=load_meshes(root/'project',scene_path)
    objects=[dict(id=i,**m,matrix=graph.evaluate_base(i)['world_matrix']) for i,m in meshes.items()]
    (root/'input.json').write_text(json.dumps(dict(objects=objects),allow_nan=False))
    script=root/'native.py';script.write_text(BLENDER_SCRIPT)
    with (root/'blender.log').open('w') as log:
        subprocess.run([str(blender),'--background','--factory-startup','--threads','2','--python-exit-code','1','--python',str(script),'--',str(root)],
                       stdout=log,stderr=subprocess.STDOUT,check=True,timeout=180)
    from PIL import Image
    with Image.open(root/'inspection.png') as image:
        image.verify()
    files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir() if p.is_file()}
    result=dict(recipe=recipe,scene=scene_path,native=json.loads((root/'native.json').read_text()),sha256=files)
    (root/'receipt.json').write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path);parser.add_argument('--blender',required=True,type=Path)
    args=parser.parse_args();print(json.dumps(demonstrate(args.destination,args.blender),indent=2))
