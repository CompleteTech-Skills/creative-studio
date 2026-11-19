"""Exercise the Model3D bridge using original GLB names retained by the registry."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from asset_library import AssetLibrary
from asset_library_import import ImportPipeline
from model3d_mesh_asset import build_asset
from model3d_mesh_import import import_canonical
from model3d_primitives import primitive
from model3d_validation import validate_file
from rig_gltf import export_geometry
from test_model3d_mesh_asset import provenance, record, source
from test_rig_asset_library_import import IDENTITY, METADATA, VALIDATOR


@unittest.skipUnless(shutil.which('node') and (VALIDATOR / 'package.json').is_file(),
                     'Requires the pinned offline Khronos glTF validator')
class OriginalFilenameBoundsTests(unittest.TestCase):
    def assert_original_roundtrip(self, filename):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / filename
            export_geometry(primitive('box').geometry(), original)
            original_bytes = original.read_bytes()
            original_hash = hashlib.sha256(original_bytes).hexdigest()
            license_file = root / 'license.txt'
            license_file.write_text('Synthetic engineering fixture. CC0-1.0.', encoding='utf-8')

            library = AssetLibrary(root / 'library', create=True)
            library.upgrade_storage()
            ref = ImportPipeline(library).import_asset(
                original, identity=IDENTITY, version='1.0.0', metadata=METADATA,
                license_file=license_file, fixture=True,
            )
            reopened = AssetLibrary(root / 'library')
            manifest = reopened.resolve(ref)
            acquired, = manifest['provenance']['source_records']
            self.assertEqual(acquired['original_filename'], filename)
            self.assertEqual(acquired['file']['path'], 'source/model.glb')
            self.assertEqual(acquired['file']['sha256'], original_hash)
            self.assertEqual(reopened.read_file(ref, 'source/model.glb'), original_bytes)

            with import_canonical(reopened, ref) as imported:
                self.assertEqual(imported.provenance['source_entry'], 'normalized.glb')
                snapshot = imported.root
                self.assertEqual((snapshot / 'source/model.glb').read_bytes(), original_bytes)
                original_record = source(record(snapshot, 'source/model.glb', 'model/gltf-binary'))
                original_record['original_filename'] = acquired['original_filename']
                canonical = snapshot / 'canonical.json'
                canonical.write_text(json.dumps(dict(canonical_ref=ref, manifest=manifest)), encoding='utf-8')
                asset = build_asset(
                    snapshot, record(snapshot, 'normalized.glb', 'model/gltf-binary'),
                    asset_id='filename-roundtrip', version='1.0.0', category='prop',
                    sources=[original_record], provenance=provenance(),
                    canonical_evidence=record(snapshot, 'canonical.json', 'application/json'),
                )
                retained, = asset['sources']
                self.assertEqual(retained['original_filename'], filename)
                self.assertEqual(retained['file']['sha256'], original_hash)
                self.assertEqual(retained['file']['bytes'], len(original_bytes))
                output = snapshot / 'asset.json'
                output.write_text(json.dumps(asset), encoding='utf-8')
                checked = validate_file(output, snapshot)
                self.assertTrue(checked['payloads_verified'])
                self.assertTrue(checked['geometry_inspected'])

            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertEqual(reopened.resolve(ref), manifest)
            self.assertEqual(reopened.read_file(ref, 'source/model.glb'), original_bytes)

    def test_ordinary_original_filename(self):
        self.assert_original_roundtrip('blue-box.glb')

    def test_129_character_original_filename(self):
        filename = 'n' * 125 + '.glb'
        self.assertEqual(len(filename), 129)
        self.assert_original_roundtrip(filename)


if __name__ == '__main__':
    unittest.main()
