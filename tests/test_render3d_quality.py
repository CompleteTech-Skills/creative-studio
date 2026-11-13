"""Real Blender pass/precision acceptance, separate from legacy 2D usage."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from render3d_contracts import Render3DError
from render3d_quality import output_plan, _validated_plan


def settings():
    return dict(engine='cycles',device_policy='cpu',device=None,allow_cpu_fallback=False,
        resolution=[64,64],samples=2,seed=17,denoise=False,
        output=dict(format='png',bit_depth=8,passes=['combined'],alpha='straight',color_space='srgb'),
        exposure=0,view_transform='standard',resources=dict(memory_bytes=1024**3,
        storage_bytes=1024**3,timeout_seconds=120,workers=1,max_frames=10))


class QualityContracts(unittest.TestCase):
    def test_output_policy_rejects_inconsistent_or_unimplemented_settings(self):
        for changes in (dict(format='png',bit_depth=32),dict(format='exr',color_space='srgb'),
                        dict(passes=['combined','normal'])):
            value=settings();value['output'].update(changes)
            with self.subTest(changes=changes),self.assertRaises(Render3DError):output_plan(value)
        value=settings();value['output'].update(format='exr',bit_depth=16,color_space='linear',passes=['combined','depth'])
        with self.assertRaises(Render3DError):output_plan(value)
        value['output']['passes']=['combined','albedo']
        plan=output_plan(value);plan['passes'].append('normal')
        with self.assertRaises(Render3DError):_validated_plan(plan)
        value=settings();value['engine']='eevee'
        with self.assertRaises(Render3DError):output_plan(value)
        value=settings();value['samples']=True
        with self.assertRaises(Render3DError):output_plan(value)

    def test_import_does_not_require_bpy(self):
        result=subprocess.run([sys.executable,'-c',
            "import sys;sys.path.insert(0,'scripts');import render3d_quality;assert 'bpy' not in sys.modules"],
            cwd=ROOT,capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


WORKER=r'''
import bpy,json,sys
from pathlib import Path
from mathutils import Vector
root=Path(sys.argv[sys.argv.index('--')+1]);sys.path.insert(0,sys.argv[sys.argv.index('--')+2])
from render3d_quality import render_frame,configure_output
from render3d_contracts import Render3DError
bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
scene=bpy.context.scene
scene.render.threads_mode='FIXED';scene.render.threads=2
bpy.ops.mesh.primitive_uv_sphere_add(segments=16,ring_count=8,radius=.7)
sphere=bpy.context.object
material=bpy.data.materials.new('HDR engineering fixture');material.use_nodes=True
shader=material.node_tree.nodes.get('Principled BSDF');shader.inputs['Base Color'].default_value=(.5,.1,.02,1)
shader.inputs['Emission Color'].default_value=(5,1,.2,1);shader.inputs['Emission Strength'].default_value=2
sphere.data.materials.append(material)
bpy.ops.mesh.primitive_plane_add(size=2.5,location=(0,0,-.7))
plane=bpy.context.object
floor=bpy.data.materials.new('Floor');floor.diffuse_color=(.4,.4,.4,1);plane.data.materials.append(floor)
light=bpy.data.lights.new('Physical area','AREA');light.energy=300;light.size=2
lamp=bpy.data.objects.new('Physical area',light);scene.collection.objects.link(lamp);lamp.location=(2,-2,4)
camera_data=bpy.data.cameras.new('Camera');camera=bpy.data.objects.new('Camera',camera_data);scene.collection.objects.link(camera)
camera.location=(3,-4,2.5);camera.rotation_euler=(Vector((0,0,0))-camera.location).to_track_quat('-Z','Y').to_euler();scene.camera=camera
scene.world.color=(.05,.05,.05)
# Exercise caller-provided scene rather than relying on active context.
scene=scene.copy();scene.name='Quality target scene'
bpy.context.scene.render.filepath=str(root/'wrong-context.png')
plans=json.loads((root/'settings.json').read_text())
scene.view_layers[0].use_pass_z=True;scene.view_layers[0].use_pass_normal=True
configure_output(scene,plans[0]);assert scene.view_layers[0].use_pass_z and scene.view_layers[0].use_pass_normal
# Disabled owner graphs are still owned data and must survive rejected calls.
scene.use_nodes=True;marker=scene.node_tree.nodes.new('CompositorNodeValue');marker.name='owner-marker';scene.use_nodes=False
before={n.name for n in scene.node_tree.nodes}
reject=root/'rejected';reject.mkdir()
try:render_frame(scene,plans[2],reject,99)
except Render3DError:pass
else:raise AssertionError('Disabled owner graph was accepted without explicit sockets')
assert {n.name for n in scene.node_tree.nodes}==before and not scene.use_nodes
scene.node_tree.nodes.clear()
# Failed setup must restore node state and must not mutate output files.
try:render_frame(scene,plans[2],reject,98,pass_sockets={})
except Render3DError:pass
else:raise AssertionError('Missing owner sockets accepted')
assert len(scene.node_tree.nodes)==0 and not scene.use_nodes
reports=[]
for index,config in enumerate(json.loads((root/'settings.json').read_text())):
 destination=root/str(index);destination.mkdir()
 reports.append(render_frame(scene,config,destination,index))
assert not (root/'wrong-context.png').exists()
(root/'report.json').write_text(json.dumps(reports))
'''


@unittest.skipUnless(os.environ.get('HATCH_RENDER3D_QUALITY_TEST')=='1','real Blender quality acceptance not enabled')
class RealQuality(unittest.TestCase):
    def test_real_precision_hdr_alpha_and_passes(self):
        from render3d_verify import verify_image
        from rig_blender import find_blender
        retained=os.environ.get('HATCH_RENDER3D_QUALITY_DIR')
        temporary=tempfile.TemporaryDirectory() if not retained else None
        root=Path(temporary.name if temporary else retained)
        root.mkdir(parents=True,exist_ok=True)
        self.assertFalse((root/'report.json').exists(),'Use a new evidence directory')
        configurations=[]
        for format,depth in [('png',8),('png',16),('exr',16),('exr',32)]:
            value=settings();value['output'].update(format=format,bit_depth=depth,
                color_space='linear' if format=='exr' else 'srgb',
                passes=(['combined','depth','normal','albedo'] if depth==32 else ['combined','albedo']) if format=='exr' else ['combined'])
            configurations.append(value)
        (root/'settings.json').write_text(json.dumps([output_plan(c) for c in configurations]));(root/'worker.py').write_text(WORKER)
        command=[find_blender(),'--background','--factory-startup','--disable-autoexec','--python-exit-code','1',
                 '--python',str(root/'worker.py'),'--',str(root),str(ROOT/'scripts')]
        with (root/'blender.log').open('wb') as log:
            process=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=240)
        self.assertEqual(process.returncode,0,(root/'blender.log').read_text()[-5000:])
        results=json.loads((root/'report.json').read_text());verified=[]
        for config,records in zip(configurations,results):
            self.assertEqual({r['pass_name'] for r in records},set(config['output']['passes']))
            for record in records:
                report=verify_image(record['path'],output=config['output'],resolution=[64,64],pass_name=record['pass_name'])
                verified.append(report)
                if record['pass_name']=='combined':
                    self.assertEqual(report['channels']['A']['minimum'],0)
                    self.assertEqual(report['channels']['A']['maximum'],1)
                    if config['output']['format']=='exr':
                        self.assertGreater(report['channels']['R']['maximum'],1)
        (root/'verified.json').write_text(json.dumps(verified,indent=2))
        if temporary:temporary.cleanup()
