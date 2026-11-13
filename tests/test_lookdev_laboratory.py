"""Independent oracle images exercise measurement failure boundaries."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from lookdev_laboratory import highlight_metrics,sweep_documents,measure_sweep
from lookdev_contracts import Registry,canonical_bytes
from rig_errors import RigError

class LaboratoryTests(unittest.TestCase):
    def test_nine_independent_canonical_combinations_with_one_controlled_surface(self):
        documents=sweep_documents()
        self.assertEqual({(x['surface']['metallic'],x['surface']['roughness']) for x in documents},
                         {(m,r) for m in (0,.5,1) for r in (.1,.5,.9)})
        Registry([(x['id']+'.json',canonical_bytes(x)) for x in documents])
        controls=[]
        for value in documents:
            surface=dict(value['surface']);surface.pop('metallic');surface.pop('roughness');controls.append(surface)
        self.assertTrue(all(x==controls[0] for x in controls))
        documents[0]['surface']['base_color'][0]=1
        self.assertEqual(documents[1]['surface']['base_color'][0],.12)

    def test_highlight_width_detects_known_narrow_and_broad_radiance_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            values=[]
            for radius in (3,12):
                y,x=np.indices((64,64));alpha=((x-32)**2+(y-32)**2<30**2)
                light=(.1+.8*np.exp(-((x-28)**2+(y-25)**2)/(2*radius**2)))
                rgba=np.stack([light,light,light,alpha],axis=-1)
                path=Path(temporary)/f'{radius}.png';Image.fromarray((rgba*255).astype('uint8')).save(path)
                values.append(highlight_metrics(path))
            self.assertLess(values[0]['half_height_pixels'],values[1]['half_height_pixels'])
            self.assertLess(values[0]['excess_rms_radius_pixels'],values[1]['excess_rms_radius_pixels'])

    def test_generic_differences_cannot_accept_reversed_highlight_physics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); report={'materials':{}}
            y,x=np.indices((64,64));alpha=((x-32)**2+(y-32)**2<30**2)
            for resource in sweep_documents():
                identifier=resource['id'];surface=resource['surface']
                roughness=surface['roughness'];metallic=surface['metallic']
                # Deliberately wrong physics: increasing roughness sharpens and
                # strengthens the highlight, while all nine RGB images differ.
                radius=15-roughness*13;amplitude=.3+roughness*.5
                light=.02+metallic*.015+amplitude*np.exp(-((x-28)**2+(y-25)**2)/(2*radius**2))
                rgba=np.stack([light,light*(1-metallic*.2),light,alpha],axis=-1)
                Image.fromarray((rgba*255).astype('uint8')).save(root/(identifier+'.png'))
                report['materials'][identifier]={'metallic':metallic,'roughness':roughness}
            (root/'render-report.json').write_text(json.dumps(report))
            with self.assertRaises(RigError) as raised:measure_sweep(root)
            self.assertEqual(raised.exception.report['code'],'LOOKDEV_LAB_HIGHLIGHT')

    def test_empty_silhouette_and_flat_signal_cannot_be_claimed_as_highlight(self):
        with tempfile.TemporaryDirectory() as temporary:
            for alpha in (0,255):
                path=Path(temporary)/f'{alpha}.png'
                Image.new('RGBA',(64,64),(50,50,50,alpha)).save(path)
                with self.assertRaises(RigError):highlight_metrics(path)

if __name__=='__main__':unittest.main()
