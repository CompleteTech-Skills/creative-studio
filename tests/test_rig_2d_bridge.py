"""Real 3D triangle pixels enter the existing reviewed 2D import lifecycle."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

import hatch
import rig_2d_bridge as bridge
from fixture_workflow import small_profile
from rig_errors import RigError, sha256
from rig_models import rig_signature
from rig_test_helpers import fixture


class Rig2DBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        self.rig=fixture(self.home)
        self.run=self.home/'run2d';self.prepare(self.run)
        self.profile=self.run/'profile.json'

    def prepare(self, root, definition=None):
        profile=self.home/(root.name+'-profile.json');hatch.atomic(profile,definition or small_profile())
        hatch.prepare(root,argparse.Namespace(replace_inputs=False,fixture_mode=True,profile=str(profile),
            pet_name='Native triangle fixture',pet_notes='Synthetic bridge test, not production artwork.',
            reference=[],avoid=[],require=[],brand_source=[]))

    def render(self, name, job='jump', **kwargs):
        options=dict(width=96,height=96);options.update(kwargs)
        return bridge.render_bridge(self.rig.root,self.home/name,profile=self.profile,job=job,clip='bend',
            expected_rig_sha256=rig_signature(self.rig.manifest),**options)

    def base(self):
        result=self.render('base-render',job='base')
        imported=bridge.import_bridge(self.rig.root,result['path'],self.run,expected_sha256=result['sha256'],
            note='Synthetic base fixture imported to exercise review gate only.')
        self.assertTrue(imported['visual_review_required'])
        data=hatch.manifest(self.run)
        hatch.record_base_review(self.run,data,'Synthetic gate setup; no production visual approval.',True)

    def test_real_triangle_frames_import_with_exact_timing_and_portable_lineage(self):
        self.base();result=self.render('motion-render')
        info=result['manifest']
        self.assertEqual([f['time'] for f in info['frames']],[[0,1],[1,25],[11,100]])
        self.assertEqual([f['duration_ms'] for f in info['frames']],[40,70,90])
        self.assertEqual(info['visual_review'],'pending')
        frames=[]
        for record in info['frames']:
            with Image.open(Path(result['path'])/record['path']) as image:frames.append(np.asarray(image).copy())
        self.assertTrue(any(np.any(frame[:,:,3]) for frame in frames))
        self.assertTrue(np.any(frames[0]!=frames[-1]),'native joint motion must move rendered pixels')
        imported=bridge.import_bridge(self.rig.root,result['path'],self.run,expected_sha256=result['sha256'],
            note='Synthetic geometry sequence; requires separate visual review.')
        self.assertFalse(imported['imagegen_output']);self.assertEqual(imported['review_status'],'pending')
        data=hatch.manifest(self.run);job=hatch.job_for(data,'jump')
        self.assertNotIn('generation_receipt',job)
        self.assertEqual(job['import_receipt']['rig_2d_bridge']['manifest'],info)
        self.assertEqual(job['import_receipt']['rig_2d_bridge']['target_constraints']['request_sha256'],
                         sha256(self.run/'pet_request.json'))
        self.assertNotIn('.rig-2d-import-',json.dumps(job['import_receipt']))
        with Image.open(self.run/job['output_path']) as strip:
            np.testing.assert_array_equal(np.asarray(strip),np.concatenate(frames,axis=1))
        self.assertFalse(hatch.status(self.run,data)['reviewed'])
        self.assertEqual(sha256(self.profile),info['profile_sha256'])
        from portable_runs import archive_run,restore_run
        archive=self.home/'portable.zip';archive_run(self.run,data,archive)
        restored=self.home/'restored';restore_run(archive,restored)
        restored_job=hatch.job_for(hatch.manifest(restored),'jump')
        self.assertEqual(restored_job['import_receipt']['rig_2d_bridge'],job['import_receipt']['rig_2d_bridge'])
        with Image.open(restored/restored_job['output_path']) as image:
            np.testing.assert_array_equal(np.asarray(image),np.concatenate(frames,axis=1))

    def test_state_import_requires_existing_base_review(self):
        result=self.render('unreviewed-state');before=(self.run/'imagegen-jobs.json').read_bytes()
        with self.assertRaisesRegex(ValueError,'base-review'):
            bridge.import_bridge(self.rig.root,result['path'],self.run,expected_sha256=result['sha256'],note='test')
        self.assertEqual((self.run/'imagegen-jobs.json').read_bytes(),before)

    def test_frame_tampering_and_changed_rig_fail_before_import(self):
        result=self.render('tamper')
        frame=Path(result['path'])/result['manifest']['frames'][0]['path'];frame.write_bytes(b'changed')
        with self.assertRaises(RigError):bridge.verify_bridge(self.rig.root,result['path'],expected_sha256=result['sha256'])
        result=self.render('stale')
        original=(self.rig.root/'rig.json').read_bytes()
        manifest=json.loads(original);manifest['version']='1.0.99'
        (self.rig.root/'rig.json').write_text(json.dumps(manifest))
        with self.assertRaises(RigError):bridge.verify_bridge(self.rig.root,result['path'],expected_sha256=result['sha256'])
        (self.rig.root/'rig.json').write_bytes(original)

    def test_json_snapshot_accepts_a_canonical_root_reached_by_alias(self):
        root=self.home/'json-root';root.mkdir();payload=root/'value.json'
        payload.write_text('{"fixture": true}')
        alias=self.home/'json-alias'
        try:
            alias.symlink_to(root,target_is_directory=True)
        except OSError as error:
            if os.name=='nt' and getattr(error,'winerror',None)==1314:
                self.skipTest('Windows symlink creation privilege is unavailable: '+str(error))
            raise
        value,digest=bridge._captured_json(alias,'value.json',sha256(payload))
        self.assertEqual(value,{'fixture':True});self.assertEqual(digest,sha256(payload))

    def test_verification_decodes_the_same_manifest_bytes_it_authenticates(self):
        result=self.render('manifest-race');path=Path(result['path'])/'bridge.json'
        original=bridge.decode_json;changed=deepcopy(result['manifest']);changed['camera']='back'
        def replace_after_capture(payload):
            if json.loads(payload).get('kind')=='hatch-rig-2d-bridge':
                path.write_text(json.dumps(changed))
            return original(payload)
        with patch.object(bridge,'decode_json',side_effect=replace_after_capture):
            verified=bridge.verify_bridge(self.rig.root,result['path'],expected_sha256=result['sha256'])
        self.assertEqual(verified['camera'],'three-quarter')
        self.assertNotEqual(sha256(path),result['sha256'])
        with self.assertRaises(RigError):
            bridge.verify_bridge(self.rig.root,result['path'],expected_sha256=result['sha256'])

    def test_profile_duration_budget_and_stale_revision_are_rejected(self):
        with self.assertRaises(RigError):self.render('oversized',width=2048,height=2048)
        with self.assertRaises(RigError):self.render('short-clip',start_time='9/10')
        with self.assertRaises(RigError):bridge.render_bridge(self.rig.root,self.home/'stale-render',
            profile=self.profile,job='jump',clip='bend',expected_rig_sha256='0'*64)
        result=self.render('other-profile');other=self.home/'other-run'
        definition=small_profile();definition['states'][0]['durations']=[50,70,90];self.prepare(other,definition)
        with self.assertRaisesRegex(RigError,'different exact profile'):
            bridge.import_bridge(self.rig.root,result['path'],other,expected_sha256=result['sha256'],note='test')

    def test_publication_preserves_racing_destination(self):
        publish=bridge._rename_no_replace
        def race(stage,destination):
            destination.mkdir();(destination/'keep').write_bytes(b'other writer')
            publish(stage,destination)
        with patch.object(bridge,'_rename_no_replace',side_effect=race):
            with self.assertRaises(FileExistsError):self.render('racing')
        self.assertEqual((self.home/'racing/keep').read_bytes(),b'other writer')
        self.assertFalse(list(self.home.glob('.rig-2d-*')))

    def test_import_captures_bytes_before_legacy_importer_reopens_them(self):
        self.base();result=self.render('captured')
        import source_interface
        original=source_interface.import_source
        def race(root,data,job,descriptor,note):
            # The public originals change after capture; private verified bytes
            # are the source actually consumed by the legacy importer.
            for record in result['manifest']['frames']:
                (Path(result['path'])/record['path']).write_bytes(b'changed concurrently')
            return original(root,data,job,descriptor,note)
        with patch.object(source_interface,'import_source',side_effect=race):
            bridge.import_bridge(self.rig.root,result['path'],self.run,expected_sha256=result['sha256'],note='test')
        data=hatch.manifest(self.run);job=hatch.job_for(data,'jump')
        with Image.open(self.run/job['output_path']) as image:self.assertEqual(image.size,(288,96))
        self.assertEqual([f['sha256'] for f in job['import_receipt']['files']],
                         [f['sha256'] for f in result['manifest']['frames']])

    def test_malformed_frames_and_unsafe_paths_reject_with_no_2d_mutation(self):
        result=self.render('malformed');path=Path(result['path'])/'bridge.json'
        original=result['manifest'];before=(self.run/'imagegen-jobs.json').read_bytes()
        for mutation in (lambda m:m.update(frames=[None]),
                         lambda m:m['frames'][0].update(path='../escape.png'),
                         lambda m:m.update(width=10**20),
                         lambda m:m['frames'][0].update(time=[-1,1])):
            value=deepcopy(original);mutation(value);path.write_text(json.dumps(value))
            with self.assertRaises(RigError):bridge.verify_bridge(self.rig.root,path.parent,expected_sha256=sha256(path))
        self.assertEqual((self.run/'imagegen-jobs.json').read_bytes(),before)
