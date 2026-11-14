"""Actual worker lifecycle through the canonical owned-directory runner."""
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import physics_backends as core


@unittest.skipUnless(os.name == 'posix', 'POSIX physics descriptor runner')
class DirectoryRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name); self.root = self.parent / 'stage'; self.root.mkdir()
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY); self.addCleanup(os.close, self.fd)

    def run_worker(self, code, **changes):
        options = dict(timeout=5, memory_bytes=1024**3, output_bytes=16*1024**2)
        options.update(changes)
        return core._run_process_at([sys.executable, '-I', '-c', code], self.fd, **options)

    def test_pinned_worker_and_logs_stay_in_owned_directory(self):
        (self.root / 'input').write_bytes(b'actual source')
        self.root.rename(self.parent / 'owned'); self.root.mkdir()
        (self.root / 'input').write_bytes(b'foreign replacement')
        result = self.run_worker("from pathlib import Path; Path('result').write_bytes(Path('input').read_bytes()); print('actual worker')")
        self.assertEqual(result['exit_code'], 0)
        owned = self.parent / 'owned'
        self.assertEqual((owned / 'result').read_bytes(), b'actual source')
        self.assertIn('actual worker', (owned / 'simulation-stdout.log').read_text())
        self.assertEqual(list(self.root.iterdir()), [self.root / 'input'])

    def test_quick_worker_output_is_measured_after_exit(self):
        with self.assertRaises(core.BackendError) as raised:
            self.run_worker("open('output','wb').write(b'x'*200000)", output_bytes=65536)
        self.assertEqual(raised.exception.report['code'], 'PHYSICS_RESOURCE_LIMIT')
        self.assertEqual((self.root / 'output').stat().st_size, 200000)
        self.assertTrue(os.fstat(self.fd))

    def test_timeout_reaps_owned_worker_and_retains_descriptor_logs(self):
        with self.assertRaises(core.BackendError) as raised:
            self.run_worker('import os,time;print(os.getpid(),flush=True);time.sleep(30)', timeout=.25)
        self.assertEqual(raised.exception.report['code'], 'PHYSICS_TIMEOUT')
        pid = int((self.root / 'simulation-stdout.log').read_text().strip())
        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)
        self.assertEqual(raised.exception.report['preserved_workspace'], 'owned-directory-descriptor')
        self.assertIn(str(pid), raised.exception.report['stdout_tail'])

    def test_invalid_label_rejects_before_any_log_or_process(self):
        with self.assertRaises(core.BackendError) as raised:
            self.run_worker("open('must-not-exist','w').write('invalid')", label='../outside')
        self.assertEqual(raised.exception.report['code'], 'PHYSICS_PATH')
        self.assertFalse(list(self.root.iterdir()))

    def test_global_slot_is_shared_by_processes_with_different_tmpdir(self):
        alternate=self.parent/'alternate-tmp';alternate.mkdir()
        marker=self.parent/'child-owned'
        code=('import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);'
            'import physics_backends as core\n'
            'try:\n'
            ' with core._slot():Path(sys.argv[2]).write_bytes(b"actual owner")\n'
            'except core.BackendError as exc:\n'
            ' print(exc.report["code"]);sys.exit(4 if exc.report["code"]=="PHYSICS_BUSY" else 5)\n')
        command=[sys.executable,'-I','-c',code,str(Path(core.__file__).parent),str(marker)]
        env={**os.environ,'TMPDIR':str(alternate),'TEMP':str(alternate),'TMP':str(alternate)}
        self.assertNotEqual(Path(tempfile.gettempdir()).resolve(),alternate.resolve())
        with core._slot():
            blocked=subprocess.run(command,env=env,capture_output=True,timeout=30)
            self.assertEqual(blocked.returncode,4,(blocked.stdout,blocked.stderr))
            self.assertIn(b'PHYSICS_BUSY',blocked.stdout)
            self.assertFalse(marker.exists())
        owned=subprocess.run(command,env=env,capture_output=True,timeout=30)
        self.assertEqual(owned.returncode,0,(owned.stdout,owned.stderr))
        self.assertEqual(marker.read_bytes(),b'actual owner')
