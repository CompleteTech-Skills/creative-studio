"""Read-only resolution compatibility check on a POSIX caller path."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import native_journal

class OrdinaryPath(unittest.TestCase):
    def test_parent_after_preexisting_alias_uses_its_filesystem_meaning(self):
        with TemporaryDirectory() as directory:
            top = Path(directory).resolve()
            intended = top / 'physical' / 'run'
            lexical = top / 'run'
            target = top / 'physical' / 'nested'
            for location in (intended, lexical, target):
                location.mkdir(parents=True)
            (top / 'alias').symlink_to(target, target_is_directory=True)
            caller = top / 'alias' / '..' / 'run'
            self.assertTrue(caller.samefile(intended))
            self.assertFalse(caller.samefile(lexical))
            with native_journal.admission(caller) as admitted:
                self.assertTrue(admitted.samefile(caller),
                                'Operation admission changed the directory designated by a valid caller path')

if __name__ == '__main__':
    unittest.main(verbosity=2)
