"""Re-publish genuine native results through the owned directory transport."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import atomic_publish
import physics_backends as core


@unittest.skipUnless(os.environ.get('HATCH_PHYSICS_PUBLICATION_SOURCE'), 'actual native publication source required')
class NativePublicationTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(os.environ['HATCH_PHYSICS_PUBLICATION_SOURCE']).resolve()
        # These are retained execution artifacts, not newly simulated states.
        manifest, world, _, _ = core._read_cache(self.source)
        self.backend = core.BlenderBackend()
        self.result = core.SimulationResult('retained-publication-fixture', self.source,
            manifest['world_sha256'], manifest['backend'], tuple(manifest['files']), manifest['usage'], {}, manifest['fixture'])
        info = self.source.stat()
        self.backend._owned[self.result.token] = dict(root=self.source, inode=(info.st_dev, info.st_ino), world=world,
            inventory={str(i): dict(sha256=value) for i, value in enumerate(manifest['upstream_hashes'])})
        self.backend._results[self.result.token] = self.result

    def test_stage_namespace_change_preserves_verified_payload_and_foreign_wrapper(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder); destination = parent / 'published'
            original = atomic_publish.rename_no_replace_at
            changed = []
            def rename(source_fd, source_name, destination_fd, destination_name):
                owned = os.fstat(source_fd)
                stage = next(p for p in parent.iterdir() if p.stat().st_ino == owned.st_ino and p.stat().st_dev == owned.st_dev)
                stage.rename(parent / 'owned-moved')
                stage.mkdir(); (stage / 'foreign-marker').write_bytes(b'preserve replacement')
                changed.append(stage)
                return original(source_fd, source_name, destination_fd, destination_name)
            with patch.object(atomic_publish, 'rename_no_replace_at', side_effect=rename):
                report = self.backend.bake(self.result, destination)
            self.assertEqual(report['status'], 'baked-and-verified')
            self.assertEqual(core.verify_cache(destination)['status'], 'hash-and-contract-verified')
            self.assertEqual(report['cache_sha256'], core.digest((destination / 'cache.json').read_bytes()))
            self.assertEqual(len(changed), 1)
            self.assertEqual((changed[0] / 'foreign-marker').read_bytes(), b'preserve replacement')
            self.assertFalse((destination / 'foreign-marker').exists())

    def test_racing_destination_is_preserved_without_publishing_partial_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder); destination = parent / 'published'
            original = atomic_publish.rename_no_replace_at
            def rename(source_fd, source_name, destination_fd, destination_name):
                destination.mkdir(); (destination / 'foreign-marker').write_bytes(b'existing destination')
                return original(source_fd, source_name, destination_fd, destination_name)
            with patch.object(atomic_publish, 'rename_no_replace_at', side_effect=rename), self.assertRaises(core.BackendError) as raised:
                self.backend.bake(self.result, destination)
            self.assertEqual(raised.exception.report['code'], 'PHYSICS_DESTINATION_EXISTS')
            self.assertEqual(list(destination.iterdir()), [destination / 'foreign-marker'])
            self.assertEqual((destination / 'foreign-marker').read_bytes(), b'existing destination')
