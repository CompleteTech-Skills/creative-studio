"""Actual directory ownership tests; synthetic pixels do not prove a 3D film."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import silent_video as video
import silent_timeline as timeline

if not video.secure_publication_supported():
    raise RuntimeError('descriptor timeline gate requires tested Linux publication support')


class DescriptorTimeline(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source_root = self.root / 'source'
        source_root.mkdir()
        for index in range(3):
            with Image.new('RGB', (8, 8), (index * 80, 20, 30)) as image:
                image.save(source_root / ('%d.png' % index))
        source = video.build_source(source_root, ['0.png', '1.png', '2.png'],
                                    {'numerator': 24, 'denominator': 1})
        self.shots = [{'id': 'shot', 'root': source_root, 'source': source, 'start': 0, 'end': 3}]

    def test_parent_namespace_replacement_preserves_original_authority_and_borrowed_fd(self):
        parent = self.root / 'parent'
        parent.mkdir()
        moved = self.root / 'moved'
        with video._pin_directory(parent) as descriptor:
            identity = os.fstat(descriptor)
            parent.rename(moved)
            parent.mkdir()
            (parent / 'foreign.txt').write_text('preserved')
            timeline.assemble(self.shots, 'timeline', destination_dir_fd=descriptor)
            self.assertEqual(os.fstat(descriptor).st_ino, identity.st_ino)
        self.assertEqual(len(timeline.validate(moved / 'timeline')['mapping']), 3)
        self.assertEqual([p.name for p in parent.iterdir()], ['foreign.txt'])

    def test_nested_owned_stage_uses_explicit_fd_and_registered_source_scope(self):
        with video._pin_directory(self.root) as parent_fd:
            with video._private_stage(parent_fd) as (stage, descriptor):
                timeline.assemble(self.shots, 'timeline', destination_dir_fd=descriptor)
                self.assertEqual(len(timeline.validate(stage / 'timeline')['mapping']), 3)

    def test_collision_and_unsafe_basename_cannot_replace_foreign_directory(self):
        parent = self.root / 'parent'
        parent.mkdir()
        (parent / 'existing').mkdir()
        (parent / 'existing' / 'foreign.txt').write_text('preserved')
        with video._pin_directory(parent) as descriptor:
            for name in ('../escape', '/absolute', 'nested/name'):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    timeline.assemble(self.shots, name, destination_dir_fd=descriptor)
            with self.assertRaises(FileExistsError):
                timeline.assemble(self.shots, 'existing', destination_dir_fd=descriptor)
        self.assertEqual((parent / 'existing' / 'foreign.txt').read_text(), 'preserved')
        self.assertFalse((self.root / 'escape').exists())


if __name__ == '__main__':
    unittest.main()
