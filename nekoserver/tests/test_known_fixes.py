"""0.50.0, item 7: a step that did not finish shows the fix setup knows for its reason
(known_fixes.py) and can be tried again: at once, while it is the step under way, and
from the list at the end of the run. The dashboard gets the fix and the command; it
never runs setup."""

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import guided_setup  # noqa: E402
import installer  # noqa: E402
import known_fixes  # noqa: E402
import setup_progress  # noqa: E402
import wizard  # noqa: E402

# Each kind of failure, as the failing program or NekoServer itself words it.
REASONS = {
    'no_internet': (
        "Could not reach download.docker.com (Docker's site) to download Docker's packages, after 3 tries "
        '(<urlopen error [Errno -3] Temporary failure in name resolution>). Likely causes: something on your network',
        'Installing caddy failed: curl: (6) Could not resolve host: dl.cloudsmith.io',
        'git clone failed: fatal: unable to access: Could not resolve host: github.com',
        "pacman -S failed: error: failed retrieving file 'core.db' from mirror : Network is unreachable",
        'docker pull failed: dial tcp 3.94.224.37:443: connect: no route to host',
        "ssh: Name or service not known",
    ),
    'package_lock': (
        'Installing samba failed: E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 2211 '
        '(unattended-upgr)',
        'E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?',
        'error: failed to init transaction (unable to lock database)',
        'System management is locked by the application with pid 1820 (zypper).',
        'pacman is still locked (/var/lib/pacman/db.lck) after 10 minutes: another program is installing packages',
        'Another app is currently holding the yum lock; waiting for it to exit...',
        'Waiting for process with pid 4410 to finish.',
    ),
    'docker_down': (
        'docker info failed: Cannot connect to the Docker daemon at unix:///var/run/docker.sock. '
        'Is the docker daemon running?',
        'Docker is not running',
        'error during connect: Get "http://%2Fvar%2Frun%2Fdocker.sock/v1.47/info": EOF',
    ),
    'disk_full': (
        'docker compose up failed: write /var/lib/docker/tmp/GetImageBlob123: no space left on device',
        "[Errno 28] No space left on device: '/srv/nekoserver/appdata/immich'",
        'rsync: write failed on "/mnt/nekoserver-media/backups/x": Disk quota exceeded (122)',
    ),
    'drive_missing': (
        'The drive chosen as the HDD (movies, shows, Nextcloud files and backups) (UUID 5fa5-0000) is not connected, '
        'or is not formatted ext4, xfs or btrfs. Connect it and run setup again.',
        'Drive UUID 1a2b-3c4d not found; connect the disk and use lsblk -f',
        'Drive check failed: /mnt/nekoserver-media should be the HDD (UUID 5fa5-0000, ext4); nothing is mounted there',
        'External drive missing',
    ),
    'download_timeout': (
        'docker compose pull did not finish within 1800 seconds',
        'Installing samba did not finish within 15 minutes',
        'Get "https://registry-1.docker.io/v2/": net/http: TLS handshake timeout',
        'Error response from daemon: context deadline exceeded',
        'curl: (28) Operation timed out after 300001 milliseconds with 0 out of 0 bytes received',
        'error pulling image configuration: read tcp 10.0.2.15:41234->104.16.1.1:443: read: connection reset by peer',
    ),
    'tailscale_certificate': (
        'Tailscale has not got the HTTPS certificate for neko.tail1234.ts.net yet, so the app addresses on the phone '
        "do not open. Let's Encrypt gives a machine name at most 5 certificates a week; it allows the next one after "
        '2026-11-12 08:15:00 UTC. Run  sudo bash install.sh  again later; it checks again.',
    ),
}


class FixTableTests(unittest.TestCase):
    def test_every_entry_is_found_by_the_words_of_its_failure(self):
        self.assertEqual({fix['id'] for fix in known_fixes.FIXES}, set(REASONS))
        for wanted, reasons in REASONS.items():
            for reason in reasons:
                with self.subTest(fix=wanted, reason=reason[:60]):
                    found = known_fixes.fix_for(reason)
                    self.assertIsNotNone(found, reason)
                    self.assertEqual(found['id'], wanted)

    def test_each_fix_is_steps_a_person_can_do_ending_in_retry(self):
        for fix in known_fixes.FIXES:
            with self.subTest(fix=fix['id']):
                self.assertGreaterEqual(len(fix['steps']), 2)
                self.assertIn('Retry this step', fix['steps'][-1])
                self.assertFalse(fix['title'].endswith('.'))
                said = ' '.join(fix['steps'])
                # A fix never has the person force something that could lose data.
                for never in ('rm -rf', 'mkfs', '--force', 'kill -9', 'dd if='):
                    self.assertNotIn(never, said)
                for pattern in fix['matches']:
                    self.assertTrue(isinstance(pattern, str) and len(pattern) >= 6, pattern)

    def test_a_reason_setup_has_no_fix_for_gets_none(self):
        for reason in (
            '',
            None,
            'Jellyfin was not ready after 5 minutes (Container not healthy)',
            'Stopped at a question (end of input); run setup again in a terminal',
            'Filesystem type mismatch for media_hdd',
            'Waits for Docker to finish first',
        ):
            self.assertIsNone(known_fixes.fix_for(reason), reason)

    def test_a_site_not_reached_because_it_timed_out_is_a_site_not_reached(self):
        reason = "Could not reach download.docker.com (Docker's site) to download Docker's packages (timed out)."
        self.assertEqual(known_fixes.fix_for(reason)['id'], 'no_internet')

    def test_the_terminal_shows_the_fix_as_numbered_steps(self):
        shown = known_fixes.lines(known_fixes.fix_for('Docker is not running'))
        self.assertEqual(shown[0], '  What usually fixes it: Docker, which runs the apps, is not running.')
        self.assertTrue(
            shown[1].startswith('    1. In a second terminal (Ctrl+Alt+T), start it:  sudo systemctl start docker')
        )
        self.assertEqual(len(shown), 4)


class RetryQuestionTests(unittest.TestCase):
    def setUp(self):
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)

    def row(self, answer='yes', reason='Docker is not running'):
        return {'answer': answer, 'title': 'Docker', 'reason': reason, 'fix': known_fixes.fix_for(reason)}

    def asked(self, rows, typed, leave=''):
        answers, said, pages = iter(typed), [], []

        def ask(prompt):
            said.append(prompt)
            pages.append(setup_progress.page_shown())
            return next(answers)

        return guided_setup.retry_question(rows, leave, ask, said.append), said, pages

    def test_the_step_that_just_failed_says_why_the_fix_and_asks(self):
        answer, said, pages = self.asked([self.row()], ['maybe', 'YES'], leave='no')
        self.assertEqual(answer, 'yes')
        self.assertEqual(said[0], 'Docker did not finish: Docker is not running')
        self.assertEqual(said[1], '  What usually fixes it: Docker, which runs the apps, is not running.')
        self.assertIn('Retry this step now? [yes/no]: ', said)
        self.assertIn('Type yes or no.', said)
        # In the window it is one page: the step, its reason, its fix, and what leaving answers.
        page = pages[0]
        self.assertEqual((page['form'], page['asking'], page['leave']), ('retry', 'answer', 'no'))
        self.assertEqual(page['steps'][0]['fix']['id'], 'docker_down')
        for typed in (['no'], [''], ['n']):
            self.assertEqual(self.asked([self.row()], typed, leave='no')[0], 'no')

    def test_the_list_at_the_end_is_chosen_from_by_number(self):
        rows = [self.row('1'), self.row('2', 'something NekoServer has no fix for')]
        answer, said, _ = self.asked(rows, ['7', '2'])
        self.assertEqual(answer, '2')
        self.assertEqual(said[0], '  1) Docker did not finish: Docker is not running')
        self.assertIn('  2) Docker did not finish: something NekoServer has no fix for', said)
        self.assertIn('Retry one now? Type its number (Enter alone: finish): ', said)
        self.assertIn('Type one of the numbers above, or press Enter alone to finish.', said)
        self.assertEqual(self.asked(rows, [''])[0], '')  # Enter alone: finish

    def test_ctrl_d_or_three_wrong_answers_leave_it(self):
        def ended(prompt):
            raise EOFError

        self.assertEqual(guided_setup.retry_question([self.row()], 'no', ended, lambda line: None), 'no')
        self.assertEqual(self.asked([self.row()], ['x', 'y?', 'z'], leave='no')[0], 'no')


class Step:
    """A setup step that fails a given number of times, then finishes."""

    def __init__(self, fails=0, reason='Docker is not running', needs=()):
        self.fails, self.reason, self.runs = fails, reason, 0
        self.DEPENDENCIES = list(needs)

    def run(self, ctx):
        self.runs += 1
        if self.runs <= self.fails:
            raise RuntimeError(self.reason)


class RetryNowTests(unittest.TestCase):
    """Right after a failure, in the step loop."""

    def test_asked_only_with_a_person_there_and_at_most_three_times(self):
        said = []
        yes = mock.Mock(return_value='yes')
        self.assertFalse(installer.retry_now('docker', 'x', 0, said.append, interactive=False, asked=yes))
        yes.assert_not_called()
        self.assertTrue(installer.retry_now('docker', 'Docker is not running', 0, said.append, True, yes))
        row = yes.call_args.args[0][0]
        self.assertEqual((row['answer'], row['title'], row['fix']['id']), ('yes', 'Docker', 'docker_down'))
        self.assertEqual(yes.call_args.kwargs, {'leave': 'no'})
        self.assertFalse(installer.retry_now('docker', 'x', 0, said.append, True, mock.Mock(return_value='no')))
        # The fourth time it is not asked any more, and setup says to come back later.
        yes.reset_mock()
        self.assertFalse(installer.retry_now('docker', 'x', installer.MOST_RETRIES, said.append, True, yes))
        yes.assert_not_called()
        self.assertIn('tried again 3 times and it still does not finish', said[-1])
        self.assertIn('run  sudo bash install.sh  again later', said[-1])

    def run_plan(self, steps, answers, tmp):
        """A run of the installer over these steps, the retry question answered in turn."""
        modules = {name: {'object': step, 'deps': step.DEPENDENCIES} for name, step in steps.items()}
        config = Path(tmp) / 'config.json'
        config.write_text(
            json.dumps({'data_mount': tmp + '/ssd', 'media_mount': tmp + '/hdd', 'profiles': {'full': list(steps)}})
        )
        args = argparse.Namespace(
            all=False, profile='full', module=None, retry_failed=False, apply=True, check=False, dry_run=False,
            config=str(config), state_dir=tmp + '/state',
        )  # fmt: skip
        asked = iter(answers)
        with (
            mock.patch.object(installer, 'load_modules', return_value=modules),
            mock.patch.object(installer, 'validate_config'),
            mock.patch.object(installer, 'template_steps', return_value=([], False)),
            mock.patch('config_checks.validate'),
            mock.patch.object(installer, 'review_retention'),
            mock.patch.object(installer, 'save_profiles'),
            mock.patch.object(installer, 'tidy_old_releases'),
            mock.patch.object(installer, 'settle_upgrade'),
            mock.patch.object(installer.sys.stdin, 'isatty', return_value=True),
            mock.patch.object(guided_setup, 'retry_question', side_effect=lambda rows, leave='': next(asked)),
            mock.patch('builtins.print'),
        ):
            code = installer._execute(args)
        return code, json.loads((Path(tmp) / 'state/state.json').read_text())

    def test_a_retry_that_works_lets_the_run_go_on_as_if_nothing_failed(self):
        steps = {'recovery': Step(fails=1), 'media': Step(needs=['recovery'])}
        with tempfile.TemporaryDirectory() as tmp:
            code, states = self.run_plan(steps, ['yes'], tmp)
        self.assertEqual(code, 0)
        self.assertEqual((steps['recovery'].runs, steps['media'].runs), (2, 1))
        self.assertEqual(
            {name: record['status'] for name, record in states.items()}, {'recovery': 'success', 'media': 'success'}
        )

    def test_a_retry_that_fails_again_is_asked_again_up_to_three_times(self):
        steps = {'recovery': Step(fails=9), 'media': Step(needs=['recovery'])}
        with tempfile.TemporaryDirectory() as tmp:
            code, states = self.run_plan(steps, ['yes', 'yes', 'yes'], tmp)
        self.assertEqual(code, installer.STEP_FAILED)
        self.assertEqual(steps['recovery'].runs, 4)  # the first run and three retries
        self.assertEqual(steps['media'].runs, 0)
        self.assertEqual(states['recovery']['status'], 'failed')
        self.assertEqual(states['media']['status'], 'blocked')

    def test_no_leaves_the_step_failed_and_the_rest_goes_on(self):
        steps = {'recovery': Step(fails=1), 'samba': Step()}
        with tempfile.TemporaryDirectory() as tmp:
            code, states = self.run_plan(steps, ['no'], tmp)
        self.assertEqual(code, installer.STEP_FAILED)
        self.assertEqual((steps['recovery'].runs, steps['samba'].runs), (1, 1))
        self.assertEqual(states['samba']['status'], 'success')


class RetryAtTheEndTests(unittest.TestCase):
    """The list of unfinished steps after the run."""

    def test_the_step_and_what_waited_for_it_run_and_nothing_that_is_done(self):
        modules = dict.fromkeys(('system', 'docker', 'media', 'photos', 'dashboard', 'tailscale'))
        states = {
            'system': {'status': 'success'},
            'docker': {'status': 'failed', 'reason': 'Docker is not running'},
            'media': {'status': 'blocked', 'waiting_for': ['docker']},
            'photos': {'status': 'blocked', 'waiting_for': ['docker']},
            'dashboard': {'status': 'blocked', 'waiting_for': ['tailscale']},
            'tailscale': {'status': 'needs_setup', 'reason': 'sign in'},
        }
        self.assertEqual(installer.retry_selection('docker', states, modules), ['docker', 'media', 'photos'])
        self.assertEqual(installer.retry_selection('tailscale', states, modules), ['tailscale', 'dashboard'])
        with self.assertRaisesRegex(installer.InstallerError, 'Unknown step'):
            installer.retry_selection('nothing', states, modules)

    def test_a_retry_run_does_only_those_steps(self):
        steps = {'system': Step(), 'recovery': Step(fails=1), 'media': Step(needs=['recovery']), 'samba': Step()}
        with tempfile.TemporaryDirectory() as tmp:
            runner = RetryNowTests()
            code, states = runner.run_plan(steps, ['no'], tmp)
            self.assertEqual((code, states['media']['status']), (installer.STEP_FAILED, 'blocked'))
            before = {name: step.runs for name, step in steps.items()}
            # "Retry this step" at the end: that step, then the one that waited for it.
            again = argparse.Namespace(
                all=False, profile=None, module=None, retry_failed=False, retry_step='recovery', apply=True, check=False,
                dry_run=False, config=tmp + '/config.json', state_dir=tmp + '/state',
            )  # fmt: skip
            modules = {name: {'object': step, 'deps': step.DEPENDENCIES} for name, step in steps.items()}
            with (
                mock.patch.object(installer, 'load_modules', return_value=modules),
                mock.patch.object(installer, 'validate_config'),
                mock.patch.object(installer, 'template_steps', return_value=([], False)),
                mock.patch('config_checks.validate'),
                mock.patch.object(installer, 'review_retention'),
                mock.patch.object(installer, 'save_profiles'),
                mock.patch.object(installer, 'tidy_old_releases'),
                mock.patch.object(installer, 'settle_upgrade'),
                mock.patch.object(installer.sys.stdin, 'isatty', return_value=False),
                mock.patch('builtins.print'),
            ):
                self.assertEqual(installer._execute(again), 0)
            states = json.loads((Path(tmp) / 'state/state.json').read_text())
        ran = {name: step.runs - before[name] for name, step in steps.items()}
        self.assertEqual(ran, {'system': 0, 'recovery': 1, 'media': 1, 'samba': 0})
        self.assertTrue(all(record['status'] == 'success' for record in states.values()), states)

    def end(self, runs, answers, most=3):
        """wizard.retry_unfinished with the state files `runs` gives in turn (the first is
        the run's own); returns (exit code, the steps retried, what was said)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)
        config = Path(tmp.name) / 'my-config.json'
        config.write_text(json.dumps({'data_mount': tmp.name + '/ssd'}))
        state = Path(tmp.name) / 'state/state.json'
        state.parent.mkdir()
        left = list(runs)
        state.write_text(json.dumps(left.pop(0)))
        setup_progress.event('plan', steps=['system', 'docker', 'media', 'tailscale'])
        retried, said, asked = [], [], iter(answers)

        def execute(args):
            retried.append(args.retry_step)
            self.assertIsNone(args.profile)
            state.write_text(json.dumps(left.pop(0)))
            return 5

        args = argparse.Namespace(profile='full', config=str(config), apply=True)
        with mock.patch.object(wizard, 'state_file', return_value=state):
            code = wizard.retry_unfinished(
                execute,
                args,
                5,
                Path(tmp.name) / 'state',
                lambda rows: (said.append(rows), next(asked))[1],
                said.append,
                most,
            )
        return code, retried, said

    FAILED = {
        'system': {'status': 'success'},
        'docker': {'status': 'failed', 'reason': 'Docker is not running'},
        'media': {'status': 'blocked', 'waiting_for': ['docker'], 'reason': 'Waits for Docker'},
        'tailscale': {'status': 'needs_setup', 'reason': 'Sign in to Tailscale'},
    }
    DONE = {name: {'status': 'success'} for name in FAILED}

    def test_every_unfinished_step_is_listed_with_its_fix_and_one_is_retried(self):
        fixed = {**self.FAILED, 'docker': {'status': 'success'}, 'media': {'status': 'success'}}
        code, retried, said = self.end([self.FAILED, fixed, self.DONE], ['1', '1'])
        self.assertEqual(retried, ['docker', 'tailscale'])
        self.assertEqual(code, 0)  # everything is done now: the first checkup may run
        rows = said[1]
        # The steps a person can act on: the failed one and the one that waits for them;
        # the one that only waits for Docker runs with it.
        self.assertEqual([row['title'] for row in rows], ['Docker', 'Tailscale'])
        self.assertEqual([row['answer'] for row in rows], ['1', '2'])
        self.assertEqual(rows[0]['fix']['id'], 'docker_down')
        self.assertIsNone(rows[1]['fix'])
        self.assertEqual(said[0], '\nThese steps did not finish. Each can be tried again now:')

    def test_enter_alone_finishes_and_the_code_stays(self):
        code, retried, _ = self.end([self.FAILED], [''])
        self.assertEqual((code, retried), (5, []))

    def test_a_step_is_retried_three_times_at_most_then_left_for_later(self):
        code, retried, said = self.end([self.FAILED] * 4, ['1', '1', '1', ''])
        self.assertEqual(retried, ['docker', 'docker', 'docker'])
        self.assertEqual(code, 5)
        self.assertIn(
            'Docker was tried 3 times and still does not finish', '\n'.join(s for s in said if isinstance(s, str))
        )
        # Docker is no longer offered; Tailscale still is.
        self.assertEqual([row['title'] for row in said[-1]], ['Tailscale'])

    def test_a_run_that_finished_asks_nothing(self):
        code, retried, said = self.end([self.DONE], [])
        self.assertEqual((code, retried, said), (0, [], []))  # every step is done: nothing to ask, and the code says so
        self.assertEqual(wizard.retry_unfinished(None, None, 0, None), 0)


class DashboardNoteTests(unittest.TestCase):
    def test_the_failed_steps_are_written_for_the_dashboard_with_their_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            states = {
                'docker': {'status': 'failed', 'reason': 'Docker is not running'},
                'tailscale': {'status': 'needs_setup', 'reason': 'Sign in'},  # yours to do: not a failure
                'media': {'status': 'blocked', 'waiting_for': ['docker']},
                'system': {'status': 'success'},
            }
            with mock.patch.object(installer.os.path, 'ismount', return_value=True):
                installer.note_unfinished({'data_mount': tmp}, states, lambda line: None)
                written = json.loads((Path(tmp) / 'reports/setup-unfinished.json').read_text())
                self.assertEqual(written['format'], 'neko-setup-unfinished-v1')
                self.assertEqual([step['id'] for step in written['steps']], ['docker'])
                self.assertEqual(written['steps'][0]['title'], 'Docker')
                self.assertEqual(written['steps'][0]['fix']['id'], 'docker_down')
                # A later run that finishes it takes it off the dashboard.
                installer.note_unfinished({'data_mount': tmp}, {'docker': {'status': 'success'}}, lambda line: None)
                self.assertEqual(json.loads((Path(tmp) / 'reports/setup-unfinished.json').read_text())['steps'], [])
            # The SSD not mounted: nothing is written under its folder on the system disk.
            other = Path(tmp) / 'not-mounted'
            other.mkdir()
            with mock.patch.object(installer.os.path, 'ismount', return_value=False):
                installer.note_unfinished({'data_mount': str(other)}, states, lambda line: None)
            self.assertEqual(list(other.iterdir()), [])

    def test_the_dashboard_never_runs_setup(self):
        collect = (ROOT / 'runtime/src/collect.rs').read_text()
        self.assertIn('Setup is never started from this page', collect)
        control = (ROOT / 'runtime/src/serve.rs').read_text() + (ROOT / 'dashboard/app.js').read_text()
        self.assertNotIn('install.sh --module', control)
        self.assertNotIn('retry_step', control)


if __name__ == '__main__':
    unittest.main()
