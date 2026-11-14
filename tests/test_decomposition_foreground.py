"""Synthetic contract fixtures only; these do not count as model evidence."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import decomposition_foreground as fg
from asset_contracts import sha
from character_decomposition import load_decomposition

class ForegroundContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'proposals'; self.root.mkdir()
        (self.root/'masks').mkdir(); (self.root/'source').mkdir()
        image = Image.new('RGBA', (8, 6), (24, 60, 90, 77)); image.save(self.root/'source/original.png')
        first = Image.new('L', image.size); first.paste(255, (0, 0, 5, 6))
        second = Image.new('L', image.size); second.paste(255, (3, 0, 8, 6))
        first.save(self.root/'masks/candidate-1.png'); second.save(self.root/'masks/candidate-2.png')
        fg.intersect(first, second).save(self.root/'masks/overlap.png'); image.save(self.root/'overlay.png')
        candidates = [{'id': 'candidate-'+str(index), 'category': 'person', 'confidence': .9,
            'mask': fg._asset(self.root, 'masks/candidate-'+str(index)+'.png'),
            'bbox': list(mask.getbbox()), 'pixels': fg.count(mask)} for index,mask in enumerate([first,second],1)]
        self.receipt = {'schema_version': 1, 'kind': 'foreground-proposals',
            'source': {'asset': fg._asset(self.root, 'source/original.png'), 'width': 8, 'height': 6,
                       'origin': 'Synthetic fixture, no actual model execution', 'fixture': True},
            'backend': {'name': 'torchvision-maskrcnn-coco-v1', 'weights_sha256': fg.MODEL_SHA256,
                        'torch': '2.10.0+cpu', 'torchvision': '0.25.0+cpu', 'execution': 'local'},
            'parameters': {'score_threshold': .7, 'mask_threshold': .5, 'threads': 2, 'max_seconds': 120, 'max_pixels': fg.MAX_PIXELS},
            'elapsed_seconds': 1, 'candidates': candidates,
            'overlap': fg._asset(self.root, 'masks/overlap.png'), 'overlay': fg._asset(self.root, 'overlay.png'),
            'uncertainty': ['Test fixture only; this receipt exercises validation, not inference.'],
            'anatomy_recognized': False, 'review_status': 'pending'}
        self.save()

    def save(self):
        (self.root/'foreground-proposals.json').write_text(json.dumps(self.receipt))

    def test_valid_native_binary_masks_and_retained_overlap(self):
        self.assertEqual(len(fg.load(self.root)['candidates']), 2)
        self.assertEqual(fg.count(fg._mask(self.root,self.receipt['overlap'],(8,6))),12)

    def test_selection_preserves_rgba_and_portable_receipt(self):
        before = sha(self.root/'source/original.png')
        destination = Path(self.temp.name)/'selected'
        selected = fg.select(self.root,['candidate-1','candidate-2'],destination)
        self.assertTrue(selected['validation']['faithful_fidelity_passed'])
        self.assertEqual(selected['validation']['reconstruction_error']['max_channel_error'],0)
        self.assertEqual(selected['validation']['semantic_status'],'pending')
        self.assertFalse(selected['analysis']['semantic_model_called'])
        self.assertTrue(selected['source']['fixture'])
        self.assertEqual(selected['quality']['partial_alpha_pixels'],48)
        self.assertEqual(sha(self.root/'source/original.png'),before)
        self.assertEqual(len(fg.load(destination/'foreground-analysis')['candidates']),2)
        self.assertEqual(load_decomposition(destination)['source']['sha256'],before)

    def test_current_runtime_counts_are_bound_and_historical_threads_remain_readable(self):
        self.assertEqual(fg.load(self.root)['parameters']['threads'],2)
        self.receipt['parameters']['threads']=1
        self.receipt['runtime']={'torch_threads':1,'torch_interop_threads':1};self.save()
        fg.load(self.root)
        self.receipt['runtime']['torch_threads']=2;self.save()
        with self.assertRaises(ValueError):fg.load(self.root)

    def test_unknown_duplicate_empty_selection_rejected(self):
        for ids in [[],['bogus'],['candidate-1','candidate-1']]:
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                fg.select(self.root,ids,Path(self.temp.name)/'bad')

    def test_proposal_timeout_cleans_pinned_stage_after_replacement(self):
        moved = Path(self.temp.name) / 'moved-proposal'
        foreign = None
        def interrupt(command, **kwargs):
            nonlocal foreign
            payload = Path(command[3])
            outer = Path(os.readlink(payload.parent))
            outer.rename(moved)
            outer.mkdir()
            foreign = outer / 'foreign'
            foreign.write_text('untouched')
            raise fg.subprocess.TimeoutExpired(command, 1)
        destination = Path(self.temp.name) / 'timed-out'
        with patch.object(fg, 'capabilities', return_value={'available': True, 'versions': {}}), \
             patch.object(fg, '_weight', return_value=Path('unused')), \
             patch.object(fg.subprocess, 'run', side_effect=interrupt):
            with self.assertRaisesRegex(ValueError, 'runtime limit'):
                fg.propose(self.root/'source/original.png', destination, weights='unused',
                           enabled=True, origin='fixture', max_seconds=1)
        self.assertFalse(destination.exists())
        self.assertEqual(foreign.read_text(), 'untouched')
        self.assertEqual(list(moved.iterdir()), [])

    def test_selection_namespace_replacement_preserves_foreign_tree(self):
        real_create = fg.create_decomposition
        moved = Path(self.temp.name) / 'moved-wrapper'
        foreign = None
        def replace_wrapper(source, run, **kwargs):
            nonlocal foreign
            outer = Path(os.readlink(run.parent))
            outer.rename(moved)
            outer.mkdir()
            foreign = outer / 'foreign'
            foreign.write_text('untouched')
            return real_create(source, run, **kwargs)
        destination = Path(self.temp.name) / 'selected-race'
        with patch.object(fg, 'create_decomposition', side_effect=replace_wrapper):
            fg.select(self.root, ['candidate-1'], destination)
        self.assertEqual(foreign.read_text(), 'untouched')
        self.assertEqual(list(moved.iterdir()), [])
        self.assertEqual(load_decomposition(destination)['source']['sha256'], sha(self.root/'source/original.png'))

    def test_selection_failure_cleans_owned_tree_only_after_replacement(self):
        real_create = fg.create_decomposition
        moved = Path(self.temp.name) / 'moved-failure'
        foreign = None
        def replace_then_fail(source, run, **kwargs):
            nonlocal foreign
            outer = Path(os.readlink(run.parent))
            outer.rename(moved)
            outer.mkdir()
            foreign = outer / 'foreign'
            foreign.write_text('untouched')
            real_create(source, run, **kwargs)
            raise ValueError('injected failure after real creation')
        destination = Path(self.temp.name) / 'failed-race'
        with patch.object(fg, 'create_decomposition', side_effect=replace_then_fail):
            with self.assertRaisesRegex(ValueError, 'injected failure'):
                fg.select(self.root, ['candidate-1'], destination)
        self.assertFalse(destination.exists())
        self.assertEqual(foreign.read_text(), 'untouched')
        self.assertEqual(list(moved.iterdir()), [])

    def test_selection_does_not_overwrite_existing_directory(self):
        destination=Path(self.temp.name)/'exists';destination.mkdir()
        (destination/'retained').write_text('keep')
        with self.assertRaises(ValueError):fg.select(self.root,['candidate-1'],destination)
        self.assertEqual((destination/'retained').read_text(),'keep')

    def test_tampered_or_symlink_source_is_rejected(self):
        path=self.root/'source/original.png';backup=self.root/'backup.png';path.rename(backup);path.symlink_to(backup)
        with self.assertRaises(ValueError):fg.load(self.root)
        path.unlink();backup.rename(path)
        path.write_bytes(path.read_bytes()+b'change')
        with self.assertRaises(ValueError):fg.load(self.root)

    def test_admitted_image_hash_and_decode_share_the_same_bytes(self):
        path = self.root/'source/original.png'
        expected = sha(path)
        original = fg._record_bytes
        def replace_after_read(target, maximum):
            data = original(target, maximum)
            Image.new('RGBA', (8, 6), (240, 20, 20, 255)).save(path)
            return data
        with patch.object(fg, '_record_bytes', side_effect=replace_after_read):
            image = fg._verified_image(path, expected)
        self.assertEqual(image.getpixel((0, 0)), (24, 60, 90, 77))
        self.assertNotEqual(sha(path), expected)

    def test_invalid_hash_rejected_before_image_decoder(self):
        with patch.object(fg.Image, 'open', side_effect=AssertionError('decoder called')):
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                fg._verified_image(self.root/'source/original.png', '0'*64)

    def test_record_byte_bounds_preserve_default_and_reject_invalid_limits(self):
        path = self.root/'record.bin'; path.write_bytes(b'abcde')
        self.assertEqual(fg._record_bytes(path, 5), b'abcde')
        with self.assertRaisesRegex(ValueError, 'bounded regular'):
            fg._record_bytes(path, 4)
        for bound in (True, 0, -1, 200_000_001, 5.0):
            with self.subTest(bound=bound), self.assertRaises(ValueError):
                fg._record_bytes(path, bound)

    def test_selection_rechecks_source_before_creating_decomposition(self):
        original = fg._mask
        calls = 0
        def replace_source_after_selection(*args):
            nonlocal calls
            mask = original(*args)
            calls += 1
            if calls == 4:
                Image.new('RGBA', (8, 6), (240, 20, 20, 255)).save(self.root/'source/original.png')
            return mask
        destination = Path(self.temp.name)/'changed-selection'
        with patch.object(fg, '_mask', side_effect=replace_source_after_selection), \
             patch.object(fg, 'create_decomposition', side_effect=AssertionError('unexpected create')):
            with self.assertRaisesRegex(ValueError, 'source changed'):
                fg.select(self.root, ['candidate-1'], destination)
        self.assertFalse(destination.exists())

    def test_geometry_confidence_and_identity_validation(self):
        for mutate in [lambda r:r['candidates'][0].update(pixels=1),
                       lambda r:r['candidates'][0].update(bbox=[0,0,8,6]),
                       lambda r:r['candidates'][0].update(confidence=.1),
                       lambda r:r['candidates'][0].update(id='candidate-2'),
                       lambda r:r['backend'].update(weights_sha256='0'*64),
                       lambda r:r['backend'].update(torch='2.0.0'),
                       lambda r:r.update(review_status='approved'),
                       lambda r:r.update(anatomy_recognized=True)]:
            original=copy.deepcopy(self.receipt);mutate(self.receipt);self.save()
            with self.assertRaises(ValueError):fg.load(self.root)
            self.receipt=original

    def test_fractional_wrong_size_and_overlap_masks_rejected(self):
        path=self.root/'masks/candidate-1.png'
        for mode,size,value in [('L',(8,6),127),('L',(4,6),255),('RGB',(8,6),(255,255,255))]:
            Image.new(mode,size,value).save(path)
            self.receipt['candidates'][0]['mask']=fg._asset(self.root,'masks/candidate-1.png');self.save()
            with self.assertRaises(ValueError):fg.load(self.root)

    def test_false_overlap_rejected(self):
        Image.new('L',(8,6)).save(self.root/'masks/overlap.png')
        self.receipt['overlap']=fg._asset(self.root,'masks/overlap.png');self.save()
        with self.assertRaises(ValueError):fg.load(self.root)

    def test_mask_outside_original_alpha_rejected(self):
        image=Image.new('RGBA',(8,6));image.save(self.root/'source/original.png')
        self.receipt['source']['asset']=fg._asset(self.root,'source/original.png');self.save()
        with self.assertRaises(ValueError):fg.load(self.root)

    def test_duplicate_json_keys_and_path_traversal_rejected(self):
        self.receipt['overlay']['path']='../overlay.png';self.save()
        with self.assertRaises(ValueError):fg.load(self.root)
        (self.root/'foreground-proposals.json').write_text('{"kind":1,"kind":2}')
        with self.assertRaises(ValueError):fg.load(self.root)

    def test_no_implicit_inference_or_model_download(self):
        with self.assertRaises(ValueError):
            fg.propose(self.root/'source/original.png',Path(self.temp.name)/'new',weights='absent',origin='Test')
        self.assertFalse(fg.capabilities()['available'])
        with self.assertRaises(ValueError):fg._weight(self.root/'source/original.png')

    def test_invalid_options_fail_before_any_model_call(self):
        for kwargs in [{'score_threshold':float('nan')},{'score_threshold':True},
                       {'score_threshold':1},{'max_seconds':False},{'fixture':1}]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                fg.propose('absent','unused',weights='absent',origin='Test',enabled=True,**kwargs)

if __name__=='__main__':unittest.main()
