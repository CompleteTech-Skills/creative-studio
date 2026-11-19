"""Offline public-contract regressions for search admission and derived indexes.

Fixtures register real OBJ bytes in disposable native libraries. Compatibility
and embedding doubles prove dispatch contracts only, not native rig evaluation,
model execution, network delivery, or semantic retrieval quality.
"""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import unittest

from asset_library import AssetLibrary
from asset_library_search import ProviderRegistry, SearchIndex
from rig_errors import RigError
import test_asset_library_search as search_fixture


class DispatchOnlyProvider:
    """Never-network protocol adapter whose only effect is counting calls."""
    def __init__(self, *, remote=False):
        self.model_id = 'offline-dispatch-fixture'
        self.model_version = '1'
        self.runtime_available = True
        self.requires_credentials = remote
        self.credentials_available = remote
        self.transmits_metadata = remote
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]


class SearchAdmissionRegressions(unittest.TestCase):
    def setUp(self):
        # Composition deliberately avoids collecting the existing suite again.
        self.fixture = search_fixture.SearchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.library = self.fixture.library
        self.index = self.fixture.index
        self.hero = self.fixture.register('cobalt-sentinel', kind='character',
                                          keywords=['cobalt'])
        self.prop = self.fixture.register('village-rock', kind='prop')
        self.locations = [self.hero, self.prop]
        self.assertEqual(self.index.rebuild(self.locations)['indexed'], 2)
        self.assertEqual(self.index.search('cobalt')['results'][0]['location'], self.hero)
        self.assertEqual(self.index.versions, {})

    @staticmethod
    def tree_hashes(root):
        return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(Path(root).rglob('*')) if path.is_file()}

    def source_hashes(self):
        return self.tree_hashes(self.fixture.example)

    def authorities(self):
        roots = {'current': self.library.root}
        if hasattr(self, 'saved_original'):
            roots['original'] = self.saved_original
        return {name: self.tree_hashes(root) for name, root in roots.items()}

    def rejected_without_writes(self, operation, *, provider=None, codes=None):
        before = (self.authorities(), self.source_hashes(), self.tree_hashes(self.index.root))
        calls = provider.calls if provider is not None else None
        error = None
        try:
            operation()
        except Exception as exc:
            # A leaked AttributeError/TypeError is a contract failure, not a
            # fixture setup error; assert a typed public refusal below.
            error = exc
        with self.subTest(invariant='canonical-and-source-preservation'):
            self.assertEqual((self.authorities(), self.source_hashes()), before[:2])
        with self.subTest(invariant='derived-index-preservation'):
            self.assertEqual(self.tree_hashes(self.index.root), before[2])
        if provider is not None:
            with self.subTest(invariant='no-encode-dispatch'):
                self.assertEqual(provider.calls, calls)
        with self.subTest(invariant='typed-refusal'):
            self.assertIsInstance(error, RigError,
                                  'Expected typed refusal before success or adapter dispatch; got %r' % error)
            if codes is not None:
                self.assertIn(error.report['code'], codes)

    def replace_native_root(self):
        replacement = AssetLibrary(self.root / 'replacement', create=True)
        old_hashes = self.tree_hashes(self.library.root)
        replacement_hashes = self.tree_hashes(replacement.root)
        self.saved_original = self.root / 'saved-original'
        self.library.root.rename(self.saved_original)
        replacement.root.rename(self.library.root)
        self.assertEqual(self.tree_hashes(self.saved_original), old_hashes)
        self.assertEqual(self.tree_hashes(self.library.root), replacement_hashes)
        with self.assertRaises(RigError) as stale:
            self.library.resolve(self.hero['reference'])
        self.assertEqual(stale.exception.report['code'], 'LIBRARY_ROOT')

    def test_stale_native_handle_without_versions_refuses_index_creation(self):
        self.replace_native_root()
        destination = self.root / 'new-index'
        self.rejected_without_writes(lambda: SearchIndex(
            destination, {'local': self.library}, create=True))
        self.assertFalse(destination.exists(), 'Stale native owner created a derived index')

    def test_stale_native_handle_without_versions_preserves_explicit_rebuild(self):
        self.replace_native_root()
        self.rejected_without_writes(lambda: self.index.rebuild(self.locations))

    def test_stale_native_handle_without_versions_preserves_full_rebuild(self):
        self.replace_native_root()
        self.rejected_without_writes(self.index.rebuild)

    def test_stale_native_handle_without_versions_refuses_queries_and_encoding(self):
        provider = DispatchOnlyProvider()
        registry = ProviderRegistry()
        registry.register('dispatch', provider)
        self.index.rebuild_embeddings(registry, 'dispatch', enabled=True)
        self.replace_native_root()
        operations = {
            'lexical-query': lambda: self.index.search('cobalt'),
            'semantic-query': lambda: self.index.search(
                'cobalt', registry=registry, provider='dispatch', enabled=True),
            'embedding-rebuild': lambda: self.index.rebuild_embeddings(
                registry, 'dispatch', enabled=True),
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                self.rejected_without_writes(operation, provider=provider)

    def test_reopened_current_native_handle_without_versions_still_works(self):
        before = (self.authorities(), self.source_hashes())
        reopened = SearchIndex(self.index.root, {'local': AssetLibrary(self.library.root)})
        self.assertEqual(reopened.rebuild(self.locations)['indexed'], 2)
        self.assertEqual(reopened.search('cobalt')['results'][0]['location'], self.hero)
        self.assertEqual((self.authorities(), self.source_hashes()), before)

    def test_divergent_fts_postings_refuse_queries_until_explicit_rebuild(self):
        sources = (self.authorities(), self.source_hashes())
        with closing(sqlite3.connect(self.index.database)) as db, db:
            # Simulate a stale derived cache only. Public content remains exact,
            # while the inverted postings were built from obsolete cached text.
            retained = db.execute('SELECT id,c0 FROM words_content').fetchall()
            db.execute('UPDATE words_content SET c0=?', ('obsoletecachetoken',))
            db.execute("INSERT INTO words(words) VALUES('rebuild')")
            db.executemany('UPDATE words_content SET c0=? WHERE id=?',
                           [(text, number) for number, text in retained])
            self.assertEqual(db.execute('SELECT rowid,text FROM words ORDER BY rowid').fetchall(),
                             db.execute('SELECT id,text FROM entries ORDER BY id').fetchall())
        for query in ('cobalt', 'obsoletecachetoken', ''):
            with self.subTest(query=query):
                self.rejected_without_writes(lambda: self.index.search(query),
                                             codes={'LIBRARY_SEARCH_INDEX', 'LIBRARY_SEARCH_STALE'})
        # Repair is explicit and rebuilds exclusively from registered sources.
        try:
            repaired = self.index.rebuild(self.locations)
        except RigError as exc:
            self.fail('Explicit rebuild could not repair the disposable FTS cache: ' + exc.report['code'])
        self.assertEqual(repaired['indexed'], 2)
        self.assertEqual([row['location'] for row in self.index.search('cobalt')['results']], [self.hero])
        self.assertEqual(self.index.search('obsoletecachetoken')['total'], 0)
        self.assertEqual((self.authorities(), self.source_hashes()), sources)

    def test_mutated_provider_capabilities_refuse_both_dispatch_paths(self):
        missing = object()
        mutations = [
            ('transmits_metadata', None),
            ('credentials_available', 'unavailable'),
            ('runtime_available', missing),
            ('requires_credentials', None),
            ('encode', missing),
            ('model_id', missing),
            ('model_version', ''),
        ]
        for field, value in mutations:
            provider = DispatchOnlyProvider(remote=True)
            registry = ProviderRegistry()
            registry.register('dispatch', provider)
            self.index.rebuild_embeddings(registry, 'dispatch', enabled=True,
                                          authorize_private_metadata=True)
            if value is missing:
                if field == 'encode':
                    provider.encode = None
                else:
                    delattr(provider, field)
            else:
                setattr(provider, field, value)
            # All gates other than the malformed field are satisfied. The
            # unknown transmission flag specifically receives no authorization.
            authorize = field != 'transmits_metadata'
            for operation in ('rebuild', 'query'):
                with self.subTest(field=field, operation=operation):
                    if operation == 'rebuild':
                        call = lambda: self.index.rebuild_embeddings(
                            registry, 'dispatch', enabled=True, authorize_private_metadata=authorize)
                    else:
                        call = lambda: self.index.search('cobalt', registry=registry,
                                                        provider='dispatch', enabled=True,
                                                        authorize_private_metadata=authorize)
                    self.rejected_without_writes(call, provider=provider,
                                                 codes={'LIBRARY_SEARCH_PROVIDER'})

    def test_valid_local_and_authorized_remote_protocol_paths_remain_enabled_explicitly(self):
        for remote in (False, True):
            with self.subTest(remote=remote):
                provider = DispatchOnlyProvider(remote=remote)
                registry = ProviderRegistry()
                registry.register('dispatch', provider)
                self.rejected_without_writes(lambda: self.index.rebuild_embeddings(
                    registry, 'dispatch'), provider=provider, codes={'LIBRARY_SEARCH_PROVIDER'})
                if remote:
                    self.rejected_without_writes(lambda: self.index.rebuild_embeddings(
                        registry, 'dispatch', enabled=True), provider=provider,
                        codes={'LIBRARY_SEARCH_PRIVATE'})
                self.index.rebuild_embeddings(registry, 'dispatch', enabled=True,
                                              authorize_private_metadata=remote)
                self.rejected_without_writes(lambda: self.index.search(
                    'cobalt', registry=registry, provider='dispatch'), provider=provider,
                    codes={'LIBRARY_SEARCH_PROVIDER'})
                if remote:
                    self.rejected_without_writes(lambda: self.index.search(
                        'cobalt', registry=registry, provider='dispatch', enabled=True),
                        provider=provider, codes={'LIBRARY_SEARCH_PRIVATE'})
                response = self.index.search('cobalt', registry=registry, provider='dispatch',
                                             enabled=True, authorize_private_metadata=remote)
                self.assertTrue(response['embedding_used'])
                self.assertIn(self.hero, [row['location'] for row in response['results']])
                self.assertEqual(provider.calls, 2)

    def test_documented_compatibility_phrase_evaluates_registered_character(self):
        requirements = {'renderers': ['cycles']}
        calls = []
        def compatible(location, supplied):
            calls.append((location, supplied))
            return {'compatible': location == self.hero}
        self.index.compatibility = compatible
        structured = self.index.search(filters={'kind': ['character']}, compatibility=requirements)
        self.assertEqual([row['location'] for row in structured['results']], [self.hero])
        for query in ('Find characters compatible with this animation rig',
                      'Find characters compatible with this animation rig.'):
            calls.clear()
            with self.subTest(query=query):
                response = self.index.search(query, compatibility=requirements)
                self.assertEqual([row['location'] for row in response['results']], [self.hero])
                self.assertEqual(calls, [(self.hero, requirements)])
                self.assertEqual(response['results'][0]['compatibility_score'], 1)
                self.assertFalse(response['embedding_used'])

    def test_documented_compatibility_phrase_honors_false_and_keeps_real_terms(self):
        calls = []
        def incompatible(location, requirements):
            calls.append(location)
            return {'compatible': False}
        self.index.compatibility = incompatible
        requirements = {'renderers': ['cycles']}
        response = self.index.search('Find characters compatible with this animation rig',
                                     compatibility=requirements)
        self.assertEqual(response['total'], 0)
        with self.subTest(invariant='false-authority-was-invoked'):
            self.assertEqual(calls, [self.hero])
        self.index.compatibility = lambda location, supplied: {'compatible': True}
        self.assertEqual(self.index.search('Find cobalt characters compatible with this animation rig',
                                          compatibility=requirements)['total'], 1)
        self.assertEqual(self.index.search('Find absentterm characters compatible with this animation rig',
                                          compatibility=requirements)['total'], 0)
        self.rejected_without_writes(lambda: self.index.search(
            'Find characters compatible with this animation rig'), codes={'LIBRARY_SEARCH_REQUEST'})
        self.index.compatibility = None
        self.rejected_without_writes(lambda: self.index.search(
            'Find characters compatible with this animation rig', compatibility=requirements),
            codes={'LIBRARY_SEARCH_CAPABILITY'})

    def test_explicit_kind_conflict_never_claims_character_match_for_prop(self):
        try:
            response = self.index.search('Find characters', filters={'kind': ['prop']})
        except RigError as exc:
            self.assertEqual(exc.report['code'], 'LIBRARY_SEARCH_REQUEST')
            return
        # Explicit-filter precedence is supported; an intersection returning no
        # results is also safe. Any returned row must have an honest explanation.
        for row in response['results']:
            self.assertEqual(row['location'], self.prop)
            self.assertEqual(self.library.resolve(row['location']['reference'])['identity']['kind'], 'prop')
            self.assertNotIn('explicit asset kind: character', row['reasons'])
            for reason in row['reasons']:
                if 'character' in reason.casefold():
                    self.assertRegex(reason.casefold(), r'overrid|overridden|ignor|discard|conflict|supersed')


if __name__ == '__main__':
    unittest.main()
