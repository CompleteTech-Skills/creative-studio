"""Exact payloads and revision-checked lifecycle with canonical storage."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from asset_library import INDEX_SCHEMA, AssetLibrary, file_record
from asset_library_contracts import seal
from asset_library_versions import AssetVersions
from rig_errors import RigError

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.library = AssetLibrary(self.root / 'library', create=True)
        self.versions = AssetVersions(self.library)
        self.manifest = json.loads((ROOT / 'examples/asset-library-foundation/manifest.json').read_text())
        self.sources = {}
        for item in self.manifest['files']:
            target = self.root / item['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / 'examples/asset-library-foundation' / item['path']).read_bytes())
            self.sources[item['path']] = target

    def initial(self):
        return self.library.register(self.manifest, self.sources)

    def newer(self, previous, version='1.0.1', **changes):
        manifest = deepcopy(self.manifest)
        manifest['version'] = version
        manifest['metadata']['description'] = 'Corrected description'
        manifest['provenance']['parents'] = [previous]
        manifest.update(changes)
        return seal(manifest)

    def test_register_promote_compare_rollback_preserves_old_bytes(self):
        first = self.initial()
        saved = {name: path.read_bytes() for name, path in self.sources.items()}
        self.versions.promote(first, expected_revision=0)
        mesh_path = 'mesh/triangle.obj'
        changed = self.root / 'changed.obj'
        changed.write_bytes(saved[mesh_path] + b'\n# version 1.0.1\n')
        files = [file_record(changed, mesh_path, file['role']) if file['path'] == mesh_path else file
                 for file in self.manifest['files']]
        provenance = deepcopy(self.manifest['provenance'])
        provenance.update(parents=[first], sources=[file for file in files if file['path'] == mesh_path])
        second = self.library.register(
            self.newer(first, files=files, provenance=provenance), {**self.sources, mesh_path: changed},
        )
        self.assertEqual(self.versions.history('triangle'), [first, second])
        comparison = self.versions.compare(first, second)
        self.assertTrue(comparison['allowed'])
        self.assertEqual(comparison['actual_bump'], 'patch')
        self.assertEqual(comparison['files'], [mesh_path])
        plan = self.versions.promotion_plan(second)
        self.assertFalse(plan['grants_approval'])
        self.assertEqual(plan['previous'], first)
        self.versions.promote(second, expected_revision=1)
        self.assertEqual(self.versions.state('triangle')['promoted'], second)
        self.versions.rollback('triangle', expected_revision=2)
        self.assertEqual(self.versions.state('triangle')['promoted'], first)
        reopened = AssetVersions(AssetLibrary(self.library.root))
        observed = reopened.state('triangle')
        self.assertEqual(observed['promoted'], first)
        self.library.database.unlink()
        AssetLibrary.rebuild_index(self.library.root)
        rebuilt = AssetLibrary(self.library.root)
        self.assertEqual(AssetVersions(rebuilt).state('triangle'), observed)
        self.assertEqual(AssetVersions(rebuilt).history('triangle'), [first, second])
        for name, data in saved.items():
            self.assertEqual(rebuilt.read_file(first, name), data)
        self.assertEqual(rebuilt.read_file(second, mesh_path), changed.read_bytes())

    def test_promotion_rejects_same_name_incompatible_signature(self):
        first = self.initial()
        with self.assertRaisesRegex(RigError, 'requirements'):
            self.versions.promote(first, expected_revision=0, consumers=[dict(id='actor', requirements={'skeleton_signature': ['b' * 64]})])
        self.assertEqual(self.versions.state('triangle')['revision'], 0)
        result = self.versions.promote(first, expected_revision=0, consumers=[dict(id='actor', requirements={'formats': ['obj']})])
        self.assertEqual(result['revision'], 1)

    def test_malformed_consumers_fail_closed(self):
        first = self.initial()
        for consumers in [False, {}, '']:
            with self.assertRaises(RigError): self.versions.promote(first, expected_revision=0, consumers=consumers)
        self.assertEqual(self.versions.state('triangle')['revision'], 0)

    def test_rollback_rejects_concurrent_promotion_during_plan(self):
        first = self.initial()
        self.versions.promote(first, expected_revision=0)
        second = self.library.register(self.newer(first), self.sources)
        self.versions.promote(second, expected_revision=1)
        original = self.versions.promotion_plan
        def racing_plan(reference, **kwargs):
            other = AssetVersions(AssetLibrary(self.library.root))
            other.promote(second, expected_revision=2)
            return original(reference, **kwargs)
        with patch.object(self.versions, 'promotion_plan', side_effect=racing_plan):
            with self.assertRaises(RigError): self.versions.rollback('triangle', expected_revision=2)
        self.assertEqual(self.versions.state('triangle')['revision'], 3)
        self.assertEqual(self.versions.state('triangle')['promoted'], second)

    def test_stale_revisions_and_concurrent_promotions_are_rejected(self):
        first = self.initial()
        second = self.library.register(self.newer(first), self.sources)
        barrier = threading.Barrier(2)
        outcomes = []

        def writer(reference):
            try:
                versions = AssetVersions(AssetLibrary(self.library.root))
                original = versions.promotion_plan

                def plan(*args, **kwargs):
                    observed = original(*args, **kwargs)
                    barrier.wait(timeout=5)
                    return observed

                with patch.object(versions, 'promotion_plan', side_effect=plan):
                    outcomes.append(versions.promote(reference, expected_revision=0))
            except RigError as error:
                outcomes.append(error.report['code'])

        threads = [threading.Thread(target=writer, args=(reference,)) for reference in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        errors = [outcome for outcome in outcomes if isinstance(outcome, str)]
        self.assertEqual(errors, ['LIBRARY_CONFLICT'])
        committed = self.versions.state('triangle')
        self.assertEqual(committed['revision'], 1)
        self.assertIn(committed['promoted'], (first, second))
        with self.assertRaises(RigError): self.versions.promote(first, expected_revision=0)

    def test_deprecate_never_removes_payload_or_selects_deprecated(self):
        first = self.initial()
        self.versions.promote(first, expected_revision=0)
        with self.assertRaises(RigError): self.versions.deprecate(first, expected_revision=1, reason='Retire')
        second = self.library.register(self.newer(first), self.sources)
        self.versions.promote(second, expected_revision=1)
        self.versions.deprecate(first, expected_revision=2, reason='Retained historical version')
        self.assertEqual(self.library.resolve(first)['version'], '1.0.0')
        with self.assertRaises(RigError): self.versions.promote(first, expected_revision=3)
        with self.assertRaises(RigError): self.versions.rollback('triangle', expected_revision=3)


    def test_wrong_hash_cannot_compare_promote_or_deprecate(self):
        first = self.initial()
        wrong = {**first, 'sha256': 'f' * 64}
        for operation in (
            lambda: self.versions.compare(first, wrong),
            lambda: self.versions.promotion_plan(wrong),
            lambda: self.versions.promote(wrong, expected_revision=0),
            lambda: self.versions.deprecate(wrong, expected_revision=0, reason='Retire'),
        ):
            with self.assertRaises(RigError):
                operation()
        self.assertEqual(self.versions.state('triangle')['revision'], 0)

    def test_legacy_reads_and_failed_event_append_preserve_registered_assets(self):
        root = self.root / 'legacy'
        root.mkdir(mode=0o700)
        database = root / 'library.sqlite3'
        database.touch(mode=0o600)
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript(INDEX_SCHEMA)
        library = AssetLibrary(root)
        versions = AssetVersions(library)
        first = library.register(self.manifest, self.sources)
        second = library.register(self.newer(first), self.sources)
        self.assertEqual(versions.history('triangle'), [first, second])
        self.assertTrue(versions.compare(first, second)['allowed'])
        self.assertEqual(versions.promotion_plan(first)['expected_revision'], 0)
        with self.assertRaises(RigError) as rejected:
            versions.promote(first, expected_revision=0)
        self.assertEqual(rejected.exception.report['code'], 'LIBRARY_DURABILITY')
        self.assertEqual(versions.state('triangle')['history'], [])
        self.assertEqual(library.read_file(first, 'mesh/triangle.obj'), self.sources['mesh/triangle.obj'].read_bytes())
        self.assertFalse(library.storage_status()['durable'])

    def test_durable_append_split_requires_owner_recovery_before_retry(self):
        first = self.initial()
        append = self.library.storage.append

        def split(record):
            append(record)
            raise OSError('interrupted before index commit')

        with patch.object(self.library.storage, 'append', side_effect=split):
            with self.assertRaisesRegex(OSError, 'interrupted before index commit'):
                self.versions.promote(first, expected_revision=0)
        with self.assertRaises(RigError) as rejected:
            self.versions.promote(first, expected_revision=0)
        self.assertEqual(rejected.exception.report['code'], 'LIBRARY_DURABILITY')
        self.library.recover()
        self.assertEqual(self.versions.state('triangle')['promoted'], first)
        self.assertEqual(self.versions.state('triangle')['revision'], 1)
        with self.assertRaises(RigError) as stale:
            self.versions.promote(first, expected_revision=0)
        self.assertEqual(stale.exception.report['code'], 'LIBRARY_VERSION_CONFLICT')

    def test_underscore_ids_use_valid_durable_stream_names(self):
        self.manifest['identity']['id'] = 'asset_name'
        self.manifest = seal(self.manifest)
        reference = self.initial()
        event = self.versions.promote(reference, expected_revision=0)
        self.assertRegex(event['stream'], r'^asset-version\.[a-f0-9]{64}$')
        self.assertEqual(self.versions.state('asset_name')['promoted'], reference)

    def test_semver_comparison_distinguishes_added_and_removed_capabilities(self):
        first = self.initial()
        compatible = self.newer(first, '1.10.0')
        compatible['compatibility']['renderers'] = ['blender']
        second = self.library.register(seal(compatible), self.sources)
        older = self.library.register(self.newer(first, '1.2.0'), self.sources)
        removed = self.library.register(self.newer(second, '1.11.0'), self.sources)
        self.assertEqual(self.versions.history('triangle'), [first, older, second, removed])
        added = self.versions.compare(first, second)
        self.assertEqual(added['minimum_bump'], 'minor')
        self.assertTrue(added['allowed'])
        breaking = self.versions.compare(second, removed)
        self.assertEqual(breaking['breaking'], ['renderers'])
        self.assertEqual(breaking['minimum_bump'], 'major')
        self.assertFalse(breaking['allowed'])

    def test_replay_rejects_unregistered_exact_event_reference(self):
        first = self.initial()
        wrong = {**first, 'sha256': 'f' * 64}
        document = dict(schema_version=1, operation='promote', reference=wrong,
                        previous=None, reason='Imported historical decision', consumers=[])
        stream = self.versions._stream('triangle')
        self.library.append_event(stream, expected_revision=0, document=document)
        reopened = AssetVersions(AssetLibrary(self.library.root))
        with self.assertRaises(RigError) as rejected:
            reopened.state('triangle')
        self.assertEqual(rejected.exception.report['code'], 'LIBRARY_HASH')

    def test_replay_rejects_invalid_action_and_evidence(self):
        first = self.initial()
        stream = self.versions._stream('triangle')
        document = dict(schema_version=1, reference=first,
                        previous=None, reason='Imported historical decision')
        invalid = [
            dict(operation='rollback', consumers=[]),
            dict(operation='deprecate', consumers=[dict(id='actor', compatible=True, mismatches=[])]),
            dict(operation='promote', consumers=[dict(id='actor', compatible=False, mismatches=['formats'])]),
        ]
        for fields in invalid:
            with self.subTest(fields=fields):
                library = AssetLibrary(self.root / fields['operation'], create=True)
                library.register(self.manifest, self.sources)
                library.append_event(stream, expected_revision=0, document={**document, **fields})
                reopened = AssetVersions(AssetLibrary(library.root))
                with self.assertRaises(RigError) as rejected:
                    reopened.state('triangle')
                self.assertEqual(rejected.exception.report['code'], 'LIBRARY_VERSION_HISTORY')

    def test_collections_preserve_manifest_annotations_and_survive_rebuild(self):
        first = self.initial()
        before = self.library.resolve(first)
        payload = self.library.read_file(first, 'mesh/triangle.obj')
        self.assertEqual(self.versions.collections(first), dict(revision=0, collections=[]))
        event = self.versions.set_collections(first, expected_revision=0, collections=['laboratory', 'favorites'])
        self.assertEqual(event['document'], dict(schema_version=1, reference=first,
                                                collections=['favorites', 'laboratory']))
        annotations = self.library.update_annotations(first, expected_revision=0,
                                                       tags=['robot'], favorite=True, note='Preserve source')
        with self.assertRaises(RigError) as stale:
            self.versions.set_collections(first, expected_revision=0, collections=[])
        self.assertEqual(stale.exception.report['code'], 'LIBRARY_VERSION_CONFLICT')
        reopened = AssetVersions(AssetLibrary(self.library.root))
        self.assertEqual(reopened.collections(first), dict(revision=1, collections=['favorites', 'laboratory']))
        self.assertEqual(self.library.events(event['stream']), [event])
        self.library.database.unlink()
        AssetLibrary.rebuild_index(self.library.root)
        rebuilt = AssetLibrary(self.library.root)
        self.assertEqual(AssetVersions(rebuilt).collections(first), reopened.collections(first))
        self.assertEqual(rebuilt.resolve(first), before)
        self.assertEqual(rebuilt.annotations(first), annotations)
        self.assertEqual(rebuilt.read_file(first, 'mesh/triangle.obj'), payload)
        self.assertEqual(AssetVersions(rebuilt).state(first['id'])['revision'], 0)

    def test_collection_writers_race_at_canonical_event_cas(self):
        first = self.initial()
        barrier = threading.Barrier(2)
        outcomes = []

        def writer(collection):
            try:
                versions = AssetVersions(AssetLibrary(self.library.root))
                original = versions.collections

                def snapshot(reference):
                    observed = original(reference)
                    barrier.wait(timeout=5)
                    return observed

                with patch.object(versions, 'collections', side_effect=snapshot):
                    outcomes.append(versions.set_collections(first, expected_revision=0, collections=[collection]))
            except Exception as error:
                outcomes.append(error.report['code'] if isinstance(error, RigError) else type(error).__name__)

        threads = [threading.Thread(target=writer, args=(name,)) for name in ('favorites', 'laboratory')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(outcomes), 2)
        self.assertEqual([value for value in outcomes if isinstance(value, str)], ['LIBRARY_CONFLICT'])
        self.assertEqual(len(self.library.events('asset-collections.' + first['sha256'])), 1)
        state = AssetVersions(AssetLibrary(self.library.root)).collections(first)
        self.assertEqual(state['revision'], 1)
        self.assertIn(state['collections'], (['favorites'], ['laboratory']))

    def test_collection_admission_rejects_invalid_values_and_exact_references(self):
        first = self.initial()
        for values in (False, {}, ['favorites'] * 2, ['bad name'], [1], ['x'] * 129):
            with self.subTest(values=values), self.assertRaises(RigError):
                self.versions.set_collections(first, expected_revision=0, collections=values)
        with self.assertRaises(RigError):
            self.versions.set_collections(first, expected_revision=True, collections=[])
        wrong = {**first, 'sha256': 'f' * 64}
        with self.assertRaises(RigError):
            self.versions.collections(wrong)
        with self.assertRaises(RigError):
            self.versions.set_collections(wrong, expected_revision=0, collections=[])
        self.assertEqual(self.library.events('asset-collections.' + first['sha256']), [])

    def test_collection_replay_rejects_malformed_canonical_documents(self):
        first = self.initial()
        document = dict(schema_version=1, reference=first, collections=['favorites'])
        invalid = [
            {**document, 'schema_version': True}, {**document, 'unexpected': None},
            {**document, 'reference': {**first, 'sha256': 'f' * 64}},
            {**document, 'collections': ['favorites', 'favorites']},
            {**document, 'collections': ['bad name']},
        ]
        for index, candidate in enumerate(invalid):
            with self.subTest(index=index):
                library = AssetLibrary(self.root / ('collections-' + str(index)), create=True)
                library.register(self.manifest, self.sources)
                library.append_event('asset-collections.' + first['sha256'],
                                     expected_revision=0, document=candidate)
                with self.assertRaises(RigError):
                    AssetVersions(AssetLibrary(library.root)).collections(first)

    def test_collection_interrupted_append_requires_canonical_recovery(self):
        first = self.initial()
        append = self.library.storage.append

        def split(record):
            append(record)
            raise OSError('interrupted collection index commit')

        with patch.object(self.library.storage, 'append', side_effect=split):
            with self.assertRaises(OSError):
                self.versions.set_collections(first, expected_revision=0, collections=['favorites'])
        with self.assertRaises(RigError) as rejected:
            self.versions.collections(first)
        self.assertEqual(rejected.exception.report['code'], 'LIBRARY_DURABILITY')
        self.library.recover()
        self.assertEqual(AssetVersions(AssetLibrary(self.library.root)).collections(first),
                         dict(revision=1, collections=['favorites']))
        with self.assertRaises(RigError):
            self.versions.set_collections(first, expected_revision=0, collections=[])
