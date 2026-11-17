"""Cumulative admission of retained native import outputs."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from rig_contracts import DEFAULT_LIMITS
from rig_errors import RigError
from rig_gltf import GltfBuilder
from rig_import import load_character_model
from rig_load_limits import LoadBudget


class ImportOutputBudgetTests(unittest.TestCase):
    def animated(self, path, interpolation, malformed=False):
        b = GltfBuilder()
        pos = b.accessor([[0, 0, 0], [1, 0, 0], [0, 1, 0]], 'VEC3', target=34962, bounds=True)
        times = b.accessor([0, 1], 'SCALAR', bounds=True)
        values = [[0, 0, 0], [1, 0, 0]]
        if interpolation == 'CUBICSPLINE':
            values = [[0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0], [1, 0, 0], [0, 0, 0]]
        if malformed: values.pop()
        output = b.accessor(values, 'VEC3')
        b.json.update(nodes=[{'mesh': 0}], meshes=[{'primitives': [{'attributes': {'POSITION': pos}}]}],
                      scenes=[{'nodes': [0]}], animations=[{'samplers': [{'input': times, 'output': output, 'interpolation': interpolation}],
                      'channels': [{'sampler': 0, 'target': {'node': 0, 'path': 'translation'}}]}])
        b.write(path)

    def test_actual_animation_cumulative_and_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for mode in ('LINEAR', 'CUBICSPLINE'):
                with self.subTest(mode=mode):
                    path = Path(tmp) / 'animation.glb'
                    self.animated(path, mode)
                    budget = LoadBudget({**DEFAULT_LIMITS, 'keyframes': 4})
                    first = load_character_model(path, budget=budget)
                    track = next(iter(first.clips.values()))['tracks'][0]
                    self.assertEqual(track['values'], [[0., 0., 0.], [1., 0., 0.]])
                    if mode == 'CUBICSPLINE': self.assertEqual(len(track['in_tangents']), 2)
                    load_character_model(path, budget=budget)
                    before = deepcopy(budget._state())
                    with self.assertRaises(RigError): load_character_model(path, budget=budget)
                    self.assertEqual(budget._state(), before)
                    self.animated(path, mode, malformed=True)
                    fresh = LoadBudget(DEFAULT_LIMITS)
                    before = deepcopy(fresh._state())
                    with self.assertRaises(RigError): load_character_model(path, budget=fresh)
                    self.assertEqual(fresh._state(), before)

    def test_obj_output_cumulative_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'triangle.obj'
            path.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
            probe = LoadBudget(DEFAULT_LIMITS)
            model = load_character_model(path, budget=probe)
            self.assertEqual(len(model.geometry.meshes[0].triangles), 1)
            limit = probe.memory
            budget = LoadBudget({**DEFAULT_LIMITS, 'memory_bytes': limit})
            load_character_model(path, budget=budget)
            before = deepcopy(budget._state())
            with self.assertRaises(RigError): load_character_model(path, budget=budget)
            self.assertEqual(budget._state(), before)
            path.write_text('v 0 0 0\nf 1 2 3\n')
            fresh = LoadBudget(DEFAULT_LIMITS)
            before = deepcopy(fresh._state())
            with self.assertRaises(RigError): load_character_model(path, budget=fresh)
            self.assertEqual(fresh._state(), before)
