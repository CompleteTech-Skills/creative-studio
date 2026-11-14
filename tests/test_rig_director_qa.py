"""Real native geometry/motion QA through the canonical owner validators."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from director_qa import QAService
from rig_test_helpers import fixture
from test_director_qa import artifact, setup_qa, request_for


class NativeQATests(unittest.TestCase):
    def test_actual_scene_and_rig_geometry_motion(self):
        from shutil import copytree
        with tempfile.TemporaryDirectory() as temporary:
            root, project, profile = setup_qa(Path(temporary))
            copytree(Path(__file__).resolve().parents[1]/'examples/scene3d-contracts', root/'scene')
            rig = fixture(root)
            selections = [('scene', 'scene3d.project', 'scene/project.json'),
                          ('rig', 'native.rig', 'fixture/rig.json'),
                          ('motion', 'native.motion', 'fixture/rig.json')]
            profile['checks'] = [dict(id=name, operation=operation,
                artifact=artifact(root,path), options={}, scene='actual-native',shot=None,frames=None)
                for name,operation,path in selections]
            request = request_for(root, project, profile)
            value = QAService(root, project).validate(request)
            self.assertEqual(value['result']['status'],'succeeded',value['report']['findings'])
            checks = {check['id']:check for check in value['report']['checks']}
            self.assertGreater(checks['scene']['details']['files'], 1)
            self.assertEqual(checks['rig']['details']['vertices'],rig.geometry.vertex_count)
            self.assertEqual(checks['motion']['details']['clips'],len(rig.clips))
            self.assertFalse(value['report']['production_art_acceptance'])

    def test_native_fixture_cannot_be_relabeled_as_production(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, project, profile = setup_qa(Path(temporary))
            fixture(root)
            ref=artifact(root,'fixture/rig.json');ref['fixture']=False
            profile['checks']=[dict(id='rig',operation='native.rig',artifact=ref,options={},scene=None,shot=None,frames=None)]
            result=QAService(root,project).validate(request_for(root,project,profile))
            self.assertEqual(result['result']['status'],'failed')
            self.assertEqual(result['report']['findings'][0]['category'],'DIRECTOR_QA_PROVENANCE')


if __name__=='__main__': unittest.main()
