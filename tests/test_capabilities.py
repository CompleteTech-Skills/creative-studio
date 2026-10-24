"""New contracts and failure paths. All assets are synthetic, all destinations local."""
import argparse
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from PIL import Image, ImageDraw
from fixture_workflow import build_run, small_profile, ingest_fixture, record_fixture_review, hatch
from asset_contracts import check_image, check_resources, generation_projection, validate_identity
from extraction_annotations import annotated_source
import extract_strip_frames as extraction
import portable_runs
from portable_runs import archive_run, restore_run, migrate, verify_archive
from source_interface import import_source
from profile_spec import load_profile
from review_ui import build_review


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hatch-capabilities-');self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name);self.root=self.home/'run'

    def build(self, validate=False, profile=None):
        self.data=build_run(self.root,profile,validate=validate);return self.data

    def directory_symlink(self, link, target):
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            if os.name == 'nt' and getattr(error, 'winerror', None) == 1314:
                self.skipTest('Windows symlink creation privilege is unavailable: ' + str(error))
            raise

    def test_import_origin_is_distinct_and_source_still_gets_reviewed(self):
        self.build();hatch.repair(self.root,self.data,'jump','Import the approved existing strip.')
        descriptor=self.home/'source.json'
        hatch.atomic(descriptor,{'schema_version':1,'kind':'import','layout':'strip','files':['fixture-sources/jump.png'],
            'origin':'User-owned synthetic example, supplied for import', 'fixture':True})
        result=import_source(self.root,self.data,'jump',descriptor,'Viewed the supplied strip.')
        job=hatch.job_for(self.data,'jump')
        self.assertFalse(result['imagegen_output']);self.assertIn('import_receipt',job)
        self.assertNotIn('generation_receipt',job);self.assertEqual(job['source_origin'],'import')
        self.assertNotIn('model_call',job)
        hatch.validate(self.root,self.data)
        with self.assertRaisesRegex(ValueError,'visual review'):hatch.package(self.root,self.data,self.home/'unreviewed-package')
        record_fixture_review(self.root,self.data);self.assertTrue(hatch.status(self.root,self.data)['reviewed'])

    def test_unsupported_and_malformed_imports_preserve_claim(self):
        self.build();hatch.repair(self.root,self.data,'jump','Import test.')
        path=self.home/'bad.json';hatch.atomic(path,{'schema_version':1,'kind':'import','layout':'video','files':['x.mp4'],'origin':'fixture','fixture':True})
        with self.assertRaisesRegex(ValueError,'unsupported source layout'):import_source(self.root,self.data,'jump',path,'note')
        handoff=hatch.claim(self.root,self.data,'jump')
        with self.assertRaisesRegex(ValueError,'active generation token'):import_source(self.root,self.data,'jump',path,'note')
        self.assertEqual(hatch.job_for(self.data,'jump')['token'],handoff['token'])

    def test_import_frames_do_not_resize_each_pose(self):
        self.build();hatch.repair(self.root,self.data,'jump','Import frames.')
        Image.new('RGBA',(10,10),'red').save(self.home/'a.png');Image.new('RGBA',(11,10),'red').save(self.home/'b.png')
        path=self.home/'bad.json';hatch.atomic(path,{'schema_version':1,'kind':'import','layout':'frames','files':['a.png','b.png','a.png'],'origin':'fixture','fixture':True})
        with self.assertRaisesRegex(ValueError,'share one viewport'):import_source(self.root,self.data,'jump',path,'note')

    def test_structured_identity_roles_and_constraints_survive_retries(self):
        image=self.home/'front.png';Image.new('RGBA',(20,20),'red').save(image)
        identity={'schema_version':1,'markings':['left ear white'],'proportions':{'head_to_body':0.6},
            'palette':{'coat':'#B43C28'},'materials':['felt'],'approved_props':['cup'],'asymmetries':['white tip only on left'],
            'directions':{'rest':'front'},'forbid_mirror':True}
        path=self.home/'identity.json';hatch.atomic(path,identity)
        roles=self.home/'roles.json';hatch.atomic(roles,[{'role':'front','note':'Approved front silhouette'}])
        p=self.home/'profile.json';hatch.atomic(p,small_profile())
        hatch.prepare(self.root,argparse.Namespace(replace_inputs=False,fixture_mode=True,profile=str(p),identity=str(path),reference_roles=str(roles),
            pet_name='test',pet_notes='fixture',reference=[str(image)],avoid=['glitter'],require=['cup'],brand_source=[]))
        data=hatch.manifest(self.root);handoff=hatch.claim(self.root,data,'base')
        self.assertEqual(handoff['input_images'][0]['role'],'front')
        hatch.fail(self.root,data,'base',handoff['token'],'synthetic failure')
        retried=hatch.claim(self.root,data,'base')
        self.assertEqual(retried['identity']['asymmetries'],identity['asymmetries'])
        self.assertEqual(retried['input_images'],handoff['input_images'])
        hatch.fail(self.root,data,'base',retried['token'],'synthetic failure')
        hatch.repair(self.root,data,'base','Keep left ear white.')
        prompt=(self.root/'prompts/base-pet.md').read_text()
        self.assertIn('left ear white',prompt);self.assertIn('glitter',prompt);self.assertIn('Keep left ear white',prompt)

    def test_identity_conflicts_are_explicit(self):
        with self.assertRaisesRegex(ValueError,'unresolved identity'):validate_identity({'schema_version':1,'unresolved_conflicts':['front says blue; side says red']},small_profile())
        with self.assertRaisesRegex(ValueError,'positive finite'):validate_identity({'schema_version':1,'proportions':{'head':float('nan')}},small_profile())
        with self.assertRaisesRegex(ValueError,'contradicts'):validate_identity({'schema_version':1,'directions':{'running-left':'right'}},load_profile())

    def test_exact_component_annotation_and_invalid_overrides(self):
        image=Image.new('RGBA',(120,50));d=ImageDraw.Draw(image)
        d.rectangle((5,10,24,35),fill='red');d.rectangle((35,10,54,35),fill='blue')
        d.rectangle((70,10,99,35),fill='red')
        with self.assertRaisesRegex(ValueError,'extra complete pose'):extraction.analyze_slots(image,2)
        annotation={'schema_version':1,'source_sha256':'a'*64,'author':'test','note':'Two intended disconnected pieces in frame zero.',
            'frames':[{'rect':[0,0,60,50],'allow_components':[{'bounds':[35,10,55,36],'permit':'disconnected','reason':'separate approved component'}]}, {'rect':[60,0,120,50]}]}
        normalized,overrides=annotated_source(image,2,annotation,'a'*64)
        groups,evidence=extraction.analyze_slots(normalized,2,False,overrides)
        self.assertEqual(evidence['slots'][0]['major_components'],2)
        self.assertEqual(len(groups[0]),2)
        for change,message in [(lambda a:a.update(source_sha256='other'),'exact source'),
             (lambda a:a['frames'][0].update(rect=[0,0,45,50]),'component permission'),
             (lambda a:a['frames'][0]['allow_components'][0].update(permit='all'),'component permission'),
             (lambda a:a['frames'][1].update(rect=[59,0,120,50]),'overlap')]:
            bad=copy.deepcopy(annotation);change(bad)
            with self.subTest(message=message),self.assertRaisesRegex(ValueError,message):annotated_source(image,2,bad,'a'*64)

    def test_rejected_extraction_writes_diagnostic(self):
        source=self.home/'source.png';image=Image.new('RGBA',(192,96));d=ImageDraw.Draw(image)
        d.rectangle((10,30,40,70),fill='red');d.rectangle((60,30,85,70),fill='red');d.rectangle((120,30,150,70),fill='red');image.save(source)
        p=small_profile();p['states']=p['states'][:1];p['states'][0].update(frames=2,durations=[100,100]);p['layout']['rows']=1;p['layout']['height']=96;p['events']={}
        with self.assertRaisesRegex(ValueError,'extra complete pose'):extraction.extract_state(source,'rest',self.home/'frames',(0,255,0),48,'auto',p)
        self.assertTrue((self.home/'qa/extraction-diagnostics/rest.png').is_file())
        with Image.open(self.home/'qa/extraction-diagnostics/rest.png') as preview:
            self.assertGreaterEqual(preview.width,480)
        self.assertIn('extra complete pose',hatch.read(self.home/'qa/extraction-diagnostics/rest.json')['error'])

    def test_nearest_resampling_preserves_palette_body_scale_and_jump(self):
        source=Image.new('RGBA',(64,32));d=ImageDraw.Draw(source)
        d.rectangle((8,16,15,23),fill='#E02030');d.rectangle((40,6,47,13),fill='#E02030')
        frames,info=extraction.render_slots(source,2,(32,32),scale=.5,padding=0,resampling='nearest')
        self.assertEqual(set(extraction.pixels(frames[0])),{(0,0,0,0),(224,32,48,255)})
        self.assertEqual(frames[0].getbbox()[3]-frames[0].getbbox()[1],frames[1].getbbox()[3]-frames[1].getbbox()[1])
        self.assertEqual(frames[0].getbbox()[1]-frames[1].getbbox()[1],5)
        self.assertIn('baseline_pixels',info)
        with self.assertRaisesRegex(ValueError,'unsupported resampling'):extraction.render_slots(source,2,resampling='magic')

    def test_calibration_only_change_reuses_generation(self):
        self.build();before=copy.deepcopy(self.data['jobs'])
        result=hatch.configure(self.root,self.data,argparse.Namespace(shared_scale=.5,chroma_key=None))
        self.assertFalse(result['model_call_required'])
        self.assertEqual([j['status'] for j in self.data['jobs']],['generated']*3)
        self.assertEqual([j['generation_receipt'] for j in self.data['jobs']],[j['generation_receipt'] for j in before])
        self.assertFalse(hatch.base_review_current(self.root,self.data))
        history=[e for e in self.data['history'] if e['event']=='extraction-configured'][-1]
        self.assertEqual(history['current']['shared_scale'],.5)
        self.assertIn('note',history['previous_base_review'])

    def test_optional_mirror_does_not_create_generation_dependency(self):
        profile=small_profile();profile['states'][1]['allowed_derivations']=[{'kind':'horizontal-mirror','source':'rest','requires_explicit_approval':True}]
        self.build(profile=profile)
        result=hatch.repair(self.root,self.data,'rest','Repair an independent source.')
        self.assertEqual(result['pending'],['rest']);self.assertEqual(hatch.job_for(self.data,'jump')['status'],'generated')
        ingest_fixture(self.root,self.data,'rest',self.home/'fixture-sources/rest.png')
        hatch.repair(self.root,self.data,'jump','Derive explicitly.')
        hatch.derive(self.root,self.data,'jump','Symmetric fixture with no sided props.',True)
        self.assertEqual(hatch.job_for(self.data,'jump')['source_origin'],'derived')
        self.assertNotIn('model_call',hatch.job_for(self.data,'jump'))
        self.assertEqual(hatch.repair(self.root,self.data,'rest','Change actual derivation input.')['pending'],['jump','rest'])

    def test_playback_and_export_revisions_reuse_extraction(self):
        self.build(validate=True);p=load_profile(self.root/'profile.json');p['states'][0]['durations']=[60,80,100]
        p['export']['formats']=['frames','browser']
        path=self.home/'revised.json';hatch.atomic(path,p)
        before=[j['output_hash'] for j in self.data['jobs']]
        result=hatch.revise_profile(self.root,self.data,path)
        self.assertEqual(result['generation_stale'],[]);self.assertEqual(result['extraction_stale'],[])
        validated=hatch.validate(self.root,self.data)
        self.assertEqual(validated['extracted_states'],[])
        self.assertEqual(before,[j['output_hash'] for j in self.data['jobs']])

    def test_portable_archive_restores_exact_review_and_identity(self):
        self.build(validate=True);record_fixture_review(self.root,self.data)
        path=self.home/'archive.zip';archive_run(self.root,self.data,path)
        restored=self.home/'different'/'restored';result=restore_run(path,restored)
        self.assertFalse(result['revalidation_required'])
        data=hatch.manifest(restored)
        self.assertEqual(data['run_id'],self.data['run_id']);self.assertEqual(data['jobs'],self.data['jobs'])
        self.assertTrue(hatch.status(restored,data)['reviewed'])
        self.assertEqual(hatch.read(restored/'pet_request.json'),hatch.read(self.root/'pet_request.json'))

    def test_restore_rejects_dangling_destination_symlink_before_extraction(self):
        self.build(validate=True)
        record_fixture_review(self.root, self.data)
        archive = self.home / 'archive.zip'
        archive_run(self.root, self.data, archive)
        destination = self.home / 'restored'
        target = self.home / 'missing-target'
        self.directory_symlink(destination, target)
        before = destination.lstat()
        linked_target = destination.readlink()

        with patch.object(portable_runs.tempfile, 'mkdtemp', wraps=tempfile.mkdtemp) as create_stage:
            with self.assertRaisesRegex(ValueError, 'destination must not exist'):
                restore_run(archive, destination)
        create_stage.assert_not_called()
        self.assertTrue(destination.is_symlink())
        after = destination.lstat()
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertEqual(destination.readlink(), linked_target)
        self.assertFalse(target.exists())
        self.assertFalse(list(self.home.glob('.hatch-restore-*')))

    def test_restore_cli_rejects_dangling_destination_symlink(self):
        self.build(validate=True)
        record_fixture_review(self.root, self.data)
        archive = self.home / 'archive.zip'
        archive_run(self.root, self.data, archive)
        destination = self.home / 'restored'
        target = self.home / 'missing-target'
        self.directory_symlink(destination, target)
        before = destination.lstat()
        linked_target = destination.readlink()
        script = Path(__file__).resolve().parents[1] / 'scripts/hatch.py'

        process = subprocess.run([sys.executable, str(script), 'restore', '--source', str(archive),
                                  '--run-dir', str(destination)], capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn('restore destination must not exist', process.stderr)
        self.assertTrue(destination.is_symlink())
        after = destination.lstat()
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertEqual(destination.readlink(), linked_target)
        self.assertFalse(target.exists())
        self.assertFalse((self.home / '.missing-target.hatch.lock').exists())
        self.assertFalse(list(self.home.glob('.hatch-restore-*')))

    def test_restore_cli_preserves_leaf_link_racing_ownership(self):
        self.build(validate=True)
        record_fixture_review(self.root, self.data)
        archive = self.home / 'archive.zip'
        archive_run(self.root, self.data, archive)
        # Match the CLI's parent-only normalization (macOS /var aliases /private/var).
        destination = self.home.resolve() / 'restored'
        parent_alias = self.home / 'destination-parent-alias'
        self.directory_symlink(parent_alias, self.home.resolve())
        target_parent = self.home / 'unrequested-parent'
        target = target_parent / 'target'
        probe = self.home / 'symlink-capability'
        self.directory_symlink(probe, target)
        probe.unlink()
        original_is_symlink = Path.is_symlink
        created = []

        def race(candidate):
            result = original_is_symlink(candidate)
            if candidate == destination and not created:
                self.assertFalse(result)
                destination.symlink_to(target, target_is_directory=True)
                status = destination.lstat()
                created.append((status.st_dev, status.st_ino, destination.readlink()))
            return result

        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ['hatch.py', 'restore', '--source', str(archive), '--run-dir', str(parent_alias / destination.name)]
        with patch.object(sys, 'argv', argv), patch.object(Path, 'is_symlink', race), \
                patch.object(hatch, 'recover_prepare', side_effect=AssertionError('restore must not recover prepare')), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as error:
                hatch.main()
        self.assertEqual(error.exception.code, 1)
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('restore destination must not exist', stderr.getvalue())
        self.assertEqual(len(created), 1)
        status = destination.lstat()
        self.assertEqual((status.st_dev, status.st_ino, destination.readlink()), created[0])
        self.assertFalse(target_parent.exists())
        self.assertFalse(target.exists())
        self.assertFalse(list(self.home.glob('.hatch-restore-*')))

    def test_restore_cli_leaves_unrelated_prepare_journal_untouched(self):
        self.build(validate=True)
        record_fixture_review(self.root, self.data)
        archive = self.home / 'archive.zip'
        archive_run(self.root, self.data, archive)
        destination = self.home / 'restored'
        journal = self.home / '.restored.prepare.json'
        journal.write_bytes(b'unrelated interrupted prepare journal')
        argv = ['hatch.py', 'restore', '--source', str(archive), '--run-dir', str(destination)]
        stdout = io.StringIO()
        with patch.object(sys, 'argv', argv), contextlib.redirect_stdout(stdout):
            hatch.main()
        self.assertEqual(journal.read_bytes(), b'unrelated interrupted prepare journal')
        self.assertEqual(hatch.manifest(destination)['run_id'], self.data['run_id'])
        self.assertFalse(json.loads(stdout.getvalue())['revalidation_required'])

    def test_restore_preserves_destination_symlink_created_after_initial_check(self):
        self.build(validate=True)
        record_fixture_review(self.root, self.data)
        archive = self.home / 'archive.zip'
        archive_run(self.root, self.data, archive)
        destination = self.home / 'restored'
        target = self.home / 'missing-target'
        probe = self.home / 'symlink-capability'
        self.directory_symlink(probe, target)
        probe.unlink()
        original_is_symlink = Path.is_symlink
        created = []

        def race(candidate):
            result = original_is_symlink(candidate)
            if candidate == destination and not created:
                self.assertFalse(result)
                destination.symlink_to(target, target_is_directory=True)
                status = destination.lstat()
                created.append((status.st_dev, status.st_ino, destination.readlink()))
            return result

        with patch.object(Path, 'is_symlink', race):
            with self.assertRaises(FileExistsError):
                restore_run(archive, destination)
        self.assertEqual(len(created), 1)
        self.assertTrue(destination.is_symlink())
        status = destination.lstat()
        self.assertEqual((status.st_dev, status.st_ino, destination.readlink()), created[0])
        self.assertFalse(target.exists())
        self.assertFalse(list(self.home.glob('.hatch-restore-*')))

    def test_restore_resolves_symlink_parent_without_following_destination_leaf(self):
        self.build(validate=True)
        record_fixture_review(self.root, self.data)
        archive = self.home / 'archive.zip'
        archive_run(self.root, self.data, archive)
        parent = self.home / 'actual-parent'
        parent.mkdir()
        alias = self.home / 'parent-alias'
        self.directory_symlink(alias, parent)

        result = restore_run(archive, alias / 'restored')
        destination = parent / 'restored'
        self.assertEqual(result['restored'], str(destination.resolve()))
        self.assertFalse(result['revalidation_required'])
        self.assertTrue(alias.is_symlink())
        restored = hatch.manifest(destination)
        self.assertEqual(restored['run_id'], self.data['run_id'])
        self.assertTrue(hatch.status(destination, restored)['reviewed'])

        script = Path(__file__).resolve().parents[1] / 'scripts/hatch.py'
        process = subprocess.run([sys.executable, str(script), 'restore', '--source', str(archive),
                                  '--run-dir', str(alias / 'cli-restored')], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertEqual(json.loads(process.stdout)['restored'], str((parent / 'cli-restored').resolve()))
        restored = hatch.manifest(parent / 'cli-restored')
        self.assertEqual(restored['run_id'], self.data['run_id'])
        self.assertTrue(hatch.status(parent / 'cli-restored', restored)['reviewed'])
        self.assertFalse(list(parent.glob('.hatch-restore-*')))

    def test_restore_preserves_empty_destination_racing_publication(self):
        self.build(validate=True)
        record_fixture_review(self.root, self.data)
        archive = self.home / 'archive.zip'
        archive_run(self.root, self.data, archive)
        destination = self.home / 'restored'
        unrelated = self.home / '.hatch-restore-unrelated'
        unrelated.mkdir()
        (unrelated / 'keep.txt').write_bytes(b'Independent staging directory')
        original_publish = portable_runs._rename_no_replace
        staged, created = [], []

        def race(stage, target):
            staged.append(stage)
            target.mkdir()
            status = target.stat()
            created.append((status.st_dev, status.st_ino))
            return original_publish(stage, target)

        with patch.object(portable_runs, '_rename_no_replace', side_effect=race):
            with self.assertRaises(FileExistsError):
                restore_run(archive, destination)
        self.assertEqual(len(created), 1)
        status = destination.stat()
        self.assertEqual((status.st_dev, status.st_ino), created[0])
        self.assertEqual(list(destination.iterdir()), [])
        self.assertFalse(staged[0].exists())
        self.assertEqual(list(self.home.glob('.hatch-restore-*')), [unrelated])
        self.assertEqual((unrelated / 'keep.txt').read_bytes(), b'Independent staging directory')

    def test_archive_version_change_requires_revalidation(self):
        self.build(validate=True);record_fixture_review(self.root,self.data)
        path=self.home/'archive.zip';archive_run(self.root,self.data,path)
        with patch('portable_runs.versions',return_value={'different_code':True}):
            result=restore_run(path,self.home/'restored')
        self.assertTrue(result['revalidation_required']);self.assertFalse(hatch.status(self.home/'restored',hatch.manifest(self.home/'restored'))['reviewed'])

    def test_archive_tamper_and_traversal_fail_without_destination(self):
        for name in ('../escaped','safe'):
            path=self.home/(name.replace('/','_')+'.zip')
            with zipfile.ZipFile(path,'w') as archive:
                archive.writestr(name,b'changed');archive.writestr('archive-receipt.json',json.dumps({'files':{name:'0'*64}}))
            with self.assertRaises(ValueError):restore_run(path,self.home/'unwritten')
            self.assertFalse((self.home/'unwritten').exists())

    def test_v2_migration_preserves_history_and_requires_review(self):
        self.build(validate=True);record_fixture_review(self.root,self.data)
        old=copy.deepcopy(self.data);old['schema_version']=2
        for job in old['jobs']:
            current=hatch.inputs(self.root,job);current.pop('@generation-profile')
            current['profile.json']=hatch.digest(self.root/'profile.json');job['input_hashes']=current
        hatch.save(self.root,old);result=migrate(self.root);data=hatch.manifest(self.root)
        self.assertTrue(result['migrated']);self.assertEqual(data['run_id'],old['run_id'])
        self.assertEqual([j['status'] for j in data['jobs']],['generated']*3)
        self.assertTrue(all(j['history'][:-1]==o['history'] for j,o in zip(data['jobs'],old['jobs'])))
        self.assertFalse(hatch.status(self.root,data)['reviewed']);hatch.validate(self.root,data)

    def test_review_dimensions_and_warning_evidence_are_required(self):
        self.build(validate=True);record_fixture_review(self.root,self.data)
        value=hatch.review_template(self.root,self.data);old=hatch.read(self.root/'qa/visual-review.json')
        old['rows']['rest']['dimensions']['identity']['verdict']='fail';path=self.home/'bad-review.json';hatch.atomic(path,old)
        with self.assertRaisesRegex(ValueError,'failing review dimension'):hatch.review(self.root,self.data,path)
        old['rows']['rest']['dimensions'].pop('identity');hatch.atomic(path,old)
        with self.assertRaisesRegex(ValueError,'requires identity'):hatch.review(self.root,self.data,path)
        result=build_review(self.root,self.data,self.home/'review-desk')
        self.assertTrue(Path(result['review_ui']).exists());self.assertFalse(result['accepted'])

    def test_limits_preserve_inflight_identity_and_stop_before_claim(self):
        self.build();hatch.repair(self.root,self.data,'jump','Limit test')
        hatch.configure_limits(self.root,self.data,argparse.Namespace(max_calls=7,max_attempts=5,concurrency=1,estimate_units=2,unit_budget=20))
        claim=hatch.claim(self.root,self.data,'jump')
        status=hatch.resume(self.root,self.data)
        self.assertEqual(status['claims'][0]['token'],claim['token'])
        hatch.fail(self.root,self.data,'jump',claim['token'],'failed fixture attempt')
        self.data['limits']['generation_calls']=4
        with self.assertRaisesRegex(ValueError,'call budget'):hatch.claim(self.root,self.data,'jump')
        self.data['limits']['generation_calls']=20;self.data['limits']['unit_budget']=8
        with self.assertRaisesRegex(ValueError,'unit budget'):hatch.claim(self.root,self.data,'jump')
        self.assertEqual(hatch.job_for(self.data,'jump')['attempts'],1)

    def test_changed_inputs_keep_stale_claim_until_reconciled(self):
        self.build();hatch.repair(self.root,self.data,'jump','Stale claim test')
        claim=hatch.claim(self.root,self.data,'jump')
        request=hatch.read(self.root/'pet_request.json');request['constraints']['require'].append('retain white tip')
        hatch.atomic(self.root/'pet_request.json',request)
        result=hatch.status(self.root,self.data)
        self.assertEqual(result['claims'][0]['token'],claim['token'])
        self.assertEqual(result['claims'][0]['state'],'stale-awaiting-reconciliation')
        with self.assertRaisesRegex(ValueError,'already running'):hatch.claim(self.root,self.data,'jump')
        hatch.fail(self.root,self.data,'jump',claim['token'],'Inputs changed while result was unresolved.')
        self.assertEqual(hatch.job_for(self.data,'jump')['status'],'failed')

    def test_attempt_prompt_and_superseded_review_contents_are_retained(self):
        import hashlib
        self.build(validate=True);record_fixture_review(self.root,self.data)
        previous=(self.root/'qa/visual-review.json').read_bytes()
        record_fixture_review(self.root,self.data)
        preserved=[e for e in self.data['history'] if e['event']=='preserved-visual-review'][-1]
        self.assertEqual(preserved['receipt_utf8'].encode('utf-8'),previous)
        hatch.repair(self.root,self.data,'jump','Preserve exact attempt text.')
        claim=hatch.claim(self.root,self.data,'jump')
        snapshot=hatch.job_for(self.data,'jump')['history'][-1]['prompt_snapshot']
        self.assertEqual(hashlib.sha256(snapshot['utf8'].encode('utf-8')).hexdigest(),snapshot['sha256'])
        self.assertEqual(snapshot['sha256'],hatch.digest(claim['prompt']))
        hatch.fail(self.root,self.data,'jump',claim['token'],'Fixture failed attempt for history test.')
        hatch.repair(self.root,self.data,'jump','A later repair must retain earlier bytes.')
        self.assertIn(snapshot,[e.get('prompt_snapshot') for e in hatch.job_for(self.data,'jump')['history']])

    def test_resource_bounds_and_supported_large_frame_count(self):
        p=small_profile();p['layout'].update(columns=65);p['layout'].pop('width');p['states'][0].update(frames=65,durations=[100]*65)
        with self.assertRaisesRegex(ValueError,'resource bound'):load_profile(p)
        p=small_profile();p['layout'].update(cell_width=4096,cell_height=4096);p['layout'].pop('width');p['layout'].pop('height')
        with self.assertRaisesRegex(ValueError,'working set|64 million'):load_profile(p)
        image=Image.new('RGBA',(32*64,32));d=ImageDraw.Draw(image)
        for i in range(64):d.rectangle((i*32+8,8,i*32+24,24),fill='red')
        frames,info=extraction.render_slots(image,64,(32,32),padding=0,resampling='nearest')
        self.assertEqual(len(frames),64);self.assertTrue(all(f.getbbox()==(8,8,25,25) for f in frames))
        path=self.home/'large.png';Image.new('RGB',(2001,1000)).save(path)
        with self.assertRaisesRegex(ValueError,'source exceeds'):check_image(path)


if __name__=='__main__':unittest.main()
