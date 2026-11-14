"""Actual native revisions and failures; no film/provider execution is claimed."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from director_revision import RevisionService, _hash
from editor3d_repository import EditorRepository
from rig_errors import RigError
from scene3d_contracts import all_refs, canonical_bytes
from scene3d_example import build_example, node
from scene3d_materials import obj_material_bindings


def project(root, *, constraints=(), camera_locked=False):
    source = root/'source'
    build_example(source)
    scene = json.loads((source/'example-scene.json').read_text())
    camera = next(n for n in scene['nodes'] if n['id'] == 'camera')
    camera['locked'] = camera_locked
    second = deepcopy(camera); second.update(id='other-camera', name='Other camera', locked=False)
    scene['nodes'].append(second); scene['cameras'].append(second['id'])
    light = node('lab-light', 'light', 'root')
    light['light'] = dict(type='point', color=[1, 1, 1], intensity=10)
    scene['nodes'].append(light); scene['lights'].append(light['id'])
    scene['shots'].append('other-shot')
    (source/'example-scene.json').write_bytes(canonical_bytes(scene))
    shot = json.loads((source/'example-shot.json').read_text())
    shot.update(id='other-shot', name='Other shot', camera='other-camera')
    (source/'other-shot.json').write_bytes(canonical_bytes(shot))
    p = json.loads((source/'project.json').read_text())
    ref = dict(id='other-shot', version='1.0.0', kind='scene3d-shot',
               path='other-shot.json', sha256=hashlib.sha256(canonical_bytes(shot)).hexdigest())
    p['shots'].append(ref); p['dependency_lock'].append(ref)
    p['metadata']['director_locked_constraints'] = json.dumps(list(constraints))
    (source/'project.json').write_bytes(canonical_bytes(p))
    asset = json.loads((source/'triangle.json').read_text())
    asset['material_bindings'] = obj_material_bindings(asset['id'])
    (source/'triangle.json').write_bytes(canonical_bytes(asset))
    # Only known Scene3D fixture manifests are rewritten; native Rig/clip bytes
    # are never treated as arbitrary JSON by a production revision adapter.
    for _ in range(6):
        for path in source.glob('*.json'):
            value = json.loads(path.read_text())
            for ref in all_refs(value):
                ref['sha256'] = hashlib.sha256((source/ref['path']).read_bytes()).hexdigest()
            path.write_bytes(canonical_bytes(value))
    return EditorRepository.create(source, root/'repository', scene_path='example-scene.json')


class CoreRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = project(self.root)
        self.service = RevisionService(self.repository)

    def plan(self, change=None, instruction='Set camera camera near to 0.2', **extra):
        state = self.repository.snapshot()
        return self.service.plan(instruction, expected_revision=state['revision'],
            expected_sha256=state['sha256'], change=change, **extra)

    def test_camera_plan_is_readonly_and_only_invalidates_its_actual_shot(self):
        before = self.repository.snapshot()
        planned = self.plan()
        self.assertEqual(self.repository.snapshot(), before)
        self.assertEqual(planned['patch']['instruction'], 'Set camera camera near to 0.2')
        self.assertEqual([s['id'] for s in planned['impact']['affected_shots']], ['example-shot'])
        self.assertFalse(planned['impact']['timeline_reflow'])
        self.assertEqual(planned['impact']['estimate']['render_frames'], 24)
        self.assertEqual(planned['impact']['estimate']['provider_calls'], 0)
        self.assertIsNone(planned['impact']['estimate']['amount'])

    def test_camera_edit_is_real_versioned_reopened_and_undo_redo_preserves_assets(self):
        before = self.repository.snapshot()
        source = self.repository.read_file('triangle.obj', before['sha256'])
        receipt = self.service.apply(self.plan()['patch'], transaction_id='camera-change')
        current = EditorRepository(self.repository.root).snapshot()
        camera = next(n for n in current['scene']['nodes'] if n['id'] == 'camera')
        self.assertEqual(camera['camera']['near'], .2)
        self.assertEqual(current['revision'], 1)
        self.assertEqual(self.repository.read_file('triangle.obj', current['sha256']), source)
        self.assertIn('triangle.obj', receipt['comparison']['preserved_files'])
        self.assertNotEqual(current['scene']['version'], before['scene']['version'])
        undone = self.service.undo(1, 'undo-camera', expected_sha256=current['sha256'])
        self.assertEqual(next(n for n in undone['scene']['nodes'] if n['id'] == 'camera')['camera']['near'], .1)
        redone = self.service.redo(2, 'redo-camera', expected_sha256=undone['sha256'])
        self.assertEqual(next(n for n in redone['scene']['nodes'] if n['id'] == 'camera')['camera']['near'], .2)
        self.assertEqual(receipt['regeneration'], 'pending-native-execution-admission')
        self.assertEqual(receipt['visual_review'], 'not-performed')

    def test_native_transaction_replay_keeps_one_committed_revision(self):
        patch = self.plan()['patch']
        first = self.service.apply(patch, transaction_id='same-delivery')
        again = self.service.apply(patch, transaction_id='same-delivery')
        self.assertEqual(first, again)
        self.assertEqual(len(self.repository.history()), 2)

    def test_transaction_identity_binds_exact_patch_not_only_native_commands(self):
        first = self.plan()['patch']
        second = self.plan(instruction='Change camera camera near to 0.2')['patch']
        self.service.apply(first, transaction_id='exact-patch')
        after = self.repository.snapshot()
        with self.assertRaises(RigError):
            self.service.apply(second, transaction_id='exact-patch')
        self.assertEqual(self.repository.snapshot(), after)
        world = self.plan(change=dict(operation='world', target='world', properties={'exposure': 1}))['patch']
        self.service.apply(world, transaction_id='unrelated-world')
        current = self.repository.snapshot()
        later = self.plan()['patch']
        with self.assertRaises(RigError):
            self.service.apply(later, transaction_id='exact-patch')
        self.assertEqual(self.repository.snapshot(), current)

    def test_material_adapter_transaction_identity_retains_exact_patch(self):
        change = dict(operation='material', target='mesh', slot='surface', surface={'roughness': .7})
        # Select the canonical actual primitive slot, never a guessed slot ID.
        from editor3d_inspectors import material_slot_projection
        _, _, slots = material_slot_projection(self.repository.snapshot(), 'mesh')
        change['slot'] = slots[0]['slot']
        first = self.plan(change=change, instruction='Set selected roughness to .7')['patch']
        second = self.plan(change=change, instruction='Change selected roughness to .7')['patch']
        self.service.apply(first, transaction_id='material-exact-patch')
        state = self.repository.snapshot()
        with self.assertRaises(RigError):
            self.service.apply(second, transaction_id='material-exact-patch')
        self.assertEqual(self.repository.snapshot(), state)
        replay = self.service.apply(first, transaction_id='material-exact-patch')
        self.assertEqual(replay['after']['sha256'], state['sha256'])

    def test_camera_angle_and_position_use_native_transform_authority(self):
        rotation = [0, 0.7071067811865476, 0, 0.7071067811865476]
        plan = self.plan(dict(operation='camera-pose', target='camera',
                              transform=dict(rotation=rotation, translation=[1, 2, 3])),
                         'Set the camera angle and position explicitly')
        receipt = self.service.apply(plan['patch'], transaction_id='camera-pose')
        state = self.repository.snapshot()
        camera = next(n for n in state['scene']['nodes'] if n['id'] == 'camera')
        self.assertEqual(camera['transform']['rotation'], rotation)
        self.assertEqual(camera['transform']['translation'], [1, 2, 3])
        self.assertEqual([item['id'] for item in receipt['impact']['affected_shots']], ['example-shot'])
        self.assertEqual(next(n for n in state['scene']['nodes'] if n['id'] == 'other-camera')['transform']['translation'], [0, 2, 4])

    def test_stale_patch_and_tampered_patch_do_not_mutate_saved_project(self):
        old = self.plan()['patch']
        self.service.apply(self.plan(instruction='Set camera camera far to 200')['patch'], transaction_id='newer')
        state = self.repository.snapshot()
        with self.assertRaises(RigError):
            self.service.apply(old, transaction_id='stale-delivery')
        bad = deepcopy(old); bad['change']['properties']['near'] = 400
        with self.assertRaises(RigError):
            self.service.apply(bad, transaction_id='changed-bytes')
        self.assertEqual(self.repository.snapshot(), state)

    def test_malformed_rehashed_native_change_rejects_before_mutation(self):
        patch = self.plan()['patch']; before = self.repository.snapshot()
        patch['change']['properties']['near'] = 500
        patch['sha256'] = _hash({k: v for k, v in patch.items() if k != 'sha256'})
        with self.assertRaises(RigError): self.service.apply(patch, transaction_id='invalid-near')
        self.assertEqual(self.repository.snapshot(), before)

    def test_real_material_assignment_and_edit_version_only_selected_slot(self):
        change = dict(operation='material', target='mesh', slot='surface', surface={'base_color': [0, 0, 1, 1]})
        first = self.service.apply(self.plan(change, 'Make the mesh blue')['patch'], transaction_id='assign-blue')
        state = self.repository.snapshot()
        blue = next(n for n in state['scene']['nodes'] if n['id'] == 'mesh')['material_overrides'][0]['material']
        second = self.service.apply(self.plan(dict(change, surface={'roughness': .2}), 'Make the mesh less rough')['patch'], transaction_id='roughness')
        final = self.repository.snapshot()
        newer = next(n for n in final['scene']['nodes'] if n['id'] == 'mesh')['material_overrides'][0]['material']
        self.assertEqual(blue['id'], newer['id'])
        self.assertEqual(blue['version'], '1.0.0')
        self.assertEqual(newer['version'], '1.0.1')
        self.assertNotEqual(blue['sha256'], newer['sha256'])
        self.assertIn('triangle.obj', second['comparison']['preserved_files'])
        self.assertEqual(len(first['impact']['affected_shots']), 2)

    def test_authoritative_identity_lock_needs_exact_trusted_authorizer(self):
        other = self.root/'locked'; other.mkdir()
        repository = project(other, constraints=['identity:mesh'])
        service = RevisionService(repository)
        state = repository.snapshot()
        patch = service.plan('Make the mesh blue', expected_revision=0, expected_sha256=state['sha256'],
            change=dict(operation='material', target='mesh', slot='surface', surface={'base_color': [0, 0, 1, 1]}))['patch']
        with self.assertRaises(RigError) as error: service.apply(patch, transaction_id='no-authority')
        self.assertEqual(error.exception.report['code'], 'DIRECTOR_REVISION_AUTHORIZATION')
        self.assertEqual(repository.snapshot(), state)
        denied = RevisionService(repository, authorizer=lambda p, a: False)
        with self.assertRaises(RigError): denied.apply(patch, transaction_id='denied', authorization={'approved': True})
        # This trusted synthetic authority tests binding only, never human signoff.
        authorized = RevisionService(repository, authorizer=lambda p, a: a == p['sha256'])
        receipt = authorized.apply(patch, transaction_id='authorized-fixture', authorization=patch['sha256'])
        self.assertEqual(receipt['after']['revision'], 1)

    def test_omitting_native_constraints_even_with_new_digest_is_rejected(self):
        other = self.root/'locked-source'; other.mkdir()
        repo = project(other, constraints=['camera:camera']); state = repo.snapshot()
        service = RevisionService(repo)
        patch = service.plan('Set camera camera near to 0.2', expected_revision=0, expected_sha256=state['sha256'])['patch']
        patch['locked_constraints'] = []
        patch['sha256'] = _hash({k: v for k, v in patch.items() if k != 'sha256'})
        with self.assertRaises(RigError): service.apply(patch, transaction_id='removed-policy')
        self.assertEqual(repo.snapshot(), state)

    def test_many_original_constraints_are_counted_once_and_revalidated(self):
        constraints = ['identity:protected-'+str(index) for index in range(129)]
        repository = project(self.root/'many-locks', constraints=constraints)
        service = RevisionService(repository, locked_constraints=constraints)
        state = repository.snapshot()
        patch = service.plan('Set camera camera near to 0.2', expected_revision=0,
                             expected_sha256=state['sha256'], locked_constraints=constraints)['patch']
        self.assertEqual(patch['locked_constraints'], sorted(constraints))
        result = service.apply(patch, transaction_id='many-original-locks')
        self.assertEqual(result['after']['revision'], 1)
        too_many = ['identity:extra-'+str(index) for index in range(128)]
        with self.assertRaises(RigError) as error:
            service.plan('Set camera camera near to 0.3', expected_revision=1,
                         expected_sha256=repository.snapshot()['sha256'], locked_constraints=too_many)
        self.assertEqual(error.exception.report['code'], 'DIRECTOR_REVISION_POLICY')
        self.assertEqual(repository.snapshot()['revision'], 1)

    def test_locked_native_node_cannot_be_revised(self):
        other = self.root/'native-lock'; other.mkdir()
        repo = project(other, camera_locked=True); state = repo.snapshot()
        with self.assertRaises(RigError) as error:
            RevisionService(repo).plan('Set camera camera near to 0.2', expected_revision=0, expected_sha256=state['sha256'])
        self.assertEqual(error.exception.report['code'], 'DIRECTOR_REVISION_LOCKED')

    def test_exact_duration_plan_and_bounded_exponents(self):
        planned = self.plan(dict(operation='duration', target='example-shot', additional_seconds='3'), 'Extend example-shot by three seconds')
        self.assertEqual(planned['patch']['change']['duration_frames'], 96)
        self.assertEqual(planned['impact']['affected_shots'][0]['end_frame'], 96)
        self.assertTrue(planned['impact']['timeline_reflow'])
        for seconds in ('1e999999999', '1/0', '0.01', True, -1, 10**400):
            with self.subTest(seconds=str(seconds)[:40]), self.assertRaises(RigError):
                self.plan(dict(operation='duration', target='example-shot', additional_seconds=seconds), 'Invalid duration')

    def test_native_frame_budget_prevents_revision_and_regeneration_budget_is_separate(self):
        before = self.repository.snapshot()
        with self.assertRaises(RigError):
            self.plan(dict(operation='duration', target='example-shot', additional_seconds=1000), 'Oversized duration')
        service = RevisionService(self.repository, max_affected_frames=1)
        with self.assertRaises(RigError):
            service.plan('Set camera camera near to 0.2', expected_revision=0, expected_sha256=before['sha256'])
        self.assertEqual(self.repository.snapshot(), before)

    def test_ending_plan_is_scoped_to_actual_light_and_half_open_range(self):
        change = dict(operation='ending', target='example-shot', light='lab-light', frame=12, intensity=25)
        planned = self.plan(change, 'Turn on the laboratory light during the ending')
        self.assertEqual(planned['impact']['affected_shots'][0]['start_frame'], 12)
        self.assertEqual(planned['impact']['affected_shots'][0]['end_frame'], 24)
        self.assertEqual(planned['impact']['changed_ids'], ['example-shot', 'lab-light'])
        with self.assertRaises(RigError): self.plan(dict(change, frame=24), 'Outside the shot')

    def test_ambiguous_high_impact_instruction_never_guesses_or_mutates(self):
        before = self.repository.snapshot()
        for text in ('Make the ending better', 'Give the blue robot a smaller head', 'Move the camera closer'):
            with self.subTest(text=text), self.assertRaises(RigError) as error: self.plan(instruction=text)
            self.assertEqual(error.exception.report['code'], 'DIRECTOR_REVISION_AMBIGUOUS')
        self.assertEqual(self.repository.snapshot(), before)


class NativeTimelineRevisionTests(unittest.TestCase):
    """Required real timeline acceptance; no skip masks a missing native owner."""
    setUp = CoreRevisionTests.setUp
    plan = CoreRevisionTests.plan

    def test_duration_and_ending_edits_persist_real_native_versions(self):
        # This test intentionally requires the authoritative timeline dependency.
        import editor3d_timeline
        state = self.repository.snapshot()
        duration = self.plan(dict(operation='duration', target='example-shot', additional_seconds='3'), 'Extend example shot')
        self.service.apply(duration['patch'], transaction_id='extend-native')
        current = self.repository.snapshot()
        shots = [current['documents'][r['path']] for r in current['project']['shots']]
        self.assertEqual(next(s for s in shots if s['id'] == 'example-shot')['duration_frames'], 96)
        ending = self.plan(dict(operation='ending', target='example-shot', light='lab-light', frame=48, intensity=40), 'Turn light on')
        receipt = self.service.apply(ending['patch'], transaction_id='ending-native')
        final = self.repository.snapshot()
        shot = next(final['documents'][r['path']] for r in final['project']['shots'] if r['id'] == 'example-shot')
        track = next(t for t in shot['tracks'] if t['target'] == 'lab-light')
        self.assertEqual(track['property'], 'light_intensity')
        self.assertEqual(track['keys'], [dict(frame=48, value=40)])
        self.assertEqual(receipt['impact']['affected_shots'][0]['start_frame'], 48)
        self.assertEqual(self.repository.snapshot(state['revision'])['sha256'], state['sha256'])


class CanonicalRevisionMethodTests(unittest.TestCase):
    """Method envelopes use actual files; the fixture publisher is not shipped."""
    setUp = CoreRevisionTests.setUp

    def publish(self, payload, *, kind, artifact_id=None, schema_version=1, semantic_version='1.0.0', dependencies=(), fixture=True):
        directory = self.root/'fixture-artifacts'; directory.mkdir(exist_ok=True)
        digest = hashlib.sha256(payload).hexdigest(); path = directory/(digest+'.json')
        path.write_bytes(payload)
        return dict(artifact_id=artifact_id or 'fixture-'+digest[:24], kind=kind, schema_version=schema_version,
                    semantic_version=semantic_version, path=path.relative_to(self.root).as_posix(),
                    sha256=digest, byte_size=len(payload), media_type='application/json',
                    provenance_ref=None, fixture=fixture, dependencies=list(dependencies))

    def request(self, method):
        root = Path(__file__).resolve().parents[1]
        documents = json.loads((root/'examples/director-contracts/contracts.json').read_text())
        request = deepcopy(next(d for d in documents if d.get('kind') == 'director-request' and d['method'] == method))
        state = self.repository.snapshot()
        request['context'].update(project_id=state['project']['id'],
            project_revision_sha256=state['inventory']['project.json']['sha256'],
            deadline=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
            idempotency_key='method-'+method.rsplit('.', 1)[1])
        return request

    def planned(self):
        request = self.request('RevisionService.plan')
        request['input'].update(instruction='Set camera camera near to 0.2', locked_constraints=[],
            current_project=self.service.artifact(self.repository.snapshot(), 'project.json', artifact_root=self.root),
            budget=self.publish(b'{"fixture":"no-regeneration-admission"}', kind='budget-fixture'),
            capabilities=self.publish(b'{"fixture":"no-provider-execution"}', kind='capability-fixture'))
        result = self.service.execute(request, artifact_root=self.root, publish_artifact=self.publish)
        return request, result

    def test_plan_apply_envelopes_pin_real_native_project_closure(self):
        from director_contracts import validate_method_result, _read_artifact
        request, planned = self.planned()
        validate_method_result('RevisionService.plan', planned, request)
        patch = planned['value']['patch']
        value = json.loads(_read_artifact(self.root, patch))
        self.assertEqual(value['parent']['artifact'], request['input']['current_project'])
        apply = self.request('RevisionService.apply')
        apply['input'].update(patch=patch, expected_revision=request['input']['current_project']['sha256'],
                              authorization=self.publish(b'{"fixture":"no-protected-change"}', kind='authorization-fixture'))
        result = self.service.execute(apply, artifact_root=self.root)
        validate_method_result('RevisionService.apply', result, apply)
        native = result['value']['project']
        document = json.loads(_read_artifact(self.root, native))
        self.assertEqual(document, self.repository.snapshot()['project'])
        # Reference points into the real immutable native closure, where all
        # document-relative files remain present, not a copied bare manifest.
        from scene3d_contracts import load_project
        reopened = load_project((self.root/native['path']).parent)
        self.assertEqual(reopened['project'], document)

    def test_bad_context_and_expired_apply_never_commit(self):
        _, planned = self.planned()
        apply = self.request('RevisionService.apply')
        before = self.repository.snapshot()
        apply['input'].update(patch=planned['value']['patch'], expected_revision=before['inventory']['project.json']['sha256'],
                             authorization=self.publish(b'{}', kind='authorization-fixture'))
        wrong = deepcopy(apply); wrong['context']['project_id'] = 'different-project'
        with self.assertRaises(RigError) as error: self.service.execute(wrong, artifact_root=self.root)
        self.assertEqual(error.exception.report['code'], 'DIRECTOR_REVISION_CONTEXT')
        expired = deepcopy(apply); expired['context']['deadline'] = '2000-01-01T00:00:00Z'
        with self.assertRaises(RigError): self.service.execute(expired, artifact_root=self.root)
        self.assertEqual(self.repository.snapshot(), before)

    def test_missing_canonical_publisher_does_not_create_plan_or_mutate(self):
        request, _ = self.planned(); before = self.repository.snapshot()
        with self.assertRaises(RigError) as error: self.service.execute(request, artifact_root=self.root)
        self.assertEqual(error.exception.report['code'], 'DIRECTOR_REVISION_DEPENDENCY')
        self.assertEqual(self.repository.snapshot(), before)

    def test_changed_patch_artifact_bytes_fail_before_native_mutation(self):
        _, planned = self.planned(); before = self.repository.snapshot()
        ref = planned['value']['patch']; (self.root/ref['path']).write_bytes(b'{}')
        request = self.request('RevisionService.apply')
        request['input'].update(patch=ref, expected_revision=before['inventory']['project.json']['sha256'],
                                authorization=self.publish(b'{}', kind='authorization-fixture'))
        with self.assertRaises(RigError): self.service.execute(request, artifact_root=self.root)
        self.assertEqual(self.repository.snapshot(), before)

    def test_impact_propagates_real_shot_dependency_to_only_its_graph_descendants(self):
        _, planned = self.planned(); state = self.repository.snapshot()
        scene = self.service.artifact(state, state['scene_path'], artifact_root=self.root)
        first = self.service.artifact(state, 'example-shot.json', artifact_root=self.root)
        second = self.service.artifact(state, 'other-shot.json', artifact_root=self.root)
        def graph_node(identifier, inputs):
            return dict(id=identifier, operation='RenderAdapter.submit', inputs=inputs,
                        output_schema='render-job', resource_class='render', cache_key='1'*64,
                        gates=[], native_owner='render3d')
        graph = dict(nodes=[graph_node('render-main', [scene, first]),
                            graph_node('render-other', [scene, second]), graph_node('encode', [first])],
                     edges=[dict(source='render-main', target='encode', artifact=first)],
                     critical_path_seconds=None)
        reference = self.publish(json.dumps(graph).encode(), kind='execution-graph-fixture')
        request = self.request('RevisionService.impact')
        request['input'].update(patch=planned['value']['patch'], graph=reference)
        result = self.service.execute(request, artifact_root=self.root)
        self.assertEqual(result['value']['changed_ids'], ['camera', 'encode', 'render-main'])
        self.assertNotIn('render-other', result['value']['changed_ids'])
        self.assertEqual(self.repository.snapshot(), state)
        cyclic = deepcopy(graph)
        cyclic['edges'].append(dict(source='encode', target='render-main', artifact=first))
        request['input']['graph'] = self.publish(json.dumps(cyclic).encode(), kind='execution-graph-fixture')
        with self.assertRaises(RigError): self.service.execute(request, artifact_root=self.root)
        dependency = self.publish(b'{}', kind='engineering-dependency')
        missing = deepcopy(graph); missing['nodes'][0]['inputs'].append(dependency)
        request['input']['graph'] = self.publish(json.dumps(missing).encode(), kind='execution-graph-fixture')
        (self.root/dependency['path']).unlink()
        with self.assertRaises(RigError): self.service.execute(request, artifact_root=self.root)
        self.assertEqual(self.repository.snapshot(), state)

    def test_original_hash_pinned_brief_constraints_cannot_be_omitted_or_rebound(self):
        root = Path(__file__).resolve().parents[1]
        docs = json.loads((root/'examples/director-contracts/contracts.json').read_text())
        brief = deepcopy(next(d for d in docs if d['kind'] == 'director-creative-brief'))
        brief['project_id'] = self.repository.snapshot()['project']['id']
        brief['requirements']['locked'] = ['camera:camera']
        reference = self.publish(json.dumps(brief).encode(), kind=brief['kind'])
        reference.update(artifact_id=brief['id'], semantic_version=brief['semantic_version'])
        service = RevisionService(self.repository, brief_reference=reference, artifact_root=self.root)
        state = self.repository.snapshot()
        patch = service.plan('Set camera camera near to 0.2', expected_revision=0,
                             expected_sha256=state['sha256'], locked_constraints=[])['patch']
        self.assertIn('camera:camera', patch['locked_constraints'])
        with self.assertRaises(RigError): service.apply(patch, transaction_id='original-lock')
        omitted = deepcopy(patch); omitted['locked_constraints'] = []
        omitted['sha256'] = _hash({key: value for key, value in omitted.items() if key != 'sha256'})
        with self.assertRaises(RigError): service.apply(omitted, transaction_id='omit-original-lock')
        (self.root/reference['path']).write_bytes(b'{}')
        with self.assertRaises(RigError): service.apply(patch, transaction_id='tampered-original')
        self.assertEqual(self.repository.snapshot(), state)


if __name__ == '__main__':
    unittest.main()
