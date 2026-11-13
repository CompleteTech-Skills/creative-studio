"""Immutable timeline document promotions through the native snapshot owner."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from rig_errors import RigError
from scene3d_contracts import load_project
from scene3d_example import build_example
from scene3d_snapshot import snapshot_project


class SceneDocumentSnapshots(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root/'source'
        build_example(self.source)
        self.bundle = load_project(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_key_edit_promotes_shot_and_preserves_original(self):
        shot=deepcopy(self.bundle['documents']['example-shot.json'])
        shot['tracks']=[dict(target='mesh',property='translation',interpolation='linear',
            keys=[dict(frame=0,value=[0,0,0]),dict(frame=24,value=[4,0,0])])]
        original=(self.source/'example-shot.json').read_bytes()
        receipt=snapshot_project(self.source,self.root/'out',
            document_replacements={'example-shot.json':shot})
        reopened=load_project(self.root/'out')
        ref=reopened['project']['shots'][0]
        self.assertNotEqual(ref['path'],'example-shot.json')
        self.assertEqual(reopened['documents'][ref['path']]['tracks'],shot['tracks'])
        self.assertEqual((self.root/'out/example-shot.json').read_bytes(),original)
        self.assertEqual((self.source/'example-shot.json').read_bytes(),original)
        self.assertEqual(receipt['status'],'complete-byte-and-contract-verified')

    def test_scene_promotion_transitively_rebinds_shot(self):
        scene=deepcopy(self.bundle['documents']['example-scene.json']);scene['name']='Edited scene'
        receipt=snapshot_project(self.source,self.root/'out',scene_path='example-scene.json',
            document_replacements={'example-scene.json':scene})
        reopened=load_project(self.root/'out');project=reopened['project']
        self.assertEqual(project['scenes'][0],receipt['active_scene'])
        shot=reopened['documents'][project['shots'][0]['path']]
        self.assertEqual(shot['scene'],project['scenes'][0])
        self.assertEqual(reopened['documents'][project['scenes'][0]['path']]['name'],'Edited scene')

    def test_explicit_reorder_uses_registered_paths_and_rejects_injection(self):
        with self.assertRaises(RigError):
            snapshot_project(self.source,self.root/'out',project_selection={'shots':[]})
        self.assertFalse((self.root/'out').exists())

    def test_add_and_order_shots_rebinds_their_new_scene(self):
        scene=deepcopy(self.bundle['documents']['example-scene.json'])
        scene['shots'].append('second-shot')
        shot=deepcopy(self.bundle['documents']['example-shot.json']);shot['id']='second-shot'
        path='versions/second-shot/1.0.0.json'
        snapshot_project(self.source,self.root/'out',
            document_replacements={'example-scene.json':scene},
            derived_documents={path:shot},
            project_selection={'shots':[path,'example-shot.json']})
        reopened=load_project(self.root/'out');project=reopened['project']
        self.assertEqual([r['id'] for r in project['shots']],['second-shot','example-shot'])
        for reference in project['shots']:
            self.assertEqual(reopened['documents'][reference['path']]['scene'],project['scenes'][0])

    def test_graph_and_explicit_shot_edit_coalesce_one_promotion(self):
        from scene3d_graph import SceneGraph
        graph=SceneGraph(self.bundle['documents']['example-scene.json'])
        shot=deepcopy(self.bundle['documents']['example-shot.json'])
        shot['tracks']=[dict(target='mesh',property='translation',interpolation='step',keys=[dict(frame=0,value=[8,0,0])])]
        receipt=snapshot_project(self.source,self.root/'out',graph,scene_path='example-scene.json',
            document_replacements={'example-shot.json':shot})
        reopened=load_project(self.root/'out')
        selected=reopened['project']['shots']
        self.assertEqual(len(selected),2)
        edited=next(ref for ref in selected if ref['version']!='1.0.0')
        self.assertEqual(reopened['documents'][edited['path']]['scene'],receipt['active_scene'])
        self.assertEqual(reopened['documents'][edited['path']]['tracks'],shot['tracks'])

    def test_invalid_identity_and_unknown_project_fields_preserve_destination(self):
        shot=deepcopy(self.bundle['documents']['example-shot.json']);shot['id']='other'
        for kwargs in ({'document_replacements':{'example-shot.json':shot}},
                       {'project_selection':{'budgets':[]}},
                       {'project_selection':{'shots':['missing.json']}},
                       {'document_replacements':[]}):
            with self.subTest(kwargs=kwargs),self.assertRaises(RigError):
                snapshot_project(self.source,self.root/'out',**kwargs)
            self.assertFalse((self.root/'out').exists())


if __name__=='__main__':unittest.main()
