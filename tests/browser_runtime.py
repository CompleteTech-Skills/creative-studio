"""Use a configured or installed real Chromium; do not hide launch failures."""
import os
from pathlib import Path
import unittest


def launch_chromium(runtime):
    executable = os.environ.get('HATCH_BROWSER_EXECUTABLE')
    if executable:
        if not Path(executable).is_file():
            raise ValueError('configured HATCH_BROWSER_EXECUTABLE is missing')
    elif not Path(runtime.chromium.executable_path).is_file():
        raise unittest.SkipTest('Chromium is not installed; run python -m playwright install chromium')
    return runtime.chromium.launch(headless=True, executable_path=executable,
                                   args=['--no-sandbox', '--disable-dev-shm-usage'])


def launch_browser(runtime, *, engine='chromium'):
    """Launch the requested real engine; never substitute Chromium for another."""
    if engine not in ('chromium', 'firefox', 'webkit'):
        raise ValueError('browser engine must be chromium, firefox or webkit')
    if engine == 'chromium':
        return launch_chromium(runtime)
    browser_type = getattr(runtime, engine)
    if not Path(browser_type.executable_path).is_file():
        raise RuntimeError(engine + ' is not installed; run python -m playwright install ' + engine)
    return browser_type.launch(headless=True)
