"""Actual subprocess cwd/descriptor lifetime tests; no optional solver."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from physics_directory_transport import launch_at


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'fchdir'), 'POSIX physics directory transport')
class DirectoryTransportTests(unittest.TestCase):
    def test_owned_namespace_survives_rename_and_relative_child_script(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); stage = root / 'stage'; stage.mkdir()
            (stage / 'worker.py').write_text("from pathlib import Path\nPath('result').write_bytes(Path('input').read_bytes())\n")
            (stage / 'input').write_bytes(b'actual owned source')
            fd = os.open(stage, os.O_RDONLY | os.O_DIRECTORY)
            try:
                stage.rename(root / 'preserved'); stage.mkdir(); (stage / 'input').write_bytes(b'foreign replacement')
                process = launch_at(fd, [sys.executable, '-I', 'worker.py'], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
                out, err = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, (out, err))
                self.assertEqual((root / 'preserved' / 'result').read_bytes(), b'actual owned source')
                self.assertFalse((stage / 'result').exists())
                self.assertTrue(os.fstat(fd))
            finally: os.close(fd)

    def test_child_retains_directory_after_parent_closes_caller_fd(self):
        with tempfile.TemporaryDirectory() as folder:
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            code = "import os; n=int(os.environ['HATCH_PHYSICS_DIRECTORY_FD']); os.fstat(n); open('result','wb').write(b'owned')"
            process = launch_at(fd, [sys.executable, '-I', '-c', code], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
            os.close(fd)
            out, err = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, (out, err))
            self.assertEqual((Path(folder) / 'result').read_bytes(), b'owned')

    def test_invalid_and_nondirectory_descriptors_do_not_spawn(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'file'; target.write_bytes(b'')
            fd = os.open(target, os.O_RDONLY)
            try:
                for value in (fd, True, -1):
                    with self.subTest(value=value), self.assertRaises((ValueError, OSError)):
                        launch_at(value, [sys.executable, '-c', 'pass'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={})
                self.assertTrue(os.fstat(fd))
            finally: os.close(fd)
            with self.assertRaises(OSError):
                launch_at(fd, [sys.executable, '-c', 'pass'], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={})

    def test_invalid_commands_fail_before_acquiring_directory(self):
        for command in ([], [''], ['python', '\0'], 'python'):
            with self.subTest(command=command), self.assertRaises(ValueError):
                launch_at(-1, command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={})

    def test_exec_failure_is_reported_by_owned_child(self):
        with tempfile.TemporaryDirectory() as folder:
            fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                process = launch_at(fd, ['/nonexistent/physics-engine'], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ.copy())
                _, err = process.communicate(timeout=30)
                self.assertNotEqual(process.returncode, 0)
                self.assertIn(b'FileNotFoundError', err)
                self.assertTrue(os.fstat(fd))
            finally: os.close(fd)
