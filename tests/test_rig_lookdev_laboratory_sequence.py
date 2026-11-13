"""History-independent canonical fixtures and boundary regression tests."""
import sys
from pathlib import Path
import unittest
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev_laboratory_sequence import canonical_fixture_triangles,frame_parameters,frame_documents,light_document,material_document,inspect_mesh_assignment
from lookdev_contracts import Registry,canonical_bytes
from lookdev_blender import material_plan
from lookdev_lighting import light_plan
from rig_errors import RigError

class SequenceTests(unittest.TestCase):
    def test_fixture_faces_keep_exact_topology_across_loop_roots_and_face_order(self):
        expected=canonical_fixture_triangles([[0,1,2,3],[4,5,6]])
        self.assertEqual(expected,canonical_fixture_triangles([[6,4,5],[2,3,0,1]]))
        self.assertNotEqual(expected,canonical_fixture_triangles([[0,3,2,1],[4,5,6]]))
        for invalid in ([[1,1,2]],[[0,1]],[[-1,0,1]],[[True,1,2]]):
            with self.assertRaises(RigError):canonical_fixture_triangles(invalid)

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

    def test_actual_face_binding_rejects_labels_without_correct_material(self):
        expected={'sha256':'correct-plan','effective_sha256':'correct-effective'}
        material={'hatch_plan_sha256':'wrong-plan','hatch_effective_sha256':'correct-effective'}
        obj=SimpleNamespace(type='MESH',data=SimpleNamespace(
            polygons=[SimpleNamespace(material_index=0)],materials=[material]))
        with self.assertRaises(RigError):inspect_mesh_assignment(obj,expected)
        material['hatch_plan_sha256']='correct-plan'
        obj.data.polygons[0].material_index=1
        with self.assertRaises(RigError):inspect_mesh_assignment(obj,expected)
        obj.data.polygons=[]
        with self.assertRaises(RigError):inspect_mesh_assignment(obj,expected)

    def test_documents_do_not_alias_shared_palette_or_transform(self):
        value=material_document('body');other=material_document('body')
        value['surface']['base_color'][0]=1
        self.assertNotEqual(value,other)
        a=light_document();b=light_document();a['transform']['translation_meters'][0]=30
        self.assertNotEqual(a,b)

if __name__=='__main__':unittest.main()
