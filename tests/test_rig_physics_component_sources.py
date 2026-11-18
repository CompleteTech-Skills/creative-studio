"""Actual source geometry and canonical transform admission for components."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from physics_backend_example import build_inputs
from physics_component_sources import admit_component_meshes
from physics_backends import canonical, digest, BackendError
from scene3d_contracts import load_project


class ComponentSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.world,payloads=build_inputs()
        for name,data in payloads.items():(self.root/name).write_bytes(data)
        self.world['components']=[dict(id='ball',category='particles',geometry='ball-asset',count=10,
            lifetime_seconds=1,seed=7,mass=1,radius=.1,initial_velocity=[0,1,0],colliders=[])]
        self.bundle=load_project(self.root)

    def test_real_mesh_uses_canonical_scene_world_transform(self):
        result=admit_component_meshes(self.root,self.world,self.bundle)
        mesh=result['components']['ball']
        self.assertEqual(len(mesh['positions']),6)
        self.assertEqual(mesh['positions'][0],[.5,3.,0.])
        self.assertEqual(mesh['local_positions'][0],[.5,0.,0.])
        self.assertEqual(mesh['world_matrix'][1][3],3.)
        self.assertTrue(result['fixture'])

    def test_changed_bytes_and_forged_reference_reject(self):
        original=(self.root/'ball.obj').read_bytes()
        (self.root/'ball.obj').write_bytes(original+b'\n')
        with self.assertRaises(BackendError):admit_component_meshes(self.root,self.world,self.bundle)
        (self.root/'ball.obj').write_bytes(original)
        self.world['components'][0]['geometry']='floor-asset'
        with self.assertRaises(BackendError):admit_component_meshes(self.root,self.world,self.bundle)

    def test_missing_node_or_wrong_declared_vertex_count_reject(self):
        self.world['components'][0]['vertex_count']=9
        with self.assertRaises(BackendError):admit_component_meshes(self.root,self.world,self.bundle)
        self.world['components'][0].pop('vertex_count');self.world['components'][0]['id']='missing'
        with self.assertRaises(BackendError):admit_component_meshes(self.root,self.world,self.bundle)

    def test_symlink_source_cannot_escape_verified_root(self):
        external=self.root/'external.obj';external.write_bytes((self.root/'ball.obj').read_bytes())
        (self.root/'ball.obj').unlink();(self.root/'ball.obj').symlink_to(external)
        with self.assertRaises(BackendError):admit_component_meshes(self.root,self.world,self.bundle)
