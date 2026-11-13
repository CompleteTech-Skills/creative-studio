"""Actual immutable payloads, CAS lifecycle and pinned shared-library regressions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest

from asset_library import AssetLibrary, file_record
from asset_library_contracts import seal
from asset_library_versions import AssetVersions, LegacyMigration, ScopedResolver, compare_manifests
from rig_errors import RigError

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.library = AssetLibrary(self.root / 'library', create=True)
        self.versions = AssetVersions(self.library)
        self.manifest = json.loads((ROOT / 'examples/asset-library-foundation/manifest.json').read_text())
        self.sources = {}
        for item in self.manifest['files']:
            target = self.root / item['path']; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / 'examples/asset-library-foundation' / item['path']).read_bytes())
            self.sources[item['path']] = target

    def initial(self):
        return self.versions.create(self.manifest, self.sources)

    def newer(self, previous, version='1.0.1', **changes):
        manifest = deepcopy(self.manifest); manifest['version'] = version
        manifest['metadata']['description'] = 'Corrected description'
        manifest['provenance']['parents'] = [previous]
        for key, value in changes.items(): manifest[key] = value
        return seal(manifest)

    def test_create_promote_compare_rollback_preserves_old_bytes(self):
        first = self.initial()
        saved = {name: path.read_bytes() for name, path in self.sources.items()}
        self.versions.promote(first, expected_revision=0)
        second = self.versions.create(self.newer(first), self.sources, previous=first)
        comparison = self.versions.compare(first, second)
        self.assertTrue(comparison['allowed']); self.assertEqual(comparison['actual_bump'], 'patch')
        plan = self.versions.promotion_plan(second)
        self.assertFalse(plan['grants_approval']); self.assertEqual(plan['previous'], first)
        self.versions.promote(second, expected_revision=1)
        self.assertEqual(self.versions.state('triangle')['promoted'], second)
        self.versions.rollback('triangle', expected_revision=2)
        self.assertEqual(self.versions.state('triangle')['promoted'], first)
        reopened = AssetVersions(AssetLibrary(self.library.root))
        self.assertEqual(reopened.state('triangle')['promoted'], first)
        for name, data in saved.items(): self.assertEqual(self.library.read_file(first, name), data)

    def test_duplicate_version_and_missing_parent_fail(self):
        first = self.initial()
        changed = deepcopy(self.manifest); changed['metadata']['description'] = 'Changed under old version'
        with self.assertRaises(RigError): self.versions.create(seal(changed), self.sources, previous=first)
        with self.assertRaises(RigError): self.versions.create(self.newer(first), self.sources)
        no_parent = self.newer(first); no_parent['provenance']['parents'] = []
        with self.assertRaisesRegex(RigError, 'parent'): self.versions.create(seal(no_parent), self.sources, previous=first)

    def test_breaking_actual_signature_requires_major_not_semver_label(self):
        first = self.initial()
        changed = self.newer(first, '1.0.1')
        changed['compatibility']['skeleton_signature'] = ['a' * 64]
        changed = seal(changed)
        self.assertEqual(compare_manifests(self.manifest, changed)['minimum_bump'], 'major')
        with self.assertRaisesRegex(RigError, 'increment'): self.versions.create(changed, self.sources, previous=first)
        changed['version'] = '2.0.0'
        self.versions.create(seal(changed), self.sources, previous=first)

    def test_added_capabilities_require_minor_and_lost_capabilities_major(self):
        first = self.initial(); change = self.newer(first)
        change['compatibility']['renderers'] = ['blender']
        with self.assertRaises(RigError): self.versions.create(seal(change), self.sources, previous=first)
        change['version'] = '1.1.0'; second = self.versions.create(seal(change), self.sources, previous=first)
        removal = deepcopy(change); removal['version'] = '1.2.0'; removal['compatibility']['renderers'] = []
        removal['provenance']['parents'] = [second]
        self.assertEqual(compare_manifests(seal(change), seal(removal))['minimum_bump'], 'major')

    def test_attribution_restrictions_cannot_silently_broaden(self):
        first = self.initial()
        for field, value in [('license', 'Unknown'), ('author', 'Other'), ('allowed_uses', ['test', 'redistribute'])]:
            with self.subTest(field=field):
                changed = self.newer(first); changed['metadata'][field] = value
                with self.assertRaises(RigError): self.versions.create(seal(changed), self.sources, previous=first)

    def test_promotion_rejects_same_name_incompatible_signature(self):
        first = self.initial()
        with self.assertRaisesRegex(RigError, 'requirements'):
            self.versions.promote(first, expected_revision=0, consumers=[dict(id='actor', requirements={'skeleton_signature': ['b' * 64]})])
        self.assertEqual(self.versions.state('triangle')['revision'], 0)
        result = self.versions.promote(first, expected_revision=0, consumers=[dict(id='actor', requirements={'formats': ['obj']})])
        self.assertEqual(result['revision'], 1)

    def test_malformed_scopes_and_consumers_fail_closed(self):
        first = self.initial()
        for libraries in [[('shared',)], [('shared', self.library, 'extra')]]:
            with self.assertRaises(RigError): ScopedResolver(libraries)
        for consumers in [False, {}, '']:
            with self.assertRaises(RigError): self.versions.promote(first, expected_revision=0, consumers=consumers)
        self.assertEqual(self.versions.state('triangle')['revision'], 0)

    def test_stale_revisions_and_concurrent_promotions_are_rejected(self):
        first = self.initial(); barrier = threading.Barrier(2); outcomes = []
        def writer():
            try:
                barrier.wait(); AssetVersions(AssetLibrary(self.library.root)).promote(first, expected_revision=0)
                outcomes.append('ok')
            except RigError: outcomes.append('conflict')
        threads = [threading.Thread(target=writer) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(outcomes, ['ok', 'conflict'])
        with self.assertRaises(RigError): self.versions.promote(first, expected_revision=0)

    def test_deprecate_never_removes_payload_or_selects_deprecated(self):
        first = self.initial(); self.versions.promote(first, expected_revision=0)
        with self.assertRaises(RigError): self.versions.deprecate(first, expected_revision=1, reason='Retire')
        second = self.versions.create(self.newer(first), self.sources, previous=first)
        self.versions.promote(second, expected_revision=1)
        self.versions.deprecate(first, expected_revision=2, reason='Retained historical version')
        self.assertEqual(self.library.resolve(first)['version'], '1.0.0')
        with self.assertRaises(RigError): self.versions.promote(first, expected_revision=3)
        with self.assertRaises(RigError): self.versions.rollback('triangle', expected_revision=3)

    def test_collections_and_existing_annotation_cas_leave_manifest_unchanged(self):
        first = self.initial(); before = self.library.resolve(first)
        self.versions.set_collections(first, expected_revision=0, collections=['favorites', 'laboratory'])
        self.library.update_annotations(first, expected_revision=0, tags=['robot'], favorite=True, note='Preserve source')
        self.assertEqual(self.versions.collections(first), dict(revision=1, collections=['favorites', 'laboratory']))
        with self.assertRaises(RigError): self.versions.set_collections(first, expected_revision=0, collections=[])
        self.assertEqual(self.library.resolve(first), before)

    def test_project_pins_survive_shared_latest_changes_and_relocation(self):
        first = self.initial(); self.versions.promote(first, expected_revision=0)
        local = AssetLibrary(self.root / 'project', create=True)
        resolver = ScopedResolver([('project', local), ('shared', self.library)], project_library='project')
        binding = resolver.select('triangle')
        resolver.bind('film', 'actor', binding, expected_revision=0)
        second = self.versions.create(self.newer(first), self.sources, previous=first)
        self.versions.promote(second, expected_revision=1)
        self.assertEqual(resolver.select('triangle')['reference'], second)
        self.assertEqual(resolver.bindings('film')['bindings']['actor']['reference'], first)
        moved = self.root / 'moved-library'; self.library.root.rename(moved)
        reopened = ScopedResolver([('project', local), ('shared', AssetLibrary(moved))], project_library='project')
        self.assertEqual(reopened.load(binding)['version'], '1.0.0')
        self.assertNotIn(str(self.root), json.dumps(reopened.bindings('film')))
        with self.assertRaises(RigError): reopened.bind('film', 'actor', binding, expected_revision=0)

    def test_scope_shadowing_and_missing_pinned_root_fail_closed(self):
        first = self.initial(); self.versions.promote(first, expected_revision=0)
        local = AssetLibrary(self.root / 'project', create=True)
        local.register(self.manifest, self.sources)
        resolver = ScopedResolver([('project', local), ('shared', self.library)])
        with self.assertRaises(RigError): resolver.select('triangle')
        with self.assertRaises(RigError): resolver.load(dict(library='missing', reference=first))

    def test_exact_scope_selection_does_not_fall_through_wrong_hash(self):
        first = self.initial(); wrong = {**first, 'sha256': 'f' * 64}
        with self.assertRaises(RigError): ScopedResolver([('shared', self.library)]).select('triangle', reference=wrong)

    def test_actual_legacy_profile_migration_preserves_bytes_and_native_identity(self):
        profile = self.root / 'profile.json'
        profile.write_bytes((ROOT / 'profiles/codex-v1.json').read_bytes())
        item = file_record(profile, 'source/profile.json', 'source')
        manifest = deepcopy(self.manifest); manifest['identity']['kind'] = 'legacy-pet'
        manifest['compatibility']['coordinate_system'] = 'legacy-2d'
        manifest['compatibility']['formats'] = ['json']; manifest['files'] = [item, self.manifest['files'][1]]
        manifest['provenance']['sources'] = [item]
        manifest['publication']['inventory'] = ['source/profile.json', 'source/license.txt']
        manifest = seal(manifest); sources = {'source/profile.json': profile, 'source/license.txt': self.sources['source/license.txt']}
        migration = LegacyMigration(self.library)
        plan = migration.preview(manifest, sources, native_path='source/profile.json')
        original = profile.read_bytes(); result = migration.apply(plan, sources)
        self.assertFalse(result['production_approved']); self.assertFalse(result['source_mutation'])
        self.assertEqual(profile.read_bytes(), original)
        self.assertEqual(self.library.read_file(result['reference'], 'source/profile.json'), original)
        self.assertEqual(json.loads(original)['id'], 'codex-v1')

    def test_migration_missing_changed_and_unknown_sources_fail_without_registration(self):
        migration = LegacyMigration(self.library)
        with self.assertRaises(RigError): migration.preview(self.manifest, self.sources, native_path='mesh/triangle.obj')
        profile = self.root / 'profile.json'; profile.write_bytes(b'{"schema_version":999}')
        manifest = deepcopy(self.manifest); manifest['identity']['kind'] = 'legacy-pet'
        record = file_record(profile, 'source/profile.json', 'source')
        manifest['files'][0] = record; manifest['provenance']['sources'] = [record]
        manifest['publication']['inventory'][0] = record['path']; manifest = seal(manifest)
        with self.assertRaises((RigError, ValueError)):
            migration.preview(manifest, {record['path']: profile, 'source/license.txt': self.sources['source/license.txt']}, native_path=record['path'])
        self.assertEqual(self.versions.history('triangle'), [])


if __name__ == '__main__': unittest.main()
