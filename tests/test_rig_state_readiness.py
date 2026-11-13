"""Real tiny native jobs must not confuse state discovery with animation readiness."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from rig_test_helpers import fixture
from rig_errors import RigError, atomic_json, read_json, sha256
from rig_jobs import save_rig, write_rig_manifest
from rig_models import load_rig
from rig_operations import mutate
from rig_validation import validate_rig


class RigStateReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.rig=fixture(self.temp.name)

    def mapping(self, *, recipes=None):
        return mutate(self.rig.root,'state-map',dict(selected=['static-neutral'],
            recipes={'static-neutral':'neutral'} if recipes is None else recipes))

    def status(self):return read_json(self.rig.root/'job.json')['status']

    def test_no_skins_or_clips_retains_skeleton_stage_and_missing_animation_report(self):
        self.rig.weights={};self.rig.clips={};save_rig(self.rig,operation='fixture-unbound')
        response=self.mapping()['result']
        self.assertEqual(response['mapping']['static-neutral']['status'],'requires-animation')
        self.assertEqual(response['job_status'],'skeleton-generated')
        self.assertEqual(self.status(),'skeleton-generated')
        self.assertFalse(response['readiness']['report']['ok'])
        self.assertEqual(response['readiness']['clip_ids'],[])
        self.assertEqual(load_rig(self.rig.root).weights,{})

    def test_valid_skin_but_missing_requested_clip_is_only_structurally_valid(self):
        response=self.mapping(recipes={'static-neutral':'not-authored'})['result']
        self.assertEqual(response['mapping']['static-neutral']['status'],'requires-animation')
        self.assertTrue(response['readiness']['report']['ok'])
        self.assertEqual(self.status(),'structurally-valid')
        self.assertEqual(response['readiness']['executable_recipes'],0)

    def test_static_recipe_is_ready_but_cannot_preserve_late_stage_job_labels(self):
        # Deliberately stale lifecycle labels, not fabricated visual receipts.
        # The source remains an explicitly unapproved engineering fixture.
        self.rig.manifest['state_mapping']['idle']=dict(status='supported',clips=['neutral'],reason='fixture prior mapping')
        write_rig_manifest(self.rig.root,self.rig.manifest)
        for index, prior in enumerate(('exported','review-approved')):
            job=read_json(self.rig.root/'job.json');job['status']=prior;atomic_json(self.rig.root/'job.json',job)
            before=read_json(self.rig.root/'rig.json')['revision']
            recipe='neutral' if index==0 else ['neutral','neutral']
            response=self.mapping(recipes={'static-neutral':recipe})['result'];loaded=load_rig(self.rig.root)
            self.assertEqual(response['job_status'],'animation-ready');self.assertEqual(self.status(),'animation-ready')
            self.assertEqual(response['mapping']['static-neutral']['status'],'needs-review')
            self.assertEqual(loaded.manifest['state_mapping']['idle']['status'],'needs-review')
            self.assertEqual(loaded.manifest['review']['status'],'pending')
            self.assertEqual(loaded.manifest['revision'],before+1)
            self.assertLess(response['readiness']['report']['metrics']['max_vertex_displacement_m'],1e-12)
            self.assertFalse(response['readiness']['visual_approval'])
            self.assertFalse(response['readiness']['full_package_acceptance'])
            descriptor=loaded.manifest['validation'][-1]
            self.assertEqual(sha256(loaded.root/descriptor['path']),descriptor['sha256'])
            self.assertEqual(read_json(loaded.root/descriptor['path']),response['readiness'])

    def test_invalid_existing_bindings_report_needs_correction(self):
        self.rig.weights['strip'].weights*=.5;save_rig(self.rig,operation='fixture-invalid-binding')
        response=self.mapping()['result']
        self.assertFalse(response['readiness']['report']['ok'])
        self.assertEqual(response['readiness']['executable_recipes'],1)
        self.assertEqual(self.status(),'needs-correction')
        self.assertNotEqual(self.status(),'animation-ready')

    def test_only_deduplicated_selected_recipe_clips_are_sampled(self):
        bad=deepcopy(self.rig.clips['bend']);bad['id']='unselected-bad';bad['tracks'][0]['target']='missing'
        self.rig.clips['unselected-bad']=bad;save_rig(self.rig,operation='fixture-unselected-bad')
        self.assertFalse(validate_rig(self.rig,require_animation=False)['ok'])
        response=self.mapping(recipes={'static-neutral':['neutral','neutral']})['result']
        self.assertEqual(response['readiness']['clip_ids'],['neutral'])
        self.assertEqual(response['readiness']['report']['metrics']['clips'],1)
        self.assertEqual(response['readiness']['scope'],'selected-recipes-only')
        self.assertEqual(self.status(),'animation-ready')
        self.assertFalse(response['readiness']['full_package_acceptance'])

    def test_selected_unsatisfied_contact_orientation_is_not_animation_ready(self):
        from rig_math import axis_angle
        self.rig.manifest['controllers']['constraints']=[dict(schema_version=1,id='hand-limit',
            bone='b2',type='hinge',axis=[1,0,0],minimum=0.,maximum=.2)]
        self.rig.clips['neutral']['contacts']=[dict(schema_version=1,id='hold',chain='arm',start=0.,end=1.,
            target=[0.,2.,0.],orientation=axis_angle([1,0,0],1.2).tolist(),kind='hand')]
        save_rig(self.rig,operation='fixture-blocked-contact-orientation')
        response=self.mapping()['result']
        self.assertEqual(self.status(),'needs-correction')
        self.assertAlmostEqual(response['readiness']['report']['metrics']['max_contact_orientation_error_rad'],1.)
        self.assertFalse(response['readiness']['report']['ok'])

    def test_failed_selected_admission_preserves_active_rig_and_mapping(self):
        bad=deepcopy(self.rig.clips['bend']);bad['id']='bad';bad['tracks'][0]['target']='missing'
        self.rig.clips['bad']=bad;save_rig(self.rig,operation='fixture-bad-selected')
        active=(self.rig.root/'rig.json').read_bytes()
        with self.assertRaises(RigError):self.mapping(recipes={'static-neutral':'bad'})
        self.assertEqual((self.rig.root/'rig.json').read_bytes(),active)
        self.assertEqual(self.status(),'failed')
        self.assertEqual(read_json(self.rig.root/'job.json')['steps']['state-map']['status'],'failed')

    def test_artifact_failure_after_staged_save_does_not_publish_revision(self):
        active=(self.rig.root/'rig.json').read_bytes()
        with patch('rig_operations.retain_artifact',side_effect=RuntimeError('fixture artifact failure')):
            with self.assertRaises(RigError):self.mapping()
        self.assertEqual((self.rig.root/'rig.json').read_bytes(),active)
        self.assertEqual(self.status(),'failed')
        self.assertEqual(load_rig(self.rig.root).manifest['state_mapping'],{})


if __name__=='__main__':unittest.main()
