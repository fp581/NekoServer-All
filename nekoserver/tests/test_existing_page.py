"""0.50.0, the wizard (P03): media you already have. The user's own case: a folder of films
on a storage box was given to setup, and Jellyfin could not see it once it ran. Now the
folder is asked for once, checked as the apps' account, mounted into the apps before they
start, and given a library of its own in Jellyfin. Nothing is copied.

The checks, the mounting and the library are existing_media's
(tests/test_existing_media.py); here: the page and where setup acts on the answer."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import existing_media  # noqa: E402
import existing_page  # noqa: E402
import media_libraries  # noqa: E402
import placement  # noqa: E402
import questions  # noqa: E402

FOUND = {
    'path': '/mnt/tank/films',
    'mount': '/mnt/tank',
    'share': 'nas:/tank',
    'fstype': 'nfs4',
    'read_only': False,
    'persistent': True,
    'add_to_fstab': None,
    'hardlinks': False,
    'new_files': None,
}


DRIVES = {'data_ssd': {'uuid': 'ssd-0001', 'fstype': 'ext4'}, 'media_hdd': {'uuid': 'hdd-0001', 'fstype': 'ext4'}}


def context(**settings):
    logs = []
    return SimpleNamespace(config=dict(settings), log=logs.append, logs=logs)


def typed(*answers):
    given = iter(answers)
    return lambda question='': next(given)


class PageTests(unittest.TestCase):
    def test_only_a_server_that_plays_films_is_asked_and_only_once(self):
        self.assertEqual(existing_page.kinds({}), [('movies', 'films'), ('tv', 'shows')])
        self.assertEqual(existing_page.kinds({'media_apps': {'movies': False}}), [])
        ctx = context(media_apps={'movies': False})
        never = mock.Mock(side_effect=AssertionError('asked'))
        self.assertFalse(existing_page.ask(ctx, never, never))
        # Answered no: saved as answered, and not asked again.
        ctx = context()
        self.assertTrue(existing_page.ask(ctx, typed('no'), lambda line: None))
        self.assertEqual(ctx.config, {existing_page.ASKED: True})
        self.assertFalse(existing_page.ask(ctx, never, never))
        # Left (Ctrl+D): nothing saved, asked again next time.
        ctx = context()
        self.assertFalse(existing_page.ask(ctx, mock.Mock(side_effect=EOFError), lambda line: None))
        self.assertNotIn(existing_page.ASKED, ctx.config)

    def test_a_folder_is_checked_said_back_and_saved_in_the_table(self):
        ctx = context()
        said = []
        with mock.patch.object(existing_media, 'check', return_value=dict(FOUND)) as check:
            # Yes; the films' folder; no shows (Enter alone).
            self.assertTrue(existing_page.ask(ctx, typed('yes', ' /mnt/tank/films ', ''), said.append))
        check.assert_called_once_with(ctx.config, 'movies', '/mnt/tank/films')
        self.assertIn(
            'Your films: /mnt/tank/films (nas:/tank). They are used where they are; nothing is copied or changed.', said
        )
        self.assertEqual(
            existing_media.extras(ctx.config),
            [
                {
                    'path': '/mnt/tank/films', 'holds': 'movies', 'use': 'in_place', 'new_files': False,
                    'mount': '/mnt/tank', 'share': 'nas:/tank', 'fstype': 'nfs4',
                }
            ],
        )  # fmt: skip

    def test_a_folder_that_cannot_be_used_says_why_and_is_asked_for_again(self):
        refused = existing_media.Refused('not_a_folder', path='/mnt/tank/films')
        said = []
        ctx = context()
        with mock.patch.object(existing_media, 'check', side_effect=[refused, dict(FOUND)]):
            entry = existing_page.folder_asked(
                ctx.config, 'movies', 'films', typed('/mnt/tank/films', '/mnt/tank/films'), said.append
            )
        self.assertEqual(entry['path'], '/mnt/tank/films')
        self.assertIn(str(refused), said)
        self.assertIn('/mnt/tank/films is not a folder on this PC.', str(refused))
        # Three folders that cannot be used: none is taken, and that is said.
        with mock.patch.object(existing_media, 'check', side_effect=refused):
            self.assertIsNone(
                existing_page.folder_asked(ctx.config, 'movies', 'films', typed('a', 'b', 'c'), said.append)
            )
        self.assertIn('No folder of films is used for now. Run setup again to give one.', said)

    def test_the_words(self):
        asked = questions.Q['existing_media']
        self.assertEqual(asked['title'], 'Do you have films or shows already that this server should use?')
        self.assertIn('nothing is copied, and nothing in your folder is changed', asked['hint'])
        self.assertEqual((asked['yes'], asked['no']), ('Yes, I have some', 'No, start empty'))
        self.assertIn('not its network address', questions.Q['existing_folder']['hint'])
        self.assertEqual(questions.Q['existing_folder']['skip_label'], 'I have none')


class TableTests(unittest.TestCase):
    def test_the_saved_table_follows_the_settings_and_keeps_only_the_folders(self):
        """A table is saved only because a folder is kept in it. Where things are kept
        still follows "locations": a place chosen later must not be frozen out."""
        entry = existing_media.entry_for('movies', FOUND)
        config = {'locations': {'photos': 'ssd'}, 'storage': DRIVES}
        config['placement'] = existing_media.with_choice(config, entry)['placement']
        self.assertEqual(config['placement']['photos']['data'], 'ssd')
        self.assertFalse(existing_page.keep_table(config))  # nothing changed since
        config['locations']['photos'] = 'media'
        self.assertTrue(existing_page.keep_table(config))
        self.assertEqual(config['placement']['photos']['data'], 'media')
        self.assertEqual(config['placement']['movies']['extra'], [entry])
        self.assertEqual(placement.table(config), config['placement'])
        # No table saved: none is made.
        plain = {'locations': {'photos': 'ssd'}, 'storage': DRIVES}
        self.assertFalse(existing_page.keep_table(plain))
        self.assertNotIn('placement', plain)


class SetupTests(unittest.TestCase):
    def with_folder(self):
        config = {}
        config['placement'] = existing_media.with_choice(config, existing_media.entry_for('movies', FOUND))['placement']
        return context(**config)

    def test_the_folder_is_mounted_before_the_apps_start_with_the_line_that_makes_it_permanent(self):
        ctx = self.with_folder()
        line = 'nas:/tank /mnt/tank nfs4 hard,nofail 0 0'
        mounted = [{'holds': 'movies', 'path': '/mnt/tank/films', 'mounted': True, 'reason': None}]
        with (
            mock.patch.object(existing_media, 'check', return_value={**FOUND, 'add_to_fstab': line}),
            mock.patch.object(existing_media, 'use_in_place', return_value=mounted) as use,
        ):
            self.assertEqual(existing_page.mount(ctx), mounted)
        self.assertEqual(use.call_args.args[1], [line])
        self.assertIn('Your own folder (movies): /mnt/tank/films is used where it is', ctx.logs)
        # The media step calls it before any app is started.
        source = (ROOT / 'modules/media.py').read_text()
        self.assertLess(source.index('existing_page.mount(context)'), source.index('compose_up('))

    def test_a_share_that_is_away_is_said_and_never_fails_the_step(self):
        ctx = self.with_folder()
        away = [
            {'holds': 'movies', 'path': '/mnt/tank/films', 'mounted': False, 'reason': 'nas:/tank is not connected'}
        ]
        gone = existing_media.Refused('not_a_folder', path='/mnt/tank/films')
        with (
            mock.patch.object(existing_media, 'check', side_effect=gone),
            mock.patch.object(existing_media, 'use_in_place', return_value=away) as use,
        ):
            existing_page.mount(ctx)
        self.assertEqual(use.call_args.args[1], [])
        self.assertIn(
            'Your own folder (movies): /mnt/tank/films is not there now: nas:/tank is not connected', ctx.logs
        )
        # No folder: nothing is touched.
        with mock.patch.object(existing_media, 'use_in_place', side_effect=AssertionError('called')):
            self.assertEqual(existing_page.mount(context()), [])

    def test_jellyfin_gets_a_library_for_it_with_its_signed_in_client(self):
        self.assertIsNone(existing_page.libraries({}))
        ctx = self.with_folder()
        more = existing_page.libraries(ctx.config, apply=True)
        with mock.patch.object(
            existing_media, 'jellyfin_libraries', return_value=[{'check': 'x', 'status': 'pass'}]
        ) as made:
            self.assertEqual(more('CLIENT'), [{'check': 'x', 'status': 'pass'}])
        made.assert_called_once_with('CLIENT', ctx.config, True)
        # media_libraries.configure calls it once Jellyfin is signed in to, and adds its rows.
        client = mock.Mock()
        with (
            mock.patch.object(media_libraries.account_store, 'get', return_value=None),
            mock.patch.object(
                media_libraries, 'jellyfin', return_value=([{'check': 'jellyfin', 'status': 'pass'}], 'id', {})
            ),
        ):
            rows = media_libraries.configure(
                '/srv/nekoserver',
                '/mnt/media',
                False,
                ('jellyfin',),
                factory=lambda app: client,
                more=lambda jf: ['MORE'],
            )
        self.assertEqual(rows, [{'check': 'jellyfin', 'status': 'pass'}, 'MORE'])


if __name__ == '__main__':
    unittest.main()
