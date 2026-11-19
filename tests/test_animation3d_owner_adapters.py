"""Native foreign owners, not authored registry rows, establish target presence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from animation3d_contracts import KINDS
from animation3d_registry import TargetRegistry
from rig_errors import RigError


class NativeOwnerAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = Path(__file__).resolve().parents[1]
        shutil.copytree(self.repository / 'examples/camera3d', self.root / 'camera')
        shutil.copytree(self.repository / 'examples/scene3d-contracts', self.root / 'scene')

    def reference(self, name):
        payload = (self.root / name).read_bytes()
        document = json.loads(payload)
        return dict(id=document['id'], version=document['version'], kind=document['kind'],
                    path=name, sha256=hashlib.sha256(payload).hexdigest())

    def registry(self, owner, source, target):
        entry = dict(owner=owner, source=source, target=target)
        registry = TargetRegistry(self.root, dict(schema_version=1, kind=KINDS['registry'],
            id='native-targets', version='1.0.0', entries=[entry]))
        return registry, entry

    def test_camera_optics_and_transform_properties_resolve_native_registry(self):
        source = self.reference('camera/registry.json')
        for prop in ('translation', 'rotation', 'focal_length', 'focus_distance', 'aperture'):
            with self.subTest(property=prop):
                target = dict(kind='camera', camera_id='camera', property=prop)
                registry, entry = self.registry('camera3d', source, target)
                self.assertEqual(registry.resolve(target), entry)
                resolved = registry.resolve(target)
                resolved['source']['id'] = 'mutated'
                self.assertEqual(registry.resolve(target), entry)

    def test_declared_camera_properties_without_native_representation_reject(self):
        source = self.reference('camera/registry.json')
        for prop in ('orthographic_size', 'exposure', 'field_of_view'):
            with self.subTest(property=prop):
                target = dict(kind='camera', camera_id='camera', property=prop)
                registry, _ = self.registry('camera3d', source, target)
                with self.assertRaises(RigError):
                    registry.resolve(target)

    def test_native_camera_target_id_and_owner_cannot_be_claimed_by_registry(self):
        source = self.reference('camera/registry.json')
        for owner, target in [('camera3d', dict(kind='camera', camera_id='absent', property='translation')),
                              ('scene3d', dict(kind='camera', camera_id='camera', property='translation'))]:
            registry, _ = self.registry(owner, source, target)
            with self.assertRaises(RigError):
                registry.resolve(target)

    def test_scene_transform_and_visibility_resolve_real_nodes(self):
        source = self.reference('scene/project.json')
        for prop in ('translation', 'rotation', 'scale', 'visibility'):
            with self.subTest(property=prop):
                target = dict(kind='node', node_id='root', property=prop)
                registry, entry = self.registry('scene3d', source, target)
                self.assertEqual(registry.resolve(target), entry)
        target = dict(kind='node', node_id='not-a-real-node', property='translation')
        registry, _ = self.registry('scene3d', source, target)
        with self.assertRaises(RigError):
            registry.resolve(target)

    def test_camera_transitive_optics_tamper_rejects_even_unchanged_registry(self):
        target = dict(kind='camera', camera_id='camera', property='translation')
        registry, _ = self.registry('camera3d', self.reference('camera/registry.json'), target)
        self.assertEqual(registry.resolve(target)['target'], target)
        optics = self.root / 'camera/optics.json'
        original = optics.read_bytes()
        optics.write_bytes(original + b'\n')
        with self.assertRaises(RigError):
            registry.resolve(target)
