"""Real native source closure tests for the Director Scene3D adapter."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np

from director_scene_binding import bind_scene_project
from rig_demo import create_demo
from rig_animation import clip_from_poses,evaluate_animation
from rig_skeleton import neutral_pose,evaluate_fk
from rig_gltf import export_gltf, export_geometry
from rig_jobs import save_rig
from rig_models import load_rig
from scene3d_contracts import load_project
from scene3d_example import node

ROOT=Path(__file__).resolve().parents[1]


def fixture(root):
    (root/'assets').mkdir()
    for actor in ('aster','moss'):
        create_demo(root/'assets'/('rig-'+actor),'humanoid',identifier=actor,fps=4,animate=False)
        rig=load_rig(root/'assets'/('rig-'+actor))
        for suffix,duration in [('investigation',5),('establish',2.5),('discovery',2.5)]:
            pose=neutral_pose(rig.skeleton)
            clip=clip_from_poses(rig,actor+'-'+suffix,[0.,duration],[pose,pose],fps=24)
            rig.clips[clip['id']]=clip
        save_rig(rig)
        export_gltf(rig,root/(actor+'-animated.glb'))
        if actor=='aster':
            export_geometry(rig.geometry,root/'assets/laboratory.glb')
            export_geometry(rig.geometry,root/'assets/access-key.glb')
    scene=json.loads((ROOT/'examples/scene3d-contracts/example-scene.json').read_text())
    scene.update(id='laboratory',dependencies=[],shots=['establish','discovery'])
    camera=next(n for n in scene['nodes'] if n['type']=='camera')
    wide=deepcopy(camera);wide.update(id='wide',name='wide')
    close=deepcopy(camera);close.update(id='investigation',name='investigation')
    scene.update(nodes=[node('root'),node('aster',parent='root'),node('moss',parent='root'),wide,close],cameras=['wide','investigation'])
    (root/'scene.json').write_text(json.dumps(scene))
    rig=load_rig(root/'assets/rig-aster')
    matrix=evaluate_fk(rig.skeleton,evaluate_animation(rig,'aster-investigation',3.,contacts=False,constraints=False)['pose'])['hand.right'].tolist()
    return [dict(frame=i,matrix=matrix) for i in range(121)]


class SceneBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary=tempfile.TemporaryDirectory()
        cls.root=Path(cls.temporary.name)
        cls.source=cls.root/'source';cls.source.mkdir()
        cls.frames=fixture(cls.source)

    @classmethod
    def tearDownClass(cls):cls.temporary.cleanup()

    def test_actual_native_project_closure_and_two_local_shots(self):
        target=self.root/'bound'
        report=bind_scene_project(self.source,target,self.frames)
        self.assertEqual(report['status'],'hash-and-contract-verified')
        bundle=load_project(target)
        self.assertEqual(len(bundle['project']['characters']),2)
        for name in ('establish','discovery'):
            shot=bundle['documents'][name+'-shot.json']
            self.assertEqual(shot['duration_frames'],60)
            self.assertEqual(len(shot['bindings']),3)
            self.assertEqual(shot['bindings'][0]['clip']['id'],'aster-'+name)
        relocation=self.root/'relocated';shutil.copytree(target,relocation)
        self.assertEqual(load_project(relocation)['inventory'],bundle['inventory'])
        rigfile=relocation/'native/aster/rig.json'
        rigfile.write_bytes(rigfile.read_bytes()+b' ')
        with self.assertRaises(ValueError):load_project(relocation)

    def test_rejects_missing_source_clip_and_keeps_destination_absent(self):
        source=self.root/'missing';shutil.copytree(self.source,source)
        rig=load_rig(source/'assets/rig-aster');del rig.clips['aster-discovery'];save_rig(rig)
        target=self.root/'no-publication'
        with self.assertRaisesRegex(ValueError,'shot clips'):
            bind_scene_project(source,target,self.frames)
        self.assertFalse(target.exists())

    def test_rejects_invalid_matrix_and_racing_existing_destination(self):
        target=self.root/'occupied';target.mkdir();(target/'mine').write_text('preserve')
        with self.assertRaises(ValueError):bind_scene_project(self.source,target,self.frames)
        self.assertEqual((target/'mine').read_text(),'preserve')
        frames=deepcopy(self.frames);frames[24]['matrix'][0][0]=float('nan')
        with self.assertRaises(ValueError):bind_scene_project(self.source,self.root/'invalid',frames)
        self.assertFalse((self.root/'invalid').exists())

    def test_rejects_key_motion_that_is_not_the_actual_native_hand(self):
        frames=deepcopy(self.frames)
        frames[80]['matrix'][0][3]+=1.0
        target=self.root/'wrong-key-motion'
        with self.assertRaisesRegex(ValueError,'native hand.right'):
            bind_scene_project(self.source,target,frames)
        self.assertFalse(target.exists())
