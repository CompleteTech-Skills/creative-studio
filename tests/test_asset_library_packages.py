"""Real library closures, strict bytes, licensing boundaries and safe transports."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import asset_library_packages as packages

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
            diagnostic = inspect_package(self.package)
            self.assertEqual(diagnostic['state'], 'incomplete')
            row = next(d for d in diagnostic['diagnostics'] if d['path'] == blob.relative_to(self.package).as_posix())
            self.assertEqual(row['expected']['sha256'], self.fixture.files[0]['sha256'])
            self.assertEqual(row['code'], 'MISSING_MEMBER' if label == 'missing' else 'CORRUPT_MEMBER')
            self.assertIn(locator('local', self.mesh), row['affected_assets'])
            self.assertIn(locator('local', self.character), row['affected_assets'])
            self.assertEqual(row['logical_files'][0]['path'], 'mesh/triangle.obj')
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

    def test_extra_empty_directories_are_rejected(self):
        (self.package / 'unlisted-directory').mkdir()
        with self.assertRaisesRegex(RigError, 'empty directory'):
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

    def test_resealed_lock_cannot_omit_optional_canonical_dependencies(self):
        doc = deepcopy(self.library.resolve(self.character)); doc['version'] = '2.0.0'
        doc['dependencies'][0]['required'] = False
        ref = self.library.register(seal(doc), self.fixture.sources)
        package = self.root / 'optional-forged'
        create_package(self.graph, [locator('local', ref)], package, package_id='optional-forged')
        index = self.read_index(package)
        lock_path = index['locks']['default']
        raw = canonical_bytes(self.graph.lock([locator('local', ref)], include_optional=False))
        (package / lock_path).write_bytes(raw)
        index['members'][lock_path] = dict(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        removed = next(asset for asset in index['assets'] if asset['location']['reference'] == self.mesh)
        index['assets'].remove(removed)
        del index['members'][removed['manifest']]; (package / removed['manifest']).unlink()
        self.reseal(index, package)
        with self.assertRaisesRegex(RigError, 'include optional'):
            verify_package(package)
        self.assertFalse(inspect_package(package)['verified'])

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

    def test_transport_rechecks_inner_package_before_publication(self):
        original = packages.create_source_bundle
        blob = self.package / 'blobs' / self.fixture.files[0]['sha256']
        def changed_source(*args, **kwargs):
            blob.write_bytes(b'X' * len(blob.read_bytes()))
            return original(*args, **kwargs)
        destination = self.root / 'raced-transport'
        with patch.object(packages, 'create_source_bundle', changed_source):
            with self.assertRaises(RigError): create_transport(self.package, destination)
        self.assertFalse(destination.exists())

    def test_archive_rechecks_package_identity_before_publication(self):
        original = packages._read; count = 0
        def changed_index(root, relative, limit=packages.MAX_FILE_BYTES):
            nonlocal count
            if Path(root) == self.package and relative == INDEX:
                count += 1
                if count == 2:
                    index = self.read_index(); index['package_id'] = 'changed-after-verification'
                    self.reseal(index)
            return original(root, relative, limit)
        destination = self.root / 'raced-archive.zip'
        with patch.object(packages, '_read', changed_index):
            with self.assertRaisesRegex(RigError, 'changed'): create_archive(self.package, destination)
        self.assertFalse(destination.exists())

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



class StructuredPackageErrorTests(unittest.TestCase):
    """Malformed but self-consistent packages fail with LIBRARY_PACKAGE_* codes."""
    setUp = PackageTests.setUp
    read_index = PackageTests.read_index
    reseal = PackageTests.reseal

    def replace_member(self, path, payload, index=None):
        index = deepcopy(self.read_index() if index is None else index)
        (self.package / path).write_bytes(payload)
        index['members'][path] = dict(bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
        self.reseal(index)

    def refused(self, call, code):
        with self.assertRaises(RigError) as caught:
            call()
        self.assertEqual(caught.exception.report['code'], 'LIBRARY_PACKAGE_' + code)

    def test_malformed_lock_is_structured_and_inspection_survives(self):
        self.replace_member('locks/default.json', b'[]')
        self.refused(lambda: verify_package(self.package), 'SCHEMA')
        self.refused(lambda: restore_package(self.package, self.root / 'restored'), 'SCHEMA')
        self.assertFalse((self.root / 'restored').exists())
        report = inspect_package(self.package)
        self.assertFalse(report['verified'])

    def test_asset_location_must_be_the_exact_canonical_locator(self):
        original = self.read_index()
        for change in ('extra-key', 'extra-reference-key', 'string'):
            with self.subTest(change=change):
                index = deepcopy(original); node = index['assets'][0]['location']
                if change == 'extra-key':
                    node['unbound'] = 'attacker'
                elif change == 'extra-reference-key':
                    node['reference']['unbound'] = 'attacker'
                else:
                    index['assets'][0]['location'] = 'x'
                self.reseal(index)
                self.refused(lambda: verify_package(self.package), 'SCHEMA')
                self.refused(lambda: restore_package(self.package, self.root / ('restored-' + change)), 'SCHEMA')
                self.assertFalse((self.root / ('restored-' + change)).exists())
        self.reseal(original)
        self.assertTrue(verify_package(self.package)['verified'])

    def test_nonstring_context_entry_is_structured(self):
        index = deepcopy(self.read_index()); index['context'] = [{}]; self.reseal(index)
        self.refused(lambda: verify_package(self.package), 'CONTEXT')

    def test_resealed_index_guards_reject_with_their_codes(self):
        original = self.read_index()
        edited = deepcopy(original); edited['package_id'] = 'edited'
        (self.package / INDEX).write_bytes(canonical_bytes(edited))
        self.refused(lambda: verify_package(self.package), 'HASH')
        self.reseal(original)
        extra = b'unreferenced'
        (self.package / 'context').mkdir(exist_ok=True)
        index = deepcopy(original)
        index['members']['context/extra.json'] = dict(bytes=len(extra), sha256=hashlib.sha256(extra).hexdigest())
        (self.package / 'context/extra.json').write_bytes(extra); self.reseal(index)
        self.refused(lambda: verify_package(self.package), 'INVENTORY')

    def test_directory_symlink_member_is_refused(self):
        outside = self.root / 'outside'; outside.mkdir()
        (self.package / 'linked').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(RigError):
            verify_package(self.package)

    def test_existing_destination_is_structured(self):
        (self.root / 'taken').mkdir()
        self.refused(lambda: restore_package(self.package, self.root / 'taken'), 'DESTINATION')

    def test_aliased_scope_names_are_structured(self):
        self.refused(lambda: create_package(self.graph, [], self.root / 'aliased', package_id='aliased',
                     scopes={'A': [locator('local', self.character)], 'a': [locator('local', self.mesh)]}), 'PATH')

    def test_malformed_transport_index_is_structured(self):
        bundle = self.root / 'bundle'; create_transport(self.package, bundle, part_size=4096)
        (bundle / 'sources-index.json').write_bytes(b'[]')
        self.refused(lambda: verify_transport(bundle), 'SCHEMA')

    def test_malformed_archives_are_structured(self):
        broken = self.root / 'broken.zip'; broken.write_bytes(b'not a zip')
        self.refused(lambda: verify_package_archive(broken), 'ARCHIVE')
        self.refused(lambda: restore_package_archive(broken, self.root / 'from-broken'), 'ARCHIVE')
        self.assertFalse((self.root / 'from-broken').exists())
        empty = self.root / 'empty.zip'
        with zipfile.ZipFile(empty, 'w') as archive:
            archive.writestr('payload.txt', b'x')
        self.refused(lambda: verify_package_archive(empty), 'ARCHIVE')

    def archive_members(self):
        path = self.root / 'package.zip'; create_archive(self.package, path)
        with zipfile.ZipFile(path) as source:
            members = {info.filename: source.read(info) for info in source.infolist()}
        receipt = json.loads(members.pop('archive-receipt.json'))
        return members, receipt

    def write_archive(self, name, members, receipt, compression=zipfile.ZIP_STORED):
        path = self.root / name
        with zipfile.ZipFile(path, 'w') as target:
            target.writestr('archive-receipt.json', json.dumps(receipt))
            for member, data in members.items():
                target.writestr(member, data, compress_type=compression)
        return path

    def test_compressed_archive_members_are_refused_before_decompression(self):
        members, receipt = self.archive_members()
        for compression in (zipfile.ZIP_DEFLATED, zipfile.ZIP_LZMA):
            with self.subTest(compression=compression):
                path = self.write_archive('compressed-%d.zip' % compression, members, receipt, compression)
                destination = self.root / ('compressed-%d-out' % compression)
                self.refused(lambda: restore_package_archive(path, destination), 'ARCHIVE')
                self.assertFalse(destination.exists())
                # Corrupt the first member's compressed stream: without the
                # stored-only rule it escapes as zlib.error / LZMAError.
                path = self.write_archive('corrupt-%d.zip' % compression, members, receipt, compression)
                raw = bytearray(path.read_bytes())
                with zipfile.ZipFile(path) as archive:
                    info = archive.infolist()[1]
                start = info.header_offset + 30 + len(info.filename.encode()) + 10
                raw[start:start + 3] = b'\xff\xff\xff'; path.write_bytes(bytes(raw))
                self.refused(lambda: verify_package_archive(path), 'ARCHIVE')

    def test_receipt_listed_case_aliases_are_path_errors(self):
        members, receipt = self.archive_members()
        for name, data in (('ALIAS.txt', b'a'), ('alias.txt', b'b')):
            members[name] = data; receipt['files'][name] = hashlib.sha256(data).hexdigest()
        path = self.write_archive('aliased.zip', members, receipt)
        self.refused(lambda: restore_package_archive(path, self.root / 'aliased-out'), 'PATH')
        self.assertFalse((self.root / 'aliased-out').exists())

if __name__ == '__main__': unittest.main()
