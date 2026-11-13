"""Actual analytic primitive and immutable polygon boundary regressions."""
import copy
import hashlib
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from model3d_primitives import editable_mesh, primitive
from rig_errors import RigError
from rig_geometry import analyze_geometry, mesh_edges


class PrimitiveTests(unittest.TestCase):
    def test_box_analytic_bounds_volume_winding_and_editable_quads(self):
        result = primitive('box', dict(width=2, height=3, depth=4), name='Semantic Box', mesh_id='semantic-box')
        report = analyze_geometry(result.geometry())
        self.assertEqual(result.mesh.name, 'Semantic Box')
        self.assertEqual(result.mesh.id, 'semantic-box')
        self.assertEqual(result.source_positions.shape, (8,3))
        self.assertEqual(len(result.faces), 6)
        self.assertTrue(all(len(f)==4 for f in result.faces))
        self.assertEqual(result.mesh.triangles.shape, (12,3))
        self.assertEqual(report['bounds'], [[-1,-1.5,-2],[1,1.5,2]])
        self.assertAlmostEqual(report['meshes'][0]['signed_volume_m3'],24)
        self.assertEqual(report['meshes'][0]['boundary_edges'],0)
        self.assertEqual(set(result.triangle_faces),set(range(6)))

    def test_smooth_cube_corner_normals_are_symmetric(self):
        result = primitive('box')
        expected = np.sign(result.mesh.positions)/np.sqrt(3)
        np.testing.assert_allclose(result.mesh.attributes['NORMAL'], expected, atol=1e-12)

    def test_flat_hard_edges_preserve_original_source_topology(self):
        smooth=primitive('box'); flat=primitive('box',normal_mode='flat')
        np.testing.assert_array_equal(smooth.source_positions,flat.source_positions)
        self.assertEqual(smooth.faces,flat.faces)
        self.assertEqual(len(flat.mesh.positions),24)
        self.assertEqual(len(np.unique(flat.mesh.attributes['NORMAL'],axis=0)),6)
        for fi in range(6):
            triangles=flat.mesh.triangles[flat.triangle_faces==fi]
            self.assertEqual(len(np.unique(flat.mesh.attributes['NORMAL'][triangles.flatten()],axis=0)),1)
        self.assertFalse(flat.source_positions.flags.writeable)
        self.assertFalse(flat.mesh.positions.flags.writeable)
        with self.assertRaises(ValueError): flat.source_positions[0,0]=10

    def test_plane_open_intentionally_y_up(self):
        result=primitive('plane',dict(width=2,depth=4));r=analyze_geometry(result.geometry())
        np.testing.assert_allclose(result.mesh.attributes['NORMAL'],[[0,1,0]]*4)
        self.assertEqual(r['meshes'][0]['boundary_edges'],4)
        self.assertEqual(r['surface_area_m2'],8)
        self.assertIsNone(r['meshes'][0]['signed_volume_m3'])

    def test_capped_primitives_analytic_polygon_volume(self):
        n=12;r=2.;h=3.;area=n*r*r*math.sin(2*math.pi/n)/2
        for kind,factor in [('cylinder',1),('cone',1/3)]:
            result=primitive(kind,dict(radius=r,height=h,segments=n))
            report=analyze_geometry(result.geometry())['meshes'][0]
            self.assertAlmostEqual(report['signed_volume_m3'],area*h*factor)
            self.assertEqual(report['boundary_edges'],0)
            self.assertEqual(report['degenerate_triangles'],0)

    def test_sphere_poles_not_degenerate_and_converges_volume(self):
        result=primitive('sphere',dict(radius=2,segments=32,rings=16))
        report=analyze_geometry(result.geometry())['meshes'][0]
        self.assertEqual(report['vertices'],482)
        self.assertEqual(report['triangles'],960)
        self.assertEqual(report['degenerate_triangles'],0)
        self.assertEqual(report['boundary_edges'],0)
        self.assertAlmostEqual(report['signed_volume_m3']/(4/3*math.pi*8),1,delta=.02)

    def test_torus_actual_hole_and_positive_volume(self):
        result=primitive('torus',dict(major_radius=2,minor_radius=.5,segments=32,rings=16))
        report=analyze_geometry(result.geometry())['meshes'][0]
        self.assertEqual(report['boundary_edges'],0)
        self.assertAlmostEqual(report['signed_volume_m3']/(2*math.pi**2*2*.5**2),1,delta=.04)
        radial=np.linalg.norm(result.source_positions[:,[0,2]],axis=1)
        self.assertGreaterEqual(radial.min(),1.5-1e-12)

    def test_concave_editable_polygon_uses_native_ear_clipping(self):
        positions=[[0,0,0],[2,0,0],[2,0,2],[1,0,1],[0,0,2]]
        result=editable_mesh(positions,[[4,3,2,1,0]])
        self.assertEqual(result.faces,((4,3,2,1,0),))
        self.assertAlmostEqual(analyze_geometry(result.geometry())['surface_area_m2'],3)
        self.assertEqual(result.mesh.triangles.shape,(3,3))

    def test_integral_json_numbers_normalized_copy(self):
        parameters=dict(segments=8.,rings=4.,radius=2.)
        original=copy.deepcopy(parameters)
        a=primitive('sphere',parameters)
        b=primitive('sphere',dict(segments=8,rings=4,radius=2))
        np.testing.assert_array_equal(a.mesh.triangles,b.mesh.triangles)
        self.assertEqual(parameters,original)
        positions=[[0,0,0],[1,0,0],[0,0,1]]; faces=[[0.,2.,1.]]
        result=editable_mesh(positions,faces)
        self.assertEqual(result.faces,((0,2,1),)); self.assertEqual(faces,[[0.,2.,1.]])

    def test_portable_identity_separate_from_display_name(self):
        for name in ('Left Arm', '../mesh', 'mesh\n', '_mesh', 'mesh/part'):
            with self.subTest(name=name), self.assertRaises(RigError):
                primitive('box', name=name)
        result = primitive('box', name='Left Arm', mesh_id='left-arm')
        self.assertEqual(result.mesh.id, 'left-arm')
        self.assertEqual(result.mesh.name, 'Left Arm')

    def test_invalid_parameters_structured(self):
        cases=[('sphere',{'segments':True}),('sphere',{'segments':3.5}),('box',{'width':float('nan')}),
               ('box',{'width':float('inf')}),('box',{'width':0}),('cone',{'segments':257}),
               ('sphere',{'rings':1}),('torus',{'major_radius':.1,'minor_radius':.2}),
               ('box',{'garbage':1}),('bad',{}),('box',{'width':10**1000})]
        for kind,parameters in cases:
            with self.subTest(kind=kind,parameters=parameters), self.assertRaises(RigError):
                primitive(kind,parameters)
        for mode in ('weird',None):
            with self.assertRaises(RigError): primitive('box',normal_mode=mode)

    def test_budget_rejected_before_geometry_allocation(self):
        with patch('model3d_primitives.editable_mesh',side_effect=AssertionError('must not allocate')):
            for limits in [dict(vertices=10),dict(triangles=10),dict(memory_bytes=100)]:
                with self.subTest(limits=limits), self.assertRaises(RigError):
                    primitive('sphere',dict(segments=1000000,rings=1000000),limits=limits)
        with self.assertRaises(RigError): primitive('box',normal_mode='flat',limits=dict(vertices=23))
        for value in [True,3.5,float('inf')]:
            with self.assertRaises(RigError): primitive('box',limits=dict(vertices=value))

    def test_invalid_faces_and_positions(self):
        valid=[[0,0,0],[1,0,0],[0,0,1]]
        for faces in [[],[[0,1]],[[0,0,1]],[[0,1,3]],[[0,1,True]],[[0,1,1.5]],[[0,1,-1]]]:
            with self.subTest(faces=faces),self.assertRaises(RigError):editable_mesh(valid,faces)
        for positions in [[],[[0,0,0]]*3,[[0,0,0],[1,0,0],[2,0,0]],
                          [[0,0,0],[1,float('nan'),0],[0,0,1]],[[False,0,0],[1,0,0],[0,0,1]],
                          [['0',0,0],[1,0,0],[0,0,1]],[[0,0],[1,0],[0,1]]]:
            with self.subTest(positions=positions), self.assertRaises(RigError):editable_mesh(positions,[[0,1,2]])

    def test_nonplanar_and_self_crossing_rejected(self):
        for points in [[[0,0,0],[1,0,0],[1,1,1],[0,0,1]],[[0,0,0],[1,0,1],[0,0,1],[1,0,0]]]:
            with self.assertRaises(RigError):editable_mesh(points,[[0,1,2,3]])

    def test_actual_native_glb_gltf_roundtrips(self):
        from rig_gltf import export_geometry
        from rig_import import load_character_model
        for suffix in ('glb','gltf'):
            for kind in ('box','plane','cylinder','cone','sphere','torus'):
                with self.subTest(suffix=suffix,kind=kind),tempfile.TemporaryDirectory() as tmp:
                    original=primitive(kind,name='semantic-'+kind)
                    path=Path(tmp)/('geometry.'+suffix)
                    export_geometry(original.geometry(),path)
                    before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(tmp).iterdir() if p.is_file()}
                    imported=load_character_model(path)
                    self.assertEqual(imported.geometry.vertex_count,len(original.mesh.positions))
                    self.assertEqual(imported.geometry.triangle_count,len(original.mesh.triangles))
                    self.assertEqual(imported.geometry.meshes[0].name,'semantic-'+kind)
                    np.testing.assert_allclose(imported.geometry.meshes[0].positions,original.mesh.positions,atol=1e-7)
                    self.assertIn(path,imported.source_files)
                    self.assertEqual(before,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_tiny_geometry_relative_normalization(self):
        result=primitive('box',dict(width=1e-8,height=1e-8,depth=1e-8))
        self.assertTrue(np.isfinite(result.mesh.attributes['NORMAL']).all())
        np.testing.assert_allclose(np.linalg.norm(result.mesh.attributes['NORMAL'],axis=1),1)
        self.assertEqual(len(result.mesh.triangles),12)


if __name__=='__main__':unittest.main()
