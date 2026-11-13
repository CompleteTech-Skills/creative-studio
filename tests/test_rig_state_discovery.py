"""State discovery reports exact executable evidence and honest missing requirements."""
from copy import deepcopy
import tempfile
import unittest
from rig_test_helpers import fixture
from rig_animation import clip_from_poses
from rig_errors import RigError, object_hash
from rig_skeleton import neutral_pose, rotate_bone
from rig_math import axis_angle
from rig_states import map_states
from state_catalog import load_library


class StateDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.rig=fixture(self.temp.name)

    def test_real_recipe_reports_channels_skin_contacts_and_exact_source_provenance(self):
        rig=self.rig;a=neutral_pose(rig.skeleton);b=rotate_bone(rig.skeleton,a,'b1',axis_angle([0,0,1],.3))
        b['morphs']={'strip':{'blink':.7}}
        clip=clip_from_poses(rig,'inspect',[0.,1.],[a,b],contacts=[dict(schema_version=1,id='plant',
            chain='arm',start=0.,end=1.,target=[0.,2.,0.],kind='hand')])
        rig.clips['inspect']=clip
        result=map_states(rig,selected=['static-neutral'],recipes={'static-neutral':['inspect','inspect']})
        discovery=result['mapping']['static-neutral']['discovery'];actual=discovery['recipe']
        self.assertTrue(actual['validated_complete_recipe'])
        self.assertEqual(actual['required_bones'],['b0','b1','b2'])
        self.assertIn(dict(bone='b1',path='rotation'),actual['bone_channels'])
        self.assertEqual(actual['required_morph_targets'],['strip::blink'])
        self.assertEqual(actual['contact_chains'],[dict(id='arm',bones=['b0','b1','b2'])])
        self.assertTrue(actual['skinned_joints'])
        self.assertEqual(discovery['provenance_refs']['clips'],['inspect'])
        evidence=result['asset_provenance']
        self.assertEqual(evidence['rig']['source'],rig.manifest['source'])
        self.assertEqual(evidence['rig']['manifest_sha256'],object_hash(rig.manifest))
        self.assertEqual(evidence['clips']['inspect']['sha256'],object_hash(clip))
        self.assertEqual(evidence['clips']['inspect']['provenance'],clip['provenance'])
        self.assertEqual(result['work_estimate']['unique_clips'],1)
        self.assertFalse(result['all_416_animated'])

    def test_incomplete_recipe_reports_present_and_missing_without_fabricated_requirements(self):
        result=map_states(self.rig,selected=['static-neutral'],recipes={'static-neutral':['bend','missing']})
        state=result['mapping']['static-neutral'];actual=state['discovery']['recipe']
        self.assertEqual(state['status'],'requires-animation');self.assertEqual(state['clips'],[])
        self.assertEqual(actual['available_clips'],['bend']);self.assertEqual(actual['missing_clips'],['missing'])
        self.assertFalse(actual['validated_complete_recipe'])
        self.assertIsNone(actual['required_bones']);self.assertIsNone(actual['required_morph_targets'])
        self.assertEqual(state['discovery']['provenance_refs']['clips'],[])
        self.assertEqual(result['asset_provenance']['clips'],{})

    def test_anatomy_and_prop_requirements_are_catalog_data_and_approval_is_explicit(self):
        library=load_library();name=next(name for name,state in library.states.items() if state['props']['required'])
        result=map_states(self.rig,selected=[name],recipes={name:'bend'})
        discovery=result['mapping'][name]['discovery']
        self.assertEqual(discovery['catalog_requirements']['props'],library.states[name]['props'])
        self.assertEqual(discovery['compatibility']['missing_approved_props'],sorted(library.states[name]['props']['required']))
        self.assertIn('no automatic socket',discovery['recipe']['accessory_binding_status'])
        approved=map_states(self.rig,selected=[name],recipes={name:'bend'},approved_props=library.states[name]['props']['required'])
        self.assertEqual(approved['mapping'][name]['discovery']['compatibility']['missing_approved_props'],[])
        wings=map_states(self.rig,selected=['wing-fluttering'],recipes={'wing-fluttering':'bend'})
        self.assertIn('wings',wings['mapping']['wing-fluttering']['discovery']['compatibility']['missing_all'])
        self.assertIsNone(wings['mapping']['wing-fluttering']['discovery']['recipe']['required_bones'])

    def test_discovery_is_independent_copy_and_invalid_executable_channels_still_reject(self):
        rig=self.rig;before=deepcopy(rig.manifest)
        result=map_states(rig,selected=['static-neutral'],recipes={'static-neutral':'bend'})
        result['asset_provenance']['rig']['source']['origin']='changed'
        result['mapping']['static-neutral']['discovery']['catalog_requirements']['anatomy']['requires_all'].append('fake')
        self.assertEqual(rig.manifest,before)
        fresh=map_states(rig,selected=['static-neutral'],recipes={'static-neutral':'bend'})
        self.assertNotIn('fake',fresh['mapping']['static-neutral']['discovery']['catalog_requirements']['anatomy']['requires_all'])
        rig.clips['bend']['tracks'][0]['target']='missing'
        with self.assertRaises(RigError):map_states(rig,selected=['static-neutral'],recipes={'static-neutral':'bend'})
