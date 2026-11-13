"""Portable job contracts only; Linux/media gates are explicit optional suites."""
import json
import io
from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import silent_jobs as jobs


class PortableJobContracts(unittest.TestCase):
    def test_schemas_are_strict_and_checkpoints_have_explicit_terminal_states(self):
        schemas=jobs.schemas()
        for schema in schemas.values():
            self.assertFalse(schema['additionalProperties'])
        self.assertIn('interrupted',schemas['state']['properties']['phase']['enum'])
        self.assertIn('stale',schemas['state']['properties']['phase']['enum'])
        self.assertEqual(schemas['plan']['properties']['request']['properties']['audio']['properties']['policy']['const'],'forbidden')

    def test_unsupported_staging_fails_before_tool_discovery_or_job_creation(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            with mock.patch.object(jobs,'publication_supported',return_value=False), \
                    mock.patch.object(jobs,'_tools',side_effect=AssertionError('tools must not execute')):
                self.assertEqual(jobs.worker_capability()['status'],'unavailable')
                with self.assertRaisesRegex(ValueError,'unavailable'):
                    jobs.create({},'.',root/'job',root/'delivery')
            self.assertEqual(list(root.iterdir()),[])

    def test_missing_scoped_boundary_is_explicitly_blocked(self):
        with mock.patch.object(jobs,'publication_supported',return_value=True), \
                mock.patch.object(jobs,'_worker_boundary',side_effect=ValueError('canonical FD boundary absent')):
            report=jobs.worker_capability()
            self.assertEqual(report['status'],'blocked');self.assertIn('canonical',report['reason'])

    def test_hidden_worker_never_signals_an_unowned_session(self):
        result=subprocess.run([sys.executable,str(Path(jobs.__file__)),'_worker'],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,2);self.assertIn('owned session',result.stderr)

    def test_cli_streams_observed_progress_separately_from_result(self):
        checkpoint={'job':'a'*64,'phase':'encoding','progress':{'frame':3,'speed':'1.2x'}}
        def execute(root,*,progress):
            progress(checkpoint)
            return {'state':{'phase':'completed'}}
        output=io.StringIO();events=io.StringIO()
        with mock.patch.object(jobs,'run',side_effect=execute),redirect_stdout(output),redirect_stderr(events):
            self.assertEqual(jobs.main(['run','--job-root','job']),0)
        self.assertEqual(json.loads(output.getvalue())['state']['phase'],'completed')
        event=json.loads(events.getvalue())
        self.assertEqual(event['job'],checkpoint['job'])
        self.assertEqual(event['progress'],checkpoint['progress'])


if __name__=='__main__':unittest.main()
