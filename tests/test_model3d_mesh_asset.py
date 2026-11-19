"""Strict document bridge with genuine native payload and caller evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from model3d_mesh_asset import build_asset
from model3d_contracts import LIMITS
from model3d_validation import validate_file
from rig_errors import RigError, object_hash
from rig_gltf import export_geometry
from model3d_primitives import primitive
import test_rig_asset_library_import as native_import_fixture


def record(root,name,media):
    data=(root/name).read_bytes()
    return dict(path=name,bytes=len(data),sha256=hashlib.sha256(data).hexdigest(),media_type=media)

def provenance():
    return dict(origin='procedural-fixture',fixture=True,description='Authored engineering fixture; not production approval.',
                tools=[],evidence=[],license=dict(identifier=None,attribution=None,redistribution='unknown',evidence=None))

def source(file):
    return dict(schema_version=1,kind='model3d-source',id='original',version='1.0.0',file=file,role='geometry',
                provenance=provenance(),original_filename=Path(file['path']).name,dimensions_px=None,
                acquisition=dict(method='unknown',timestamp=None),evidence_class='synthetic',
                mask=None,annotations=[],camera=dict(status='unknown'))

class MeshAsset(unittest.TestCase):
    def setUp(self):
        cache=Path.home()/'.cache/hatch-fleet/tmp';cache.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=cache);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def build(self,geometry,**kw):
        return build_asset(self.root,geometry,asset_id='fixture-asset',version='1.0.0',category='prop',
                           sources=[source(geometry)],provenance=provenance(),**kw)
    def test_primitive_builder_full_validate_file(self):
        mesh=primitive('box',{'width':1,'height':2,'depth':3})
        export_geometry(mesh.geometry(),self.root/'box.glb',provenance={'fixture':True})
        geometry=record(self.root,'box.glb','model/gltf-binary');before=(self.root/'box.glb').read_bytes()
        limits=deepcopy(LIMITS);limits['vertices']=float(limits['vertices'])
        document=self.build(geometry,limits=limits)
        self.assertEqual(document['review'],{'status':'pending','evidence':[]})
        self.assertTrue(all(p['confidence'] is None and p['anatomical_side']=='unknown' for p in document['parts']))
        self.assertEqual(document['measurements']['bounds'],{'minimum':[-.5,-1.,-1.5],'maximum':[.5,1.,1.5]})
        self.assertEqual(document['materials'][0]['id'],document['parts'][0]['material_ids'][0])
        (self.root/'asset.json').write_text(json.dumps(document))
        result=validate_file(self.root/'asset.json',self.root)
        self.assertTrue(result['payloads_verified']);self.assertTrue(result['geometry_inspected'])
        self.assertEqual((self.root/'box.glb').read_bytes(),before)
        for mutate in [lambda d:d['measurements'].__setitem__('vertices',999),
                       lambda d:d['parts'][0].__setitem__('mesh_ids',['absent'])]:
            wrong=deepcopy(document);mutate(wrong);(self.root/'wrong.json').write_text(json.dumps(wrong))
            with self.assertRaises(RigError):validate_file(self.root/'wrong.json',self.root)
    def test_real_retained_rig_document_identity(self):
        src=Path(__file__).resolve().parents[1]/'examples/rig-3d/winged-creature/exports/character.glb'
        (self.root/'rig.glb').write_bytes(src.read_bytes());geometry=record(self.root,'rig.glb','model/gltf-binary')
        document=self.build(geometry)
        (self.root/'asset.json').write_text(json.dumps(document));report=validate_file(self.root/'asset.json',self.root)
        self.assertTrue(report['geometry_inspected'])
        self.assertTrue(report['geometry'][0]['skin_bindings']);self.assertTrue(report['geometry'][0]['animation_clips'])
        self.assertEqual((self.root/'rig.glb').read_bytes(),src.read_bytes())
    def test_canonical_namespace_evidence_bound_without_schema_extension(self):
        export_geometry(primitive('box').geometry(),self.root/'asset.glb',provenance={'fixture':True})
        geometry=record(self.root,'asset.glb','model/gltf-binary')
        manifest={'identity':{'id':'box','namespace':'engineering','library':'local-fixtures'},'version':'1.0.0',
                  'files':[{'path':'runtime/asset.glb','sha256':geometry['sha256'],'bytes':geometry['bytes']}]}
        manifest['manifest_sha256']=object_hash(manifest)
        ref={'id':'box','version':'1.0.0','sha256':manifest['manifest_sha256']}
        envelope={'canonical_ref':ref,'manifest':manifest}
        (self.root/'canonical.json').write_text(json.dumps(envelope));evidence=record(self.root,'canonical.json','application/json')
        document=self.build(geometry,canonical_evidence=evidence)
        self.assertEqual(document['extensions'],[{'namespace':'asset-library-canonical','payload':evidence}])
        self.assertIn(evidence,document['provenance']['evidence'])
        envelope['canonical_ref']['sha256']='0'*64
        (self.root/'canonical.json').write_text(json.dumps(envelope));evidence=record(self.root,'canonical.json','application/json')
        with self.assertRaises(RigError):self.build(geometry,canonical_evidence=evidence)
    def test_canonical_ref_structure_and_unique_inventory(self):
        export_geometry(primitive('box').geometry(),self.root/'asset.glb',provenance={'fixture':True})
        geometry=record(self.root,'asset.glb','model/gltf-binary')
        base={'identity':{'id':'box','namespace':'engineering','library':'local-fixtures'},'version':'1.0.0',
              'files':[{'path':'runtime/asset.glb','sha256':geometry['sha256'],'bytes':geometry['bytes']}]}
        mutations=[
            ('boolean-version', lambda m,r: (m.__setitem__('version',True),r.__setitem__('version',True))),
            ('invalid-version', lambda m,r: (m.__setitem__('version','definitely-not-semver'),r.__setitem__('version','definitely-not-semver'))),
            ('leading-zero-version', lambda m,r: (m.__setitem__('version','01.0.0'),r.__setitem__('version','01.0.0'))),
            ('boolean-id', lambda m,r: (m['identity'].__setitem__('id',True),r.__setitem__('id',True))),
            ('invalid-namespace', lambda m,r:m['identity'].__setitem__('namespace','bad namespace')),
            ('boolean-library', lambda m,r:m['identity'].__setitem__('library',True)),
            ('duplicate-runtime',lambda m,r:m['files'].append(deepcopy(m['files'][0]))),
            ('case-alias-runtime',lambda m,r:m['files'].append({**m['files'][0],'path':'RUNTIME/asset.glb'})),
            ('fractional-byte-count',lambda m,r:m['files'][0].__setitem__('bytes',1.5)),
            ('boolean-byte-count',lambda m,r:m['files'][0].__setitem__('bytes',True)),
            ('malformed-digest',lambda m,r:m['files'][0].__setitem__('sha256','not-a-hash')),
            ('unsafe-path',lambda m,r:m['files'].append({**m['files'][0],'path':'../escape'})),
        ]
        for name,mutate in mutations:
            manifest=deepcopy(base);ref={'id':'box','version':'1.0.0','sha256':''}
            mutate(manifest,ref)
            manifest['manifest_sha256']=object_hash({k:v for k,v in manifest.items() if k!='manifest_sha256'})
            ref['sha256']=manifest['manifest_sha256']
            (self.root/'canonical.json').write_text(json.dumps({'canonical_ref':ref,'manifest':manifest}))
            evidence=record(self.root,'canonical.json','application/json')
            with self.subTest(name=name), self.assertRaises(RigError):self.build(geometry,canonical_evidence=evidence)
        for malformed in [True,'0'*63,'A'*64,'0'*64+'\n']:
            manifest=deepcopy(base);manifest['manifest_sha256']=object_hash(manifest)
            ref={'id':'box','version':'1.0.0','sha256':malformed}
            (self.root/'canonical.json').write_text(json.dumps({'canonical_ref':ref,'manifest':manifest}))
            with self.subTest(digest=malformed), self.assertRaises(RigError):
                self.build(geometry,canonical_evidence=record(self.root,'canonical.json','application/json'))

    @unittest.skipUnless(shutil.which('node') and (native_import_fixture.VALIDATOR/'package.json').is_file(),
                         'Requires the pinned offline Khronos glTF validator')
    def test_build_from_registered_native_normalized_geometry(self):
        from asset_library import AssetLibrary
        from asset_library_contracts import seal
        from asset_library_import import ImportPipeline
        from model3d_mesh_import import import_canonical

        library_path = self.root/'library'
        library = AssetLibrary(library_path, create=True)
        library.upgrade_storage()
        original = native_import_fixture.NativeImportTests.external_source(self)
        original = original.rename(original.with_name('Géométrie.gltf'))
        license_path = self.root/'license.txt'
        license_path.write_text('Synthetic test fixture. CC0-1.0.\n')
        ref = ImportPipeline(library).import_asset(
            original, identity=native_import_fixture.IDENTITY, version='1.0.0',
            metadata=native_import_fixture.METADATA, license_file=license_path,
            fixture=True, unit_scale=2.5)
        reopened = AssetLibrary(library_path)
        with import_canonical(reopened, ref) as imported:
            root = imported.root
            manifest = imported.provenance['manifest']
            geometry = record(root, imported.provenance['source_entry'], 'model/gltf-binary')
            texture = record(root, 'source/resources/paint.png', 'image/png')
            source_mesh = record(root, 'source/model.gltf', 'model/gltf+json')
            retained_source = dict(source(source_mesh), original_filename=original.name)

            def evidence_for(reference, registry_manifest):
                (root/'canonical.json').write_text(json.dumps(
                    dict(canonical_ref=reference, manifest=registry_manifest)))
                return record(root, 'canonical.json', 'application/json')

            evidence = evidence_for(ref, manifest)
            build_options = dict(asset_id='normalized-triangle', version='1.0.0', category='prop')
            document = build_asset(root, geometry, sources=[retained_source], provenance=provenance(),
                                   files=[geometry, texture], canonical_evidence=evidence, **build_options)
            self.assertEqual(document['geometry'], geometry)
            self.assertEqual(document['sources'], [retained_source])
            self.assertEqual(document['measurements']['bounds'], dict(minimum=[0, 0, 0], maximum=[2.5, 2.5, 0]))
            self.assertEqual(document['extensions'], [dict(namespace='asset-library-canonical', payload=evidence)])
            self.assertEqual(document['provenance']['evidence'], [evidence])
            self.assertEqual(document['review'], dict(status='pending', evidence=[]))
            (root/'asset.json').write_text(json.dumps(document))
            self.assertTrue(validate_file(root/'asset.json', root)['payloads_verified'])

            with self.assertRaisesRegex(RigError, 'Canonical normalized entry'):
                build_asset(root, source_mesh, sources=[source(source_mesh)], provenance=provenance(),
                            canonical_evidence=evidence, **build_options)

            export_geometry(primitive('box').geometry(), root/'other.glb')
            other = record(root, 'other.glb', 'model/gltf-binary')
            with self.assertRaisesRegex(RigError, 'inventory does not bind'):
                build_asset(root, other, sources=[source(other)], provenance=provenance(),
                            canonical_evidence=evidence, **build_options)

            wrong_role = deepcopy(manifest)
            changed = next(item for item in wrong_role['files'] if item['path'] == 'normalized.glb')
            changed['role'] = 'evidence'
            wrong_role['provenance']['conversions'][0]['outputs'][0] = changed
            wrong_role = seal(wrong_role)
            wrong_ref = dict(ref, sha256=wrong_role['manifest_sha256'])
            with self.assertRaisesRegex(RigError, 'native glTF conversion'):
                build_asset(root, geometry, sources=[source(geometry)], provenance=provenance(),
                            files=[geometry, texture], canonical_evidence=evidence_for(wrong_ref, wrong_role),
                            **build_options)

            claimed = dict(provenance(), fixture=False, origin='imported')
            with self.assertRaisesRegex(RigError, 'Canonical fixture provenance'):
                build_asset(root, geometry, sources=[source(geometry)], provenance=claimed,
                            files=[geometry, texture], canonical_evidence=evidence_for(ref, manifest),
                            **build_options)
            for fixture_file, role in ((geometry, 'geometry'), (source_mesh, 'geometry'), (texture, 'texture')):
                with self.subTest(fixture_file=fixture_file['path']):
                    claimed_source = dict(source(fixture_file), role=role, provenance=claimed)
                    if role == 'texture':
                        claimed_source['dimensions_px'] = [2, 2]
                    with self.assertRaisesRegex(RigError, 'Canonical fixture sources must remain fixtures'):
                        build_asset(root, geometry, sources=[claimed_source], provenance=provenance(),
                                    files=[geometry, texture], canonical_evidence=evidence, **build_options)

    def test_actual_material_texture_binding(self):
        import io
        from PIL import Image
        from rig_models import TextureImage
        buffer=io.BytesIO();Image.new('RGBA',(2,3),(20,40,60,255)).save(buffer,format='PNG')
        data=buffer.getvalue();geometry=primitive('box').geometry()
        import numpy as np
        geometry.meshes[0].attributes['TEXCOORD_0']=np.zeros((len(geometry.meshes[0].positions),2))
        geometry.images=[TextureImage('fixture','image/png',data)];geometry.textures=[{'source':0}]
        geometry.materials[0]={'pbrMetallicRoughness':{'baseColorFactor':[.2,.4,.6,1.], 'baseColorTexture':{'index':0}}}
        export_geometry(geometry,self.root/'box.glb',provenance={'fixture':True})
        (self.root/'texture.png').write_bytes(data)
        encoded=record(self.root,'box.glb','model/gltf-binary');texture=record(self.root,'texture.png','image/png')
        with self.assertRaises(RigError):self.build(encoded)
        doc=self.build(encoded,files=[encoded,texture])
        self.assertEqual(doc['materials'][0]['textures']['base_color']['file'],texture)
        self.assertEqual(doc['materials'][0]['texture_dimensions']['base_color'],[2,3])
        self.assertEqual(doc['materials'][0]['base_color'],[.2,.4,.6,1.])
    def test_bad_hash_and_limits_rejected_no_output(self):
        export_geometry(primitive('box').geometry(),self.root/'box.glb',provenance={'fixture':True})
        geometry=record(self.root,'box.glb','model/gltf-binary');geometry['sha256']='0'*64
        with self.assertRaises(RigError):self.build(geometry)
        geometry=record(self.root,'box.glb','model/gltf-binary')
        for value in (True,1.5):
            limits=deepcopy(LIMITS);limits['vertices']=value
            with self.assertRaises(RigError):self.build(geometry,limits=limits)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['box.glb'])

if __name__=='__main__':unittest.main()
