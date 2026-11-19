"""Actual process termination and simulated exit/kill race evidence contracts."""
import os
import sys
import unittest
from unittest.mock import Mock, patch

import test_silent_video as core

video = core.video


class ProcessTerminationEvidence(unittest.TestCase):
    def test_actual_running_over_limit_process_reports_requested_termination(self):
        command = [sys.executable, '-c',
                   'import sys,time; sys.stderr.write("x"*8192); sys.stderr.flush(); time.sleep(10)']
        limits = dict(video.DEFAULT_LIMITS, max_log_bytes=1024, timeout_seconds=5)
        with self.assertRaisesRegex(ValueError, 'evidence byte budget') as failed:
            video._run(command, limits)
        observed = failed.exception.evidence['command_failure']
        self.assertIs(observed['termination_requested'], True)
        self.assertIs(observed['evidence_limit_exceeded'], True)
        self.assertEqual(observed['exit_status'], -9 if os.name == 'posix' else 1)
        self.assertIs(observed['stderr_truncated'], True)
        self.assertEqual(observed['stderr'], 'x'*1024)

    def test_simulated_natural_exit_before_or_during_kill_keeps_observed_status(self):
        # These are deterministic process-record fixtures, not native codec
        # evidence. A kill request cannot establish how the process exited.
        for running_at_poll in (False, True):
            process = Mock(returncode=1)
            process.poll.return_value = None if running_at_poll else 1
            process.wait.return_value = 1

            def launch(command, **options):
                options['stderr'].write(b'x'*8192)
                options['stderr'].flush()
                return process

            with self.subTest(running_at_poll=running_at_poll), \
                    patch.object(video.subprocess, 'Popen', side_effect=launch), \
                    self.assertRaisesRegex(ValueError, 'evidence byte budget') as failed:
                video._run(['fixture-tool'], dict(video.DEFAULT_LIMITS, max_log_bytes=1024))
            observed = failed.exception.evidence['command_failure']
            self.assertIs(observed['termination_requested'], running_at_poll)
            self.assertIs(observed['evidence_limit_exceeded'], True)
            self.assertEqual(observed['exit_status'], 1)
            self.assertEqual(process.kill.call_count, int(running_at_poll))
            self.assertEqual(observed['stderr'], 'x'*1024)
            self.assertIs(observed['stderr_truncated'], True)


if __name__ == '__main__':
    unittest.main()
