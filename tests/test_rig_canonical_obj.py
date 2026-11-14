"""Registry-bound OBJ publication uses the authenticated bytes and identity."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from asset_library import AssetLibrary, file_record
from asset_library_contracts import load_manifest, seal
from rig_jobs import CanonicalArtifact, create_run
from rig_errors import RigError

ROOT = Path(__file__).resolve().parents[1]

class CanonicalObjTests(unittest.TestCase):
    def test_binding_closure_and_failure_before_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            obj = root/'model.obj'; mtl = root/'materials.mtl'
            obj.write_bytes(b'mtllib materials.mtl\no triangle\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
            mtl.write_bytes(b'newmtl painted\nKd 1 1 1\n')
            license_path = ROOT/'examples/asset-library-foundation/source/license.txt'
            records = [file_record(obj, 'model.obj', 'geometry'), file_record(mtl, 'materials.mtl', 'source'),
                       file_record(license_path, 'license.txt', 'license', False)]
            manifest = load_manifest((ROOT/'examples/asset-library-foundation/manifest.json').read_bytes())
            manifest['files'] = records
            manifest['provenance']['sources'] = [records[0]]
            manifest['provenance']['license_evidence'] = [records[2]]
            manifest['publication']['inventory'] = [x['path'] for x in records]
            library = AssetLibrary(root/'library', create=True)
            ref = library.register(seal(manifest), dict(zip([x['path'] for x in records], [obj, mtl, license_path])))
            reopened = AssetLibrary(root/'library')
            rig = create_run(CanonicalArtifact(reopened, ref, entry='model.obj'), root/'rig')
            self.assertTrue(rig.manifest['source']['fixture'])
            self.assertEqual(rig.manifest['source']['sha256'], hashlib.sha256(obj.read_bytes()).hexdigest())
            self.assertEqual((rig.root/'source/model.obj').read_bytes(), obj.read_bytes())
            self.assertEqual((rig.root/'source/materials.mtl').read_bytes(), mtl.read_bytes())
            self.assertEqual(rig.geometry.triangle_count, 1)
            for bad in (None, {**ref, 'id': 'foreign'}, {**ref, 'sha256': 'f'*64}):
                with self.subTest(bad=bad), self.assertRaises((RigError, TypeError)):
                    create_run(CanonicalArtifact(reopened, bad, entry='model.obj'), root/'bad')
            with patch.object(reopened, 'resolve', return_value={**manifest, 'version': '2.0.0'}), \
                 patch('model3d_mesh_import._load') as decode:
                with self.assertRaises(RigError):
                    create_run(CanonicalArtifact(reopened, ref, entry='model.obj'), root/'manifest-tampered')
                decode.assert_not_called()
            with patch.object(reopened, 'read_file', side_effect=lambda r, name: b'tampered' if name == 'materials.mtl' else library.read_file(r, name)), \
                 patch('model3d_mesh_import._load') as decode:
                with self.assertRaises(RigError):
                    create_run(CanonicalArtifact(reopened, ref, entry='model.obj'), root/'tampered')
                decode.assert_not_called()
            self.assertFalse((root/'tampered').exists())
            bare = create_run(obj, root/'bare')
            self.assertFalse(bare.manifest['source']['fixture'])
