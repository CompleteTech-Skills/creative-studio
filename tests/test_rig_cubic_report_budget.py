"""Real cubic glTF inspection and cumulative exact-time report admission."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

from asset_library_validation import _inspect_geometry, _limits, inspect_geometry
from rig_errors import RigError
from rig_gltf import GltfBuilder
from rig_import import load_character_model
from rig_interop import validate_gltf
from rig_load_limits import LoadBudget


class CubicReportBudgetTests(unittest.TestCase):
    def make_asset(self, path, malformed=False):
        builder = GltfBuilder()
        position = builder.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3', bounds=True)
        times = builder.accessor([0, 0.5, 1], 'SCALAR', bounds=True)
        # glTF cubic output is in-tangent, value, out-tangent for each key.
        values = [[0, 0, 0], [0, 0, 0], [1, 0, 0],
                  [1, 0, 0], [0.5, 0, 0], [1, 0, 0],
                  [1, 0, 0], [1, 0, 0], [0, 0, 0]]
        if malformed:
            values.pop()
        output = builder.accessor(values, 'VEC3')
        builder.json.update(meshes=[{'primitives': [{'attributes': {'POSITION': position}}]}],
            nodes=[{'mesh': 0}, {'mesh': 0}], scenes=[{'nodes': [0, 1]}], scene=0,
            animations=[{'name': 'cubic', 'samplers': [{'input': times, 'output': output,
                'interpolation': 'CUBICSPLINE'}], 'channels': [
                {'sampler': 0, 'target': {'node': i, 'path': 'translation'}} for i in range(2)]}])
        builder.write(path)

    def test_real_validator_import_and_report_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cubic.glb'
            self.make_asset(path)
            original = path.read_bytes()
            digest = hashlib.sha256(original).hexdigest()
            validation = validate_gltf(path)
            self.assertTrue(validation['ok'], validation)
            self.assertEqual(validation['issues']['numErrors'], 0)
            self.assertEqual(validation['asset_sha256'], digest)
            model = load_character_model(path)
            tracks = model.clips['cubic']['tracks']
            self.assertEqual(len(tracks), 2)
            for track in tracks:
                self.assertEqual(track['interpolation'], 'CUBICSPLINE')
                self.assertEqual(track['times'], [0., 0.5, 1.])
                self.assertEqual(track['values'][1], [0.5, 0., 0.])
                self.assertEqual(track['in_tangents'][1], [1., 0., 0.])
                self.assertEqual(track['out_tangents'][1], [1., 0., 0.])
            public = inspect_geometry(path)
            self.assertEqual(public['source_files'][path.name], {'bytes': len(original), 'sha256': digest})
            self.assertTrue(public['independent_validation']['ok'])
            self.assertEqual(public['independent_validation']['asset_sha256'], digest)
            report_tracks = public['clips'][0]['tracks']
            self.assertEqual(len(report_tracks), 2)
            for track in report_tracks:
                self.assertEqual(track['interpolation'], 'CUBICSPLINE')
                self.assertEqual(track['key_times_exact'], [
                    {'numerator': 0, 'denominator': 1}, {'numerator': 1, 'denominator': 2},
                    {'numerator': 1, 'denominator': 1}])
            profile = _limits(None)
            probe = LoadBudget(profile)
            _inspect_geometry(path, limits=profile, budget=probe)
            first = probe.memory
            report_bytes = probe.report_bytes
            self.assertGreater(report_bytes, 0)
            _inspect_geometry(path, limits=profile, budget=probe)
            self.assertEqual(probe.report_bytes, 2 * report_bytes)
            second = probe.memory
            self.assertGreater(second, first)
            capped_profile = dict(profile, memory_bytes=second - 1)
            capped = LoadBudget(capped_profile)
            _inspect_geometry(path, limits=capped_profile, budget=capped)
            before = deepcopy(capped._state())
            # Observe actual calls, without replacing production functions. No ratio
            # object should be built on the rejected second inspection.
            ratios = []
            def trace(frame, event, arg):
                if event == 'call' and frame.f_code.co_name == '_ratio' and frame.f_globals.get('__name__') == 'asset_library_validation':
                    ratios.append(1)
                return trace
            previous = sys.getprofile()
            try:
                sys.setprofile(trace)
                with self.assertRaises(RigError) as caught:
                    _inspect_geometry(path, limits=capped_profile, budget=capped)
            finally:
                sys.setprofile(previous)
            self.assertEqual(caught.exception.report['code'], 'RIG_LIMIT_MEMORY')
            self.assertEqual(caught.exception.report['asset'], 'animation exact-time report expansion')
            self.assertEqual(ratios, [])
            self.assertEqual(capped._state(), before)
            self.assertEqual(path.read_bytes(), original)

    def test_malformed_cubic_rejected_and_ledger_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.glb'
            self.make_asset(path, malformed=True)
            original = path.read_bytes()
            self.assertFalse(validate_gltf(path)['ok'])
            profile = _limits(None)
            budget = LoadBudget(profile)
            before = deepcopy(budget._state())
            with self.assertRaises(RigError):
                _inspect_geometry(path, limits=profile, budget=budget)
            self.assertEqual(budget._state(), before)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(RigError):
                load_character_model(path, budget=budget)
            self.assertEqual(budget._state(), before)


if __name__ == '__main__':
    unittest.main()
