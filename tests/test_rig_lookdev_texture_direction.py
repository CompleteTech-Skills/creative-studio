"""Analytic directional ground truth, beyond merely changed render pixels."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('HATCH_LOOKDEV_TEXTURE_TEST')=='1',
                     'Enable actual Blender directional normal-map rendering.')
class DirectionTests(unittest.TestCase):
    def test_world_space_tangent_direction_matches_analytic_geometry(self):
        import numpy as np
        evidence=os.environ.get('HATCH_LOOKDEV_DIRECTION_EVIDENCE')
        temporary=None if evidence else tempfile.TemporaryDirectory(prefix='texture-direction-')
        if temporary:self.addCleanup(temporary.cleanup)
        output=Path(evidence or temporary.name)
        output.mkdir(parents=True,exist_ok=True)
        self.assertFalse((output/'report.json').exists(),'Use a new evidence directory.')
        fixture=Path(__file__).parent/'fixtures/lookdev_texture_direction_probe.py'
        result=subprocess.run([os.environ['HATCH_BLENDER'],'--background','--factory-startup',
            '--disable-autoexec','--threads','1','--python-exit-code','1','--python',str(fixture),
            '--',str(output)],text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=180)
        (output/'render.log').write_text(result.stdout)
        self.assertEqual(result.returncode,0,result.stdout[-5000:])
        report=json.loads((output/'report.json').read_text())
        # The texture stores encoded tangent vector (.36,.48,.8). Mirror U flips
        # tangent X; rotating UV=(v,1-u) maps +U to world +Y. Nonuniform XY
        # object scale leaves the normalized world tangent unchanged.
        expected={'base':[.36,.48,.8],'mirror':[-.36,.48,.8],
                  'rotate':[-.48,.36,.8],'scale':[.36,.48,.8],
                  'deform':np.array([.16,.48*np.sqrt(1.0625),.89])/np.sqrt(1.0625)}
        errors={}
        for case,vector in expected.items():
            decoded=np.array(report[case])*2-1
            np.testing.assert_allclose(decoded,vector,atol=.002,rtol=0,err_msg=case)
            errors[case]=float(np.max(abs(decoded-vector)))
        (output/'analytic-errors.json').write_text(json.dumps(errors,indent=2))
