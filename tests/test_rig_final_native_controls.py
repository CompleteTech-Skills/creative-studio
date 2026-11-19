"""Run unchanged, independently authored native geometry controls in the gate."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class FinalNativeControls(unittest.TestCase):
    def check_control(self, name):
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, PYTHONPATH=os.pathsep.join(
            (str(root / 'scripts'), str(root / 'tests'))))
        result = subprocess.run([sys.executable, str(root / 'tests/independent' / name)],
            cwd=root, env=environment, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rotated_x_anisotropic_parent_keeps_attainable_native_turn(self):
        self.check_control('test_rig_fold_rotated_x.py')

    def test_rotated_z_anisotropic_parent_keeps_attainable_native_turn(self):
        self.check_control('test_rig_fold_rotated_z.py')

    def test_final_validation_measures_all_active_position_contacts(self):
        self.check_control('test_rig_active_position_contacts.py')


if __name__ == '__main__':
    unittest.main()
