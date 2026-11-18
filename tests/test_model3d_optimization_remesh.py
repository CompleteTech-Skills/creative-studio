import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_optimization_remesh import vertex_cluster_remesh
from rig_demo import ellipsoid
from rig_errors import RigError
from rig_models import Mesh

class Remesh(unittest.TestCase):
    def sphere(self):
        mesh=ellipsoid('ball',[0,0,0],[1,1,1],0,latitudes=12,longitudes=24)
        mesh.attributes={};mesh.name='Semantic ball';mesh.region='head'
        return mesh
    def test_actual_remesh_reduces_real_surface_preserves_source(self):
        mesh=self.sphere();before=copy.deepcopy(mesh)
        result=vertex_cluster_remesh(mesh,cell_size=.3,source_version='asset-v1')
        self.assertLess(len(result.mesh.positions),len(mesh.positions));self.assertLess(len(result.mesh.triangles),len(mesh.triangles))
        self.assertGreater(result.report['max_vertex_displacement'],0)
        self.assertLessEqual(result.report['max_vertex_displacement'],np.sqrt(3)*.3)
        np.testing.assert_array_equal(mesh.positions,before.positions);np.testing.assert_array_equal(mesh.triangles,before.triangles)
        self.assertEqual(result.report['source_sha256'],hashlib.sha256(result.source_bytes).hexdigest())
        source=json.loads(result.source_bytes);np.testing.assert_array_equal(source['triangles'],mesh.triangles)
        self.assertEqual(result.mesh.name,'Semantic ball');self.assertEqual(result.mesh.region,'head')
        self.assertEqual(result.report['components'],1)
        np.testing.assert_allclose(np.linalg.norm(result.mesh.attributes['NORMAL'],axis=1),1)
    def test_identical_overlapping_components_never_merge(self):
        mesh=self.sphere();n=len(mesh.positions)
        mesh.positions=np.concatenate([mesh.positions,mesh.positions]);mesh.triangles=np.concatenate([mesh.triangles,mesh.triangles+n])
        result=vertex_cluster_remesh(mesh,cell_size=.3,source_version='v1')
        self.assertEqual(result.report['components'],2)
        half=len(result.mesh.positions)//2
        np.testing.assert_allclose(result.mesh.positions[:half],result.mesh.positions[half:])
    def test_open_boundary_vertices_and_edges_preserved(self):
        # Fan with four protected boundary vertices and two close interior points.
        positions=np.array([[-1.,-1,0],[1,-1,0],[1,1,0],[-1,1,0],[0,0,0],[.01,0,0]])
        faces=np.array([[0,1,5],[1,2,5],[2,4,5],[2,3,4],[3,0,4],[0,5,4]])
        mesh=Mesh('patch',positions,faces)
        result=vertex_cluster_remesh(mesh,cell_size=.5,source_version='open')
        np.testing.assert_array_equal(result.mesh.positions[:4],positions[:4])
        self.assertEqual(result.report['protected_boundary_vertices'],4)
        self.assertEqual(len(result.mesh.triangles),4)
    def test_rejects_attribute_and_morph_loss(self):
        for attribute in ['NORMAL','TEXCOORD_0','TANGENT']:
            mesh=self.sphere();mesh.attributes={attribute:np.zeros((len(mesh.positions),3))}
            with self.assertRaises(RigError):vertex_cluster_remesh(mesh,cell_size=.3,source_version='v1')
        mesh=self.sphere();mesh.morphs={'move':{'POSITION':np.zeros_like(mesh.positions)}}
        with self.assertRaises(RigError):vertex_cluster_remesh(mesh,cell_size=.3,source_version='v1')
    def test_rejects_unsafe_input_and_coarse_grid(self):
        for edit in ['duplicate','flip','nonfinite','invalid','degenerate','integerprecision']:
            mesh=self.sphere()
            if edit=='duplicate':mesh.triangles=np.vstack([mesh.triangles,mesh.triangles[0]])
            if edit=='flip':mesh.triangles[0]=mesh.triangles[0,::-1]
            if edit=='nonfinite':mesh.positions[0,0]=np.inf
            if edit=='invalid':mesh.triangles[0,0]=len(mesh.positions)
            if edit=='degenerate':mesh.triangles[0,0]=mesh.triangles[0,1]
            if edit=='integerprecision':mesh.positions=np.full_like(mesh.positions,2**60,dtype=np.int64)
            with self.subTest(edit=edit),self.assertRaises(RigError):vertex_cluster_remesh(mesh,cell_size=.3,source_version='v1')
        with self.assertRaises(RigError):vertex_cluster_remesh(self.sphere(),cell_size=100,source_version='v1')
    def test_pinched_vertex_fans_rejected(self):
        positions=np.array([[0.,0,0],[1,0,0],[0,1,0],[0,0,1],[-1,0,0],[0,-1,0],[0,0,-1]])
        faces=np.array([[0,2,1],[0,1,3],[0,3,2],[1,2,3],[0,5,4],[0,4,6],[0,6,5],[4,5,6]])
        with self.assertRaises(RigError):vertex_cluster_remesh(Mesh('pinch',positions,faces),cell_size=.01,source_version='v1')
    def test_real_native_glb_roundtrip(self):
        import tempfile
        from rig_models import Geometry
        from rig_gltf import export_geometry
        from rig_import import load_character_model
        result=vertex_cluster_remesh(self.sphere(),cell_size=.3,source_version='v1')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'remeshed.glb';export_geometry(Geometry([result.mesh],[{}]),path)
            geometry=load_character_model(path).geometry
            self.assertEqual(geometry.triangle_count,len(result.mesh.triangles))
            np.testing.assert_allclose(geometry.meshes[0].positions,result.mesh.positions,atol=1e-7)
    def test_translated_tetra_normals_are_translation_and_scale_invariant(self):
        base=np.array([[0.,0,0],[1,0,0],[0,1,0],[0,0,1]])
        faces=np.array([[0,2,1],[0,1,3],[0,3,2],[1,2,3]])
        expected=np.array([[-1,-1,-1],[1,0,0],[0,1,0],[0,0,1]],dtype=float)
        expected/=np.linalg.norm(expected,axis=1)[:,None]
        for scale,offset in [(1.,2e6),(1.,1e9),(1e-9,2e-3),(1e9,1e15)]:
            with self.subTest(scale=scale,offset=offset):
                positions=base*scale+offset
                result=vertex_cluster_remesh(Mesh('tetra',positions,faces),cell_size=scale*.01,source_version='v1')
                np.testing.assert_array_equal(result.mesh.positions,positions)
                np.testing.assert_allclose(result.mesh.attributes['NORMAL'],expected,atol=1e-9)
                self.assertEqual(result.report['max_vertex_displacement'],0.)
    def test_protected_boundary_singletons_are_bit_exact(self):
        positions=np.array([[1e9+.1,.123456789,.2],[1e9+1.1,.123456789,.2],[1e9+.1,1.123456789,.2]])
        mesh=Mesh('boundary',positions,np.array([[0,1,2]]))
        result=vertex_cluster_remesh(mesh,cell_size=.01,source_version='v1')
        np.testing.assert_array_equal(result.mesh.positions,positions)
        np.testing.assert_array_equal(result.mesh.positions.view(np.uint64),positions.view(np.uint64))
        self.assertEqual(result.report['max_vertex_displacement'],0.)
        self.assertEqual(result.report['rms_vertex_displacement'],0.)
    def test_displacement_reports_actual_world_coordinate_differences(self):
        mesh=self.sphere();mesh.positions+=1e9
        result=vertex_cluster_remesh(mesh,cell_size=.3,source_version='v1')
        # Recover the exact algorithm's component-local grid correspondence and
        # independently measure displacement from the actual stored coordinates.
        grid=np.floor((mesh.positions-mesh.positions.min(axis=0))/.3).astype(int)
        buckets={};mapping=[]
        for cell in grid:
            key=tuple(cell)
            if key not in buckets:buckets[key]=len(buckets)
            mapping.append(buckets[key])
        distances=np.hypot.reduce(mesh.positions-result.mesh.positions[mapping],axis=1)
        self.assertEqual(result.report['max_vertex_displacement'],float(distances.max()))
        self.assertEqual(result.report['rms_vertex_displacement'],float(np.hypot.reduce(distances/np.sqrt(len(distances)))))
        self.assertGreater(result.report['max_vertex_displacement'],0)
    def test_different_scale_components_keep_valid_normals(self):
        base=np.array([[0.,0,0],[1,0,0],[0,1,0],[0,0,1]])
        faces=np.array([[0,2,1],[0,1,3],[0,3,2],[1,2,3]])
        positions=np.vstack([base*.1,base*1e3+1e9])
        mesh=Mesh('multi',positions,np.vstack([faces,faces+4]))
        result=vertex_cluster_remesh(mesh,cell_size=.01,source_version='v1')
        self.assertEqual(result.report['components'],2)
        np.testing.assert_allclose(result.mesh.attributes['NORMAL'][:4],result.mesh.attributes['NORMAL'][4:])
        np.testing.assert_array_equal(result.mesh.positions,positions)
    def test_resource_numeric_boundaries(self):
        for size in [True,0,-1,float('nan'),10**5000]:
            with self.subTest(size=type(size)),self.assertRaises(RigError):vertex_cluster_remesh(self.sphere(),cell_size=size,source_version='v1')
        for limits in [{'memory_bytes':1},{'vertices':1},{'triangles':1},{'source_bytes':1},{'memory_bytes':True},{'vertices':3.5}]:
            with self.subTest(limits=limits),self.assertRaises(RigError):vertex_cluster_remesh(self.sphere(),cell_size=.3,source_version='v1',limits=limits)
        vertex_cluster_remesh(self.sphere(),cell_size=.3,source_version='v1',limits={'vertices':1000.0})
        for label in ['',True,'bad\nlabel']:
            with self.assertRaises(RigError):vertex_cluster_remesh(self.sphere(),cell_size=.3,source_version=label)
    def test_deterministic_and_fine_grid_identity(self):
        mesh=self.sphere()
        a=vertex_cluster_remesh(mesh,cell_size=.3,source_version='v1');b=vertex_cluster_remesh(mesh,cell_size=.3,source_version='v1')
        np.testing.assert_array_equal(a.mesh.positions,b.mesh.positions);np.testing.assert_array_equal(a.mesh.triangles,b.mesh.triangles)
        fine=vertex_cluster_remesh(mesh,cell_size=.001,source_version='v1')
        np.testing.assert_allclose(fine.mesh.positions,mesh.positions,atol=1e-15)
        np.testing.assert_array_equal(fine.mesh.triangles,mesh.triangles)

if __name__=='__main__':unittest.main()
