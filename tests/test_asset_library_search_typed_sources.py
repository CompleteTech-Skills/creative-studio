"""Native asset-search regressions for source fidelity and owner availability."""
from contextlib import closing
from copy import deepcopy
import hashlib
import sqlite3
import unittest

from asset_library import file_record
from asset_library_contracts import canonical_bytes, seal
from rig_errors import RigError, decode_json
import test_asset_library_search as native_fixture


class TypedSearchSourceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.SearchTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.library = self.fixture.library
        self.index = self.fixture.index

    @staticmethod
    def tree_digests(root):
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}

    def sources(self):
        return (self.tree_digests(self.fixture.example), self.tree_digests(self.library.root))

    def hits(self, *args, **kwargs):
        return [row['location'] for row in self.index.search(*args, **kwargs)['results']]

    def test_only_numeric_triangle_measurements_satisfy_a_budget(self):
        measured = self.fixture.register('measured', triangles=1)
        flag = self.fixture.register('flag', triangles=True)
        before = self.sources()
        self.assertEqual(self.index.rebuild([measured, flag])['indexed'], 2)
        cached = self.tree_digests(self.index.root)
        self.assertEqual(self.hits(filters={'max_triangles': 1}), [measured])
        self.assertEqual(self.sources(), before)
        self.assertEqual(self.tree_digests(self.index.root), cached)

    def test_missing_annotation_projection_preserves_full_and_pinned_caches_until_recovery(self):
        item = self.fixture.register('retained', keywords=['searchable'])
        reference = item['reference']
        annotation = self.library.annotations(reference)
        self.assertEqual(self.index.rebuild()['indexed'], 1)
        cached = self.tree_digests(self.index.root)
        manifest = self.library.resolve(reference)
        payloads = {f['path']: self.library.read_file(reference, f['path'])
                    for f in manifest['files']}
        with closing(sqlite3.connect(self.library.database)) as db, db:
            db.execute('DELETE FROM annotations WHERE id=? AND version=?',
                       (reference['id'], reference['version']))
        damaged_sources = self.sources()
        for selection in (None, [item]):
            with self.subTest(selection=selection):
                with self.assertRaises(RigError) as raised:
                    self.index.rebuild(selection)
                self.assertEqual(raised.exception.report['code'], 'LIBRARY_CORRUPT')
                self.assertEqual(self.tree_digests(self.index.root), cached)
                self.assertEqual(self.sources(), damaged_sources)
        self.library.recover()
        self.assertEqual(self.library.annotations(reference), annotation)
        self.assertEqual(self.library.resolve(reference), manifest)
        for path, payload in payloads.items():
            self.assertEqual(self.library.read_file(reference, path), payload)
        self.assertEqual(self.hits('searchable'), [item])
        self.assertEqual(self.tree_digests(self.index.root), cached)
        self.assertEqual(self.index.rebuild([item])['indexed'], 1)

    def test_find_animations_selects_a_real_clip_instead_of_a_tagged_prop(self):
        source = next((native_fixture.ROOT / 'examples/rig-3d/robotic/animations').glob('hinge-sweep-*.json'))
        clip = decode_json(source.read_bytes())
        self.assertTrue(clip['tracks'])
        self.assertTrue(any(track['times'] for track in clip['tracks']))
        original = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest = deepcopy(self.fixture.template)
        payload = file_record(source, 'source/motion.json', role='animation')
        manifest['identity'].update(id='motion', slug='motion', name='Motion',
                                    kind='animation', subtype='skeletal')
        manifest['files'][0] = payload
        manifest['provenance']['sources'] = [payload]
        manifest['publication']['inventory'] = [f['path'] for f in manifest['files']]
        manifest['compatibility']['formats'] = ['json']
        paths = {f['path']: self.fixture.example / f['path'] for f in manifest['files']}
        paths[payload['path']] = source
        animation = {'library': 'local', 'reference': self.library.register(seal(manifest), paths)}
        prop = self.fixture.register('display', keywords=['animation', 'animations'])
        self.library.update_annotations(prop['reference'], expected_revision=0,
                                        tags=['animations'], favorite=False, note='')
        self.assertEqual(self.index.rebuild([animation, prop])['indexed'], 2)
        cached = self.tree_digests(self.index.root)
        sources = self.sources()
        self.assertEqual(self.hits(filters={'kind': ['animation']}), [animation])
        for query in ('Find animations', 'find an animation'):
            with self.subTest(query=query):
                self.assertEqual(self.hits(query), [animation])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)
        self.assertEqual(self.sources(), sources)
        self.assertEqual(self.tree_digests(self.index.root), cached)

    def test_parser_covers_every_canonical_kind_and_plural(self):
        from asset_library_contracts import KINDS
        from asset_library_search import parse_query
        for kind in KINDS:
            for word in (kind, kind + 's'):
                with self.subTest(word=word):
                    parsed = parse_query('Find ' + word)
                    self.assertEqual(parsed['constraints'], {'kind': [kind]})
                    self.assertEqual(parsed['tokens'], [])

    def test_wrong_json_measurement_type_refuses_query_without_changing_sources(self):
        measured = self.fixture.register('measured', triangles=1)
        before = self.sources()
        self.assertEqual(self.index.rebuild([measured])['indexed'], 1)
        budget = {'max_triangles': 1}
        self.assertEqual(self.hits(filters=budget), [measured])
        # Only fault the disposable cache. The native manifest and snapshot
        # retain the integer 1, which ordinary Python equality equates to True.
        with closing(sqlite3.connect(self.index.database)) as db, db:
            row_id, data = db.execute('SELECT id, document FROM entries').fetchone()
            row = decode_json(data)
            row['manifest']['files'][0]['technical_metadata']['triangles'] = True
            db.execute('UPDATE entries SET document=? WHERE id=?', (canonical_bytes(row), row_id))
        damaged = self.tree_digests(self.index.root)
        with self.subTest('typed refusal'):
            with self.assertRaises(RigError) as raised:
                self.index.search(filters=budget)
            self.assertEqual(raised.exception.report['code'], 'LIBRARY_SEARCH_INDEX')
        self.assertEqual(self.sources(), before)
        self.assertEqual(self.tree_digests(self.index.root), damaged)
        self.assertEqual(self.index.rebuild([measured])['indexed'], 1)
        self.assertEqual(self.hits(filters=budget), [measured])
        self.assertEqual(self.sources(), before)

    def test_unavailable_native_owner_preserves_the_pinned_index(self):
        item = self.fixture.register('measured', triangles=1)
        before = self.sources()
        self.assertEqual(self.index.rebuild([item])['indexed'], 1)
        self.assertEqual(self.hits(), [item])
        cached = self.tree_digests(self.index.root)
        # Real temporary writer unavailability. There are no canonical writes
        # and the lock is released before checking the original search again.
        with closing(sqlite3.connect(self.library.database)) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            try:
                with self.subTest('typed refusal'):
                    with self.assertRaises(RigError) as raised:
                        self.index.rebuild([item])
                    self.assertEqual(raised.exception.report['code'], 'LIBRARY_STORAGE')
            finally:
                writer.rollback()
        self.assertEqual(self.sources(), before)
        with self.subTest('known-good bytes preserved'):
            self.assertEqual(self.tree_digests(self.index.root), cached)
        with self.subTest('readable immediately after release'):
            self.assertEqual(self.hits(), [item])

    def test_find_rigs_binds_kind_even_with_a_lexical_prop_decoy(self):
        source = native_fixture.ROOT / 'examples' / 'rig-3d' / 'robotic' / 'rig.json'
        original_rig = hashlib.sha256(source.read_bytes()).hexdigest()
        manifest = deepcopy(self.fixture.template)
        payload = file_record(source, 'source/articulation.json', role='source')
        manifest['identity'].update(id='articulated', slug='articulated', name='Articulated',
                                    kind='rig', subtype='articulated')
        manifest['files'][0] = payload
        manifest['provenance']['sources'] = [payload]
        manifest['publication']['inventory'] = [f['path'] for f in manifest['files']]
        manifest['compatibility']['formats'] = ['json']
        paths = {f['path']: self.fixture.example / f['path'] for f in manifest['files']}
        paths[payload['path']] = source
        rig = {'library': 'local', 'reference': self.library.register(seal(manifest), paths)}
        prop = self.fixture.register('display', keywords=['rig', 'rigs'])
        before = self.sources()
        self.assertEqual(self.index.rebuild([rig, prop])['indexed'], 2)
        cached = self.tree_digests(self.index.root)
        self.assertEqual(self.hits(filters={'kind': ['rig']}), [rig])
        self.assertEqual(self.hits(filters={'kind': ['prop']}), [prop])
        for query in ('Find rigs', 'find a rig'):
            with self.subTest(query=query):
                self.assertEqual(self.hits(query), [rig])
        self.assertEqual(self.sources(), before)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original_rig)
        self.assertEqual(self.tree_digests(self.index.root), cached)
