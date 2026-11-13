"""Real generated geometry, spatial limits and immutable save/reopen regressions."""
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile
import unittest
import numpy as np
from rig_errors import RigError
from scene3d_geometry import primitive, wall, extrude, curve
from scene3d_terrain import Heightfield, scatter
from scene3d_environment import plan, create_environment, apply_recipe, fingerprint
from scene3d_contracts import load_project
from scene3d_graph import SceneGraph
from scene3d_snapshot import snapshot_project


def recipe(generator='rooms',**parameters):
    return dict(id='test',version=1,seed=7,generator=generator,parameters=parameters,budget=100)


class GeometryTests(unittest.TestCase):
    def test_supported_primitives_have_real_nondegenerate_meshes(self):
        for kind in ('box','plane','cylinder','cone','sphere','capsule','torus','ramp','stairs','pipe','beam','platform','door','window'):
            with self.subTest(kind=kind):
                mesh=primitive(kind);mesh.validate()
                self.assertGreater(len(mesh.obj()),100)
                self.assertIn(b'\nf ',mesh.obj())
                self.assertIn(b'\nvn ',mesh.obj())
        extrude([[0,0],[1,0],[1,1],[0,1]]).validate()
        curve([[0,0,0],[1,1,0],[2,1,1]]).validate()

    def test_closed_shapes_outward_winding(self):
        for kind in ('box','sphere','cylinder','cone','torus','pipe','capsule'):
            mesh=primitive(kind);v=np.array(mesh.vertices);f=np.array(mesh.triangles)
            volume=np.einsum('ij,ij->i',v[f[:,0]],np.cross(v[f[:,1]],v[f[:,2]])).sum()/6
            self.assertGreater(volume,0,kind)

    def test_opening_has_no_crossing_triangles(self):
        mesh=wall(5,3,.2,[dict(x=2,y=0,width=1,height=2)])
        for tri in np.array(mesh.vertices)[mesh.triangles]:
            center=tri.mean(axis=0)
            self.assertFalse(-.5<center[0]<.5 and 0<center[1]<2)

    def test_invalid_dimensions_and_profiles_rejected(self):
        for kwargs in (dict(width=0),dict(width=float('nan')),dict(no_such_parameter=1)):
            with self.assertRaises(RigError):primitive('box',**kwargs)
        with self.assertRaises(RigError):primitive('pipe',radius=1,inner_radius=2)
        with self.assertRaises(RigError):wall(2,2,.1,[dict(x=1,y=0,width=2,height=1)])
        with self.assertRaises(RigError):extrude([[0,0],[1,1],[0,1],[1,0]])


class TerrainTests(unittest.TestCase):
    def test_seed_slope_and_triangle_sampling(self):
        a=Heightfield(seed=8,max_slope=10);b=Heightfield(seed=8,max_slope=10)
        self.assertEqual(a.mesh().obj(),b.mesh().obj())
        self.assertNotEqual(a.mesh().obj(),Heightfield(seed=9).mesh().obj())
        t=Heightfield(width=2,depth=2,resolution=2,elevation=4,heights=[[0,1],[2,4]])
        self.assertAlmostEqual(t.sample(-.5,-.5)['height'],.75)
        self.assertAlmostEqual(t.sample(.5,.5)['height'],2.75)
        with self.assertRaises(RigError):t.sample(2,0)

    def test_triangle_slope_bound_and_edited_slope_failure(self):
        heights=np.zeros((5,5));heights[2,2]=1
        terrain=Heightfield(width=4,depth=4,resolution=5,elevation=1,heights=heights,max_slope=10)
        self.assertLessEqual(terrain.sample(.25,.25)['slope'],10+1e-9)
        terrain.heights[2,2]=4
        with self.assertRaises(RigError):terrain.mesh()

    def test_flatten_path_and_spatial_scatter(self):
        a=Heightfield(seed=8).path([[-10,0],[10,0]],width=2,elevation=0)
        self.assertEqual(a.sample(0,0)['height'],0)
        points=scatter(a,20,spacing=2,seed=8,max_slope=90,exclusions=[[-2,-2,2,2]])
        self.assertEqual(points,scatter(a,20,spacing=2,seed=8,max_slope=90,exclusions=[[-2,-2,2,2]]))
        for i,p in enumerate(points):
            self.assertFalse(-2<=p[0]<=2 and -2<=p[2]<=2)
            for q in points[i+1:]:self.assertGreaterEqual(math.hypot(p[0]-q[0],p[2]-q[2]),2)
        with self.assertRaises(RigError):scatter(a,10,spacing=100,retries=1)


class EnvironmentTests(unittest.TestCase):
    def test_tree_geometry_fits_bounds_and_does_not_overlap(self):
        objects=plan(dict(recipe('terrain',elevation=0,scatter_count=100),budget=300))
        crowns=[o for o in objects if 'crown' in o['id']]
        for i,a in enumerate(crowns):
            self.assertLessEqual(abs(a['position'][0])+.65,10)
            self.assertLessEqual(abs(a['position'][2])+.65,10)
            for b in crowns[i+1:]:
                self.assertGreaterEqual(math.hypot(a['position'][0]-b['position'][0],a['position'][2]-b['position'][2]),1.3)

    def test_malformed_parameters_are_structured_errors(self):
        for p in ({'kind':[],'dimensions':{}},{'kind':'box','dimensions':[]}):
            with self.assertRaises(RigError):plan(recipe('primitive',**p))
        for data in ('not a heightmap',[[object()]]):
            with self.assertRaises(RigError):Heightfield(heights=data)
        with self.assertRaises(RigError):extrude([['a','b']])

    def test_rooms_are_connected_and_bounded(self):
        items=plan(recipe(rooms=3))
        self.assertEqual(len([x for x in items if 'partition' in x['id']]),4)
        with self.assertRaises(RigError):plan(recipe(width=1,door_width=2))
        with self.assertRaises(RigError):plan(dict(recipe(),budget=2))
        with self.assertRaises(RigError):plan(recipe('caves'))

    def test_deleted_nodes_stay_deleted_across_generator_switch(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);first=root/'first'
            original=recipe('primitive',kind='box',dimensions={})
            create_environment(first,original)
            b=load_project(first);path=b['project']['scenes'][-1]['path']
            graph=SceneGraph(b['documents'][path]);graph.delete('test-mesh')
            snapshot_project(first,root/'deleted',graph,scene_path=path)
            b=load_project(root/'deleted');path=b['project']['scenes'][-1]['path']
            apply_recipe(root/'deleted',root/'rooms',path,recipe(rooms=1))
            b=load_project(root/'rooms');path=b['project']['scenes'][-1]['path']
            result=apply_recipe(root/'rooms',root/'back',path,original)
            b=load_project(root/'back');scene=b['documents'][b['project']['scenes'][-1]['path']]
            self.assertIn('test-mesh',result['conflicts'])
            self.assertNotIn('test-mesh',[n['id'] for n in scene['nodes']])

    def test_preprocessing_collections_are_bounded(self):
        with self.assertRaises(RigError):plan(recipe('terrain',paths=[{}]*9))
        with self.assertRaises(RigError):scatter(Heightfield(),1,exclusions=[[0,0,1,1]]*129)

    def test_real_save_reopen_and_manual_edit_survive_regeneration(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);first=root/'first'
            result=create_environment(first,recipe(rooms=2))
            bundle=load_project(first)
            scene_ref=bundle['project']['scenes'][-1]
            scene=bundle['documents'][scene_ref['path']]
            generated=[n for n in scene['nodes'] if n['metadata'].get('generator_id')=='test']
            self.assertEqual(len(generated),11)
            for node in generated:
                asset=bundle['documents'][node['asset']['path']]
                self.assertIn(b'\nf ',(first/asset['content']['path']).read_bytes())
            graph=SceneGraph(scene)
            selected=generated[0]['id']
            transform=deepcopy(generated[0]['transform']);transform['translation'][1]=12
            graph.set_transform(selected,transform)
            edited=root/'edited';saved=snapshot_project(first,edited,graph,scene_path=scene_ref['path'])
            b=load_project(edited);active=b['project']['scenes'][-1]['path']
            regenerated=root/'regenerated'
            result=apply_recipe(edited,regenerated,active,recipe(rooms=3))
            self.assertEqual(result['conflicts'],[selected])
            final=load_project(regenerated);nodes=final['documents'][final['project']['scenes'][-1]['path']]['nodes']
            self.assertEqual(next(n for n in nodes if n['id']==selected)['transform']['translation'][1],12)
            with self.assertRaises(RigError):apply_recipe(edited,root/'rejected',active,recipe(),policy='reject')
            self.assertFalse((root/'rejected').exists())

if __name__=='__main__':unittest.main()
