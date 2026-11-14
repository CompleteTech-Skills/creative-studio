"""Actual snapshot source revision binding before any snapshot copy or publication."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rig_errors import RigError
from scene3d_example import build_example
from scene3d_contracts import canonical_bytes, load_project
import scene3d_snapshot

class SourceRevisionSnapshots(unittest.TestCase):
    def test_expected_raw_root_hash_admits_exact_source_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';expected=build_example(source)
            digest=hashlib.sha256((source/'project.json').read_bytes()).hexdigest()
            receipt=scene3d_snapshot.snapshot_project(source,root/'published',expected_project_sha256=digest)
            self.assertEqual(load_project(root/'published')['status'],'hash-and-contract-verified')
            self.assertEqual(load_project(source)['inventory'],expected['inventory'])
            self.assertEqual(hashlib.sha256((source/'project.json').read_bytes()).hexdigest(),digest)
            self.assertEqual(receipt['status'],'complete-byte-and-contract-verified')

    def test_valid_foreign_root_rejected_before_copy_or_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';build_example(source)
            expected=hashlib.sha256((source/'project.json').read_bytes()).hexdigest()
            document=load_project(source)['project'];document['name']='Valid foreign revision'
            (source/'project.json').write_bytes(canonical_bytes(document));foreign=load_project(source)['inventory']
            with patch.object(scene3d_snapshot,'read_source_bytes',wraps=scene3d_snapshot.read_source_bytes) as reader:
                with self.assertRaises(RigError) as caught:
                    scene3d_snapshot.snapshot_project(source,root/'rejected',expected_project_sha256=expected)
                reader.assert_not_called()
            self.assertEqual(caught.exception.report['code'],'SCENE3D_SOURCE_CHANGED')
            self.assertFalse((root/'rejected').exists())
            self.assertEqual(load_project(source)['inventory'],foreign)
            self.assertEqual(sorted(p.name for p in root.iterdir()),['source'])

    def test_malformed_expected_hashes_rejected_without_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';build_example(source)
            for invalid in (True,'0'*63,'A'*64,'g'*64,[],0):
                with self.subTest(value=invalid),self.assertRaises(RigError):
                    scene3d_snapshot.snapshot_project(source,root/'rejected',expected_project_sha256=invalid)
                self.assertFalse((root/'rejected').exists())

if __name__=='__main__':unittest.main()
