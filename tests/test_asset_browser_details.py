"""Real registered-byte browser reports; no competing validation/storage authority."""
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import struct
import unittest

from PIL import Image
from asset_browser_details import BrowserDetails
from asset_library import file_record
from asset_library_contracts import seal
from asset_library_search import SearchIndex
from asset_library_versions import AssetVersions, ScopedResolver
from rig_errors import RigError
import test_asset_library as foundation


class BrowserDetailTests(unittest.TestCase):
    def setUp(self):
        self.fixture=foundation.AssetLibraryTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.root;self.library=self.fixture.library
        self.libraries={'local':self.library}
        self.reference=self.fixture.register()
        self.location=dict(library='local',reference=self.reference)
        self.index=SearchIndex(self.root/'index',self.libraries,create=True)
        self.details=BrowserDetails(self.libraries,self.index)

    def test_actual_graph_and_readonly_cleanup_preserve_all_canonical_versions(self):
        child=deepcopy(self.fixture.manifest);child['identity'].update(id='child',slug='child',name='Child geometry')
        child_ref=self.fixture.register(seal(child))
        parent=deepcopy(self.fixture.manifest);parent['version']='2.0.0'
        parent['dependencies']=[dict(target=child_ref,purpose='fixture-prop',required=True,
                                    origin='authored',relationship='direct',capabilities=[])]
        ref=self.fixture.register(seal(parent));location=dict(library='local',reference=ref)
        before=self.library.storage_status()
        graph=self.details.dependency_graph(location)
        self.assertEqual(graph['status'],'verified')
        self.assertEqual(len(graph['report']['nodes']),2)
        self.assertEqual(graph['report']['edges'][0]['target']['reference'],child_ref)
        plan=self.details.cleanup_analysis('local')
        self.assertEqual(plan['status'],'verified');self.assertFalse(plan['applied'])
        self.assertEqual(plan['plan']['protected_versions'],3)
        self.assertEqual(plan['plan']['candidates'],[])
        self.assertEqual(self.library.storage_status()['snapshot'],before['snapshot'])
        self.assertEqual(self.library.read_file(ref,'mesh/triangle.obj'),self.fixture.mesh.read_bytes())

    def test_broken_payload_diagnosis_keeps_metadata_and_never_claims_ready(self):
        with sqlite3.connect(self.library.database) as db:
            db.execute('UPDATE blobs SET payload=? WHERE sha256=?',(b'corrupt',self.fixture.files[0]['sha256']))
        report=self.details.diagnose(self.location)
        self.assertEqual(report['status'],'incomplete');self.assertFalse(report['payload_complete'])
        self.assertTrue(report['metadata_available'])
        self.assertEqual(report['files'],self.fixture.manifest['files'])
        self.assertTrue(report['errors']);self.assertEqual(report['dependency_graph']['status'],'incomplete')
        self.assertFalse(report['repair_executed'])
        missing=dict(library='local',reference=dict(self.reference,version='9.0.0'))
        absent=self.details.diagnose(missing)
        self.assertEqual(absent['status'],'incomplete');self.assertFalse(absent['metadata_available'])

    def test_actual_usage_is_limited_to_explicit_native_project_bindings(self):
        self.assertEqual(self.details.usage(self.location)['status'],'unavailable')
        resolver=ScopedResolver([('local',self.library)],project_library='local')
        resolver.bind('scene-one','hero',self.location,expected_revision=0)
        index=SearchIndex(self.root/'scoped-index',self.libraries,create=True,
                          usage_resolver=resolver,project_ids=['scene-one','scene-two'])
        report=BrowserDetails(self.libraries,index).usage(self.location)
        self.assertEqual(report['status'],'verified')
        self.assertEqual(report['report']['projects'],[dict(project='scene-one',revision=1,slots=['hero'])])
        self.assertEqual(report['report']['inspected_projects'],['scene-one','scene-two'])
        self.assertEqual(resolver.bindings('scene-two'),dict(revision=0,bindings={}))

    def test_version_comparison_uses_actual_native_breaking_change_rules(self):
        changed=deepcopy(self.fixture.manifest);changed['version']='1.0.1';changed['compatibility']['unit_scale']=.01
        candidate=self.fixture.register(seal(changed))
        before=self.library.list_references()
        report=self.details.compare(self.location,candidate)
        self.assertEqual(report['status'],'verified')
        self.assertEqual(report['report']['comparison']['minimum_bump'],'major')
        self.assertFalse(report['report']['comparison']['allowed'])
        self.assertEqual(self.library.list_references(),before)

    def test_export_capability_candidates_do_not_claim_executed_or_valid_geometry(self):
        obj=self.details.exports(self.location)
        self.assertEqual(obj['status'],'verified');self.assertFalse(obj['report']['export_executed'])
        self.assertEqual({x['asset_status'] for x in obj['report']['formats']},{'unavailable'})
        # A syntactically recognized GLB without geometry is a candidate requiring
        # native export verification; metadata alone must never certify it.
        document=json.dumps({'asset':{'version':'2.0'}}).encode();document+=b' '*((-len(document))%4)
        raw=struct.pack('<4sII',b'glTF',2,20+len(document))+struct.pack('<II',len(document),0x4E4F534A)+document
        source=self.root/'header-only.glb';source.write_bytes(raw)
        manifest=deepcopy(self.fixture.manifest);manifest['version']='2.0.0'
        record=file_record(source,'runtime/asset.glb','runtime')
        manifest['files'].append(record);manifest['publication']['inventory'].append(record['path'])
        ref=self.library.register(seal(manifest),{**self.fixture.sources,record['path']:source})
        result=self.details.exports(dict(library='local',reference=ref))
        glb=next(x for x in result['report']['formats'] if x['format']=='glb')
        blend=next(x for x in result['report']['formats'] if x['format']=='blend')
        self.assertEqual(glb['asset_status'],'candidate');self.assertTrue(glb['verification_required'])
        self.assertEqual(blend['asset_status'],'unavailable');self.assertFalse(result['production_approved'])

    @unittest.skipUnless(any(os.environ.get(name)=='1' for name in ('HATCH_ASSET_BROWSER_TEST','HATCH_RIG_INTEROP_TEST','HATCH_RIG_BROWSER_TEST')), 'Enable native asset validation acceptance.')
    def test_real_obj_validation_is_tightened_and_bound_to_exact_source_bytes(self):
        result=self.details.validation(self.location)
        self.assertEqual(result['status'],'verified',result)
        report=result['report'];self.assertEqual(report['status'],'technically-verified')
        self.assertIn('mesh',report['capabilities'])
        self.assertEqual(report['reference'],self.reference)
        self.assertEqual(report['file_hashes']['mesh/triangle.obj'],hashlib.sha256(self.fixture.mesh.read_bytes()).hexdigest())
        self.assertEqual(report['limits']['vertices'],100000)
        self.assertEqual(report['limits']['memory_bytes'],128*1024**2)
        self.assertFalse(report['production_approved'])

    def preview(self, *, declared_size=(4,3), payload=None, descriptor=True, status='generated'):
        source=self.root/'preview.png'
        if payload is None:
            Image.new('RGBA',(4,3),(10,120,230,255)).save(source)
        else:source.write_bytes(payload)
        record=file_record(source,'preview/thumbnail.png','preview')
        manifest=deepcopy(self.fixture.manifest);manifest['version']='2.0.0'
        manifest['files'].append(record);manifest['publication']['inventory'].append(record['path'])
        manifest['provenance']['parents']=[self.reference]
        config=self.fixture.files[1]
        if descriptor:
            manifest['previews']=[dict(type='thumbnail',source_version_sha256=self.reference['sha256'],
                backend='authored-test-image',backend_version='1.0.0',settings_sha256=config['sha256'],
                camera_sha256=config['sha256'],lighting_sha256=config['sha256'],settings_file=config,
                camera_file=config,lighting_file=config,file=record,width=declared_size[0],height=declared_size[1],status=status)]
        return self.library.register(seal(manifest),{**self.fixture.sources,record['path']:source}),source.read_bytes()

    def test_thumbnail_decodes_explicit_exact_preview_and_retains_generated_status(self):
        ref,payload=self.preview()
        result=self.details.thumbnail(dict(library='local',reference=ref))
        self.assertEqual(result[0],payload);self.assertEqual(result[1],'image/png')
        self.assertEqual(result[2]['status'],'generated');self.assertEqual(result[2]['visual_review'],'unreviewed')
        self.assertEqual(result[2]['source_version_sha256'],self.reference['sha256'])
        self.assertEqual(result[2]['sha256'],hashlib.sha256(payload).hexdigest())
        self.assertTrue(result[2]['fixture'])

    def test_ordinary_image_without_preview_descriptor_is_not_a_thumbnail(self):
        self.assertIsNone(self.details.thumbnail(self.location))
        ref,_=self.preview(descriptor=False)
        self.assertIsNone(self.details.thumbnail(dict(library='local',reference=ref)))

    def test_invalid_declared_thumbnail_dimensions_and_pixel_budget_fail_closed(self):
        ref,_=self.preview(declared_size=(2,6))
        with self.assertRaises(RigError) as caught:self.details.thumbnail(dict(library='local',reference=ref))
        self.assertEqual(caught.exception.report['code'],'LIBRARY_BROWSER_DETAILS_THUMBNAIL_DIMENSIONS')
        # A distinct version keeps the previous immutable evidence intact.
        manifest=deepcopy(self.library.resolve(ref));manifest['version']='3.0.0'
        manifest['previews'][0]['width']=2048;manifest['previews'][0]['height']=2048
        source={f['path']:(self.root/'preview.png' if f['path']=='preview/thumbnail.png' else self.fixture.sources[f['path']]) for f in manifest['files']}
        large=self.library.register(seal(manifest),source)
        with self.assertRaises(RigError) as caught:self.details.thumbnail(dict(library='local',reference=large))
        self.assertEqual(caught.exception.report['code'],'LIBRARY_BROWSER_DETAILS_THUMBNAIL_LIMIT')

    def test_declared_png_preview_must_actually_decode(self):
        ref,_=self.preview(payload=b'\x89PNG\r\n\x1a\nnot-a-decodable-image')
        with self.assertRaises(RigError) as caught:self.details.thumbnail(dict(library='local',reference=ref))
        self.assertEqual(caught.exception.report['code'],'LIBRARY_BROWSER_DETAILS_THUMBNAIL_DECODE')

    def test_actual_encoded_thumbnail_byte_limit_rejects_before_decode(self):
        image=io.BytesIO();Image.new('RGBA',(4,3),(10,120,230,255)).save(image,format='PNG')
        payload=image.getvalue()+b'x'*(4*1024**2+1-len(image.getvalue()))
        ref,_=self.preview(payload=payload)
        with self.assertRaises(RigError) as caught:self.details.thumbnail(dict(library='local',reference=ref))
        self.assertEqual(caught.exception.report['code'],'LIBRARY_BROWSER_DETAILS_THUMBNAIL_LIMIT')

    def test_scope_errors_reject_unconfigured_aliases_and_mismatched_indexes(self):
        invalid=dict(self.location,library='../outside')
        for method in (self.details.diagnose,self.details.validation,self.details.exports,self.details.thumbnail):
            with self.subTest(method=method.__name__),self.assertRaises(RigError):method(invalid)
        with self.assertRaises(RigError):self.details.cleanup_analysis('../outside')
        with self.assertRaises(RigError):BrowserDetails({'other':self.library},self.index)
