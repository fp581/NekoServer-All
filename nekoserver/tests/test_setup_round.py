"""Setup as a first-time user meets it (the 0.40.1 customer, IT and QA tests): a download
that fails, the setup window opened again, the drive sizes, the drive question, the
finishing screen, and the docs that promise these."""

import argparse
import functools
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import MappingProxyType
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import capabilities
import host
import installer
import setup_gui
import setup_progress
import wizard
from modules import storage

PROXY_403 = 'Tunnel connection failed: 403 Forbidden'
SSD_FOLDER = '/srv/nekoserver'
HDD_FOLDER = '/mnt/nekoserver-media'
DRIVE_FOLDERS = {'data_mount': SSD_FOLDER, 'media_mount': HDD_FOLDER}
STOPPED_ONE = 'Setup stopped: 1 thing needs you'
WAITS_FOR_DOCKER = 'Waits for Docker to finish first'
SETTINGS_NAME = 'my-config.json'
README = 'README.md'
PYTHON_LIST = re.compile(r"\[\s*'")  # "['docker', 'photos']" in words meant for people


def quiet(family):
    """A family whose words and pauses are recorded, not said or slept."""
    family.said, family.slept = [], []
    family.say, family.sleep = family.said.append, family.slept.append
    return family


def ubuntu(fetch):
    return quiet(host.Debian(lambda *a, **k: None, {'ID': 'ubuntu', 'VERSION_CODENAME': 'noble'}, fetch=fetch))


class DownloadTests(unittest.TestCase):
    """D1, IT-04: Docker's key download failed at once, with "check that this PC is
    connected to the internet" right after 28 MB of packages had downloaded, and without
    naming the site."""

    def failing(self, *errors):
        calls = []

        def fetch(url):
            calls.append(url)
            error = errors[min(len(calls), len(errors)) - 1]
            if error is None:
                return b'key'
            raise error

        return calls, fetch

    def test_a_site_that_does_not_answer_is_tried_again_as_package_sources_are(self):
        calls, fetch = self.failing(urllib.error.URLError('timed out'), urllib.error.URLError('timed out'), None)
        family = ubuntu(fetch)
        self.assertEqual(family.download('https://download.docker.com/linux/ubuntu/gpg'), b'key')
        self.assertEqual(len(calls), 3)
        self.assertEqual(family.slept, [host.RETRY_PAUSE] * 2)
        self.assertIn(
            'download.docker.com did not answer (timed out); trying again in 20 seconds (2 of 3)...', family.said
        )

    def test_the_message_names_the_site_and_the_likely_causes(self):
        calls, fetch = self.failing(urllib.error.URLError(PROXY_403))
        family = ubuntu(fetch)
        with self.assertRaises(RuntimeError) as caught:
            family.fetch_key(host.DOCKER, 'ubuntu', 'noble')
        message = str(caught.exception)
        self.assertEqual(len(calls), host.RETRIES)
        self.assertIn("Could not reach download.docker.com (Docker's site)", message)
        self.assertIn('after 3 tries', message)
        self.assertIn('A proxy, firewall or filter on your network refused it', message)
        self.assertIn('Pi-hole', message)
        self.assertIn('https://download.docker.com opens in a browser', message)
        self.assertIn('sudo bash install.sh', message)
        self.assertTrue(message.endswith('again.'))  # a sentence, with its full stop
        self.assertNotIn('Check that this PC is connected to the internet', message)

    def test_no_connection_names_every_likely_cause_not_the_internet_alone(self):
        _, fetch = self.failing(urllib.error.URLError('Temporary failure in name resolution'))
        family = ubuntu(fetch)
        with self.assertRaises(RuntimeError) as caught:
            family.fetch_key(host.TAILSCALE, 'ubuntu', 'noble')
        message = str(caught.exception)
        self.assertIn("Could not reach pkgs.tailscale.com (Tailscale's site)", message)
        for cause in ('blocks that site', 'the site is down', 'offline'):
            self.assertIn(cause, message)

    def test_not_found_and_a_wrong_clock_are_not_asked_again(self):
        for error in (
            urllib.error.HTTPError('u', 404, 'Not Found', {}, None),
            urllib.error.URLError('certificate verify failed: certificate is not yet valid'),
        ):
            with self.subTest(error=error):
                calls, fetch = self.failing(error)
                family = ubuntu(fetch)
                with self.assertRaises(RuntimeError):
                    family.fetch_key(host.DOCKER, 'ubuntu', 'noble')
                self.assertEqual(len(calls), 1)

    def test_an_error_of_the_site_itself_is_asked_again_then_said_plainly(self):
        calls, fetch = self.failing(urllib.error.HTTPError('u', 503, 'Unavailable', {}, None))
        family = ubuntu(fetch)
        with self.assertRaisesRegex(
            RuntimeError,
            r"download\.docker\.com \(Docker's site\) answered with an error 3 times "
            r'\(HTTP 503',
        ):
            family.fetch_key(host.DOCKER, 'ubuntu', 'noble')
        self.assertEqual(len(calls), 3)

    def test_fedora_and_rhel_say_the_same(self):
        calls, fetch = self.failing(urllib.error.URLError(PROXY_403))
        family = quiet(host.Fedora(lambda *a, **k: None, {'ID': 'fedora', 'VERSION_ID': '42'}, fetch=fetch))
        with self.assertRaisesRegex(
            RuntimeError,
            r"Could not reach download\.docker\.com \(Docker's site\) to download the "
            r"list of Docker's packages, after 3 tries",
        ):
            family.check_release(host.DOCKER_RPM)
        self.assertEqual(len(calls), 3)

    def test_the_reason_is_left_for_the_setup_window(self):
        with tempfile.TemporaryDirectory() as d:
            note = Path(d) / 'note'
            note.write_text('')
            with mock.patch('sys.stderr', io.StringIO()) as err:
                host.stopped('Could not reach x.', str(note))
            self.assertEqual(note.read_text(), 'Could not reach x.\n')
            self.assertIn('ERROR: Could not reach x.', err.getvalue())
            # Only a file install.sh made: never one created here, never through a link.
            with mock.patch('sys.stderr', io.StringIO()):
                host.stopped('y', str(Path(d) / 'missing'))
                (Path(d) / 'link').symlink_to(note)
                host.stopped('z', str(Path(d) / 'link'))
            self.assertFalse((Path(d) / 'missing').exists())
            self.assertEqual(note.read_text(), 'Could not reach x.\n')

    def test_install_sh_shows_the_reason_in_the_window_and_tidies_up(self):
        script = (ROOT / 'install.sh').read_text()
        self.assertIn('export NEKOSERVER_STOP_NOTE="$stop_note"', script)
        self.assertIn('trap show_stop_note EXIT', script)
        self.assertIn('python3 "$ROOT/setup_gui.py" stopped "$stop_note"', script)
        self.assertLess(script.index('trap show_stop_note EXIT'), script.index('python3 "$ROOT/host.py" bootstrap'))
        self.assertLess(script.index('trap - EXIT'), script.index('exec python3 "$ROOT/installer.py"'))


class StoppedWindowTests(unittest.TestCase):
    """D1: setup stopped before the wizard, and no window ever appeared."""

    def test_the_window_opens_to_say_why_setup_stopped(self):
        seen = {}

        def browser(config, url):
            # As the browser does: the page, then its state.
            import http.cookiejar

            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            opener.open(url, timeout=5).read()
            seen.update(json.loads(opener.open(url + 'api/state', timeout=5).read()))
            return True

        with tempfile.TemporaryDirectory() as d:
            note = mktemp_note(self)
            note.write_text(
                "Could not reach download.docker.com (Docker's site). Check that it opens in a browser, then run  "
                "sudo bash install.sh  again.\n"
            )
            out = []
            self.assertTrue(setup_gui.show_stopped(note, Path(d) / SETTINGS_NAME, out.append, wait=5, opener=browser))
        self.assertEqual(seen['phase'], 'finished')
        self.assertTrue(seen['stopped'])
        self.assertEqual(seen['title'], setup_gui.STOPPED_TITLE)
        self.assertIn('download.docker.com', seen['finish'][0])
        self.assertEqual(' '.join(seen['finish']).count('sudo bash install.sh'), 1)  # what to do, once
        self.assertEqual(seen['total'], 0)
        self.assertEqual(out, ['The setup window shows this too.'])

    def test_the_reason_once_and_what_to_do_once(self):
        # QA (0.40.2 check): "run  sudo bash install.sh  again" was said twice.
        said = setup_gui.stop_lines('Could not reach x. Then run  sudo bash install.sh  again.')
        self.assertEqual(' '.join(said).count('install.sh'), 1)
        self.assertEqual(said[-1], setup_gui.NOTHING_TOUCHED)
        said = setup_gui.stop_lines('apt-get failed: E: Unable to locate package caddy')
        self.assertEqual(said[-1], setup_gui.NOTHING_TOUCHED + ' ' + setup_gui.AGAIN)

    def test_the_retry_lines_come_before_the_error_in_one_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'out.txt'
            code = (
                'import sys; sys.path.insert(0, sys.argv[1]); import host; print("trying again (2 of 3)..."); '
                'host.stopped("Could not reach x.", "")'
            )
            with out.open('w') as stream:
                subprocess.run(
                    [sys.executable, '-c', code, str(ROOT)], stdout=stream, stderr=subprocess.STDOUT, check=False
                )
            text = out.read_text()
        self.assertLess(text.index('trying again'), text.index('ERROR: Could not reach x.'))

    def test_without_a_desktop_or_a_reason_nothing_is_opened(self):
        with tempfile.TemporaryDirectory() as d:
            elsewhere = Path(d) / 'note'
            elsewhere.write_text('x')
            # Only the file install.sh made (mktemp, in the temporary folder) is read.
            self.assertFalse(
                setup_gui.show_stopped(
                    elsewhere, Path(d) / 'none.json', output=self.fail, opener=lambda config, url: self.fail('opened')
                )
            )
            note = mktemp_note(self)
            note.write_text('x')
            out = []
            self.assertFalse(
                setup_gui.show_stopped(
                    note, Path(d) / 'none.json', out.append, wait=1, opener=lambda config, url: False
                )
            )
            self.assertEqual(out, [])
            note.write_text('')
            self.assertFalse(setup_gui.show_stopped(note, output=self.fail))


def mktemp_note(test):
    """A reason file as install.sh makes it (mktemp: /tmp/tmp.XXXXXXXXXX)."""
    handle, name = tempfile.mkstemp(prefix='tmp.')
    os.close(handle)
    test.addCleanup(os.unlink, name)
    return Path(name)


class WindowAgainTests(unittest.TestCase):
    """D2: "Closed it by mistake? Open the address again" was refused."""

    def test_the_same_account_or_root_opens_it_again_with_a_new_session(self):
        with tempfile.TemporaryDirectory() as d:
            window = setup_gui.Window(Path(d) / SETTINGS_NAME, {}, wizard.STEPS, [], owners={0, 1000})
            self.addCleanup(window.stop)
            allowed, cookie, _ = window.admit('', None, 'GET', lambda: 1000)
            self.assertTrue(allowed)
            first = cookie
            self.assertEqual(window.admit('', None, 'GET', lambda: 2000)[2], setup_gui.SPENT)  # another account
            self.assertEqual(window.admit('api/state', None, 'GET', lambda: 1000)[2], setup_gui.SPENT)
            self.assertEqual(window.admit('', None, 'POST', lambda: 1000)[2], setup_gui.SPENT)
            allowed, again, _ = window.admit('', None, 'GET', lambda: 1000)
            self.assertTrue(allowed)
            self.assertNotEqual(again, first)
            self.assertFalse(window.admit('api/state', setup_gui.COOKIE + '=' + first, 'GET', lambda: 1000)[0])
            self.assertTrue(window.admit('api/state', setup_gui.COOKIE + '=' + again, 'GET', lambda: 1000)[0])
            self.assertTrue(window.admit('', None, 'GET', lambda: 0)[0])

    def test_the_refusal_is_a_page_that_wraps(self):
        for code, body, kind in (setup_gui.SPENT, setup_gui.OTHER_ACCOUNT):
            self.assertEqual((code, kind), (403, setup_gui.HTML))
            self.assertIn(b'<meta name="viewport"', body)
            self.assertNotIn(b'Ctrl+C', body)


class WindowPackageTests(unittest.TestCase):
    """D9: the TV card listed Arch's Intel-only package names on Ubuntu."""

    def test_the_tv_card_names_this_linuxs_packages_for_this_pcs_chip(self):
        debian = host.Debian(lambda *a, **k: None, {'ID': 'ubuntu', 'VERSION_CODENAME': 'noble'})
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(host, 'current', return_value=debian),
            mock.patch('gpu.graphics', return_value=['amd']),
        ):
            window = setup_gui.Window(Path(d) / SETTINGS_NAME, {}, wizard.STEPS, [])
            self.addCleanup(window.stop)
            tv = next(e for e in window._extras() if e['id'] == 'tv')
        names = [p['name'] for p in tv['installs']]
        self.assertEqual(
            names[:4], ['steam-installer', 'steam-devices', 'mesa-vulkan-drivers', 'mesa-vulkan-drivers:i386']
        )
        self.assertNotIn('vulkan-intel', names)
        self.assertIn('Vulkan', tv['installs'][2]['explanation'] + 'Vulkan')  # with what it is for

    def test_the_button_saves_every_switch_so_nothing_is_asked_later(self):
        """Until the wizard of 0.50.0 a switch left alone was "asked later", by its step.
        Now the first page is where each is chosen: pressing the button saves every one
        as it stands, and a step that finds its answer does not ask."""
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(setup_gui, 'family_packages', return_value={'names': lambda n: [n], 'vulkan': []}),
            mock.patch.object(setup_gui.neko_common, 'server_exists', return_value=False),
        ):
            path = Path(d) / SETTINGS_NAME
            path.write_text(json.dumps({'tv': {'wanted': True}}))
            window = setup_gui.Window(path, {'tv': {'wanted': True}}, wizard.STEPS, [])
            self.addCleanup(window.stop)
            self.assertTrue(all('asked' not in extra for extra in window._extras()))
            self.assertTrue(window.choose({'media_music': False}, start=True))
            saved = json.loads(path.read_text())
        self.assertEqual(
            saved,
            {
                'tv': {'wanted': True},  # chosen before: kept
                # A new server: the apps are offered on, with finding and downloading; the extras off.
                'media_apps': {
                    'photos': True,
                    'movies': True,
                    'music': False,
                    'movies_downloads': True,
                    'music_downloads': True,
                },
                'nextcloud': {'enabled': True},
                'notifications': {'wanted': False},
                'heartbeat': {'wanted': False},
                'assistant': {'wanted': False},
                'network_storage': {'wanted': False},
            },
        )


class WindowPageTests(unittest.TestCase):
    """setup.js in Node (skipped without Node): D4, D5, D2 and D9 as the page shows them."""

    def page(self, states, action=''):
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

    @staticmethod
    def state(phase, **more):
        return {
            'version': 'x',
            'phase': phase,
            'extras': [],
            'steps': [],
            'current': None,
            'done': 0,
            'total': 0,
            'question': None,
            'finish': [],
            'title': '',
            'complete': False,
            **more,
        }

    FINISHED = MappingProxyType(
        {
            'steps': [
                {'id': 'docker', 'title': 'Docker', 'status': 'failed', 'reason': 'Could not reach x'},
                {'id': 'media', 'title': 'Movie, TV and music apps', 'status': 'blocked', 'reason': WAITS_FOR_DOCKER},
            ],
            'finish': ['', '=' * 64, '  Setup stopped: 1 thing needs you', '=' * 64, 'Done: 0 of 2 steps.'],
            'title': STOPPED_ONE,
            'done': 0,
            'total': 2,
        }
    )

    def test_waiting_is_not_a_failure(self):
        seen = self.page([self.state('finished', **self.FINISHED)])
        self.assertEqual(
            seen['marks'],
            [['failed', '✕', 'Docker — did not finish'], ['blocked', '…', 'Movie, TV and music apps — waiting']],
        )
        self.assertEqual(seen['title'], STOPPED_ONE)
        self.assertNotIn('Setup stopped', seen['lines'])  # the title is not said twice
        self.assertEqual(seen['lines'], 'Done: 0 of 2 steps.')

    def test_the_finishing_screen_stays_once_setup_has_closed(self):
        gone = {'fail': 'gone'}
        seen = self.page([self.state('finished', **self.FINISHED), gone, gone, gone, gone, gone])
        self.assertEqual(seen['shown'], ['finished', 'finish-closed'])
        self.assertEqual(seen['badge'], 'Setup closed')
        self.assertEqual(seen['title'], STOPPED_ONE)
        # Before the end, a setup that went away still says so.
        seen = self.page([self.state('running'), gone, gone, gone, gone, gone])
        self.assertEqual(seen['shown'], ['gone'])

    def test_a_window_opened_again_elsewhere_says_so(self):
        seen = self.page([self.state('running'), {'fail': 403}])
        self.assertEqual(seen['shown'], ['moved'])
        self.assertEqual(seen['badge'], 'Opened elsewhere')

    def test_setup_stopped_before_it_began_has_no_step_count(self):
        seen = self.page(
            [
                self.state(
                    'finished',
                    finish=['Could not reach download.docker.com.'],
                    title=setup_gui.STOPPED_TITLE,
                    stopped=True,
                )
            ]
        )
        self.assertTrue(seen['progressHidden'])
        self.assertEqual((seen['label'], seen['badge']), ('STOPPED', 'Stopped'))  # stopped, not "finished"
        self.assertEqual(seen['title'], setup_gui.STOPPED_TITLE)
        seen = self.page([self.state('finished', **self.FINISHED)])
        self.assertEqual(seen['label'], 'FINISHED')

    def test_every_switch_says_yes_or_no_and_a_press_is_saved_at_once(self):
        extras = [
            {'id': i, 'title': i, 'explanation': '', 'installs': [], 'on': i == 'nextcloud'}
            for i in ('tv', 'nextcloud')
        ]
        seen = self.page([self.state('choose', extras=extras)])
        # Nothing is "asked later" since the wizard of 0.50.0: every switch is a choice made
        # here. (A card that is on shows it: a ring inside its border.)
        self.assertEqual(seen['cards'], [['extra', 'no', False], ['extra on', 'yes', False]])
        seen = self.page([self.state('choose', extras=extras, press='tv')])
        self.assertEqual(seen['cards'][0], ['extra on', 'yes', False])
        self.assertEqual(seen['posts'], [['api/choose', {'tv': True}]])


class FinishingScreenTests(unittest.TestCase):
    """D4, D12: about 25 lines of "Dependencies not successful: ['storage', 'recovery']",
    each a red ✕; "almost ready" at 1 of 30; three different step counts."""

    STATES = MappingProxyType(
        {
            'system': {'status': 'success'},
            'storage': {'status': 'success'},
            'docker': {'status': 'failed', 'reason': "Could not reach download.docker.com (Docker's site)."},
            'recovery_checkpoint': {'status': 'blocked', 'waiting_for': ['docker'], 'reason': WAITS_FOR_DOCKER},
            'media': {'status': 'blocked', 'waiting_for': ['docker'], 'reason': WAITS_FOR_DOCKER},
            # A record from 0.40.1: the step names in its reason, the cause two steps up.
            'music': {'status': 'blocked', 'reason': "Dependencies not successful: ['media', 'recovery']"},
            'recovery': {'status': 'success'},
            'tv': {'status': 'unavailable', 'reason': 'No desktop on this PC'},
            'music_backup': {'status': 'success'},
        }
    )

    def screen(self, states, ran=()):
        out = []
        with tempfile.TemporaryDirectory() as d:
            config = {'data_mount': d, 'profiles': {'full': [n for n in states if n != 'music_backup']}}
            complete = wizard.finishing_screen(config, states, None, out.append, ran=ran)
        return complete, '\n'.join(out)

    def test_plain_words_the_cause_first_and_what_waits_on_it(self):
        complete, text = self.screen(self.STATES)
        self.assertFalse(complete)
        self.assertIsNone(PYTHON_LIST.search(text), text)
        self.assertNotIn('Dependencies not successful', text)
        self.assertNotIn('music_backup', text)
        needs = text.index('What needs you')
        self.assertIn("  - Docker: Could not reach download.docker.com (Docker's site).", text[needs:])
        self.assertIn('  - After Docker: Recovery checkpoint, Movie, TV and music apps, Music', text)
        self.assertLess(needs, text.index('After Docker'))
        self.assertIn('Done: 3 of 7 steps.', text)
        self.assertIn(STOPPED_ONE, text)  # fewer than half done
        self.assertIn('1. Fix the first item', text)

    def test_fewer_than_half_done_is_not_almost_ready(self):
        states = {
            **self.STATES,
            'storage': {'status': 'failed', 'reason': 'No drive was chosen'},
            'recovery': {'status': 'blocked', 'waiting_for': ['storage']},
        }
        _, text = self.screen(states)
        self.assertIn('  Setup stopped: 2 things need you', text)

    def test_the_count_is_the_installers(self):
        # The installer ran music_backup too (the music step needs it): the screen counts it.
        _, text = self.screen(self.STATES, ran=['music_backup', *self.STATES])
        self.assertIn('Done: 4 of 8 steps.', text)
        self.assertIn('NekoServer is almost ready', text)  # half done, one thing left

    def test_the_window_gets_the_same_title_and_count(self):
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)
        setup_progress.event('plan', steps=['system', 'storage', 'docker', 'tv'])
        for name, status in (
            ('system', 'success'),
            ('storage', 'success'),
            ('docker', 'failed'),
            ('tv', 'unavailable'),
        ):
            setup_progress.step(name, status)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / SETTINGS_NAME
            path.write_text(json.dumps({'data_mount': d, 'profiles': {'full': ['system', 'storage']}}))
            window = setup_gui.Window(path, {}, wizard.STEPS, ['system', 'storage'])
            self.addCleanup(window.stop)
            window.delivered.set()
            states = {
                'system': {'status': 'success'},
                'storage': {'status': 'success'},
                'docker': {'status': 'failed', 'reason': 'x'},
                'tv': {'status': 'unavailable'},
            }
            with (
                mock.patch.object(
                    wizard,
                    'read_json',
                    side_effect=lambda p: (
                        states if 'state' in str(p) else json.loads(path.read_text()) if p == path else {}
                    ),
                ),
                mock.patch('builtins.print'),
            ):
                wizard.after_setup(1, path, Path(d), lambda *a: None, window)
            state = window.state()
        self.assertEqual((state['done'], state['total']), (2, 3))
        self.assertIn('\nDone: 2 of 3 steps.', state['finish'])
        self.assertEqual(state['title'], 'NekoServer is almost ready')


class LoginCommandTests(unittest.TestCase):
    """The passwords are shown one app at a time, with a QR code (account-setup.py show),
    not by reading the root-only file with sudo cat."""

    def test_the_finishing_screen_and_the_login_check_name_the_show_command(self):
        import guided_setup

        command = guided_setup.login_command({'data_mount': '/srv/ssd/'}, 'jellyfin')
        self.assertEqual(
            command, f'sudo python3 /srv/ssd/installer/releases/v{wizard.VERSION}/account-setup.py show jellyfin'
        )
        _, text = FinishingScreenTests().screen({'storage': {'status': 'success'}})
        self.assertIn('account-setup.py show <app>', text)
        self.assertNotIn('sudo cat', text)
        self.assertNotIn('APP-CREDENTIALS.txt', (ROOT / 'guided_setup.py').read_text())
        guide = (ROOT / README).read_text()
        self.assertIn('account-setup.py show jellyfin', guide)
        self.assertIn('/etc/fstab.nekoserver.bak', guide)


class InstallerLogTests(unittest.TestCase):
    """D4, IT-03: blocked steps named their dependencies as a Python list, and NEEDS SETUP
    lines in the terminal and install.log gave no reason."""

    def run_plan(self, results):
        modules = {
            'storage': {'deps': []},
            'docker': {'deps': ['storage']},
            'media': {'deps': ['docker']},
            'music': {'deps': ['media', 'storage']},
            'tv': {'deps': []},
        }
        for name, result in results.items():
            modules[name]['object'] = mock.Mock(run=mock.Mock(**result), DESCRIPTION='')
        printed = []
        with tempfile.TemporaryDirectory() as d:
            settings = Path(d) / SETTINGS_NAME
            settings.write_text(
                json.dumps(
                    {
                        'data_mount': '/srv/x',
                        'media_mount': '/mnt/y',
                        'external_backup_mount': '/mnt/z',
                        'media': {'services': ['jellyfin']},
                        'modules': {n: {'enabled': True} for n in modules},
                    }
                )
            )
            args = argparse.Namespace(
                config=str(settings),
                state_dir=d,
                profile=None,
                module=None,
                retry_failed=False,
                apply=True,
                check=False,
                dry_run=False,
            )
            setup_progress.enable()
            self.addCleanup(setup_progress.disable)
            with (
                mock.patch.object(installer, 'load_modules', return_value=modules),
                mock.patch('config_checks.validate'),
                mock.patch.object(installer, 'template_steps', return_value=([], False)),
                mock.patch.object(installer, 'tidy_old_releases'),
                mock.patch('native_runtime.payload'),
                mock.patch('builtins.print', side_effect=lambda *a, **k: printed.append(' '.join(map(str, a)))),
            ):
                installer._execute(args)
            states = json.loads((Path(d) / 'state.json').read_text())
            log = (Path(d) / 'install.log').read_text()
        return states, log, printed

    def test_blocked_steps_wait_for_the_first_cause_in_words(self):
        states, log, _ = self.run_plan(
            {
                'storage': {'return_value': None},
                'docker': {'side_effect': RuntimeError('Could not reach download.docker.com.')},
                'tv': {'return_value': {'status': 'needs_setup', 'reason': 'Answer the TV question'}},
            }
        )
        self.assertEqual(states['media'], {'status': 'blocked', 'waiting_for': ['docker'], 'reason': WAITS_FOR_DOCKER})
        self.assertEqual(states['music']['waiting_for'], ['docker'])  # not "media": the cause up its chain
        self.assertIn('BLOCKED music: Waits for Docker to finish first', log)
        self.assertIsNone(PYTHON_LIST.search(log), log)
        self.assertIn('NEEDS SETUP tv: Answer the TV question', log)
        self.assertIn('Finished: 1 of 5 steps done; 4 failed, waiting or unfinished', log)
        self.assertEqual(setup_progress.last('plan')['steps'], ['storage', 'docker', 'media', 'music', 'tv'])


class DriveSizeTests(unittest.TestCase):
    """D3: a 16 GB "SSD" was mounted, written to fstab and saved, and only then refused;
    the sizes were never said; "choose a bigger drive" gave no way to do it. D7: the
    choice showed neither the label nor the model, under 19 lines of loop devices."""

    INVENTORY = MappingProxyType(
        {
            'blockdevices': (
                {'name': 'loop0', 'type': 'loop', 'size': '24G'},
                {'name': 'zram0', 'type': 'disk', 'size': '4G', 'mountpoint': '[SWAP]'},
                {'name': 'vdb', 'type': 'disk', 'size': '9.6M'},
                {
                    'name': 'nvme0n1',
                    'type': 'disk',
                    'size': '476.9G',
                    'model': 'WD SN770',
                    'children': [
                        {
                            'name': 'nvme0n1p2',
                            'type': 'part',
                            'uuid': 'root-0001',
                            'fstype': 'ext4',
                            'mountpoint': '/',
                            'size': '476G',
                        }
                    ],
                },
                {
                    'name': 'sdb',
                    'type': 'disk',
                    'size': '16G',
                    'model': 'Cruzer',
                    'children': [
                        {
                            'name': 'sdb1',
                            'type': 'part',
                            'uuid': 'small-0001',
                            'fstype': 'ext4',
                            'size': '16G',
                            'label': 'SSD',
                        }
                    ],
                },
                {
                    'name': 'sdc',
                    'type': 'disk',
                    'size': '465.8G',
                    'model': 'Samsung SSD 870',
                    'children': [
                        {
                            'name': 'sdc1',
                            'type': 'part',
                            'uuid': 'ssd-0001',
                            'fstype': 'ext4',
                            'size': '465.8G',
                            'label': 'Fast',
                        }
                    ],
                },
                {
                    'name': 'sdd',
                    'type': 'disk',
                    'size': '1.8T',
                    'model': 'WDC WD20',
                    'children': [
                        {'name': 'sdd1', 'type': 'part', 'uuid': 'hdd-0001', 'fstype': 'ext4', 'size': '1.8T'}
                    ],
                },
            )
        }
    )

    def setUp(self):
        # The disk Linux runs from, the same on every machine these tests run on: ext4 and
        # full, so it is in the list and cannot be chosen (tests/test_drive_table.py has
        # the one with room).
        for name, value in (('system_filesystem', ('root-fs-0001', 'ext4')), ('system_free_gb', 0)):
            patch = mock.patch.object(storage, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)
        # The list is shown afresh, and no drive of this computer is looked into.
        import drive_table

        drive_table.forget_shown()
        look = mock.patch.object(drive_table, 'look_inside', return_value=(None, None))
        look.start()
        self.addCleanup(look.stop)

    def resolve(self, answers, config=None, inventory=None):
        config = config or dict(DRIVE_FOLDERS)
        given = iter(answers)
        out = []
        storage.resolve_storage_identity(
            config,
            inventory or self.INVENTORY,
            apply=True,
            interactive=True,
            input_fn=lambda q='': out.append(q) or next(given),
            out_fn=out.append,
        )
        return config, out

    def test_a_replacement_drive_keeps_nothing_of_the_old_one(self):
        # The owner's review of 0.41.5: with only the old "uuid" taken out (as the docs say),
        # the old Btrfs drive's "subvolume" stayed, so an ext4 replacement failed its check
        # and a new Btrfs one looked for a subvolume it may not have.
        config = dict(DRIVE_FOLDERS)
        config['storage'] = {'data_ssd': {'uuid': '', 'fstype': 'btrfs', 'subvolume': '/@data'}}
        config, _ = self.resolve(['4', 'sdc1', '5', 'sdd1'], config)
        self.assertEqual(config['storage']['data_ssd'], {'uuid': 'ssd-0001', 'fstype': 'ext4'})
        self.assertIsNone(storage.subvolume_of(config['storage']['data_ssd']))

    def test_the_question_says_the_sizes_first_and_a_too_small_drive_is_asked_again(self):
        # 3 is the 16 GB drive; every drive keeps its number from the list for both questions.
        config, out = self.resolve(['3', '4', 'sdc1', '5', 'sdd1'])
        self.assertEqual(config['storage']['data_ssd']['uuid'], 'ssd-0001')
        self.assertEqual(config['storage']['media_hdd']['uuid'], 'hdd-0001')
        self.assertEqual(out[0], 'Everything connected to this PC:')  # the list comes first (0.50.0)
        self.assertIn(
            'Now choose the drive for your apps, photos and music (the app SSD). It needs at least 20 GB free, so '
            'a drive of 22 GB or more (100 GB or more is recommended).',
            out,
        )
        too_small = 'it is too small: the app SSD needs a drive of at least 22 GB (20 GB free once formatted)'
        self.assertIn(f'  3) sdb1: 16 GB drive, Cruzer, named "SSD", ext4  - not this one: {too_small}', out)
        self.assertIn('  4) sdc1: 466 GB drive, Samsung SSD 870, named "Fast", ext4  (recommended)', out)
        self.assertIn(
            '  2) nvme0n1: 477 GB NVMe SSD, WD SN770  - not this one: it has too little room: 0 GB free, and the app SSD needs at least 20 GB there',
            out,
        )
        self.assertIn(
            f'Drive 3 (sdb1: 16 GB drive, Cruzer, named "SSD") cannot be the app SSD: {too_small}. '
            'Choose another one from the list.',
            out,
        )
        self.assertIn(
            'Number of the drive for your apps, photos and music (the app SSD) (Enter for 4, the recommended one): ',
            out,
        )
        self.assertIn(
            'Drive 4 (sdc1: 466 GB drive, Samsung SSD 870, named "Fast") is the app SSD. '
            'Nothing on it is changed by choosing it.',
            out,
        )
        self.assertTrue(
            any(line.startswith('Now choose the drive for your movies') and '50 GB' in line for line in out)
        )
        # The drive just chosen keeps its number, and may hold the media and backups too (0.50.0).
        row = next(line for line in out if line.startswith('  4) sdc1') and 'media' not in line.split('(')[0])
        self.assertNotIn('it is already the app SSD', row)
        # The UUID is shown in the list and never has to be typed.
        self.assertIn('       ext4, UUID ssd-0001; not open', out)
        self.assertFalse(any('UUID=' in line or 'mounted=' in line for line in out))

    def test_no_drive_big_enough_stops_before_anything_with_the_sizes(self):
        inventory = {'blockdevices': self.INVENTORY['blockdevices'][:5]}
        with self.assertRaises(RuntimeError) as caught:
            self.resolve([], inventory=inventory)
        self.assertIn('No drive here can be the app SSD (your apps, photos and music).', str(caught.exception))
        self.assertIn(
            'drive 3 (sdb1: 16 GB drive, Cruzer, named "SSD"): it is too small: the app SSD needs a drive of at '
            'least 22 GB (20 GB free once formatted)',
            str(caught.exception),
        )
        self.assertIn('Nothing was changed', str(caught.exception))

    def test_a_drive_of_exactly_the_minimum_is_never_too_small_for_the_minimum(self):
        # Found in the lab: a 20 GB drive was refused as "too small: it needs at least
        # 20 GB" (the formatted drive keeps some of it for itself, so it needs 22 GB).
        smallest, _ = storage.sizes('data_ssd')
        exactly = {'size': f'{smallest}G'}
        self.assertFalse(storage.big_enough(exactly, smallest))
        self.assertGreater(storage.drive_needed(smallest), smallest)
        self.assertTrue(storage.big_enough({'size': f'{storage.drive_needed(smallest)}G'}, smallest))

    def test_no_drive_chosen_says_how_to_go_on(self):
        with self.assertRaisesRegex(
            RuntimeError, r'No drive was chosen for the app SSD\. Run  sudo bash install\.sh  again'
        ):
            self.resolve(['99', '99', '99'])

    def test_the_size_is_checked_before_mounting_writing_fstab_or_saving(self):
        config = {**DRIVE_FOLDERS, 'storage': {}}
        context = mock.Mock(config=config, apply=True, log=lambda *_: None)
        answers = iter(['1', '1', '1'])  # a drive with no filesystem, three times
        with (
            mock.patch.object(storage, 'inventory_now', return_value=self.INVENTORY),
            mock.patch.object(storage.os, 'geteuid', return_value=0),
            mock.patch.object(storage.shutil, 'which', return_value='/usr/bin/x'),
            mock.patch.object(storage.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(
                storage,
                'resolve_storage_identity',
                functools.partial(storage.resolve_storage_identity, input_fn=lambda q='': next(answers)),
            ),
            mock.patch.object(storage, 'save_storage') as saved,
            mock.patch.object(storage, 'mount_all') as mounted,
            mock.patch.object(storage, 'install_fstab') as fstab,
            self.assertRaisesRegex(RuntimeError, 'No drive was chosen'),
        ):
            storage.run(context)
        saved.assert_not_called()
        mounted.assert_not_called()
        fstab.assert_not_called()
        self.assertNotIn('uuid', config['storage']['data_ssd'])

    # A folder of this test's own: a server set up on this PC's /srv/nekoserver (the
    # drive's "docker-engine" folder) would count as the saved drive's, and keep it.
    EMPTY = {**DRIVE_FOLDERS, 'data_mount': '/nonexistent/nekoserver-test'}

    def test_a_saved_drive_too_small_is_asked_for_again(self):
        config = {**self.EMPTY, 'storage': {'data_ssd': {'uuid': 'small-0001', 'fstype': 'ext4'}}}
        config, out = self.resolve(['4', 'sdc1', '5', 'sdd1'], config)
        self.assertEqual(config['storage']['data_ssd']['uuid'], 'ssd-0001')
        self.assertIn(
            'Drive 3 (sdb1: 16 GB drive, Cruzer, named "SSD"), the drive saved as the app SSD, is too small: it '
            'needs a drive of at least 22 GB (20 GB free once formatted). Choose the SSD (your apps, photos and music) again:',
            out,
        )

    def test_a_saved_drive_too_small_already_mounted_says_how_to_choose_again(self):
        inventory = json.loads(json.dumps(dict(self.INVENTORY)))
        inventory['blockdevices'][4]['children'][0]['mountpoint'] = self.EMPTY['data_mount']
        config = {**self.EMPTY, 'storage': {'data_ssd': {'uuid': 'small-0001', 'fstype': 'ext4'}}}
        with self.assertRaises(RuntimeError) as caught:
            self.resolve([], config, inventory)
        message = str(caught.exception)
        for step in (
            'sudo umount /nonexistent/nekoserver-test',
            'UUID=small-0001 out of /etc/fstab',
            'it asks for the drive anew',
        ):
            self.assertIn(step, message)
        # Gate run C10: never "edit my-config.json"; unmounted, the next run asks for the drive.
        self.assertNotIn('my-config.json', message)

    def test_the_drive_lines_leave_out_loop_devices_swap_and_tiny_ones(self):
        line = storage.disk_line(self.INVENTORY)
        self.assertEqual(
            line,
            'Drives on this PC: nvme0n1 477 GB WD SN770 (Linux runs from it); sdb 16 GB Cruzer; '
            'sdc 466 GB Samsung SSD 870; sdd 1.8 TB WDC WD20',
        )
        self.assertEqual(storage.size_words('9.6M'), '10 MB')
        self.assertIsNone(storage.size_bytes(None))


class UnlistedDeviceTests(unittest.TestCase):
    """IT-01, D11: a loop device was "not connected, or not formatted"; a drive on one
    was "Cannot verify the physical parent disk; inspect full lsblk topology first"."""

    def test_a_saved_loop_device_is_named_with_why(self):
        inventory = {
            'blockdevices': [
                {
                    'name': 'sda',
                    'type': 'disk',
                    'children': [{'name': 'sda1', 'type': 'part', 'uuid': 'root', 'fstype': 'ext4', 'mountpoint': '/'}],
                },
                {'name': 'loop0', 'type': 'loop', 'uuid': '93bb-0001', 'fstype': 'ext4', 'size': '24G'},
            ]
        }
        config = {**DRIVE_FOLDERS, 'storage': {'data_ssd': {'uuid': '93bb-0001'}}}
        with self.assertRaises(RuntimeError) as caught:
            storage.resolve_storage_identity(config, inventory, apply=True)
        message = str(caught.exception)
        self.assertIn(
            'is connected, but /dev/loop0 is a loop device (a file attached as a disk), which NekoServer '
            'does not use for its drives',
            message,
        )
        self.assertNotIn('not connected', message)

    def test_a_drive_on_no_known_disk_is_said_plainly(self):
        items = [{'uuid': 'u-1', 'name': 'loop5p1', 'physical_disks': []}]
        with self.assertRaisesRegex(RuntimeError, r'cannot tell which physical drive /dev/loop5p1 is on .*loop device'):
            storage.own_disks('u-1', items, set(), True)


class DesktopRowTests(unittest.TestCase):
    """IT-05: "Desktop: yes" on a server Ubuntu, which starts in graphical mode too."""

    def test_graphical_mode_without_a_desktop_is_no_desktop(self):
        self.assertEqual(capabilities.desktop_found('graphical.target', lambda name: None), 'no (no desktop installed)')
        self.assertEqual(
            capabilities.desktop_found(
                'graphical.target', lambda name: '/usr/bin/x' if name == 'gnome-shell' else None
            ),
            'yes',
        )
        self.assertEqual(capabilities.desktop_found('multi-user.target', lambda name: '/usr/bin/x'), 'no (server)')


class RootWarningTests(unittest.TestCase):
    """QA-09: "stop now (Ctrl+C)" scrolled away at once."""

    def test_a_terminal_gets_ten_seconds_to_stop_and_a_script_none(self):
        script = (ROOT / 'install.sh').read_text()
        warning = script.index('stop now (Ctrl+C)')
        pause = script.index('sleep 10', warning)
        self.assertLess(pause, script.index('exec 9>>/run/nekoserver-setup.lock'))
        self.assertIn('if [[ -t 0 ]]; then', script[warning:pause])
        piece = script[script.index('  if [[ -z "${SUDO_USER:-}"') : script.index('  # One setup at a time')]
        piece = piece.replace('sleep 10', 'echo SLEPT')
        for stdin, slept in ((subprocess.DEVNULL, False), ('tty', True)):
            with self.subTest(terminal=slept):
                env = {**os.environ, 'ROOT': '/nonexistent'}
                env.pop('SUDO_USER', None)
                if stdin == 'tty':
                    master, slave = os.openpty()
                    self.addCleanup(os.close, master)
                    result = subprocess.run(
                        ['bash', '-c', piece],
                        stdin=slave,
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=30,
                        check=False,
                    )
                    os.close(slave)
                else:
                    result = subprocess.run(
                        ['bash', '-c', piece],
                        stdin=stdin,
                        capture_output=True,
                        text=True,
                        env=env,
                        timeout=30,
                        check=False,
                    )
                self.assertIn('stop now (Ctrl+C)', result.stdout)
                self.assertEqual('SLEPT' in result.stdout, slept)


class DocsTests(unittest.TestCase):
    """D3, D6, D17, D18, IT-11, D2: what the READMEs promise is what setup does."""

    def test_both_readmes_say_one_drive_is_enough(self):
        # 0.50.0 (the user: any number of drives works, one drive included).
        for path in (ROOT.parent / README, ROOT / README):
            if not path.exists():
                continue  # (a copy of the nekoserver folder alone)
            with self.subTest(readme=path.name):
                text = path.read_text()
                self.assertIn('one drive is enough', text.lower().replace('*', ''))
                self.assertNotIn('three drives besides the disk linux is on', text.lower().replace('*', ''))
                self.assertIn('16 GB', text)
                self.assertIn('sudo apt install git', text)
        guide = (ROOT / README).read_text()
        for size in ('20 GB', '100 GB', '50 GB', '500 GB'):
            self.assertIn(size, guide)

    def test_the_guide_promises_what_happens_when_it_happens(self):
        text = (ROOT / README).read_text()
        self.assertNotIn('A few seconds after you start', text)
        self.assertNotIn('Before changing anything, setup also lists', text)
        self.assertNotIn('Four drives', text)
        self.assertIn('Once you start, it first lists what it found on this computer', text)
        self.assertIn('opens again', text)
        install = text[text.index('## Install') : text.index('Then the setup wizard shows')]
        # The useful part first; the details of each Linux folded away after it.
        self.assertLess(install.index('That\'s it'), install.index('<details>'))
        self.assertLess(install.index('<details>'), install.index('pacman -Syu'))
        self.assertLess(install.index('<details>'), install.index('dnf makecache'))
        self.assertIn('> **First, in your router, reserve that address', text)
        # The questions in the order setup asks them: the PIN, then the phone, the home
        # network, and the backup password last.
        steps = text[text.index('Then the setup wizard shows') : text.index('### Two ways to follow')]
        order = [
            steps.index(words)
            for words in (
                'where to store your apps and files',
                'dashboard PIN',
                'set up your phone',
                'Uses your home network',
                'backup password',
                'first checkup',
            )
        ]
        self.assertEqual(order, sorted(order))


if __name__ == '__main__':
    unittest.main()
