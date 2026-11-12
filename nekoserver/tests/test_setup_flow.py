"""The install and first-setup flow (review of 0.39.5): which settings file the commands
in the docs use, and the questions and screens on the way."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import installer


class SettingsFileTests(unittest.TestCase):
    """`sudo bash install.sh --module server_os --apply` (as the docs show it) ran on the
    template config.json, with no drives in it, and saved the answers into that file,
    which git tracks: the next `git pull` failed."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'config.json').write_text('{}')

    def run_main(self, *argv):
        seen = []
        with (
            mock.patch.object(installer, 'ROOT', self.root),
            mock.patch.object(installer, 'execute', side_effect=lambda args: seen.append(args.config) or 0),
        ):
            code = installer.main(list(argv))
        return code, seen

    def test_your_settings_file_is_used_once_the_wizard_has_made_it(self):
        (self.root / 'my-config.json').write_text('{}')
        code, seen = self.run_main('--module', 'server_os', '--apply')
        self.assertEqual((code, Path(seen[0]).name), (0, 'my-config.json'))

    def test_changes_are_never_saved_into_the_template(self):
        with mock.patch('sys.stderr'), self.assertRaises(SystemExit) as stopped:
            self.run_main('--module', 'server_os', '--apply')
        self.assertEqual(stopped.exception.code, 2)
        with mock.patch('sys.stderr'), self.assertRaises(SystemExit):
            self.run_main('--module', 'server_os', '--apply', '--config', str(self.root / 'config.json'))

    def test_a_template_someone_put_their_drives_in_says_how_to_keep_them(self):
        import io

        (self.root / 'config.json').write_text(json.dumps({'storage': {'data_ssd': {'uuid': 'abcd-1234'}}}))
        err = io.StringIO()
        with mock.patch('sys.stderr', err), self.assertRaises(SystemExit):
            self.run_main('--module', 'server_os', '--apply')
        self.assertIn('cp config.json my-config.json', err.getvalue())

    def test_a_check_may_still_read_the_template(self):
        code, seen = self.run_main('--check')
        self.assertEqual((code, Path(seen[0]).name), (0, 'config.json'))


class PreflightTests(unittest.TestCase):
    """SELinux enforcing (Fedora's default) stopped setup only after minutes of package
    installs and the runtime build; install.sh now asks first, and again after them."""

    def test_what_setup_cannot_run_without_is_said_before_anything_is_installed(self):
        import io

        import capabilities

        rows = [
            capabilities.row('selinux', 'SELinux', 'enforcing', False, True, problem=capabilities.SELINUX_ENFORCING),
            capabilities.row('docker', 'Docker Engine', 'not installed yet (setup installs it)', True),
            capabilities.row('memory', 'Memory', '4 GB', False),
        ]
        out = io.StringIO()
        self.assertEqual(capabilities.preflight(rows, out), 1)
        self.assertIn('setenforce 0', out.getvalue())
        self.assertNotIn('Docker', out.getvalue())  # installed later by setup itself
        self.assertNotIn('Memory', out.getvalue())  # a note, not a reason to stop
        self.assertEqual(capabilities.preflight(rows[1:], io.StringIO()), 0)

    def test_a_folder_older_than_the_server_is_stopped_before_it_sets_the_old_release_up(self):
        """A VM upgraded from the dashboard (0.47.6 -> 0.47.8) and then "sudo bash
        install.sh" in the folder downloaded for 0.47.6: setup ran the old release over the
        new one. Now install.sh's preflight sees the newer release's runtime copy on the
        SSD, which the installed services point at, and says to run git pull first."""
        import io

        import capabilities

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        data, units = root / 'ssd', root / 'units'
        units.mkdir()
        (root / 'my-config.json').write_text(json.dumps({'data_mount': str(data)}))
        for name in ('v0.47.6', 'v0.47.8', 'v0.47.9', 'v0.47.9.replaced-20261108'):
            (data / 'installer/runtime' / name).mkdir(parents=True)
        for name in ('v0.47.6', 'v0.47.8'):  # v0.47.9: left by an upgrade put back, nothing runs it
            unit = units / f'nekoserver-{name}.service'
            unit.write_text(f'ExecStart={data}/installer/runtime/{name}/nekoserver-runtime\n')
        self.assertEqual(capabilities.newer_installed(root, '0.47.6', units), '0.47.8')
        self.assertIsNone(capabilities.newer_installed(root, '0.47.8', units))  # the same release: fine
        self.assertIsNone(capabilities.newer_installed(root, '0.47.10', units))  # a newer folder: an upgrade
        self.assertIsNone(capabilities.newer_installed(root / 'nowhere', '0.47.6', units))  # a first install
        (root / 'my-config.json').write_text('{broken')
        self.assertIsNone(capabilities.newer_installed(root, '0.47.6', units))
        (root / 'my-config.json').write_text(json.dumps({'data_mount': str(data)}))
        out = io.StringIO()
        with mock.patch('neko_common.VERSION', '0.47.6'):
            self.assertEqual(capabilities.preflight([], out, root=root, units=units), 1)
        self.assertIn('This folder has NekoServer 0.47.6, and this server already runs 0.47.8', out.getvalue())
        self.assertIn('git pull', out.getvalue())
        with mock.patch('neko_common.VERSION', '0.47.8'):
            self.assertEqual(capabilities.preflight([], io.StringIO(), root=root, units=units), 0)

    def test_install_sh_checks_before_the_packages_and_again_before_setup(self):
        script = (Path(installer.ROOT) / 'install.sh').read_text()
        first = script.index('capabilities.py" preflight')
        self.assertLess(first, script.index('pacman -Syu --noconfirm'))  # before the system update
        self.assertLess(first, script.index('zypper_run --quiet refresh'))
        self.assertLess(first, script.index('host.py" bootstrap'))
        second = script.rindex('capabilities.py" preflight')
        self.assertLess(script.index('runtime/build.py'), second)
        self.assertLess(second, script.index('exec python3 "$ROOT/installer.py"'))


class SetupWindowTests(unittest.TestCase):
    """setup.js run in Node against a stand-in page (skipped without Node)."""

    def page(self, states, action=''):
        import shutil
        import subprocess

        node = shutil.which('node')
        if not node:
            self.skipTest('Node is not installed')
        here = Path(__file__).resolve().parent
        result = subprocess.run(
            [node, str(here / 'setup_window_harness.mjs'), json.dumps(states), action],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return json.loads(result.stdout)

    def choose(self):
        extras = [
            {'id': name, 'title': name, 'explanation': '', 'installs': [], 'on': True}
            for name in ('tv', 'notifications', 'heartbeat', 'nextcloud')
        ]
        return {
            'version': 'x',
            'phase': 'choose',
            'extras': extras,
            'steps': [],
            'current': None,
            'done': 0,
            'total': 0,
            'question': None,
            'finish': [],
            'complete': False,
        }

    def test_start_without_touching_a_switch_decides_nothing(self):
        # The page sends only a switch pressed here that is not saved yet; setup's own
        # server saves every switch as it stands when the button is pressed
        # (tests/test_first_page.py).
        seen = self.page([self.choose()], 'start')
        self.assertEqual(seen['posts'], [['api/start', {'choices': {}}]])

    def test_a_refused_start_gives_the_button_back_with_the_reason(self):
        # post() took any status for a success: after the server's 409 (the settings
        # file cannot be read right now, or setup has started) the button stayed
        # disabled at "Starting…" and nothing said why.
        reason = "my-config.json can't be read right now: check it for a typo."
        seen = self.page([{**self.choose(), 'refuseStart': reason}], 'start')
        self.assertEqual(seen['posts'], [['api/start', {'choices': {}}]])
        self.assertEqual(seen['start'], [False, 'Install selected apps'])
        self.assertEqual(seen['review']['note'], reason)

    def test_the_first_page_then_the_review_then_one_button(self):
        """0.50.0, the wizard: the switches under their headings, Continue, the review of
        what will be installed with Back, and one button that starts it."""
        extras = [
            {'id': 'media_movies', 'title': 'Movies and shows - Jellyfin', 'part': 'Apps', 'needs': [], 'on': False},
            {'id': 'media_music', 'title': 'Music - Navidrome', 'part': 'Apps', 'needs': [], 'on': True},
            # The switch inside each of the two cards: drawn in its card, offered while the card is on.
            {
                'id': 'movies_downloads',
                'title': 'Also find and download new ones',
                'part': 'Apps',
                'under': 'media_movies',
                'needs': ['media_movies'],
                'on': True,
            },
            {
                'id': 'music_downloads',
                'title': 'Also find and download new ones',
                'part': 'Apps',
                'under': 'media_music',
                'needs': ['media_music'],
                'on': True,
            },
            {'id': 'tv', 'title': 'A TV connected to this PC', 'part': 'Optional extras', 'needs': [], 'on': False},
        ]
        extras = [{'explanation': '', 'installs': [], **extra} for extra in extras]
        review = {
            'title': 'Ready to install NekoServer',
            'button': 'Install selected apps',
            'sections': [{'title': 'Apps to install', 'lines': ['Jellyfin: movies & shows']}],
        }
        state = {**self.choose(), 'extras': extras, 'review': review}
        seen = self.page([state])
        self.assertEqual(seen['parts'], ['Apps', 'Optional extras'])
        # Three cards; every card says yes or no (nothing is "asked later").
        self.assertEqual(seen['cards'], [['extra', 'no', False], ['extra on', 'yes', False], ['extra', 'no', False]])
        # The finding switch is inside its card, and cannot be pressed while the card is off.
        self.assertEqual(
            seen['subs'],
            [
                ['Movies and shows - Jellyfin', 'sub-switch unavailable', 'no', True],
                ['Music - Navidrome', 'sub-switch', 'yes', False],
            ],
        )
        self.assertEqual((seen['review']['appsHidden'], seen['review']['hidden']), (False, True))
        # The card switched on: its switch is offered, on as it was saved.
        seen = self.page([{**state, 'press': 'Movies and shows - Jellyfin'}])
        self.assertEqual(seen['subs'][0], ['Movies and shows - Jellyfin', 'sub-switch', 'yes', False])
        self.assertEqual(seen['posts'], [['api/choose', {'media_movies': True}]])
        # The switch inside the Music card pressed: Navidrome alone.
        seen = self.page([{**state, 'pressInside': 'Music - Navidrome'}])
        self.assertEqual(seen['subs'][1], ['Music - Navidrome', 'sub-switch', 'no', False])
        self.assertEqual(seen['posts'], [['api/choose', {'music_downloads': False}]])
        # Continue is setup's to follow (it asks for the drives next); nothing has started.
        seen = self.page([{**state, 'continue': True}])
        self.assertEqual(seen['posts'], [['api/continue', {}]])
        self.assertEqual((seen['review']['appsHidden'], seen['review']['hidden']), (False, True))
        # The review, once setup says so, with what the button will do.
        seen = self.page([{**state, 'stage': 'review'}])
        self.assertEqual((seen['review']['appsHidden'], seen['review']['hidden']), (True, False))
        self.assertEqual(seen['review']['title'], 'Ready to install NekoServer')
        self.assertEqual(seen['review']['sections'], [['Apps to install', ['Jellyfin: movies & shows']]])
        self.assertEqual(seen['start'], [False, 'Install selected apps'])
        self.assertEqual(seen['posts'], [])
        # Back: asked of setup, which shows the switches again.
        seen = self.page([{**state, 'stage': 'review', 'back': True}])
        self.assertEqual(seen['posts'], [['api/back', {}]])
        # A server that exists is changed, not installed.
        again = {**state, 'review': {**review, 'title': 'Review your changes', 'button': 'Apply these changes'}}
        seen = self.page([{**again, 'stage': 'review'}])
        self.assertEqual((seen['review']['title'], seen['start'][1]), ('Review your changes', 'Apply these changes'))

    def backup_times_question(self, **changes):
        step = {'id': 'storage', 'title': 'Drives', 'status': 'running', 'explanation': '', 'packages': [], 'notes': []}
        page = {
            'form': 'backup_times',
            'fields': {'daily': '04:15', 'weekly_day': 'Sun', 'weekly': '03:00', 'monthly': '02:30'},
            'asking': 'daily',
            'weekdays': ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
            'night_end': 6,
        }
        question = {
            'seq': 7,
            'text': 'When should the daily backups run? (HH:MM, Enter for 04:15): ',
            'secret': False,
            'kind': 'text',
            'context': [],
            'page': page,
        }
        return {
            **self.choose(),
            'phase': 'running',
            'steps': [step],
            'current': 'storage',
            'total': 1,
            'question': question,
            **changes,
        }

    def test_the_backup_times_are_one_page_of_three_rows(self):
        chosen = {
            'ask-page-daily': '23:30',
            'ask-page-weekly_day': 'Sat',
            'ask-page-weekly': '21:00',
            'ask-page-monthly': '01:00',
        }
        seen = self.page([self.backup_times_question(fill=chosen)])
        # Three rows, each with the time shown and a line saying what runs then.
        self.assertEqual(
            [row[0] for row in seen['rows']], ['Daily backups', 'Weekly USB copy', 'Monthly check and updates']
        )
        self.assertEqual([row[1] for row in seen['rows']], [['23:30'], ['Sat', '21:00'], ['01:00']])
        self.assertIn('music copy 30 minutes before', seen['rows'][0][2])
        self.assertIn("Immich's database copy 45 minutes before", seen['rows'][1][2])
        self.assertIn('before 06:00', seen['rows'][2][2])
        self.assertEqual((seen['pageHidden'], seen['answerHidden']), (False, True))
        self.assertEqual(seen['prompt'], 'When should the backups run? Times are on this PC’s clock.')
        # Save sent the field the terminal is asking for as the answer, the rest with it.
        self.assertEqual(
            seen['posts'],
            [
                [
                    'api/answer',
                    {
                        'seq': 7,
                        'text': '23:30',
                        'fields': {'daily': '23:30', 'weekly_day': 'Sat', 'weekly': '21:00', 'monthly': '01:00'},
                    },
                ]
            ],
        )

    def test_the_backups_page_also_says_how_many_are_kept_and_what_goes_where(self):
        """0.50.0, the wizard (P05): a number from 1 to 7 for each kind of nightly backup, and
        the table of what is backed up, on the same page as the times."""
        state = self.backup_times_question()
        page = state['question']['page']
        page['fields'].update(keep_app_backup='3', keep_host_backup='3')
        page['counts'] = [
            {'group': 'app_backup', 'what': 'nightly app backups', 'value': 3, 'least': 1, 'most': 7, 'review': None},
            {
                'group': 'host_backup',
                'what': 'nightly settings backups',
                'value': 3,
                'least': 1,
                'most': 7,
                'review': None,
            },
        ]
        page['table'] = [
            {'key': 'app_backup', 'what': 'The media apps', 'drive': 'media', 'when': 'every night at 04:15',
             'kept': 'the newest 3', 'covers': 'about the last 3 nights', 'same_drive': False, 'note': None},
            {'key': 'offline', 'what': 'Photos and Nextcloud files', 'drive': None, 'when': '', 'kept': '', 'covers': '',
             'same_drive': True, 'note': 'Not set up.'},
        ]  # fmt: skip
        seen = self.page([state])
        self.assertEqual(
            [row[0] for row in seen['rows']][3:],
            ['Keep the latest nightly app backups', 'Keep the latest nightly settings backups'],
        )
        self.assertEqual([row[1] for row in seen['rows']][3:], [['3'], ['3']])
        self.assertIn('An older one is removed only after a new backup has succeeded.', seen['rows'][3][2])
        self.assertEqual(
            seen['backups'],
            [
                ['The media apps', 'every night at 04:15 · the newest 3 · about the last 3 nights'],
                [
                    'Photos and Nextcloud files',
                    '',
                    'This backup is on the same drive as what it protects. If that drive fails, both are gone.',
                    'Not set up.',
                ],
            ],
        )
        # A number outside the limits is said, not sent; within them it goes with the page.
        seen = self.page([{**state, 'fill': {'ask-page-keep_app_backup': '14'}}])
        self.assertEqual(
            (seen['posts'], seen['note']), ([], 'Keep the latest nightly app backups: a whole number from 1 to 7.')
        )
        seen = self.page([{**state, 'fill': {'ask-page-keep_app_backup': '5'}}])
        self.assertEqual(
            (seen['posts'][0][1]['fields']['keep_app_backup'], seen['posts'][0][1]['fields']['keep_host_backup']),
            ('5', '3'),
        )
        # A count from before that is no longer offered: its review is beside it, and empty leaves it.
        page['counts'][0].update(review='This server keeps the newest 14 nightly app backups.')
        page['fields']['keep_app_backup'] = ''
        seen = self.page([{**state, 'fill': {'ask-page-daily': '23:30'}}])
        self.assertIn(
            'This server keeps the newest 14 nightly app backups. Leave it empty to leave them as they are.',
            seen['rows'][3][2],
        )
        self.assertEqual(seen['posts'][0][1]['fields']['keep_app_backup'], '')

    def test_a_monthly_time_after_the_night_is_refused_on_the_page(self):
        seen = self.page([self.backup_times_question(fill={'ask-page-monthly': '06:00'})])
        self.assertEqual(seen['posts'], [])
        self.assertEqual(seen['note'], 'The monthly check must start at night, before 06:00.')
        # Skip for now leaves the question as Ctrl+D does: setup keeps the times shown.
        seen = self.page([self.backup_times_question(skipPage=True)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 7, 'text': '', 'skip': True}]])
        # A plain question draws its field, not the page.
        plain = self.backup_times_question()
        plain['question'] = {**plain['question'], 'page': None}
        seen = self.page([plain])
        self.assertEqual((seen['pageHidden'], seen['answerHidden'], seen['rows']), (True, False, []))

    def asked(self, text, page=None, context=(), kind='text', secret=False, **changes):
        """The window with this question waiting."""
        state = self.backup_times_question(**changes)
        state['question'] = {
            'seq': 9,
            'text': text,
            'secret': secret,
            'kind': kind,
            'context': list(context),
            'page': page,
        }
        return state

    PASSWORD = {'form': 'new_password', 'fields': {'password': '', 'again': ''}, 'asking': 'password'}

    def test_a_password_chosen_now_is_two_hidden_fields_that_must_match(self):
        """0.50.0, item 8: the shared-folder password, an app's own password, the backup
        password and the dashboard PIN were two hidden prompts one after the other."""
        page = {**self.PASSWORD, 'what': 'Password for the shared folders (account neko)'}
        typed = 'typed-' + 'x' * 10
        question = 'Password for the shared folders (account neko): '
        seen = self.page([self.asked(question, page, secret=True, kind='secret')])
        self.assertEqual([row[0] for row in seen['rows']], [page['what'], 'The same again'])
        self.assertEqual(seen['fieldTypes'], [['password'], ['password']])
        self.assertEqual(seen['prompt'], page['what'] + ': choose one, and enter it in both fields.')
        self.assertEqual((seen['send'], seen['skipHidden'], seen['answerHidden']), ('Save', True, True))
        self.assertIn('written to no log', seen['note'])
        # Two that differ are not sent.
        differ = {'ask-page-password': typed, 'ask-page-again': typed + 'y'}
        seen = self.page([self.asked(question, page, secret=True, kind='secret', fill=differ)])
        self.assertEqual((seen['posts'], seen['note']), ([], 'The two do not match. Type them again.'))
        seen = self.page([self.asked(question, page, secret=True, kind='secret', fill={})])
        self.assertEqual((seen['posts'], seen['note']), ([], 'Type it in both fields.'))
        # Two that match go together: the first as the answer, the second with it.
        same = {'ask-page-password': typed, 'ask-page-again': typed}
        seen = self.page([self.asked(question, page, secret=True, kind='secret', fill=same)])
        self.assertEqual(
            seen['posts'], [['api/answer', {'seq': 9, 'text': typed, 'fields': {'password': typed, 'again': typed}}]]
        )

    def test_the_rules_of_a_pin_or_a_backup_password_are_checked_before_sending(self):
        pin = {**self.PASSWORD, 'what': 'Dashboard PIN', 'least': 6, 'most': 8, 'digits': True, 'optional': True}

        def tried(first, again, page=pin):
            fill = {'ask-page-password': first, 'ask-page-again': again}
            seen = self.page([self.asked('PIN: ', page, secret=True, kind='secret', fill=fill)])
            return seen['note'], seen['posts']

        self.assertEqual(tried('12ab56', '12ab56')[0], 'Use digits only (0-9).')
        self.assertEqual(tried('12345', '12345')[0], 'Use at least 6 digits.')
        self.assertEqual(tried('123456789', '123456789')[0], 'Use at most 8 digits.')
        self.assertEqual(tried('123456', '123457')[0], 'The two do not match. Type them again.')
        self.assertEqual(tried('123456', '123456')[1][0][1]['text'], '123456')
        # Optional: both left empty goes on without one, as Enter does in the terminal.
        self.assertEqual(
            tried('', '')[1], [['api/answer', {'seq': 9, 'text': '', 'fields': {'password': '', 'again': ''}}]]
        )
        backup = {**self.PASSWORD, 'what': 'Backup password', 'least': 8}
        self.assertEqual(tried('short', 'short', backup)[0], 'Use at least 8 characters.')
        self.assertEqual(tried('', '', backup)[0], 'Type it in both fields.')
        # The first typed in the terminal: the page asks only for the second.
        again = {**backup, 'asking': 'again'}
        seen = self.page(
            [self.asked('Same backup password again: ', again, secret=True, fill={'ask-page-again': 'longer-one'})]
        )
        self.assertEqual([row[0] for row in seen['rows']], ['The same again'])
        self.assertEqual(seen['posts'][0][1]['text'], 'longer-one')

    def test_a_long_list_is_searched_and_picked_from(self):
        items = [{'label': name} for name in ('1337x', 'Nyaa', 'Radarr list', 'The Pirate Bay', 'YTS')]
        page = {
            'form': 'pick',
            'fields': {'choice': ''},
            'asking': 'choice',
            'what': 'Select an indexer',
            'items': items,
        }
        question = 'Select an indexer (its number; Enter alone skips): '
        seen = self.page([self.asked(question, page, search='ra')])
        self.assertEqual([row[1] for row in seen['pick']], [item['label'] for item in items])
        self.assertEqual({row[0] for row in seen['pick']}, {'radio'})  # one of them
        self.assertEqual(
            (seen['prompt'], seen['send'], seen['skipHidden']), ('Select an indexer', 'Choose this one', False)
        )
        # Nothing ticked: nothing sent.
        seen = self.page([self.asked(question, page, tick=[])])
        self.assertEqual((seen['posts'], seen['note']), ([], 'Tick one in the list first, or press Skip for now.'))
        # The row's number is the answer, as the terminal takes it.
        seen = self.page([self.asked(question, page, tick=[4])])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '4', 'fields': {'choice': '4'}}]])
        seen = self.page([self.asked(question, page, skipPage=True)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '', 'skip': True}]])
        # Several at once (item 10): tick boxes, a count, and the numbers together.
        many = {**page, 'multiple': True}
        seen = self.page([self.asked(question, many, tick=[1, 3, 5])])
        self.assertEqual({row[0] for row in seen['pick']}, {'checkbox'})
        self.assertEqual((seen['send'], seen['pickCount']), ('Add the selected ones', '3 selected'))
        self.assertEqual(seen['posts'][0][1], {'seq': 9, 'text': '1 3 5', 'fields': {'choice': '1 3 5'}})

    def test_the_terminals_numbered_lines_can_be_clicked(self):
        context = ['Now choose the SSD.', '  1) sdb: 232 GB, Samsung (ext4)', '  2) sdc: 1.8 TB, WD Red (ext4)']
        seen = self.page([self.asked('Number of the SSD (Enter alone stops setup here): ', context=context, choose=2)])
        self.assertEqual(
            [option[:2] for option in seen['options']],
            [['1', 'sdb: 232 GB, Samsung (ext4)'], ['2', 'sdc: 1.8 TB, WD Red (ext4)']],
        )
        self.assertEqual(seen['options'][1][2], 'option picked')
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '2'}]])
        # Other shapes of a numbered line: " 1. name", "[1] ...", "1 = ...".
        for lines in (
            [' 1. Any', ' 2. HD-1080p'],
            ['[1] generate random login', '[2] enter username/password'],
            ['1 = NekoServer makes them', '2 = you choose them'],
        ):
            seen = self.page([self.asked('Choose a number (Enter alone skips): ', context=lines)])
            self.assertEqual([option[0] for option in seen['options']], ['1', '2'], lines)
        # Not for a question that asks for words, a hidden entry or a yes or no.
        for text, extra in (
            ('Type sdb to confirm it is the SSD: ', {}),
            ('Number: ', {'secret': True}),
            ('Use number 2? [yes/no]: ', {'kind': 'yesno'}),
        ):
            seen = self.page([self.asked(text, context=context, **extra)])
            self.assertEqual((seen['options'], seen['optionsHidden']), ([], True), text)

    def test_what_enter_alone_keeps_is_shown_in_the_field(self):
        for text, kept in (
            ('qBittorrent username [admin]: ', 'admin'),
            ('When should the daily backups run? (HH:MM, Enter for 04:15): ', '04:15'),
            ('Which one does NekoServer use? [1/2] (Enter alone: 1): ', '1'),
        ):
            seen = self.page([self.asked(text)])
            self.assertEqual(seen['placeholder'], 'Enter alone keeps: ' + kept, text)
        for text in ('Listener username: ', 'Does the NAS ask for a CHAP user name and password? [y/N]: '):
            self.assertEqual(self.page([self.asked(text)])['placeholder'], '', text)

    FIX = {
        'id': 'docker_down',
        'title': 'Docker, which runs the apps, is not running',
        'steps': ['Start it:  sudo systemctl start docker', 'Then press Retry this step.'],
    }

    def retry_page(self, steps, leave):
        return {'form': 'retry', 'fields': {'answer': ''}, 'asking': 'answer', 'steps': steps, 'leave': leave}

    def test_a_step_that_failed_has_its_reason_its_fix_and_retry(self):
        """0.50.0, item 7: a failed step said "did not finish" and its reason; trying again
        meant running setup again."""
        step = {'answer': 'yes', 'title': 'Docker', 'reason': 'Cannot connect to the Docker daemon', 'fix': self.FIX}
        page = self.retry_page([step], 'no')
        context = ['Docker did not finish: Cannot connect to the Docker daemon']
        question = 'Retry this step now? [yes/no]: '
        seen = self.page([self.asked(question, page, context, kind='yesno')])
        self.assertEqual(seen['prompt'], 'Docker did not finish.')
        self.assertEqual(
            seen['retry'],
            [
                [
                    'Docker',
                    'Cannot connect to the Docker daemon',
                    ['What usually fixes it: Docker, which runs the apps, is not running', self.FIX['steps']],
                    'Retry this step',
                ]
            ],
        )
        # The cards say it all: not the terminal's lines again, nor plain Yes and No.
        self.assertEqual(
            (seen['contextHidden'], seen['yesnoHidden'], seen['send'], seen['skipHidden']),
            (True, True, 'Not now', True),
        )
        seen = self.page([self.asked(question, page, context, kind='yesno', retry=1)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': 'yes'}]])
        seen = self.page([self.asked(question, page, context, kind='yesno', leave=True)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': 'no', 'fields': {'answer': 'no'}}]])

    def test_the_unfinished_steps_at_the_end_are_retried_one_at_a_time(self):
        steps = [
            {'answer': '1', 'title': 'Docker', 'reason': 'Cannot connect to the Docker daemon', 'fix': self.FIX},
            {'answer': '2', 'title': 'Tailscale', 'reason': 'Sign in to Tailscale', 'fix': None},
        ]
        page = self.retry_page(steps, '')
        question = 'Retry one now? Type its number (Enter alone: finish): '
        seen = self.page([self.asked(question, page)])
        self.assertEqual(seen['prompt'], 'These steps did not finish. Each can be tried again now.')
        self.assertEqual([card[0] for card in seen['retry']], ['Docker', 'Tailscale'])
        self.assertEqual(seen['retry'][1], ['Tailscale', 'Sign in to Tailscale', 'Retry this step'])  # no fix known
        self.assertEqual(seen['send'], 'Finish')
        seen = self.page([self.asked(question, page, retry=2)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '2'}]])
        seen = self.page([self.asked(question, page, leave=True)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '', 'fields': {'answer': ''}}]])

    def test_a_sign_in_step_has_show_login_for_each_saved_login(self):
        """0.50.0, item 9: setup made the password, then asked the person to sign in with it."""
        apps = [
            {'id': 'jellyfin', 'title': 'Jellyfin administrator', 'username': 'neko_ab12'},
            {'id': 'immich', 'title': 'Immich administrator', 'username': 'me@example.org'},
        ]
        page = {'form': 'logins', 'fields': {'answer': ''}, 'asking': 'answer', 'apps': apps}
        question = 'Show a login now? Type its number (Enter alone: go on): '
        fetched = 'fetched-' + 'x' * 12
        login = {'ok': True, 'username': 'neko_ab12', 'password': fetched, 'qr': 'data:image/svg+xml;base64,QR'}
        # Until the button is pressed nothing of the password is on the page.
        seen = self.page([self.asked(question, page, login=login)])
        self.assertEqual(seen['prompt'], 'Your login for this step is saved on this server.')
        self.assertEqual([card['title'] for card in seen['logins']], ['Jellyfin administrator', 'Immich administrator'])
        self.assertEqual(seen['logins'][0]['user'], 'Username: neko_ab12')
        self.assertEqual(
            [(card['hidden'], card['password'], card['button']) for card in seen['logins']],
            [(True, None, 'Show login')] * 2,
        )
        self.assertEqual((seen['send'], seen['skipHidden'], seen['contextHidden']), ('Go on', True, True))
        self.assertEqual(seen['posts'], [])
        # Show login asks setup's own server for that one login and shows it.
        seen = self.page([self.asked(question, page, login=login, showLogin=1)])
        self.assertEqual(seen['posts'], [['api/login', {'app': 'jellyfin'}]])
        shown = seen['logins'][0]
        self.assertEqual(
            (shown['hidden'], shown['password'], shown['qr'], shown['button']), (False, fetched, login['qr'], 'Hide')
        )
        self.assertEqual(seen['logins'][1]['hidden'], True)
        # Pressed again it is hidden, and gone from the page.
        seen = self.page([self.asked(question, page, login=login, showLogin=[1, 1])])
        self.assertEqual(
            (seen['logins'][0]['hidden'], seen['logins'][0]['password'], seen['logins'][0]['button']),
            (True, None, 'Show login'),
        )
        # A login setup will not give (the step is over) is said, and nothing is shown.
        refused = {'ok': False, 'reason': 'No sign-in step is asking for that login now.'}
        seen = self.page([self.asked(question, page, login=refused, showLogin=2)])
        self.assertEqual((seen['logins'][1]['hidden'], seen['logins'][1]['note']), (True, refused['reason']))
        # Go on answers the terminal's question with Enter alone.
        seen = self.page([self.asked(question, page, leave=True)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '', 'fields': {'answer': ''}}]])

    def test_download_sources_are_ticked_many_at_once_with_the_recommended_in_one_press(self):
        """0.50.0, item 10: one indexer at a time from a numbered list, in the terminal only."""
        items = [
            {'label': 'Alpha', 'note': 'public, en-US, torrent'},
            {'label': 'Beta', 'note': 'private: needs an account'},
            {'label': 'Gamma', 'note': ''},
            {'label': 'Delta', 'note': 'needs a free account, usenet'},
        ]
        advice = 'Use only providers and content you are authorized to access.'
        page = {
            'form': 'pick', 'fields': {'choice': ''}, 'asking': 'choice', 'what': 'Download sources (indexers) to add',
            'items': items, 'multiple': True, 'recommended': [1, 3], 'advice': advice,
        }  # fmt: skip
        question = 'Numbers to add, such as 1 4 7 or 1-5, or r for the recommended ones (Enter alone skips): '
        seen = self.page([self.asked(question, page)])
        self.assertEqual(
            seen['pickNotes'],
            ['public, en-US, torrent', 'private: needs an account', '', 'needs a free account, usenet'],
        )
        self.assertEqual(seen['recommended'], ['Tick the 2 recommended', False])
        self.assertEqual(
            (seen['advice'], seen['send'], seen['pickCount']), (advice, 'Add the selected ones', '0 selected')
        )
        # The recommended ones in one press, another ticked by hand, all sent together.
        seen = self.page([self.asked(question, page, tickRecommended=True, tick=[4])])
        self.assertEqual(seen['pickCount'], '3 selected')
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '1 3 4', 'fields': {'choice': '1 3 4'}}]])
        # Without a recommended list there is no such button, and no word of advice is missing.
        plain = {**page, 'recommended': []}
        seen = self.page([self.asked(question, plain)])
        self.assertEqual((seen['recommended'][1], seen['advice']), (True, advice))

    def test_a_download_source_that_needs_a_login_has_a_small_form_of_its_own(self):
        rows = [
            {'key': 'baseUrl', 'label': 'Site address', 'secret': False},
            {'key': 'apiKey', 'label': 'API Key', 'secret': True},
        ]
        page = {
            'form': 'fields',
            'fields': {'baseUrl': '', 'apiKey': ''},
            'asking': 'baseUrl',
            'what': 'Beta needs these',
            'rows': rows,
        }
        question = 'Site address (Enter alone keeps its default): '
        typed = 'typed-' + 'k' * 12
        seen = self.page([self.asked(question, page)])
        self.assertEqual(seen['prompt'], 'Beta needs these')
        self.assertEqual([row[0] for row in seen['rows']], ['Site address', 'API Key'])
        self.assertEqual(seen['fieldTypes'], [['text'], ['password']])
        self.assertEqual(seen['rows'][1][2], 'Hidden, and written to no log.')
        self.assertEqual((seen['send'], seen['skipHidden']), ('Save', False))
        # Saved together: the field the terminal asks for as the answer, the rest with it.
        seen = self.page([self.asked(question, page, fill={'ask-page-apiKey': typed})])
        self.assertEqual(
            seen['posts'], [['api/answer', {'seq': 9, 'text': '', 'fields': {'baseUrl': '', 'apiKey': typed}}]]
        )
        # Skip for now leaves this one out (the terminal's Ctrl+D).
        seen = self.page([self.asked(question, page, skipPage=True)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '', 'skip': True}]])

    DRIVES = [
        {
            'number': 1, 'name': 'nvme0n1', 'what': '477 GB NVMe SSD, WD SN770', 'inside': '', 'role': '',
            'why': 'it is the disk Linux runs from, which NekoServer never uses for your files',
        },
        {
            'number': 2, 'name': 'sda1', 'what': '466 GB SSD, Samsung SSD 870, named "apps"',
            'inside': 'ext4, UUID ssd-0001; not open; about 1 % full', 'role': '', 'why': '',
        },
        {
            'number': 3, 'name': 'sdb1', 'what': '1.8 TB HDD, WDC WD20EZRZ, named "media"',
            'inside': 'ext4, UUID hdd-0001; not open', 'role': 'Chosen as the media HDD',
            'why': 'it is already the media HDD',
        },
    ]  # fmt: skip

    def test_the_drives_are_cards_chosen_by_clicking(self):
        """0.50.0, item 5: the drive questions were the terminal's numbered lines only."""
        names = 'The short names (sdb1) are Linux\'s own and can change at the next start.'
        page = {
            'form': 'drives', 'fields': {'choice': ''}, 'asking': 'choice', 'rows': self.DRIVES, 'skippable': False,
            'what': 'Choose the drive for your apps, photos and music (the app SSD).',
            'needs': 'It needs at least 20 GB free, so a drive of 22 GB or more (100 GB or more is recommended).',
            'names': names,
        }  # fmt: skip
        question = 'Number of the drive for your apps, photos and music (the app SSD) (Enter alone stops setup here): '
        seen = self.page([self.asked(question, page)])
        self.assertEqual(seen['prompt'], page['what'])
        self.assertEqual(
            (seen['send'], seen['skipHidden'], seen['note'], seen['contextHidden']),
            ('Use this drive', True, names, True),
        )
        self.assertEqual(
            seen['drives'],
            [
                {
                    'name': '1 · nvme0n1 · 477 GB NVMe SSD, WD SN770',
                    'facts': ['Not this one: it is the disk Linux runs from, which NekoServer never uses for your files'],
                    'off': True, 'radioHidden': False,
                },
                {
                    'name': '2 · sda1 · 466 GB SSD, Samsung SSD 870, named "apps"',
                    'facts': ['ext4, UUID ssd-0001; not open; about 1 % full'],
                    'off': False, 'radioHidden': False,
                },
                {
                    'name': '3 · sdb1 · 1.8 TB HDD, WDC WD20EZRZ, named "media"',
                    'facts': ['ext4, UUID hdd-0001; not open', 'Chosen as the media HDD', 'Not this one: it is already the media HDD'],
                    'off': True, 'radioHidden': False,
                },
            ],
        )  # fmt: skip
        # Nothing chosen: said, not sent. A card clicked: its number is the answer.
        seen = self.page([self.asked(question, page, leave=True)])
        self.assertEqual((seen['posts'], seen['note']), ([], 'Choose a drive in the list first.'))
        seen = self.page([self.asked(question, page, tick=[2])])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '2', 'fields': {'choice': '2'}}]])
        # The USB backup drive may be left for later.
        seen = self.page([self.asked(question, {**page, 'skippable': True}, skipPage=True)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '', 'skip': True}]])

    def test_the_accounts_page_is_one_choice_and_a_row_for_each_app(self):
        """0.50.0, the wizard (P04): no question per app; a row is typed in only with your
        own logins, or for an app that has data already."""
        page = {
            'form': 'accounts', 'asking': 'mode',
            'fields': {'mode': '', 'problem': '', 'user:jellyfin': '', 'pass:jellyfin': '', 'again:jellyfin': '',
                       'user:immich': '', 'pass:immich': '', 'again:immich': ''},
            'title': 'How would you like to set up your app accounts?', 'hint': 'Each selected app has a separate account.',
            'choices': [['1', 'Create strong passwords for me (recommended)'], ['2', 'Choose my own logins']],
            'apps': [
                {'id': 'jellyfin', 'name': 'Jellyfin administrator', 'email': False, 'existing': False},
                {'id': 'immich', 'name': 'Immich administrator', 'email': True, 'existing': True},
            ],
            'existing': 'This app has data already: enter its current login.',
        }  # fmt: skip
        question = 'How would you like to set up your app accounts? Type 1 or 2, then press Enter. '
        typed = 'typed-' + 'x' * 12
        seen = self.page([self.asked(question, page)])
        self.assertEqual((seen['prompt'], seen['send'], seen['skipHidden']), (page['title'], 'Save these logins', True))
        # Strong passwords made for you is chosen to begin with: Jellyfin's row only says so;
        # Immich has data already, so its current login is typed in whatever the choice.
        self.assertEqual(
            seen['accounts'], [['Jellyfin administrator', False, True], ['Immich administrator', True, False]]
        )
        seen = self.page([self.asked(question, page, leave=True)])
        self.assertEqual((seen['posts'], seen['note']), ([], 'Immich administrator: fill in the email.'))
        # Your own logins: every row is typed in, and two passwords that differ are said.
        seen = self.page([self.asked(question, page, check=['ask-page-mode-2'])])
        self.assertEqual(
            seen['accounts'], [['Jellyfin administrator', True, False], ['Immich administrator', True, False]]
        )
        entries = {
            'ask-page-user:jellyfin': 'dana', 'ask-page-pass:jellyfin': typed, 'ask-page-again:jellyfin': typed + '!',
            'ask-page-user:immich': 'dana@example.org', 'ask-page-pass:immich': typed, 'ask-page-again:immich': typed,
        }  # fmt: skip
        seen = self.page([self.asked(question, page, check=['ask-page-mode-2'], fill=entries)])
        self.assertEqual(
            (seen['posts'], seen['note']), ([], 'Jellyfin administrator: the two passwords are not the same.')
        )
        # All in order: one answer carries the choice and every row.
        entries['ask-page-again:jellyfin'] = typed
        seen = self.page([self.asked(question, page, check=['ask-page-mode-2'], fill=entries)])
        sent = seen['posts'][0][1]
        self.assertEqual((sent['text'], sent['fields']['mode']), ('2', '2'))
        self.assertEqual(
            (sent['fields']['user:immich'], sent['fields']['pass:jellyfin'], sent['fields']['again:jellyfin']),
            ('dana@example.org', typed, typed),
        )

    def test_the_storage_page_has_a_list_for_every_line_and_sends_them_together(self):
        """0.50.0, the wizard (P02): every drive question on one page; a drive is chosen
        in a list, no name is typed, and a drive that cannot have a role says why."""
        places = [['', 'Let setup choose'], ['ssd', 'On the drive for your apps (faster)'], ['media', 'On the other']]
        page = {
            'form': 'storage', 'asking': 'data_ssd',
            'fields': {'data_ssd': '', 'media_hdd': '', 'usb': '', 'photos': ''},
            'title': 'Where should NekoServer store your apps and files?', 'hint': 'Choose a drive for each line.',
            'roles': [
                {'key': 'data_ssd', 'title': 'NekoServer and your apps', 'holds': 'your apps', 'needs': 'It needs 20 GB.',
                 'optional': False, 'rows': self.DRIVES},
                {'key': 'media_hdd', 'title': 'Backups', 'holds': 'every backup', 'needs': '',
                 'optional': False, 'rows': [{**self.DRIVES[0]}, {**self.DRIVES[1]}, {**self.DRIVES[2], 'why': ''}]},
                {'key': 'usb', 'title': 'Offline backup', 'holds': 'the weekly copy', 'needs': '', 'optional': True,
                 'rows': []},
            ],
            'places': [{'key': 'photos', 'title': "Immich's photos and videos", 'options': places,
                        'beside_backup': 'Their backup is then on the same drive.'}],
            'names': 'The short names are Linux\'s own.', 'network': 'SMB is not supported yet.',
        }  # fmt: skip
        question = 'Number of the drive for your apps (the app SSD) (Enter alone stops setup here): '
        seen = self.page([self.asked(question, page)])
        self.assertEqual((seen['prompt'], seen['send']), (page['title'], 'Use these drives'))
        self.assertEqual(seen['skipHidden'], True)
        lists = seen['storage']
        self.assertEqual([row['key'] for row in lists], ['data_ssd', 'media_hdd', 'usb', 'photos'])
        self.assertEqual(
            lists[0]['options'],
            [
                ['', 'Choose a drive…', False],
                ['1', '1 · 477 GB NVMe SSD, WD SN770 (not this one: it is the disk Linux runs from, which '
                 'NekoServer never uses for your files)', True],
                ['2', '2 · 466 GB SSD, Samsung SSD 870, named "apps"', False],
                ['3', '3 · 1.8 TB HDD, WDC WD20EZRZ, named "media" (not this one: it is already the media HDD)', True],
            ],
        )  # fmt: skip
        self.assertEqual(lists[2]['options'], [['', 'None for now', False]])  # the offline copy can wait
        self.assertEqual([option[0] for option in lists[3]['options']], ['', 'ssd', 'media'])
        # Files kept on the backups' drive: what that means is said under the line, only then.
        self.assertEqual(lists[3]['note'], '')
        beside = {**page, 'fields': {**page['fields'], 'photos': 'media'}}
        seen = self.page([self.asked(question, beside)])
        self.assertEqual(seen['storage'][3]['note'], 'Their backup is then on the same drive.')
        # A group's row of the table, where there is a drive for it: the list of where its
        # apps are installed beside the list of where its files are, both sent with the page.
        install = {'key': 'install_photos', 'options': [['', 'Same drive as NekoServer and your apps'],
                                                        ['drive:3', 'On a drive of its own: 3']]}  # fmt: skip
        table = {**page, 'fields': {**page['fields'], 'install_photos': 'drive:3'},
                 'places': [{**page['places'][0], 'group': 'Immich', 'install': install}]}  # fmt: skip
        seen = self.page([self.asked(question, table)])
        row = seen['storage'][3]
        self.assertEqual((row['key'], row['title']), ('photos', 'Immich'))
        self.assertEqual(row['install']['key'], 'install_photos')
        self.assertEqual(row['install']['value'], 'drive:3')
        self.assertEqual([option[0] for option in row['install']['options']], ['', 'drive:3'])
        self.assertIsNone(lists[3]['install'])  # no such drive: the line as it always was
        # The recommended drive is chosen already when the page opens, and says so.
        ready = {**page, 'roles': [{**page['roles'][0], 'recommended': '2'}, *page['roles'][1:]]}
        seen = self.page([self.asked(question, ready)])
        self.assertEqual((seen['storage'][0]['value'], seen['storage'][1]['value']), ('2', ''))
        self.assertEqual(
            seen['storage'][0]['options'][2][1], '2 · 466 GB SSD, Samsung SSD 870, named "apps" (recommended)'
        )
        # Nothing chosen for a line that needs a drive: said, not sent.
        seen = self.page([self.asked(question, page, leave=True)])
        self.assertEqual((seen['posts'], seen['note']), ([], 'Choose a drive for “NekoServer and your apps” first.'))
        # The apps and the backups on one drive: sent (setup says what that means).
        both = {'ask-page-data_ssd': '2', 'ask-page-media_hdd': '2'}
        seen = self.page([self.asked(question, page, fill=both)])
        self.assertEqual(seen['posts'][0][1]['fields']['media_hdd'], '2')
        # The offline backup on a drive of another line: said, not sent.
        offline = {'ask-page-data_ssd': '2', 'ask-page-media_hdd': '3', 'ask-page-usb': '2'}
        usb_rows = [{**self.DRIVES[0]}, {**self.DRIVES[1]}, {**self.DRIVES[2], 'why': ''}]
        with_usb = {**page, 'roles': [*page['roles'][:2], {**page['roles'][2], 'rows': usb_rows}]}
        seen = self.page([self.asked(question, with_usb, fill=offline)])
        self.assertEqual(seen['posts'], [])
        self.assertIn('One drive cannot be both', seen['note'])
        # Chosen: every line goes in one answer, the asked one as the terminal takes it.
        chosen = {'ask-page-data_ssd': '2', 'ask-page-media_hdd': '3', 'ask-page-photos': 'ssd'}
        seen = self.page([self.asked(question, page, fill=chosen)])
        self.assertEqual(
            seen['posts'],
            [['api/answer', {'seq': 9, 'text': '2', 'fields': {'data_ssd': '2', 'media_hdd': '3', 'usb': '', 'photos': 'ssd'}}]],
        )  # fmt: skip

    PART = {
        'form': 'part', 'fields': {'answer': '', 'done': ''}, 'asking': 'answer', 'title': 'Set up your phone',
        'question': 'Is your phone set up, and did an app open on mobile data?',
        'steps': [
            {'id': 'tailscale', 'title': 'Tailscale on the phone', 'lines': ['Install it.', 'Turn it on.'], 'done': False},
            {'id': 'jellyfin', 'title': 'Jellyfin: movies and shows', 'lines': ['Open the app.'], 'done': True},
            {'id': 'away_from_home', 'title': 'Check it works away from home', 'lines': ['Turn off Wi-Fi.'], 'done': False},
        ],
    }  # fmt: skip

    def test_a_part_shows_what_setup_checked_itself_above_its_steps(self):
        """0.50.0, the wizard (P08): passed, not yet, or not known (never shown as passed)."""
        status = [
            {'id': 'server', 'status': 'pass', 'title': 'This server is signed in to Tailscale',
             'reason': 'This server is signed in to Tailscale.', 'next': '', 'shows': ''},
            {'id': 'https', 'status': 'unknown', 'title': 'Private HTTPS addresses',
             'reason': 'No certificate could be checked.', 'next': 'Check again.', 'shows': ''},
            {'id': 'phone', 'status': 'fail', 'title': 'A phone in your Tailscale account',
             'reason': 'No phone is signed in yet.', 'next': 'Install Tailscale on the phone.',
             'shows': 'Setup cannot see whether a phone is on Wi-Fi or on mobile data.'},
        ]  # fmt: skip
        question = self.PART['question'] + ' [yes/no]: '
        seen = self.page([self.asked(question, {**self.PART, 'status': status}, kind='yesno')])
        self.assertEqual(
            seen['status'],
            [
                [
                    'status-row pass',
                    '✓',
                    'This server is signed in to Tailscale',
                    'This server is signed in to Tailscale.',
                ],
                [
                    'status-row unknown',
                    '?',
                    'Private HTTPS addresses',
                    'No certificate could be checked.',
                    'Check again.',
                ],
                [
                    'status-row fail',
                    '✕',
                    'A phone in your Tailscale account',
                    'No phone is signed in yet.',
                    'Install Tailscale on the phone.',
                    'Setup cannot see whether a phone is on Wi-Fi or on mobile data.',
                ],
            ],
        )
        self.assertEqual(len(seen['part']), 3)  # the steps are under the rows, as they were
        # Without rows the page is as before.
        self.assertEqual(self.page([self.asked(question, self.PART, kind='yesno')])['status'], [])

    def test_a_part_is_one_page_with_a_sliding_yes_for_each_step(self):
        """0.50.0, item 13: the phone was up to eight questions in a row, each a yes/no."""
        question = self.PART['question'] + ' [yes/no]: '
        seen = self.page([self.asked(question, self.PART, kind='yesno')])
        self.assertEqual((seen['prompt'], seen['send'], seen['skipHidden']), ('Set up your phone', 'Continue', True))
        self.assertEqual((seen['yesnoHidden'], seen['contextHidden']), (True, True))  # no second yes/no beside it
        self.assertTrue(seen['note'].startswith('Slide each step to yes once it is done.'))
        self.assertEqual(
            seen['part'],
            [
                {
                    'title': '1. Tailscale on the phone', 'lines': ['Install it.', 'Turn it on.'], 'caption': 'not yet',
                    'on': False, 'off': False, 'role': 'switch', 'label': 'Done: Tailscale on the phone',
                },
                {
                    'title': '2. Jellyfin: movies and shows (done last time)', 'lines': [], 'caption': 'yes',
                    'on': True, 'off': True, 'role': 'switch', 'label': 'Done: Jellyfin: movies and shows',
                },
                {
                    'title': '3. Check it works away from home', 'lines': ['Turn off Wi-Fi.'], 'caption': 'not yet',
                    'on': False, 'off': False, 'role': 'switch', 'label': 'Done: Check it works away from home',
                },
            ],
        )  # fmt: skip
        # Every switch on yes: Continue confirms the part.
        slid = self.asked(
            question, self.PART, kind='yesno', slide=['tailscale', 'away_from_home'], **{'continue': True}
        )
        seen = self.page([slid])
        done = 'tailscale jellyfin away_from_home'
        self.assertEqual(
            seen['posts'], [['api/answer', {'seq': 9, 'text': 'yes', 'fields': {'answer': 'yes', 'done': done}}]]
        )
        self.assertEqual([step['caption'] for step in seen['part']], ['yes', 'yes', 'yes'])
        # One still off: Continue is there all the same, answers no, and says which are done.
        seen = self.page([self.asked(question, self.PART, kind='yesno', slide=['tailscale'], **{'continue': True})])
        self.assertEqual(
            seen['posts'],
            [['api/answer', {'seq': 9, 'text': 'no', 'fields': {'answer': 'no', 'done': 'tailscale jellyfin'}}]],
        )

    def test_a_steps_own_question_can_be_left_for_later_and_no_other_yes_no_can(self):
        step = {'form': 'confirm_step', 'fields': {'answer': ''}, 'asking': 'answer'}
        seen = self.page(
            [self.asked('Did the Tailscale page say this server is connected? [yes/no]: ', step, kind='yesno')]
        )
        self.assertEqual((seen['skipShown'], seen['yesnoHidden'], seen['pageHidden']), (True, False, True))
        seen = self.page([self.asked('Continue with these drives anyway? [yes/no]: ', kind='yesno')])
        self.assertEqual(seen['skipShown'], False)

    def test_a_question_has_its_own_words_and_buttons_in_the_window(self):
        """0.50.0, item 13 redone: the window showed the terminal's line ("... [yes/no]:",
        "Type 1 or 2", "Enter alone skips"). Each question now has a title, a hint and
        buttons that say what they do; nothing in the window tells anyone to type."""
        import questions

        def asked(key, choices=None, leave=False, **more):
            page = {'form': 'ask', 'fields': {'answer': ''}, 'asking': 'answer', 'leave': leave}
            page.update(questions.window(key, choices, **questions.Q[key].get('example', {})))
            kind = 'yesno' if page['kind'] == 'yesno' else 'text'
            return self.asked(questions.terminal(key, choices), page, kind=kind, context=['An earlier line.'], **more)

        # A yes or no: two buttons in the question's own words, no Send and no yes/no pair.
        seen = self.page([asked('tailscale_connected', leave=True)])
        self.assertEqual(seen['prompt'], 'Is this server connected to Tailscale?')
        self.assertTrue(seen['note'].startswith('The Tailscale page in your browser says so'))
        self.assertEqual(seen['choices'], [['Yes, it is connected', 'primary'], ['Not yet', 'secondary']])
        self.assertEqual((seen['sendHidden'], seen['yesnoHidden'], seen['answerHidden']), (True, True, True))
        self.assertEqual((seen['skipHidden'], seen['skipLabel'], seen['contextHidden']), (False, 'Skip for now', True))
        seen = self.page([asked('tailscale_connected', leave=True, pressChoice=2)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': 'no'}]])
        # A step that can be left: Skip for now does what Ctrl+D does in the terminal.
        seen = self.page([asked('tailscale_connected', leave=True, skipPage=True)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '', 'skip': True}]])
        # A choice: a button for each, sending what the terminal would be given.
        seen = self.page([asked('logins_who', pressChoice=2)])
        self.assertEqual(seen['prompt'], 'How would you like to set up your app accounts?')
        self.assertEqual(
            [label for label, _ in seen['choices']],
            ['Create strong passwords for me (recommended)', 'Choose my own logins'],
        )
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '2'}]])
        self.assertEqual(seen['skipHidden'], True)
        # A list made at run time (the assistants this PC can run).
        models = [('0', 'No assistant'), ('1', 'qwen3.5:4b')]
        seen = self.page([asked('assistant_model', models, pressChoice=2)])
        self.assertEqual(seen['prompt'], 'Which AI assistant do you want?')
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': '1'}]])
        # Something to paste: a password box, Save, and a Skip that answers "not now".
        seen = self.page([asked('alerts_token')])
        self.assertEqual(seen['prompt'], 'Does your alerts topic have an access token? Paste it here')
        self.assertEqual(
            (seen['send'], seen['sendHidden'], seen['skipHidden'], seen['skipLabel']),
            ('Save', False, False, 'It has none'),
        )
        seen = self.page([asked('alerts_token', fill={'ask-page-answer': 'pasted-here'})])
        self.assertEqual(seen['posts'][0][1]['text'], 'pasted-here')
        seen = self.page([asked('alerts_token', skipPage=True)])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': ''}]])  # Enter alone, not Ctrl+D
        # A question about what was listed just before it keeps that list in view.
        seen = self.page([asked('drive_space')])
        self.assertEqual(seen['contextHidden'], False)

    def test_an_answer_that_was_not_delivered_can_be_sent_again(self):
        """The setup review (S8): when sending an answer failed, the window's buttons and
        fields stayed off, and the same question on the next polls did not bring them back."""
        question = self.asked('Username for the shared folders [neko]: ')
        # Sent and delivered: the controls are off until the next question, as before.
        seen = self.page([{**question, 'fill': {'ask-input': 'me'}}, question])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 9, 'text': 'me'}]])
        self.assertIn('ask-send', seen['askOff'])
        # Not delivered: they are on again, the note says so, and a second try goes through.
        lost = {**question, 'failAnswer': 1, 'fill': {'ask-input': 'me'}}
        seen = self.page([lost, question])
        self.assertEqual(seen['askOff'], [])
        self.assertEqual(
            seen['note'], 'Could not reach setup, so that was not sent. Try again, or answer in the terminal.'
        )
        seen = self.page([lost, {**question, 'fill': {'ask-input': 'me'}}, question])
        self.assertEqual(len(seen['posts']), 2)
        self.assertEqual(seen['note'], 'Sent. Setup goes on in a moment…')

    def test_a_menu_of_its_own_shows_its_name_and_the_terminals_words(self):
        """The recovery menu and the uninstaller in the window (0.50.0): no steps, no
        switches; what the terminal said, and the menu's questions."""
        lines = ['What do you want to get back?', '  1. Photos and videos (Immich)', '  0. Quit']
        state = {
            **self.choose(),
            'phase': 'running',
            'extras': [],
            'session': {'title': 'NekoServer recovery', 'lines': lines},
        }
        seen = self.page([state])
        self.assertEqual(seen['now'], ['IN THE TERMINAL TOO', 'NekoServer recovery', '\n'.join(lines), 'said'])
        self.assertEqual((seen['stepsHidden'], seen['running'], seen['progressHidden']), (True, 'running alone', True))
        self.assertEqual(seen['badge'], 'Working…')
        # A question of the menu is asked as setup's own are, under the menu's name.
        asked = {
            **state,
            'question': {
                'seq': 3,
                'text': 'Choose a number: ',
                'secret': False,
                'kind': 'text',
                'context': lines,
                'page': None,
            },
            'choose': 1,
        }
        seen = self.page([asked])
        self.assertEqual(seen['askLabel'], 'A QUESTION · NekoServer recovery')
        self.assertEqual(seen['options'][0][:2], ['1', 'Photos and videos (Immich)'])
        self.assertEqual(seen['posts'], [['api/answer', {'seq': 3, 'text': '1'}]])
        # Setup's own run is drawn as before: its steps, two columns.
        seen = self.page([self.backup_times_question()])
        self.assertEqual((seen['stepsHidden'], seen['running'], seen['now'][3]), (False, 'running', ''))

    def test_the_pages_rows_go_when_a_plain_question_follows(self):
        # The rows' inputs are `required` and sit in the same form as the plain answer:
        # left behind hidden, they blocked every later Send.
        plain = self.backup_times_question()
        plain['question'] = {
            'seq': 8,
            'text': 'Select the SSD. Choose number: ',
            'secret': False,
            'kind': 'text',
            'context': [],
            'page': None,
        }
        seen = self.page([self.backup_times_question(), plain])
        self.assertEqual((seen['pageHidden'], seen['answerHidden'], seen['rows']), (True, False, []))

    def test_the_finish_title_is_setups_own_verdict(self):
        done = {
            **self.choose(),
            'phase': 'finished',
            'steps': [{'id': 'a', 'title': 'A', 'status': 'success'}],
            'finish': ['  NekoServer is almost ready'],
            'complete': False,
        }
        self.assertEqual(self.page([done])['title'], 'NekoServer is almost ready')
        self.assertEqual(self.page([{**done, 'complete': True}])['title'], 'NekoServer is ready')


class SavedSettingsTests(unittest.TestCase):
    """After Linux is reinstalled, setup made new settings before it could see the SSD
    and then wrote them over the copy kept there, the one recovery needs."""

    def test_the_earlier_settings_are_kept_beside_the_new_ones(self):
        from modules import recovery

        with tempfile.TemporaryDirectory() as d:
            saved = Path(d) / 'config.json'
            saved.write_text(json.dumps({'storage': 'the real drives'}))
            logged = []
            for run in range(7):
                recovery.keep_previous(saved, {'storage': 'new', 'run': run}, logged.append, now=1_800_000_000 + run)
                saved.write_text(json.dumps({'storage': 'new', 'run': run}))
            kept = sorted(Path(d).glob('config.previous-*.json'))
            self.assertEqual(len(kept), recovery.PREVIOUS_KEPT)
            # The settings from before (the first ones kept) are never rotated out.
            self.assertIn({'storage': 'the real drives'}, [json.loads(p.read_text()) for p in kept])
            recovery.keep_previous(saved, {'storage': 'new', 'run': 6}, logged.append, now=1_900_000_000)
            self.assertEqual(
                len(sorted(Path(d).glob('config.previous-*.json'))), recovery.PREVIOUS_KEPT
            )  # same: nothing new
            self.assertIn('config.previous-', logged[0])

    def test_the_first_copy_is_kept_from_the_first_change(self):
        from modules import recovery

        with tempfile.TemporaryDirectory() as d:
            saved = Path(d) / 'config.json'
            saved.write_text(json.dumps({'storage': 'the real drives'}))
            recovery.keep_previous(saved, {'storage': 'new'}, lambda *_: None, now=1_800_000_000)
            [kept] = Path(d).glob('config.previous-*.json')
            self.assertEqual(json.loads(kept.read_text()), {'storage': 'the real drives'})
            # A second change in the same second keeps its own copy.
            saved.write_text(json.dumps({'storage': 'new'}))
            recovery.keep_previous(saved, {'storage': 'newer'}, lambda *_: None, now=1_800_000_000)
            self.assertEqual(len(list(Path(d).glob('config.previous-*.json'))), 2)


class BackupDriveMessageTests(unittest.TestCase):
    def test_a_fat32_stick_is_named_instead_of_connect_it_first(self):
        from modules import photo_backup

        config = {
            'storage': {'data_ssd': {'uuid': 'ssd'}, 'media_hdd': {'uuid': 'hdd'}},
            'external_backup_mount': '/mnt/usb',
        }
        inventory = {
            'blockdevices': [
                {
                    'name': 'sda',
                    'type': 'disk',
                    'children': [{'name': 'sda1', 'type': 'part', 'mountpoint': '/', 'fstype': 'ext4', 'uuid': 'os'}],
                },
                {
                    'name': 'sdc',
                    'type': 'disk',
                    'tran': 'usb',
                    'rm': False,
                    'children': [{'name': 'sdc1', 'type': 'part', 'fstype': 'vfat', 'uuid': 'AB-CD'}],
                },
            ]
        }
        with self.assertRaisesRegex(RuntimeError, r'/dev/sdc1 is formatted FAT \(FAT32\).*exFAT or ext4'):
            photo_backup.select_external(config, inventory)
        # The system disk's own FAT boot partition is never named, and neither is an
        # internal disk (Windows on a second disk): never told to format those.
        inventory['blockdevices'][0]['children'].append(
            {'name': 'sda2', 'type': 'part', 'fstype': 'vfat', 'uuid': 'EF'}
        )
        inventory['blockdevices'][1] = {
            'name': 'nvme1n1',
            'type': 'disk',
            'tran': 'nvme',
            'rm': False,
            'children': [
                {'name': 'nvme1n1p1', 'type': 'part', 'fstype': 'vfat', 'uuid': 'EF2'},
                {'name': 'nvme1n1p3', 'type': 'part', 'fstype': 'ntfs', 'uuid': 'WIN'},
            ],
        }
        with self.assertRaises(RuntimeError) as caught:
            photo_backup.select_external(config, inventory)
        self.assertIn('connect it first', str(caught.exception))
        self.assertNotIn('nvme1n1', str(caught.exception))
        self.assertNotIn('this erases it', str(caught.exception))


class DockerOrderTests(unittest.TestCase):
    def test_the_ssd_data_root_is_never_set_without_the_drive_guard(self):
        # daemon.json pointed Docker at the SSD folder before the guard existed; a run
        # stopped in between let Docker start at boot before the SSD and fill that
        # folder on the system disk, which then refused the SSD's mount.
        from modules import docker

        written = []
        config = {'data_mount': '/srv/nekoserver', 'docker': {'install_engine': True}}
        context = mock.Mock(config=config, apply=True, log=lambda *_: None)
        # Not this PC's own daemon.json: on a PC running NekoServer it names the SSD already.
        daemon = Path(tempfile.mkdtemp()) / 'daemon.json'
        self.addCleanup(shutil.rmtree, daemon.parent, True)
        with (
            mock.patch.object(docker, 'DAEMON_FILE', daemon),
            mock.patch.object(docker, 'mounted_data_root', return_value=('/srv/nekoserver', '/dev/sdb1')),
            mock.patch.object(docker.os, 'geteuid', return_value=0),
            mock.patch.object(docker.shutil, 'which', return_value='/usr/bin/x'),
            mock.patch.object(docker.host, 'snap_docker', return_value=False),
            mock.patch.object(
                docker, 'prepare_daemon_config', return_value={'data-root': '/srv/nekoserver/docker-engine'}
            ),
            mock.patch.object(docker, 'check_existing_docker'),
            mock.patch.object(docker.Path, 'mkdir'),
            mock.patch.object(docker, 'write_atomic', side_effect=lambda path, text: written.append(str(path))),
            mock.patch.object(docker, 'validate_daemon'),
            mock.patch.object(docker, 'install_storage_guard', side_effect=RuntimeError('runtime build failed')),
            mock.patch.object(
                docker.subprocess,
                'run',  # Docker not running; its Compose plugin there
                side_effect=lambda argv, **kw: mock.Mock(returncode=3 if 'is-active' in argv else 0),
            ),
            self.assertRaisesRegex(RuntimeError, 'runtime build failed'),
        ):
            docker.run(context)
        self.assertNotIn(str(daemon), written)


class DriveLayoutTests(unittest.TestCase):
    """Btrfs pools over several drives stopped setup ("Duplicate filesystem UUID"), and
    encrypted, LVM and RAID drives were never offered."""

    def setUp(self):
        from modules import storage

        self.real_profiles = storage.data_profiles
        # A pool's data profile is never read from this computer's own drives here.
        profiles = mock.patch.object(storage, 'data_profiles', return_value=None)
        profiles.start()
        self.addCleanup(profiles.stop)

    # Owner review of 0.40.2: a Btrfs pool's size was its first drive's, so the same
    # 32 GB + 4 TB pool listed the other way round went from "too small" to "big enough".
    def pool_both_ways(self, profiles=None, statvfs=None, mountpoint=None):
        """The pool of a 32 GB and a 4 TB drive, as discovered with the drives listed in
        each order."""
        from modules import storage

        found = []
        for sizes in (('32G', '4T'), ('4T', '32G')):
            members = [self.fs(name, 'pool', fstype='btrfs', mountpoint=mountpoint) for name in ('sdb1', 'sdc1')]
            for row, size in zip(members, sizes):
                row['size'] = size
            inventory = {'blockdevices': [self.disk('sdb', members[0]), self.disk('sdc', members[1])]}
            with (
                mock.patch.object(storage, 'data_profiles', return_value=profiles),
                mock.patch.object(storage.os, 'statvfs', side_effect=statvfs or OSError(2, 'not mounted')),
            ):
                [pool] = storage.discover_filesystems(inventory, {'sdb1': 'dev-1', 'sdc1': 'dev-2'}.get)
            found.append(pool)
        self.assertEqual(found[0]['size'], found[1]['size'], 'the order the drives are listed in changes nothing')
        return found[0]

    def test_a_pool_whose_layout_cannot_be_read_counts_only_its_smallest_drive(self):
        from modules import storage

        pool = self.pool_both_ways()
        self.assertEqual(storage.size_bytes(pool['size']), 32 * 1024**3)
        self.assertIn('only its smallest drive is counted, to be safe', pool['size_note'])
        self.assertIn('could not be read', pool['size_note'])
        self.assertFalse(storage.big_enough(pool, storage.sizes('media_hdd')[0]))

    def test_a_raid1_pool_holds_what_its_smallest_mirror_holds_and_a_single_one_the_sum(self):
        from modules import storage

        mirrored = self.pool_both_ways(profiles={'RAID1'})
        self.assertEqual(storage.size_bytes(mirrored['size']), 32 * 1024**3)
        self.assertIn('keeping its data as RAID1', mirrored['size_note'])
        self.assertFalse(storage.big_enough(mirrored, storage.sizes('media_hdd')[0]))
        single = self.pool_both_ways(profiles={'SINGLE'})
        self.assertEqual(storage.size_bytes(single['size']), (4 * 1024 + 32) * 1024**3)
        self.assertTrue(storage.big_enough(single, storage.sizes('media_hdd')[0]))

    def test_a_drive_of_exactly_the_minimum_size_is_too_small_once_formatted(self):
        # The drive space step wants the minimum FREE; a drive of that size in all never
        # has it, so the picker says so, instead of accepting it and failing a step later.
        from modules import storage

        exactly = {'name': 'sdb1', 'size': '50G', 'fstype': 'ext4', 'uuid': 'x'}
        enough = {'name': 'sdc1', 'size': '54G', 'fstype': 'ext4', 'uuid': 'y'}
        self.assertFalse(storage.big_enough(exactly, 50))
        self.assertTrue(storage.big_enough(enough, 50))
        # The list says so with the size that would do, and the drive cannot be chosen.
        import drive_table

        inventory = {'blockdevices': [self.disk('sdb', {**exactly, 'type': 'part'})]}
        config = {'media_mount': '/mnt/nekoserver-media'}
        [row] = drive_table.rows(inventory, config)
        self.assertEqual(
            drive_table.verdict(row, 'media_hdd', config),
            'it is too small: the media HDD needs a drive of at least 54 GB (50 GB free once formatted)',
        )
        with self.assertRaisesRegex(RuntimeError, 'No drive here can be the media HDD'):
            storage.choose_new(config, [row], 'media_hdd', lambda q: '', lambda s: None)

    def test_a_mounted_pool_counts_what_the_pool_itself_reports(self):
        from modules import storage

        reported = mock.Mock(f_blocks=1000, f_bfree=900, f_bavail=700, f_frsize=1024**3 // 25)
        pool = self.pool_both_ways(statvfs=lambda folder: reported, mountpoint='/run/media/neko/pool')
        self.assertEqual(storage.size_bytes(pool['size']), 800 * (1024**3 // 25))
        self.assertIn('usable space as the pool itself reports it', pool['size_note'])

    def test_usable_space_by_data_profile(self):
        from modules import storage

        gib = 1024**3
        cases = {
            ('RAID1', (32, 4096)): 32,
            ('RAID1', (1000, 1000, 1000)): 1500,
            ('RAID1C3', (1000, 1000, 1000)): 1000,
            ('RAID1C4', (1000, 1000, 1000)): 0,
            ('DUP', (100, 300)): 200,
            ('RAID0', (32, 4096)): 64,
            ('RAID10', (500, 500, 500, 500)): 1000,
            ('RAID6', (32, 4096, 4096)): 32,
        }
        for (profile, sizes), usable in cases.items():
            self.assertEqual(
                storage.profile_capacity(profile, [s * gib for s in sizes]), usable * gib, (profile, sizes)
            )
        chunks = (
            'item 2 key (FIRST_CHUNK_TREE CHUNK_ITEM 13631488) itemoff 15975 itemsize 112\n'
            '\t\tlength 8388608 owner 2 stripe_len 65536 type SYSTEM|RAID1\n'
            '\t\tlength 1073741824 owner 2 stripe_len 65536 type METADATA|RAID1\n'
            '\t\tlength 1073741824 owner 2 stripe_len 65536 type DATA|RAID1\n'
            '\t\tlength 1073741824 owner 2 stripe_len 65536 type DATA\n'
        )  # a conversion from single, unfinished
        with (
            mock.patch.object(storage.os.path, 'exists', return_value=True),
            mock.patch.object(storage, 'call', return_value=chunks) as called,
        ):
            self.assertEqual(self.real_profiles(['sdb1']), {'RAID1', 'SINGLE'})
        self.assertEqual(
            called.call_args.args[0], ['btrfs', 'inspect-internal', 'dump-tree', '-t', 'chunk', '/dev/sdb1']
        )

    def disk(self, name, *children, mountpoint=None):
        return {'name': name, 'type': 'disk', 'size': '1T', 'children': list(children), 'mountpoint': mountpoint}

    def fs(self, name, uuid, kind='part', fstype='ext4', mountpoint=None, children=()):
        return {
            'name': name,
            'type': kind,
            'fstype': fstype,
            'uuid': uuid,
            'size': '1T',
            'mountpoint': mountpoint,
            'children': list(children),
        }

    def test_a_btrfs_pool_over_two_drives_is_one_choice_on_both(self):
        from modules import storage

        inventory = {
            'blockdevices': [
                self.disk('sda', self.fs('sda1', 'os', mountpoint='/')),
                self.disk('sdb', self.fs('sdb1', 'pool', fstype='btrfs')),
                self.disk('sdc', self.fs('sdc1', 'pool', fstype='btrfs')),
            ]
        }
        devices = {'sdb1': 'dev-1', 'sdc1': 'dev-2'}  # each pool member has a UUID_SUB of its own
        [pool] = storage.discover_filesystems(inventory, devices.get)
        self.assertEqual((pool['uuid'], pool['physical_disks']), ('pool', ['sdb', 'sdc']))

    def test_a_cloned_btrfs_drive_is_not_taken_for_a_pool(self):
        # dd or a disk cloner copies the device's own UUID_SUB too: mounted by UUID, the
        # kernel could write to either drive.
        from modules import storage

        inventory = {
            'blockdevices': [
                self.disk('sdb', self.fs('sdb1', 'pool', fstype='btrfs')),
                self.disk('sdc', self.fs('sdc1', 'pool', fstype='btrfs')),
            ]
        }
        devices = {'sdb1': 'dev-1', 'sdc1': 'dev-1'}
        with self.assertRaisesRegex(RuntimeError, 'cloned drives'):
            storage.discover_filesystems(inventory, devices.get)
        # A device ID blkid could not read (no sudo) still stops setup, but the drives are
        # not called copies: it says what could not be read, and to use sudo.
        with self.assertRaisesRegex(RuntimeError, r'Could not read the Btrfs device ID of /dev/sdc1.*sudo') as caught:
            storage.discover_filesystems(inventory, {'sdb1': 'dev-1'}.get)
        self.assertNotIn('clone', str(caught.exception))

    def test_lvm_raid_and_encrypted_volumes_are_offered(self):
        from modules import storage

        lv = self.fs('data-media', 'lv', kind='lvm')
        inventory = {
            'blockdevices': [
                self.disk('sda', self.fs('sda1', 'os', mountpoint='/')),
                self.disk('sdb', self.fs('sdb1', None, fstype='LVM2_member', children=[lv])),
                self.disk('sdc', self.fs('sdc1', None, fstype='LVM2_member', children=[lv])),  # the LV spans both
                self.disk(
                    'sdd',
                    self.fs('sdd1', None, fstype='crypto_LUKS', children=[self.fs('secret', 'luks', kind='crypt')]),
                ),
                self.disk(
                    'sde',
                    self.fs('sde1', None, fstype='linux_raid_member', children=[self.fs('md0', 'md', kind='raid1')]),
                ),
                self.disk(
                    'sdf',
                    self.fs('sdf1', None, fstype='linux_raid_member', children=[self.fs('md0', 'md', kind='raid1')]),
                ),
            ]
        }
        found = {v['uuid']: v['physical_disks'] for v in storage.discover_filesystems(inventory)}
        self.assertEqual(found, {'lv': ['sdb', 'sdc'], 'luks': ['sdd'], 'md': ['sde', 'sdf']})

    def test_two_clones_of_one_drive_still_stop_setup(self):
        from modules import storage

        inventory = {
            'blockdevices': [self.disk('sdb', self.fs('sdb1', 'same')), self.disk('sdc', self.fs('sdc1', 'same'))]
        }
        with self.assertRaisesRegex(RuntimeError, 'cloned drives'):
            storage.discover_filesystems(inventory)

    def test_the_systems_own_btrfs_raid_is_never_offered(self):
        from modules import storage

        inventory = {
            'blockdevices': [
                self.disk('sda', self.fs('sda1', 'root', fstype='btrfs', mountpoint='/')),
                self.disk('sdb', self.fs('sdb1', 'root', fstype='btrfs')),
                self.disk('sdc', self.fs('sdc1', 'ssd')),
            ]
        }
        # Never offered, so never checked for clones either (even where blkid can't say).
        self.assertEqual([v['uuid'] for v in storage.discover_filesystems(inventory, lambda name: None)], ['ssd'])

    def test_a_raid_array_that_lost_a_drive_is_refused_at_setup(self):
        from modules import storage

        with tempfile.TemporaryDirectory() as d:
            md = Path(d) / 'md0' / 'md'
            for member, state in (('dev-sda1', 'in_sync'), ('dev-sdb1', 'in_sync')):
                (md / member).mkdir(parents=True)
                (md / member / 'state').write_text(state + '\n')
            (md / 'degraded').write_text('0\n')
            device = {'name': 'md0', 'kind': 'raid1', 'uuid': 'x'}
            self.assertIs(storage.whole(device, sys_block=d), device)
            (md / 'degraded').write_text('1\n')
            (md / 'dev-sdb1' / 'state').write_text('faulty\n')
            with self.assertRaisesRegex(RuntimeError, 'md0 has lost a drive .*sdb1'):
                storage.whole(device, sys_block=d)
            # Not an array: nothing in /sys is read for it.
            self.assertEqual(storage.whole({'name': 'sdc1', 'kind': 'part'}, sys_block=d)['name'], 'sdc1')

    def test_a_rerun_warns_about_a_degraded_array_instead_of_stopping(self):
        from modules import storage

        with tempfile.TemporaryDirectory() as d:
            md = Path(d) / 'md0' / 'md'
            md.mkdir(parents=True)
            (md / 'degraded').write_text('1\n')
            with mock.patch.object(storage, 'findmnt', return_value='/dev/md0[/@data]'):
                note = storage.raid_note('/srv/nekoserver', sys_block=d)
            self.assertIn('one copy fewer', note)
            self.assertIn('md0', note)
            with mock.patch.object(storage, 'findmnt', return_value='/dev/sdb1'):
                self.assertIsNone(storage.raid_note('/srv/nekoserver', sys_block=d))

    def test_an_encrypted_drive_must_be_unlocked_at_every_start(self):
        from modules import storage

        with tempfile.TemporaryDirectory() as d:
            crypttab = Path(d) / 'crypttab'
            crypttab.write_text('# <name> <device> <key>\nother UUID=1 none\n')
            device = {'name': 'secret', 'kind': 'crypt'}
            self.assertIn('/etc/crypttab', storage.boot_unlock_problem(device, crypttab))
            crypttab.write_text('secret UUID=2 /root/key luks\n')
            self.assertIsNone(storage.boot_unlock_problem(device, crypttab))
            self.assertIsNone(storage.boot_unlock_problem({'name': 'sdb1', 'kind': 'part'}, crypttab))

    def test_a_drive_unlocked_only_by_hand_or_under_an_lvm_volume_is_named(self):
        from modules import storage

        with tempfile.TemporaryDirectory() as d:
            crypttab = Path(d) / 'crypttab'
            # `noauto`: listed, but Linux does not open it at start-up.
            crypttab.write_text('secret UUID=2 /root/key luks,noauto\n')
            self.assertIn(
                '/dev/mapper/secret', storage.boot_unlock_problem({'name': 'secret', 'kind': 'crypt'}, crypttab)
            )
            # An LVM volume on an encrypted drive opened by hand: the drive under it is named.
            lv = self.fs('vg-data', 'lv', kind='lvm')
            inventory = {
                'blockdevices': [
                    self.disk('sda', self.fs('sda1', 'os', mountpoint='/')),
                    self.disk(
                        'sdb',
                        self.fs(
                            'sdb1',
                            None,
                            fstype='crypto_LUKS',
                            children=[self.fs('cryptdata', None, kind='crypt', fstype='LVM2_member', children=[lv])],
                        ),
                    ),
                ]
            }
            [volume] = storage.discover_filesystems(inventory)
            self.assertEqual(volume['unlocks'], ['cryptdata'])
            crypttab.write_text('other UUID=1 none\n')
            self.assertIn('/dev/mapper/cryptdata', storage.boot_unlock_problem(volume, crypttab))
            crypttab.write_text('cryptdata UUID=3 none luks\n')
            self.assertIsNone(storage.boot_unlock_problem(volume, crypttab))


class SharedLockTests(unittest.TestCase):
    def test_the_lock_never_makes_folders_on_the_system_disk(self):
        import recovery_tools

        with tempfile.TemporaryDirectory() as d:
            config = {'data_mount': d}  # an empty mount point: the SSD is not mounted
            with self.assertRaisesRegex(RuntimeError, 'not mounted'), recovery_tools.lock(config, changes_apps=False):
                pass
            self.assertFalse((Path(d) / 'installer').exists())


class FstabTests(unittest.TestCase):
    def test_a_line_for_the_drive_or_folder_written_another_way_is_found(self):
        # Only "UUID=<uuid>" and the exact folder text were recognised, so a second line
        # was added: the first one wins at boot and mounts something else there.
        from modules import storage

        entry = storage.fstab_line('abcd-1234', 'ext4', '/srv/nekoserver')
        devices = {
            '/dev/sdb1': 'abcd-1234',
            'LABEL=ssd': 'abcd-1234',
            'PARTUUID=9-1': 'abcd-1234',
            '/dev/sdc1': 'other',
        }
        for theirs in (
            '/dev/disk/by-uuid/abcd-1234 /data ext4 defaults 0 2',
            'UUID="abcd-1234" /data ext4 defaults 0 2',
            '/dev/sdb1 /data ext4 defaults 0 2',
            'LABEL=ssd /data ext4 defaults 0 2',
            'PARTUUID=9-1 /data ext4 defaults 0 2',
            '/dev/sdc1 /srv/nekoserver/ ext4 defaults 0 2',
            'UUID=zz /srv/nek\\040oserver ext4 defaults 0 2'.replace('nek\\040oserver', 'nekoserver'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'NekoServer did not write', msg=theirs):
                storage.existing_line([theirs], 'abcd-1234', '/srv/nekoserver', entry, resolve=devices.get)
        # Another drive at another folder: nothing to do with it.
        self.assertIsNone(
            storage.existing_line(
                ['/dev/sdc1 /mnt/other ext4 defaults 0 2'], 'abcd-1234', '/srv/nekoserver', entry, resolve=devices.get
            )
        )
        # A drive that isn't connected can't be told apart: not taken for this one.
        self.assertIsNone(
            storage.existing_line(
                ['LABEL=gone /mnt/gone ext4 defaults 0 2'], 'abcd-1234', '/srv/nekoserver', entry, resolve=devices.get
            )
        )
        # NekoServer's own line is still its own.
        self.assertEqual(storage.existing_line([entry], 'abcd-1234', '/srv/nekoserver', entry, resolve=devices.get), 0)

    def test_a_replaced_drive_takes_over_nekoservers_own_fstab_line(self):
        # The SSD broke and a new one was put in (RECOVERY.md): the line NekoServer wrote
        # for the old drive is rewritten for the new one, and setup says so.
        from modules import storage

        entry = storage.fstab_line('new-1111', 'ext4', '/srv/nekoserver')
        old = storage.fstab_line('old-9999', 'ext4', '/srv/nekoserver')
        notes = []
        self.assertEqual(
            storage.existing_line(
                [old], 'new-1111', '/srv/nekoserver', entry, resolve=dict().get, connected=lambda u: False, notes=notes
            ),
            0,
        )
        self.assertIn('old-9999', notes[0])
        self.assertIn('not connected', notes[0])
        # The old drive is still connected: that is someone's drive, so setup stops as before.
        with self.assertRaisesRegex(RuntimeError, 'did not write'):
            storage.existing_line(
                [old], 'new-1111', '/srv/nekoserver', entry, resolve=dict().get, connected=lambda u: True
            )
        # A line with the folder but not NekoServer's options is never taken over.
        theirs = 'UUID=old-9999 /srv/nekoserver ext4 defaults 0 2'
        with self.assertRaisesRegex(RuntimeError, 'already has a line'):
            storage.existing_line(
                [theirs], 'new-1111', '/srv/nekoserver', entry, resolve=dict().get, connected=lambda u: False
            )
        # The whole line is rewritten in the file, and the change is said.
        with tempfile.TemporaryDirectory() as d:
            fstab = Path(d) / 'fstab'
            fstab.write_text('# keep\n' + old + '\n')
            said = []
            self.assertTrue(
                storage.install_fstab([('data_ssd', 'new-1111', 'ext4', '/srv/nekoserver', None)], fstab, said.append)
            )
            self.assertEqual(fstab.read_text(), '# keep\n' + entry + '\n')
            self.assertEqual(len(said), 1)

    def test_the_first_fstab_copy_is_never_left_cut_off(self):
        from modules import storage

        with tempfile.TemporaryDirectory() as d:
            fstab = Path(d) / 'fstab'
            fstab.write_text('UUID=os / ext4 defaults 0 1\n')
            spec = [('data_ssd', 'abcd-1234', 'ext4', str(Path(d) / 'ssd'), None)]
            self.assertTrue(storage.install_fstab(spec, fstab))
            backup = Path(d) / 'fstab.nekoserver.bak'
            self.assertEqual(backup.read_text(), 'UUID=os / ext4 defaults 0 1\n')
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ['fstab', 'fstab.nekoserver.bak'])

    def test_a_slow_mount_is_waited_for(self):
        from modules import storage

        self.assertGreaterEqual(storage.MOUNT_WAIT, 300)
        seen = []
        with (
            mock.patch.object(storage, 'findmnt', side_effect=[None, '/dev/sdb1', '/dev/sdb1']),
            mock.patch.object(storage, 'call', side_effect=lambda argv, timeout=30: seen.append(timeout) or ''),
            mock.patch.object(storage, 'mounted_filesystem', return_value=('abcd-1234', 'btrfs')),
            mock.patch.object(storage, 'mounted_subvolume', return_value=None),
            mock.patch.object(storage.Path, 'mkdir'),
            mock.patch.object(storage.Path, 'exists', return_value=True),
        ):
            storage.mount_and_check('abcd-1234', '/srv/nekoserver', None)
        self.assertEqual(seen, [storage.MOUNT_WAIT])

    def test_a_btrfs_pool_missing_a_drive_is_mounted_on_the_drives_left(self):
        from modules import storage

        short = 'Label: none  uuid: p-1\n\tTotal devices 2 FS bytes used 1.00GiB\n\tdevid 1 size 50.00GiB path /dev/sdb1\n\t*** Some devices missing\n'
        whole = short.replace('\t*** Some devices missing\n', '\tdevid 2 size 50.00GiB path /dev/sdc1\n')

        def call(argv, timeout=30):
            seen.append(argv)
            if argv[:2] == ['btrfs', 'filesystem']:
                return answers['show']
            if argv == ['mount', '/mnt/nekoserver-media']:
                raise RuntimeError('mount failed: wrong fs type, bad option, bad superblock')
            return ''

        said = []
        # A drive gone: mounted with `degraded`, and setup says so.
        seen, answers = [], {'show': short}
        with mock.patch.object(storage, 'call', call):
            storage.mount_target('/mnt/nekoserver-media', 'p-1', 'btrfs', said.append)
        self.assertEqual(seen[-1], ['mount', '-o', 'degraded', '/mnt/nekoserver-media'])
        self.assertIn('mounted on the drives left (degraded)', said[0])
        # A whole pool that still fails to mount: the failure stands, nothing is forced.
        seen, answers = [], {'show': whole}
        with mock.patch.object(storage, 'call', call), self.assertRaises(RuntimeError):
            storage.mount_target('/mnt/nekoserver-media', 'p-1', 'btrfs')
        self.assertNotIn(['mount', '-o', 'degraded', '/mnt/nekoserver-media'], seen)
        # Not Btrfs: the tool is not even asked.
        seen = []
        with mock.patch.object(storage, 'call', call), self.assertRaises(RuntimeError):
            storage.mount_target('/mnt/nekoserver-media', 'p-1', 'ext4')
        self.assertEqual(seen, [['mount', '/mnt/nekoserver-media']])


class DockerSettingsTests(unittest.TestCase):
    def test_a_setting_dockers_service_already_sets_stops_before_anything_changes(self):
        from modules import docker

        content = {'data-root': '/srv/nekoserver/docker-engine', 'log-opts': {'max-size': '10m'}}
        shown = mock.Mock(
            returncode=0,
            stdout='{ path=/usr/bin/dockerd ; argv[]=/usr/bin/dockerd -H fd:// --data-root=/data/docker ; }',
        )
        with self.assertRaisesRegex(RuntimeError, 'data-root on its command line'):
            docker.validate_daemon(content, run=lambda *a, **k: shown)
        self.assertEqual(docker.flag_conflicts(content, '/usr/bin/dockerd --log-opt max-size=5m'), ['log-opts'])
        self.assertEqual(docker.flag_conflicts(content, '/usr/bin/dockerd -H fd://'), [])

    def test_a_daemon_json_docker_refuses_is_never_written(self):
        from modules import docker

        def run(argv, **kw):
            if '--validate' in argv:
                return mock.Mock(returncode=1, stdout='', stderr='invalid configuration: unknown key "nope"')
            return mock.Mock(returncode=0, stdout='/usr/bin/dockerd -H fd://')

        with mock.patch.object(docker.shutil, 'which', return_value='/usr/bin/dockerd'):
            with self.assertRaisesRegex(RuntimeError, 'refuses the new'):
                docker.validate_daemon({'nope': 1}, run=run)

            # Docker before 23 has no --validate: not taken for a refusal.
            def old(argv, **kw):
                return mock.Mock(returncode=125, stdout='', stderr='unknown flag: --validate')

            docker.validate_daemon({}, run=old)


class ReadBackTests(unittest.TestCase):
    def test_a_restored_file_is_read_back_from_the_drive_not_from_memory(self):
        import neko_common

        advised = []
        real = neko_common.os.posix_fadvise
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'a').write_bytes(b'photo')
            with mock.patch.object(
                neko_common.os,
                'posix_fadvise',
                side_effect=lambda fd, o, n, how: advised.append(how) or real(fd, o, n, how),
            ):
                neko_common.copy_checked(Path(d) / 'a', Path(d) / 'b')
        self.assertIn(neko_common.os.POSIX_FADV_DONTNEED, advised)


class SambaShareTests(unittest.TestCase):
    def test_a_share_path_cannot_add_settings_of_its_own(self):
        from modules import samba

        self.assertEqual(samba.validate_share('Media', 'library/My Movies'), Path('library/My Movies'))
        for bad in ('library\n   root preexec = /bin/sh -c x', 'library/%U', 'a\\b'):
            with self.assertRaisesRegex(RuntimeError, 'may use only'):
                samba.validate_share('Media', bad)


class SnapshotErrorTests(unittest.TestCase):
    def test_a_failed_restart_never_hides_why_the_snapshot_failed(self):
        import io

        import recovery_tools

        err = io.StringIO()
        with (
            mock.patch.object(recovery_tools, 'restart_owed', side_effect=RuntimeError('docker is down')),
            mock.patch('sys.stderr', err),
        ):
            recovery_tools.restart_again({}, [], failed=True)  # said, not raised
            self.assertIn('docker is down', err.getvalue())
            with self.assertRaisesRegex(RuntimeError, 'docker is down'):
                recovery_tools.restart_again({}, [], failed=False)  # the snapshot passed: this is the error


class AnswerRaceTests(unittest.TestCase):
    def test_only_one_side_is_told_its_answer_was_used(self):
        import threading

        import setup_progress

        pending = {'seq': 7, 'answer': None, 'event': threading.Event()}
        with mock.patch.object(setup_progress, '_pending', pending):
            self.assertTrue(setup_progress._claimed_by_terminal(pending, 'yes'))
            self.assertFalse(setup_progress.answer_from_window(7, 'no'))  # too late: told so
            self.assertEqual(pending['answer'], 'yes')
        # The window first: the terminal's line gives way.
        other = {'seq': 8, 'answer': None, 'event': threading.Event()}
        with mock.patch.object(setup_progress, '_pending', other):
            self.assertTrue(setup_progress.answer_from_window(8, 'no'))
            self.assertFalse(setup_progress._claimed_by_terminal(other, 'yes'))
            self.assertEqual(other['answer'], 'no')


class StateMoveTests(unittest.TestCase):
    def test_an_old_record_on_the_system_disk_never_undoes_newer_results(self):
        here = {'system': {'status': 'success'}, 'storage': {'status': 'success'}, 'docker': {'status': 'blocked'}}
        on_ssd = {'docker': {'status': 'success'}, 'media': {'status': 'success'}}
        merged = installer.moved_states(on_ssd, here, ['system', 'storage'])
        self.assertEqual(merged['docker'], {'status': 'success'})  # the SSD's newer result
        self.assertEqual(merged['storage'], {'status': 'success'})  # what this run did
        self.assertEqual(merged['media'], {'status': 'success'})
        with tempfile.TemporaryDirectory() as d:
            old = Path(d) / 'state.json'
            old.write_text('{}')
            installer.retire(old)
            self.assertFalse(old.exists())
            self.assertTrue((Path(d) / 'state.json.moved-to-ssd').exists())


class AccountStoreTests(unittest.TestCase):
    def test_a_first_save_cut_off_does_not_lock_the_store_for_good(self):
        import account_store

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'secrets/accounts'
            root.mkdir(parents=True, mode=0o700)
            (root / '.credentials.json-abc123').write_text('{"half": ')  # left by a power cut
            with mock.patch.object(account_store, 'check_parent'), mock.patch.object(account_store, 'private'):
                value = account_store.load({'data_mount': d})
            self.assertEqual(value['apps'], {})
            self.assertEqual(list(root.iterdir()), [])
            (root / 'something-else').write_text('x')  # anything else is still left for a person
            with (
                mock.patch.object(account_store, 'check_parent'),
                mock.patch.object(account_store, 'private'),
                self.assertRaisesRegex(RuntimeError, 'Incomplete account store'),
            ):
                account_store.load({'data_mount': d})

    def test_the_store_is_read_and_written_only_on_the_ssd(self):
        """The SSD's path comes from the settings file: a store folder that does not lie on
        the SSD is refused, and nothing is read or made there."""
        import account_store

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as elsewhere:
            config = {'data_mount': d}
            outside = Path(elsewhere) / 'accounts'
            value = {'format': 1, 'mode': 'per-app', 'apps': {}}
            with (
                mock.patch.object(account_store, 'folder', return_value=outside),
                mock.patch.object(account_store, 'check_parent') as looked,
            ):
                with self.assertRaisesRegex(RuntimeError, 'must lie on the app SSD'):
                    account_store.load(config)
                with self.assertRaisesRegex(RuntimeError, 'must lie on the app SSD'):
                    account_store.save(config, value)
            looked.assert_not_called()
            self.assertEqual(list(Path(elsewhere).iterdir()), [])
            # On the SSD it is made and written as before.
            with mock.patch.object(account_store, 'check_parent'), mock.patch.object(account_store, 'private'):
                account_store.save(config, value)
                self.assertEqual(account_store.load(config)['apps'], {})
            self.assertEqual(oct((Path(d) / 'secrets/accounts').stat().st_mode & 0o777), '0o700')


class UsbPickerSystemDiskTests(unittest.TestCase):
    def test_a_btrfs_system_disk_showing_home_is_still_the_system_disk(self):
        from modules import photo_backup

        config = {'storage': {}, 'external_backup_mount': '/mnt/usb'}
        inventory = {
            'blockdevices': [
                {
                    'name': 'sda',
                    'type': 'disk',
                    'children': [
                        {
                            'name': 'sda2',
                            'type': 'part',
                            'fstype': 'btrfs',
                            'uuid': 'root',
                            'mountpoint': '/home',
                            'mountpoints': ['/home', '/', '/.snapshots'],
                        },
                        {'name': 'sda3', 'type': 'part', 'fstype': 'ext4', 'uuid': 'spare'},
                    ],
                }
            ]
        }
        self.assertEqual(photo_backup.eligible(config, inventory), [])

    def test_every_disk_of_a_btrfs_system_pool_is_the_system_disk(self):
        # A root pool over sda2 and sdb1: lsblk shows the mount on one member only.
        from modules import photo_backup

        config = {'storage': {}, 'external_backup_mount': '/mnt/usb'}
        inventory = {
            'blockdevices': [
                {
                    'name': 'sda',
                    'type': 'disk',
                    'children': [
                        {'name': 'sda2', 'type': 'part', 'fstype': 'btrfs', 'uuid': 'pool', 'mountpoints': ['/']}
                    ],
                },
                {
                    'name': 'sdb',
                    'type': 'disk',
                    'children': [
                        {'name': 'sdb1', 'type': 'part', 'fstype': 'btrfs', 'uuid': 'pool'},
                        {'name': 'sdb2', 'type': 'part', 'fstype': 'ext4', 'uuid': 'spare'},
                    ],
                },
                {
                    'name': 'sdc',
                    'type': 'disk',
                    'children': [{'name': 'sdc1', 'type': 'part', 'fstype': 'ext4', 'uuid': 'usb'}],
                },
            ]
        }
        self.assertEqual([r['uuid'] for r in photo_backup.eligible(config, inventory)], ['usb'])
        self.assertIn('sdb', photo_backup.shared_disk(config, inventory, 'spare'))


if __name__ == '__main__':
    unittest.main()


class StaleProfileTests(unittest.TestCase):
    """A settings file keeps the profiles it was made with: on a server set up before
    0.28.7 the desktop step (and before 0.24.1 the graphics-chip video step) never ran,
    and the finishing screen still said "ready"."""

    def setUp(self):
        self.template = json.loads((installer.ROOT / 'config.json').read_text())
        self.old = json.loads(json.dumps(self.template))
        self.old['project_name'] = 'NekoServer v0.23.1'
        full = self.old['profiles']['full']
        for step in ('desktop', 'hardware_video', 'samba'):  # samba: taken out by its owner
            full.remove(step)

    def test_steps_added_since_are_merged_and_steps_taken_out_stay_out(self):
        import config_checks

        lines, changed = config_checks.merge_profiles(self.old, self.template)
        full = self.old['profiles']['full']
        self.assertTrue(changed)
        self.assertIn('desktop', full)
        self.assertIn('hardware_video', full)
        self.assertNotIn('samba', full)  # in the 0.23.1 template already: taken out on purpose
        self.assertLess(full.index('dashboard'), full.index('desktop'))  # in the template's order
        self.assertEqual(len(lines), 2)
        # Taken out again after the merge: the record now knows it, so it stays out.
        full.remove('desktop')
        lines, _ = config_checks.merge_profiles(self.old, self.template)
        self.assertEqual(lines, [])
        self.assertNotIn('desktop', self.old['profiles']['full'])
        # A step a later release adds is found through the record.
        newer = json.loads(json.dumps(self.template))
        newer['profiles']['full'].append('future_step')
        lines, _ = config_checks.merge_profiles(self.old, newer)
        self.assertIn('future_step', self.old['profiles']['full'])

    def test_your_own_additions_stay(self):
        import config_checks

        self.old['profiles']['full'].append('my_step')
        config_checks.merge_profiles(self.old, self.template)
        self.assertIn('my_step', self.old['profiles']['full'])

    def test_the_finishing_screen_judges_against_the_templates_steps(self):
        import wizard

        states = {name: {'status': 'success'} for name in self.old['profiles']['full']}
        self.assertFalse(wizard.finishing_screen(self.old, states, None, lambda *_: None, checkup=True))
        states.update(desktop={'status': 'success'}, hardware_video={'status': 'success'})
        self.assertTrue(wizard.finishing_screen(self.old, states, None, lambda *_: None, checkup=True))

    def test_setup_plans_the_new_steps_and_saves_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'my-config.json'
            settings.write_text(json.dumps(self.old))
            args = installer.argparse.Namespace(
                config=str(settings),
                state_dir=tmp + '/state',
                profile='full',
                module=None,
                retry_failed=False,
                check=False,
                dry_run=True,
                apply=False,
            )
            shown = []
            with mock.patch('builtins.print', side_effect=lambda *a, **k: shown.append(' '.join(map(str, a)))):
                installer._execute(args)
            self.assertTrue(any(line.startswith('  desktop:') for line in shown), shown)
            self.assertTrue(any('"hardware_video"' in line for line in shown), shown)
            self.assertNotIn('desktop', json.loads(settings.read_text())['profiles']['full'])  # read-only: not saved


class SavedProfileTests(unittest.TestCase):
    def test_the_added_steps_are_logged_and_saved_only_with_apply(self):
        class Ctx:
            def __init__(self):
                self.lines, self.saved = [], 0

            def log(self, line):
                self.lines.append(line)

            def persist_config(self):
                self.saved += 1

        ctx = Ctx()
        installer.save_profiles(ctx, ['Settings: the step "desktop" ...'], True, False)
        self.assertEqual((ctx.lines, ctx.saved), (['Settings: the step "desktop" ...'], 0))
        installer.save_profiles(ctx, [], True, True)  # only the record of known steps changed
        self.assertEqual(ctx.saved, 1)
