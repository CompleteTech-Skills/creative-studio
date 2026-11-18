import copy
import sys
from pathlib import Path
import unittest
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_coordinates import conversion_matrix, transform_geometry
from rig_errors import RigError
from rig_models import Mesh, Geometry

class Coordinates(unittest.TestCase):
    def geometry(self):
        return Geometry([Mesh('semantic', np.array([[0.,0,0],[1.,0,0],[0.,1,0]]),np.array([[0,1,2]]),attributes={'NORMAL':np.tile([0.,0,1],(3,1)), 'TANGENT':np.tile([1.,0,0,1],(3,1))},name='Left hand')],[{}])
    def test_basis_units_pivot_roundtrip(self):
        g=self.geometry(); before=copy.deepcopy(g)
        m=conversion_matrix(source_basis='right-handed-z-up',unit_scale=.01,pivot=[1,2,3],translation=[4,5,6])
        changed=transform_geometry(g,m); back=transform_geometry(changed,np.linalg.inv(m))
        np.testing.assert_allclose(back.meshes[0].positions,g.meshes[0].positions,atol=1e-12)
        self.assertEqual(changed.meshes[0].name,'Left hand')
        np.testing.assert_array_equal(g.meshes[0].positions,before.meshes[0].positions)
        np.testing.assert_allclose(m@np.array([1,2,3,1]),[4,5,6,1])
    def test_reflection_winding(self):
        g=transform_geometry(self.geometry(),conversion_matrix(source_basis='left-handed-y-up'))
        np.testing.assert_array_equal(g.meshes[0].triangles,[[0,2,1]])
        self.assertEqual(g.meshes[0].attributes['TANGENT'][0,3],-1)
        p=g.meshes[0].positions[g.meshes[0].triangles[0]]
        self.assertGreater(np.dot(np.cross(p[1]-p[0],p[2]-p[0]),g.meshes[0].attributes['NORMAL'][0]),0)
    def test_morph_nonuniform(self):
        g=self.geometry();g.meshes[0].attributes['NORMAL'][:]=[1,1,0]
        g.meshes[0].morphs={'move':{'POSITION':np.ones((3,3)),'NORMAL':np.tile([0.,1,1],(3,1))}}
        m=np.diag([2.,3.,4.,1]);out=transform_geometry(g,m).meshes[0]
        expected=(np.array([1,1,0])+.4*np.array([0,1,1]))@np.linalg.inv(m[:3,:3]);expected/=np.linalg.norm(expected)
        actual=out.attributes['NORMAL'][0]+.4*out.morphs['move']['NORMAL'][0];actual/=np.linalg.norm(actual)
        np.testing.assert_allclose(actual,expected)
    def test_all_basis_pairs_and_scales(self):
        from model3d_coordinates import BASES
        g=self.geometry()
        for source in BASES:
            for target in BASES:
                for scale in [1e-6,1.,1e6]:
                    with self.subTest(source=source,target=target,scale=scale):
                        m=conversion_matrix(source_basis=source,target_basis=target,unit_scale=scale)
                        back=transform_geometry(transform_geometry(g,m),np.linalg.inv(m))
                        np.testing.assert_allclose(back.meshes[0].positions,g.meshes[0].positions,atol=1e-12)
                        np.testing.assert_array_equal(back.meshes[0].triangles,g.meshes[0].triangles)
    def test_tangent_morph_reflected_nonuniform(self):
        g=self.geometry();g.meshes[0].attributes['TANGENT'][:]=[1,1,0,1]
        g.meshes[0].morphs={'move':{'POSITION':np.ones((3,3)),'TANGENT':np.tile([0.,1,1],(3,1))}}
        m=np.diag([-2.,3.,4.,1]); out=transform_geometry(g,m).meshes[0]
        expected=(np.array([1,1,0])+.4*np.array([0,1,1]))@m[:3,:3].T;expected/=np.linalg.norm(expected)
        actual=out.attributes['TANGENT'][0,:3]+.4*out.morphs['move']['TANGENT'][0];actual/=np.linalg.norm(actual)
        np.testing.assert_allclose(actual,expected)
        self.assertEqual(out.attributes['TANGENT'][0,3],-1)
        np.testing.assert_array_equal(g.meshes[0].morphs['move']['POSITION'],np.ones((3,3)))
    def test_integer_direction_arrays_promote_without_truncation(self):
        g=self.geometry(); g.meshes[0].attributes['NORMAL']=np.tile([1,1,0],(3,1))
        out=transform_geometry(g,np.eye(4))
        np.testing.assert_allclose(out.meshes[0].attributes['NORMAL'][0],[2**-.5,2**-.5,0])
        self.assertEqual(g.meshes[0].attributes['NORMAL'].dtype.kind,'i')
    def test_invalid_geometry_and_missing_direction_base(self):
        g=self.geometry();g.meshes[0].positions[0,0]=np.nan
        with self.assertRaises(RigError):transform_geometry(g,np.eye(4))
        g=self.geometry();g.meshes[0].morphs={'move':{'POSITION':np.ones((3,3)),'NORMAL':np.ones((3,3))}}
        del g.meshes[0].attributes['NORMAL']
        with self.assertRaises(RigError):transform_geometry(g,np.eye(4))
    def test_real_primitive_obj_and_glb_conversion_roundtrip(self):
        import tempfile
        from model3d_primitives import primitive
        from model3d_interchange import export_mesh
        from rig_import import load_character_model
        g=primitive('box').geometry()
        m=conversion_matrix(source_basis='right-handed-z-up',unit_scale=.01,pivot=[1,2,3])
        converted=transform_geometry(g,m)
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in ['obj','glb','gltf']:
                directory=Path(tmp)/fmt
                export_mesh(converted,directory,format=fmt)
                imported=load_character_model(directory/('model.'+fmt)).geometry
                np.testing.assert_allclose(imported.meshes[0].positions.min(axis=0),converted.meshes[0].positions.min(axis=0),atol=1e-8)
                np.testing.assert_allclose(imported.meshes[0].positions.max(axis=0),converted.meshes[0].positions.max(axis=0),atol=1e-8)
                self.assertEqual(imported.triangle_count,converted.triangle_count)
    def test_partial_limits_and_computed_transform_bounds(self):
        g=self.geometry()
        transform_geometry(g,np.eye(4),limits={'vertices':3.0})
        with self.assertRaises(RigError):transform_geometry(g,np.eye(4),limits={'memory_bytes':1})
        with self.assertRaises(RigError):conversion_matrix(pivot=[1e308,0,0],unit_scale=1e6)
        with self.assertRaises(RigError):conversion_matrix(translation=[True,0,0])
    def test_default_placement_constraints_and_overrides(self):
        from model3d_primitives import primitive
        from model3d_coordinates import placement
        geometry=primitive('box',{'width':2,'height':4,'depth':6}).geometry()
        prop=placement(geometry,purpose='prop',minimum_dimensions=[2,4,6],maximum_dimensions=[2,4,6])
        self.assertEqual(prop['pivot'],[0,0,0])
        character=placement(geometry,purpose='character',ground=True)
        self.assertEqual(character['pivot'],[0,-2,0])
        moved=transform_geometry(geometry,character['matrix']);self.assertEqual(moved.meshes[0].positions[:,1].min(),0)
        self.assertEqual(placement(geometry,purpose='architecture')['pivot'],[0,0,0])
        self.assertEqual(placement(geometry,purpose='component',anchor=[1,2,3],pivot=[4,5,6])['pivot'],[4,5,6])
        with self.assertRaises(RigError):placement(geometry,purpose='component')
        with self.assertRaises(RigError):placement(geometry,maximum_dimensions=[1,2,3])
    def test_named_root_socket_camera_frames_roundtrip(self):
        from model3d_coordinates import transform_frames
        camera=np.eye(4);camera[:3,3]=[1,2,3]
        frames={'root':np.eye(4),'left hinge':camera,'camera':camera}
        matrix=conversion_matrix(source_basis='right-handed-z-up',unit_scale=.01,pivot=[1,0,0])
        transformed=transform_frames(frames,matrix)
        restored=transform_frames(transformed,np.linalg.inv(matrix))
        self.assertEqual(set(restored),set(frames))
        for name in frames:np.testing.assert_allclose(restored[name],frames[name],atol=1e-12)
        np.testing.assert_array_equal(frames['camera'],camera)
        with self.assertRaises(RigError):transform_frames({'camera':np.zeros((4,4))},matrix)
    def test_integer_placement_does_not_wrap(self):
        from model3d_coordinates import placement
        geometry=self.geometry();geometry.meshes[0].positions=np.array([[200,200,200],[201,200,200],[200,201,200]],dtype=np.uint8)
        report=placement(geometry)
        self.assertEqual(report['pivot'],[200.5,200.5,200.])
        geometry.meshes[0].positions=np.array([[2**60,0,0],[2**60+1,0,0],[2**60,1,0]],dtype=np.uint64)
        with self.assertRaises(RigError):placement(geometry)
        with self.assertRaises(RigError):transform_geometry(geometry,np.eye(4))
    def test_camera_optical_local_minus_z_world_basis_oracle(self):
        from model3d_coordinates import transform_frames
        from rig_math import BLENDER_FROM_CANONICAL
        source=np.eye(4);source[:3,3]=[1,2,3]
        matrix=conversion_matrix(source_basis='right-handed-z-up')
        world=np.asarray(transform_frames({'camera-affine-anchor':source},matrix)['camera-affine-anchor'])
        np.testing.assert_allclose(world,BLENDER_FROM_CANONICAL.T@source,atol=1e-12)
        np.testing.assert_allclose(world[:3,3],[1,3,-2])
        np.testing.assert_allclose(-world[:3,2],[0,-1,0])
        # Optical-local -Z is opposite the actor/object +Z axis.
        np.testing.assert_allclose(world[:3,2],[0,1,0])
        np.testing.assert_allclose(world@np.array([0,0,-2,1]),[1,1,-2,1])
        np.testing.assert_allclose(world@np.array([1,0,-2,1]),[2,1,-2,1])
    def test_invalid_and_tiny(self):
        for scale in [True,0,1e-7,float('nan'),10**5000]:
            with self.assertRaises(RigError):conversion_matrix(unit_scale=scale)
        for pivot in [[True,False,True],[True,0,0],['1','2','3']]:
            with self.assertRaises(RigError):conversion_matrix(pivot=pivot)
        with self.assertRaises(RigError):conversion_matrix(source_basis=[])
        with self.assertRaises(RigError):transform_geometry(self.geometry(),np.eye(4,dtype=bool))
        transform_geometry(self.geometry(),conversion_matrix(unit_scale=1e-6))
        for m in [np.zeros((4,4)),np.diag([1,1,0,1]),[[1]],np.full((4,4),np.nan)]:
            with self.assertRaises(RigError):transform_geometry(self.geometry(),m)

if __name__=='__main__':unittest.main()
