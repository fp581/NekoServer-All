"""0.50.0, the wizard (P04): "How would you like to set up your app accounts?" is one page:
the one choice (strong passwords made for you, or your own logins) and a row for each
app. Nothing asks "who picks the login for this app?" app by app any more. The window
sends the page together; the terminal is asked one entry at a time.

That an app whose login could not be verified keeps its address off (nobody types
SECURED) is in tests/test_review_fixes.py (GatewayConfirmationTests)."""

import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account_store  # noqa: E402
import questions  # noqa: E402
import setup_progress  # noqa: E402

# Made when the tests run: nothing that looks like a real password is written here.
TYPED = 'typed-' + secrets.token_hex(10)


class Person:
    """What is typed in the terminal, and the page the window showed at each question."""

    def __init__(self, *answers):
        self.answers = iter(answers)
        self.asked = []
        self.pages = []

    def __call__(self, question=''):
        self.asked.append(question)
        self.pages.append(setup_progress.page_shown())
        return next(self.answers)


class AccountsPageTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.config = {'data_mount': folder.name, 'accounts': {'enabled': True}}
        self.saved = {}
        store = {'format': 1, 'mode': 'per-app', 'apps': {}}
        for name, stand_in in (
            ('load', lambda config: store),
            ('save', lambda config, value: self.saved.update(value)),
        ):
            patch = mock.patch.object(account_store, name, stand_in)
            patch.start()
            self.addCleanup(patch.stop)

    def collect(self, apps, prompt, passwords=None, said=None):
        account_store.collect(
            self.config,
            apps,
            prompt=prompt,
            password_prompt=passwords or mock.Mock(side_effect=AssertionError('a password was asked for')),
            output=(said if said is not None else []).append,
        )
        return self.saved

    def test_the_words_are_the_specifications(self):
        asked = questions.Q['logins_who']
        self.assertEqual(asked['title'], 'How would you like to set up your app accounts?')
        self.assertEqual(
            [label for _number, label in asked['choices']],
            ['Create strong passwords for me (recommended)', 'Choose my own logins'],
        )
        self.assertEqual(account_store.PAGE_TITLE, asked['title'])
        self.assertIn('stores its required credentials privately', account_store.PAGE_HINT)
        # Nothing asks app by app who picks its login, and no promise about email is made.
        self.assertNotIn('login_who', questions.Q)
        self.assertNotIn('No email is sent', questions.Q['login_email']['hint'])
        self.assertIn('Email delivery and password recovery depend on how', questions.Q['login_email']['hint'])

    def test_strong_passwords_for_me_is_one_answer_for_every_app(self):
        person = Person('1')
        said = []
        saved = self.collect(['jellyfin', 'immich', 'nextcloud'], person, said=said)
        self.assertEqual(len(person.asked), 1)  # the one choice; no app is asked about
        self.assertEqual(saved['mode'], 'generate')
        self.assertEqual(
            {app: entry['source'] for app, entry in saved['apps'].items()}, dict.fromkeys(saved['apps'], 'generate')
        )
        self.assertEqual(sorted(saved['apps']), ['immich', 'jellyfin', 'nextcloud'])
        # The page the window showed with that question: the choice and a row for each app.
        page = person.pages[0]
        self.assertEqual((page['form'], page['asking'], page['title']), ('accounts', 'mode', account_store.PAGE_TITLE))
        self.assertEqual(
            page['choices'], [['1', 'Create strong passwords for me (recommended)'], ['2', 'Choose my own logins']]
        )
        self.assertEqual(
            page['apps'],
            [
                {'id': 'jellyfin', 'name': 'Jellyfin administrator', 'email': False, 'existing': False},
                {'id': 'immich', 'name': 'Immich administrator', 'email': True, 'existing': False},
                {'id': 'nextcloud', 'name': 'Nextcloud administrator', 'email': False, 'existing': False},
            ],
        )
        # No password is in what the page carries, and none is said.
        made = [entry['password'] for entry in saved['apps'].values()]
        self.assertFalse([value for value in page['fields'].values() if value])
        self.assertFalse([line for line in said if any(password in line for password in made)])
        self.assertIsNone(setup_progress.page_shown())

    def test_my_own_logins_are_typed_app_by_app_on_the_same_page(self):
        person = Person('2', 'dana', 'dana@example.org')
        passwords = Person(TYPED, TYPED, TYPED, TYPED)
        saved = self.collect(['jellyfin', 'immich'], person, passwords)
        self.assertEqual(saved['mode'], 'custom')
        self.assertEqual(
            {app: (entry['username'], entry['password'], entry['source']) for app, entry in saved['apps'].items()},
            {'jellyfin': ('dana', TYPED, 'custom'), 'immich': ('dana@example.org', TYPED, 'custom')},
        )
        # Every entry was a field of the one page, never a page of its own.
        shown = person.pages + passwords.pages
        self.assertEqual({page['form'] for page in shown}, {'accounts'})
        self.assertEqual(
            [page['asking'] for page in passwords.pages],
            ['pass:jellyfin', 'again:jellyfin', 'pass:immich', 'again:immich'],
        )
        self.assertIn('What email address do you want to sign in to Immich administrator with?', person.asked[2])

    def test_the_window_sends_the_whole_page_and_nothing_more_is_asked(self):
        sent = {
            'user:jellyfin': 'dana', 'pass:jellyfin': TYPED, 'again:jellyfin': TYPED,
            'user:immich': 'dana@example.org', 'pass:immich': TYPED, 'again:immich': TYPED,
        }  # fmt: skip

        def window(question=''):
            # The window answers the first question (the choice) and sends every row with it.
            setup_progress._page['answers'].update(sent)
            return '2'

        saved = self.collect(['jellyfin', 'immich'], window)
        self.assertEqual(saved['apps']['immich']['username'], 'dana@example.org')
        self.assertEqual(saved['apps']['jellyfin']['password'], TYPED)

    def test_a_refused_entry_is_said_on_the_page_and_asked_again(self):
        person = Person('2', '9lives', 'dana')
        passwords = Person(TYPED, TYPED, TYPED, TYPED)
        said = []
        saved = self.collect(['jellyfin'], person, passwords, said)
        self.assertEqual(saved['apps']['jellyfin']['username'], 'dana')
        self.assertIn('Username: start with a letter; use 1-32 letters, digits, underscores or hyphens', said)
        # The second time round, the page says what was wrong, beside its rows.
        self.assertEqual(
            person.pages[2]['fields']['problem'],
            'Jellyfin administrator: ' + said[said.index(next(s for s in said if s.startswith('Username:')))],
        )

    def test_an_app_that_has_data_already_is_asked_for_its_login_whatever_the_choice(self):
        (Path(self.config['data_mount']) / 'appdata/jellyfin').mkdir(parents=True)
        (Path(self.config['data_mount']) / 'appdata/jellyfin/library.db').write_text('x')
        person = Person('1', 'mary.smith')
        passwords = Person(TYPED, TYPED)
        said = []
        saved = self.collect(['jellyfin', 'immich'], person, passwords, said)
        self.assertEqual(
            person.pages[0]['apps'][0],
            {'id': 'jellyfin', 'name': 'Jellyfin administrator', 'email': False, 'existing': True},
        )
        entry = saved['apps']['jellyfin']
        self.assertEqual((entry['username'], entry['allow_create'], entry['source']), ('mary.smith', False, 'custom'))
        self.assertEqual(saved['apps']['immich']['source'], 'generate')
        self.assertIn('  Existing app data: enter the current login; it will not be reset.', said)
        self.assertIn('not changed or reset', account_store.EXISTING)

    def test_a_store_from_before_reads_and_an_app_added_later_follows_its_choice(self):
        # "per-app" (asked app by app until 0.50.0) is your own logins; both names load.
        source = (ROOT / 'account_store.py').read_text()
        self.assertIn("not in ('generate', 'custom', 'per-app')", source)
        store = {'format': 1, 'mode': 'generate', 'apps': {'jellyfin': {'username': 'a', 'password': TYPED, 'state': 'verified', 'allow_create': True, 'source': 'generate'}}}  # fmt: skip
        with mock.patch.object(account_store, 'load', lambda config: store):
            person = Person()
            saved = self.collect(['jellyfin', 'navidrome'], person)
        self.assertEqual(person.asked, [])  # the choice was made the first time
        self.assertEqual(saved['apps']['navidrome']['source'], 'generate')


if __name__ == '__main__':
    unittest.main()
