"""Real stdlib process-tree regressions, also discovered by Windows Delivery."""
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]

# Every synthetic process also has a deadline and a cooperative stop file.
# The grandchild inherits the worker's group/job exactly as a native tool would.
FIXTURE = r"""
import os
from pathlib import Path
import subprocess
import sys
import time

root, mode, role = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
deadline = time.monotonic() + 20
child = None

def stopped():
    return (root / 'stop').exists() or time.monotonic() >= deadline

def ready():
    temporary = root / (role + '.tmp')
    temporary.write_text(str(os.getpid()), encoding='ascii')
    temporary.replace(root / (role + '.pid'))

def idle():
    if (root / 'after-return').exists():
        (root / ('late-' + role)).write_text('still running', encoding='ascii')
    time.sleep(.01)

try:
    ready()
    if role == 'supervisor':
        # A Windows venv redirector adds a process, so Popen.pid would name
        # the launcher rather than the grandchild that records os.getpid().
        # This stdlib-only fixture can use the base interpreter directly.
        executable = sys._base_executable if os.name == 'nt' else sys.executable
        child = subprocess.Popen(
            [executable, __file__, str(root), mode, 'grandchild'],
            stdin=subprocess.DEVNULL)
        while not (root / 'grandchild.pid').exists() and not stopped():
            time.sleep(.01)
        while not (root / 'go').exists() and not stopped():
            time.sleep(.01)
        if mode == 'success' and not stopped():
            (root / 'finish').touch()
            child.wait(timeout=3)
            print('TREE_FINISHED', flush=True)
            sys.exit(0)
        if mode == 'success-with-descendant' and not stopped():
            if child.poll() is not None:
                raise RuntimeError('Synthetic descendant exited before supervisor')
            (root / 'live-descendant-at-exit').write_text(str(child.pid), encoding='ascii')
            print('TREE_PARENT_FINISHED', flush=True)
            sys.exit(0)
        if mode == 'output-limit' and not stopped():
            # Cross the 1 MiB limit without approaching the 16 MiB POSIX cap.
            for _ in range(32):
                sys.stdout.write('x' * 65536)
                sys.stdout.flush()
    while not stopped() and not (root / 'finish').exists():
        idle()
finally:
    if child is not None and mode != 'success-with-descendant':
        (root / 'stop').touch()
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=3)
"""

HARNESS = r"""
import json
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
import director_capabilities as cap

root, mode = Path(sys.argv[2]), sys.argv[3]
result, text = cap._worker(
    [sys.executable, root / 'tree.py', root, mode, 'supervisor'],
    root, 5 if mode == 'timeout' else 10)
(root / 'result.json').write_text(json.dumps([result, text]), encoding='utf-8')
"""


def wait_until(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return bool(predicate())


class OwnedProcess:
    """Pin Windows identity before termination; queries never send a signal."""
    def __init__(self, pid):
        self.pid = pid
        self.handle = None
        if os.name == 'nt':
            from ctypes import wintypes
            self.api = ctypes.WinDLL('kernel32', use_last_error=True)
            self.api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.api.OpenProcess.restype = wintypes.HANDLE
            self.api.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                                    ctypes.POINTER(wintypes.DWORD)]
            self.api.GetExitCodeProcess.restype = wintypes.BOOL
            self.api.CloseHandle.argtypes = [wintypes.HANDLE]
            self.api.CloseHandle.restype = wintypes.BOOL
            self.handle = self.api.OpenProcess(0x1000, False, pid)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())

    def alive(self):
        if os.name == 'nt':
            from ctypes import wintypes
            code = wintypes.DWORD()
            if not self.api.GetExitCodeProcess(self.handle, ctypes.byref(code)):
                raise ctypes.WinError(ctypes.get_last_error())
            return code.value == 259  # STILL_ACTIVE
        # A killed orphan can remain a zombie until the OS reaps it.
        result = subprocess.run(['ps', '-o', 'stat=', '-p', str(self.pid)],
                                capture_output=True, text=True, timeout=2)
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise RuntimeError('Could not inspect synthetic process: ' + result.stderr)
        state = result.stdout.strip()
        return bool(state) and not state.startswith('Z')

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None


class DirectorWorkerTreeTests(unittest.TestCase):
    def run_tree(self, mode, *, venv_launcher=False):
        with tempfile.TemporaryDirectory(prefix='director-worker-tree-') as directory:
            root = Path(directory).resolve()
            (root / 'tree.py').write_text(FIXTURE, encoding='utf-8')
            (root / 'harness.py').write_text(HARNESS, encoding='utf-8')
            harness_python = sys.executable
            if venv_launcher:
                import venv
                venv.EnvBuilder(with_pip=False, system_site_packages=True).create(root/'venv')
                harness_python = root/'venv'/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            # An independent process using the same Python executable must survive.
            sibling = subprocess.Popen(
                [sys.executable, '-c', 'import time; time.sleep(40)'],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            harness = None
            owned = []
            try:
                harness = subprocess.Popen(
                    [harness_python, root / 'harness.py', ROOT / 'scripts', root, mode],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE)
                self.assertTrue(wait_until(
                    lambda: all((root / (role + '.pid')).exists()
                                for role in ('supervisor', 'grandchild')), 4),
                    'Both processes must be ready before exercising the limit')
                for role in ('supervisor', 'grandchild'):
                    owned.append(OwnedProcess(int((root / (role + '.pid')).read_text())))
                self.assertTrue(all(process.alive() for process in owned))
                self.assertIsNone(harness.poll(), 'Worker returned before readiness')
                # Output cannot flood before both identities have been recorded.
                (root / 'go').touch()
                _, errors = harness.communicate(timeout=16)
                self.assertEqual(harness.returncode, 0, errors.decode('utf-8', 'replace'))
                result, text = json.loads((root / 'result.json').read_text())
                stopped = wait_until(lambda: all(not p.alive() for p in owned), 3)
                # Give a surviving descendant an explicit chance to prove activity.
                (root / 'after-return').touch()
                wait_until(lambda: any(root.glob('late-*')), .5)
                late = [path.name for path in root.glob('late-*')]
                self.assertIsNone(sibling.poll(), 'Worker stopped an unrelated process')
                self.assertTrue(stopped, 'Worker left a supervisor or grandchild running')
                self.assertEqual(late, [], 'Descendant wrote after worker return')
                self.assertFalse((root / 'worker-output.txt').exists())
                if mode == 'success':
                    self.assertEqual(result, {'success': True, 'reason': None, 'exit_code': 0})
                    self.assertEqual(text.strip(), 'TREE_FINISHED')
                elif mode == 'success-with-descendant':
                    self.assertEqual(result, {'success': True, 'reason': None, 'exit_code': 0})
                    self.assertEqual(text.strip(), 'TREE_PARENT_FINISHED')
                    self.assertEqual(int((root / 'live-descendant-at-exit').read_text()),
                                     owned[1].pid)
                else:
                    self.assertFalse(result['success'])
                    self.assertEqual(result['reason'], mode)
                    self.assertNotEqual(result['exit_code'], 0)
                    self.assertEqual(text, '')
            finally:
                # Only this temporary tree observes stop; never signal a PID read
                # from a file, which could have been reused by an unrelated process.
                (root / 'stop').touch()
                if harness is not None:
                    try:
                        harness.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        harness.kill()
                        harness.communicate(timeout=3)
                cleanup_ok = wait_until(lambda: all(not p.alive() for p in owned), 4)
                for process in owned:
                    process.close()
                sibling.kill()
                sibling.wait(timeout=3)
                self.assertTrue(cleanup_ok, 'Synthetic descendant ignored cooperative cleanup')

    def test_timeout_stops_supervisor_and_grandchild(self):
        self.run_tree('timeout')

    def test_output_limit_stops_supervisor_and_grandchild(self):
        self.run_tree('output-limit')

    def test_success_finishes_tree_and_preserves_unrelated_process(self):
        self.run_tree('success')

    def test_success_stops_live_descendant_and_preserves_unrelated_process(self):
        self.run_tree('success-with-descendant')

    def test_venv_launcher_stops_live_descendant(self):
        self.run_tree('success-with-descendant', venv_launcher=True)

    def test_darwin_group_permission_with_only_zombies_is_complete(self):
        with patch.object(sys, 'path', [str(ROOT/'scripts'), *sys.path]):
            import director_capabilities as cap
        for stage, effects in (('signal', [PermissionError()]),
                               ('probe', [None, PermissionError()])):
            with self.subTest(stage=stage):
                process = SimpleNamespace(pid=42, wait=Mock(return_value=0))
                signal_group = Mock(side_effect=effects)
                inspect = Mock(return_value=SimpleNamespace(
                    returncode=0, stderr=b'', stdout=b' 42 Z\n 77 R\n'))
                with patch.object(cap, 'sys', SimpleNamespace(platform='darwin')), \
                        patch.object(cap, 'os', SimpleNamespace(killpg=signal_group)), \
                        patch.object(cap, 'signal', SimpleNamespace(SIGKILL=9)), \
                        patch.object(cap, 'subprocess', SimpleNamespace(run=inspect)):
                    cap._stop_group(process)
                expected = [call(42, 9)]
                if stage == 'probe':
                    expected.append(call(42, 0))
                self.assertEqual(signal_group.call_args_list, expected)
                process.wait.assert_called_once_with(timeout=5)
                inspect.assert_called_once()

    def test_darwin_group_permission_with_live_member_stays_failed(self):
        with patch.object(sys, 'path', [str(ROOT/'scripts'), *sys.path]):
            import director_capabilities as cap
        process = SimpleNamespace(pid=42, wait=Mock())
        signal_group = Mock(side_effect=PermissionError())
        inspect = Mock(return_value=SimpleNamespace(
            returncode=0, stderr=b'', stdout=b' 42 Z\n 42 S\n 77 R\n'))
        with patch.object(cap, 'sys', SimpleNamespace(platform='darwin')), \
                patch.object(cap, 'os', SimpleNamespace(killpg=signal_group)), \
                patch.object(cap, 'signal', SimpleNamespace(SIGKILL=9)), \
                patch.object(cap, 'subprocess', SimpleNamespace(run=inspect)):
            with self.assertRaises(PermissionError):
                cap._stop_group(process)
        signal_group.assert_called_once_with(42, 9)
        inspect.assert_called_once()
        process.wait.assert_not_called()

    def test_kqueue_observes_exit_and_closes_without_reaping(self):
        with patch.object(sys, 'path', [str(ROOT/'scripts'), *sys.path]):
            import director_capabilities as cap
        process = SimpleNamespace(pid=42)
        for exited_before_registration in (False, True):
            with self.subTest(exited_before_registration=exited_before_registration):
                # Model the Python 3.12 kqueue API: close(), no context manager.
                queue = Mock(spec=['close', 'control'])
                event = object()
                selector = SimpleNamespace(kqueue=Mock(return_value=queue), kevent=Mock(return_value=event),
                                           KQ_FILTER_PROC=1, KQ_EV_ADD=2, KQ_NOTE_EXIT=3)
                queue.control.side_effect = (ProcessLookupError() if exited_before_registration else
                                             [[], [], [event]])
                with patch.object(cap, 'os', SimpleNamespace()), patch.dict(sys.modules, select=selector):
                    with cap._posix_completion(process) as finished:
                        if not exited_before_registration:
                            self.assertFalse(finished())
                        self.assertTrue(finished())
                        queue.close.assert_not_called()
                selector.kevent.assert_called_once_with(process.pid, filter=1, flags=2, fflags=3)
                expected = [call([event], 0)]
                if not exited_before_registration:
                    expected.extend([call(None, 1, 0)]*2)
                self.assertEqual(queue.control.call_args_list, expected)
                queue.close.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
