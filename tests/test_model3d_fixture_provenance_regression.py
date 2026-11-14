"""Actual normalized GLB provenance across native import, rig and export."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from asset_library_import import SourceSnapshot, normalize_gltf
from model3d_interchange import export_mesh
from model3d_mesh_import import import_mesh
from model3d_mesh_asset import build_asset
from model3d_primitives import primitive
from rig_errors import RigError
from rig_gltf import export_geometry
from rig_import import load_character_model
from rig_jobs import create_run
from test_model3d_mesh_asset import record, provenance, source


class FixtureProvenanceRegression(unittest.TestCase):
    def test_normalized_fixture_import_rig_export_and_spoof(self):
        with tempfile.TemporaryDirectory() as tmp:
            # This fixture owns the temporary directory and explicitly admits
            # its canonical location before creating any source files.
            root = Path(tmp).resolve(strict=True)
            original = root / 'original.glb'
            export_geometry(primitive('box').geometry(), original)
            before = original.read_bytes()
            snapshot = SourceSnapshot(original, root / 'snapshot')
            normalized, _, _ = normalize_gltf(snapshot, root / 'normalized.glb', fixture=True)
            normalized_bytes = normalized.read_bytes()
            self.assertTrue(load_character_model(normalized).report['fixture'])
            rig = create_run(normalized, root / 'rig')
            self.assertTrue(rig.manifest['source']['fixture'])
            self.assertEqual(rig.manifest['source']['origin'], 'procedural-fixture')
            geometry_record = record(root, 'normalized.glb', 'model/gltf-binary')
            claim = provenance(); claim.update(fixture=False, origin='imported')
            src = source(geometry_record); src.update(provenance=claim, evidence_class='observed')
            with patch.object(Path, 'home', return_value=root), self.assertRaises(RigError):
                build_asset(root, geometry_record, asset_id='probe', version='1.0.0',
                            category='prop', sources=[src], provenance=claim)
            with import_mesh(normalized) as captured:
                self.assertTrue(captured.model.report['fixture'])
                for fmt in ('glb', 'gltf'):
                    dest = root / fmt
                    export_mesh(captured.model.geometry, dest, format=fmt, source_snapshot=captured)
                    self.assertTrue(load_character_model(dest / ('model.' + fmt)).report['fixture'])
                    self.assertEqual((dest / 'source/normalized.glb').read_bytes(), normalized_bytes)
                captured.model.report['fixture'] = False
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, root / 'spoof', source_snapshot=captured)
                self.assertFalse((root / 'spoof').exists())
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(normalized.read_bytes(), normalized_bytes)

    def test_unmarked_source_cannot_be_spoofed_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_geometry(primitive('box').geometry(), root / 'plain.glb')
            with import_mesh(root / 'plain.glb') as captured:
                captured.model.report['fixture'] = True
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, root / 'spoof', source_snapshot=captured)
                self.assertFalse((root / 'spoof').exists())

    def test_fixture_obj_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_geometry(primitive('box').geometry(), root / 'fixture.glb', provenance={'fixture': True})
            with import_mesh(root / 'fixture.glb') as captured:
                with self.assertRaises(RigError):
                    export_mesh(captured.model.geometry, root / 'obj', format='obj', source_snapshot=captured)
                self.assertFalse((root / 'obj').exists())
