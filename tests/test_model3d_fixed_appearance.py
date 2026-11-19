"""Independent fixed-worker appearance regression over real packed glTF.

Adversarial envelopes use the existing *synthetic* Blender-claim fixture. Their
GLBs, Khronos validation, immutable registration and native decoding are real;
their process receipts are invented and do not authenticate a Blender run.
The separate pinned-Blender control executes the producer and reopen stages.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import numpy as np

from asset_library_import import ImportPipeline
from model3d_mesh_import import import_canonical
from rig_errors import RigError
from rig_gltf import GltfBuilder, read_gltf
import test_model3d_registered_provenance as fixtures


# Independently derived from the fixed triangle's oriented cross product in
# glTF coordinates: (1, 0, 0) cross (0, 1, -2) = (0, 2, 1).
NORMAL = np.tile(np.asarray((0., 2., 1.)) / np.sqrt(5.), (3, 1))
RED = np.tile((1., 0., 0., 1.), (3, 1))


@unittest.skipUnless(
    shutil.which('node') and (fixtures.native_fixture.VALIDATOR / 'package.json').is_file(),
    'Requires the pinned offline Khronos glTF validator',
)
class FixedAppearanceTests(unittest.TestCase):
    def setUp(self):
        # Compose the existing fixture rather than inherit its test methods.
        self.fixture = fixtures.RegisteredProvenanceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def register_attributes(self, attributes, *, blender_claim):
        """Change genuine glTF bytes before validation, hashing and registration."""
        original_export = fixtures.export_geometry

        def export(geometry, path, **kwargs):
            original_export(geometry, path, **kwargs)
            document = read_gltf(path)
            builder = GltfBuilder()
            builder.json = deepcopy(document.json)
            builder.binary = bytearray(document.buffers[0])
            primitive = builder.json['meshes'][0]['primitives'][0]
            for name, values in attributes.items():
                values = np.asarray(values)
                primitive['attributes'][name] = builder.accessor(
                    values, 'VEC' + str(values.shape[1]), target=34962)
            builder.write(path)

        with patch.object(fixtures, 'export_geometry', export):
            if blender_claim:
                ref = self.fixture.synthetic_blender_claim()
            else:
                ref, _ = self.fixture.native_registration()

        # Check the mutation survived native normalization and exact-byte storage
        # before asking the consumer to reject anything. Registration/validation
        # errors must never masquerade as a successful negative import test.
        packed = self.fixture.root / 'registered-normalized.glb'
        packed.write_bytes(self.fixture.library.read_file(ref, 'normalized.glb'))
        document = read_gltf(packed)
        primitive = document.json['meshes'][0]['primitives'][0]
        np.testing.assert_allclose(document.accessor(primitive['attributes']['POSITION']),
                                   fixtures.FIXED_TRIANGLE, atol=1e-6, rtol=0)
        np.testing.assert_array_equal(document.accessor(primitive['indices']).ravel(), [0, 1, 2])
        for name, values in attributes.items():
            np.testing.assert_allclose(document.accessor(primitive['attributes'][name]),
                                       values, atol=1e-6, rtol=0)
        return ref

    def assert_fixed_claim_refused(self, attributes):
        ref = self.register_attributes(attributes, blender_claim=True)
        with self.assertRaises(RigError):
            with import_canonical(self.fixture.library, ref):
                pass

    def test_fixed_claim_rejects_reversed_normals(self):
        self.assert_fixed_claim_refused({'NORMAL': -NORMAL})

    def test_fixed_claim_rejects_one_corrupted_normal(self):
        corrupted = NORMAL.copy()
        corrupted[1] = (1., 0., 0.)  # Still finite and unit length; wrong appearance.
        self.assert_fixed_claim_refused({'NORMAL': corrupted})

    def test_fixed_claim_rejects_red_vertex_colors(self):
        self.assert_fixed_claim_refused({'COLOR_0': RED})

    def test_fixed_claim_rejects_texture_coordinates(self):
        # Valid appearance data remains outside this closed worker profile even
        # when no material collection happens to reference it.
        uv = np.asarray(((0., 0.), (1., 0.), (0., 1.)))
        self.assert_fixed_claim_refused({'TEXCOORD_0': uv})

    def test_fixed_claim_rejects_tangents(self):
        self.assert_fixed_claim_refused({'TANGENT': np.tile((1., 0., 0., 1.), (3, 1))})

    def test_fixed_claim_accepts_geometric_normals(self):
        ref = self.register_attributes({'NORMAL': NORMAL}, blender_claim=True)
        with import_canonical(self.fixture.library, ref) as imported:
            mesh, = imported.model.geometry.meshes
            np.testing.assert_allclose(mesh.attributes['NORMAL'], NORMAL, atol=1e-6, rtol=0)

    def test_native_gltf_preserves_authored_color_and_normals(self):
        # Authored glTF can intentionally use different shading. Applying the
        # fixed-worker restriction to generic native inputs would lose data.
        authored = {'COLOR_0': RED, 'NORMAL': -NORMAL}
        ref = self.register_attributes(authored, blender_claim=False)
        with import_canonical(self.fixture.library, ref) as imported:
            self.assertEqual(imported.provenance['canonical_ref'], ref)
            mesh, = imported.model.geometry.meshes
            for name, expected in authored.items():
                np.testing.assert_allclose(mesh.attributes[name], expected, atol=1e-6, rtol=0)

    @unittest.skipUnless(
        os.environ.get('HATCH_BLENDER') and hasattr(os, 'wait4')
        and Path('/usr/bin/prlimit').is_file(),
        'Requires pinned Blender 4.5.3 on Linux with prlimit',
    )
    def test_real_pinned_blender_profile_is_accepted(self):
        # No synthetic process receipt or mocked producer in this control.
        ref = ImportPipeline(self.fixture.library).import_blender_fixture(
            identity=fixtures.native_fixture.IDENTITY, version='1.0.0',
            metadata=fixtures.native_fixture.METADATA, license_file=self.fixture.license)
        with import_canonical(self.fixture.library, ref) as imported:
            report = json.loads((imported.root / 'import-report.json').read_bytes())
            self.assertEqual(report['blender']['inspection']['version'], '4.5.3')
            self.assertEqual(report['blender']['inspection']['build'], '67807e1800cc')
            for stage in ('create', 'reopen'):
                self.assertEqual(report['blender'][stage]['status'], 'completed')
                self.assertEqual(report['blender'][stage]['exit_code'], 0)
            document = read_gltf(imported.root / 'source/model.glb')
            primitive, = document.json['meshes'][0]['primitives']
            self.assertEqual(set(primitive['attributes']), {'POSITION', 'NORMAL'})
            mesh, = imported.model.geometry.meshes
            np.testing.assert_allclose(mesh.positions, fixtures.FIXED_TRIANGLE, atol=1e-6, rtol=0)
            np.testing.assert_array_equal(mesh.triangles, [[0, 1, 2]])
            np.testing.assert_allclose(mesh.attributes['NORMAL'], NORMAL, atol=1e-6, rtol=0)
            files = {}
            for name in ('source/fixture.blend', 'source/model.glb', 'normalized.glb'):
                payload = self.fixture.library.read_file(ref, name)
                files[name] = dict(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
            # Preserve measured versions, bounds and hashes in the test log.
            print('MODEL3D_FIXED_APPEARANCE_NATIVE ' + json.dumps(
                dict(ref=ref, files=files, blender=report['blender']), sort_keys=True))


if __name__ == '__main__':
    unittest.main()
