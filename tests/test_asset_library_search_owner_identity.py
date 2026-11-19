"""Native directory replacement must fail before search publication or encoding.

Only disposable synthetic libraries are renamed. The provider double exercises
dispatch boundaries, not actual embedding inference or retrieval quality.
"""
from pathlib import Path
import unittest

from asset_library import AssetLibrary
from asset_library_search import ProviderRegistry, SearchIndex
from asset_library_versions import AssetVersions
from rig_errors import RigError
import test_asset_library as foundation_fixture


class DispatchOnlyProvider:
    """Protocol double: count encoding calls; no model or network is involved."""
    model_id = 'dispatch-only-fixture'
    model_version = '1'
    runtime_available = True
    requires_credentials = False
    credentials_available = False
    transmits_metadata = False

    def __init__(self):
        self.calls = 0

    def encode(self, texts):
        self.calls += 1
        return [[1.0, 0.0] for _ in texts]


class DirectoryOwnerIdentityTests(unittest.TestCase):
    def setUp(self):
        fixture = foundation_fixture.AssetLibraryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        self.old_library = fixture.library
        self.reference = fixture.register()
        self.location = dict(library='local', reference=self.reference)
        self.old_owner = AssetVersions(self.old_library)
        self.old_owner.set_collections(self.reference, expected_revision=0,
                                       collections=['original-only'])
        self.old_reopened_owner = AssetVersions(AssetLibrary(self.old_library.root))

        replacement = AssetLibrary(self.root / 'replacement', create=True)
        self.assertEqual(replacement.register(fixture.manifest, fixture.sources),
                         self.reference)
        AssetVersions(replacement).set_collections(
            self.reference, expected_revision=0, collections=['replacement-only'])
        # Move only this TemporaryDirectory's synthetic roots. Both original
        # libraries and all input bytes remain available until fixture cleanup.
        self.saved_original = self.root / 'saved-original'
        self.old_library.root.rename(self.saved_original)
        replacement.root.rename(self.old_library.root)
        self.current = AssetLibrary(self.old_library.root)
        self.current_owner = AssetVersions(AssetLibrary(self.current.root))
        self.assertEqual(self.current.resolve(self.reference), fixture.manifest)
        with self.assertRaises(RigError) as stale:
            self.old_owner.collections(self.reference)
        self.assertEqual(stale.exception.report['code'], 'LIBRARY_ROOT')

    @staticmethod
    def tree_bytes(root):
        return {str(path.relative_to(root)): path.read_bytes()
                for path in sorted(Path(root).rglob('*')) if path.is_file()}

    def authoritative_bytes(self):
        return dict(original=self.tree_bytes(self.saved_original),
                    replacement=self.tree_bytes(self.current.root),
                    sources={name: path.read_bytes()
                             for name, path in self.fixture.sources.items()})

    def stale_handles(self):
        return (
            ('stale-collection-owner', self.current, self.old_owner),
            ('both-stale-same-handle', self.old_library, self.old_owner),
            ('both-stale-independent-handles', self.old_library, self.old_reopened_owner),
        )

    def current_index(self, name):
        index = SearchIndex(self.root / name, {'local': self.current}, create=True,
                            versions={'local': self.current_owner})
        self.assertEqual(index.rebuild([self.location])['indexed'], 1)
        self.assertEqual(index.search(filters={'collections': ['replacement-only']})['total'], 1)
        return index

    def assert_rejected_preserving_bytes(self, operation, index=None, provider=None):
        authoritative = self.authoritative_bytes()
        derived = self.tree_bytes(index.root) if index is not None else None
        error = None
        try:
            operation()
        except RigError as exc:
            error = exc
        with self.subTest(invariant='canonical-and-source-bytes'):
            self.assertTrue(self.authoritative_bytes() == authoritative,
                            'Rejected ownership must preserve both libraries and input bytes')
        if index is not None:
            with self.subTest(invariant='derived-index-bytes'):
                self.assertTrue(self.tree_bytes(index.root) == derived,
                                'Invalid ownership changed an existing good search index')
        if provider is not None:
            with self.subTest(invariant='no-provider-dispatch'):
                self.assertEqual(provider.calls, 0)
        with self.subTest(invariant='typed-rejection'):
            self.assertIsInstance(error, RigError,
                                  'Invalid native ownership was accepted as success or incomplete data')

    def test_same_root_independently_reopened_handles_work(self):
        before = self.authoritative_bytes()
        index = self.current_index('reopened-index')
        self.assertEqual(index.search(filters={'collections': ['original-only']})['total'], 0)
        reopened = SearchIndex(index.root, {'local': AssetLibrary(self.current.root)},
                               versions={'local': AssetVersions(AssetLibrary(self.current.root))})
        self.assertEqual(reopened.rebuild()['indexed'], 1)
        self.assertEqual(reopened.search(filters={'collections': ['replacement-only']})['results'][0]['location'],
                         self.location)
        self.assertEqual(AssetLibrary(self.saved_original).read_file(
            self.reference, 'mesh/triangle.obj'), self.fixture.mesh.read_bytes())
        self.assertTrue(self.authoritative_bytes() == before)

    def test_stale_native_handles_reject_before_index_creation(self):
        for name, library, owner in self.stale_handles():
            with self.subTest(handles=name):
                destination = self.root / name
                self.assert_rejected_preserving_bytes(lambda: SearchIndex(
                    destination, {'local': library}, create=True, versions={'local': owner}))
                self.assertFalse(destination.exists(), 'Invalid ownership created an index directory')

    def test_stale_native_handles_reject_rebuild_without_replacing_good_index(self):
        for name, library, owner in self.stale_handles():
            for explicit in (False, True):
                with self.subTest(handles=name, explicit=explicit):
                    index = self.current_index(name + '-rebuild-' + str(explicit))
                    index.libraries['local'] = library
                    index.versions['local'] = owner
                    operation = (lambda: index.rebuild([self.location])) if explicit else index.rebuild
                    self.assert_rejected_preserving_bytes(operation, index=index)

    def test_stale_native_handles_reject_queries_and_embeddings_before_encoding(self):
        for name, library, owner in self.stale_handles():
            for operation_name in ('lexical', 'semantic-query', 'embedding-rebuild'):
                with self.subTest(handles=name, operation=operation_name):
                    index = self.current_index(name + '-' + operation_name)
                    provider = DispatchOnlyProvider()
                    registry = ProviderRegistry()
                    registry.register('dispatch', provider)
                    index.rebuild_embeddings(registry, 'dispatch', enabled=True)
                    provider.calls = 0
                    index.libraries['local'] = library
                    index.versions['local'] = owner
                    operations = {
                        'lexical': lambda: index.search('triangle'),
                        'semantic-query': lambda: index.search(
                            'triangle', registry=registry, provider='dispatch', enabled=True),
                        'embedding-rebuild': lambda: index.rebuild_embeddings(
                            registry, 'dispatch', enabled=True),
                    }
                    self.assert_rejected_preserving_bytes(operations[operation_name],
                                                          index=index, provider=provider)


if __name__ == '__main__':
    unittest.main()
