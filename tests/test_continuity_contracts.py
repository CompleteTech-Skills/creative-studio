"""Executable contract/lifecycle gates, explicitly synthetic and nonvisual."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from continuity_contracts import (KINDS, MAX_BYTES, canonical_bytes, definitions, digest,
    generate, load_document, portable_path, references, schemas, validate_document, verify_references)
from continuity_registry import IdentityRegistry
from continuity_test_helpers import pointer, sample, write_assets, review_for

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class ContractTests(unittest.TestCase):
    def test_all_23_models_roundtrip_generated_schema_and_unknown_fields(self):
        from jsonschema import Draft202012Validator
        self.assertEqual(len(KINDS), 23)
        self.assertEqual(set(KINDS), set(schemas()))
        for kind in KINDS:
            with self.subTest(kind=kind):
                value = sample(kind)
                validate_document(value, kind)
                encoded = canonical_bytes(value)
                self.assertEqual(value, json.loads(encoded))
                Draft202012Validator.check_schema(schemas()[kind])
                Draft202012Validator(schemas()[kind]).validate(value)
                bad = deepcopy(value); bad['arbitrary'] = 'rejected'
                self.assertTrue(list(Draft202012Validator(schemas()[kind]).iter_errors(bad)))
                with self.assertRaises(ValueError):
                    validate_document(bad, kind)
                bad = deepcopy(value); bad['schema_version'] = '999.0.0'
                with self.assertRaises(ValueError):
                    validate_document(bad, kind)
        generate(ROOT/'profiles', check=True)

    def test_direct_schema_rejects_terminal_newlines_and_nonportable_paths(self):
        from jsonschema import Draft202012Validator
        validator=Draft202012Validator(schemas()['revision'])
        for field,value in [('revision_id','robot\n'),('semantic_version','1.0.0\n'),
                            ('created_at','2026-10-24T00:00:00Z\n')]:
            bad=sample('revision');bad[field]=value
            with self.subTest(field=field):
                self.assertTrue(list(validator.iter_errors(bad)))
                with self.assertRaises(ValueError):validate_document(bad)
        for field,value in [('id','native_id\n'),('version','1.0.0\n'),('kind','hatch_3d_rig\n'),
                            ('sha256','0'*64+'\n')]:
            bad=sample('revision');bad['asset'][field]=value
            self.assertTrue(list(validator.iter_errors(bad)))
            with self.assertRaises(ValueError):validate_document(bad)
        for path in ('../x','a//b','CON.txt','x.','x ','a/../b','a\n','/abs','C:drive','a\\b','a?b','COM¹.txt','LPT²','CONIN$','CONOUT$.json',' leading/file'):
            bad=sample('revision');bad['asset']['path']=path
            with self.subTest(path=repr(path)):
                self.assertTrue(list(validator.iter_errors(bad)))
                with self.assertRaises(ValueError):validate_document(bad)
        for path in ('mesh.obj','models/v1/mesh.obj','native_id/CONsumer.glb'):
            good=sample('revision');good['asset']['path']=path
            self.assertFalse(list(validator.iter_errors(good)))
            validate_document(good)

    def test_canonical_serialization_versions_duplicate_keys_finite_bounded(self):
        self.assertEqual(digest({'b':2,'a':1}), digest({'a':1,'b':2}))
        for value in (float('nan'), float('inf'), -float('inf')):
            with self.assertRaises(ValueError): canonical_bytes({'x':value})
        with self.assertRaises(ValueError): canonical_bytes({'x':'a' * MAX_BYTES})
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'bad.json'
            for payload in (b'{"kind":"continuity-identity","kind":"continuity-revision"}', b'{"x":NaN}'):
                path.write_bytes(payload)
                with self.assertRaises(ValueError): load_document(path)
        with self.assertRaises(ValueError): validate_document(sample('identity'), 'unknown')
        bad = sample('identity'); bad['identity_id'] = '../robot'
        with self.assertRaises(ValueError): validate_document(bad)

    def test_path_portability_and_reference_integrity(self):
        for path in ('../x', '/x', 'a/../x', 'a\\x', 'C:x', 'a//x', 'NUL.txt', 'x.', 'x ', 'a\x00b', 'x:e', 'e\u0301'):
            with self.subTest(path=repr(path)), self.assertRaises(ValueError): portable_path(path)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); write_assets(root)
            doc = sample('revision')
            report = verify_references(root, doc)
            self.assertFalse(report['geometry_inspected']);self.assertFalse(report['visual_approval'])
            self.assertEqual(report['total_bytes'], (root/'mesh.obj').stat().st_size + (root/'reference.ppm').stat().st_size)
            bad = deepcopy(doc); bad['textures'] = [dict(pointer(), path='MESH.OBJ')]
            with self.assertRaisesRegex(ValueError,'conflicting'): references(bad)
            (root/'mesh.obj').write_bytes(b'changed bytes')
            with self.assertRaisesRegex(ValueError,'HASH'): verify_references(root, doc)
            (root/'mesh.obj').unlink()
            with self.assertRaises(OSError): verify_references(root,doc)
            write_assets(root)
            with self.assertRaisesRegex(ValueError,'LIMIT'): verify_references(root,doc,maximum_bytes=1)
            if hasattr(os,'symlink'):
                (root/'mesh.obj').unlink();(root/'mesh.obj').symlink_to(root/'reference.ppm')
                with self.assertRaisesRegex(ValueError,'symlink'): verify_references(root,doc)

    # This filesystem API does not exist on Windows; no solver/portable capability
    # is inferred from this POSIX-only negative case.
    if hasattr(os, 'mkfifo'):
        def test_nonregular_dependency_is_rejected_without_blocking(self):
            with tempfile.TemporaryDirectory() as temp:
                root=Path(temp);write_assets(root)
                (root/'mesh.obj').unlink();os.mkfifo(root/'mesh.obj')
                source="from continuity_contracts import verify_references; from continuity_test_helpers import sample; verify_references("+repr(str(root))+",sample('revision'))"
                process=subprocess.run([PYTHON,'-c',source],capture_output=True,text=True,timeout=5,
                    env={**os.environ,'PYTHONPATH':str(ROOT/'scripts')+os.pathsep+str(ROOT/'tests')})
                self.assertNotEqual(process.returncode,0)
                self.assertIn('nonregular',process.stderr)

    def test_exact_time_transforms_graphs_and_semantic_failures(self):
        valid = sample('snapshot')
        valid['query']['story_time'] = dict(numerator=1, denominator=3)
        valid['query']['presentation_time'] = dict(numerator=2, denominator=3)
        self.assertEqual(validate_document(valid)['query']['story_time'],dict(numerator=1,denominator=3))
        for mutate in (lambda v:v['query'].update(story_time=dict(numerator=2,denominator=6)),
                       lambda v:v['query'].update(boundary='last-frame')):
            bad=deepcopy(valid);mutate(bad)
            with self.assertRaises(ValueError):validate_document(bad)
        bad=sample('revision');bad['components'][0]['local_transform']['rotation']=[0,0,0,0]
        with self.assertRaisesRegex(ValueError,'quaternion'):validate_document(bad)
        bad=sample('revision');bad['components'][0]['parent_id']='missing'
        with self.assertRaisesRegex(ValueError,'parent'):validate_document(bad)
        bad=sample('revision');bad['components'][0]['parent_id']=bad['components'][0]['id']
        with self.assertRaisesRegex(ValueError,'cyclic'):validate_document(bad)
        bad=sample('revision');bad['geometry']['kind']='image'
        with self.assertRaisesRegex(ValueError,'3D'):validate_document(bad)
        bad=sample('identity');bad['provenance']['created_at']='2026-02-30T00:00:00Z'
        with self.assertRaises(ValueError):validate_document(bad)
        bad=sample('rule');bad['scope']['end']=bad['scope']['start']
        with self.assertRaisesRegex(ValueError,'half-open'):validate_document(bad)
        bad=sample('review');bad['reviewer_kind']='synthetic-test'
        with self.assertRaisesRegex(ValueError,'synthetic'):validate_document(bad)
        bad=sample('link');bad['outgoing']['boundary']='after'
        with self.assertRaisesRegex(ValueError,'terminal'):validate_document(bad)
        bad=sample('entity');bad['identity_id']='different'
        with self.assertRaisesRegex(ValueError,'disagree'):validate_document(bad)
        bad=sample('prop-state');bad['owner_socket_id']='hand'
        with self.assertRaisesRegex(ValueError,'owner'):validate_document(bad)

    def test_schema_integer_equivalent_floats_remain_exact_and_structured(self):
        from jsonschema import Draft202012Validator
        def float_integers(value):
            if type(value) is int:return float(value)
            if isinstance(value,dict):return {k:float_integers(v) for k,v in value.items()}
            if isinstance(value,list):return [float_integers(v) for v in value]
            return value
        for kind in KINDS:
            value=float_integers(sample(kind))
            with self.subTest(kind=kind):
                Draft202012Validator(schemas()[kind]).validate(value)
                result=validate_document(value)
                self.assertEqual(result['kind'],value['kind'])
        value=sample('snapshot');value['query']['story_time']=dict(numerator=1.0,denominator=3.0)
        result=validate_document(value)
        self.assertIs(type(result['query']['story_time']['numerator']),int)
        self.assertEqual(result['query']['story_time'],dict(numerator=1,denominator=3))

    def test_uninspected_geometry_is_explicitly_unavailable_not_fake_measurements(self):
        value=sample('revision')
        self.assertEqual(value['geometry_statistics']['status'],'unverified')
        self.assertIsNone(value['bounds'])
        self.assertTrue(all(value['geometry_statistics'][key] is None for key in ('vertices','triangles','components','measured_by')))
        bad=deepcopy(value);bad['geometry_statistics']['vertices']=1
        with self.assertRaisesRegex(ValueError,'unverified'):validate_document(bad)
        bad=deepcopy(value);bad['geometry_statistics'].update(status='measured',vertices=3,triangles=1,components=1,measured_by=pointer())
        bad['bounds']=dict(minimum=[0,0,0],maximum=[1,1,0])
        with self.assertRaisesRegex(ValueError,'inspection receipt'):validate_document(bad)
        bad=sample('identity');bad['canonical_revision']=dict(identity_id='other',revision_id='r1',semantic_version='1.0.0',sha256='0'*64)
        with self.assertRaisesRegex(ValueError,'another logical identity'):validate_document(bad)

    def test_actual_cli_structured_success_and_invalid_document(self):
        command=[PYTHON,str(ROOT/'scripts/continuity_contracts.py'),'--validate']
        result=subprocess.run(command+[str(ROOT/'examples/continuity/revision.json')],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['valid'])
        self.assertFalse(json.loads(result.stdout)['geometry_inspected'])
        with tempfile.TemporaryDirectory() as temp:
            invalid=Path(temp)/'invalid.json';value=sample('revision');value['unknown']=True
            invalid.write_text(json.dumps(value))
            result=subprocess.run(command+[str(invalid)],capture_output=True,text=True)
            self.assertEqual(result.returncode,2)
            self.assertFalse(json.loads(result.stdout)['valid'])
            self.assertNotIn('Traceback',result.stdout+result.stderr)

    def test_manifest_leaf_replacement_at_open_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);target=root/'manifest.json';value=sample('identity')
            target.write_bytes(canonical_bytes(value));original_open=os.open
            replacement=root/'new.json';replacement.write_bytes(canonical_bytes(value))
            def replace_before_open(path,*args,**kwargs):
                if Path(path)==target:
                    os.replace(replacement,target)
                return original_open(path,*args,**kwargs)
            with patch('continuity_contracts.os.open',replace_before_open):
                with self.assertRaisesRegex(ValueError,'CHANGED'):load_document(target)
            self.assertEqual(json.loads(target.read_bytes()),value)

    if hasattr(os,'mkfifo'):
        def test_manifest_fifo_swap_at_open_never_blocks(self):
            source="""import tempfile,os
from pathlib import Path
from unittest.mock import patch
import continuity_contracts as cc
from continuity_test_helpers import sample
with tempfile.TemporaryDirectory() as temp:
 target=Path(temp)/'manifest.json';target.write_bytes(cc.canonical_bytes(sample('identity')))
 original=os.open
 def replace_before_open(path,*args,**kwargs):
  if Path(path)==target:
   assert args[0]&os.O_NONBLOCK
   target.unlink();os.mkfifo(target)
  return original(path,*args,**kwargs)
 with patch('continuity_contracts.os.open',replace_before_open):
  try:cc.load_document(target)
  except ValueError as error:assert 'nonregular' in str(error)
  else:raise AssertionError('FIFO was accepted')
 assert target.is_fifo()
"""
            result=subprocess.run([PYTHON,'-c',source],capture_output=True,text=True,timeout=5,
                env={**os.environ,'PYTHONPATH':str(ROOT/'scripts')+os.pathsep+str(ROOT/'tests')})
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_programmatic_depth_limit_precedes_recursive_copy(self):
        from rig_errors import RigError
        root={};cursor=root
        for _ in range(1500):
            child={};cursor['nested']=child;cursor=child
        with self.assertRaises(RigError) as error:
            validate_document(root,'identity')
        self.assertEqual(error.exception.report['code'],'RIG_JSON_DEPTH')
        cursor=root;depth=0
        while cursor:
            self.assertEqual(set(cursor),{'nested'})
            cursor=cursor['nested'];depth+=1
        self.assertEqual(depth,1500)

    def test_import_requires_no_optional_numerical_or_renderer_dependencies(self):
        script = """import sys
class Block:
 def find_spec(self, fullname, path=None, target=None):
  if fullname.split('.')[0] in {'numpy','PIL','jsonschema','bpy','pygltflib','playwright'}: raise ImportError(fullname)
sys.meta_path.insert(0,Block())
import continuity_contracts, continuity_registry
assert len(continuity_contracts.schemas())==23
"""
        process=subprocess.run([PYTHON,'-c',script],env={**os.environ,'PYTHONPATH':str(ROOT/'scripts')},capture_output=True,text=True)
        self.assertEqual(process.returncode,0,process.stderr)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.assets=self.root/'assets';write_assets(self.assets)
        self.registry=IdentityRegistry(self.root/'identity.sqlite',asset_root=self.assets)
        self.identity=sample('identity');self.registry.create_identity(self.identity)

    def tearDown(self):
        self.registry.close();self.temp.cleanup()

    def promote(self, version=1, parent=None, *, select=False):
        revision=sample('revision');revision.update(revision_id=f'robot-r{version}',semantic_version=f'{version}.0.0',parent_revision_id=parent)
        # Actual native payload refs are retained; only editable identity metadata changes.
        revision['visual_spec']['description']=f'Explicit synthetic variant {version}'
        checksum=self.registry.create_draft(f'draft{version}',revision)
        review=review_for(self.assets,revision,name=f'review{version}')
        self.registry.promote(f'draft{version}',expected_sha256=checksum,review=review,select=select)
        return revision,checksum

    def test_draft_isolation_promotion_reload_compare_pin_and_rollback(self):
        r1,h1=self.promote(select=True)
        r2,h2=self.promote(2,'robot-r1')
        self.assertEqual(self.registry.identity('robot')['selected_revision'],'robot-r1')
        self.assertFalse(self.registry.revision('robot-r1')['production_artwork_approved'])
        comparison=self.registry.compare('robot-r1','robot-r2')
        self.assertFalse(comparison['geometry_compared'])
        self.assertIn('/visual_spec/description',[x['path'] for x in comparison['changes']])
        self.registry.select('robot','robot-r2',expected_revision_id='robot-r1')
        self.registry.select('robot','robot-r1',expected_revision_id='robot-r2')
        self.assertEqual(self.registry.revision('robot-r1')['sha256'],h1)
        self.assertEqual(self.registry.revision('robot-r2')['sha256'],h2)
        self.registry.close();self.registry=IdentityRegistry(self.root/'identity.sqlite',asset_root=self.assets)
        self.assertEqual(self.registry.identity('robot')['selected_revision'],'robot-r1')
        self.assertEqual(self.registry.history()[-1]['action'],'select')
        self.assertEqual(self.registry.revision('robot-r1')['document'],r1)

    def test_identical_revision_reuse_deduplicates_and_cannot_select_deprecated(self):
        revision,checksum=self.promote()
        self.registry.create_draft('same',revision)
        review=review_for(self.assets,revision,name='same')
        self.assertEqual(self.registry.promote('same',expected_sha256=checksum,review=review,select=True),checksum)
        self.assertEqual(self.registry.db.execute('SELECT count(*) FROM contents').fetchone()[0],1)
        self.assertEqual(self.registry.identity('robot')['selected_pin']['sha256'],checksum)
        with self.assertRaisesRegex(ValueError,'unknown draft'):self.registry.draft('same')
        self.registry.deprecate('robot-r1',expected_sha256=checksum,reason='retained historical version')
        self.registry.create_draft('deprecated-copy',revision)
        before=list(self.registry.db.iterdump())
        with self.assertRaisesRegex(ValueError,'deprecated'):
            self.registry.promote('deprecated-copy',expected_sha256=checksum,review=review,select=True,expected_selected_revision_id='robot-r1')
        self.assertEqual(list(self.registry.db.iterdump()),before)

    def test_invalid_draft_and_stale_edits_preserve_all_rows(self):
        revision=sample('revision');checksum=self.registry.create_draft('draft',revision)
        original=self.registry.db.iterdump();before=list(original)
        edited=deepcopy(revision);edited['visual_spec']['description']='draft edit'
        with self.assertRaisesRegex(ValueError,'stale'):
            self.registry.update_draft('draft',edited,expected_sha256='0'*64)
        self.assertEqual(list(self.registry.db.iterdump()),before)
        updated=self.registry.update_draft('draft',edited,expected_sha256=checksum)
        self.assertNotEqual(checksum,updated)
        self.assertEqual(self.registry.draft('draft')['document'],edited)
        self.assertIsNone(self.registry.identity('robot')['selected_revision'])
        self.assertEqual(self.registry.db.execute('SELECT count(*) FROM revisions').fetchone()[0],0)
        edited['parent_revision_id']='missing'
        with self.assertRaisesRegex(ValueError,'unknown'):
            self.registry.update_draft('draft',edited,expected_sha256=updated)

    def test_reused_revision_and_semantic_version_with_different_bytes_rejected(self):
        r1,h1=self.promote()
        changed=deepcopy(r1);changed['visual_spec']['description']='changed immutable bytes'
        checksum=self.registry.create_draft('changed',changed)
        review=review_for(self.assets,changed,name='changed')
        before=list(self.registry.db.iterdump())
        with self.assertRaisesRegex(ValueError,'IMMUTABLE'):
            self.registry.promote('changed',expected_sha256=checksum,review=review)
        self.assertEqual(list(self.registry.db.iterdump()),before)
        self.assertEqual(self.registry.revision('robot-r1')['sha256'],h1)
        changed['revision_id']='other-r1'
        checksum=self.registry.create_draft('changed2',changed);review=review_for(self.assets,changed,name='changed2')
        with self.assertRaisesRegex(ValueError,'semantic version'):
            self.registry.promote('changed2',expected_sha256=checksum,review=review)

    def test_approval_integrity_failure_never_promotes_or_relabels_fixture(self):
        revision=sample('revision');checksum=self.registry.create_draft('draft',revision)
        good=review_for(self.assets,revision)
        bads=[]
        for key,value in [('subject_sha256','0'*64),('fixture',False),('verdict','pending'),('reviewer_kind','synthetic-test')]:
            bad=deepcopy(good);bad[key]=value;bads.append(bad)
        bad=deepcopy(good);bad['inspected']=[bad['inspected'][0]];bads.append(bad)
        bad=deepcopy(good);bad['reviewer']='forged changed supplied decision';bads.append(bad)
        before=list(self.registry.db.iterdump())
        for bad in bads:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                self.registry.promote('draft',expected_sha256=checksum,review=bad)
            self.assertEqual(list(self.registry.db.iterdump()),before)
        (self.assets/'reference.ppm').write_bytes(b'changed referenced visual bytes')
        with self.assertRaisesRegex(ValueError,'HASH'):
            self.registry.promote('draft',expected_sha256=checksum,review=good)
        self.assertEqual(list(self.registry.db.iterdump()),before)

    def test_migration_selected_exact_pins_and_all_or_nothing_preconditions(self):
        r1,h1=self.promote();r2,h2=self.promote(2,'robot-r1')
        pin=dict(identity_id='robot',revision_id='robot-r1',semantic_version='1.0.0',sha256=h1)
        bindings=dict(schema_version='1.0.0',project_id='project',project_revision=pointer(),instances=[
            dict(instance_id=name,entity_id='entity-'+name,shot_id='shot',revision=deepcopy(pin)) for name in ('a','b')])
        initial=self.registry.register_bindings(bindings)
        before=list(self.registry.db.iterdump())
        for selected,expected in [(['a','missing'],initial),(['a','a'],initial),(['a'],'0'*64)]:
            with self.assertRaises(ValueError):
                self.registry.migrate_selected('project',selected,'robot-r2',expected_sha256=expected)
            self.assertEqual(list(self.registry.db.iterdump()),before)
        changed=self.registry.migrate_selected('project',['a'],'robot-r2',expected_sha256=initial)
        self.assertNotEqual(initial,changed)
        after=self.registry.bindings('project')['document']
        self.assertEqual(after['instances'][1],bindings['instances'][1])
        self.assertEqual(after['instances'][0]['revision']['sha256'],h2)
        self.assertEqual(after['instances'][0]['entity_id'],'entity-a')
        self.registry.deprecate('robot-r2',expected_sha256=h2,reason='Replaced, retained forever')
        self.assertEqual(self.registry.revision('robot-r2')['sha256'],h2)
        self.assertTrue(self.registry.revision('robot-r2')['deprecated'])
        self.assertEqual(self.registry.bindings('project')['sha256'],changed)
        with self.assertRaisesRegex(ValueError,'deprecated'):
            self.registry.migrate_selected('project',['b'],'robot-r2',expected_sha256=changed)

    def test_authoritative_asset_and_dependency_changes_need_real_migration_adapter(self):
        first,h1=self.promote()
        for name,field in [('asset-change','asset'),('closure-change','dependencies')]:
            changed=deepcopy(first);changed.update(revision_id=name,semantic_version='2.0.0' if field=='asset' else '3.0.0')
            payload=b'new native bytes';(self.assets/(name+'.obj')).write_bytes(payload)
            native=pointer(name+'.obj',payload)
            if field=='asset':changed['asset']=native
            else:changed['dependencies']=[native]
            h=self.registry.create_draft(name,changed);review=review_for(self.assets,changed,name=name)
            self.registry.promote(name,expected_sha256=h,review=review)
        bindings=dict(schema_version='1.0.0',project_id='project',project_revision=pointer(),instances=[
            dict(instance_id='a',entity_id='entity-a',shot_id='shot',revision=dict(identity_id='robot',revision_id='robot-r1',semantic_version='1.0.0',sha256=h1))])
        initial=self.registry.register_bindings(bindings)
        for target in ('asset-change','closure-change'):
            before=list(self.registry.db.iterdump())
            with self.assertRaisesRegex(ValueError,'ADAPTER_REQUIRED'):
                self.registry.migrate_selected('project',['a'],target,expected_sha256=initial)
            self.assertEqual(list(self.registry.db.iterdump()),before)

    def test_register_deprecation_race_and_project_byte_changes_are_atomic(self):
        from contextlib import contextmanager
        r1,h1=self.promote()
        project_bytes=b'{"native":"project"}';(self.assets/'project.json').write_bytes(project_bytes)
        bindings=dict(schema_version='1.0.0',project_id='project',project_revision=pointer('project.json',project_bytes,kind='scene'),instances=[
            dict(instance_id='a',entity_id='entity-a',shot_id='shot',revision=dict(identity_id='robot',revision_id='robot-r1',semantic_version='1.0.0',sha256=h1))])
        original=self.registry._transaction
        with IdentityRegistry(self.root/'identity.sqlite',asset_root=self.assets) as second:
            @contextmanager
            def race():
                second.deprecate('robot-r1',expected_sha256=h1,reason='other controller before admission lock')
                with original():yield
            with patch.object(self.registry,'_transaction',race):
                with self.assertRaisesRegex(ValueError,'DEPRECATED'):self.registry.register_bindings(bindings)
            self.assertEqual(self.registry.db.execute('SELECT count(*) FROM projects').fetchone()[0],0)
        r2,h2=self.promote(2,'robot-r1')
        bindings['instances'][0]['revision']=dict(identity_id='robot',revision_id='robot-r2',semantic_version='2.0.0',sha256=h2)
        initial=self.registry.register_bindings(bindings);before=list(self.registry.db.iterdump())
        (self.assets/'project.json').write_bytes(b'{"native":"modified project"}')
        with self.assertRaisesRegex(ValueError,'HASH'):self.registry.bindings('project')
        with self.assertRaisesRegex(ValueError,'HASH'):
            self.registry.migrate_selected('project',['a'],'robot-r2',expected_sha256=initial)
        self.assertEqual(list(self.registry.db.iterdump()),before)

    def test_promotion_selection_cas_preserves_concurrent_pin(self):
        from contextlib import contextmanager
        r1,h1=self.promote(select=True);r2,h2=self.promote(2,'robot-r1')
        r3=sample('revision');r3.update(revision_id='robot-r3',semantic_version='3.0.0',parent_revision_id='robot-r2')
        h3=self.registry.create_draft('draft3',r3);review=review_for(self.assets,r3,name='review3')
        original=self.registry._transaction
        with IdentityRegistry(self.root/'identity.sqlite',asset_root=self.assets) as second:
            @contextmanager
            def race():
                second.select('robot','robot-r2',expected_revision_id='robot-r1')
                with original():yield
            with patch.object(self.registry,'_transaction',race):
                with self.assertRaisesRegex(ValueError,'stale promotion selection'):
                    self.registry.promote('draft3',expected_sha256=h3,review=review,select=True,expected_selected_revision_id='robot-r1')
            self.assertEqual(self.registry.identity('robot')['selected_revision'],'robot-r2')
            self.assertEqual(self.registry.draft('draft3')['sha256'],h3)
            with self.assertRaisesRegex(ValueError,'unknown immutable'):self.registry.revision('robot-r3')
        self.registry.promote('draft3',expected_sha256=h3,review=review,select=True,expected_selected_revision_id='robot-r2')
        self.assertEqual(self.registry.identity('robot')['selected_revision'],'robot-r3')

    def test_indexed_revision_and_selected_identity_integrity(self):
        r1,h1=self.promote();r2,h2=self.promote(2,'robot-r1')
        self.registry.db.execute("UPDATE revisions SET hash=? WHERE id='robot-r1'",(h2,))
        with self.assertRaisesRegex(ValueError,'indexed identity/version'):self.registry.revision('robot-r1')
        self.registry.db.execute("UPDATE revisions SET hash=? WHERE id='robot-r1'",(h1,))
        other=deepcopy(self.identity);other['identity_id']='other';self.registry.create_identity(other)
        self.registry.db.execute("UPDATE identities SET selected='robot-r1' WHERE id='other'")
        with self.assertRaisesRegex(ValueError,'another identity'):self.registry.identity('other')

    if hasattr(os,'mkfifo'):
        def test_review_receipt_fifo_swap_after_verification_never_blocks(self):
            source="""import sys,os,tempfile
from pathlib import Path
from unittest.mock import patch
import continuity_registry as cr
from continuity_test_helpers import sample,write_assets,review_for
with tempfile.TemporaryDirectory() as temp:
 root=Path(temp);assets=root/'assets';write_assets(assets)
 with cr.IdentityRegistry(root/'registry.sqlite',asset_root=assets) as registry:
  registry.create_identity(sample('identity'));revision=sample('revision');checksum=registry.create_draft('draft',revision);review=review_for(assets,revision)
  original=cr.verify_references
  def race(root,value,**kwargs):
   result=original(root,value,**kwargs)
   if value.get('kind')=='continuity-review':
    leaf=Path(root)/value['native_receipt']['path'];leaf.unlink();os.mkfifo(leaf)
   return result
  with patch.object(cr,'verify_references',race):
   try:registry.promote('draft',expected_sha256=checksum,review=review)
   except ValueError as error:
    assert 'nonregular' in str(error);assert registry.draft('draft')['sha256']==checksum
   else:raise AssertionError('racing FIFO accepted')
"""
            process=subprocess.run([PYTHON,'-c',source],capture_output=True,text=True,timeout=5,
                env={**os.environ,'PYTHONPATH':str(ROOT/'scripts')+os.pathsep+str(ROOT/'tests')})
            self.assertEqual(process.returncode,0,process.stdout+process.stderr)

    def test_revision_drafts_cannot_upgrade_fixture_identity_to_production(self):
        revision=sample('revision');revision['source_provenance'].update(origin='import',fixture=False)
        before=list(self.registry.db.iterdump())
        with self.assertRaisesRegex(ValueError,'fixture classification'):
            self.registry.create_draft('relabel',revision)
        self.assertEqual(list(self.registry.db.iterdump()),before)
        bad=sample('identity');bad['provenance']['fixture']=False
        with self.assertRaisesRegex(ValueError,'procedural fixtures'):
            validate_document(bad)

    def test_cross_identity_lineage_and_migration_rejected(self):
        r1,h1=self.promote()
        other=deepcopy(self.identity);other['identity_id']='other';self.registry.create_identity(other)
        bad=sample('revision');bad.update(identity_id='other',revision_id='other-r2',semantic_version='2.0.0',parent_revision_id='robot-r1')
        with self.assertRaisesRegex(ValueError,'another identity'):self.registry.create_draft('other',bad)
        bad=sample('revision');bad.update(revision_id='robot-r0',semantic_version='0.1.0',parent_revision_id='robot-r1')
        with self.assertRaisesRegex(ValueError,'advance'):self.registry.create_draft('old',bad)

    def test_transaction_failure_rolls_back_draft_promotion_and_bindings(self):
        revision=sample('revision');checksum=self.registry.create_draft('draft',revision);review=review_for(self.assets,revision)
        before=list(self.registry.db.iterdump())
        original=self.registry._history
        def fail(*args): raise OSError('injected actual transaction failure')
        self.registry._history=fail
        with self.assertRaises(OSError):self.registry.promote('draft',expected_sha256=checksum,review=review,select=True)
        self.assertEqual(list(self.registry.db.iterdump()),before)
        self.registry._history=original
        self.registry.promote('draft',expected_sha256=checksum,review=review)
        self.assertEqual(self.registry.revision('robot-r1')['sha256'],checksum)

    def test_independent_connections_enforce_cas_and_immutable_reads(self):
        revision=sample('revision');checksum=self.registry.create_draft('draft',revision)
        with IdentityRegistry(self.root/'identity.sqlite',asset_root=self.assets) as second:
            changed=deepcopy(revision);changed['visual_spec']['description']='second controller edit'
            second.update_draft('draft',changed,expected_sha256=checksum)
            before=list(self.registry.db.iterdump())
            with self.assertRaisesRegex(ValueError,'stale'):
                self.registry.update_draft('draft',revision,expected_sha256=checksum)
            review=review_for(self.assets,revision)
            with self.assertRaisesRegex(ValueError,'stale'):
                self.registry.promote('draft',expected_sha256=checksum,review=review)
            self.assertEqual(list(self.registry.db.iterdump()),before)

    def test_changed_structural_payload_migration_requires_real_adapter(self):
        r1,h1=self.promote()
        second=deepcopy(r1);second.update(revision_id='robot-r2',semantic_version='2.0.0',parent_revision_id='robot-r1')
        second['components'][0]['local_transform']['translation']=[1,0,0]
        h2=self.registry.create_draft('draft2',second)
        review=review_for(self.assets,second,name='review2')
        self.registry.promote('draft2',expected_sha256=h2,review=review)
        bindings=dict(schema_version='1.0.0',project_id='project',project_revision=pointer(),instances=[
            dict(instance_id='a',entity_id='entity-a',shot_id='shot',revision=dict(identity_id='robot',revision_id='robot-r1',semantic_version='1.0.0',sha256=h1))])
        initial=self.registry.register_bindings(bindings);before=list(self.registry.db.iterdump())
        with self.assertRaisesRegex(ValueError,'ADAPTER_REQUIRED'):
            self.registry.migrate_selected('project',['a'],'robot-r2',expected_sha256=initial)
        self.assertEqual(list(self.registry.db.iterdump()),before)

    @contextmanager
    def capture_initialization_connections(self, authorizer=None):
        """Keep genuine native connections alive to expose deterministic leaks.

        Assertions run before this test-owned cleanup. The original Windows
        teardown test remains unchanged and never suppresses cleanup failures.
        """
        import sqlite3
        connect=sqlite3.connect;captured=[]
        def record(*args,**kwargs):
            connection=connect(*args,**kwargs)
            captured.append(connection)
            if authorizer is not None:connection.set_authorizer(authorizer)
            return connection
        try:
            with patch('continuity_registry.sqlite3.connect',record):
                yield captured
        finally:
            for connection in captured:connection.close()

    def assert_native_connection_closed(self, connection):
        import sqlite3
        with self.assertRaisesRegex(sqlite3.ProgrammingError,'closed database'):
            connection.execute('SELECT 1')

    def test_rejected_unknown_version_closes_owned_connection_without_gc(self):
        self.registry.db.execute("UPDATE meta SET version='999.0.0'")
        before={str(path.relative_to(self.root)):path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
        with self.capture_initialization_connections() as captured:
            with self.assertRaisesRegex(ValueError,'incompatible') as rejection:
                IdentityRegistry(self.root/'identity.sqlite',asset_root=self.assets)
            # Keep both exception and failed native connection reachable. Linux
            # unlink success or garbage collection cannot hide the leaked owner.
            self.assertIsNotNone(rejection.exception)
            self.assertEqual(len(captured),1)
            self.assert_native_connection_closed(captured[0])
            after={str(path.relative_to(self.root)):path.read_bytes() for path in self.root.rglob('*') if path.is_file()}
            self.assertEqual(after,before)
        # A rejected opener must not close or modify the legitimate existing one.
        self.assertEqual(self.registry.db.execute('SELECT version FROM meta').fetchone()[0],'999.0.0')

    def test_sqlite_pragma_rejection_closes_constructor_connection(self):
        import sqlite3
        before=(self.root/'identity.sqlite').read_bytes()
        def deny_setting(action,name,*unused):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_PRAGMA and name=='trusted_schema' else sqlite3.SQLITE_OK
        with self.capture_initialization_connections(deny_setting) as captured:
            with self.assertRaisesRegex(sqlite3.DatabaseError,'authorized'):
                IdentityRegistry(self.root/'identity.sqlite',asset_root=self.assets)
            self.assertEqual(len(captured),1)
            self.assert_native_connection_closed(captured[0])
            self.assertEqual((self.root/'identity.sqlite').read_bytes(),before)

    def test_schema_initialization_rejection_closes_and_rolls_back_native_state(self):
        import sqlite3
        path=self.root/'partial.sqlite'
        connection=sqlite3.connect(path,isolation_level=None)
        try:
            connection.execute('PRAGMA application_id=1212372017')
            connection.execute('CREATE TABLE meta(version TEXT NOT NULL)')
            connection.execute("INSERT INTO meta VALUES ('1.0.0')")
            connection.execute('CREATE TABLE precious(value TEXT)')
            connection.execute("INSERT INTO precious VALUES ('original owner data')")
        finally:connection.close()
        before=path.read_bytes()
        def deny_new_table(action,name,*unused):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_TABLE and name=='identities' else sqlite3.SQLITE_OK
        with self.capture_initialization_connections(deny_new_table) as captured:
            with self.assertRaisesRegex(sqlite3.DatabaseError,'authorized'):
                IdentityRegistry(path,asset_root=self.assets)
            self.assertEqual(len(captured),1)
            self.assert_native_connection_closed(captured[0])
            self.assertEqual(path.read_bytes(),before)
        connection=sqlite3.connect(path)
        try:
            self.assertEqual(connection.execute('SELECT value FROM precious').fetchall(),[('original owner data',)])
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),[('meta',),('precious',)])
        finally:connection.close()

    def test_successful_constructor_transfers_connection_until_explicit_close(self):
        path=self.root/'successful.sqlite'
        with self.capture_initialization_connections() as captured:
            with IdentityRegistry(path,asset_root=self.assets) as registry:
                self.assertEqual(len(captured),1)
                self.assertIs(registry.db,captured[0])
                self.assertEqual(registry.db.execute('SELECT version FROM meta').fetchone()[0],'1.0.0')
            self.assert_native_connection_closed(captured[0])

    def test_existing_unrelated_database_and_unknown_versions_preserved(self):
        import sqlite3
        unrelated=self.root/'unrelated.sqlite'
        connection=sqlite3.connect(unrelated);connection.execute('CREATE TABLE precious(x)');connection.commit();connection.close()
        before=unrelated.read_bytes()
        with self.assertRaisesRegex(ValueError,'not a continuity'):
            IdentityRegistry(unrelated,asset_root=self.assets)
        self.assertEqual(unrelated.read_bytes(),before)
        self.registry.db.execute("UPDATE meta SET version='999.0.0'")
        before=(self.root/'identity.sqlite').read_bytes()
        with self.assertRaisesRegex(ValueError,'incompatible'):
            IdentityRegistry(self.root/'identity.sqlite',asset_root=self.assets)
        self.assertEqual((self.root/'identity.sqlite').read_bytes(),before)

    def test_registry_path_symlinks_reject_and_parent_aliases_normalize(self):
        if not hasattr(os,'symlink'):self.skipTest('platform lacks symlinks')
        link=self.root/'linked.sqlite';link.symlink_to(self.root/'uncreated'/'unrelated.sqlite')
        with self.assertRaises(ValueError):IdentityRegistry(link,asset_root=self.assets)
        self.assertTrue(link.is_symlink());self.assertFalse((self.root/'uncreated').exists())
        sidecar=self.root/'bad.sqlite-journal';sidecar.symlink_to(self.root/'uncreated'/'journal')
        with self.assertRaisesRegex(ValueError,'sidecar'):
            IdentityRegistry(self.root/'bad.sqlite',asset_root=self.assets)
        self.assertTrue(sidecar.is_symlink());self.assertFalse((self.root/'uncreated').exists())
        alias=self.root/'alias';alias.symlink_to(self.root,target_is_directory=True)
        with IdentityRegistry(alias/'second.sqlite',asset_root=self.assets) as registry:
            registry.create_identity(self.identity)
            self.assertEqual(registry.identity('robot')['document'],self.identity)
