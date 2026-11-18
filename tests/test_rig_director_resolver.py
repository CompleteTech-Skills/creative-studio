"""Actual native-library reuse without generation or fictitious approval."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import director_resolver

from asset_library import AssetLibrary
from director_assets import build_assets
from director_resolver import AssetResolver
from rig_errors import RigError, atomic_json, sha256

ROOT=Path(__file__).resolve().parents[1]


def artifact(path,root,kind='asset-library-version',identifier=None):
    return dict(artifact_id=identifier or path.stem,kind=kind,schema_version=1,semantic_version='1.0.0',
        path=path.relative_to(root).as_posix(),sha256=sha256(path),byte_size=path.stat().st_size,
        media_type='application/json',provenance_ref=None,fixture=True,dependencies=[])


@unittest.skipUnless(sys.platform.startswith('linux'),
                     'Native resolver fixtures require Linux descriptor-pinned asset publication.')
class ResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)/'assets'
        cls.assets=build_assets(cls.root,seed=42)
        cls.library=AssetLibrary(cls.root/'library')
        cls.resolver=AssetResolver(cls.library,cls.root)
        cls.refs=[artifact(cls.root/(name+'-asset.json'),cls.root,identifier=name) for name in cls.assets['assets']]
        docs=json.loads((ROOT/'examples/director-contracts/contracts.json').read_text())
        cls.request_template=deepcopy(next(x for x in docs if x.get('method')=='AssetResolver.resolve' and x['kind']=='director-request'))
        cls.spec_template=json.loads((ROOT/'examples/director-contracts/asset-spec-fixture.json').read_text())

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def setUp(self):
        self.now=datetime(2026,1,1,tzinfo=timezone.utc)
        self.deadline=self.now+timedelta(minutes=1)
        clock=patch('director_resolver.datetime')
        self.clock=clock.start()
        self.addCleanup(clock.stop)
        self.clock.now.side_effect=lambda zone:self.now

    def request(self,name='aster',*,policy='engineering-fixtures',licenses='CC0-1.0',changes=None,refs=None):
        spec=deepcopy(self.spec_template)
        spec.update(id='spec-'+name,logical_id=name,asset_type='character' if name in ('aster','moss') else 'environment' if name=='laboratory' else 'prop',
            formats=['glb'],scale_meters=1,licensing='CC0-1.0',polygon_budget=100000,memory_budget=128*1024*1024,
            capabilities=['mesh','rig'] if name in ('aster','moss') else ['mesh'],candidates=[])
        spec.update(changes or {})
        path=self.root/'request-spec.json';atomic_json(path,spec)
        snapshot=self.root/'registry-snapshot.json';atomic_json(snapshot,dict(schema_version=1,kind='director-registry-snapshot',candidates=refs if refs is not None else self.refs))
        request=deepcopy(type(self).request_template)
        request['context']['deadline']=self.deadline.isoformat()
        request['input'].update(specs=[artifact(path,self.root,'director-asset-spec',spec['id'])],
            registry_snapshot=artifact(snapshot,self.root,'director-registry-snapshot'),reuse_policy=policy,license_policy=licenses)
        return request

    def resolution(self,request):return self.resolver.resolve(request)['value']['requirements'][0]

    def test_reuses_all_actual_assets_without_new_generation(self):
        before={p.relative_to(self.root).as_posix():sha256(p) for p in self.root.rglob('*') if p.is_file()}
        for name in self.assets['assets']:
            request=self.request(name)
            before={p.relative_to(self.root).as_posix():sha256(p) for p in self.root.rglob('*') if p.is_file()}
            result=self.resolver.resolve(request)
            self.assertEqual(result['value']['requirements'][0]['disposition'],'reusable', (name,result['value']['requirements'][0]))
            self.assertEqual(result['usage']['provider_calls']['value'],0)
            after={p.relative_to(self.root).as_posix():sha256(p) for p in self.root.rglob('*') if p.is_file()}
            self.assertEqual(before,after)

    def test_production_request_does_not_invent_review_approval(self):
        self.assertEqual(self.resolution(self.request(policy='require-reviewed'))['disposition'],'unreviewed')

    def test_missing_registry_and_unmatched_identity_are_explicit(self):
        self.assertEqual(self.resolution(self.request(refs=[]))['disposition'],'missing')
        result=self.resolution(self.request(changes={'logical_id':'other'}))
        self.assertEqual(result['disposition'],'incompatible')
        self.assertTrue(any('identity' in x.lower() for x in result['reasons']))

    def test_license_scale_and_missing_native_capabilities(self):
        self.assertEqual(self.resolution(self.request(licenses='MIT'))['disposition'],'incompatible')
        self.assertEqual(self.resolution(self.request(changes={'scale_meters':2}))['disposition'],'adaptable')
        self.assertEqual(self.resolution(self.request(changes={'capabilities':['unimplemented-magic']}))['disposition'],'incompatible')
        self.assertEqual(self.resolution(self.request(changes={'formats':['blend']}))['disposition'],'adaptable')

    def test_changed_or_missing_exact_manifest_is_not_reused(self):
        path=self.root/'aster-asset.json';original=path.read_bytes()
        request=self.request(refs=[self.refs[0]])
        try:
            path.write_bytes(original+b' ')
            self.assertEqual(self.resolution(request)['disposition'],'unavailable')
            path.unlink()
            self.assertEqual(self.resolution(request)['disposition'],'unavailable')
        finally:path.write_bytes(original)

    def test_corrupt_registered_blob_is_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            copied=Path(folder)/'library';shutil.copytree(self.root/'library',copied)
            library=AssetLibrary(copied)
            with sqlite3.connect(copied/'library.sqlite3') as db:
                db.execute('UPDATE blobs SET payload=? WHERE sha256=?',(b'corrupt',self.assets['assets']['aster']['identity']['source_sha256']))
            result=AssetResolver(library,self.root).resolve(self.request(refs=[self.refs[0]]))
            self.assertEqual(result['value']['requirements'][0]['disposition'],'unavailable')

    def test_ref_identity_cannot_relabel_a_valid_manifest(self):
        ref=deepcopy(self.refs[0]);ref['artifact_id']='wrong'
        self.assertEqual(self.resolution(self.request(refs=[ref]))['disposition'],'unavailable')
        ref=deepcopy(self.refs[0]);ref['semantic_version']='2.0.0'
        self.assertEqual(self.resolution(self.request(refs=[ref]))['disposition'],'unavailable')

    def test_registry_order_is_not_ranking_order(self):
        first=self.resolution(self.request(refs=self.refs))
        second=self.resolution(self.request(refs=list(reversed(self.refs))))
        self.assertEqual(first['candidates'],second['candidates'])
        self.assertEqual(first['disposition'],'reusable')
        self.assertEqual(first['candidates'][0]['artifact_id'],'aster')

    def test_invalid_policy_and_duplicate_registry_are_rejected(self):
        with self.assertRaises(RigError):self.resolver.resolve(self.request(policy='approve-all'))
        with self.assertRaises(RigError):self.resolver.resolve(self.request(refs=[self.refs[0],self.refs[0]]))

    def test_conversion_does_not_bypass_production_review(self):
        result=self.resolution(self.request(policy='require-reviewed',changes={'scale_meters':2,'formats':['blend']}))
        self.assertEqual(result['disposition'],'unreviewed')
        self.assertEqual(result['conversions'],['scale','format'])

    def test_visual_requirements_cannot_be_inferred_from_fixture(self):
        result=self.resolution(self.request(changes={'visual_characteristics':['striped blue fur']}))
        self.assertEqual(result['disposition'],'unreviewed')
        self.assertTrue(any('visual' in reason.lower() for reason in result['reasons']))

    def test_all_needed_conversions_are_preserved(self):
        result=self.resolution(self.request(changes={'scale_meters':2,'formats':['blend']}))
        self.assertEqual(result['disposition'],'adaptable')
        self.assertEqual(result['conversions'],['scale','format'])

    def test_fixture_reference_cannot_relabel_native_asset(self):
        ref=deepcopy(self.refs[0]);ref['fixture']=False
        self.assertEqual(self.resolution(self.request(refs=[ref]))['disposition'],'unavailable')

    def test_foreign_rig_cannot_supply_requested_capability(self):
        from asset_library_contracts import seal
        manifest=json.loads((self.root/'aster-asset.json').read_text())
        other=json.loads((self.root/'moss-asset.json').read_text())
        manifest['version']='2.0.0'
        manifest['files']=[deepcopy(next(f for f in other['files'] if f['role']=='rig'))
                           if f['role']=='rig' else f for f in manifest['files']]
        manifest['publication']['inventory']=[f['path'] for f in manifest['files']]
        manifest['provenance']['sources']=[deepcopy(f) for f in manifest['files'] if f['role']!='license']
        manifest=seal(manifest)
        with tempfile.TemporaryDirectory() as folder:
            library=AssetLibrary(Path(folder)/'library',create=True)
            library.register(manifest,{f['path']:self.root/f['path'] for f in manifest['files']})
            path=self.root/'foreign-rig-asset.json';atomic_json(path,manifest)
            ref=artifact(path,self.root,identifier='aster');ref['semantic_version']='2.0.0'
            request=self.request(refs=[ref])
            result=AssetResolver(library,self.root).resolve(request)['value']['requirements'][0]
            self.assertEqual(result['disposition'],'unavailable')
            self.assertTrue(any('RIG_BINDING' in reason for reason in result['reasons']))

    def test_unavailable_exact_identity_is_not_hidden_by_other_assets(self):
        path=self.root/'aster-asset.json';original=path.read_bytes()
        request=self.request()
        try:
            path.write_bytes(original+b' ')
            result=self.resolution(request)
            self.assertEqual(result['disposition'],'unavailable')
            self.assertEqual(result['candidates'][0]['artifact_id'],'aster')
        finally:path.write_bytes(original)

    def test_malformed_registry_reference_is_structured_error(self):
        for ref in ({},{'artifact_id':'aster'}):
            with self.subTest(reference=ref), self.assertRaises(RigError) as error:
                self.resolver.resolve(self.request(refs=[ref]))
            self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_REFERENCE')

    def test_malformed_rig_binding_remains_unavailable(self):
        import struct
        from asset_library_contracts import seal
        from asset_library import file_record
        manifest=json.loads((self.root/'aster-asset.json').read_text())
        rig=next(f for f in manifest['files'] if f['role']=='rig')
        original=(self.root/rig['path']).read_bytes()
        size=struct.unpack_from('<I',original,12)[0]
        document=json.loads(original[20:20+size]);document['asset']['extras']['hatchRig']='malformed'
        encoded=json.dumps(document,separators=(',',':')).encode()
        encoded+=b' '*((-len(encoded))%4)
        tail=original[20+size:]
        payload=struct.pack('<4sII',b'glTF',2,20+len(encoded)+len(tail))+struct.pack('<I4s',len(encoded),b'JSON')+encoded+tail
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);path=folder/'rig.glb';path.write_bytes(payload)
            replacement=file_record(path,rig['path'],'rig')
            manifest['version']='3.0.0'
            manifest['files']=[replacement if f['role']=='rig' else f for f in manifest['files']]
            manifest['provenance']['sources']=[deepcopy(f) for f in manifest['files'] if f['role']!='license']
            manifest=seal(manifest)
            library=AssetLibrary(folder/'library',create=True)
            library.register(manifest,{f['path']:path if f['role']=='rig' else self.root/f['path'] for f in manifest['files']})
            specpath=self.root/'malformed-rig-asset.json';atomic_json(specpath,manifest)
            ref=artifact(specpath,self.root,identifier='aster');ref['semantic_version']='3.0.0'
            result=AssetResolver(library,self.root).resolve(self.request(refs=[ref]))['value']['requirements'][0]
            self.assertEqual(result['disposition'],'unavailable')
            self.assertTrue(any('RIG_BINDING' in reason for reason in result['reasons']))

    def expire_after(self, operation):
        def execute(*args, **kwargs):
            result=operation(*args, **kwargs)
            self.now=self.deadline
            return result
        return execute

    def test_expired_or_equal_deadline_performs_no_artifact_or_native_work(self):
        request=self.request()
        for deadline in ('2000-01-01T00:00:00Z',self.now.isoformat()):
            request['context']['deadline']=deadline
            with self.subTest(deadline=deadline), ExitStack() as stack:
                operations=[stack.enter_context(patch.object(owner,name,wraps=getattr(owner,name)))
                            for owner,name in ((director_resolver,'_read'),(self.library,'resolve'),
                                (self.library,'read_file'),(director_resolver,'load_character_model'),
                                (director_resolver,'read_gltf'),(director_resolver,'_same_geometry'),
                                (director_resolver.tempfile,'TemporaryDirectory'))]
                with self.assertRaises(RigError) as error:self.resolver.resolve(request)
                self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_DEADLINE')
                for operation in operations:operation.assert_not_called()

    def test_live_lowercase_and_offset_deadlines_preserve_native_reuse(self):
        for deadline in (self.deadline.isoformat().replace('T','t').replace('+00:00','z'),
                         self.deadline.astimezone(timezone(timedelta(hours=5,minutes=30))).isoformat()):
            request=self.request(refs=[self.refs[0]])
            request['context']['deadline']=deadline
            with self.subTest(deadline=deadline):
                result=self.resolver.resolve(request)
                self.assertEqual(result['status'],'succeeded')
                self.assertEqual(result['value']['requirements'][0]['disposition'],'reusable')
                self.assertTrue(all(call.args==(timezone.utc,) for call in self.clock.now.call_args_list))

    def test_expiry_after_snapshot_stops_before_spec_and_native_reads(self):
        request=self.request()
        with patch('director_resolver._read',side_effect=self.expire_after(director_resolver._read)) as read, \
             patch.object(self.library,'resolve',wraps=self.library.resolve) as native, \
             patch.object(self.resolver,'_candidate',wraps=self.resolver._candidate) as candidate:
            with self.assertRaises(RigError) as error:self.resolver.resolve(request)
        self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_DEADLINE')
        self.assertEqual(read.call_count,1)
        self.assertEqual(read.call_args.args[1],request['input']['registry_snapshot'])
        native.assert_not_called()
        candidate.assert_not_called()

    def test_expiry_after_spec_stops_before_candidates(self):
        request=self.request()
        original=director_resolver._read
        def read(root,ref):
            result=original(root,ref)
            if ref['kind']=='director-asset-spec':self.now=self.deadline
            return result
        with patch('director_resolver._read',side_effect=read) as artifact_read, \
             patch.object(self.resolver,'_candidate',wraps=self.resolver._candidate) as candidate:
            with self.assertRaises(RigError) as error:self.resolver.resolve(request)
        self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_DEADLINE')
        self.assertEqual(artifact_read.call_count,2)
        candidate.assert_not_called()

    def test_expiry_between_candidates_stops_later_candidates(self):
        request=self.request(refs=self.refs)
        with patch.object(self.resolver,'_candidate',side_effect=self.expire_after(self.resolver._candidate)) as candidate, \
             patch('director_resolver.load_character_model',wraps=director_resolver.load_character_model) as imported:
            with self.assertRaises(RigError) as error:self.resolver.resolve(request)
        self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_DEADLINE')
        self.assertEqual(candidate.call_count,1)
        self.assertEqual(candidate.call_args.args[1],self.refs[0])
        self.assertGreater(imported.call_count,0)

    def test_expiry_between_specs_stops_later_spec_reads(self):
        request=self.request(refs=[self.refs[0]])
        original_ref=request['input']['specs'][0]
        path=self.root/'second-spec.json'
        spec=json.loads((self.root/original_ref['path']).read_text())
        spec['id']='second-spec'
        atomic_json(path,spec)
        second=artifact(path,self.root,'director-asset-spec',spec['id'])
        request['input']['specs'].append(second)
        with patch('director_resolver._budget',side_effect=self.expire_after(director_resolver._budget)) as budget, \
             patch('director_resolver._read',wraps=director_resolver._read) as read:
            with self.assertRaises(RigError) as error:self.resolver.resolve(request)
        self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_DEADLINE')
        self.assertEqual(budget.call_count,1)
        self.assertNotIn(second,[call.args[1] for call in read.call_args_list])

    def test_expiry_after_geometry_import_is_not_an_unavailable_candidate(self):
        request=self.request(refs=self.refs)
        with patch('director_resolver.load_character_model',side_effect=self.expire_after(director_resolver.load_character_model)) as imported, \
             patch('director_resolver.read_gltf',wraps=director_resolver.read_gltf) as rig_read, \
             patch.object(self.resolver,'_candidate',wraps=self.resolver._candidate) as candidate:
            with self.assertRaises(RigError) as error:self.resolver.resolve(request)
        self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_DEADLINE')
        self.assertEqual(imported.call_count,1)
        self.assertEqual(candidate.call_count,1)
        rig_read.assert_not_called()

    def test_expiry_after_rig_decode_stops_before_rig_import(self):
        request=self.request(refs=self.refs)
        with patch('director_resolver.read_gltf',side_effect=self.expire_after(director_resolver.read_gltf)) as rig_read, \
             patch('director_resolver.load_character_model',wraps=director_resolver.load_character_model) as imported:
            with self.assertRaises(RigError) as error:self.resolver.resolve(request)
        self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_DEADLINE')
        self.assertEqual(rig_read.call_count,1)
        self.assertEqual(imported.call_count,1)

    def test_expiry_after_geometry_comparison_never_returns_success(self):
        request=self.request(refs=self.refs)
        with patch('director_resolver._same_geometry',side_effect=self.expire_after(director_resolver._same_geometry)) as compared, \
             patch('director_resolver.validate_method_result',wraps=director_resolver.validate_method_result) as validated:
            with self.assertRaises(RigError) as error:self.resolver.resolve(request)
        self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_DEADLINE')
        self.assertEqual(compared.call_count,1)
        validated.assert_not_called()

    def test_expiry_during_candidate_failure_is_not_swallowed(self):
        request=self.request(refs=self.refs)
        def failed_import(*args,**kwargs):
            self.now=self.deadline
            raise ValueError('Synthetic importer failure at the deadline.')
        with patch('director_resolver.load_character_model',side_effect=failed_import) as imported, \
             patch.object(self.resolver,'_candidate',wraps=self.resolver._candidate) as candidate:
            with self.assertRaises(RigError) as error:self.resolver.resolve(request)
        self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_DEADLINE')
        self.assertEqual(imported.call_count,1)
        self.assertEqual(candidate.call_count,1)

    def test_expiry_during_final_validation_never_returns_success(self):
        request=self.request(refs=[self.refs[0]])
        with patch('director_resolver.validate_method_result',
                   side_effect=self.expire_after(director_resolver.validate_method_result)) as validated:
            with self.assertRaises(RigError) as error:self.resolver.resolve(request)
        self.assertEqual(error.exception.report['code'],'DIRECTOR_RESOLVER_DEADLINE')
        self.assertEqual(validated.call_count,1)
