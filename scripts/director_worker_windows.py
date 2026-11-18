"""Windows containment for fixed Director workers; standard library only.

The isolated launcher executes no target code until its parent assigns it to a
non-breakaway, kill-on-close Job Object and releases its private stdin gate.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import subprocess
import sys
import time


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ('PerProcessUserTimeLimit', ctypes.c_longlong),
        ('PerJobUserTimeLimit', ctypes.c_longlong),
        ('LimitFlags', wintypes.DWORD),
        ('MinimumWorkingSetSize', ctypes.c_size_t),
        ('MaximumWorkingSetSize', ctypes.c_size_t),
        ('ActiveProcessLimit', wintypes.DWORD),
        ('Affinity', ctypes.c_size_t),
        ('PriorityClass', wintypes.DWORD),
        ('SchedulingClass', wintypes.DWORD),
    ]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ('BasicLimitInformation', _BasicLimits),
        ('IoInfo', _IOCounters),
        ('ProcessMemoryLimit', ctypes.c_size_t),
        ('JobMemoryLimit', ctypes.c_size_t),
        ('PeakProcessMemoryUsed', ctypes.c_size_t),
        ('PeakJobMemoryUsed', ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ('TotalUserTime', ctypes.c_longlong),
        ('TotalKernelTime', ctypes.c_longlong),
        ('ThisPeriodTotalUserTime', ctypes.c_longlong),
        ('ThisPeriodTotalKernelTime', ctypes.c_longlong),
        ('TotalPageFaultCount', wintypes.DWORD),
        ('TotalProcesses', wintypes.DWORD),
        ('ActiveProcesses', wintypes.DWORD),
        ('TotalTerminatedProcesses', wintypes.DWORD),
    ]


def _kernel32():
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    signatures = {
        'CreateJobObjectW': ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        'SetInformationJobObject': ([wintypes.HANDLE, ctypes.c_int,
                                     ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        'AssignProcessToJobObject': ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
    }
    for name, (args, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes = args
        function.restype = result
    return api


def _check(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


class WindowsJob:
    """One worker tree. Never inherit or name its controlling job handle."""

    def __init__(self):
        self.api = _kernel32()
        self.process = None
        self.assigned = False
        # NULL security attributes make this handle non-inheritable.
        self.handle = _check(self.api.CreateJobObjectW(None, None))
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        try:
            _check(self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        except BaseException:
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise

    def __enter__(self):
        return self

    def start(self, argv, directory, env, stream):
        # -I -S prevents site hooks, PYTHONPATH and the scratch directory from
        # executing code before assignment. The requested tool still gets env.
        self.process = subprocess.Popen(
            [sys.executable, '-I', '-S', str(Path(__file__).resolve()), *argv],
            cwd=directory, env=env, stdin=subprocess.PIPE, stdout=stream,
            stderr=subprocess.STDOUT, close_fds=True)
        # Popen owns a real process handle: no PID lookup/reuse race. CPython's
        # Windows Popen exposes that handle as _handle.
        _check(self.api.AssignProcessToJobObject(
            self.handle, int(self.process._handle)))
        self.assigned = True
        self.process.stdin.write(b'G')
        self.process.stdin.close()
        return self.process

    def close(self):
        if self.handle is None:
            return
        deadline = time.monotonic() + 5
        try:
            # An assignment failure can leave only the trusted gated launcher.
            if self.process is not None and not self.assigned:
                self.process.kill()
            _check(self.api.TerminateJobObject(self.handle, 1))
            while True:
                counts = _Accounting()
                _check(self.api.QueryInformationJobObject(
                    self.handle, 1, ctypes.byref(counts), ctypes.sizeof(counts), None))
                if not counts.ActiveProcesses:
                    break
                if time.monotonic() >= deadline:
                    raise OSError('Worker job termination did not complete.')
                time.sleep(.01)
        finally:
            # Also kills the tree if termination/query failed or the parent
            # exits abruptly. No other process has inherited this handle.
            handle, self.handle = self.handle, None
            try:
                _check(self.api.CloseHandle(handle))
            finally:
                if self.process is not None:
                    try:
                        if self.process.stdin is not None and not self.process.stdin.closed:
                            self.process.stdin.close()
                    except OSError:
                        pass
                    finally:
                        self.process.wait(timeout=max(.01, deadline-time.monotonic()))

    def __exit__(self, *_):
        self.close()


def _launch():
    if sys.stdin.buffer.read(1) != b'G' or len(sys.argv) < 2:
        return 125
    # The control pipe is never the tool's stdin. Output/cwd/environment and
    # the requested command's exit status retain their direct-worker meaning.
    sys.stdin.close()
    try:
        return subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL, close_fds=True)
    except OSError:
        return 127


if __name__ == '__main__':
    sys.exit(_launch())
