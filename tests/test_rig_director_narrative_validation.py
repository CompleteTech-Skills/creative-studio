"""Adversarial admission tests; synthetic rows never grant native acceptance."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from director_narrative_validation import (_artifact, _matrix, _verify_rows,
    verify_narrative_acceptance, ACTORS, FPS, FRAMES, SHOTS, LIMITS)
from rig_errors import RigError


def artifact_ref(path, data):
    return dict(artifact_id="numeric-proof", kind="narrative-proof", schema_version=1, semantic_version="1.0.0",
        path=path, byte_size=len(data), sha256=hashlib.sha256(data).hexdigest(), media_type="application/json",
        provenance_ref=None, fixture=True, dependencies=[])


def numerical_envelope():
    """A bounded transport fixture, deliberately without native image evidence."""
    shots = [dict(id=n, start_frame=a, end_frame=b,
                  source_start=dict(numerator=0, denominator=1),
                  playback_rate=dict(numerator=1, denominator=1)) for n, a, b in SHOTS]
    rows = []
    for frame in range(FRAMES):
        shot = next(s for s in shots if s['start_frame'] <= frame < s['end_frame'])
        owner = None if frame < 360 else 'aster' if frame < 528 else 'moss'
        contacts = ACTORS if 516 <= frame <= 540 else (owner,) if owner else ()
        root = np.eye(4); root[2, 3] = 3*frame/(FRAMES-1)
        rows.append(dict(frame=frame, time=dict(numerator=frame, denominator=FPS),
            shot=shot['id'], local_frame=frame-shot['start_frame'], owner=owner,
            prop_matrix=np.eye(4).tolist(), prop_native_matrix=np.eye(4).tolist(), prop_matrix_error=0.,
            collision=dict(passed=True), grip_errors_m={a:0. for a in contacts},
            grip_rotation_errors_rad={a:0. for a in contacts},
            actors={a:dict(root_matrix=root.tolist(), pose_equivalence_error=0.,
                feet={s:dict(planted=True, plant_drift_m=0., sole_y_m=0.) for s in ('left','right')}) for a in ACTORS}))
    return dict(schema_version=1, kind='director-narrative-acceptance', frames=FRAMES,
        fps=dict(numerator=FPS, denominator=1), production_shots=shots,
        limits=deepcopy(LIMITS), numeric_status='passed', ready_for_final_render=True,
        ownership_changes=[dict(frame=360, previous_owner=None, next_owner='aster'),
                           dict(frame=528, previous_owner='aster', next_owner='moss')],
        metrics=dict(root_travel_m={a:3. for a in ACTORS}), rows=rows)


class NarrativeProofAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.proof = numerical_envelope()

    def test_transport_envelope_requires_all_frames_and_original_limits(self):
        _verify_rows(self.proof)
        for mutate in (lambda p:p['rows'].pop(), lambda p:p['limits'].update(grip_m=1.),
                       lambda p:p.update(production_shots=[])):
            value = deepcopy(self.proof); mutate(value)
            with self.assertRaises(RigError): _verify_rows(value)

    def test_ready_flag_cannot_replace_exact_retained_native_views(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); project = root/'project'; project.mkdir()
            evidence = root/'evidence'; evidence.mkdir()
            payload = json.dumps(self.proof).encode(); (evidence/'proof.json').write_bytes(payload)
            ref = artifact_ref('proof.json',payload)
            with self.assertRaises(RigError) as error:
                verify_narrative_acceptance(project,evidence,ref)
            self.assertEqual(error.exception.report['code'], 'DIRECTOR_NARRATIVE_NATIVE_VIEWS')

    def test_stale_duplicate_owner_and_missing_paired_contact_rejected(self):
        for mutate in (lambda p:p['rows'][528].update(owner='aster'),
                       lambda p:p['ownership_changes'].append(deepcopy(p['ownership_changes'][-1])),
                       lambda p:p['rows'][527]['grip_errors_m'].pop('moss')):
            value = deepcopy(self.proof); mutate(value)
            with self.assertRaises(RigError): _verify_rows(value)

    def test_prop_jump_or_disagreeing_native_matrix_cannot_hide_behind_zero_metric(self):
        for native in (False,True):
            value = deepcopy(self.proof)
            value['rows'][500]['prop_matrix'][0][3] = .1
            if native: value['rows'][500]['prop_native_matrix'][0][3] = .1
            with self.assertRaises(RigError): _verify_rows(value)

    def test_aggregate_travel_cannot_hide_stationary_roots(self):
        for row in self.proof['rows']:
            for actor in ACTORS: row['actors'][actor]['root_matrix'] = np.eye(4).tolist()
        with self.assertRaises(RigError): _verify_rows(self.proof)

    def test_nonfinite_contact_and_bad_boot_contact_are_rejected(self):
        for mutate in (lambda p:p['rows'][528]['grip_errors_m'].update(moss=float('nan')),
                       lambda p:p['rows'][100]['actors']['aster']['feet']['left'].update(sole_y_m=.003),
                       lambda p:p['rows'][100]['actors']['aster']['feet']['left'].update(plant_drift_m=.002)):
            value = deepcopy(self.proof); mutate(value)
            with self.assertRaises(RigError): _verify_rows(value)

    def test_rigid_transform_rejects_scale_reflection_nonfinite_and_wrong_shape(self):
        for bad in ([[1]], None, np.diag([2.,1,1,1]).tolist(), np.diag([-1.,1,1,1]).tolist(),
                    [[float('nan')]*4]*4):
            with self.subTest(bad=bad), self.assertRaises(RigError): _matrix(bad)

    def test_exact_bounded_artifact_rejects_stale_bytes_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); data=b'current'; path=root/'proof.json'; path.write_bytes(data)
            ref=artifact_ref('proof.json',data)
            self.assertEqual(_artifact(root,ref),data)
            path.write_bytes(b'changed')
            with self.assertRaises(RigError): _artifact(root,ref)
            path.unlink(); target=root/'target'; target.write_bytes(data); path.symlink_to(target)
            with self.assertRaises(RigError): _artifact(root,ref)

    def test_malformed_reference_and_escape_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for ref in (None, {}, dict(path='../escape', byte_size=1, sha256='0'*64),
                        dict(path='x',byte_size=True,sha256='0'*64)):
                with self.subTest(ref=ref),self.assertRaises(RigError): _artifact(root,ref)


if __name__ == '__main__': unittest.main()
