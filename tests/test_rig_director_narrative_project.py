"""Actual project-source preservation and bounded narrative author admission.

The source fixture uses real native geometry, rigs and two-pose clips. None of
these tests generates the 1,442 full narrative poses or starts Blender/rendering.
"""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import director_narrative_project as narrative
from director_scene_binding import bind_scene_project
from rig_errors import RigError, sha256
from scene3d_contracts import canonical_bytes, load_project
from scene3d_example import build_example
from test_rig_director_scene_binding import fixture


def inventory(root):
    return {p.relative_to(root).as_posix(): sha256(p)
            for p in sorted(root.rglob('*')) if p.is_file()}


class NarrativeStageAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def test_nonempty_stage_rejects_before_source_read_and_preserves_owned_file(self):
        stage = self.root/'occupied'
        stage.mkdir()
        original = b'An unrelated user-owned file must remain exact.\n'
        (stage/'mine.txt').write_bytes(original)
        with self.assertRaises(RigError) as error:
            narrative.populate_narrative(self.root/'absent-source', stage, motions={})
        self.assertEqual(error.exception.report['code'], 'DIRECTOR_NARRATIVE_STAGE')
        self.assertEqual(inventory(stage), {'mine.txt': sha256(stage/'mine.txt')})
        self.assertEqual((stage/'mine.txt').read_bytes(), original)

    def test_symlink_stage_never_writes_through_to_target(self):
        target = self.root/'unrelated'
        target.mkdir()
        linked = self.root/'linked-stage'
        try:
            linked.symlink_to(target, target_is_directory=True)
        except (NotImplementedError, PermissionError) as error:
            self.skipTest('Host cannot create a directory symlink: '+type(error).__name__)
        with self.assertRaises(RigError) as error:
            narrative.populate_narrative(self.root/'absent-source', linked, motions={})
        self.assertEqual(error.exception.report['code'], 'DIRECTOR_NARRATIVE_STAGE')
        self.assertTrue(linked.is_symlink())
        self.assertEqual(list(target.iterdir()), [])

    def test_missing_or_regular_file_stage_is_not_implicitly_created_or_replaced(self):
        missing = self.root/'not-created'
        file_stage = self.root/'ordinary-file'
        file_stage.write_bytes(b'preserve')
        for stage in (missing, file_stage):
            with self.subTest(stage=stage.name), self.assertRaises(RigError) as error:
                narrative.populate_narrative(self.root/'absent-source', stage, motions={})
            self.assertEqual(error.exception.report['code'], 'DIRECTOR_NARRATIVE_STAGE')
        self.assertFalse(missing.exists())
        self.assertEqual(file_stage.read_bytes(), b'preserve')

    def test_valid_scene_without_the_two_required_native_actors_is_not_reinterpreted(self):
        source = self.root/'unrelated-scene'
        build_example(source)
        before = inventory(source)
        stage = self.root/'build'
        stage.mkdir()
        with self.assertRaises(RigError) as error:
            narrative.populate_narrative(source, stage)
        self.assertEqual(error.exception.report['code'], 'DIRECTOR_NARRATIVE_IDENTITY')
        self.assertEqual(inventory(source), before)
        self.assertEqual(list(stage.iterdir()), [])

    def test_malformed_source_project_never_creates_an_authored_snapshot(self):
        for index, payload in enumerate((b'{}', b'{"id":"one","id":"two"}')):
            with self.subTest(payload=payload):
                source = self.root/('invalid-source-'+str(index))
                source.mkdir()
                (source/'project.json').write_bytes(payload)
                stage = self.root/('private-'+str(index))
                stage.mkdir()
                with self.assertRaises(RigError):
                    narrative.populate_narrative(source, stage)
                self.assertEqual((source/'project.json').read_bytes(), payload)
                self.assertEqual(list(stage.iterdir()), [])


class NativeNarrativeProjectBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        performance = cls.root/'performance'
        performance.mkdir()
        frames = fixture(performance)
        cls.source = cls.root/'source'
        bind_scene_project(performance, cls.source, frames)
        cls.before = inventory(cls.source)
        cls.bundle = load_project(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def build_stage(self):
        directory = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    def assert_original_unchanged(self):
        self.assertEqual(inventory(self.source), self.before)

    def test_missing_character_in_actual_source_is_rejected_before_copying(self):
        copied = self.build_stage()/'source'
        shutil.copytree(self.source, copied)
        project = deepcopy(self.bundle['project'])
        project['characters'] = [r for r in project['characters'] if r['id'] == 'aster']
        (copied/'project.json').write_bytes(canonical_bytes(project))
        load_project(copied)  # A valid generic project may list one character.
        before = inventory(copied)
        stage = self.build_stage()
        with self.assertRaises(RigError) as error:
            narrative.populate_narrative(copied, stage)
        self.assertEqual(error.exception.report['code'], 'DIRECTOR_NARRATIVE_IDENTITY')
        self.assertEqual(list(stage.iterdir()), [])
        self.assertEqual(inventory(copied), before)
        self.assert_original_unchanged()

    def test_malformed_motion_containers_and_actor_sets_fail_with_typed_diagnostics(self):
        cases = [[], True, 'opaque', {}, {'aster': {}},
                 {'aster': {}, 'moss': {}, 'third-robot': {}},
                 {'aster': {}, 'third-robot': {}},
                 {'aster': None, 'moss': {}},
                 {'aster': {'clips': []}, 'moss': {'clips': {}}}]
        for motions in cases:
            with self.subTest(motions=motions):
                stage = self.build_stage()
                with self.assertRaises(RigError):
                    narrative.populate_narrative(self.source, stage, motions=motions)
                self.assertEqual(list(stage.iterdir()), [])
                self.assert_original_unchanged()

    def test_incomplete_native_clip_set_never_publishes_or_mutates_source(self):
        stage = self.build_stage()
        motions = {actor: {'clips': {}} for actor in ('aster', 'moss')}
        with self.assertRaises(RigError) as error:
            narrative.populate_narrative(self.source, stage, motions=motions)
        self.assertEqual(error.exception.report['code'], 'DIRECTOR_NARRATIVE_CLIPS')
        self.assertEqual(list(stage.iterdir()), [])
        self.assert_original_unchanged()

    def test_malformed_native_clip_records_reject_before_copying(self):
        names = {'entry', 'exploration', 'discovery', 'cooperation', 'resolution', 'full-narrative'}
        stage = self.build_stage()
        motions = {actor: {'clips': dict.fromkeys(names, None)} for actor in ('aster', 'moss')}
        with self.assertRaises(RigError):
            narrative.populate_narrative(self.source, stage, motions=motions)
        self.assertEqual(list(stage.iterdir()), [])
        self.assert_original_unchanged()

    def test_invalid_native_clip_documents_use_native_owner_rejection(self):
        names = {'entry', 'exploration', 'discovery', 'cooperation', 'resolution', 'full-narrative'}
        stage = self.build_stage()
        motions = {actor: {'clips': {name: {} for name in names}}
                   for actor in ('aster', 'moss')}
        with self.assertRaises(RigError):
            narrative.populate_narrative(self.source, stage, motions=motions)
        self.assertFalse((stage/'project.json').exists())
        self.assertEqual(load_project(stage/'source-original')['inventory'], self.bundle['inventory'])
        self.assert_original_unchanged()


class NarrativePublicationDependencyTests(unittest.TestCase):
    def test_unverified_publication_profiles_fail_before_private_authoring(self):
        for platform, supported in [('linux', False), ('darwin', True)]:
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                stage = root/'private'
                stage.mkdir()
                with patch.object(narrative.sys, 'platform', platform), \
                     patch('atomic_publish.private_stage_supported', return_value=supported):
                    with self.assertRaises(RigError) as caught:
                        narrative.build_narrative(root/'absent-source', root/'delivery', stage=stage)
                self.assertEqual(caught.exception.report['code'], 'DIRECTOR_NARRATIVE_DEPENDENCY')
                self.assertEqual(list(stage.iterdir()), [])
                self.assertFalse((root/'delivery').exists())

    def test_retained_native_motion_input_is_checked_before_source_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            stage = root/'private'
            stage.mkdir()
            with patch.object(narrative.sys, 'platform', 'linux'), \
                 patch('atomic_publish.private_stage_supported', return_value=True):
                with self.assertRaises(RigError) as caught:
                    narrative.build_narrative(root/'absent-source', root/'delivery', stage=stage, motions=[])
            self.assertEqual(caught.exception.report['code'], 'DIRECTOR_NARRATIVE_CLIPS')
            self.assertEqual(list(stage.iterdir()), [])
            self.assertFalse((root/'delivery').exists())

    def test_missing_capture_hash_guard_fails_before_private_authoring(self):
        import scene3d_snapshot
        # This old API shape deliberately lacks the canonical expected hash.
        # It must never execute or be replaced with another publication engine.
        def old_snapshot(source_root, destination):
            raise AssertionError('Missing dependency must be rejected before dispatch.')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            stage = root/'private'
            stage.mkdir()
            destination = root/'delivery'
            with patch.object(scene3d_snapshot, 'snapshot_project', old_snapshot):
                with self.assertRaises(RigError):
                    narrative.build_narrative(root/'absent-source', destination, stage=stage)
            self.assertEqual(list(stage.iterdir()), [])
            self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
