"""Actual importer primitive mappings and exact material override projection."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from asset_library import AssetLibrary
from rig_errors import RigError
from rig_gltf import GltfBuilder
from scene3d_contracts import validate_document
from scene3d_import import import_gltf, _json
from scene3d_materials import project_material_slots, obj_material_bindings


class SceneMaterialProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.library = AssetLibrary(self.root/'library', create=True)
        license = self.root/'license.txt'
        license.write_text('CC0-1.0 engineering fixture')
        builder = GltfBuilder()
        builder.json['asset']['extras'] = {'hatchSource': {'fixture': True}}
        positions = builder.accessor([[0,0,0],[1,0,0],[0,1,0]], 'VEC3', bounds=True)
        indices = builder.accessor([0,1,2], 'SCALAR', 5123)
        builder.json['materials'] = [{'name':'first'}, {'name':'second'}]
        builder.json['meshes'] = [dict(primitives=[
            dict(attributes={'POSITION':positions}, indices=indices, material=1),
            dict(attributes={'POSITION':positions}, indices=indices, material=0)])]
        builder.json['nodes'] = [dict(mesh=0),dict(mesh=0,translation=[3,0,0])]
        builder.json['scenes'] = [dict(nodes=[0,1])]
        source = self.root/'input.glb'
        builder.write(source)
        self.imported = import_gltf(source,self.library,asset_id='material-fixture',
            attribution='Fixture author',license_file=license)
        self.pin = dict(id='paint', version='1.0.0',kind='material',path='materials/paint.json',sha256='0'*64)

    def test_persisted_mapping_matches_actual_normalized_glb_primitives_and_instances(self):
        asset = self.imported.asset
        normalized = _json(self.library.read_file(self.imported.library_reference, asset['content']['path']),'.glb')
        projection = project_material_slots(asset)
        self.assertEqual(len(projection),4)
        self.assertEqual({p['source_mesh'] for p in projection},{0})
        self.assertEqual({p['source_primitive'] for p in projection},{0,1})
        self.assertEqual({p['source_material_index'] for p in projection},{0,1})
        for record in projection:
            mesh = normalized['meshes'][record['normalized_mesh']]
            self.assertEqual(mesh['extras']['hatchMeshId'],record['mesh'])
            self.assertEqual(mesh['primitives'][record['normalized_primitive']]['material'],record['material_index'])
            self.assertIsNone(record['override'])
        self.assertEqual(len(project_material_slots(asset,component='node-0')),2)
        self.assertEqual(len(project_material_slots(asset,component='node-1')),2)

    def test_named_override_and_explicit_default_are_distinct_exact_pins_without_mutation(self):
        asset = self.imported.asset
        before = deepcopy(asset)
        pin = deepcopy(self.pin); pin['id']='specific'
        overrides=[dict(slot='default',material=self.pin),dict(slot='material-1',material=pin)]
        result=project_material_slots(asset,component='node-0',overrides=overrides)
        self.assertEqual({p['override']['id'] for p in result},{'paint','specific'})
        self.assertEqual(next(p for p in result if p['material_index']==1)['override'],pin)
        result[0]['override']['id']='mutated'
        self.assertEqual(asset,before)
        self.assertEqual(overrides[0]['material']['id'],'paint')

    def test_unknown_duplicate_or_unpinned_override_and_incomplete_mapping_fail(self):
        for overrides in ([dict(slot='typo',material=self.pin)],
                          [dict(slot='default',material=self.pin)]*2,
                          [dict(slot='material-0',material={'id':'paint'})]):
            with self.subTest(overrides=overrides), self.assertRaises(RigError):
                project_material_slots(self.imported.asset,overrides=overrides)
        asset=deepcopy(self.imported.asset)
        asset['material_bindings'][0]['primitives'].pop()
        with self.assertRaises(RigError): validate_document(asset,'asset')
        asset=deepcopy(self.imported.asset); del asset['material_bindings']
        validate_document(asset,'asset')
        with self.assertRaises(RigError): project_material_slots(asset)
        with self.assertRaises(RigError): project_material_slots(self.imported.asset,component='missing')

    def test_generated_obj_mapping_has_no_fabricated_native_material_or_blender_slot(self):
        from scene3d_example import build_example
        asset = deepcopy(build_example(self.root/'example')['documents']['triangle.json'])
        asset['material_bindings'] = obj_material_bindings(asset['id'])
        validate_document(asset,'asset')
        projected=project_material_slots(asset,overrides=[dict(slot='surface',material=self.pin)])
        self.assertEqual(len(projected),1)
        self.assertEqual(projected[0]['encoding'],'obj')
        self.assertIsNone(projected[0]['material_index'])
        self.assertIsNone(projected[0]['normalized_mesh'])
        self.assertNotIn('runtime_slot',projected[0])
        self.assertEqual(projected[0]['override'],self.pin)

    def test_override_for_an_unselected_component_is_rejected_instead_of_silently_ignored(self):
        asset=deepcopy(self.imported.asset)
        asset['components']['only-first']=[asset['material_bindings'][0]['primitives'][0]['mesh']]
        with self.assertRaises(RigError):
            project_material_slots(asset,component='only-first',
                overrides=[dict(slot=asset['material_bindings'][1]['slot'],material=self.pin)])

    def test_present_empty_mapping_cannot_drop_an_explicit_default_material(self):
        asset=deepcopy(self.imported.asset)
        asset['material_bindings']=[]
        with self.assertRaises(RigError): validate_document(asset,'asset')
        with self.assertRaises(RigError):
            project_material_slots(asset,overrides=[dict(slot='default',material=self.pin)])

    def test_integral_json_indexes_are_normalized_without_mutating_authored_descriptor(self):
        asset=deepcopy(self.imported.asset)
        for binding in asset['material_bindings']:
            binding['material_index']=float(binding['material_index'])
            for primitive in binding['primitives']:
                for key in ('source_mesh','source_primitive','source_material_index','normalized_mesh','normalized_primitive'):
                    if primitive[key] is not None: primitive[key]=float(primitive[key])
        before=deepcopy(asset)
        projected=project_material_slots(asset)
        for primitive in projected:
            self.assertIs(type(primitive['material_index']),int)
            self.assertIs(type(primitive['normalized_mesh']),int)
            self.assertIs(type(primitive['normalized_primitive']),int)
        self.assertEqual(asset,before)
        self.assertIs(type(asset['material_bindings'][0]['primitives'][0]['normalized_mesh']),float)
