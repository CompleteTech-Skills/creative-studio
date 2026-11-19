"""Offline read concurrency, compatibility admission and pagination contracts."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from threading import Event
import unittest

from rig_errors import RigError
import test_asset_library_search as native_fixture


class SearchConcurrencyAndCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.SearchTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.index = self.fixture.index
        self.locations = [self.fixture.register(name, kind='character')
                          for name in ('alpha', 'beta', 'gamma')]
        self.requirements = {'renderers': ['cycles']}
        self.assertEqual(self.index.rebuild(self.locations)['indexed'], 3)

    def file_hashes(self):
        roots = (self.fixture.example, self.fixture.library.root, self.index.root)
        return [{p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob('*') if p.is_file()} for root in roots]

    @staticmethod
    def hits(page):
        return [row['location'] for row in page['results']]

    def test_overlapping_reads_complete_while_compatibility_callback_is_paused(self):
        entered, resume = Event(), Event()
        def paused_authority(location, requirements):
            self.assertEqual(requirements, self.requirements)
            entered.set()
            if not resume.wait(60):
                raise AssertionError('The first reader was never released')
            return {'compatible': True}

        self.index.compatibility = paused_authority
        initial = self.file_hashes()
        with ThreadPoolExecutor(max_workers=2) as readers:
            first = readers.submit(self.index.search, compatibility=self.requirements, sort='name')
            try:
                self.assertTrue(entered.wait(30), 'First reader never reached compatibility evaluation')
                second = readers.submit(self.index.search, sort='name')
                try:
                    other_hits = self.hits(second.result(timeout=15))
                except Exception as exc:
                    other_hits = type(exc).__name__ + ': ' + str(exc)
            finally:
                resume.set()
            self.assertEqual(self.hits(first.result(timeout=30)), self.locations)
        self.assertEqual(self.hits(self.index.search(sort='name')), self.locations)
        self.assertEqual(self.file_hashes(), initial, 'Read-only queries changed source or index bytes')
        self.assertEqual(other_hits, self.locations, 'Ordinary lookup failed during an overlapping read')

    def test_compatibility_requires_a_typed_boolean_decision(self):
        before = self.file_hashes()
        bad_replies = [
            ('text false', {'compatible': 'false'}), ('one', {'compatible': 1}),
            ('zero', {'compatible': 0}), ('empty string', {'compatible': ''}),
            ('null value', {'compatible': None}), ('missing field', {}),
            ('nested decision', {'compatible': {'value': False}}),
            ('bare boolean', False), ('null response', None), ('list response', []),
        ]
        for label, reply in bad_replies:
            with self.subTest(response=label):
                self.index.compatibility = lambda location, requirements: reply
                try:
                    self.index.search(compatibility=self.requirements)
                except Exception as exc:
                    self.assertIsInstance(exc, RigError, 'Malformed compatibility requires a typed refusal')
                else:
                    self.fail('Malformed compatibility decision completed as a successful query')
        self.assertEqual(self.file_hashes(), before, 'Malformed decisions changed source or index bytes')

    def test_valid_boolean_compatibility_controls_admit_only_exact_matches(self):
        def mixed(location, requirements):
            self.assertEqual(requirements, self.requirements)
            return {'compatible': location == self.locations[1]}

        self.index.compatibility = mixed
        report = self.index.search('find characters compatible with this rig', compatibility=self.requirements)
        self.assertEqual(self.hits(report), [self.locations[1]])
        self.assertEqual(report['total'], 1)
        self.assertEqual(report['results'][0]['compatibility_score'], 1)

    def test_changed_eligibility_never_skips_a_still_compatible_exact_reference(self):
        eligible = list(self.locations)
        self.index.compatibility = lambda location, requirements: {'compatible': location in eligible}
        args = dict(sort='name', limit=1, compatibility=self.requirements)
        page = self.index.search(**args)
        self.assertEqual(self.hits(page), [self.locations[0]])
        self.assertIsNotNone(page['next_cursor'])
        eligible.remove(self.locations[0])
        self.assertEqual(self.hits(self.index.search(**args)), [self.locations[1]])
        # Either refuse the stale cursor or return a complete stable continuation.
        try:
            resumed = self.index.search(cursor=page['next_cursor'], **args)
        except RigError as exc:
            self.assertIn(exc.report['code'], ('LIBRARY_SEARCH_CURSOR', 'LIBRARY_SEARCH_STALE'))
            return
        self.assertEqual(self.hits(resumed), [self.locations[1]], 'Continuation silently skipped Beta')
        self.assertIsNotNone(resumed['next_cursor'])
        last = self.index.search(cursor=resumed['next_cursor'], **args)
        self.assertEqual(self.hits(last), [self.locations[2]])
        self.assertIsNone(last['next_cursor'])

    def test_unchanged_eligibility_pages_all_exact_references_in_order(self):
        self.index.compatibility = lambda location, requirements: {'compatible': location in self.locations}
        args = dict(sort='name', limit=1, compatibility=self.requirements)
        first = self.index.search(**args)
        self.assertIsNotNone(first['next_cursor'])
        second = self.index.search(cursor=first['next_cursor'], **args)
        self.assertIsNotNone(second['next_cursor'])
        third = self.index.search(cursor=second['next_cursor'], **args)
        self.assertEqual(self.hits(first) + self.hits(second) + self.hits(third), self.locations)
        self.assertIsNone(third['next_cursor'])
