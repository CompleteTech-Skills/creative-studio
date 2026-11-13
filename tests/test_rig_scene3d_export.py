"""Real GLB static-frame export, independent validation and isolated reopen."""
from copy import deepcopy
from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

from rig_errors import RigError
from rig_gltf import read_gltf
from scene3d_contracts import load_project
from scene3d_example import build_example
from scene3d_export import export_scene_frame
from scene3d_graph import SceneGraph
from scene3d_snapshot import snapshot_project


class SceneExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source'
        self.bundle=build_example(self.source)

    def test_actual_triangle_and_camera_hierarchy_roundtrip(self):
        before=deepcopy(self.bundle['inventory'])
        report=export_scene_frame(self.source,'example-scene.json','example-shot.json',Fraction(1,2),self.root/'export')
        self.assertTrue(report['khronos']['ok']);self.assertTrue(report['native_reimport']['ok'])
        self.assertLess(report['native_reimport']['max_world_vertex_error_m'],1e-6)
        self.assertEqual((report['vertices'],report['triangles'],report['meshes']),(3,1,1))
        doc=read_gltf(self.root/'export/scene.glb').json
        self.assertEqual(len(doc['cameras']),1)
        self.assertTrue(any(n.get('extras',{}).get('hatchSceneNode')=='camera' for n in doc['nodes']))
        self.assertEqual(load_project(self.source)['inventory'],before)
        self.assertTrue(report['losses']);self.assertFalse(report['production_approved'])

    def test_new_destination_exclusive_and_frame_rejection(self):
        occupied=self.root/'occupied';occupied.mkdir();(occupied/'mine').write_text('preserve')
        for destination,frame in [(occupied,0),(self.root/'bad-frame',24),(self.root/'nonexact',0.5)]:
            with self.subTest(destination=destination),self.assertRaises(RigError):
                export_scene_frame(self.source,'example-scene','example-shot',frame,destination)
        self.assertEqual((occupied/'mine').read_text(),'preserve')
        self.assertFalse((self.root/'bad-frame').exists())

    def test_hidden_geometry_rejects_empty_export_without_publication(self):
        graph=SceneGraph.from_bundle(self.bundle,'example-scene.json',source_root=self.source)
        scene=graph.document();next(n for n in scene['nodes'] if n['id']=='mesh')['render']['visible']=False
        graph.replace_document(scene,expected_revision=graph.revision)
        result=snapshot_project(self.source,self.root/'hidden',graph,scene_path='example-scene.json')
        bundle=load_project(self.root/'hidden');scene_path=result['active_scene']['path']
        shot_path=next(r['path'] for r in bundle['project']['shots'] if bundle['documents'][r['path']]['scene']['path']==scene_path)
        with self.assertRaises(RigError):export_scene_frame(self.root/'hidden',scene_path,shot_path,0,self.root/'export')
        self.assertFalse((self.root/'export').exists())


if __name__=='__main__':unittest.main()
