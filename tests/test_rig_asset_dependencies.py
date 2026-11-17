"""Actual native rig compatibility; optional native dependency group."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from asset_library import AssetLibrary,file_record
from asset_library_contracts import seal
from asset_library_dependencies import DependencyGraph,check_compatibility,locator,native_signature
from rig_gltf import export_gltf
from rig_test_helpers import fixture
from rig_skeleton import skeleton_from_heads
from rig_skinning import generate_skin_weights
from rig_jobs import save_rig

ROOT=Path(__file__).resolve().parents[1]


class NativeDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.library=AssetLibrary(self.root/'library',create=True)
        self.graph=DependencyGraph({'local':self.library})
        self.template=json.loads((ROOT/'examples/asset-library-foundation/manifest.json').read_text())

    def register(self,name,scale=1.,*,morph=True,morph_scale=1.,morph_default=0.,unit_scale=1.):
        rig=fixture(self.root,name=name)
        specs=[dict(id='b'+str(i),role='b'+str(i),parent='b'+str(i-1) if i else None,
                    head=[0,i*scale,0],tail=[0,(i+1)*scale,0],deform=True) for i in range(3)]
        rig.manifest['skeleton']=skeleton_from_heads(specs)
        rig.weights=generate_skin_weights(rig.geometry,rig.skeleton)
        if not morph:
            for mesh in rig.geometry.meshes:mesh.morphs={};mesh.morph_defaults={}
            rig.manifest['facial']['expressions']={}
        else:
            for mesh in rig.geometry.meshes:
                for target,attributes in mesh.morphs.items():
                    for semantic,values in attributes.items():attributes[semantic]=values*morph_scale
                    mesh.morph_defaults[target]=morph_default
        # Existing clips bind names; rest-shape differences must still reject.
        save_rig(rig,operation='compatibility-test')
        source=self.root/(name+'-export.glb');export_gltf(rig,source)
        license_path=ROOT/'examples/asset-library-foundation/source/license.txt'
        files=[file_record(source,'runtime/model.glb','runtime'),file_record(license_path,'source/license.txt','license',False)]
        doc=deepcopy(self.template);doc['identity']['id']=name;doc['identity']['slug']=name;doc['identity']['name']=name
        doc['files']=files;doc['publication']['inventory']=[f['path'] for f in files]
        doc['provenance']['sources']=[files[0]];doc['provenance']['license_evidence']=[files[1]]
        doc['compatibility']['formats']=['glb']
        doc['compatibility']['unit_scale']=unit_scale
        ref=self.library.register(seal(doc),{'runtime/model.glb':source,'source/license.txt':license_path})
        return locator('local',ref)

    def test_identical_bone_names_do_not_hide_changed_rest_pose(self):
        first=self.register('first');changed=self.register('changed',scale=1.25)
        expected=native_signature(self.graph,first,'runtime/model.glb')
        actual=native_signature(self.graph,changed,'runtime/model.glb')
        self.assertEqual(expected['skeleton_sha256'],actual['skeleton_sha256'])
        self.assertNotEqual(expected['rest_pose_sha256'],actual['rest_pose_sha256'])
        result=check_compatibility(self.graph,changed,{'rest_pose_sha256':expected['rest_pose_sha256']},native_path='runtime/model.glb')
        self.assertFalse(result['compatible'])

    def test_morph_and_skin_requirements_use_decoded_payload(self):
        first=self.register('first');changed=self.register('nomorph',morph=False)
        expected=native_signature(self.graph,first,'runtime/model.glb')
        result=check_compatibility(self.graph,changed,{'morph_sha256':expected['morph_sha256']},native_path='runtime/model.glb')
        self.assertFalse(result['compatible'])
        okay=check_compatibility(self.graph,first,{'skin_sha256':expected['skin_sha256'],'root_motion':expected['root_motion']},native_path='runtime/model.glb')
        self.assertTrue(okay['compatible']);self.assertFalse(okay['renderer_execution_verified'])

    def test_same_named_morphs_bind_actual_deltas_and_default_weights(self):
        first=self.register('first')
        expected=native_signature(self.graph,first,'runtime/model.glb')
        for name,options in [('changed-deltas',dict(morph_scale=5.)),
                             ('changed-default',dict(morph_default=.25))]:
            changed=self.register(name,**options)
            actual=native_signature(self.graph,changed,'runtime/model.glb')
            self.assertNotEqual(expected['morph_sha256'],actual['morph_sha256'])
            result=check_compatibility(self.graph,changed,
                {'morph_sha256':expected['morph_sha256']},native_path='runtime/model.glb')
            self.assertFalse(result['compatible'])

    def test_native_requirements_do_not_override_declared_source_units(self):
        source=self.register('centimeters',unit_scale=.01)
        expected=native_signature(self.graph,source,'runtime/model.glb')
        for requirements in [{'unit_scale':1.},
                             {'unit_scale':1.,'morph_sha256':expected['morph_sha256']}]:
            result=check_compatibility(self.graph,source,requirements,
                                       native_path='runtime/model.glb')
            self.assertFalse(result['compatible'])
            self.assertEqual(result['mismatches'][0]['field'],'unit_scale')
            self.assertEqual(result['mismatches'][0]['actual'],.01)


class UnitConversionTests(unittest.TestCase):
    def test_actual_static_units_convert_and_reopen_without_changing_original(self):
        from asset_library_dependencies import normalize_static_units
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();library=AssetLibrary(root/'library',create=True)
            template=json.loads((ROOT/'examples/asset-library-foundation/manifest.json').read_text())
            template['compatibility']['unit_scale']=.01
            base=ROOT/'examples/asset-library-foundation'
            original=library.register(seal(template),{f['path']:base/f['path'] for f in template['files']})
            graph=DependencyGraph({'local':library});before=library.read_file(original,'mesh/triangle.obj')
            result=normalize_static_units(graph,locator('local',original),'mesh/triangle.obj',version='2.0.0')
            self.assertTrue(result['geometry_verified'])
            self.assertEqual(library.read_file(original,'mesh/triangle.obj'),before)
            self.assertEqual(library.resolve(result['asset']['reference'])['compatibility']['unit_scale'],1)
            self.assertTrue(graph.closure([result['asset']])['complete'])


if __name__=='__main__':unittest.main()
