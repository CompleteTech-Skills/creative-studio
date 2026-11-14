"""Clip provenance stays bound to the source bytes parsed by the importer."""
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import rig_import
from rig_errors import RigError
from rig_gltf import GltfBuilder
from rig_source_snapshot import import_snapshot


class ImportSourceProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'animated.glb'
        b = GltfBuilder()
        pos = b.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3')
        times = b.accessor([[0], [1]], 'SCALAR')
        values = b.accessor([[0, 0, 0], [1, 0, 0]], 'VEC3')
        b.json.update(nodes=[{}, {'mesh': 0}], meshes=[{'primitives': [{'attributes': {'POSITION': pos}}]}],
                      scenes=[{'nodes': [0, 1]}], animations=[{'samplers': [{'input': times, 'output': values}],
                      'channels': [{'sampler': 0, 'target': {'node': 0, 'path': 'translation'}}]}])
        b.write(self.path)
        self.original = self.path.read_bytes()
        self.digest = hashlib.sha256(self.original).hexdigest()

    def assert_source(self, model):
        self.assertEqual(model.report['source_sha256'], self.digest)
        self.assertEqual(next(iter(model.clips.values()))['provenance']['input_hashes']['source'], self.digest)

    def test_swap_and_restore_during_clip_import(self):
        original_animations = rig_import._animations
        backup = self.path.with_suffix('.backup')
        replacement = self.path.with_suffix('.replacement')
        replacement.write_bytes(b'substituted source')
        swaps = []

        def swapped(document, *args):
            if document.path != self.path:
                return original_animations(document, *args)
            os.replace(self.path, backup)
            os.replace(replacement, self.path)
            swaps.append(True)
            try:
                return original_animations(document, *args)
            finally:
                os.replace(self.path, replacement)
                os.replace(backup, self.path)

        with patch.object(rig_import, '_animations', side_effect=swapped):
            self.assert_source(rig_import.load_character_model(self.path))
            with import_snapshot(self.path) as snapshot:
                self.assert_source(snapshot.model)
                self.assertEqual((snapshot.root / self.path.name).read_bytes(), self.original)
                self.assertEqual(snapshot.source_records[0]['sha256'], self.digest)
        self.assertEqual(len(swaps), 2)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_accepted_and_limit_controls(self):
        self.assert_source(rig_import.load_character_model(self.path))
        with import_snapshot(self.path) as snapshot:
            self.assert_source(snapshot.model)
        with self.assertRaises(RigError):
            import_snapshot(self.path, limits={'source_bytes': len(self.original) - 1})
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_missing_captured_digest_fails_closed(self):
        read = rig_import.read_gltf

        def missing_digest(*args, **kwargs):
            document = read(*args, **kwargs)
            document.source_sha256 = None
            return document

        with patch.object(rig_import, 'read_gltf', side_effect=missing_digest):
            with self.assertRaises(RigError) as caught:
                rig_import.load_character_model(self.path)
        self.assertEqual(caught.exception.report['code'], 'RIG_SOURCE')
        self.assertEqual(self.path.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
