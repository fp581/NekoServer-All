"""0.50.0, the wizard (P03): media you already have. The user's own case: a folder of films
on a NAS was given to setup, and Jellyfin could not see it once it ran. Now the
folder is asked for once, checked as the apps' account, mounted into the apps before they
start, and given a library of its own in Jellyfin (music: in Navidrome). The person may
also have new ones saved there, or have the folder copied into the library instead.

The checks, the mounting and the library are existing_media's
(tests/test_existing_media.py); here: the page and where setup acts on the answer."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import existing_media  # noqa: E402
import existing_page  # noqa: E402
import media_libraries  # noqa: E402
import neko_common  # noqa: E402
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
    """The answers, one per question; then Ctrl+D (EOFError), as a person leaving."""
    given = iter(answers)

    def answer(question=''):
        try:
            return next(given)
        except StopIteration:
            raise EOFError from None

    return answer


class PageTests(unittest.TestCase):
    def test_only_a_server_that_plays_films_is_asked_and_only_once(self):
        self.assertEqual(existing_page.kinds({}), [('movies', 'films'), ('tv', 'shows'), ('music', 'music')])
        self.assertEqual(existing_page.kinds({'media_apps': {'movies': False}}), [('music', 'music')])
        self.assertEqual(existing_page.kinds({'media_apps': {'movies': False, 'music': False}}), [])
        ctx = context(media_apps={'movies': False, 'music': False})
        never = mock.Mock(side_effect=AssertionError('asked'))
        self.assertFalse(existing_page.ask(ctx, never, never))
        # Answered no: saved as answered, and not asked again.
        ctx = context()
        self.assertTrue(existing_page.ask(ctx, typed('no'), lambda line: None))
        self.assertEqual(ctx.config, {existing_page.ASKED: True, existing_page.ANSWERED: ['movies', 'tv', 'music']})
        self.assertFalse(existing_page.ask(ctx, never, never))
        # Left (Ctrl+D): nothing saved, asked again next time.
        ctx = context()
        self.assertFalse(existing_page.ask(ctx, mock.Mock(side_effect=EOFError), lambda line: None))
        self.assertNotIn(existing_page.ASKED, ctx.config)

    def test_a_kind_whose_folders_were_all_refused_is_asked_about_again(self):
        """Review of 0.51.2 (P2): three folders that could not be used ended with "Run setup
        again to give one", but the question was saved as answered, so it never came."""
        refused = existing_media.Refused('not_a_folder', path='/mnt/x')
        ctx = context(media_apps={'music': False})
        said = []
        # Films: three folders refused. Shows: Enter alone (none of those), which is an answer.
        with mock.patch.object(existing_media, 'check', side_effect=refused):
            self.assertTrue(existing_page.ask(ctx, typed('yes', 'a', 'b', 'c', ''), said.append))
        self.assertIn('No folder of films is used for now. Run setup again to give one.', said)
        self.assertEqual(ctx.config[existing_page.ANSWERED], ['tv'])
        # The next run asks again, about films only.
        keys = []
        answers = iter(['yes', ''])

        def asked(text):
            keys.append(text)
            return next(answers)

        self.assertTrue(existing_page.ask(ctx, asked, said.append))
        self.assertEqual(len(keys), 2)
        self.assertIn('films', keys[1])
        self.assertNotIn('shows', keys[1])
        self.assertEqual(ctx.config[existing_page.ANSWERED], ['movies', 'tv'])

    def test_a_kind_switched_on_later_is_asked_about(self):
        ctx = context(media_apps={'music': False})
        self.assertTrue(existing_page.ask(ctx, typed('no'), lambda line: None))
        self.assertEqual(ctx.config[existing_page.ANSWERED], ['movies', 'tv'])
        never = mock.Mock(side_effect=AssertionError('asked'))
        self.assertFalse(existing_page.ask(ctx, never, never))
        # Music switched on since: the question comes for it.
        ctx.config['media_apps'] = {'music': True}
        self.assertTrue(existing_page.ask(ctx, typed('no'), lambda line: None))
        self.assertEqual(ctx.config[existing_page.ANSWERED], ['movies', 'tv', 'music'])
        # Settings from before the list: every kind this server plays now counts as answered.
        old = context(media_apps={'music': False}, **{existing_page.ASKED: True})
        self.assertTrue(existing_page.ask(old, never, never))
        self.assertEqual(old.config[existing_page.ANSWERED], ['movies', 'tv'])

    def test_a_folder_is_checked_said_back_and_saved_in_the_table(self):
        ctx = context()
        said = []
        with mock.patch.object(existing_media, 'check', return_value=dict(FOUND)) as check:
            # Yes; the films' folder, used where it is, new ones not saved there; no shows, no music.
            answers = typed('yes', ' /mnt/tank/films ', '', 'no', '', '')
            self.assertTrue(existing_page.ask(ctx, answers, said.append))
        check.assert_called_once_with(ctx.config, 'movies', '/mnt/tank/films', False)
        self.assertIn(
            'Your films: /mnt/tank/films (nas:/tank). They are used where they are; nothing is copied or changed.', said
        )
        self.assertEqual(
            existing_media.extras(ctx.config),
            [
                {
                    'path': '/mnt/tank/films', 'holds': 'movies', 'use': 'in_place', 'new_files': False,
                    'mount': '/mnt/tank', 'share': 'nas:/tank', 'fstype': 'nfs4', 'slot': 'movies',
                }
            ],
        )  # fmt: skip

    def test_a_music_folder_on_a_drive_of_this_pc_is_not_called_a_share(self):
        """A local drive's "share" is its device: the page said "Your music: /mnt/big/music
        (/dev/sdd1)" and asked "Your music on the network share can be copied each night"
        about a folder on a drive of this PC. A NAS's share is still named and asked about."""
        local = {**FOUND, 'path': '/mnt/big/music', 'mount': '/mnt/big', 'share': '/dev/sdd1', 'fstype': 'ext4'}
        for found, asked in ((local, False), ({**FOUND, 'path': '/mnt/tank/music'}, True)):
            ctx = context(media_apps={'movies': False, 'music': True, 'music_downloads': False})
            said, keys = [], []
            real_typed = existing_page.typed

            def noting(key, prompt, **values):
                keys.append(key)
                return real_typed(key, prompt, **values)

            with (
                mock.patch.object(existing_media, 'check', return_value=dict(found)),
                mock.patch.object(existing_page, 'typed', side_effect=noting),
            ):
                existing_page.ask(ctx, typed('yes', found['path'], '1', '1'), said.append)
            self.assertEqual('existing_backup' in keys, asked)
            self.assertNotIn('/dev/sdd1', ' '.join(said))
            self.assertEqual('(nas:/tank)' in ' '.join(said), asked)

    def test_a_folder_that_cannot_be_used_says_why_and_is_asked_for_again(self):
        refused = existing_media.Refused('not_a_folder', path='/mnt/tank/films')
        said = []
        ctx = context()
        with mock.patch.object(existing_media, 'check', side_effect=[refused, dict(FOUND)]):
            found = existing_page.folder_found(
                ctx.config, 'movies', 'films', (typed('/mnt/tank/films', '/mnt/tank/films'), said.append)
            )
        self.assertEqual(found['path'], '/mnt/tank/films')
        self.assertIn(str(refused), said)
        self.assertIn('/mnt/tank/films is not a folder on this PC.', str(refused))
        # Three folders that cannot be used: none is taken, and that is said.
        with mock.patch.object(existing_media, 'check', side_effect=refused):
            self.assertIsNone(
                existing_page.folder_found(ctx.config, 'movies', 'films', (typed('a', 'b', 'c'), said.append))
            )
        self.assertIn('No folder of films is used for now. Run setup again to give one.', said)

    def test_a_folder_dragged_in_or_copied_from_a_shell_is_the_folder(self):
        """Deep check 481: a folder dragged into the terminal comes in quotes, one copied
        from a shell has its spaces escaped, and ~ is the person's home: each was "is not a
        folder on this PC" (setup runs as root, through sudo)."""
        import pwd

        with tempfile.TemporaryDirectory() as d:
            folder = os.path.join(d, "Ana's Films")
            os.mkdir(folder)
            ctx = context()
            for given in (f'"{folder}"', f"'{folder}' ", folder.replace(' ', '\\ ').replace("'", "\\'")):
                with self.subTest(given=given):
                    seen = []
                    with mock.patch.object(existing_media, 'check', side_effect=lambda c, k, f, n: seen.append(f)):
                        existing_page.folder_found(ctx.config, 'movies', 'films', (typed(given), print))
                    self.assertEqual(seen, [folder])
                    self.assertTrue(os.path.isdir(seen[0]))
        home = pwd.getpwnam('nobody').pw_dir.rstrip('/')
        self.assertEqual(neko_common.typed_folder('~/Videos', {'SUDO_USER': 'nobody'}), home + '/Videos')
        self.assertEqual(neko_common.typed_folder('/mnt/tank/films'), '/mnt/tank/films')
        self.assertEqual(neko_common.typed_folder('~other/x', {'SUDO_USER': 'nobody'}), '~other/x')

    def test_the_words(self):
        asked = questions.Q['existing_media']
        self.assertEqual(asked['title'], 'Do you already have films, shows or music that this server should use?')
        self.assertIn('Nothing in your folder is changed', asked['hint'])
        self.assertEqual(
            [label for _, label in questions.Q['existing_use']['choices']],
            ['Use the files where they are (nothing is copied)', 'Copy them into the NekoServer library'],
        )
        self.assertEqual(questions.Q['existing_new_files']['title'], 'Save new {what} here too?')
        self.assertIn('Your existing files are not moved', questions.Q['existing_new_files']['hint'])
        self.assertEqual((asked['yes'], asked['no']), ('Yes, I have some', 'No, start empty'))
        self.assertIn('not its network address', questions.Q['existing_folder']['hint'])
        self.assertEqual(questions.Q['existing_folder']['skip_label'], 'I have none')


def writable(found, ok=True, **more):
    answer = {'ok': True, 'note': 'New films are downloaded first and then copied into the folder.'}
    if not ok:
        answer = {
            'ok': False,
            'code': 'read_only',
            'reason': 'It is read-only here, so new films cannot be saved there.',
        }
    return {**found, **more, 'new_files': answer}


class NewFilesTests(unittest.TestCase):
    """ "Save new ones here too" (the user: the server should use that location for the
    rest of the library's downloads as well)."""

    def ask(self, checks, *answers, **settings):
        ctx, said = context(storage=dict(DRIVES), **settings), []
        with mock.patch.object(existing_media, 'check', side_effect=checks) as check:
            existing_page.one_kind(ctx.config, 'movies', 'films', (typed(*answers), said.append))
        return ctx.config, said, check

    def test_yes_saves_the_folder_as_the_place_for_new_ones(self):
        config, said, check = self.ask([dict(FOUND), writable(FOUND)], '/mnt/tank/films', '1', 'yes')
        self.assertEqual(check.call_args_list[1], mock.call(config, 'movies', '/mnt/tank/films', new_files=True))
        self.assertIn('New films are downloaded first and then copied into the folder.', said)
        entry = existing_media.extras(config)[0]
        self.assertEqual((entry['use'], entry['new_files']), ('in_place', True))
        self.assertEqual(existing_media.new_files_folder(config, 'movies'), '/data/existing/movies')

    def test_it_is_not_asked_on_a_server_that_downloads_nothing(self):
        off = {'media_apps': {'movies_downloads': False}}
        config, said, check = self.ask([dict(FOUND)], '/mnt/tank/films', '1', **off)
        self.assertFalse(existing_media.extras(config)[0]['new_files'])
        self.assertFalse(existing_page.downloads(off, 'tv'))
        self.assertTrue(existing_page.downloads(off, 'music'))

    def test_a_read_only_folder_is_still_used_and_another_place_is_asked_for(self):
        # The library: the folder is used read-only, nothing more.
        config, said, _ = self.ask([dict(FOUND), writable(FOUND, ok=False)], '/mnt/tank/films', '1', 'yes', '1')
        self.assertIn('It is read-only here, so new films cannot be saved there.', said)
        self.assertIn('New films are saved in the NekoServer library.', said)
        self.assertEqual([(e['use'], e['new_files']) for e in existing_media.extras(config)], [('in_place', False)])
        self.assertEqual(existing_media.new_files_folder(config, 'movies'), '/data/movies')
        # Another folder of the person's: checked for writing, and the one place for new films.
        other = writable(FOUND, path='/mnt/big/new-films', mount='/mnt/big', share='/dev/sdd1', fstype='ext4')
        config, said, check = self.ask(
            [dict(FOUND), writable(FOUND, ok=False), other], '/mnt/tank/films', '1', 'yes', '2', '/mnt/big/new-films'
        )
        self.assertEqual(check.call_args_list[2], mock.call(config, 'movies', '/mnt/big/new-films', True))
        self.assertEqual(
            [(e['path'], e['use'], e['new_files']) for e in existing_media.extras(config)],
            [('/mnt/tank/films', 'in_place', False), ('/mnt/big/new-films', 'new_files', True)],
        )
        self.assertEqual(existing_media.new_files_folder(config, 'movies'), '/data/existing/movies-new')


class MoreFoldersTests(unittest.TestCase):
    """More than one folder of a kind (0.52, the owner's decision 1): setup asks for any
    other folder after the first, with the limit and the words of the dashboard's page."""

    def found(self, path):
        return {**FOUND, 'path': path}

    def ask(self, checks, *answers, config=None):
        ctx, said = context(storage=dict(DRIVES), media_apps={'movies_downloads': False}, **(config or {})), []
        keys = []
        real_typed = existing_page.typed

        def noting(key, prompt, **values):
            keys.append((key, values))
            return real_typed(key, prompt, **values)

        with (
            mock.patch.object(existing_media, 'check', side_effect=checks),
            mock.patch.object(existing_page, 'typed', side_effect=noting),
        ):
            existing_page.one_kind(ctx.config, 'movies', 'films', (typed(*answers), said.append))
        return ctx.config, said, keys

    def test_any_other_folder_is_asked_for_until_enter_alone(self):
        checks = [self.found('/mnt/tank/films'), self.found('/mnt/tank/old-films'), self.found('/mnt/tank/kids')]
        config, said, keys = self.ask(
            checks, '/mnt/tank/films', '1', '/mnt/tank/old-films', '1', '/mnt/tank/kids', '2', ''
        )
        self.assertEqual(
            [(e['path'], e['slot']) for e in existing_media.extras(config)],
            [('/mnt/tank/films', 'movies'), ('/mnt/tank/old-films', 'movies-2')],
        )
        self.assertEqual(config[existing_page.COPIES], [{'holds': 'movies', 'path': '/mnt/tank/kids'}])
        more = [values for key, values in keys if key == 'existing_more_folder']
        self.assertEqual(more, [{'what': 'films', 'cap': existing_media.FOLDERS_PER_KIND}] * 3)
        words = questions.words('existing_more_folder', what='films', cap=existing_media.FOLDERS_PER_KIND)
        self.assertEqual(words['title'], 'Is any other folder holding your films?')
        self.assertIn(f'up to {existing_media.FOLDERS_PER_KIND} folders', words['hint'])
        self.assertEqual(questions.Q['existing_more_folder']['skip_label'], 'That is all')

    def test_a_folder_given_twice_is_said_and_a_full_kind_takes_no_more(self):
        _, said, _ = self.ask([self.found('/mnt/tank/films')] * 2, '/mnt/tank/films', '1', '/mnt/tank/films')
        self.assertIn('/mnt/tank/films is already connected for your films.', said)
        # A kind with all its folders: one more is said in the dashboard page's words.
        cap = existing_media.FOLDERS_PER_KIND
        config = {'storage': dict(DRIVES)}
        for number in range(cap):
            entry = existing_media.entry_for('movies', self.found(f'/mnt/tank/f{number}'))
            config['placement'] = existing_media.with_choice(config, entry)['placement']
        config.pop('storage')
        full, said, _ = self.ask([self.found('/mnt/tank/one-more')], '/mnt/tank/one-more', '1', config=config)
        self.assertIn(existing_media.full_words('movies'), said)
        self.assertEqual(len(existing_media.extras(full)), cap)
        # ... and copied into the library instead, it is still taken.
        copied, _, _ = self.ask([self.found('/mnt/tank/one-more')], '/mnt/tank/one-more', '2', config=config)
        self.assertEqual(copied[existing_page.COPIES], [{'holds': 'movies', 'path': '/mnt/tank/one-more'}])


class DownloadOnlyStaysTests(unittest.TestCase):
    def test_a_folder_only_for_new_ones_stays_used_when_another_takes_them(self):
        """The owner's decision 2 on PR 89, in setup too: 0.51 dropped the folder that took
        only the new films, and the films saved there left the library."""
        new = writable(FOUND, path='/mnt/big/new-films', mount='/mnt/big', share='/dev/sdd1', fstype='ext4')
        kids = {**FOUND, 'path': '/mnt/tank/kids'}
        checks = [dict(FOUND), writable(FOUND, ok=False), new, dict(kids), writable(kids)]
        ctx, said = context(storage=dict(DRIVES)), []
        answers = typed(
            *('/mnt/tank/films', '1', 'yes', '2', '/mnt/big/new-films'),  # read-only; new films elsewhere
            *('/mnt/tank/kids', '1', 'yes'),  # another folder, which takes the new films
        )
        with mock.patch.object(existing_media, 'check', side_effect=checks):
            existing_page.one_kind(ctx.config, 'movies', 'films', (answers, said.append))
        self.assertEqual(
            [(e['path'], e['use'], e['new_files'], e['slot']) for e in existing_media.extras(ctx.config)],
            [
                ('/mnt/tank/films', 'in_place', False, 'movies'),
                ('/mnt/big/new-films', 'in_place', False, 'movies-new'),
                ('/mnt/tank/kids', 'in_place', True, 'movies-2'),
            ],
        )
        self.assertIn(existing_media.STAYS.format(path='/mnt/big/new-films', what='movies'), said)


class CopyTests(unittest.TestCase):
    """ "Copy them into the NekoServer library": chosen on the page, done once the library is there."""

    SHOWN = {
        'source': '/mnt/tank/films', 'destination': '/mnt/media/library/movies', 'files': 12,
        'bytes': 12_300_000_000, 'free': 80_000_000_000, 'fits': True, 'conflicts': ['Old Film (1999).mkv'],
    }  # fmt: skip

    def test_the_choice_is_kept_until_the_copy_and_nothing_is_mounted_for_it(self):
        ctx, said = context(), []
        with mock.patch.object(existing_media, 'check', return_value=dict(FOUND)):
            existing_page.one_kind(ctx.config, 'movies', 'films', (typed('/mnt/tank/films', '2'), said.append))
        self.assertEqual(ctx.config[existing_page.COPIES], [{'holds': 'movies', 'path': '/mnt/tank/films'}])
        self.assertNotIn('placement', ctx.config)
        self.assertIn('copied into the NekoServer library once its drive is ready', said[-1])

    def test_the_copy_says_what_it_will_do_then_copies_and_forgets_the_folder(self):
        ctx = context(**{existing_page.COPIES: [{'holds': 'movies', 'path': '/mnt/tank/films'}]})
        ctx.persist_config = mock.Mock()
        with (
            mock.patch.object(existing_media, 'copy_preview', return_value=dict(self.SHOWN)),
            mock.patch.object(existing_media, 'copy_now', return_value={'status': 'copied', 'copied': 11}) as now,
        ):
            self.assertEqual(existing_page.copy(ctx), [])
        now.assert_called_once_with(ctx.config, 'movies', '/mnt/tank/films')
        self.assertEqual(
            ctx.logs[0],
            'Copying /mnt/tank/films to /mnt/media/library/movies: 12 files, 12.3 GB; 80.0 GB free. Your folder is '
            'not changed. 1 files are already in the library with other content and are left as they are: '
            'Old Film (1999).mkv',
        )
        self.assertEqual(ctx.logs[1], 'Copied into the library: 11 files from /mnt/tank/films')
        self.assertNotIn(existing_page.COPIES, ctx.config)
        ctx.persist_config.assert_called_once()

    def test_a_copy_that_cannot_be_made_says_why_and_is_tried_again_next_time(self):
        wanted = [{'holds': 'movies', 'path': '/mnt/tank/films'}, 'not a folder entry']
        ctx = context(**{existing_page.COPIES: list(wanted)})
        away = existing_media.Refused('not_a_folder', path='/mnt/tank/films')
        with (
            mock.patch.object(existing_media, 'copy_preview', side_effect=away),
            mock.patch.object(existing_media, 'copy_now', side_effect=AssertionError('copied')),
        ):
            self.assertEqual(existing_page.copy(ctx), wanted)  # the step goes on
        self.assertIn('was not copied: /mnt/tank/films is not a folder on this PC.', ctx.logs[0])
        self.assertEqual(ctx.config[existing_page.COPIES], wanted)
        # Nothing chosen: nothing looked at.
        self.assertEqual(existing_page.copy(context()), [])


class MusicTests(unittest.TestCase):
    def test_navidrome_gets_its_library_where_the_libraries_are_made(self):
        rows = [{'check': 'navidrome:existing:music', 'status': 'pass'}]
        with mock.patch.object(existing_media, 'music_libraries', return_value=rows) as made:
            self.assertEqual(existing_page.music_libraries({'a': 1}, True), rows)
        made.assert_called_once_with({'a': 1}, True)

    def music_step(self, rows, **groups):
        """The Music step on a server with these media groups, Navidrome's own check passing."""
        from modules import music

        config = {
            'data_mount': '/srv/x',
            'media': {'services': ['jellyfin', 'navidrome', 'lidarr']},
            'media_apps': {'photos': False, 'movies': False, 'music': True, **groups},
        }
        ctx = context(**config)
        ctx.apply = True
        with (
            mock.patch.object(music.os, 'geteuid', return_value=0),
            mock.patch('neko_common.verify_storage'),
            mock.patch.object(music.music_client, 'check', return_value={'status': 'pass'}),
            mock.patch.object(music, 'atomic_json'),
            mock.patch.object(existing_media, 'music_libraries', return_value=rows) as made,
        ):
            return music.run(ctx), made, ctx

    def test_a_server_without_jellyfin_gets_it_too(self):
        """Review of 0.51.2: the library was made with Jellyfin's (onboard.py --libraries,
        given only with Jellyfin among the apps), so a music-only server never got it."""
        import neko_common

        rows = [{'check': 'navidrome:existing:music', 'status': 'pass'}]
        for groups in ({}, {'music_downloads': False}, {'movies': True}):
            result, made, ctx = self.music_step(rows, **groups)
            self.assertIsNone(result, groups)
            made.assert_called_once_with(ctx.config, True)
        # Navidrome alone: the connecting step is left out altogether, so it can't be there.
        alone = {'media_apps': {'photos': False, 'movies': False, 'music': True, 'music_downloads': False}}
        self.assertIn('onboarding', neko_common.media_steps_left_out(alone))
        self.assertNotIn('existing_page.music_libraries', (ROOT / 'onboard.py').read_text())

    def test_a_folder_navidrome_did_not_take_is_a_step_left_with_its_reason(self):
        why = 'Navidrome\'s administrator login is not set up yet'
        result, _, ctx = self.music_step(
            [{'check': 'navidrome:existing:music', 'status': 'needs_setup', 'reason': why}]
        )
        self.assertEqual(result['status'], 'needs_setup')
        self.assertIn('Your own music folder is not in Navidrome yet: ' + why, result['reason'])


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
        self.assertEqual(config['placement']['movies']['extra'], [{**entry, 'slot': 'movies'}])
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
            mock.patch.object(existing_media, 'folder_answers', return_value=True),
            mock.patch.object(existing_media, 'check', return_value={**FOUND, 'add_to_fstab': line}),
            mock.patch.object(existing_media, 'use_in_place', return_value=mounted) as use,
        ):
            self.assertEqual(existing_page.mount(ctx), mounted)
        self.assertEqual(use.call_args.args[1], [line])
        self.assertIn('Your own folder (movies): /mnt/tank/films is used where it is', ctx.logs)
        # The media step calls it before any app is started.
        source = (ROOT / 'modules/media.py').read_text()
        self.assertLess(source.index('existing_page.mount(context)'), source.index('compose_up('))

    def test_a_folder_connected_while_the_apps_run_restarts_the_apps_that_see_it(self):
        """Deep check 391, tried with real mounts: a folder connected (or switched back to
        read-only) while the apps run reaches them read-write, whatever the PC's mount
        says. Only an app started anew sees it as it is."""
        from modules import media

        ctx = self.with_folder()

        def connects(config, permanent, run, log):
            run(['mount', '/mnt/nekoserver-media/library/existing/movies'], 120)
            return [{'holds': 'movies', 'path': '/mnt/tank/films', 'mounted': True, 'reason': None}]

        with (
            mock.patch.object(existing_media, 'folder_answers', return_value=False),
            mock.patch.object(existing_media, 'must_run') as ran,
            mock.patch.object(existing_media, 'use_in_place', side_effect=connects),
        ):
            existing_page.mount(ctx)
        ran.assert_called_once()
        self.assertTrue(ctx.existing_mounts_changed)
        manifests = {
            name: (Path(f'/srv/neko/compose/{name}/compose.yaml'), (ROOT / f'compose/{name}.yaml').read_text())
            for name in ('jellyfin', 'radarr', 'prowlarr', 'seerr', 'sonarr')
        }
        restarted = []
        with mock.patch.object(media, 'compose_image_override', return_value=[]):
            done = media.restart_for_own_folders(
                manifests, ['sonarr: did not start'], ctx.log, lambda argv, timeout: restarted.append(argv)
            )
        # The apps that see /data/existing; not the ones that don't, nor one that failed to start.
        self.assertEqual(done, ['jellyfin', 'radarr'])
        self.assertEqual(restarted[0][-3:], ['-p', 'neko-jellyfin', 'restart'])
        self.assertIn('these apps were restarted to see them as they are now', ctx.logs[-1])
        # Nothing changed: nothing is restarted.
        with (
            mock.patch.object(existing_media, 'folder_answers', return_value=False),
            mock.patch.object(existing_media, 'use_in_place', return_value=[]),
        ):
            existing_page.mount(ctx)
        self.assertFalse(ctx.existing_mounts_changed)
        source = (ROOT / 'modules/media.py').read_text()
        self.assertLess(source.index('restart_for_own_folders(manifests'), source.index('stop_unselected(compose_dir'))

    def test_setup_run_again_restarts_no_app_for_a_folder_as_it_was(self):
        """Deep check 597/610 (the update to 0.51.3, run with real mounts): a folder that is
        connected already is remounted at every setup run, with the options it has. That
        was counted as a change, so every setup run and every upgrade restarted the apps
        that see it (a film playing in Jellyfin stopped, downloads were cut off) and said
        "Your own folders changed". Only a remount that turns it read-only or back is one."""
        ctx = self.with_folder()
        point = '/mnt/nekoserver-media/library/existing/movies'
        writable = {point: True}

        def remounts(mode):
            def use_in_place(config, permanent, run, log):
                run(['mount', '-o', f'remount,bind,{mode}', point], 120)
                return [{'holds': 'movies', 'path': '/mnt/tank/films', 'mounted': True, 'reason': None}]

            return use_in_place

        def must_run(argv, timeout):
            writable[argv[-1]] = argv[2].endswith(',rw')

        for mode, change in (('rw', False), ('rw', False), ('ro', True), ('ro', False), ('rw', True)):
            with (
                mock.patch.object(existing_media, 'folder_answers', return_value=False),
                mock.patch.object(existing_media, 'must_run', side_effect=must_run),
                mock.patch.object(existing_media, 'takes_new_files', side_effect=lambda p: writable[p]),
                mock.patch.object(existing_media, 'use_in_place', side_effect=remounts(mode)),
            ):
                existing_page.mount(ctx)
            self.assertEqual(ctx.existing_mounts_changed, change, mode)

    def test_a_share_that_is_away_is_said_and_never_fails_the_step(self):
        ctx = self.with_folder()
        away = [
            {'holds': 'movies', 'path': '/mnt/tank/films', 'mounted': False, 'reason': 'nas:/tank is not connected'}
        ]
        gone = existing_media.Refused('not_a_folder', path='/mnt/tank/films')
        with (
            mock.patch.object(existing_media, 'folder_answers', return_value=True),
            mock.patch.object(existing_media, 'check', side_effect=gone),
            mock.patch.object(existing_media, 'use_in_place', return_value=away) as use,
        ):
            existing_page.mount(ctx)
        self.assertEqual(use.call_args.args[1], [])
        # A share that does not answer is never checked (the check would wait for it).
        with (
            mock.patch.object(existing_media, 'folder_answers', return_value=False),
            mock.patch.object(existing_media, 'check', side_effect=AssertionError('looked at')),
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
