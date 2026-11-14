"""Final delivery intent, authoritative state and exact-byte failure gates.

These fixtures test admission. They do not claim a 720-frame production render.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest

from director_final_delivery import (validate_final_requests, assemble_final_frames,
                                    validate_production_selection,
                                    _validate_binding, _binding_state, _read_json, _directory)
from render3d_contracts import Render3DError
from rig_errors import RigError, object_hash
from test_render3d_contracts import request, backend, file


def final_requests():
    bounds = [0, 120, 264, 432, 576, 720]
    result = []
    for index, (start, end) in enumerate(zip(bounds, bounds[1:])):
        value = request()
        value.update(id='final-'+str(index), fps=dict(numerator=24, denominator=1),
            frame_range=dict(start=start, end=end),
            source_start=dict(numerator=0, denominator=1),
            playback_rate=dict(numerator=1, denominator=1),
            shot=dict(id='shot-'+str(index), version='1.0.0'), camera='camera-'+str(index),
            destination=dict(path='shots/'+str(index), owner_token='test-final'))
        value['settings']['resolution'] = [1280, 720]
        value['settings']['output']['alpha'] = 'opaque'
        result.append(value)
    return result


class FinalPlanTests(unittest.TestCase):
    def narrative_report(self, requests):
        return dict(schema_version=1, kind='director-narrative-acceptance',
            ready_for_final_render=True, frames=720, fps=dict(numerator=24, denominator=1),
            resolution=[1280, 720], production_shots=[dict(id=value['shot']['id'],
                start=value['frame_range']['start'], end=value['frame_range']['end'],
                scene=deepcopy(value['scene']), shot=deepcopy(value['shot'])) for value in requests],
            fixture=True)

    def test_exact_five_shot_720_frame_intent_is_not_execution_approval(self):
        requests = final_requests()
        before = deepcopy(requests)
        result = validate_final_requests(requests)
        self.assertEqual(result['frame_count'], 720)
        self.assertEqual(result['duration'], dict(numerator=30, denominator=1))
        self.assertEqual(result['resolution'], [1280, 720])
        self.assertFalse(result['execution_verified'])
        self.assertEqual(requests, before)

    def test_partial_mvp_and_missing_overlap_duplicate_or_extra_frames_fail(self):
        values = []
        values.append(final_requests()[:2])
        value = final_requests(); value[-1]['frame_range']['end'] = 719; values.append(value)
        value = final_requests(); value[-1]['frame_range']['end'] = 721; values.append(value)
        value = final_requests(); value[1]['frame_range']['start'] = 119; values.append(value)
        value = final_requests(); value[1]['frame_range']['start'] = 121; values.append(value)
        value = final_requests(); value[2]['shot'] = deepcopy(value[1]['shot']); values.append(value)
        value = final_requests(); value[2]['destination']['path'] = value[1]['destination']['path']; values.append(value)
        value = final_requests(); value.reverse(); values.append(value)
        for value in values:
            with self.subTest(value=value[-1]['frame_range']), self.assertRaises((RigError, Render3DError)):
                validate_final_requests(value)

    def test_wrong_fps_resolution_source_identity_and_output_fail(self):
        mutations = [
            lambda value: value[0].update(fps=dict(numerator=24000, denominator=1001)),
            lambda value: value[0]['settings'].update(resolution=[1278, 720]),
            lambda value: value[0]['settings'].update(resolution=[1281, 720]),
            lambda value: value[0]['settings'].update(resolution=[1280, 718]),
            lambda value: value[1]['settings'].update(resolution=[1920, 1080]),
            lambda value: value[1]['project'].update(version='2.0.0'),
            lambda value: value[1]['snapshot'].update(sha256='f'*64),
            lambda value: value[1]['settings']['output'].update(alpha='straight'),
            lambda value: value[1]['settings']['output'].update(bit_depth=16),
            lambda value: value[1]['settings'].update(engine='eevee'),
        ]
        for mutation in mutations:
            value = final_requests(); mutation(value)
            with self.subTest(mutation=mutation), self.assertRaises((RigError, Render3DError)):
                validate_final_requests(value)

    def test_larger_consistent_resolution_and_authored_source_clock_are_preserved(self):
        requests = final_requests()
        for request_value in requests:
            request_value['settings']['resolution'] = [1920, 1080]
            request_value['playback_rate'] = dict(numerator=1, denominator=2)
        before = deepcopy(requests)
        self.assertEqual(validate_final_requests(requests)['resolution'], [1920, 1080])
        self.assertEqual(requests, before)

    def test_claimed_verified_metadata_cannot_replace_native_job_store(self):
        class ClaimedStore:
            def status(self, *args, **kwargs):
                raise AssertionError('Untrusted status adapter was consulted')
        with self.assertRaisesRegex(RigError, 'authoritative native JobStore'):
            assemble_final_frames(ClaimedStore(), '.', [dict(verified=True)]*5)

    def test_only_explicit_production_subset_is_selected(self):
        requests = final_requests()
        report = self.narrative_report(requests)
        report['proof_shots'] = [dict(id='alternate-handoff-view'), dict(id='close-up-view')]
        self.assertEqual(validate_production_selection(requests, report)['frame_count'], 720)
        changed = deepcopy(requests)
        changed[3]['shot']['id'] = 'alternate-handoff-view'
        with self.assertRaisesRegex(RigError, 'explicit accepted production shot'):
            validate_production_selection(changed, report)
        report['production_shots'].extend(report['proof_shots'])
        with self.assertRaisesRegex(RigError, 'ordered production shots'):
            validate_production_selection(requests, report)

    def test_partial_or_stale_narrative_selection_and_changed_framing_are_rejected(self):
        requests = final_requests()
        for mutate in (lambda value: value.update(ready_for_final_render=False),
                       lambda value: value.update(frames=120),
                       lambda value: value['production_shots'][2].update(start=265),
                       lambda value: value['production_shots'][2]['shot'].update(version='2.0.0'),
                       lambda value: value.update(production_shots=None)):
            report = self.narrative_report(requests); mutate(report)
            with self.subTest(mutate=mutate), self.assertRaises(RigError):
                validate_production_selection(requests, report)
        report = self.narrative_report(requests)
        for value in requests:
            value['settings']['resolution'] = [1280, 800]
        with self.assertRaisesRegex(RigError, 'framing aspect'):
            validate_production_selection(requests, report)


class FinalEvidenceTests(unittest.TestCase):
    def test_native_directory_binding_uses_directory_type_not_regular_file_gate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'snapshot').mkdir()
            self.assertEqual(_directory(root, 'snapshot'), root/'snapshot')
            (root/'not-directory').write_bytes(b'fixture')
            for name in ('not-directory', 'missing-directory'):
                with self.subTest(name=name), self.assertRaises(RigError):
                    _directory(root, name)

    def binding(self):
        return dict(job='a'*64, job_revision=1, request_sha256='b'*64,
                    backend_sha256='c'*64, directory='shots/entry', snapshot_directory='snapshot')

    def test_unsafe_alias_and_malformed_binding_values_are_typed_failures(self):
        for field, value in [('job', 'not-a-digest'), ('job_revision', True),
                             ('job_revision', 0), ('directory', '../foreign'),
                             ('snapshot_directory', '/outside'), ('directory', None),
                             ('snapshot_directory', 1), ('directory', 'C:/outside')]:
            binding = self.binding(); binding[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(RigError):
                _validate_binding(binding)
        binding = self.binding(); binding['verified'] = True
        with self.assertRaises(RigError):
            _validate_binding(binding)

    def test_exact_raw_evidence_hash_is_checked_before_json_semantics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); raw = b'{"state":"engineering-fixture"}\n'
            path = root/'evidence.json'; path.write_bytes(raw)
            expected = dict(path='evidence.json', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            value, reference = _read_json(root, 'evidence.json', expected=expected)
            self.assertEqual(value['state'], 'engineering-fixture')
            self.assertEqual(reference, expected)
            changed = raw.replace(b'fixture', b'fixturf')
            path.write_bytes(changed)
            with self.assertRaisesRegex(RigError, 'exact reference'):
                _read_json(root, 'evidence.json', expected=expected)
            self.assertEqual(path.read_bytes(), changed)
            path.write_bytes(raw+b' ')
            with self.assertRaises((RigError, Render3DError)):
                _read_json(root, 'evidence.json', expected=expected)
            self.assertEqual(path.read_bytes(), raw+b' ')

    def test_corrupt_duplicate_key_json_is_not_accepted_as_native_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); path = root/'evidence.json'
            for raw in (b'{"state":"complete","state":"failed"}', b'{"value":NaN}', b'broken'):
                path.write_bytes(raw)
                with self.subTest(raw=raw), self.assertRaises(RigError):
                    _read_json(root, 'evidence.json')

    def test_real_canceled_native_job_cannot_authorize_delivery(self):
        from render3d_jobs import JobStore
        from render3d_snapshot import Scene3DAdapter, create_snapshot
        from scene3d_example import build_example
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = build_example(root/'source')
            create_snapshot(root/'source', root/'snapshot', Scene3DAdapter())
            value = request()
            value.update(project={key: bundle['project'][key] for key in ('id', 'version')},
                scene={key: bundle['project']['scenes'][0][key] for key in ('id', 'version')},
                shot={key: bundle['project']['shots'][0][key] for key in ('id', 'version')},
                fps=dict(numerator=24, denominator=1), frame_range=dict(start=0, end=1),
                source_start=dict(numerator=0, denominator=1), playback_rate=dict(numerator=1, denominator=1),
                snapshot=file(payload=(root/'snapshot/snapshot.json').read_bytes()))
            selected_shot = bundle['documents'][bundle['project']['shots'][0]['path']]
            value['camera'] = selected_shot['camera']
            store = JobStore(root/'jobs')
            identity = backend()
            job = store.submit(value, root/'snapshot', identity)
            store.cancel(job)
            state = store.status(job)
            binding = dict(job=job, job_revision=state['revision'], request_sha256=object_hash(value),
                backend_sha256=object_hash(identity), directory='shots/entry', snapshot_directory='snapshot')
            with self.assertRaisesRegex(RigError, 'incomplete'):
                _binding_state(store, binding)
            self.assertEqual(store.status(job)['state'], 'cancelled')
            self.assertFalse((root/'shots').exists())


if __name__ == '__main__':
    unittest.main()
