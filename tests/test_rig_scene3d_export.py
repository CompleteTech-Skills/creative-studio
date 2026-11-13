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

    def test_unskinned_default_morph_is_baked_instead_of_silently_lost(self):
        import hashlib
        import numpy as np
        from rig_gltf import export_geometry
        from rig_import import load_character_model
        from rig_models import Geometry,Mesh
        from scene3d_contracts import canonical_bytes
        mesh=Mesh('morphed',np.array([[0.,0.,0.],[1,0,0],[0,0,1]]),np.array([[0,1,2]]),
            morphs={'up':{'POSITION':np.tile([0.,2.,0.],(3,1))}},morph_defaults={'up':.5})
        export_geometry(Geometry([mesh],[{'name':'default'}]),self.source/'morph.glb')
        reference=dict(path='morph.glb',sha256=hashlib.sha256((self.source/'morph.glb').read_bytes()).hexdigest())
        docs=deepcopy(self.bundle['documents']);asset=docs['triangle.json']
        asset.update(content=reference,source=reference,meshes=[reference])
        asset['resources']['bytes']=(self.source/'morph.glb').stat().st_size
        def refresh(value):
            if isinstance(value,dict):
                if 'path' in value and 'sha256' in value and value['path'] in docs:
                    value['sha256']=hashlib.sha256(canonical_bytes(docs[value['path']])).hexdigest()
                for child in value.values():refresh(child)
            elif isinstance(value,list):
                for child in value:refresh(child)
        for _ in range(8):
            for doc in docs.values():refresh(doc)
        for path,doc in docs.items():(self.source/path).write_bytes(canonical_bytes(doc))
        report=export_scene_frame(self.source,'example-scene','example-shot',0,self.root/'morphed')
        geometry=load_character_model(self.root/'morphed/scene.glb').geometry
        np.testing.assert_allclose(geometry.meshes[0].positions[:,1],[1,1,1])
        self.assertTrue(report['native_reimport']['triangle_topology_equal'])

    def test_perspective_aspect_and_nonrigid_camera_follow_owner_contract(self):
        from scene3d_contracts import canonical_bytes
        # Editing a camera on the graph keeps the selected persisted scene exact.
        graph=SceneGraph.from_bundle(self.bundle,'example-scene.json',source_root=self.source)
        scene=graph.document();next(n for n in scene['nodes'] if n['id']=='camera')['transform']['scale']=[2,1,1]
        graph.replace_document(scene,expected_revision=graph.revision)
        saved=snapshot_project(self.source,self.root/'scaled',graph,scene_path='example-scene.json')
        bundle=load_project(self.root/'scaled');scene_path=saved['active_scene']['path']
        shot_path=next(r['path'] for r in bundle['project']['shots'] if bundle['documents'][r['path']]['scene']['path']==scene_path)
        with self.assertRaises(RigError):export_scene_frame(self.root/'scaled',scene_path,shot_path,0,self.root/'scaled-output')
        self.assertFalse((self.root/'scaled-output').exists())
        export_scene_frame(self.source,'example-scene','example-shot',0,self.root/'perspective')
        definition=read_gltf(self.root/'perspective/scene.glb').json['cameras'][0]['perspective']
        self.assertEqual(definition['aspectRatio'],1.)

    def test_exact_scalar_lookdev_override_uses_canonical_primitive_mapping(self):
        import hashlib
        from test_lookdev_contracts import material
        from lookdev_contracts import canonical_bytes as material_bytes
        from scene3d_contracts import canonical_bytes
        from scene3d_materials import obj_material_bindings
        paint=material();paint['surface']['base_color']=[1.,0.,0.,1.]
        raw=material_bytes(paint);(self.source/'paint.json').write_bytes(raw)
        pin=dict(id=paint['id'],version=paint['version'],kind='material',path='paint.json',sha256=hashlib.sha256(raw).hexdigest())
        docs=deepcopy(self.bundle['documents'])
        docs['triangle.json']['material_bindings']=obj_material_bindings('triangle')
        next(n for n in docs['example-scene.json']['nodes'] if n['id']=='mesh')['material_overrides']=[dict(slot='surface',material=pin)]
        docs['project.json']['dependency_lock'].append(pin)
        def refresh(value):
            if isinstance(value,dict):
                if 'path' in value and 'sha256' in value and value['path'] in docs:
                    value['sha256']=hashlib.sha256(canonical_bytes(docs[value['path']])).hexdigest()
                for child in value.values():refresh(child)
            elif isinstance(value,list):
                for child in value:refresh(child)
        for _ in range(8):
            for doc in docs.values():refresh(doc)
        for path,doc in docs.items():(self.source/path).write_bytes(canonical_bytes(doc))
        report=export_scene_frame(self.source,'example-scene','example-shot',0,self.root/'red')
        doc=read_gltf(self.root/'red/scene.glb').json
        primitive=doc['meshes'][0]['primitives'][0]
        self.assertEqual(doc['materials'][primitive['material']]['pbrMetallicRoughness']['baseColorFactor'],[1.,0.,0.,1.])
        self.assertIn('paint.json',report['lookdev_closure']['inventory'])

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
