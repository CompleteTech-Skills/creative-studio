import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev_texture_sampling import sampler_plan
from rig_errors import RigError


class SamplerAdmissionTests(unittest.TestCase):
    def document(self):
        return json.loads((Path(__file__).resolve().parents[1]/'examples/lookdev-contracts/albedo.json').read_text())

    def test_canonical_linear_repeat_and_nearest_clamp_map_explicitly(self):
        document=self.document();plan=sampler_plan(document)
        self.assertEqual((plan['interpolation'],plan['extension']),('Linear','REPEAT'))
        document.update(filter='nearest',wrap='clamp');other=sampler_plan(document)
        self.assertEqual((other['interpolation'],other['extension']),('Closest','EXTEND'))
        self.assertNotEqual(plan['document_sha256'],other['document_sha256'])

    def test_unverified_modes_reject_instead_of_silent_defaults(self):
        for key,value in [('filter','mipmap'),('wrap','mirror')]:
            document=self.document();document[key]=value
            with self.assertRaises(RigError):sampler_plan(document)


@unittest.skipUnless(os.environ.get('HATCH_LOOKDEV_SAMPLING_TEST')=='1','Enable actual native sampler gate.')
class ActualSamplerTests(unittest.TestCase):
    def test_canonical_modes_change_real_shader_samples(self):
        import numpy as np
        from PIL import Image
        from lookdev_textures import encode_png,verify_texture_document
        root=Path(__file__).resolve().parents[1]
        output=Path(os.environ['HATCH_LOOKDEV_SAMPLING_EVIDENCE']);output.mkdir(parents=True,exist_ok=True)
        self.assertFalse((output/'job.json').exists())
        pixels=np.zeros((4,4,4),np.float32);pixels[:,:2,0]=1;pixels[:,2:,2]=1;pixels[:,:,3]=1
        encoded=encode_png(pixels,encoding='srgb')
        document=SamplerAdmissionTests().document();document.update(width=4,height=4)
        document['file'].update(bytes=len(encoded),sha256=hashlib.sha256(encoded).hexdigest())
        decoded=verify_texture_document(document,encoded)
        (output/'original.png').write_bytes(encoded)
        (output/'job.json').write_text(json.dumps(dict(scripts=str(root/'scripts'),output=str(output),
                                                       document=document,pixels=decoded.pixels.tolist())))
        result=subprocess.run([os.environ['HATCH_BLENDER'],'--background','--factory-startup',
            '--disable-autoexec','--python-use-system-env','--threads','1','--python-exit-code','1','--python',
            str(root/'tests/fixtures/lookdev_sampling_probe.py'),'--',str(output/'job.json')],
            text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=180)
        (output/'render.log').write_text(result.stdout)
        self.assertEqual(result.returncode,0,result.stdout[-4000:])
        colors={label:Image.open(output/(label+'.png')).convert('RGB').getpixel((16,16))
                for label in ('linear','nearest','repeat','clamp')}
        self.assertGreater(colors['linear'][0],80);self.assertGreater(colors['linear'][2],80)
        self.assertLess(min(colors['nearest'][0],colors['nearest'][2]),5)
        self.assertGreater(colors['repeat'][0],250);self.assertLess(colors['repeat'][2],5)
        self.assertGreater(colors['clamp'][2],250);self.assertLess(colors['clamp'][0],5)
        report=json.loads((output/'report.json').read_text())
        for case in report['cases']:
            self.assertEqual(hashlib.sha256((output/(case['label']+'.png')).read_bytes()).hexdigest(),case['sha256'])
        (output/'measurements.json').write_text(json.dumps(colors,indent=2))
