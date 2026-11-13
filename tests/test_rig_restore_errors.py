"""Corrupt transport/container failures stay structured without masking bugs."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import numpy as np
from rig_errors import RigError
from rig_models import load_rig,rig_signature
from rig_package import package_rig,restore_rig
from rig_skinning import evaluate_deformation
from rig_test_helpers import fixture


def corrupt_member(payload, *, codec=None, first_byte=None):
    result=bytearray(payload)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        member=archive.getinfo('inventory.json')
        local=member.header_offset
        name_size,extra_size=struct.unpack_from('<HH',result,local+26)
        data=local+30+name_size+extra_size
        if codec is not None:
            struct.pack_into('<H',result,local+8,codec)
            central=result.index(b'PK\x01\x02')
            struct.pack_into('<H',result,central+10,codec)
        elif first_byte is not None:result[data]=first_byte
        else:result[data]^=1
    return bytes(result)


def inventory_archive(compression=zipfile.ZIP_STORED):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression=compression) as archive:
        archive.writestr('inventory.json',json.dumps(dict(schema_version=1,kind='hatch-3d-rig-package',rig_sha256='0'*64,files=[])))
    return stream.getvalue()


class RestoreErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        cls.rig=fixture(cls.root,name='valid-source')
        cls.valid=cls.root/'valid.zip';package_rig(cls.rig.root,cls.valid)
        cls.original=cls.valid.read_bytes()
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def reject(self,payload,name,code='RIG_ARCHIVE_FORMAT'):
        archive=self.root/(name+'.zip');archive.write_bytes(payload)
        destination=self.root/(name+'-restored')
        staged=set(self.root.glob('.rig-restore-*'))
        with self.assertRaises(RigError) as caught:restore_rig(archive,destination)
        report=caught.exception.report
        self.assertEqual(report['code'],code);self.assertEqual(report['stage'],'restore')
        self.assertEqual(report['preserved'],str(archive));self.assertTrue(report['correction'])
        self.assertEqual(archive.read_bytes(),payload);self.assertFalse(destination.exists())
        self.assertEqual(set(self.root.glob('.rig-restore-*')),staged)
        self.assertEqual(self.valid.read_bytes(),self.original)
        return report

    def test_malformed_and_truncated_outer_containers_are_structured(self):
        self.reject(b'not a zip archive','malformed')
        self.reject(self.original[:-30],'truncated')
    def test_bad_inventory_crc_and_deflate_stream_are_structured(self):
        crc=self.reject(corrupt_member(inventory_archive()),'bad-crc')
        self.assertIn('BadZipFile',crc['observed'])
        stream=self.reject(corrupt_member(inventory_archive(zipfile.ZIP_DEFLATED),first_byte=7),'bad-deflate')
        self.assertTrue('error' in stream['observed'] or 'BadZipFile' in stream['observed'])
    def test_unsupported_zip_member_compression_is_structured(self):
        self.reject(corrupt_member(inventory_archive(),codec=99),'unsupported-codec','RIG_ARCHIVE_CODEC')
    def test_hash_consistent_package_with_invalid_native_bundle_is_structured(self):
        with zipfile.ZipFile(io.BytesIO(self.original)) as archive:
            payloads={name:archive.read(name) for name in archive.namelist()}
        manifest=json.loads(payloads['rig.json']);native=manifest['geometry']['data']['path']
        payloads[native]=b'not a native ZIP bundle'
        manifest['geometry']['data']['sha256']=hashlib.sha256(payloads[native]).hexdigest()
        payloads['rig.json']=(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()
        inventory=json.loads(payloads['inventory.json']);inventory['rig_sha256']=rig_signature(manifest)
        for item in inventory['files']:
            data=payloads[item['path']];item.update(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data))
        payloads['inventory.json']=json.dumps(inventory).encode()
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for name,data in payloads.items():archive.writestr(name,data)
        self.reject(stream.getvalue(),'invalid-native')
    def test_cli_emits_machine_readable_error_for_corrupt_container(self):
        from rig import main
        source=self.root/'cli-invalid.zip';source.write_bytes(b'invalid')
        output=io.StringIO()
        with redirect_stdout(output):result=main(['restore','--file',str(source),'--destination',str(self.root/'cli-invalid-out')])
        self.assertEqual(result,1);self.assertEqual(json.loads(output.getvalue())['error']['code'],'RIG_ARCHIVE_FORMAT')
    def test_existing_semantic_errors_and_programming_errors_are_not_reclassified(self):
        for error in (RigError('RIG_PROBE_SEMANTIC','specific rejection'),TypeError('programming defect'),RuntimeError('unexpected defect')):
            target=self.root/('propagate-'+type(error).__name__)
            with self.subTest(error=type(error).__name__),patch('rig_package.load_rig',side_effect=error):
                with self.assertRaises(type(error)) as caught:restore_rig(self.valid,target)
                self.assertIs(caught.exception,error)
                self.assertFalse(target.exists())
            self.assertFalse(list(self.root.glob('.rig-restore-*')))
    def test_valid_archive_still_reopens_and_evaluates_identical_geometry(self):
        destination=self.root/'valid-restored';report=restore_rig(self.valid,destination)
        self.assertTrue(report['verified']);restored=load_rig(destination)
        self.assertEqual(rig_signature(restored.manifest),rig_signature(self.rig.manifest))
        a,b=evaluate_deformation(self.rig),evaluate_deformation(restored)
        for name in a:np.testing.assert_allclose(a[name]['positions'],b[name]['positions'],atol=1e-12)


if __name__=='__main__':unittest.main()
