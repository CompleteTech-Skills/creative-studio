"""Actual catalog geometry, deterministic identities and portable project closure."""
from pathlib import Path
import tempfile
import unittest
import numpy as np

from rig_errors import RigError
from rig_math import qmatrix
from scene3d_catalog import PRESETS,catalog
from scene3d_environment import plan,create_environment,load_meshes
from scene3d_contracts import load_project


def recipe(preset,**parameters):
    return dict(id='catalog',version=1,seed=7,generator='template',parameters=dict(preset=preset,**parameters),budget=128)


class CatalogTests(unittest.TestCase):
    def test_all_declared_templates_emit_actual_bounded_meshes(self):
        self.assertEqual(len(catalog()),21)
        for preset in PRESETS:
            with self.subTest(preset=preset):
                objects=plan(recipe(preset))
                self.assertGreater(len(objects),3)
                self.assertEqual(len({o['id'] for o in objects}),len(objects))
                self.assertLessEqual(len(objects),128)
                for item in objects:
                    item['mesh'].validate()
                    rotation=qmatrix(item['rotation']) if item['rotation'] is not None else np.eye(3)
                    points=np.asarray(item['mesh'].vertices)@rotation.T+item['position']
                    self.assertTrue((points>=[-40,-5,-40]).all())
                    self.assertTrue((points<=[40,30,40]).all())
                    self.assertTrue(item['mesh'].obj().startswith(b'# Scene3D'))

    def test_catalog_is_deterministic_and_uniform_scale_is_explicit(self):
        a=plan(recipe('village'));b=plan(recipe('village'));scaled=plan(recipe('village',scale=2))
        self.assertEqual([o['id'] for o in a],[o['id'] for o in b])
        for x,y,z in zip(a,b,scaled):
            self.assertEqual(x['mesh'].obj(),y['mesh'].obj())
            np.testing.assert_allclose(z['mesh'].vertices,np.asarray(x['mesh'].vertices)*2)
            np.testing.assert_allclose(z['position'],np.asarray(x['position'])*2)
        self.assertEqual([o['mesh'].obj() for o in plan(recipe('village',detail=12.0))],[o['mesh'].obj() for o in a])
        low=plan(recipe('alien-landscape',detail=6));high=plan(recipe('alien-landscape',detail=24))
        self.assertLess(sum(len(o['mesh'].triangles) for o in low),sum(len(o['mesh'].triangles) for o in high))
        original=catalog();original[0]['input_schema']['properties']['scale']['maximum']=999
        self.assertEqual(catalog()[0]['input_schema']['properties']['scale']['maximum'],10)

    def test_modeled_cavern_is_hollow_and_not_a_heightfield_claim(self):
        cave=next(o for o in plan(recipe('cavern')) if 'modeled-cave' in o['id'])
        # Hollow pipe leaves every vertex away from its axis, including real open annular ends.
        vertices=np.asarray(cave['mesh'].vertices)
        radii=np.hypot(vertices[:,0],vertices[:,2])
        self.assertGreaterEqual(radii.min(),5-1e-8)
        self.assertGreater(radii.max(),5.9)
        self.assertAlmostEqual(np.ptp(vertices[:,1]),24)
        caps=[vertices[face] for face in cave['mesh'].triangles if np.ptp(vertices[face][:,1])<1e-8]
        self.assertTrue(caps)
        for cap in caps:
            p=cap[:,[0,2]];signs=[]
            for a,b in zip(p,np.roll(p,-1,axis=0)):
                edge=b-a;signs.append(edge[0]*(-a[1])-edge[1]*(-a[0]))
            self.assertFalse(all(v>=0 for v in signs) or all(v<=0 for v in signs),'An end cap fills the cave entrance.')
        self.assertTrue(any('modeled hollow tube' in text for entry in catalog() if entry['id']=='cavern' for text in entry['limitations']))

    def test_actual_template_assets_reopen_from_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            for preset in ('village','castle','cavern','control-room'):
                root=Path(temp)/preset;create_environment(root,recipe(preset))
                bundle=load_project(root);scene_path=bundle['project']['scenes'][-1]['path']
                meshes=load_meshes(root,scene_path)
                self.assertEqual(set(meshes),{o['id'] for o in plan(recipe(preset))})
                self.assertGreater(sum(len(m['triangles']) for m in meshes.values()),100)

    def test_invalid_catalog_inputs_and_output_budgets_fail(self):
        for p in ({'preset':'unknown'},{'preset':'castle','scale':0},{'preset':'castle','detail':3},
                  {'preset':'castle','unsupported':True},
                  {'preset':'castle','scale':10**400},{'preset':'castle','detail':10**400}):
            candidate=recipe('castle');candidate['parameters']=p
            with self.assertRaises(RigError):plan(candidate)
        with self.assertRaises(RigError):plan(dict(recipe('castle'),budget=3))
        with self.assertRaises(RigError):plan(dict(recipe('castle'),id='a'*25))

if __name__=='__main__':unittest.main()
