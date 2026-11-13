"""Explicit optional real-model execution; does not assert semantic approval."""
import os
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
import unittest
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import decomposition_foreground as fg
from asset_contracts import sha
ROOT=Path(__file__).resolve().parents[1]

def workspace(name):
    retained=os.environ.get('HATCH_FOREGROUND_EVIDENCE_DIRECTORY')
    if retained:
        path=Path(retained)/name;path.mkdir(parents=True,exist_ok=False)
        return nullcontext(str(path))
    return tempfile.TemporaryDirectory()

@unittest.skipUnless(os.environ.get('HATCH_FOREGROUND_BACKEND_TEST')=='1', 'optional real CPU instance model')
class RealForegroundBackend(unittest.TestCase):
    def test_real_astronaut_foreground_and_source_fidelity(self):
        with workspace('astronaut-test') as tmp:
            root=Path(tmp);source=ROOT/'examples/decomposition/analysis/astronaut.png';before=sha(source)
            receipt=fg.propose(source,root/'proposals',weights=os.environ['HATCH_FOREGROUND_WEIGHTS'],
                enabled=True,origin='NASA public domain astronaut photograph',max_seconds=120)
            self.assertEqual(receipt['backend']['execution'],'local')
            self.assertFalse(receipt['anatomy_recognized'])
            self.assertEqual(receipt['review_status'],'pending')
            people=[c for c in receipt['candidates'] if c['category']=='person']
            self.assertEqual(len(people),1)
            self.assertGreater(people[0]['confidence'],.9)
            self.assertGreater(people[0]['pixels'],10000)
            run=fg.select(root/'proposals',[people[0]['id']],root/'selected')
            self.assertEqual(run['validation']['reconstruction_error']['max_channel_error'],0)
            self.assertFalse(run['analysis']['semantic_model_called'])
            self.assertEqual(sha(source),before)
            self.assertEqual(run['review']['status'],'pending')

    def test_real_two_people_masks_against_retained_ground_truth(self):
        data=os.environ.get('HATCH_FOREGROUND_PENNFUDAN')
        if not data:self.skipTest('set HATCH_FOREGROUND_PENNFUDAN to explicit dataset fixture directory')
        data=Path(data);source=data/'FudanPed00001.png';truth_path=data/'FudanPed00001_mask.png'
        self.assertEqual(sha(source),'130e9f9121b88ab69105f1685ce17625e97d0ef590adb9b9f076f62fc08b3343')
        self.assertEqual(sha(truth_path),'21ce5211d6745a43c0d1a31d2574810bb592971e1207b0b4a0b22e350daa6671')
        with workspace('two-people-test') as tmp:
            root=Path(tmp)
            receipt=fg.propose(source,root/'proposals',weights=os.environ['HATCH_FOREGROUND_WEIGHTS'],
                enabled=True,origin='Penn-Fudan FudanPed00001; dataset ground truth used only after inference')
            people=[c for c in receipt['candidates'] if c['category']=='person']
            self.assertEqual(len(people),2)
            with Image.open(truth_path) as im: truth=im.convert('L')
            matches=[]
            for label in (1,2):
                target=truth.point(lambda value:255 if value==label else 0)
                ious=[]
                for person in people:
                    mask=fg._mask(root/'proposals',person['mask'],truth.size)
                    ious.append(fg.count(fg.intersect(target,mask))/fg.count(fg.union(target,mask)))
                matches.append(max(range(len(ious)),key=ious.__getitem__))
                self.assertGreater(max(ious),.75)
            self.assertEqual(len(set(matches)),2)
            for index,person in enumerate(people):
                run=fg.select(root/'proposals',[person['id']],root/f'person-{index}')
                self.assertTrue(run['validation']['faithful_fidelity_passed'])
                self.assertEqual(run['validation']['semantic_status'],'pending')

if __name__=='__main__':unittest.main()
