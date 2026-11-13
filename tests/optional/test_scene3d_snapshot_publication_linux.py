"""Actual saved-project publication races against descriptor-owned Linux staging."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from atomic_publish import private_stage_supported
from rig_errors import RigError
from scene3d_example import build_example
from scene3d_contracts import load_project
import scene3d_snapshot


class SnapshotPublicationTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(private_stage_supported(), 'This explicit Linux gate requires descriptor staging.')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.source=self.root/'source';build_example(self.source)
        self.destination=self.root/'published'

    def during_verification(self, attack):
        original=scene3d_snapshot.load_project
        def verifying(path,*args,**kwargs):
            value=original(path,*args,**kwargs)
            if Path(path)!=self.source:attack(Path(path))
            return value
        return patch.object(scene3d_snapshot,'load_project',side_effect=verifying)

    def test_outer_wrapper_replacement_cannot_redirect_publication_or_cleanup(self):
        foreign=[]
        def attack(path):
            wrapper=path.resolve().parent
            wrapper.rename(self.root/'retained-wrapper');wrapper.mkdir()
            marker=wrapper/'UNVERIFIED.txt';marker.write_bytes(b'foreign');foreign.append(marker)
        with self.during_verification(attack):
            report=scene3d_snapshot.snapshot_project(self.source,self.destination)
        self.assertEqual(report['status'],'complete-byte-and-contract-verified')
        actual=load_project(self.destination)
        self.assertEqual(actual['inventory']['project.json']['sha256'],report['project_sha256'])
        self.assertFalse((self.destination/'UNVERIFIED.txt').exists())
        self.assertEqual(foreign[0].read_bytes(),b'foreign')

    def test_inner_project_replacement_is_rejected_and_foreign_bytes_retained(self):
        foreign=[]
        def attack(path):
            project=path.resolve();project.rename(self.root/'retained-project');project.mkdir()
            marker=project/'UNVERIFIED.txt';marker.write_bytes(b'foreign');foreign.append(marker)
        with self.during_verification(attack),self.assertRaises(RigError) as caught:
            scene3d_snapshot.snapshot_project(self.source,self.destination)
        self.assertEqual(caught.exception.report['code'],'SCENE3D_SOURCE_CHANGED')
        self.assertFalse(self.destination.exists())
        self.assertEqual(foreign[0].read_bytes(),b'foreign')
        self.assertTrue((self.root/'retained-project/project.json').is_file())

    def test_changed_verified_bytes_are_rejected_before_publication(self):
        def attack(path): (path/'triangle.obj').write_bytes(b'UNVERIFIED REPLACEMENT')
        with self.during_verification(attack),self.assertRaises(RigError):
            scene3d_snapshot.snapshot_project(self.source,self.destination)
        self.assertFalse(self.destination.exists())
        self.assertTrue((self.source/'triangle.obj').is_file())

    def test_uninventoried_injected_file_is_rejected_before_publication(self):
        def attack(path): (path/'UNVERIFIED.txt').write_bytes(b'foreign')
        with self.during_verification(attack),self.assertRaises(RigError) as caught:
            scene3d_snapshot.snapshot_project(self.source,self.destination)
        self.assertEqual(caught.exception.report['code'],'SCENE3D_SOURCE_CHANGED')
        self.assertFalse(self.destination.exists())


if __name__=='__main__':unittest.main()
