"""Read-only provenance observations during an ordinary Linux offline claim.

Use one normal local run, a real owner lock and the real journal publisher.
Do not move directories, substitute a destination or simulate provider calls.
"""
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import test_hatch_direct_aliases as fixture

hatch = fixture.hatch

class WriterProvenance(unittest.TestCase):
    setUp = fixture.HatchDirectAliasTests.setUp
    assert_alias = fixture.HatchDirectAliasTests.assert_alias

    def held_original(self):
        original = os.stat(self.root)
        for entry in Path('/proc/self/fd').iterdir():
            try:
                current = os.fstat(int(entry.name))
            except OSError:
                continue
            if os.path.samestat(current, original):
                return True
        return False

    def test_previously_admitted_writer_retains_original_directory_identity(self):
        before, inside = [], []
        save, save_at = hatch.save, hatch._save_at

        def observe_before_open(target, data):
            if hatch.job_for(data, 'base')['status'] == 'running':
                before.append((self.held_original(), tuple(vars(target).keys())))
            return save(target, data)

        def observe_after_open(parent, leaf, data):
            if hatch.job_for(data, 'base')['status'] == 'running':
                inside.append((self.held_original(),
                               os.path.samestat(os.fstat(parent), os.stat(self.root))))
            return save_at(parent, leaf, data)

        with hatch.ownership(self.root), patch.object(hatch, 'save', observe_before_open), \
                patch.object(hatch, '_save_at', observe_after_open):
            handoff = hatch.claim(self.root, self.data, 'base')
        self.assertEqual(hatch.job_for(hatch.manifest(self.root), 'base')['token'], handoff['token'])
        self.assertEqual(inside, [(True, True)])
        print('ordinary claim: original root FD and marker fields before save, FD and identity inside save:', before, inside)
        self.assertTrue(before[0][0] or set(before[0][1]) != {'path'},
                        'No original directory FD held; prior admission marker has only a pathname')

if __name__ == '__main__':
    unittest.main(verbosity=2)
