"""Registered provenance acceptance boundaries over real offline GLB payloads.

The Blender-claim envelopes below are deliberately synthetic adversarial fixtures.
They exercise the consumer's verification of a registered claim, and do not claim
that Blender created their original bytes or ran their declared inspection.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from asset_library import AssetLibrary, file_record
from asset_library_contracts import seal
from asset_library_import import ImportPipeline
from model3d_mesh_asset import build_asset
from model3d_mesh_import import import_canonical
from model3d_validation import validate_file
from rig_errors import RigError
from rig_gltf import export_geometry
from rig_interop import validate_gltf
from rig_models import Geometry, Mesh
import test_rig_asset_library_import as native_fixture
from test_model3d_mesh_asset import record, provenance, source


# Independent expected glTF coordinates: Blender (x, y, z) -> (x, z, -y).
FIXED_TRIANGLE = ((0., 0., 0.), (1., 0., 0.), (0., 1., -2.))


@unittest.skipUnless(shutil.which('node') and (native_fixture.VALIDATOR / 'package.json').is_file(),
                     'Requires the pinned offline Khronos glTF validator')
class RegisteredProvenanceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = AssetLibrary(self.root / 'library', create=True)
        self.library.upgrade_storage()
        self.license = self.root / 'license.txt'
        self.license.write_text('Synthetic engineering fixture. CC0-1.0.\n', encoding='utf-8')

    def native_registration(self, positions=FIXED_TRIANGLE, triangles=((0, 1, 2),),
                            original_name='roundtrip.glb'):
        geometry = Geometry([Mesh('triangle', np.asarray(positions, dtype=float),
                                  np.asarray(triangles, dtype=int), name='Triangle')], [{}])
        original = self.root / original_name
        export_geometry(geometry, original, provenance={'fixture': True})
        ref = ImportPipeline(self.library).import_asset(
            original, identity=native_fixture.IDENTITY, version='1.0.0',
            metadata=native_fixture.METADATA, license_file=self.license,
            fixture=True, unit_scale=1)
        return ref, self.library.resolve(ref)

    def synthetic_blender_claim(self, positions=FIXED_TRIANGLE, triangles=((0, 1, 2),)):
        """Rebind a genuine packed native import to a synthetic Blender claim.

        Preserve producer packing, actual Khronos receipts, and resource evidence.
        Rehash every changed record; register through the real AssetLibrary API.
        The only false semantic assertion is that this is the fixed Blender mesh.
        """
        ref, manifest = self.native_registration(positions, triangles)
        staging = self.root / 'forged-envelope'
        staging.mkdir()
        paths = {}
        for item in manifest['files']:
            path = staging / item['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.library.read_file(ref, item['path']))
            paths[item['path']] = path
        original = staging / 'source/fixture.blend'
        original.write_bytes(b'BLENDER synthetic forged-envelope test original; never executed\n')
        paths['source/fixture.blend'] = original
        retained = file_record(original, 'source/fixture.blend', 'source')
        intermediate = file_record(paths['source/model.glb'], 'source/model.glb', 'evidence')
        inventory = {item['path']: item for item in manifest['files']}
        inventory[retained['path']] = retained
        inventory[intermediate['path']] = intermediate
        report = json.loads(paths['import-report.json'].read_text())
        report['blender'] = dict(
            inspection=dict(version='4.5.3', build='67807e1800cc', vertices=3,
                            triangles=1, unit_scale=1, external_dependencies=0),
            original_filename='fixture.blend', original_path=retained['path'],
            source_bytes=retained['bytes'], source_sha256=retained['sha256'],
            derivative_path=intermediate['path'], derivative_sha256=intermediate['sha256'])
        paths['import-report.json'].write_text(json.dumps(report), encoding='utf-8')
        report_record = file_record(paths['import-report.json'], 'import-report.json', 'evidence')
        inventory[report_record['path']] = report_record
        prov = manifest['provenance']
        source_evidence = deepcopy(prov['source_records'][0])
        source_evidence.update(file=retained, original_filename='fixture.blend',
                               source_uri='local:synthetic-forged-envelope')
        prov.update(origin='App-authored packed Blender triangle fixture',
                    sources=[retained], source_records=[source_evidence])
        prov['tools'].append(dict(name='Blender', version='4.5.3'))
        normalization = prov['conversions'][0]
        normalization.update(inputs=[intermediate],
                             outputs=[inventory['normalized.glb'], report_record])
        prov['conversions'] = [dict(tool='Blender', version='4.5.3',
            timestamp=normalization['timestamp'], operation='Reopen packed triangle and export GLB',
            inputs=[retained], outputs=[intermediate]), normalization]
        manifest['files'] = list(inventory.values())
        manifest['publication']['inventory'] = list(inventory)
        manifest['version'] = '1.1.0'
        manifest = seal(manifest)
        forged_ref = self.library.register(manifest, paths)
        self.assertEqual(self.library.resolve(forged_ref), manifest)
        for item in manifest['files']:
            payload = self.library.read_file(forged_ref, item['path'])
            self.assertEqual(len(payload), item['bytes'])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), item['sha256'])
        # Real independent validator execution, including the adversarial GLBs.
        for name in ('source/model.glb', 'normalized.glb'):
            checked = validate_gltf(paths[name])
            self.assertTrue(checked['ok'], checked)
            self.assertEqual(checked['validator'], '2.0.0-dev.3.10')
        return forged_ref

    def test_synthetic_blender_claim_accepts_genuine_fixed_triangle(self):
        ref = self.synthetic_blender_claim()
        with import_canonical(self.library, ref) as imported:
            self.assertEqual(imported.provenance['canonical_ref'], ref)
            mesh, = imported.model.geometry.meshes
            self.assertEqual(len(mesh.positions), 3)
            self.assertEqual(mesh.triangles.shape, (1, 3))
            corners = [tuple(mesh.positions[i]) for i in mesh.triangles[0]]
            expected = list(FIXED_TRIANGLE)
            self.assertIn(corners, [expected[i:] + expected[:i] for i in range(3)])

    def test_synthetic_blender_claim_rejects_contradictory_coordinates(self):
        ref = self.synthetic_blender_claim(((0, 0, 0), (2, 0, 0), (0, 1, -2)))
        with self.assertRaises(RigError):
            with import_canonical(self.library, ref):
                pass

    def test_synthetic_blender_claim_rejects_contradictory_winding(self):
        ref = self.synthetic_blender_claim(triangles=((0, 2, 1),))
        with self.assertRaises(RigError):
            with import_canonical(self.library, ref):
                pass

    def test_synthetic_blender_claim_rejects_contradictory_topology(self):
        ref = self.synthetic_blender_claim(FIXED_TRIANGLE + ((1, 1, -2),),
                                          ((0, 1, 2), (1, 3, 2)))
        with self.assertRaises(RigError):
            with import_canonical(self.library, ref):
                pass

    def build_registered_source(self, declared_name):
        genuine_name = 'Géométrie.glb'
        ref, manifest = self.native_registration(original_name=genuine_name)
        with import_canonical(self.library, ref) as imported:
            root = imported.root
            geometry = record(root, 'normalized.glb', 'model/gltf-binary')
            original = record(root, 'source/model.glb', 'model/gltf-binary')
            retained_source = source(original)
            retained_source['original_filename'] = declared_name
            registered = [item for item in manifest['provenance']['source_records']
                          if (item['file']['sha256'], item['file']['bytes']) ==
                          (original['sha256'], original['bytes'])]
            self.assertEqual(len(registered), 1)
            self.assertEqual(registered[0]['original_filename'], genuine_name)
            (root / 'canonical.json').write_text(json.dumps(
                dict(canonical_ref=ref, manifest=manifest)), encoding='utf-8')
            evidence = record(root, 'canonical.json', 'application/json')
            document = build_asset(root, geometry, asset_id='registered-triangle',
                version='1.0.0', category='prop', sources=[retained_source],
                provenance=provenance(), canonical_evidence=evidence)
            (root / 'asset.json').write_text(json.dumps(document), encoding='utf-8')
            validated = validate_file(root / 'asset.json', root)
            self.assertTrue(validated['payloads_verified'])
            self.assertTrue(validated['geometry_inspected'])
            return document

    def test_registered_original_filename_consistent_source_is_accepted(self):
        document = self.build_registered_source('Géométrie.glb')
        self.assertEqual(document['sources'][0]['original_filename'], 'Géométrie.glb')

    def test_registered_original_filename_cannot_be_contradicted_by_matching_bytes(self):
        try:
            document = self.build_registered_source('Contradictory.glb')
        except RigError as error:
            # A typed provenance/reference refusal or projection is acceptable.
            self.assertIn(error.report['code'], ('MODEL3D_PROVENANCE', 'MODEL3D_REFERENCE',
                                                  'MODEL3D_SOURCE'))
        else:
            self.assertEqual(document['sources'][0]['original_filename'], 'Géométrie.glb',
                             'Validated source retained a name contradicting its exact registered bytes')


if __name__ == '__main__':
    unittest.main()
