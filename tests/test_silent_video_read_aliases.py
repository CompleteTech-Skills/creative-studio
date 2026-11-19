"""Simulated read refusals through actual caller aliases; no native media claim."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

import test_silent_video_read_privacy as privacy


class AliasedCallerReadPrivacy(privacy.PortableReadPrivacy):
    def setUp(self):
        super().setUp()
        self.physical_root = self.root.resolve()
        temporary = tempfile.TemporaryDirectory(prefix='silent-caller-alias-')
        self.addCleanup(temporary.cleanup)
        alias = Path(temporary.name).resolve() / 'caller-root'
        if os.name == 'nt':
            result = subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(alias),
                                     str(self.physical_root)], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(alias.is_junction())
        else:
            alias.symlink_to(self.physical_root, target_is_directory=True)
            self.assertTrue(alias.is_symlink())
        self.assertNotEqual(alias, self.physical_root)
        self.assertEqual(alias.resolve(), self.physical_root)
        self.root = alias
        self.artifact = alias / self.artifact.name
        self.request_path = alias / self.request_path.name

    def assert_private(self, *values):
        super().assert_private(*values)
        public = '\n'.join(value if isinstance(value, str) else
                           json.dumps(value, default=str) for value in values)
        self.assertNotIn(str(self.physical_root), public)
