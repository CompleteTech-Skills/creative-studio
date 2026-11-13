import copy
import json
from pathlib import Path
import tempfile
import unittest
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import numpy as np
from model3d_geometry import sphere,box
from model3d_optimization import (generate_lods,compare_meshes,repair_mesh,mesh_hash,
                                 invalidated_derivatives,export_lods)
from rig_models import Geometry
from rig_errors import RigError
from rig_import import load_character_model

class OptimizationTests(unittest.TestCase):
    def setUp(self):
        self.mesh=sphere(.5,segments=24,rings=12,name='face')
        self.mesh.name='Face';self.mesh.region='head'
        self.geometry=Geometry([self.mesh],[dict(name='clay',pbrMetallicRoughness=dict(
            baseColorFactor=[.7,.2,.1,1],metallicFactor=0.,roughnessFactor=.65))])
    def generate(self,**kwargs):
        return generate_lods(self.geometry,cell_sizes=[.08,.16],source_version='source-v1',
            regenerate_normals=True,max_distance=.15,min_silhouette_iou=.85,**kwargs)
    def test_actual_lods_material_identity_and_source_are_preserved(self):
        before=mesh_hash(self.mesh);result=self.generate()
        counts=[g.triangle_count for g in result.levels]
        self.assertEqual(counts,[528,464,254]);self.assertEqual(mesh_hash(self.mesh),before)
        for level in result.levels:
            self.assertEqual(level.materials,self.geometry.materials)
            self.assertEqual((level.meshes[0].id,level.meshes[0].name,level.meshes[0].region),('face','Face','head'))
        self.assertGreater(result.report['levels'][1]['parts'][0]['comparison']['sampled_surface_distance_max'],0)
    def test_distinct_parts_material_slots_and_attachment_vertices_survive(self):
        second=copy.deepcopy(self.mesh);second.id='body';second.name='Body';second.region='torso'
        second.material=1;second.positions=second.positions+np.array([2.,0.,0.])
        self.geometry.meshes.append(second);self.geometry.materials.append({'name':'metal'})
        result=self.generate(protected_vertices={'face':[0,1]})
        for level in result.levels:
            self.assertEqual([m.id for m in level.meshes],['face','body'])
            self.assertEqual([m.material for m in level.meshes],[0,1])
            for i in [0,1]:self.assertTrue(np.any(np.all(level.meshes[0].positions==self.mesh.positions[i],axis=1)))
    def test_metrics_compare_identity_and_known_displacement(self):
        same=compare_meshes(self.mesh,self.mesh)
        self.assertLess(same['sampled_surface_distance_max'],1e-12)
        self.assertEqual([x['iou'] for x in same['silhouettes'].values()],[1.,1.,1.])
        shifted=copy.deepcopy(self.mesh);shifted.positions[:,0]+=3
        self.assertGreater(compare_meshes(self.mesh,shifted)['sampled_surface_distance_max'],2)
    def test_quality_and_budget_failures_leave_source_intact(self):
        before=mesh_hash(self.mesh)
        for options in [dict(budgets={'triangles':100}),dict(per_part={'face':{'vertices':10}})]:
            with self.assertRaises(RigError):self.generate(**options)
        with self.assertRaises(RigError):generate_lods(self.geometry,cell_sizes=[.08,.16],source_version='v',regenerate_normals=True,max_distance=0)
        self.assertEqual(mesh_hash(self.mesh),before)
    def test_attribute_loss_and_no_reduction_reject(self):
        self.mesh.attributes['TEXCOORD_0']=np.zeros((len(self.mesh.positions),2))
        with self.assertRaises(RigError):self.generate()
        del self.mesh.attributes['TEXCOORD_0']
        with self.assertRaises(RigError):generate_lods(self.geometry,cell_sizes=[.001,.002],source_version='v',regenerate_normals=True)
    def test_copy_repair_and_selective_dependency_invalidation(self):
        other=box(name='unrelated');g=copy.deepcopy(self.geometry);g.meshes.append(other)
        deps={'face-preview':{'face':mesh_hash(g.meshes[0])},'other-preview':{'unrelated':mesh_hash(other)}}
        changed=copy.deepcopy(g);changed.meshes[0].positions*=1.1
        self.assertEqual(invalidated_derivatives(g,changed,deps),['face-preview'])
        m=copy.deepcopy(self.mesh);m.triangles=np.concatenate([m.triangles,m.triangles[:1]])
        fixed,report=repair_mesh(m,policy={'remove_duplicate_faces':True})
        self.assertEqual(len(fixed.triangles)+1,len(m.triangles));self.assertEqual(report['after']['duplicate_faces'],0)
    def test_all_real_export_levels_independently_reimport_and_exclusive_publication(self):
        result=self.generate()
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'lods';export_lods(result,root)
            for i,level in enumerate(result.levels):
                geometry=load_character_model(root/f'lod{i}'/'model.glb').geometry
                self.assertEqual(geometry.triangle_count,level.triangle_count)
                self.assertEqual(len(geometry.meshes),1)
                np.testing.assert_allclose(geometry.meshes[0].positions.min(axis=0),level.meshes[0].positions.min(axis=0),atol=1e-6)
            before=(root/'lods.json').read_bytes()
            with self.assertRaises(Exception):export_lods(result,root)
            self.assertEqual((root/'lods.json').read_bytes(),before)
    def test_pinned_feature_vertices_stay_exact_and_measured_export_rejects_tamper(self):
        from model3d_optimization_remesh import vertex_cluster_remesh
        m=copy.deepcopy(self.mesh);m.attributes={}
        reduced=vertex_cluster_remesh(m,cell_size=.16,source_version='v',protected_vertices=[0,1])
        for vertex in [0,1]:
            self.assertTrue(np.any(np.all(reduced.mesh.positions==m.positions[vertex],axis=1)))
        result=self.generate();result.levels[1].meshes[0].positions[0,0]+=.1
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(RigError):export_lods(result,Path(temp)/'tampered')
            self.assertFalse((Path(temp)/'tampered').exists())
    def test_request_work_budgets_and_invalid_policies(self):
        for kwargs in [dict(samples=True),dict(resolution=1000),dict(pair_budget=1),dict(pixel_budget=1)]:
            with self.assertRaises(RigError):compare_meshes(self.mesh,self.mesh,**kwargs)
        with self.assertRaises(RigError):repair_mesh(self.mesh,policy={'orient':'yes'})
        with self.assertRaises(RigError):self.generate(budgets={'bogus':4})
        for options in [dict(cell_sizes=[1,10**1000]),dict(cell_sizes=[.08,.16],max_distance=10**1000)]:
            with self.assertRaises(RigError):generate_lods(self.geometry,source_version='v',**options)

if __name__=='__main__':unittest.main()
