"""Linux-only explicit authority tests; invoked by the dedicated color CI job."""
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
import cinematics3d_color as color
from rig_errors import directory_capability

ROOT = Path(__file__).resolve().parents[2]


class CapabilityColorTests(unittest.TestCase):
    def test_registered_pinned_profile_survives_outer_namespace_replacement(self):
        with tempfile.TemporaryDirectory(prefix='color-cap-') as temporary:
            parent = Path(temporary).resolve()
            profile = parent/'profile'
            shutil.copytree(ROOT/'tests/fixtures/cinematics-color/profile', profile)
            inventory = color.capture_config(profile)
            descriptor = os.open(profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.addCleanup(os.close, descriptor)
            with directory_capability(descriptor) as capability:
                profile.rename(parent/'retained')
                shutil.copytree(ROOT/'tests/fixtures/cinematics-color/profile', profile)
                (profile/'luts/identity.spi1d').write_text('unverified replacement')
                self.assertEqual(color.capture_config(capability.path), inventory)
                # Registered borrowed aliases re-anchor to the owned live FD.
                borrowed = Path('/proc/self/fd')/str(descriptor)
                self.assertEqual(color.capture_config(borrowed), inventory)
                self.assertEqual(color.verify_config_bytes(borrowed, inventory), inventory['closure_sha256'])
                self.assertEqual(color.verify_config_bytes(capability.path, inventory), inventory['closure_sha256'])
                with color.snapshot_config(capability.path, inventory) as snapshot:
                    self.assertEqual(color.verify_config_bytes(snapshot, inventory), inventory['closure_sha256'])
                with self.assertRaises(ValueError):
                    color.verify_config_bytes(profile, inventory)
            with self.assertRaises(ValueError):
                color.verify_config_bytes(Path('/proc/self/fd')/str(descriptor), inventory)

    def test_registered_authority_does_not_admit_payload_symlinks(self):
        with tempfile.TemporaryDirectory(prefix='color-cap-') as temporary:
            parent = Path(temporary).resolve()
            profile = parent/'profile'
            shutil.copytree(ROOT/'tests/fixtures/cinematics-color/profile', profile)
            outside = parent/'outside.spi1d'
            shutil.copy2(profile/'luts/identity.spi1d', outside)
            (profile/'luts/identity.spi1d').unlink()
            (profile/'luts/identity.spi1d').symlink_to(outside)
            descriptor = os.open(profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.addCleanup(os.close, descriptor)
            with directory_capability(descriptor) as capability:
                with self.assertRaises(ValueError):
                    color.capture_config(capability.path)


if __name__ == '__main__':
    unittest.main()
