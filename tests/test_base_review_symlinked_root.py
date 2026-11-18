"""Base-review currency under a symlinked project ancestor.

macOS temporary directories live below the /var -> /private/var symlink. The
project root is trusted and resolved like reconcile/inputs; descendants remain
no-follow reads, so a substituted leaf symlink must still be rejected.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import hatch


@unittest.skipIf(os.name == 'nt', 'POSIX directory symlinks are required')
class BaseReviewSymlinkedRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.real = base / 'real' / 'project'
        (self.real / 'references').mkdir(parents=True)
        self.png = b'\x89PNG\r\n\x1a\nfixture'
        self.settings = b'{"background": "auto"}'
        (self.real / 'references/canonical-base.png').write_bytes(self.png)
        (self.real / 'extraction-settings.json').write_bytes(self.settings)
        (base / 'alias').symlink_to(base / 'real', target_is_directory=True)
        self.aliased = base / 'alias' / 'project'
        self.data = {'base_review': {'canonical_hash': hashlib.sha256(self.png).hexdigest(),
                                     'settings_hash': hashlib.sha256(self.settings).hexdigest()}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_symlinked_ancestor_is_current_like_resolved_root(self):
        self.assertTrue(hatch.base_review_current(self.real, self.data))
        self.assertTrue(hatch.base_review_current(self.aliased, self.data))
        self.assertTrue(hatch.base_review_current(str(self.aliased), self.data))

    def test_changed_bytes_are_stale_through_symlinked_ancestor(self):
        (self.real / 'extraction-settings.json').write_bytes(b'{"background": "chroma"}')
        self.assertFalse(hatch.base_review_current(self.aliased, self.data))

    def test_symlinked_leaf_is_still_rejected(self):
        outside = Path(self.tmp.name).resolve() / 'outside.png'
        outside.write_bytes(self.png)
        leaf = self.real / 'references/canonical-base.png'
        leaf.unlink()
        leaf.symlink_to(outside)
        self.assertFalse(hatch.base_review_current(self.real, self.data))
        self.assertFalse(hatch.base_review_current(self.aliased, self.data))


if __name__ == '__main__':
    unittest.main()
