"""History-independent canonical fixtures and boundary regression tests."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev_laboratory_sequence import frame_parameters,frame_documents,light_document,material_document
from lookdev_contracts import Registry,canonical_bytes
from lookdev_blender import material_plan
from lookdev_lighting import light_plan
from rig_errors import RigError

class SequenceTests(unittest.TestCase):
    def test_frames_are_bounded_integer_absolute_evaluations(self):
        expected=frame_documents(3)
        for frame in (7,0,6,1,5,2,4):frame_documents(frame)
        self.assertEqual(frame_documents(3),expected)
        for bad in (-1,8,1.5,True,None):
            with self.subTest(frame=bad),self.assertRaises(RigError):frame_parameters(bad)

    def test_all_eight_frames_have_real_admissible_material_and_light_changes(self):
        document_hashes=set();references=set()
        for frame in range(8):
            state,body,light=frame_documents(frame)
            registry=Registry([('body.json',canonical_bytes(body)),('light.json',canonical_bytes(light))])
            material=material_plan(registry,registry.reference(body['id'],body['version']))
            plan=light_plan(registry,registry.reference(light['id'],light['version']))
            self.assertEqual(material['surface']['emission']['strength_watts_per_square_meter'],0 if frame<4 else 3)
            self.assertEqual(plan['energy'],state['watts'])
            document_hashes.add(plan['sha256']);references.add((light['id'],light['version']))
        self.assertEqual(len(document_hashes),8);self.assertEqual(len(references),8)

    def test_documents_do_not_alias_shared_palette_or_transform(self):
        value=material_document('body');other=material_document('body')
        value['surface']['base_color'][0]=1
        self.assertNotEqual(value,other)
        a=light_document();b=light_document();a['transform']['translation_meters'][0]=30
        self.assertNotEqual(a,b)

if __name__=='__main__':unittest.main()
