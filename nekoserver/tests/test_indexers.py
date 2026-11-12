"""0.50.0, item 10: adding download sources (Prowlarr's indexers) made easy. Several are
chosen at once ("1 4 7", "1-5", or ticked in the setup window's list, which has a search
box); the ones that need no login go in at once, each one that does gets a small form of
its own and can be skipped, and one Prowlarr refuses is said with its reason and can be
tried again while the rest still go in. A "Recommended" group comes from a data file
whose names the person who ships NekoServer approves."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import guided_setup  # noqa: E402
import neko_common  # noqa: E402
import setup_progress  # noqa: E402

spec = importlib.util.spec_from_file_location('media_connect_indexers', ROOT / 'media-connect.py')
connect = importlib.util.module_from_spec(spec)
spec.loader.exec_module(connect)


def field(name, required=False, label=None):
    return {'name': name, 'label': label or name, 'required': required, 'value': None}


# Prowlarr's catalogue, as far as this needs it.
CATALOGUE = [
    {'name': 'Alpha', 'implementation': 'Cardigann', 'definitionName': 'alpha', 'privacy': 'public', 'language': 'en-US',
     'protocol': 'torrent', 'description': 'Open films', 'fields': [field('definitionFile')]},
    {'name': 'Beta', 'implementation': 'Cardigann', 'definitionName': 'beta', 'privacy': 'private', 'language': 'en-US',
     'protocol': 'torrent', 'fields': [field('baseUrl'), field('apiKey', True, 'API Key')]},
    {'name': 'Gamma', 'implementation': 'Cardigann', 'definitionName': 'gamma', 'privacy': 'public', 'fields': []},
    {'name': 'Delta', 'implementation': 'Newznab', 'definitionName': 'delta', 'privacy': 'semiPrivate', 'protocol': 'usenet',
     'fields': [field('username'), field('password')]},
    {'name': 'Epsilon', 'implementation': 'Cardigann', 'definitionName': 'epsilon', 'fields': []},
]  # fmt: skip
PROFILES = [{'id': 1, 'name': 'Standard'}]


class Prowlarr:
    """A stand-in for Prowlarr's API: the catalogue, what is configured, and which names
    it refuses (a name mapped to how many times it refuses before it accepts)."""

    def __init__(self, configured=(), refuses=None):
        self.configured = [dict(row) for row in configured]
        self.refuses = dict(refuses or {})
        self.posted = []

    def __call__(self, app, key, endpoint, method='GET', payload=None, timeout=12):
        if method == 'POST':
            self.posted.append(payload)
            name = payload['name']
            if self.refuses.get(name, 0) > 0:
                self.refuses[name] -= 1
                raise RuntimeError(f'HTTP 400: Unable to connect to indexer {name}, check the API key')
            self.configured.append({'name': name, 'implementation': payload['implementation']})
            return {'name': name}
        return {'/indexer/schema': CATALOGUE, '/indexer': list(self.configured), '/appprofile': PROFILES}[endpoint]


class ChoosingTests(unittest.TestCase):
    def test_numbers_typed_together(self):
        self.assertEqual(connect.chosen_numbers('1 4 7', 9), [1, 4, 7])
        self.assertEqual(connect.chosen_numbers('1,4, 7', 9), [1, 4, 7])
        self.assertEqual(connect.chosen_numbers('1-5', 9), [1, 2, 3, 4, 5])
        self.assertEqual(connect.chosen_numbers('3 1-2 3 2-4', 9), [3, 1, 2, 4])  # once each, in the order typed
        self.assertEqual(connect.chosen_numbers('', 9), [])
        self.assertEqual(connect.chosen_numbers('r 2', 9, [5, 7]), [5, 7, 2])
        for wrong in ('0', '10', '5-3', 'two', '1-', '-3', '1-x', 'r'):
            self.assertIsNone(connect.chosen_numbers(wrong, 9), wrong)

    def chosen(self, typed, names=()):
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)
        answers, pages, said = iter(typed), [], []

        def ask(prompt):
            said.append(prompt)
            pages.append(setup_progress.page_shown())
            return next(answers)

        with mock.patch('builtins.print', side_effect=lambda *words: said.append(' '.join(map(str, words)))):
            found = connect.chosen_indexers(CATALOGUE, list(names), ask)
        return [row['name'] for row in found], pages, said

    def test_several_are_chosen_at_once_in_the_terminal(self):
        names, _, said = self.chosen(['', '1 3-4'])
        self.assertEqual(names, ['Alpha', 'Gamma', 'Delta'])
        self.assertIn('   1. Alpha  (public, en-US, torrent. Open films)', said)
        self.assertIn('   2. Beta  (private: needs an account, en-US, torrent)', said)
        self.assertIn('   4. Delta  (needs a free account, usenet)', said)
        self.assertIn('Numbers to add, such as 1 4 7 or 1-5 (Enter alone skips): ', said)
        self.assertIn(connect.INDEXER_ADVICE, said)
        # A wrong answer is asked again; Enter alone, or Ctrl+D, adds none.
        names, _, said = self.chosen(['', '9', '2'])
        self.assertEqual(names, ['Beta'])
        self.assertIn('Type numbers from 1 to 5, such as 1 4 7 or 1-5, or press Enter to skip.', said)
        self.assertEqual(self.chosen(['', ''])[0], [])
        self.assertEqual(self.chosen(['nothing like it'])[0], [])  # the filter matched none

    def test_the_filter_narrows_the_list_and_its_numbers(self):
        names, pages, _ = self.chosen(['a', '1-3'])  # Alpha, Beta, Gamma, Delta hold an "a"
        self.assertEqual(names, ['Alpha', 'Beta', 'Gamma'])
        self.assertEqual([item['label'] for item in pages[1]['items']], ['Alpha', 'Beta', 'Gamma', 'Delta'])

    def test_the_window_gets_the_whole_list_to_tick(self):
        _, pages, _ = self.chosen(['', '2'])
        self.assertEqual(pages[0]['title'], 'Looking for a download source by name?')  # the filter
        page = pages[1]
        self.assertEqual((page['form'], page['multiple'], page['asking']), ('pick', True, 'choice'))
        self.assertEqual(page['what'], 'Download sources (indexers) to add')
        self.assertEqual(len(page['items']), 5)
        self.assertEqual(page['items'][1], {'label': 'Beta', 'note': 'private: needs an account, en-US, torrent'})
        self.assertEqual(page['recommended'], [])
        self.assertEqual(page['advice'], 'Use only providers and content you are authorized to access.')

    def test_the_recommended_ones_are_one_choice(self):
        names, pages, said = self.chosen(['', 'r'], names=['gamma', 'alpha', 'gone since'])
        self.assertEqual(names, ['Alpha', 'Gamma'])  # the one Prowlarr no longer has is left out
        self.assertEqual(pages[1]['recommended'], [1, 3])
        self.assertIn('Recommended (r): Alpha, Gamma', said)
        self.assertIn('Numbers to add, such as 1 4 7 or 1-5, or r for the recommended ones (Enter alone skips): ', said)
        # Without a list there is no such choice.
        names, _, said = self.chosen(['', 'r', ''])
        self.assertEqual(names, [])
        self.assertFalse(any(line.startswith('Recommended') for line in said))


class RecommendedFileTests(unittest.TestCase):
    def test_the_file_is_data_with_a_list_of_names(self):
        value = json.loads(connect.RECOMMENDED_FILE.read_text())
        self.assertEqual(set(value), {'_about', 'indexers'})
        self.assertIsInstance(value['indexers'], list)
        for name in value['indexers']:
            self.assertTrue(isinstance(name, str) and name.strip() == name and name, name)
        self.assertEqual(len(value['indexers']), len({name.casefold() for name in value['indexers']}))
        self.assertIn('approved', value['_about'])

    def test_a_missing_or_broken_file_recommends_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'recommended-indexers.json'
            self.assertEqual(connect.recommended_names(path), [])
            for broken in ('{', '[]', '{"indexers": "all"}', '{"indexers": [7, "", "  ", "Alpha"]}'):
                path.write_text(broken)
                with self.subTest(broken=broken):
                    self.assertIn(connect.recommended_names(path), ([], ['alpha']))
            path.write_text('{"indexers": ["Alpha", "GAMMA"]}')
            self.assertEqual(connect.recommended_names(path), ['alpha', 'gamma'])
        self.assertEqual(connect.recommended_among(CATALOGUE, ['gamma', 'alpha', 'not in the catalogue']), [1, 3])

    def test_the_names_are_matched_by_name_or_definition(self):
        catalogue = [{'name': 'Internet Archive', 'definitionName': 'internetarchive'}, {'name': 'Other'}]
        self.assertEqual(connect.recommended_among(catalogue, ['internetarchive']), [1])
        self.assertEqual(connect.recommended_among(catalogue, ['internet archive']), [1])


class AddingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)

    def add(self, picked, prowlarr, typed=(), retry=(), finished=False):
        """configure_indexers with these catalogue entries chosen; `typed` answers the
        indexers' own fields, `retry` the "Retry this step" questions."""
        answers, retries, said, pages = iter(typed), iter(retry), [], []

        def ask(prompt):
            pages.append(setup_progress.page_shown())
            said.append(prompt)
            answer = next(answers)
            if answer is EOFError:
                raise EOFError
            return answer

        chosen = [row for row in CATALOGUE if row['name'] in picked]
        with (
            mock.patch.object(connect, 'app_key', return_value='k'),
            mock.patch.object(connect, 'request', prowlarr),
            mock.patch.object(connect, 'standard_indexers', return_value=chosen),
            mock.patch.object(connect, 'provider_account_ready', return_value=True),
            mock.patch.object(connect, 'finished_in_prowlarr', return_value=finished) as by_hand,
            mock.patch.object(
                guided_setup,
                'retry_question',
                side_effect=lambda rows, leave='': (said.append(rows[0]), next(retries))[1],
            ),
            mock.patch('builtins.input', ask),
            mock.patch.object(connect.getpass, 'getpass', ask),
            mock.patch('builtins.print', side_effect=lambda *words: said.append(' '.join(map(str, words)))),
        ):
            try:
                connect.configure_indexers(Path(self.tmp.name), True, {'data_mount': self.tmp.name})
                error = None
            except RuntimeError as exc:
                error = str(exc)
        return error, said, pages, by_hand

    def remembered(self):
        return neko_common.optional_answers(self.tmp.name).get('indexers')

    def test_the_ones_that_need_no_login_go_in_at_once(self):
        prowlarr = Prowlarr()
        error, said, pages, _ = self.add(['Alpha', 'Gamma', 'Epsilon'], prowlarr)
        self.assertIsNone(error)
        self.assertEqual([entry['name'] for entry in prowlarr.posted], ['Alpha', 'Gamma', 'Epsilon'])
        self.assertTrue(all(entry['enable'] and entry['appProfileId'] == 1 for entry in prowlarr.posted))
        self.assertEqual(pages, [])  # nothing was asked
        self.assertIn('Added 3: Alpha, Gamma, Epsilon', said)
        self.assertEqual(self.remembered(), 'Alpha, Gamma, Epsilon')

    def test_one_that_needs_a_login_gets_its_own_small_form(self):
        prowlarr = Prowlarr()
        key = 'typed-' + 'k' * 12
        error, said, pages, _ = self.add(['Alpha', 'Beta'], prowlarr, typed=['', key])
        self.assertIsNone(error)
        # Beta's two values, as one page in the window: its address plain, its key hidden.
        page = pages[0]
        self.assertEqual((page['form'], page['what']), ('fields', 'Beta needs these'))
        self.assertEqual(
            page['rows'],
            [
                {'key': 'baseUrl', 'label': 'baseUrl', 'secret': False},
                {'key': 'apiKey', 'label': 'API Key', 'secret': True},
            ],
        )
        self.assertEqual(page['fields'], {'baseUrl': '', 'apiKey': ''})  # never the values
        beta = prowlarr.posted[1]
        self.assertEqual({f['name']: f['value'] for f in beta['fields']}, {'baseUrl': None, 'apiKey': key})
        self.assertNotIn(key, json.dumps(said))
        self.assertNotIn(key, json.dumps([{k: v for k, v in e.items() if k != 'at'} for e in setup_progress.events()]))

    def test_one_left_for_later_does_not_cost_the_others(self):
        prowlarr = Prowlarr()
        error, said, _, _ = self.add(['Beta', 'Alpha', 'Delta'], prowlarr, typed=[EOFError, 'user', 'pass-' + 'w' * 9])
        self.assertIsNone(error)
        self.assertEqual([entry['name'] for entry in prowlarr.posted], ['Alpha', 'Delta'])
        self.assertIn('Beta: not added (left for later)', said)
        self.assertEqual(self.remembered(), 'Alpha, Delta')

    def test_one_prowlarr_refuses_is_said_and_the_rest_still_go_in(self):
        prowlarr = Prowlarr(refuses={'Gamma': 9})
        error, said, _, by_hand = self.add(['Alpha', 'Gamma', 'Epsilon'], prowlarr, retry=['no'])
        self.assertEqual(error, 'Prowlarr refused Gamma; the others were added')
        self.assertEqual([row['name'] for row in prowlarr.configured], ['Alpha', 'Epsilon'])
        row = next(line for line in said if isinstance(line, dict))
        self.assertEqual(row['title'], 'Gamma')
        self.assertIn('Prowlarr refused it: HTTP 400: Unable to connect to indexer Gamma', row['reason'])
        by_hand.assert_called_once()  # then it can be finished in Prowlarr's own page
        self.assertEqual(self.remembered(), 'Alpha, Epsilon')

    def test_a_retry_asks_its_values_again_and_may_work(self):
        prowlarr = Prowlarr(refuses={'Beta': 1})
        error, said, _, by_hand = self.add(
            ['Beta'], prowlarr, typed=['', 'wrong-key-000', '', 'right-key-000'], retry=['yes']
        )
        self.assertIsNone(error)
        self.assertEqual(
            [f['value'] for entry in prowlarr.posted for f in entry['fields'] if f['name'] == 'apiKey'],
            ['wrong-key-000', 'right-key-000'],
        )
        by_hand.assert_not_called()
        self.assertEqual(self.remembered(), 'Beta')

    def test_a_refused_one_is_retried_three_times_at_most(self):
        prowlarr = Prowlarr(refuses={'Gamma': 9})
        error, _, _, by_hand = self.add(['Gamma'], prowlarr, retry=['yes', 'yes', 'yes'])
        self.assertEqual(len(prowlarr.posted), 4)  # the first try and three retries
        self.assertEqual(error, 'Prowlarr refused Gamma; the others were added')
        # Finished by hand in Prowlarr afterwards: it counts as added.
        prowlarr = Prowlarr(refuses={'Gamma': 9})
        error, _, _, _ = self.add(['Gamma'], prowlarr, retry=['no'], finished=True)
        self.assertIsNone(error)
        self.assertEqual(self.remembered(), 'Gamma')

    def test_one_already_there_is_not_added_twice(self):
        prowlarr = Prowlarr(configured=[{'name': 'Alpha', 'implementation': 'Cardigann'}])
        error, said, _, _ = self.add(['Alpha', 'Alpha', 'Gamma', 'Gamma'], prowlarr)
        self.assertIsNone(error)
        self.assertEqual([entry['name'] for entry in prowlarr.posted], ['Gamma'])
        self.assertIn('Alpha: already configured; skipping duplicate', said)

    def test_none_chosen_is_remembered_as_such(self):
        error, _, _, _ = self.add([], Prowlarr())
        self.assertIsNone(error)
        self.assertEqual(self.remembered(), 'none chosen')

    def test_a_provider_account_not_ready_skips_only_that_one(self):
        prowlarr = Prowlarr()
        with mock.patch.object(connect, 'standard_indexers', return_value=[CATALOGUE[1], CATALOGUE[0]]):
            with (
                mock.patch.object(connect, 'app_key', return_value='k'),
                mock.patch.object(connect, 'request', prowlarr),
                mock.patch.object(
                    connect, 'provider_account_ready', side_effect=lambda entry, config: entry['name'] != 'Beta'
                ),
                mock.patch('builtins.print'),
            ):
                connect.configure_indexers(Path(self.tmp.name), True, {'data_mount': self.tmp.name})
        self.assertEqual([entry['name'] for entry in prowlarr.posted], ['Alpha'])

    def test_which_values_an_indexer_asks_for(self):
        entry = {
            'fields': [
                field('definitionFile'),  # not required, not a login: never asked
                field('baseUrl'),
                field('cookie'),
                field('sort', True, 'Sort by'),
                {'name': 'tags', 'type': 'tag', 'required': True},
                {'name': 'secret', 'hidden': True, 'required': True},
                {'name': ''},
            ]
        }
        self.assertEqual(
            connect.asked_fields(entry),
            [
                {'key': 'baseUrl', 'label': 'baseUrl', 'secret': False},
                {'key': 'cookie', 'label': 'cookie', 'secret': True},
                {'key': 'sort', 'label': 'Sort by', 'secret': False},
            ],
        )


if __name__ == '__main__':
    unittest.main()


class NothingAskedAfterTheApprovalTests(unittest.TestCase):
    """The gate's J01 run stopped after "Install selected apps" at "Looking for a download
    source by name?": the recommended sources are added without asking instead."""

    def test_the_recommended_ones_while_prowlarr_has_none(self):
        schemas = [{'name': 'The Pirate Bay'}, {'name': 'Other'}, {'name': 'YTS'}]
        with mock.patch('builtins.input', side_effect=AssertionError('asked')), mock.patch('builtins.print'):
            picked = connect.standard_indexers(schemas, [], ['the pirate bay', 'yts', 'eztv'])
        self.assertEqual([s['name'] for s in picked], ['The Pirate Bay', 'YTS'])

    def test_sources_of_its_own_are_kept_as_they_are(self):
        with mock.patch('builtins.print'):
            self.assertEqual(connect.standard_indexers([{'name': 'YTS'}], [{'name': 'Mine'}], ['yts']), [])
