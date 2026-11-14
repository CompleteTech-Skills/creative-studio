"""Strict mesh JSON contract parity against actual native interchange exports."""
import copy
import hashlib
import json
import math
from pathlib import Path
from unittest.mock import patch
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from jsonschema import Draft202012Validator
from model3d_mesh_contracts import (schemas,validate_export,validate_editable,
                                   EXPORT_SCHEMA,EDITABLE_SCHEMA)
from rig_errors import RigError


def editable_record():
    return dict(schema_version=1,kind='hatch-model3d-editable-topology',mesh='quad',
                positions=[[0,0,0],[1,0,0],[1,0,1],[0,0,1]],faces=[[0,1,2,3]],
                triangle_faces=[0,0],normal_mode='flat')


def export_record():
    return dict(schema_version=1,kind='hatch-model3d-mesh-export',format='glb',vertices=4,
        triangles=2,files={'model.glb':dict(bytes=42,sha256='0'*64)},
        mesh_identity=[dict(id='quad',name='Quad',region='',source_node=-1)],
        material_names=[None],exported_material_slots=[dict(slot=0,name='material_0')],
        limitations=['Standalone Geometry has no scene hierarchy, skin or animation authority.'])


class MeshContractsTests(unittest.TestCase):
    def test_real_native_export_schema_runtime_artifact_hashes(self):
        from model3d_primitives import primitive
        from model3d_interchange import export_mesh
        for format in ['glb','gltf','obj']:
            with self.subTest(format=format),tempfile.TemporaryDirectory() as tmp:
                editable=primitive('box',normal_mode='flat',name='semantic-box')
                geometry=editable.geometry()
                geometry.materials[0]['pbrMetallicRoughness']=dict(metallicFactor=0,roughnessFactor=.65)
                # OBJ reserves 'default', so use explicit neutral material identity.
                geometry.materials[0]['name']='neutral'
                destination=Path(tmp)/format
                report=export_mesh(geometry,destination,format=format,editable=editable)
                self.assertEqual(validate_export(report),report)
                Draft202012Validator(schemas()[EXPORT_SCHEMA]).validate(report)
                original=(destination/'editable.json').read_bytes()
                topology=json.loads(original)
                self.assertEqual(validate_editable(topology),topology)
                Draft202012Validator(schemas()[EDITABLE_SCHEMA]).validate(topology)
                for name,file in report['files'].items():
                    data=(destination/name).read_bytes()
                    self.assertEqual(len(data),file['bytes'])
                    self.assertEqual(hashlib.sha256(data).hexdigest(),file['sha256'])
                self.assertEqual((destination/'editable.json').read_bytes(),original)

    def test_fresh_valid_schemas_and_no_mutable_definition_leak(self):
        for schema in schemas().values():Draft202012Validator.check_schema(schema)
        bad=schemas();bad[EXPORT_SCHEMA]['properties'].clear()
        self.assertTrue(schemas()[EXPORT_SCHEMA]['properties'])
        self.assertEqual(validate_export(export_record()),export_record())
        self.assertEqual(validate_editable(editable_record()),editable_record())

    def test_integral_float_schema_runtime_parity_and_independent_copy(self):
        a=export_record()
        a['schema_version']=1.;a['vertices']=4.;a['triangles']=2.
        a['files']['model.glb']['bytes']=42.;a['mesh_identity'][0]['source_node']=-1.
        a['exported_material_slots'][0]['slot']=0.
        original=copy.deepcopy(a)
        Draft202012Validator(schemas()[EXPORT_SCHEMA]).validate(a)
        normalized=validate_export(a)
        self.assertEqual(a,original)
        self.assertIs(type(normalized['files']['model.glb']['bytes']),int)
        self.assertIs(type(normalized['exported_material_slots'][0]['slot']),int)
        normalized['mesh_identity'][0]['name']='changed'
        self.assertEqual(a['mesh_identity'][0]['name'],'Quad')
        b=editable_record();b['faces']=[[0.,1.,2.,3.]];b['triangle_faces']=[0.,0.]
        original=copy.deepcopy(b)
        Draft202012Validator(schemas()[EDITABLE_SCHEMA]).validate(b)
        normalized=validate_editable(b)
        self.assertEqual(b,original)
        self.assertTrue(all(type(v) is int for f in normalized['faces'] for v in f))
        self.assertTrue(all(type(v) is int for v in normalized['triangle_faces']))

    def test_bool_fraction_integer_boundaries_schema_runtime(self):
        for value in (True,False,1.5,-1,1000001):
            record=export_record();record['vertices']=value
            with self.subTest(value=value):
                self.assertFalse(Draft202012Validator(schemas()[EXPORT_SCHEMA]).is_valid(record))
                with self.assertRaises(RigError):validate_export(record)
        for value in (True,.5,-1,1000000):
            record=editable_record();record['faces'][0][0]=value
            with self.subTest(value=value):
                self.assertFalse(Draft202012Validator(schemas()[EDITABLE_SCHEMA]).is_valid(record))
                with self.assertRaises(RigError):validate_editable(record)

    def test_strict_unknown_missing_fields_every_level(self):
        for path in [(),('files','model.glb'),('mesh_identity',0),('exported_material_slots',0)]:
            record=export_record();target=record
            for component in path:target=target[component]
            target['unknown']=True
            with self.subTest(path=path),self.assertRaises(RigError):validate_export(record)
        for key in editable_record():
            record=editable_record();del record[key]
            with self.subTest(key=key),self.assertRaises(RigError):validate_editable(record)
        record=editable_record();record['unknown']=None
        with self.assertRaises(RigError):validate_editable(record)

    def test_hash_and_identifier_true_ending(self):
        for digest in ['0'*64+'\n','g'*64,'0'*63,'0'*65]:
            a=export_record();a['files']['model.glb']['sha256']=digest
            self.assertFalse(Draft202012Validator(schemas()[EXPORT_SCHEMA]).is_valid(a))
            with self.assertRaises(RigError):validate_export(a)
        for identifier in ['x\n','\x00','x'*129,'']:
            a=editable_record();a['mesh']=identifier
            self.assertFalse(Draft202012Validator(schemas()[EDITABLE_SCHEMA]).is_valid(a))
            with self.assertRaises(RigError):validate_editable(a)

    def test_paths_schema_runtime_lexical_portability(self):
        bad=['../evil','/absolute','a/../evil','C:/evil','a\\evil','nul','a/CON.txt',
             'a.','a ','a\n','a//b','./model.glb','a/./b']
        for path in bad:
            record=export_record();record['files'][path]=dict(bytes=1,sha256='1'*64)
            with self.subTest(path=path):
                self.assertFalse(Draft202012Validator(schemas()[EXPORT_SCHEMA]).is_valid(record))
                with self.assertRaises(RigError):validate_export(record)
        for extra in [{'MODEL.GLB':dict(bytes=1,sha256='1'*64)},
                      {'a':dict(bytes=1,sha256='1'*64),'a/b':dict(bytes=1,sha256='1'*64)},
                      {'cafe\u0301.png':dict(bytes=1,sha256='1'*64)}]:
            a=export_record();a['files'].update(extra)
            with self.assertRaises(RigError):validate_export(a)

    def test_export_semantic_identity_material_and_primary_artifact(self):
        records=[]
        a=export_record();a['mesh_identity']*=2;a['vertices']=8;records.append(a)
        a=export_record();a['exported_material_slots']*=2;records.append(a)
        a=export_record();a['exported_material_slots'][0]['slot']=1;records.append(a)
        a=export_record();a['exported_material_slots'][0]['name']='wrong';records.append(a)
        a=export_record();a['files']={'different.glb':dict(bytes=1,sha256='0'*64)};records.append(a)
        a=export_record();a['files']['manifest.json']=dict(bytes=1,sha256='0'*64);records.append(a)
        for record in records:
            with self.subTest(record=record),self.assertRaises(RigError):validate_export(record)

    def test_aggregate_byte_limit(self):
        a=export_record()
        a['files']={('model.glb' if i==0 else str(i)+'.bin'):dict(bytes=256*1024**2,sha256='0'*64) for i in range(5)}
        with self.assertRaises(RigError):validate_export(a)

    def test_aggregate_metadata_bound_and_obj_name_policy(self):
        with patch('model3d_mesh_contracts.MAX_JSON_BYTES',100):
            with self.assertRaises(RigError):validate_editable(editable_record())
            with self.assertRaises(RigError):validate_export(export_record())
        a=export_record();a['format']='obj';a['files']={'model.obj':dict(bytes=1,sha256='0'*64),
                                                    'materials.mtl':dict(bytes=1,sha256='0'*64)}
        for name in [' unsafe','bad\nname']:
            a['mesh_identity'][0]['name']=name
            with self.assertRaises(RigError):validate_export(a)

    def test_editable_semantic_indices_and_ownership(self):
        cases=[]
        a=editable_record();a['faces'][0]=[0,1,1,3];cases.append(a)
        a=editable_record();a['faces'][0]=[0,1,2,4];cases.append(a)
        a=editable_record();a['triangle_faces']=[0];cases.append(a)
        a=editable_record();a['triangle_faces']=[0,1];cases.append(a)
        a=editable_record();a['faces']=[[0,1,2],[0,2,3]];a['triangle_faces']=[0,0];cases.append(a)
        for record in cases:
            with self.subTest(record=record),self.assertRaises(RigError):validate_editable(record)

    def test_nonfinite_coordinates_and_metadata_never_accepted(self):
        for value in [float('nan'),float('inf'),-float('inf')]:
            a=editable_record();a['positions'][0][0]=value
            with self.assertRaises(RigError):validate_editable(a)
            b=export_record();b['vertices']=value
            with self.assertRaises(RigError):validate_export(b)
        a=editable_record();a['positions'][0][0]=True
        with self.assertRaises(RigError):validate_editable(a)


if __name__=='__main__':unittest.main()
