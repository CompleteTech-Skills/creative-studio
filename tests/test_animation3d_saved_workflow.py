"""Actual persisted CLI workflow and independent exact body/facial clocks."""
from pathlib import Path
import tempfile
import unittest
from animation3d_saved_clip_probe import run

class SavedWorkflowTests(unittest.TestCase):
    def test_saved_cli_edit_reopen_and_independent_native_geometry(self):
        with tempfile.TemporaryDirectory() as temporary:
            proof=run(Path(temporary)/'workflow')
            self.assertTrue(proof['sources_unchanged'])
            self.assertTrue(proof['body_face_independent'])
            self.assertEqual(proof['source_vertices'],4)
            self.assertGreater(len(proof['artifact_sha256']),5)
            self.assertTrue(all(s['maximum_geometry_error_m']<proof['geometry_tolerance_m'] for s in proof['samples'].values()))
            self.assertFalse(proof['visual_approval'])

if __name__=='__main__':unittest.main()
