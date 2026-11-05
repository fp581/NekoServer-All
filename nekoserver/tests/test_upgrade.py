"""upgrade.py: NekoServer upgraded from the dashboard. It downloads the release, checks it
is that release, runs its setup without questions and without a system update, checks
the result, and puts the previous release back when anything fails."""

import io
import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import upgrade  # noqa: E402
from neko_common import VERSION  # noqa: E402

NEWER = 'v{}.{}.{}Beta'.format(*[int(n) + (1 if i == 1 else 0) for i, n in enumerate(VERSION.split('.'))])


def release(tag, version=None, extra=None):
    """A release archive as GitHub serves it: one top folder with nekoserver/ inside."""
    buffer = io.BytesIO()
    top = 'NekoServer-All-' + tag.lstrip('v')
    version = version or '.'.join(map(str, upgrade.numbers(tag)))
    files = {
        f'{top}/nekoserver/install.sh': b'#!/bin/bash\n',
        f'{top}/nekoserver/neko_common.py': f"VERSION = '{version}'\n".encode(),
        **(extra or {}),
    }
    with tarfile.open(fileobj=buffer, mode='w:gz') as bundle:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.data = self.dir / 'ssd'
        self.previous = self.data / 'installer/releases' / ('v' + VERSION)
        self.previous.mkdir(parents=True)
        (self.previous / 'install.sh').write_text('#!/bin/bash\n')
        (self.data / 'installer/state').mkdir(parents=True)
        self.state({'storage': 'success', 'photos': 'success', 'tailscale': 'needs_setup'})
        self.settings = self.dir / 'my-config.json'
        self.settings.write_text(json.dumps({'data_mount': str(self.data)}))
        self.journal = self.dir / 'etc/upgrade.json'
        self.said = []
        self.ran = []
        # The check after putting the previous release back (verify): what it answers,
        # and what it was asked.
        self.back_problem, self.verified = None, []
        self.units = self.dir / 'systemd'
        self.units.mkdir()
        patched = mock.patch.object(upgrade, 'UNITS', self.units)
        patched.start()
        self.addCleanup(patched.stop)
        waits = mock.patch.object(upgrade, 'restart_pending', return_value=None)
        waits.start()
        self.addCleanup(waits.stop)
        for target, value in (
            ('verify', lambda config, tag, before: self.verified.append((tag, sorted(before))) or self.back_problem),
            ('settings_path', lambda p: Path(p)),
            ('owner', lambda config: 'neko'),
            ('MAINTENANCE_JOURNAL', str(self.dir / 'none.json')),
            ('MIGRATE_JOURNAL', self.dir / 'no-move.json'),
            ('OPERATION_LOCK', self.dir / 'operation.lock'),
        ):
            patcher = mock.patch.object(upgrade, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def state(self, steps):
        (self.data / 'installer/state/state.json').write_text(
            json.dumps({name: {'status': status} for name, status in steps.items()})
        )

    def setup(self, *codes):
        """Stand-in setup runs, answering with `codes` in turn (the new release, then the old)."""
        answers = iter(codes)

        def run(project, settings, account, log_file):
            self.ran.append((Path(project).name, account, self.journal_phase()))
            return next(answers)

        return run

    def journal_phase(self):
        return json.loads(self.journal.read_text())['phase'] if self.journal.exists() else None

    def upgrade(self, setup, check=lambda: None, payload=None, tag=NEWER):
        return upgrade.upgrade(
            tag,
            self.settings,
            out=self.said.append,
            get=lambda url, what, limit, timeout: payload or release(tag),
            setup=setup,
            check=check,
            journal=self.journal,
            lock=self.dir / 'setup.lock',
        )

    def test_a_beta_tag_is_read_either_way_it_is_written(self):
        """0.46.1 was published as v0.46.1-beta, which the upgrade refused as no release."""
        self.assertEqual(upgrade.numbers('v0.46.1-beta'), (0, 46, 1))
        self.assertEqual(upgrade.numbers('v0.46.1Beta'), (0, 46, 1))
        self.assertIsNone(upgrade.numbers('v0.46.1-bata'))

    def test_a_kept_copy_of_a_unit_is_not_a_service_behind(self):
        """The audit of 0.46.2: the gateway's `.before-<time>` copy named an old release, so
        every upgrade failed its check, and so did putting the release back."""
        (self.units / 'nekoserver-gateway-lan.service').write_text(f'ExecStart=/usr/local/lib/x/v{VERSION}/y\n')
        (self.units / 'nekoserver-gateway-lan.service.before-20261104').write_text(
            'ExecStart=/usr/local/lib/x/v0.45.0/y\n'
        )
        self.assertEqual(upgrade.services_behind(VERSION, self.units), [])

    def test_going_back_removes_the_units_the_newer_release_added(self):
        """The audit of 0.46.2: the older setup rewrites only its own units, so a new one
        kept running the newer release's program after it was put back."""
        (self.units / 'nekoserver-dashboard.service').write_text('old\n')
        added = self.units / 'nekoserver-foo.timer'

        def setup_adding(project, settings, account, log_file):
            if 'nekoserver' in str(project):
                added.write_text('ExecStart=/usr/local/lib/x/v9.9.9/y\n')
            return 2

        ran = []

        def systemctl(argv, **k):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, 3 if 'is-active' in argv else 0, '', '')

        with mock.patch.object(upgrade.subprocess, 'run', side_effect=systemctl):
            self.upgrade(setup_adding)
        self.assertFalse(added.exists())
        self.assertTrue((self.units / 'nekoserver-dashboard.service').exists())
        self.assertIn(['systemctl', 'disable', '--now', 'nekoserver-foo.timer'], ran)

    def test_a_newer_service_that_does_not_stop_keeps_the_way_back_from_passing(self):
        """The review of 0.46.3: a failed stop was ignored and the unit's file removed, so
        the check after it missed the newer service that still ran, and the way back said
        it had worked."""
        (self.units / 'nekoserver-dashboard.service').write_text('old\n')
        added = self.units / 'nekoserver-foo.service'

        def setup_adding(project, settings, account, log_file):
            if 'nekoserver' in str(project):
                added.write_text('ExecStart=/usr/local/lib/x/v9.9.9/y\n')
                return 2
            return 0  # the older release's setup works

        def systemctl(argv, **k):
            return subprocess.CompletedProcess(argv, 1 if 'disable' in argv else 0, '', 'Failed to stop')

        with mock.patch.object(upgrade.subprocess, 'run', side_effect=systemctl):
            code = self.upgrade(setup_adding)
        self.assertEqual(code, 2)
        self.assertTrue(added.exists())
        self.assertTrue(
            any('nekoserver-foo.service' in line and 'did not stop' in line for line in self.said), self.said
        )

    def test_a_template_unit_the_newer_release_added_is_switched_off_with_its_instances(self):
        """The review of 0.47.0: a template (x@.service) can't be stopped by its own name, so
        "disable --now" always failed, the way back never passed, and its instances ran on."""
        (self.units / 'nekoserver-dashboard.service').write_text('old\n')
        added = self.units / 'nekoserver-foo@.service'

        def setup_adding(project, settings, account, log_file):
            if 'nekoserver' in str(project):
                added.write_text('ExecStart=/usr/local/lib/x/v9.9.9/y %i\n')
                return 2
            return 0

        ran = []

        def systemctl(argv, **k):
            ran.append(argv)
            # systemd: a template's own name can't be stopped ("missing the instance name").
            refused = '--now' in argv and argv[-1].endswith('@.service')
            return subprocess.CompletedProcess(argv, 1 if refused else 0, '', '')

        with mock.patch.object(upgrade.subprocess, 'run', side_effect=systemctl):
            code = self.upgrade(setup_adding)
        self.assertEqual(code, 1, self.said)
        self.assertFalse(added.exists())
        self.assertIn(['systemctl', 'stop', 'nekoserver-foo@*.service'], ran)

    def test_an_upgrade_waits_for_a_pending_restart(self):
        """The audit of 0.46.2: setup stops at once when a restart waits (exit 3), the old
        release's too, so the upgrade failed and then failed to go back."""
        with mock.patch.object(upgrade, 'restart_pending', return_value='This computer waits for a restart'):
            self.assertEqual(self.upgrade(self.setup(0)), 2)
        self.assertEqual(self.ran, [])
        self.assertFalse(self.journal.exists())
        self.assertIn('waits for a restart', self.said[-1])

    def test_a_good_release_is_set_up_and_checked(self):
        self.assertEqual(self.upgrade(self.setup(1)), 0)  # 1: a step still waits for you
        self.assertEqual(self.ran, [('nekoserver', 'neko', 'installing')])
        self.assertFalse(self.journal.exists())
        self.assertIn(f'NekoServer {NEWER} is installed and checked', self.said)

    def test_a_failed_setup_or_check_puts_the_release_you_had_back(self):
        self.assertEqual(self.upgrade(self.setup(2, 0)), 1)
        self.assertEqual([r[0] for r in self.ran], ['nekoserver', 'v' + VERSION])
        self.assertEqual(self.ran[1][2], 'going_back')
        self.assertFalse(self.journal.exists())
        self.ran.clear()
        self.assertEqual(self.upgrade(self.setup(0, 0), check=lambda: 'the dashboard does not answer'), 1)
        self.assertIn('the dashboard does not answer', ' '.join(self.said))
        # Putting it back failing too: it says what to run, and keeps the record.
        self.ran.clear()
        self.assertEqual(self.upgrade(self.setup(2, 2)), 2)
        self.assertTrue(self.journal.exists())
        self.assertIn('sudo bash install.sh  in ' + str(self.previous), self.said[-1])

    def test_a_setup_that_runs_out_of_time_is_put_back_at_once(self):
        def timed_out(project, settings, account, log_file):
            self.ran.append((Path(project).name, account, self.journal_phase()))
            if len(self.ran) == 1:
                raise subprocess.TimeoutExpired('install.sh', 6 * 3600)
            return 0

        self.assertEqual(self.upgrade(timed_out), 1)
        self.assertEqual([r[0] for r in self.ran], ['nekoserver', 'v' + VERSION])
        self.assertIn('more than six hours', ' '.join(self.said))
        self.assertFalse(self.journal.exists())

        # The way back failing the same way keeps the record, with why.
        def never_starts(project, settings, account, log_file):
            raise OSError('bash is not there')

        self.assertEqual(self.upgrade(never_starts), 2)
        saved = json.loads(self.journal.read_text())
        self.assertEqual((saved['phase'], 'bash is not there' in saved['problem']), ('going_back', True))

    def test_opensuse_python_repair_never_updates_the_system_during_an_upgrade(self):
        script = (ROOT / 'install.sh').read_text()
        start = script.index('if "$apply" && [[ "$family" == suse ]] && ! python3 -c')
        block = script[start : script.index("\nif ! python3 -c 'import sys;", start)]
        bin_dir = self.dir / 'bin'
        bin_dir.mkdir()
        for name, body in (('python3', 'exit 1'), ('zypper', 'echo zypper "$@" >> "$CALLS"')):
            (bin_dir / name).write_text('#!/bin/sh\n' + body + '\n')
            (bin_dir / name).chmod(0o755)
        calls = self.dir / 'calls'
        program = 'zypper_run() { zypper "$@"; }\napply=true\nfamily=suse\nID=opensuse-leap\n' + block + '\n'

        def run(**more):
            env = {'PATH': f'{bin_dir}:/usr/bin:/bin', 'CALLS': str(calls), **more}
            return subprocess.run(['bash', '-c', program], env=env, capture_output=True, text=True, check=False)

        kept = run(NEKOSERVER_KEEP_SYSTEM='1')
        self.assertEqual(kept.returncode, 4)
        self.assertIn('does not update the system', kept.stderr)
        self.assertFalse(calls.exists())  # zypper never ran
        # Setup started by hand still brings Leap up to date.
        run()
        self.assertIn('zypper update --no-recommends', calls.read_text())

    def test_a_download_or_check_cut_off_part_way_never_leaves_the_record_behind(self):
        # The audit of 0.45.6: a short archive (EOFError) or a connection closed early
        # (IncompleteRead) ended the upgrade with a traceback and left its record, which
        # kept app updates and the monthly check waiting until the next restart.
        import http.client

        for error in (
            EOFError('Compressed file ended before the end-of-stream marker was reached'),
            http.client.IncompleteRead(b'x', 10),
        ):
            with self.subTest(type(error).__name__), mock.patch.object(upgrade, 'download', side_effect=error):
                self.said.clear()
                self.assertEqual(self.upgrade(self.setup(0)), 1)
                self.assertFalse(self.journal.exists())
                self.assertIn('this server stays on', self.said[-1])

        # A check that fails itself is a failed check: the release you had is put back.
        def broken():
            raise http.client.BadStatusLine('half a reply')

        self.assertEqual(self.upgrade(self.setup(0, 0), check=broken), 1)
        self.assertIn('the check could not be made', ' '.join(self.said))
        self.assertFalse(self.journal.exists())

    def test_a_download_that_is_not_that_release_changes_nothing(self):
        for payload in (release(NEWER, version='0.0.1'), b'not an archive', release('v0.0.1Beta')):
            with self.subTest(payload=payload[:20]):
                self.said.clear()
                self.assertEqual(self.upgrade(self.setup(), payload=payload), 1)
                self.assertFalse(self.journal.exists())
                self.assertIn('this server stays on ' + VERSION, self.said[-1])
        self.assertEqual(self.ran, [])
        # A link out of the folder is refused by the unpacking itself.
        bad = io.BytesIO()
        with tarfile.open(fileobj=bad, mode='w:gz') as bundle:
            info = tarfile.TarInfo('NekoServer-All-x/escape')
            info.type, info.linkname = tarfile.SYMTYPE, '/etc/passwd'
            bundle.addfile(info)
        self.assertEqual(self.upgrade(self.setup(), payload=bad.getvalue()), 1)

    def test_nothing_starts_while_something_else_is_at_work_or_for_an_older_release(self):
        self.journal.parent.mkdir(parents=True)
        self.journal.write_text('{}')
        self.assertEqual(self.upgrade(self.setup()), 2)
        self.assertIn('already at work', self.said[-1])
        self.journal.unlink()
        (self.dir / 'none.json').write_text('{}')  # an app update not finished
        self.assertEqual(self.upgrade(self.setup()), 2)
        (self.dir / 'none.json').unlink()
        self.assertEqual(self.upgrade(self.setup(), tag='v0.0.1Beta'), 2)
        self.assertEqual(self.upgrade(self.setup(), tag='v9.9.9Bata'), 2)
        self.assertEqual(self.ran, [])

    def test_the_check_finds_a_step_that_stopped_working(self):
        # The status is the one the installer writes (0.43.0 looked for "complete", which
        # it never writes, so no lost step could ever be found).
        self.assertEqual(upgrade.DONE, 'success')
        self.assertIn("complete_status = 'success' if args.apply", (ROOT / 'installer.py').read_text())
        before = upgrade.steps_done({'data_mount': str(self.data)})
        self.assertEqual(before, {'storage', 'photos'})
        self.state({'storage': 'success', 'photos': 'failed', 'tailscale': 'needs_setup'})
        wanted = '.'.join(map(str, upgrade.numbers(NEWER)))
        config = {'data_mount': str(self.data)}
        answers = lambda v: wanted  # noqa: E731
        found = upgrade.works_on(config, NEWER, before, answers, lambda v: [])
        self.assertIn('photos', found)
        self.state({'storage': 'success', 'photos': 'success'})
        self.assertIsNone(upgrade.works_on(config, NEWER, before, answers, lambda v: []))
        self.assertIn('dashboard', upgrade.works_on(config, NEWER, before, lambda v: VERSION, lambda v: []))
        behind = upgrade.works_on(config, NEWER, before, answers, lambda v: ['nekoserver-app-backup.service'])
        self.assertIn('nekoserver-app-backup.service', behind)

    def test_putting_the_release_back_is_checked_before_it_is_called_done(self):
        # Its setup ends with 1 (a step failed, or one only waits for you): the check decides.
        self.back_problem = 'these steps worked before and do not now: photos'
        self.assertEqual(self.upgrade(self.setup(2, 1)), 2)
        self.assertTrue(self.journal.exists())  # app updates and the monthly check keep waiting
        self.assertIn('did not pass either: these steps worked before', self.said[-1])
        # The check is of the previous release, against the steps that worked before.
        self.assertEqual(self.verified[-1], ('v' + VERSION, ['photos', 'storage']))
        self.journal.unlink()
        self.back_problem = None
        self.assertEqual(self.upgrade(self.setup(2, 1)), 1)
        self.assertFalse(self.journal.exists())
        self.assertIn('passed its check', self.said[-1])
        # After a restart too, with the steps the upgrade noted before it began.
        upgrade.record('installing', self.journal, to=NEWER, previous=str(self.previous), before=['storage'])
        self.back_problem = 'the dashboard does not answer'
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(1), self.journal), 2)
        self.assertEqual(self.verified[-1], ('v' + VERSION, ['storage']))
        self.assertTrue(self.journal.exists())

    def test_the_dashboard_is_asked_on_the_port_that_release_uses(self):
        config = {'data_mount': str(self.data), 'dashboard': {'port': 8787}}
        kept = self.data / 'installer/releases' / ('v' + VERSION) / 'config.json'
        kept.write_text(json.dumps({'dashboard': {'port': 8790}}))  # setup found 8787 taken
        self.assertEqual(upgrade.dashboard_port_of(config, VERSION), 8790)
        self.assertEqual(upgrade.dashboard_port_of(config, '9.9.9'), 8787)  # no copy kept: the settings
        self.assertEqual(upgrade.dashboard_port_of({'data_mount': str(self.data)}, '9.9.9'), 8787)

    def test_every_installed_service_must_run_the_new_release(self):
        units = self.dir / 'units'
        units.mkdir()
        (units / 'nekoserver-dashboard.service').write_text('ExecStart=/usr/local/lib/nekoserver-dashboard/v1.2.3/x\n')
        (units / 'nekoserver-app-backup.service').write_text(
            'ExecStart=/srv/n/installer/runtime/v1.2.2/x app-snapshot\n'
        )
        (units / 'nekoserver-heartbeat.timer').write_text('[Timer]\nOnCalendar=hourly\n')  # no release in it
        (units / 'docker.service').write_text('ExecStart=/v0.0.1/dockerd\n')  # not NekoServer's
        self.assertEqual(upgrade.services_behind('1.2.3', units), ['nekoserver-app-backup.service'])
        self.assertEqual(upgrade.services_behind('1.2.2', units), ['nekoserver-dashboard.service'])

    def test_a_cut_off_upgrade_is_put_right_at_the_next_start(self):
        upgrade.record('downloading', self.journal, to=NEWER, previous=str(self.previous))
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(), self.journal), 0)
        self.assertFalse(self.journal.exists())
        upgrade.record('installing', self.journal, to=NEWER, previous=str(self.previous), before=['storage'])
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(0), self.journal), 1)
        self.assertEqual(self.ran[-1][0], 'v' + VERSION)
        self.assertFalse(self.journal.exists())
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(), self.journal), 0)

    def test_setup_runs_without_questions_and_without_a_system_update(self):
        project = self.dir / 'project'
        project.mkdir()
        seen = {}

        def run(argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return mock.Mock(returncode=0)

        upgrade.run_setup(project, self.settings, 'neko', self.dir / 'log', run=run)
        self.assertEqual(seen['argv'][2:], ['--profile', 'full', '--apply'])
        self.assertIs(seen['stdin'], upgrade.subprocess.DEVNULL)
        self.assertEqual(seen['env']['NEKOSERVER_KEEP_SYSTEM'], '1')
        self.assertEqual(seen['env']['SUDO_USER'], 'neko')
        self.assertEqual((project / 'my-config.json').read_text(), self.settings.read_text())
        # install.sh and host.py honour it: no "pacman -Syu", no new package on Arch.
        script = (ROOT / 'install.sh').read_text()
        self.assertIn('elif [[ -n "${NEKOSERVER_KEEP_SYSTEM:-}" ]]; then', script)
        self.assertLess(script.index('NEKOSERVER_KEEP_SYSTEM'), script.index('pacman -Syu --noconfirm'))
        import host

        arch = host.Arch()
        with mock.patch.dict(upgrade.os.environ, {'NEKOSERVER_KEEP_SYSTEM': '1'}):
            with self.assertRaisesRegex(RuntimeError, 'full system update'):
                arch._install(['samba'], 60)


class UpgradeReportTests(unittest.TestCase):
    """0.45.8: the dashboard's "last upgrade" line read reports/jobs/upgrade.json, which
    nothing wrote, so every upgrade (and every failed one) showed as "unknown"."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.dir)]))
        self.config = {'data_mount': str(self.dir / 'ssd')}
        self.journal = self.dir / 'upgrade.json'
        self.path = self.dir / 'ssd' / upgrade.REPORT
        clock = iter(range(100, 200))
        self.now = lambda: next(clock)
        mounted = mock.patch.object(upgrade.os.path, 'ismount', lambda p: Path(p) == self.dir / 'ssd')
        mounted.start()
        self.addCleanup(mounted.stop)

    def report(self, code, said='done', journal=False, unit='nekoserver-upgrade@' + NEWER):
        seen = []

        def work(out):
            seen.append(json.loads(self.path.read_text()))
            if journal:
                self.journal.write_text('{}')
            out(said)
            return code

        got = upgrade.reported(self.config, unit, work, journal=self.journal, now=self.now, out=lambda line: None)
        self.assertEqual(got, code)
        self.assertEqual(seen[0]['status'], 'running')
        self.assertEqual(seen[0]['unit'], unit)
        return json.loads(self.path.read_text())

    def test_each_ending_is_reported_as_the_dashboard_reads_it(self):
        passed = self.report(0, 'NekoServer is installed and checked')
        self.assertEqual((passed['status'], passed['last_success']), ('pass', passed['finished_at']))
        back = self.report(1, 'This server runs 0.45.7 again')
        self.assertEqual((back['status'], back['missing']), ('partial', ['This server runs 0.45.7 again']))
        # The last success is kept from the run before.
        self.assertEqual(back['last_success'], passed['last_success'])
        refused = self.report(2, 'not newer; nothing was changed')
        self.assertEqual(refused['status'], 'partial')
        stuck = self.report(2, 'Putting it back did not pass either', journal=True)
        self.assertEqual((stuck['status'], stuck['reason']), ('fail', 'Putting it back did not pass either'))

    def test_an_error_still_ends_the_report(self):
        self.journal.write_text('{}')

        def work(out):
            raise OSError('disk full')

        with self.assertRaises(OSError):
            upgrade.reported(self.config, 'nekoserver-upgrade-resume', work, journal=self.journal, now=self.now)
        row = json.loads(self.path.read_text())
        self.assertEqual(row['status'], 'fail')
        self.assertIn('disk full', row['reason'])

    def test_nothing_is_written_while_the_ssd_is_not_mounted(self):
        self.config['data_mount'] = str(self.dir / 'gone')
        self.assertEqual(upgrade.reported(self.config, 'x', lambda out: 0, journal=self.journal, now=self.now), 0)
        self.assertFalse((self.dir / 'gone').exists())


if __name__ == '__main__':
    unittest.main()
