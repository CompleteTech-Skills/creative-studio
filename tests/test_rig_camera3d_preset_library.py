"""Real bounded parameter snapshots, immutable revisions and file roundtrips."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from camera3d_presets import CAMERA_PRESETS,MOVEMENT_PRESETS,SHOT_PRESETS
from camera3d_preset_library import (schema,create_library_preset,revise_library_preset,
    validate_library_preset,export_library_preset,import_library_preset,sample_library_movement)


class Library(unittest.TestCase):
    def test_every_curated_template_roundtrips_actual_parameters_in_project_and_global_scopes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for scope in ('project','global'):
                (root/scope).mkdir()
                for family,names in [('camera',CAMERA_PRESETS),('movement',MOVEMENT_PRESETS),('shot',SHOT_PRESETS)]:
                    for name in names:
                        with self.subTest(scope=scope,family=family,name=name):
                            document=create_library_preset(family,name)
                            path=root/scope/(family+'-'+name+'.json')
                            digest=export_library_preset(document,path)
                            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),digest)
                            self.assertEqual(import_library_preset(path),document)
                            before=path.read_bytes()
                            with self.assertRaises(ValueError):export_library_preset(document,path)
                            self.assertEqual(path.read_bytes(),before)

    def test_custom_revisions_duplicates_and_reset_preserve_previous_sources(self):
        original=create_library_preset('movement','slow-dolly-in')
        before=deepcopy(original)
        changed=revise_library_preset(original,'1.0.1',overrides={'parameters':{'distance_m':7}})
        duplicate=revise_library_preset(changed,'1.0.0',identifier='custom-dolly')
        self.assertEqual(original,before)
        self.assertEqual(changed['content']['parameters']['distance_m'],7)
        self.assertEqual(duplicate['id'],'custom-dolly')
        self.assertEqual(create_library_preset('movement','slow-dolly-in'),original)
        for version in ['1.0.0','0.9.9','1.0',True]:
            with self.subTest(version=version),self.assertRaises(ValueError):revise_library_preset(original,version)
        for identifier in ['',True,original['id']]:
            with self.subTest(identifier=identifier),self.assertRaises(ValueError):revise_library_preset(original,'1.0.0',identifier=identifier)
        shot=create_library_preset('shot','hero-shot')
        revised=revise_library_preset(shot,'1.1.0',overrides={'subject_frame_fraction':.6})
        self.assertEqual(revised['content']['subject_frame_fraction'],.6)
        self.assertEqual(shot['content']['subject_frame_fraction'],.85)

    def test_imported_custom_movement_runs_through_actual_owner_sampler(self):
        from camera3d_presets import camera_preset
        custom=create_library_preset('movement','slow-dolly-in',overrides={'parameters':{'distance_m':2}})
        camera=camera_preset('cinematic-35mm');camera['transform']['translation']=[0,0,8]
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'custom.json';export_library_preset(custom,path)
            restored=import_library_preset(path)
            result=sample_library_movement(restored,camera['transform'],camera['optics'],
                {'frame':12},24,target_position=[0,0,0])
            self.assertEqual(result['transform']['translation'],[0,0,7])
            self.assertTrue(result['target_aim'])
            changed=revise_library_preset(restored,'1.0.1',overrides={'parameters':{'distance_m':4}})
            farther=sample_library_movement(changed,camera['transform'],camera['optics'],
                {'frame':12},24,target_position=[0,0,0])
            self.assertEqual(farther['transform']['translation'],[0,0,6])
            self.assertNotEqual(result['dependency_sha256'],farther['dependency_sha256'])
        with self.assertRaises(ValueError):sample_library_movement(
            create_library_preset('camera','standard-50mm'),camera['transform'],camera['optics'],{'frame':0},24)

    def test_invalid_versions_parameters_algorithms_and_hidden_fields_fail(self):
        examples=[create_library_preset('movement','handheld'),create_library_preset('camera','standard-50mm')]
        corruptions=[]
        for original in examples:
            for key,value in [('version','2'),('id',''),('unexpected',1)]:
                changed=deepcopy(original);changed[key]=value;corruptions.append(changed)
            changed=deepcopy(original);changed['content']['preset_version']='2.0.0';corruptions.append(changed)
        changed=deepcopy(examples[0]);changed['content']['parameters']['amplitude_m']=-1;corruptions.append(changed)
        changed=deepcopy(examples[0]);changed['content']['motion']='dolly';corruptions.append(changed)
        for key in ('amplitude_m','frequency_hz'):
            changed=deepcopy(examples[0]);changed['content']['parameters'][key]=1000.1;corruptions.append(changed)
        changed=deepcopy(examples[0]);changed['content']['parameters']['seed']=True;corruptions.append(changed)
        changed=deepcopy(examples[1]);changed['content']['transform']['scale']=[2,1,1];corruptions.append(changed)
        for value in corruptions:
            with self.subTest(value=value),self.assertRaises(ValueError):validate_library_preset(value)
        for overrides in [[],{'unknown':1}]:
            with self.assertRaises(ValueError):create_library_preset('movement','handheld',overrides=overrides)

    def test_untrusted_import_rejects_duplicate_keys_symlinks_and_bounded_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source.json'
            source.write_text('{"schema_version":1,"schema_version":1}')
            with self.assertRaises(ValueError):import_library_preset(source)
            source.write_bytes(b' '* (8*1024*1024+1))
            with self.assertRaises(ValueError):import_library_preset(source)
            source.unlink();export_library_preset(create_library_preset('camera','standard-50mm'),source)
            link=root/'link.json';link.symlink_to(source)
            with self.assertRaises(ValueError):import_library_preset(link)
            with self.assertRaises(ValueError):export_library_preset(create_library_preset('camera','standard-50mm'),link)

    def test_generated_schema_is_current(self):
        self.assertEqual(json.loads((ROOT/'profiles/camera3d-library-preset.schema.json').read_text()),schema())


if __name__=='__main__':unittest.main()
