"""Real library closures, strict bytes, licensing boundaries and safe transports."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from asset_library import AssetLibrary
from asset_library_contracts import canonical_bytes, seal
from asset_library_dependencies import DependencyGraph, locator
from asset_library_packages import (INDEX, create_package, verify_package, inspect_package,
    restore_package, create_transport, verify_transport, restore_transport, create_archive,
    verify_package_archive, restore_package_archive)
from rig_errors import RigError, object_hash
import test_asset_library as fixture


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.AssetLibraryTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root; self.library = self.fixture.library
        self.graph = DependencyGraph({'local': self.library})
        self.mesh = self.fixture.register()
        doc = deepcopy(self.fixture.manifest)
        doc['identity']['id'] = 'character'; doc['identity']['slug'] = 'character'
        doc['identity']['name'] = 'Character'
        doc['dependencies'] = [dict(target=self.mesh, purpose='mesh', required=True,
            origin='authored', relationship='direct', capabilities=[])]
        self.character = self.library.register(seal(doc), self.fixture.sources)
        self.package = self.root / 'package'
        self.report = create_package(self.graph, [locator('local', self.character)], self.package,
            package_id='fixture-exact-closure', policy=dict(require_sources=True,
                require_license_evidence=True, intended_use='test'))

    def read_index(self, root=None):
        return json.loads(((root or self.package) / INDEX).read_text())

    def reseal(self, value, root=None):
        value['sha256'] = object_hash({k: v for k, v in value.items() if k != 'sha256'})
        ((root or self.package) / INDEX).write_bytes(canonical_bytes(value))

    def test_complete_dependency_restore_two_destinations_and_exact_identity(self):
        self.assertEqual(self.report['asset_count'], 2)
        self.assertTrue(self.report['verified']); self.assertFalse(self.report['production_ready'])
        self.assertFalse(self.report['public_distribution_authorized'])
        for name in ['elsewhere/one', 'elsewhere/two']:
            result = restore_package(self.package, self.root / name)
            library = AssetLibrary(result['library'])
            self.assertEqual(library.resolve(self.character), self.library.resolve(self.character))
            self.assertEqual(library.read_file(self.mesh, 'mesh/triangle.obj'), self.fixture.mesh.read_bytes())
            self.assertEqual(library.read_file(self.mesh, 'source/license.txt'), self.fixture.license.read_bytes())
        with self.assertRaises(ValueError):
            restore_package(self.package, self.root / 'elsewhere/one')

    def test_missing_corrupt_and_unrelated_substitutes_never_restore_until_exact_bytes(self):
        blob = self.package / 'blobs' / self.fixture.files[0]['sha256']; original = blob.read_bytes()
        for label in ['missing', 'corrupt']:
            if label == 'missing': blob.unlink()
            else: blob.write_bytes(b'X' * len(original))
            self.assertEqual(inspect_package(self.package)['state'], 'incomplete')
            target = self.root / label
            with self.assertRaises((RigError, FileNotFoundError)):
                restore_package(self.package, target)
            self.assertFalse(target.exists())
            blob.write_bytes(original)
        self.assertTrue(verify_package(self.package)['verified'])
        self.assertTrue(restore_package(self.package, self.root / 'repaired')['verified'])

    def test_manifest_only_missing_license_and_extra_member_block_completeness(self):
        root = self.root / 'manifest-only'; root.mkdir()
        shutil.copyfile(self.package / INDEX, root / INDEX)
        self.assertFalse(inspect_package(root)['verified'])
        license_blob = self.package / 'blobs' / self.fixture.files[1]['sha256']
        original = license_blob.read_bytes(); license_blob.unlink()
        self.assertFalse(inspect_package(self.package)['verified'])
        license_blob.write_bytes(original)
        (self.package / 'unlisted').write_bytes(b'not inventoried')
        with self.assertRaisesRegex(RigError, 'extra'):
            verify_package(self.package)

    def test_lock_asset_identity_and_required_blob_cannot_be_omitted_by_resealed_index(self):
        original = self.read_index()
        index = deepcopy(original)
        path = 'blobs/' + self.fixture.files[0]['sha256']
        del index['members'][path]; (self.package / path).unlink(); self.reseal(index)
        with self.assertRaisesRegex(RigError, 'Required asset file'):
            verify_package(self.package)
        (self.package / path).write_bytes(self.fixture.mesh.read_bytes()); self.reseal(original)
        index = deepcopy(original); index['assets'][0]['location']['reference']['sha256'] = 'f' * 64
        self.reseal(index)
        with self.assertRaises(RigError): verify_package(self.package)

    def test_license_use_and_review_are_separate_from_integrity(self):
        target = self.root / 'private'
        report = create_package(self.graph, [locator('local', self.mesh)], target,
            package_id='private-use', policy=dict(require_sources=True,
                require_license_evidence=True, intended_use='ungranted-public-use'))
        self.assertTrue(report['verified'])
        self.assertFalse(report['gates'][0]['declared_use_permitted'])
        self.assertEqual(report['gates'][0]['visual_review'], 'unreviewed')
        self.assertFalse(report['production_ready']); self.assertFalse(report['public_distribution_authorized'])
        doc = deepcopy(self.fixture.manifest)
        doc['identity']['id'] = 'no-evidence'; doc['identity']['slug'] = 'no-evidence'
        doc['provenance']['license_evidence'] = []
        with self.assertRaises(RigError):
            self.library.register(seal(doc), self.fixture.sources)

    def test_optional_declared_dependency_is_retained_for_canonical_restore(self):
        doc = deepcopy(self.library.resolve(self.character))
        doc['version'] = '2.0.0'; doc['dependencies'][0]['required'] = False
        ref = self.library.register(seal(doc), self.fixture.sources)
        package = self.root / 'optional'
        result = create_package(self.graph, [locator('local', ref)], package, package_id='optional-link')
        self.assertEqual(result['asset_count'], 2)
        restored = restore_package(package, self.root / 'optional-restored')
        self.assertEqual(AssetLibrary(restored['library']).resolve(ref), self.library.resolve(ref))

    def test_scene_project_context_and_all_package_types(self):
        context = self.root / 'scene.json'; context.write_text('{"fixture":true,"scene":"exact"}')
        for kind in ['asset', 'collection', 'prefab', 'scene', 'project', 'library']:
            with self.subTest(kind=kind):
                result = create_package(self.graph, [locator('local', self.character)], self.root / ('kind-' + kind),
                    package_id=kind, package_type=kind,
                    context_files={'scene.json': context} if kind in ['scene', 'project'] else {})
                self.assertTrue(result['verified'])
        with self.assertRaisesRegex(RigError, 'consumer documents'):
            create_package(self.graph, [locator('local', self.character)], self.root / 'no-context',
                package_id='invalid', package_type='scene')

    def test_multipart_missing_corrupt_reordered_and_assembled_hash_mismatch(self):
        bundle = self.root / 'transport'
        create_transport(self.package, bundle, part_size=32)
        self.assertTrue(verify_transport(bundle)['verified'])
        index_path = bundle / 'sources-index.json'
        index = json.loads(index_path.read_text()); original_index = index_path.read_bytes()
        part = bundle / index['parts'][0]['path']; content = part.read_bytes()
        for label in ['missing', 'corrupt']:
            if label == 'missing': part.unlink()
            else: part.write_bytes(b'Y' * len(content))
            with self.assertRaises((RigError, ValueError)):
                restore_transport(bundle, self.root / ('parts-' + label))
            self.assertFalse((self.root / ('parts-' + label)).exists())
            part.write_bytes(content)
        for label in ['order', 'whole-hash']:
            altered = deepcopy(index)
            record = next(f for f in altered['files'] if len(f['parts']) > 1)
            if label == 'order': record['parts'] = list(reversed(record['parts']))
            else: record['sha256'] = 'f' * 64
            index_path.write_text(json.dumps(altered))
            with self.assertRaises(ValueError): verify_transport(bundle)
            index_path.write_bytes(original_index)
        restored = restore_transport(bundle, self.root / 'parts-good')
        self.assertEqual(AssetLibrary(restored['library']).resolve(self.character), self.library.resolve(self.character))
        (bundle / 'unexpected').write_text('reject extra transport members')
        with self.assertRaisesRegex(RigError, 'extra'): verify_transport(bundle)

    def test_archive_roundtrip_and_exact_shared_archive_guard(self):
        archive = self.root / 'package.zip'; create_archive(self.package, archive)
        self.assertTrue(verify_package_archive(archive)['verified'])
        restored = restore_package_archive(archive, self.root / 'archive-restored')
        self.assertEqual(AssetLibrary(restored['library']).resolve(self.character), self.library.resolve(self.character))
        for label, name in [('escape', '../escape'), ('alias', './alias'), ('backslash', 'x\\y')]:
            bad = self.root / (label + '.zip')
            with zipfile.ZipFile(bad, 'w') as output:
                output.writestr(name, b'danger')
                output.writestr('archive-receipt.json', canonical_bytes(dict(schema_version=2,
                    kind='asset-library-package-transport', files={name: hashlib.sha256(b'danger').hexdigest()})))
            with self.assertRaises((ValueError, RigError)):
                restore_package_archive(bad, self.root / ('bad-' + label))
            self.assertFalse((self.root / ('bad-' + label)).exists())
        self.assertFalse((self.root / 'escape').exists())

    def test_independent_scene_scopes_allow_pinned_versions_without_weakening_graph(self):
        doc = deepcopy(self.fixture.manifest); doc['version'] = '2.0.0'
        new = self.library.register(seal(doc), self.fixture.sources)
        one, two = locator('local', self.mesh), locator('local', new)
        with self.assertRaisesRegex(RigError, 'VERSION_CONFLICT'):
            self.graph.lock([one, two])
        context = self.root / 'project.json'; context.write_text('{"fixture":true}')
        package = self.root / 'scoped'
        report = create_package(self.graph, [], package, package_id='independent-scenes',
            package_type='project', context_files={'project.json': context},
            scopes={'scene-a': [one], 'scene-b': [two]})
        self.assertEqual(report['scopes'], ['scene-a', 'scene-b'])
        restored = restore_package(package, self.root / 'scoped-restored')
        library = AssetLibrary(restored['library'])
        self.assertEqual(library.resolve(self.mesh)['version'], '1.0.0')
        self.assertEqual(library.resolve(new)['version'], '2.0.0')
        self.assertEqual((Path(restored['restored']) / 'context/project.json').read_bytes(), context.read_bytes())

    def test_scoped_identity_conflicts_reject_before_package_publication(self):
        other = AssetLibrary(self.root / 'other-library', create=True)
        conflicting = deepcopy(self.fixture.manifest)
        conflicting['metadata']['description'] = 'Conflicting bytes for same immutable ID/version'
        second = other.register(seal(conflicting), self.fixture.sources)
        graph = DependencyGraph({'local': self.library, 'other': other})
        with self.assertRaisesRegex(RigError, 'immutable ID/version'):
            create_package(graph, [], self.root / 'conflict', package_id='conflict',
                scopes={'a': [locator('local', self.mesh)], 'b': [locator('other', second)]})
        self.assertFalse((self.root / 'conflict').exists())
        conflicting['version'] = '2.0.0'; conflicting['identity']['library'] = 'different-library'
        # A separate source registry can contain this identity, but one restored
        # authority cannot change stable identity across versions of the same ID.
        third_library = AssetLibrary(self.root / 'third-library', create=True)
        third = third_library.register(seal(conflicting), self.fixture.sources)
        graph = DependencyGraph({'local': self.library, 'third': third_library})
        with self.assertRaisesRegex(RigError, 'stable identity'):
            create_package(graph, [], self.root / 'identity-conflict', package_id='conflict',
                scopes={'a': [locator('local', self.mesh)], 'b': [locator('third', third)]})

    def test_package_symlinks_hardlinks_case_alias_and_oversize_rejected(self):
        blob = self.package / 'blobs' / self.fixture.files[0]['sha256']
        original = blob.read_bytes(); blob.unlink(); blob.symlink_to(self.fixture.mesh)
        with self.assertRaises((RigError, ValueError)): verify_package(self.package)
        blob.unlink(); blob.hardlink_to(self.fixture.mesh)
        with self.assertRaisesRegex(RigError, 'hardlinks'): verify_package(self.package)
        blob.unlink(); blob.write_bytes(original)
        index = self.read_index(); key = 'blobs/' + self.fixture.files[0]['sha256']
        index['members'][key]['bytes'] = 64 * 1024 * 1024 + 1; self.reseal(index)
        with self.assertRaisesRegex(RigError, 'bounds'): verify_package(self.package)


if __name__ == '__main__': unittest.main()
