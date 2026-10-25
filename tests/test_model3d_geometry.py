"""Real primitive topology and shared GLB authority regression checks."""
import sys
import tempfile
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from model3d_geometry import box, sphere, cylinder
from rig_geometry import analyze_geometry
from rig_models import Geometry
from rig_gltf import export_geometry
from rig_import import load_character_model


class PrimitiveTests(unittest.TestCase):
    def test_topology_and_roundtrip(self):
        for factory in (box, sphere, cylinder):
            with self.subTest(factory=factory.__name__), tempfile.TemporaryDirectory() as tmp:
                mesh = factory(name='semantic-part')
                geometry = Geometry([mesh], materials=[{}])
                report = analyze_geometry(geometry)['meshes'][0]
                self.assertEqual(report['components'], 1)
                self.assertEqual(report['boundary_edges'], 0)
                self.assertEqual(report['nonmanifold_edges'], 0)
                self.assertEqual(report['degenerate_triangles'], 0)
                self.assertGreater(report['signed_volume_m3'], 0)
                np.testing.assert_allclose(np.linalg.norm(mesh.attributes['NORMAL'], axis=1), 1)
                other = factory(name='semantic-part')
                np.testing.assert_array_equal(mesh.positions, other.positions)
                np.testing.assert_array_equal(mesh.triangles, other.triangles)
                path = Path(tmp)/'primitive.glb'
                export_geometry(geometry, path)
                loaded = load_character_model(path).geometry
                self.assertEqual(loaded.triangle_count, geometry.triangle_count)
                self.assertEqual(loaded.meshes[0].name, mesh.name)
                np.testing.assert_allclose(loaded.meshes[0].positions, mesh.positions, atol=1e-7)

    def test_dimensions_and_minimum_topology(self):
        np.testing.assert_allclose(np.ptp(box(2,4,6).positions, axis=0), [2,4,6])
        for mesh in (sphere(segments=3, rings=2), cylinder(segments=3)):
            self.assertEqual(analyze_geometry(Geometry([mesh], materials=[{}]))['meshes'][0]['boundary_edges'], 0)

    def test_small_dense_sphere_normals(self):
        mesh = sphere(0.0001, segments=256, rings=256)
        radial = mesh.positions / np.linalg.norm(mesh.positions, axis=1)[:, None]
        self.assertGreater(np.einsum('ij,ij->i', radial, mesh.attributes['NORMAL']).min(), 0.999)

    def test_thin_cylinder_cap_normals(self):
        mesh = cylinder(0.0001, 10000, segments=256)
        np.testing.assert_allclose(mesh.attributes['NORMAL'][-2:], [[0, -1, 0], [0, 1, 0]], atol=1e-12)

    def test_invalid_inputs(self):
        for value in (True, 0, -1, float('nan'), float('inf'), '1', 10001, 10**1000, -(10**1000)):
            for factory in (box, sphere, cylinder):
                with self.subTest(value=value, factory=factory.__name__), self.assertRaises(ValueError):
                    factory(value)
        for value in (True, 2, 257, 3.0, '3'):
            for factory in (sphere, cylinder):
                with self.assertRaises(ValueError):
                    factory(segments=value)
        with self.assertRaises(ValueError):
            sphere(rings=1)
        with self.assertRaises(ValueError):
            box(name='')

if __name__ == '__main__':
    unittest.main()
