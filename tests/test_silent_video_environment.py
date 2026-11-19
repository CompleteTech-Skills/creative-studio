"""Portable child-environment contract; Python subprocesses are not codec evidence."""
import json
import os
import sys
import unittest
from unittest.mock import patch

import test_silent_video as core

video = core.video


class MediaChildEnvironment(unittest.TestCase):
    def test_native_loader_overrides_are_not_forwarded_to_the_child(self):
        inherited = {name: 'SYNTHETIC_UNINSTALLED_INSTRUMENTATION' for name in
            ('LD_PRELOAD', 'LD_AUDIT', 'LD_LIBRARY_PATH', 'LD_DEBUG_OUTPUT',
             'DYLD_INSERT_LIBRARIES', 'DYLD_PRINT_TO_FILE', '_RLD_LIST',
             'GCONV_PATH', 'LOCPATH', 'NLSPATH', 'MALLOC_TRACE')}
        script = ('import json,os; print(json.dumps({name: os.environ.get(name) '
                  'for name in ' + repr(tuple(inherited)) + '}))')
        with patch.dict(os.environ, inherited):
            before = dict(os.environ)
            result = video._run([sys.executable, '-c', script], video.DEFAULT_LIMITS)
            self.assertTrue(dict(os.environ) == before, 'parent environment changed')
        self.assertEqual(result['exit_status'], 0)
        self.assertEqual(result['stderr'], '')
        self.assertEqual(json.loads(result['stdout']), dict.fromkeys(inherited))

    def test_diagnostic_controls_apply_to_child_without_mutating_parent_environment(self):
        inherited = {'FFREPORT': 'file=SYNTHETIC_PRIVATE_REPORT/fixture.log:level=16',
                     'AV_LOG_FORCE_COLOR': '1', 'AV_LOG_FORCE_NOCOLOR': '0',
                     'HATCH_SILENT_ENVIRONMENT_SENTINEL': 'synthetic-preserved-value'}
        names = tuple(inherited)
        script = ('import json,os; print(json.dumps({name: os.environ.get(name) '
                  'for name in ' + repr(names) + '}))')
        with patch.dict(os.environ, inherited):
            before = dict(os.environ)
            result = video._run([sys.executable, '-c', script], video.DEFAULT_LIMITS)
            # Avoid displaying unrelated inherited environment values on failure.
            self.assertTrue(dict(os.environ) == before, 'parent environment changed')
        self.assertEqual(result['exit_status'], 0)
        self.assertEqual(result['stderr'], '')
        self.assertEqual(json.loads(result['stdout']), {
            'FFREPORT': None, 'AV_LOG_FORCE_COLOR': None, 'AV_LOG_FORCE_NOCOLOR': '1',
            'HATCH_SILENT_ENVIRONMENT_SENTINEL': 'synthetic-preserved-value'})


if __name__ == '__main__':
    unittest.main()
