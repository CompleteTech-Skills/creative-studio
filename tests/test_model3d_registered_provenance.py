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
from rig_gltf import GltfBuilder, export_geometry, read_gltf
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
                            original_name='roundtrip.glb', deformation=None,
                            embedded_fixture=True, material_free=False):
        mesh = Mesh('triangle', np.asarray(positions, dtype=float),
                    np.asarray(triangles, dtype=int), name='Triangle')
        if deformation == 'morph':
            mesh.morphs = {'move': {'POSITION': np.asarray(((0., 0., 1.),) * 3)}}
            mesh.morph_defaults = {'move': 1.}
        geometry = Geometry([mesh], [{}])
        original = self.root / original_name
        export_geometry(geometry, original,
                        provenance={'fixture': True} if embedded_fixture else None)
        if material_free or deformation in ('animation', 'skin'):
            document = read_gltf(original)
            builder = GltfBuilder()
            builder.json = deepcopy(document.json)
            builder.binary = bytearray(document.buffers[0])
            if material_free:
                # The fixed Blender worker emits no authored material. The
                # generic Geometry exporter requires a default slot, so remove
                # only that synthetic slot and its binding before native packing.
                builder.json.pop('materials', None)
                for item in builder.json['meshes']:
                    for part in item['primitives']:
                        part.pop('material', None)
            if deformation == 'animation':
                time = builder.accessor([0., 1.], 'SCALAR', bounds=True)
                motion = builder.accessor([[0., 0., 0.], [1., 0., 0.]], 'VEC3')
                builder.json['animations'] = [dict(name='move',
                    samplers=[dict(input=time, output=motion, interpolation='LINEAR')],
                    channels=[dict(sampler=0, target=dict(node=0, path='translation'))])]
            elif deformation == 'skin':
                attributes = builder.json['meshes'][0]['primitives'][0]['attributes']
                attributes['JOINTS_0'] = builder.accessor([[0, 1, 0, 0]] * 3,
                                                          'VEC4', component=5121)
                attributes['WEIGHTS_0'] = builder.accessor([[1., 0., 0., 0.]] * 3, 'VEC4')
                binds = np.stack([np.eye(4), np.eye(4)])
                binds[1, 1, 3] = -1.
                builder.json['nodes'] += [dict(name='Root joint', children=[2]),
                                           dict(name='Tip joint', translation=[0., 1., 0.])]
                builder.json['nodes'][0]['skin'] = 0
                builder.json['scenes'][0]['nodes'].append(1)
                builder.json['skins'] = [dict(joints=[1, 2], skeleton=1,
                    inverseBindMatrices=builder.accessor(binds, 'MAT4'))]
            builder.write(original)
        ref = ImportPipeline(self.library).import_asset(
            original, identity=native_fixture.IDENTITY, version='1.0.0',
            metadata=native_fixture.METADATA, license_file=self.license,
            fixture=True, unit_scale=1)
        return ref, self.library.resolve(ref)

    def synthetic_blender_claim(self, positions=FIXED_TRIANGLE, triangles=((0, 1, 2),),
                                *, mutate_report=None, deformation=None):
        """Rebind a genuine packed native import to a synthetic Blender claim.

        Preserve producer packing, actual Khronos receipts, and resource evidence.
        Rehash every changed record; register through the real AssetLibrary API.
        Stage receipts are synthetic, internally consistent declarations: they
        are not process measurements or evidence that Blender ran.
        """
        ref, manifest = self.native_registration(positions, triangles, deformation=deformation,
                                               material_free=True)
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
        # Match the existing fixed producer's complete receipt shape. These
        # invented process values test consistency only, never authentication.
        from asset_import_blender import MAX_LOG_BYTES, WORKER
        from asset_library_contracts import MAX_FILE_BYTES
        limits = ImportPipeline(self.library).limits
        memory = limits['memory_bytes']
        byte_limit = min(MAX_FILE_BYTES, limits['source_bytes'], limits['dependency_bytes'])
        report['blender'].update(
            worker_sha256=hashlib.sha256(WORKER.read_bytes()).hexdigest(),
            shared_wall_timeout_seconds=limits['timeout_seconds'],
            command_policy='private-cwd/fixed-package-worker/no-shell/factory/autoexec-disabled')
        for stage in ('create', 'reopen'):
            report['blender'][stage] = dict(status='completed', exit_code=0,
                argv=['/usr/bin/prlimit', f'--as={memory * 8}',
                      f'--cpu={limits["timeout_seconds"]}', f'--fsize={byte_limit}',
                      '--nofile=128:128', '--', '<blender>', '--background',
                      '--factory-startup', '--disable-autoexec', '--threads', '2',
                      '--python-exit-code', '1', '--python', '<worker>', '--', stage],
                cwd='<workspace>', seconds=.05, peak_rss_bytes=64 * 1024**2,
                peak_rss_source='linux-wait4', resident_budget_bytes=memory,
                virtual_limit_bytes=memory * 8, log_limit_bytes=MAX_LOG_BYTES,
                log_sha256=hashlib.sha256(('synthetic ' + stage).encode()).hexdigest())
        if mutate_report is not None:
            mutate_report(report['blender'])
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


@unittest.skipUnless(shutil.which('node') and (native_fixture.VALIDATOR / 'package.json').is_file(),
                     'Requires the pinned offline Khronos glTF validator')
class RegisteredResourceAcquisitionTests(unittest.TestCase):
    """A native acquisition retains the primary name and every external resource name."""

    texture = 'source/resources/paint.png'
    buffer = 'source/resources/triangle.bin'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = AssetLibrary(self.root / 'library', create=True)
        self.library.upgrade_storage()
        path = native_fixture.NativeImportTests.external_source(self)
        self.primary = path.rename(path.with_name('Authored.gltf'))
        self.originals = {
            'source/model.gltf': self.primary,
            self.texture: self.root / 'resources/paint.png',
            self.buffer: self.root / 'resources/triangle.bin',
        }
        self.source_bytes = {name: file.read_bytes() for name, file in self.originals.items()}
        license_file = self.root / 'license.txt'
        license_file.write_text('Synthetic engineering fixture. CC0-1.0.', encoding='utf-8')
        self.ref = ImportPipeline(self.library).import_asset(
            self.primary, identity=native_fixture.IDENTITY, version='1.0.0',
            metadata=native_fixture.METADATA, license_file=license_file, fixture=True)
        self.manifest = self.library.resolve(self.ref)
        self.work = self.root / 'snapshot'
        self.paths = {}
        self.payloads = {}
        for item in self.manifest['files']:
            name = item['path']
            data = self.library.read_file(self.ref, name)
            self.assertEqual(hashlib.sha256(data).hexdigest(), item['sha256'])
            self.assertEqual(len(data), item['bytes'])
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            self.paths[name] = path
            self.payloads[name] = data
        self.versions = [(self.ref, deepcopy(self.manifest))]
        self.addCleanup(self.assert_retained_bytes)
        records = self.manifest['provenance']['source_records']
        self.assertEqual({r['file']['path']: r['original_filename'] for r in records},
                         {'source/model.gltf': 'Authored.gltf', self.texture: 'resources/paint.png',
                          self.buffer: 'resources/triangle.bin'})
        self.assertEqual({name: self.payloads[name] for name in self.originals}, self.source_bytes)
        report = json.loads(self.payloads['import-report.json'])
        self.assertEqual({r['path']: r['sha256'] for r in report['source_files']},
                         {name.removeprefix('source/'): hashlib.sha256(data).hexdigest()
                          for name, data in self.source_bytes.items()})
        self.assertEqual(report['source_sha256'], hashlib.sha256(self.primary.read_bytes()).hexdigest())
        self.assertEqual(report['normalized_sha256'], hashlib.sha256(self.payloads['normalized.glb']).hexdigest())

    def assert_retained_bytes(self):
        for name, path in self.originals.items():
            self.assertEqual(path.read_bytes(), self.source_bytes[name])
        for ref, manifest in self.versions:
            self.assertEqual(self.library.resolve(ref), manifest)
            for name, data in self.payloads.items():
                self.assertEqual(self.library.read_file(ref, name), data)
                self.assertEqual(self.paths[name].read_bytes(), data)

    def register_metadata_variant(self, resource, *, omit):
        manifest = deepcopy(self.manifest)
        records = manifest['provenance']['source_records']
        record = next(item for item in records if item['file']['path'] == resource)
        if omit:
            records.remove(record)
        else:
            record['original_filename'] = 'alternate' + Path(resource).suffix
        manifest['version'] = '1.0.1'
        manifest = seal(manifest)
        self.assertEqual(manifest['files'], self.manifest['files'])
        self.assertEqual(manifest['provenance']['conversions'], self.manifest['provenance']['conversions'])
        ref = self.library.register(manifest, self.paths)
        self.versions.append((ref, deepcopy(manifest)))
        return ref

    def build_from_reference(self, ref, *, texture_name):
        manifest = self.library.resolve(ref)
        canonical = self.work / 'canonical.json'
        canonical.write_text(json.dumps(dict(canonical_ref=ref, manifest=manifest)), encoding='utf-8')
        texture = record(self.work, self.texture, 'image/png')
        supplied = dict(source(texture), role='texture', original_filename=texture_name, dimensions_px=[2, 2])
        geometry = record(self.work, 'normalized.glb', 'model/gltf-binary')
        asset = build_asset(self.work, geometry, asset_id='resource-names', version='1.0.0',
            category='prop', sources=[supplied], provenance=provenance(), files=[geometry, texture],
            canonical_evidence=record(self.work, 'canonical.json', 'application/json'))
        output = self.work / 'asset.json'
        output.write_text(json.dumps(asset), encoding='utf-8')
        checked = validate_file(output, self.work)
        self.assertTrue(checked['payloads_verified'])
        self.assertTrue(checked['geometry_inspected'])
        return asset

    def test_native_external_resource_acquisitions_roundtrip(self):
        from asset_library_contracts import canonical_bytes

        with import_canonical(self.library, self.ref) as imported:
            self.assertEqual(imported.provenance['canonical_ref'], self.ref)
            self.assertEqual(imported.model.geometry.triangle_count, 1)
        asset = self.build_from_reference(self.ref, texture_name='resources/paint.png')
        self.assertEqual(asset['sources'][0]['original_filename'], 'resources/paint.png')
        records = self.manifest['provenance']['source_records']
        print('MODEL3D_NATIVE_RESOURCE_ACQUISITION ' + json.dumps(dict(
            source_sha256={name: hashlib.sha256(data).hexdigest() for name, data in self.source_bytes.items()},
            acquisition_records_sha256=hashlib.sha256(canonical_bytes(records)).hexdigest(),
            manifest_sha256=self.ref['sha256'],
            import_report_sha256=hashlib.sha256(self.payloads['import-report.json']).hexdigest(),
            normalized_sha256=asset['geometry']['sha256']), sort_keys=True))

    def test_intact_texture_acquisition_refuses_incorrect_caller_name(self):
        with self.assertRaises(RigError) as caught:
            self.build_from_reference(self.ref, texture_name='alternate.png')
        self.assertEqual(caught.exception.report['code'], 'MODEL3D_PROVENANCE')

    def assert_native_claim_refused(self, resource, *, omit):
        ref = self.register_metadata_variant(resource, omit=omit)
        with self.subTest(consumer='canonical-reader'):
            with self.assertRaises(RigError) as caught:
                with import_canonical(self.library, ref):
                    pass
            self.assertIn(caught.exception.report['code'], ('MODEL3D_NORMALIZATION', 'MODEL3D_PROVENANCE',
                                                           'MODEL3D_SOURCE', 'MODEL3D_REFERENCE'))
        # Reach the builder independently. A buffer is an inventoried dependency,
        # even when the only supplied Model3D source record describes the texture.
        with self.subTest(consumer='asset-builder'):
            name = 'alternate.png' if resource == self.texture else 'resources/paint.png'
            with self.assertRaises(RigError) as caught:
                self.build_from_reference(ref, texture_name=name)
            self.assertIn(caught.exception.report['code'], ('MODEL3D_NORMALIZATION', 'MODEL3D_PROVENANCE',
                                                           'MODEL3D_SOURCE', 'MODEL3D_REFERENCE'))

    def test_native_texture_missing_acquisition_refused(self):
        self.assert_native_claim_refused(self.texture, omit=True)

    def test_native_texture_contradictory_acquisition_refused(self):
        self.assert_native_claim_refused(self.texture, omit=False)

    def test_native_buffer_missing_acquisition_refused(self):
        self.assert_native_claim_refused(self.buffer, omit=True)

    def test_native_buffer_contradictory_acquisition_refused(self):
        self.assert_native_claim_refused(self.buffer, omit=False)


if __name__ == '__main__':
    unittest.main()
