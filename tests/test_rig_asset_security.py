"""Real untrusted payload negatives through existing asset admission authorities.

These are additional security regressions, not full issue152 acceptance.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from asset_library import AssetLibrary
from asset_library_import import ImportPipeline, _gltf_json
from rig_errors import RigError
from rig_gltf import export_gltf
from rig_test_helpers import fixture

ROOT = Path(__file__).resolve().parents[1]


class AssetSecurityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        run = fixture(self.root)
        source = self.root / 'original.glb'
        export_gltf(run, source)
        self.document, binary = _gltf_json(source.read_bytes(), 'glb')
        self.document['buffers'][0]['uri'] = 'geometry.bin'
        self.buffer = self.root / 'geometry.bin'
        self.buffer.write_bytes(binary[:self.document['buffers'][0]['byteLength']])
        self.license = self.root / 'license.txt'
        self.license.write_text('Procedural engineering fixture CC0.\n')
        template = json.loads((ROOT / 'examples/asset-library-foundation/manifest.json').read_bytes())
        self.request = dict(identity=template['identity'], version='1.0.0', metadata=template['metadata'],
                            license_file=self.license, fixture=True, blender_roundtrip=False)
        self.library = AssetLibrary(self.root / 'library', create=True)
        self.pipeline = ImportPipeline(self.library)

    def reject(self, document, expected_codes):
        source = self.root / 'hostile.gltf'
        source.write_text(json.dumps(document))
        originals = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (source, self.buffer, self.license)}
        with self.assertRaises(RigError) as raised:
            self.pipeline.import_asset(source, **self.request)
        self.assertIn(raised.exception.report['code'], expected_codes)
        self.assertEqual(self.library.list_references(), [])
        self.assertEqual(originals, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in originals})

    def test_valid_fixture_import_decodes_and_reopens_real_geometry(self):
        source = self.root / 'valid.gltf'
        source.write_text(json.dumps(self.document))
        result = self.pipeline.import_asset(source, **self.request)
        self.assertEqual(result['report']['native_observation']['vertices'], 4)
        self.assertEqual(self.library.list_references(), [result['reference']])
        self.assertEqual(self.library.resolve(result['reference'])['publication']['verification'], 'payload-verified')

    def test_malformed_accessor_offsets_counts_and_ranges_never_publish(self):
        mutations = [
            lambda d: d['accessors'][0].update(byteOffset=2**40),
            lambda d: d['accessors'][0].update(count=2**40),
            lambda d: d['accessors'][0].update(bufferView=2**20),
            lambda d: d['accessors'][0].update(componentType=9999),
            lambda d: d['bufferViews'][0].update(byteOffset=2**40),
            lambda d: d['bufferViews'][0].update(byteLength=2**40),
        ]
        expected = ['LIBRARY_IMPORT_LIMIT', 'LIBRARY_IMPORT_LIMIT', 'RIG_GLTF_REFERENCE',
                    'RIG_GLTF_ACCESSOR', 'LIBRARY_IMPORT_LIMIT', 'LIBRARY_IMPORT_LIMIT']
        for number, mutate in enumerate(mutations):
            with self.subTest(mutation=number):
                document = deepcopy(self.document)
                mutate(document)
                self.reject(document, {expected[number]})

    def test_remote_encoded_and_platform_escape_uris_never_publish(self):
        for uri in ('//server/share.bin', 'https://invalid.example/geometry.bin',
                    'file:///etc/passwd', '%2e%2e/geometry.bin', '%2Fetc/passwd',
                    'C%3a/geometry.bin', '..%5cgeometry.bin', 'geometry.bin?secret=1',
                    'geometry.bin#fragment', 'data:application/octet-stream;base64,%%%'):
            with self.subTest(uri=uri):
                document = deepcopy(self.document)
                document['buffers'][0]['uri'] = uri
                self.reject(document, {'LIBRARY_IMPORT_URI', 'LIBRARY_IMPORT_PATH'})

    def test_actual_symlinked_dependency_never_discloses_or_publishes(self):
        secret = self.root / 'outside-secret.bin'
        secret.write_bytes(self.buffer.read_bytes())
        self.buffer.unlink()
        try:
            self.buffer.symlink_to(secret)
        except OSError as exc:
            if os.name != 'nt':
                raise
            self.skipTest('OS symlink creation unavailable: ' + str(exc))
        self.reject(self.document, {'LIBRARY_SYMLINK'})
        self.assertEqual(secret.read_bytes(), self.buffer.read_bytes())

    @unittest.skipUnless(os.name == 'nt', 'Actual Windows junction test runs on Windows CI.')
    def test_actual_windows_junction_dependency_never_publishes(self):
        import subprocess
        outside = self.root / 'outside-directory'
        outside.mkdir()
        secret = outside / 'geometry.bin'
        secret.write_bytes(self.buffer.read_bytes())
        junction = self.root / 'junction'
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(outside)],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        document = deepcopy(self.document)
        document['buffers'][0]['uri'] = 'junction/geometry.bin'
        before = hashlib.sha256(secret.read_bytes()).hexdigest()
        self.reject(document, {'LIBRARY_SYMLINK'})
        self.assertEqual(hashlib.sha256(secret.read_bytes()).hexdigest(), before)
