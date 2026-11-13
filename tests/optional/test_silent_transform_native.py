"""Declared raster/FPS conversions; synthetic pixels are engineering fixtures."""
from copy import deepcopy
from fractions import Fraction
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import silent_transform as transform
import silent_video as video


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_silent_transform as core


class TransformNativeTests(core._TransformFixture):
    def test_exact_nearest_duplicate_map_and_full_pixel_reverification(self):
        profile = transform.profile(64,48,{'numerator':48,'denominator':1},fps_policy='nearest')
        result = transform.transform(self.source, self.root, profile, self.output)
        self.assertEqual([item['source_index'] for item in result['mapping']], [0,1,1,2,2,3,3,3])
        self.assertEqual(result['duration_delta'], {'numerator':0,'denominator':1})
        self.assertEqual(transform.verify(self.output,source_root=self.root),result)
        self.assertEqual((self.output/'frames/000000.png').read_bytes(),(self.root/self.paths[0]).read_bytes())

    def test_nearest_duration_quantization_is_recorded_as_an_exact_fraction(self):
        self.source['fps']={'numerator':30,'denominator':1}
        profile=transform.profile(64,48,{'numerator':24,'denominator':1},fps_policy='nearest')
        result=transform.transform(self.source,self.root,profile,self.output)
        self.assertEqual(len(result['mapping']),3)
        self.assertEqual(result['duration_delta'],{'numerator':-1,'denominator':120})

    def test_letterbox_fill_and_opt_in_stretch_have_declared_geometry(self):
        for policy in ('letterbox','fill','stretch'):
            with self.subTest(policy=policy):
                settings=transform.profile(80,80,self.source['fps'],fit=policy)
                output=self.root/policy
                result=transform.transform(self.source,self.root,settings,output)
                transform.verify(output,source_root=self.root)
                with Image.open(output/'frames/000000.png') as image:
                    self.assertEqual(image.size,(80,80))
                    self.assertEqual(image.getpixel((40,40)),(30,120,80))
                    self.assertEqual(image.getpixel((40,0)),(0,0,0) if policy=='letterbox' else (30,120,80))
                self.assertEqual(result['source']['width'],80)

    def test_tampering_and_cancellation_never_become_verified(self):
        settings=transform.profile(64,48,self.source['fps'])
        with self.assertRaisesRegex(ValueError,'cancelled'):
            transform.transform(self.source,self.root,settings,self.output,cancel=lambda:True)
        self.assertFalse(self.output.exists())
        transform.transform(self.source,self.root,settings,self.output)
        path=self.output/'transform.json';original=json.loads(path.read_text())
        bad=deepcopy(original);bad['mapping'][0]['source_index']=1
        path.write_text(json.dumps(bad))
        with self.assertRaises(ValueError):transform.verify(self.output)
        path.write_text(json.dumps(original))
        Image.new('RGB',(64,48),(240,0,0)).save(self.output/'frames/000000.png')
        with self.assertRaises(ValueError):transform.verify(self.output)

    def test_private_frame_reads_keep_descriptor_spelling_during_namespace_swap(self):
        original_open = os.open
        replacements = []
        with video._pin_directory(self.root) as parent_fd:
            with video._private_stage(parent_fd, prefix='.source-read-') as (private, descriptor):
                payload = private / 'payload'; payload.mkdir()
                Image.new('RGB',(2,2),(40,80,120)).save(payload/'frame.png')
                expected = (payload/'frame.png').read_bytes()

                original_path = video._path
                def replace_public_name(root_path, relative):
                    selected = original_path(root_path, relative)
                    if not replacements:
                        wrapper = Path(os.readlink('/proc/self/fd/' + str(descriptor)))
                        wrapper.rename(wrapper.with_name(wrapper.name + '-retained'))
                        replacement = wrapper/'payload'; replacement.mkdir(parents=True)
                        Image.new('RGB',(2,2),(240,0,0)).save(replacement/'frame.png')
                        replacements.append(replacement)
                    return selected

                with patch.object(video, '_path', side_effect=replace_public_name):
                    self.assertEqual(video._read_frame(payload,'frame.png'),expected)
        self.assertEqual(Image.open(replacements[0]/'frame.png').getpixel((0,0)),(240,0,0))

    def test_descriptor_destination_and_registered_stage_survive_namespace_replacement(self):
        settings = transform.profile(64, 48, self.source['fps'])
        descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        original_path = video._path
        replacements = []
        def replace_stage(root, relative):
            selected = original_path(root, relative)
            if not replacements and str(root).startswith('/proc/self/fd/'):
                stage_fd = int(Path(root).parts[4])
                wrapper = Path(os.readlink('/proc/self/fd/' + str(stage_fd)))
                wrapper.rename(wrapper.with_name(wrapper.name + '-retained'))
                foreign = wrapper/'payload'; (foreign/'frames').mkdir(parents=True)
                Image.new('RGB', (64, 48), (240, 0, 0)).save(foreign/'frames/000000.png')
                (foreign/'sentinel.txt').write_bytes(b'foreign original bytes')
                replacements.append(foreign)
            return selected
        try:
            with patch.object(video, '_path', side_effect=replace_stage):
                result = transform.transform(self.source, self.root, settings, 'converted',
                                             destination_dir_fd=descriptor)
            os.fstat(descriptor)
        finally:
            os.close(descriptor)
        self.assertEqual(result, transform.verify(self.output, source_root=self.root))
        self.assertEqual(len(replacements), 1)
        self.assertEqual((replacements[0]/'sentinel.txt').read_bytes(), b'foreign original bytes')
        self.assertEqual((self.output/'frames/000000.png').read_bytes(), (self.root/self.paths[0]).read_bytes())

if __name__ == '__main__': unittest.main()
