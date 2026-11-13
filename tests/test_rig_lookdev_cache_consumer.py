import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev_texture_blender import create_cached_blender_image, _MODULE_SHA256
from lookdev_texture_cache import DerivedTextureCache
from rig_errors import RigError


class ConsumerAdmissionTests(unittest.TestCase):
    def test_import_snapshot_binds_actual_helper_module_sources(self):
        root=Path(__file__).resolve().parents[1]/'scripts'
        self.assertEqual(set(_MODULE_SHA256),{'lookdev_texture_blender.py','lookdev_texture_cache.py',
            'lookdev_textures.py','lookdev_contracts.py','rig_errors.py'})
        for name,digest in _MODULE_SHA256.items():
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),digest)

    def test_preloaded_helper_from_other_checkout_cannot_claim_local_source(self):
        root=Path(__file__).resolve().parents[1]/'scripts'
        with tempfile.TemporaryDirectory() as temporary:
            alternate=Path(temporary)
            (alternate/'rig_errors.py').write_bytes((root/'rig_errors.py').read_bytes()+b'\n# alternate source\n')
            command=("import sys;sys.path.insert(0,"+repr(str(alternate))+");import rig_errors;"
                     "sys.path.insert(0,"+repr(str(root))+");import lookdev_texture_blender")
            result=subprocess.run([sys.executable,'-c',command],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('same frozen source checkout',result.stdout)

    def test_invalid_requests_reject_without_blender_import(self):
        for kwargs in ({'mip_level':True},{'mip_level':-1},{'name':''}):
            values=dict(name='probe');values.update(kwargs)
            with self.assertRaises(RigError):create_cached_blender_image(DerivedTextureCache(),None,**values)


@unittest.skipUnless(os.environ.get('HATCH_LOOKDEV_CACHE_TEST')=='1','Enable real Blender cache consumer gate.')
class ActualCacheConsumerTests(unittest.TestCase):
    def test_cached_upload_reaches_real_cycles_shader_and_packed_reopen(self):
        from PIL import Image
        root=Path(__file__).resolve().parents[1]
        evidence=os.environ.get('HATCH_LOOKDEV_CACHE_EVIDENCE')
        temporary=None if evidence else tempfile.TemporaryDirectory(prefix='lookdev-cache-native-')
        if temporary:self.addCleanup(temporary.cleanup)
        output=Path(evidence or temporary.name);output.mkdir(parents=True,exist_ok=True)
        self.assertFalse((output/'job.json').exists())
        (output/'job.json').write_text(json.dumps({'scripts':str(root/'scripts'),'output':str(output)}))
        result=subprocess.run([os.environ['HATCH_BLENDER'],'--background','--factory-startup',
            '--disable-autoexec','--threads','1','--python-exit-code','1','--python',
            str(root/'tests/fixtures/lookdev_cache_consumer_probe.py'),'--',str(output/'job.json')],
            text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=180)
        (output/'render.log').write_text(result.stdout)
        self.assertEqual(result.returncode,0,result.stdout[-4000:])
        red=Image.open(output/'red.png').convert('RGB').getpixel((16,16))
        blue=Image.open(output/'blue.png').convert('RGB').getpixel((16,16))
        self.assertGreater(red[0],250);self.assertLess(red[2],5)
        self.assertGreater(blue[2],250);self.assertLess(blue[0],5)
        report=json.loads((output/'report.json').read_text())
        self.assertEqual(report['reopen'],'exact-blue-image-and-derivation')
        for name,digest in report['render_sha256'].items():
            self.assertEqual(hashlib.sha256((output/name).read_bytes()).hexdigest(),digest)
