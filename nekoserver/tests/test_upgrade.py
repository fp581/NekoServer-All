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
FINISHED = 'Finished: 2 of 3 steps done; 1 failed, waiting or unfinished'  # the installer's last line


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
        """Stand-in setup runs, answering with `codes` in turn (the new release, then the
        old), each ending its log as the installer does (upgrade.setup_problem reads it)."""
        answers = iter(codes)

        def run(project, settings, account, log_file):
            self.ran.append((Path(project).name, account, self.journal_phase()))
            self.installer_wrote(log_file, FINISHED)
            return next(answers)

        return run

    @staticmethod
    def installer_wrote(log_file, *lines):
        """Lines in setup's log as the installer writes them (a time stamp first)."""
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        with open(log_file, 'a') as log:
            log.writelines(f'[2026-01-01T00:00:00+00:00] {line}\n' for line in lines)

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
        # Seen after 0.47.4: published as "0.47.4-beta" (and 0.47.0 as "v0.47.0-Beta"),
        # which no server was offered. The runtime reads the same rule (updates.rs).
        self.assertEqual(upgrade.numbers('0.47.4-beta'), (0, 47, 4))
        self.assertEqual(upgrade.numbers('v0.47.0-Beta'), (0, 47, 0))
        for typo in ('V0.47.4', 'v0.47', '0.47.4-beta x', 'v0.47.4beta2', 'v0.47.4\n'):
            self.assertIsNone(upgrade.numbers(typo), typo)
        self.assertIn('release_tag', (ROOT / 'runtime/src/updates.rs').read_text())

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

    def test_the_older_setup_never_runs_beside_a_newer_service_still_at_work(self):
        """The review of 0.47.2: the way back ran the older setup first, and only then said
        that the newer release's service had not stopped."""
        added = self.units / 'nekoserver-foo.service'
        ran = []

        def setup_adding(project, settings, account, log_file):
            ran.append(Path(project).name)
            if 'nekoserver' in str(project):
                added.write_text('ExecStart=/usr/local/lib/x/v9.9.9/y\n')
                return 2
            return 0

        def systemctl(argv, **k):
            return subprocess.CompletedProcess(argv, 1 if 'disable' in argv else 0, '', 'Failed to stop')

        with mock.patch.object(upgrade.subprocess, 'run', side_effect=systemctl):
            self.assertEqual(self.upgrade(setup_adding), 2)
        self.assertNotIn(self.previous.name, ran)  # only the newer release's setup ran
        self.assertEqual(self.journal_phase(), 'going_back')  # resume tries again
        self.assertTrue(any('upgrade.py resume' in line for line in self.said), self.said)

    def test_setup_runs_the_profile_this_server_is_set_up_with(self):
        """The review of 0.47.2: every upgrade ran "full", which set up apps a smaller
        installation never had."""
        import installer

        profiles = {'full': ['storage', 'photos', 'nextcloud'], 'media': ['storage']}
        config = {'data_mount': str(self.data), 'profiles': profiles}
        modules = {name: {'object': None, 'deps': []} for name in ('storage', 'photos', 'nextcloud')}
        record = self.data / 'installer/state' / installer.PROFILE_FILE
        record.write_text(json.dumps({'format': installer.PROFILE_FORMAT, 'profile': 'media'}))
        self.assertEqual(upgrade.setup_profile(config, modules), 'media')
        record.write_text(json.dumps({'format': installer.PROFILE_FORMAT, 'profile': None}))
        self.assertIsNone(upgrade.setup_profile(config, modules))
        # Not recorded (set up before 0.47.3): the smallest profile covering the steps done.
        record.unlink()
        self.state({'storage': 'success'})
        self.assertEqual(upgrade.setup_profile(config, modules), 'media')
        self.state({'storage': 'success', 'photos': 'success'})
        self.assertEqual(upgrade.setup_profile(config, modules), 'full')
        # And the command setup runs with it.
        self.settings.write_text(json.dumps(config))
        record.write_text(json.dumps({'format': installer.PROFILE_FORMAT, 'profile': 'media'}))
        project = self.dir / 'project'
        project.mkdir()
        seen = []
        upgrade.run_setup(
            project,
            self.settings,
            'neko',
            self.dir / 'log',
            run=lambda argv, **k: seen.append(argv) or mock.Mock(returncode=0),
        )
        self.assertEqual(seen[0][2:], ['--profile', 'media', '--apply'])
        record.write_text(json.dumps({'format': installer.PROFILE_FORMAT, 'profile': None}))
        upgrade.run_setup(
            project,
            self.settings,
            'neko',
            self.dir / 'log',
            run=lambda argv, **k: seen.append(argv) or mock.Mock(returncode=0),
        )
        self.assertEqual(seen[1][2:], ['--apply'])

    def test_a_finished_upgrade_leaves_only_its_newest_logs(self):
        """The review of 0.47.2: every upgrade's download, unpacked release and build folder
        stayed on the SSD for good."""
        work = self.data / 'installer/upgrades'
        (work / 'v9.9.9/nekoserver/runtime/target').mkdir(parents=True)
        (work / 'v9.9.9.tar.gz').write_text('x')
        (work / 'mine.txt').write_text('someone\'s own')
        (work / 'elsewhere').mkdir()
        (work / 'v9.9.8').symlink_to(work / 'elsewhere')
        for n in range(12):
            log = work / f'v9.9.{n}.log'
            log.write_text('log')
            upgrade.os.utime(log, (1000 + n, 1000 + n))
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_text('{}')
        self.assertEqual(upgrade.tidy(work, self.journal), [])  # unfinished: nothing goes
        self.journal.unlink()
        removed = upgrade.tidy(work, self.journal)
        self.assertIn('v9.9.9', removed)
        self.assertIn('v9.9.9.tar.gz', removed)
        self.assertFalse((work / 'v9.9.9').exists())
        self.assertEqual(sorted(p.name for p in work.glob('*.log')), sorted(f'v9.9.{n}.log' for n in range(2, 12)))
        self.assertTrue((work / 'mine.txt').exists())
        self.assertTrue((work / 'v9.9.8').is_symlink() and (work / 'elsewhere').is_dir())
        # A finished upgrade tidies by itself.
        (work / 'v9.9.9').mkdir()
        self.assertEqual(self.upgrade(self.setup(0)), 0)
        self.assertFalse((work / 'v9.9.9').exists())

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
        # The last line is the one the dashboard shows: what happened, why, and how the server is now.
        self.assertEqual(
            self.said[-1],
            f'Nothing was lost: this server runs {VERSION} again and passed its check. '
            "Try the upgrade again when you're ready; this note goes away once one succeeds. "
            f'The upgrade to {NEWER} did not pass: the dashboard does not answer',
        )
        # Putting it back failing too: it says what to run, and keeps the record.
        self.ran.clear()
        self.assertEqual(self.upgrade(self.setup(2, 2)), 2)
        self.assertTrue(self.journal.exists())
        self.assertIn('sudo bash install.sh  in ' + str(self.previous), self.said[-1])

    def test_a_failed_step_and_a_refusal_by_install_sh_are_failed_setups(self):
        """The review of 0.48.0: exit code 1 meant a step that only waits for you, a step
        that failed (releases before 0.48.1) and install.sh's own refusal before the
        installer ran (the setup lock, its preflight), and the upgrade took all three for
        the first. The log says which: the installer's FAILED lines, and its Finished
        line, which a refusal never reaches."""
        import installer

        log_file = self.data / 'installer/upgrades/test.log'
        log_file.parent.mkdir()

        def problem(code, *lines):
            def run(project, settings, account, log):
                with open(log, 'a') as written:
                    written.write('\n'.join(lines) + '\n')
                return code

            return upgrade.setup_problem(run, self.previous, self.settings, 'neko', log_file)

        stamped = '[2026-01-01T00:00:00+00:00] '
        failed_photos = stamped + 'FAILED photos: the compose file is wrong'
        self.assertIsNone(problem(0, stamped + FINISHED))
        self.assertIsNone(problem(installer.WAITING, stamped + 'NEEDS SETUP tailscale: Sign in', stamped + FINISHED))
        found = problem(installer.STEP_FAILED, failed_photos, stamped + FINISHED)
        self.assertIn('FAILED photos: the compose file is wrong', found)
        self.assertTrue(found.startswith('its setup ended with a failed step'), found)
        # A release before 0.48.1 ended with 1 for a failed step as well: its log says so.
        self.assertIn('FAILED photos', problem(1, failed_photos, stamped + FINISHED))
        # install.sh's own refusal: no installer line at all; its last words are the reason.
        refused = 'Another NekoServer setup is running. Let it finish, then run  sudo bash install.sh  again.'
        found = problem(1, refused)
        self.assertIn(refused, found)
        self.assertTrue(found.startswith('its setup stopped before any step ran'), found)
        restart = 'Restart the PC now (sudo reboot), then run  sudo bash install.sh  again.'
        self.assertIn('exit code 3', problem(3, restart))
        # Only what this run wrote counts: the failed step above belongs to an earlier run.
        self.assertIsNone(problem(installer.WAITING, stamped + FINISHED))

    def test_a_new_step_that_failed_puts_the_release_you_had_back(self):
        """The check looks only at the steps done before the upgrade, so a step new in the
        release that failed was never seen: the upgrade was called installed and checked."""
        import installer

        answers = iter([(installer.STEP_FAILED, 'FAILED shows: the image could not be pulled'), (0,)])

        def run(project, settings, account, log_file):
            code, *lines = next(answers)
            self.installer_wrote(log_file, *lines, FINISHED)
            self.ran.append(Path(project).name)
            return code

        self.assertEqual(self.upgrade(run), 1)
        self.assertEqual(self.ran, ['nekoserver', 'v' + VERSION])
        self.assertIn('FAILED shows: the image could not be pulled', ' '.join(self.said))
        self.assertEqual(self.verified, [('v' + VERSION, ['photos', 'storage'])])  # only the way back was checked
        self.assertFalse(self.journal.exists())

    def test_the_old_release_guard_lets_the_way_back_run_its_setup(self):
        """The review of 0.47.9: an upgrade that wrote its unit files and then failed left
        the services pointing at the newer runtime, so the previous release's folder looked
        older than the server and install.sh's guard stopped its setup; the way back never
        ran and the record stayed open with everything waiting."""
        import io

        import capabilities

        runtime = self.data / 'installer/runtime'
        newer = NEWER.removesuffix('Beta')  # the runtime's copies are named by number alone
        for name in ('v' + VERSION, newer):
            (runtime / name).mkdir(parents=True)
        (self.units / 'nekoserver-dashboard.service').write_text(
            f'ExecStart={runtime}/{newer}/nekoserver-runtime serve\n'
        )
        (self.previous / 'my-config.json').write_text(json.dumps({'data_mount': str(self.data)}))
        guard = []
        answers = iter([2])  # the newer release's setup fails after it wrote its units

        def setup(project, settings, account, log_file):
            code = next(answers, None)
            if code is None:  # the way back: install.sh's own guard, as it runs in that folder
                out = io.StringIO()
                code = capabilities.preflight([], out, root=project, units=self.units, journal=self.journal)
                guard.append(out.getvalue())
            self.ran.append((Path(project).name, account, self.journal_phase()))
            return code

        self.assertEqual(self.upgrade(setup), 1)
        self.assertEqual([r[0] for r in self.ran], ['nekoserver', 'v' + VERSION])
        self.assertEqual(guard, [''], 'the guard let the way back run')
        self.assertFalse(self.journal.exists())
        # A person's own setup from a downloaded folder is still stopped: with no record,
        # and with a record that names the SSD copy being put back.
        download = self.dir / 'download'
        download.mkdir()
        (download / 'my-config.json').write_text(json.dumps({'data_mount': str(self.data)}))
        out = io.StringIO()
        self.assertEqual(capabilities.preflight([], out, root=download, units=self.units, journal=self.journal), 1)
        self.assertIn('this server already runs', out.getvalue())
        record = {'format': 'neko-upgrade-v1', 'phase': 'going_back', 'previous': str(self.previous)}
        self.journal.write_text(json.dumps(record))
        self.assertEqual(
            capabilities.preflight([], io.StringIO(), root=download, units=self.units, journal=self.journal), 1
        )
        self.assertEqual(
            capabilities.preflight([], io.StringIO(), root=self.previous, units=self.units, journal=self.journal), 0
        )
        # A record from a download that was cut off never lifts the guard.
        self.journal.write_text(json.dumps({**record, 'phase': 'downloading'}))
        self.assertEqual(
            capabilities.preflight([], io.StringIO(), root=self.previous, units=self.units, journal=self.journal), 1
        )

    def test_a_0479_folder_put_back_passes_its_own_old_guard(self):
        """The review of 0.47.10: the way back runs the previous release's install.sh, so
        the guard that runs is that release's. 0.47.9's (a verbatim copy here) knows nothing
        of the upgrade's record: with a service pointing at the newer runtime it stopped
        the way back. Now the newer runtime is hidden from it while the old setup runs, the
        services keep finding their program, and the copy has its own name back after."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            'old_release_guard_0479', ROOT / 'tests/old_release_guard_0479.py'
        )
        old_release_guard_0479 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(old_release_guard_0479)
        runtime = self.data / 'installer/runtime'
        newer = NEWER.removesuffix('Beta')
        for name in ('v' + VERSION, newer):
            (runtime / name).mkdir(parents=True)
        program = runtime / newer / 'nekoserver-runtime'
        program.write_text('the newer runtime')
        (self.units / 'nekoserver-dashboard.service').write_text(f'ExecStart={program} serve\n')
        (self.previous / 'my-config.json').write_text(json.dumps({'data_mount': str(self.data)}))
        before_fix = old_release_guard_0479.newer_installed(self.previous, VERSION, self.units)
        self.assertEqual(before_fix, newer.lstrip('v'), 'the old guard stops the way back without the fix')
        seen = []
        answers = iter([2])  # the newer release's setup fails after it wrote its units

        def setup(project, settings, account, log_file):
            code = next(answers, None)
            if code is None:  # the way back: the 0.47.9 guard, as its install.sh runs it
                seen.append(old_release_guard_0479.newer_installed(project, VERSION, self.units))
                seen.append(program.read_text())  # the service's program is still there
                code = 1 if seen[0] else 0
            self.ran.append((Path(project).name, account, self.journal_phase()))
            return code

        self.assertEqual(self.upgrade(setup), 1)
        self.assertEqual([r[0] for r in self.ran], ['nekoserver', 'v' + VERSION])
        self.assertEqual(seen, [None, 'the newer runtime'])
        self.assertFalse(self.journal.exists())
        # Afterwards: the newer copy has its own name back, no link and no hidden copy left.
        self.assertTrue((runtime / newer).is_dir() and not (runtime / newer).is_symlink())
        self.assertEqual(sorted(p.name for p in runtime.iterdir()), sorted(['v' + VERSION, newer]))

    def test_a_copy_hidden_when_the_power_was_cut_is_put_back(self):
        runtime = self.data / 'installer/runtime'
        newer = NEWER.removesuffix('Beta')
        aside = runtime / ('.' + newer + upgrade.HIDDEN)
        aside.mkdir(parents=True)
        (runtime / newer).symlink_to(aside.name)
        (runtime / ('v' + VERSION)).mkdir()
        with upgrade.newer_runtimes_hidden(runtime, 'v' + VERSION):
            self.assertTrue((runtime / newer).is_symlink())
        self.assertTrue((runtime / newer).is_dir() and not (runtime / newer).is_symlink())
        self.assertFalse(aside.exists())
        # An older release's copy, and the previous one itself, are never moved.
        with upgrade.newer_runtimes_hidden(runtime, newer):
            self.assertFalse(any(p.is_symlink() for p in runtime.iterdir()))

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
        # Its setup ends with 1 and its log says a step only waits for you: the check decides.
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

    def test_the_upgrade_folder_is_tidied_when_the_way_back_removed_the_settings(self):
        """Seen on the VM: upgrade.py resume, run from the newer release's copy on the SSD,
        put the previous release back, whose setup removed that copy and its settings file
        with it; the tidy after it could no longer read the settings, and the download and
        the build folder stayed in installer/upgrades."""
        work = self.data / upgrade.UPGRADES
        leftover = work / NEWER / 'NekoServer-All' / 'nekoserver'
        leftover.mkdir(parents=True)
        upgrade.record('installing', self.journal, to=NEWER, previous=str(self.previous), before=['storage'])

        def setup(project, settings, account, log_file):
            self.ran.append(Path(project).name)
            Path(self.settings).unlink()  # the previous release's cleanup removed the newer copy
            return 0

        self.assertEqual(upgrade.resume(self.settings, self.said.append, setup, self.journal), 1)
        self.assertFalse(any('could not be tidied' in line for line in self.said), self.said)
        self.assertFalse((work / NEWER).exists())

    def test_a_cut_off_upgrade_is_put_right_at_the_next_start(self):
        upgrade.record('downloading', self.journal, to=NEWER, previous=str(self.previous))
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(), self.journal), 0)
        self.assertFalse(self.journal.exists())
        upgrade.record('installing', self.journal, to=NEWER, previous=str(self.previous), before=['storage'])
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(0), self.journal), 1)
        self.assertEqual(self.ran[-1][0], 'v' + VERSION)
        self.assertFalse(self.journal.exists())
        # The last line is the one the dashboard shows: it says what was put right.
        self.assertEqual(
            self.said[-1],
            f'Nothing was lost: this server runs {VERSION} again and passed its check. '
            "Try the upgrade again when you're ready; this note goes away once one succeeds. "
            f'The upgrade to {NEWER} was cut off before it was checked',
        )
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(), self.journal), 0)

    def test_the_log_is_named_by_a_release_tag_only_and_stays_in_the_upgrades_folder(self):
        """The record's `to` is read back from a file at a start: only a release tag names
        the log, anything else gets resume.log, and a link at the log's name to a file
        outside the folder is refused (never written to or read through)."""
        work = self.data / upgrade.UPGRADES
        work.mkdir()
        self.assertEqual(upgrade.upgrade_log(work, NEWER), work / (NEWER + '.log'))
        for odd in ('../../etc/passwd', 'resume', '', None, 'v1.2', 'v1.2.3.log'):
            self.assertEqual(upgrade.upgrade_log(work, odd), work / 'resume.log')
        upgrade.record('installing', self.journal, to='../../etc/passwd', previous=str(self.previous), before=[])
        logs = []

        def setup(project, settings, account, log_file):
            logs.append(Path(log_file))
            return 0

        self.assertEqual(upgrade.resume(self.settings, self.said.append, setup, self.journal), 1)
        self.assertEqual(logs, [work / 'resume.log'])
        (work / 'resume.log').symlink_to(self.dir / 'elsewhere.log')
        with self.assertRaises(RuntimeError):
            upgrade.upgrade_log(work, 'resume')
        found = upgrade.setup_problem(setup, self.previous, self.settings, 'neko', work / 'resume.log')
        self.assertIn('a link to a file outside', found)
        self.assertEqual(len(logs), 1)  # setup was not run

    def test_a_release_installed_by_hand_over_a_failed_upgrade_is_not_put_back(self):
        """Seen on the rig: an upgrade from 0.46.2 failed and could not go back, 0.46.3 was
        then set up by hand, and at the next start its own resume service put 0.46.2 back
        over it. A server that runs the release the upgrade was for, whole, is done."""
        older = self.data / 'installer/releases/v0.1.0'
        older.mkdir()
        upgrade.record('going_back', self.journal, to='v' + VERSION, previous=str(older), before=['storage'])
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(), self.journal), 0)
        self.assertEqual((self.ran, self.verified), ([], [('v' + VERSION, ['storage'])]))
        self.assertFalse(self.journal.exists())
        self.assertIn(f'this server runs {VERSION} and passed its check', self.said[-1])
        # Not whole (the check fails): the release it had is put back, as before.
        upgrade.record('installing', self.journal, to='v' + VERSION, previous=str(older), before=['storage'])
        self.back_problem = 'the dashboard does not answer'
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(0), self.journal), 2)
        self.assertEqual(self.ran[-1][0], 'v0.1.0')

    def test_a_failed_upgrade_whose_way_back_was_cut_off_is_put_back_at_the_next_start(self):
        """The upgrade's setup failed on a step new in its release, and the way back was cut
        off (a power cut) before the older setup rewrote the resume service, so the next
        start ran the resume from the new release's copy. The check looks only at the steps
        done before the upgrade: it passed, and the record was closed on the release that
        failed ("passed its check"). Now the release the server had is put back."""
        older = self.data / 'installer/releases/v0.1.0'
        older.mkdir()
        (older / 'install.sh').write_text('#!/bin/bash\n')
        upgrade.record('failed', self.journal, to='v' + VERSION, previous=str(older), before=['storage'])
        upgrade.record('going_back', self.journal, problem='FAILED shows: the new step could not start')
        self.state({'storage': 'success', 'photos': 'success', 'new_step': 'failed'})
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(0), self.journal), 1)
        self.assertEqual(self.ran[-1][0], 'v0.1.0')
        self.assertFalse(any('passed its check. App updates' in line for line in self.said), self.said)
        self.assertIn('The upgrade to v' + VERSION + ' did not pass: FAILED shows', self.said[-1])
        # Cut off part-way through a step (left "running"): the same.
        upgrade.record('installing', self.journal, to='v' + VERSION, previous=str(older), before=['storage'])
        self.state({'storage': 'success', 'new_step': 'running'})
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(0), self.journal), 1)
        self.assertEqual(self.ran[-1][0], 'v0.1.0')
        # Cut off while it was checked, after its setup had ended: a step that was failing
        # before the upgrade (an optional one waiting for the person) puts nothing back.
        upgrade.record('verifying', self.journal, to='v' + VERSION, previous=str(older), before=['storage'])
        self.state({'storage': 'success', 'tv': 'failed'})
        ran = len(self.ran)
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(), self.journal), 0)
        self.assertEqual(len(self.ran), ran, 'nothing was put back')
        # A person's own setup run that just ended closes it all the same (settle_after_setup).
        upgrade.record('going_back', self.journal, to='v' + VERSION, previous=str(older), before=['storage'])
        self.state({'storage': 'success', 'new_step': 'failed'})
        with mock.patch.object(upgrade, 'keep_report'):
            self.assertTrue(
                upgrade.settle_after_setup(json.loads(self.settings.read_text()), self.said.append, self.journal)
            )

    def hidden_by_a_cut_off_way_back(self, linked):
        """What a way back to an older release leaves when it is cut off while the older
        setup is still in its preamble: this release's runtime copy under its hidden name,
        the link at its own name (or nothing there: cut between the rename and the link),
        and the record in phase going_back. Returns the copy's own path."""
        older = self.data / 'installer/releases/v0.1.0'
        older.mkdir()
        runtime = self.data / 'installer/runtime'
        copy, aside = runtime / ('v' + VERSION), runtime / ('.v' + VERSION + upgrade.HIDDEN)
        aside.mkdir(parents=True)
        (aside / 'nekoserver-runtime').write_text('the runtime')
        if linked:
            copy.symlink_to(aside.name)
        upgrade.record('going_back', self.journal, to='v' + VERSION, previous=str(older), before=['storage'])
        return copy

    def assert_own_name_back(self, copy):
        self.assertFalse(self.journal.exists())
        self.assertTrue(copy.is_dir() and not copy.is_symlink())
        self.assertEqual((copy / 'nekoserver-runtime').read_text(), 'the runtime')
        self.assertEqual([p.name for p in copy.parent.iterdir()], [copy.name])  # no link, no hidden copy left

    def test_a_copy_hidden_by_a_cut_off_way_back_gets_its_name_back_when_the_record_is_closed(self):
        """The review of 0.48.0: the way back hides this release's runtime copy while the
        older setup runs (newer_runtimes_hidden), and only its own end gave the name back.
        Cut off before the older setup marked a step running (a power cut, systemctl stop),
        the next start found the server whole and closed the record (settled), so the link
        stayed at the copy's name for good: the guard could not see this release, and every
        later setup of it refused the link ("Refusing symlink path")."""
        copy = self.hidden_by_a_cut_off_way_back(linked=True)
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(), self.journal), 0)
        self.assertEqual(self.ran, [])
        self.assert_own_name_back(copy)

    def test_a_copy_left_with_nothing_at_its_name_gets_it_back_after_setup_by_hand(self):
        """Cut between the rename and the link: nothing is at the copy's name. A setup run
        by hand that closes the record (settle_after_setup) gives it back too."""
        copy = self.hidden_by_a_cut_off_way_back(linked=False)
        config = json.loads(self.settings.read_text())
        self.assertTrue(upgrade.settle_after_setup(config, self.said.append, self.journal))
        self.assert_own_name_back(copy)

    def test_setup_run_by_hand_closes_the_record_of_an_upgrade_that_did_not_finish(self):
        """Seen on the rig: the record stayed after the setup the upgrade itself asked for,
        so app updates and the monthly check waited until the next start, or for ever."""
        config = json.loads(self.settings.read_text())
        upgrade.record('going_back', self.journal, to=NEWER, previous=str(self.previous), before=['storage'])
        # The upgrade's own setup run (it holds the operation lock): never.
        with upgrade.hold_operation(upgrade.OPERATION_LOCK):
            self.assertFalse(upgrade.settle_after_setup(config, self.said.append, self.journal))
        self.back_problem = 'these services do not run it'
        self.assertFalse(upgrade.settle_after_setup(config, self.said.append, self.journal))
        self.assertTrue(self.journal.exists())
        # The release it had, put back by hand and whole (what a failed way back asks for).
        self.back_problem = None
        with mock.patch.object(upgrade, 'keep_report') as kept:
            self.assertTrue(upgrade.settle_after_setup(config, self.said.append, self.journal))
        self.assertFalse(self.journal.exists())
        # The report ends too, or the dashboard goes on saying the upgrade needs you.
        path, row = kept.call_args.args
        self.assertEqual((path, row['status']), (self.data / 'reports/jobs/upgrade.json', 'partial'))
        self.assertEqual(row['missing'], [self.said[-1]])
        self.assertFalse(upgrade.settle_after_setup(config, self.said.append, self.journal))  # none left
        # Some other release than the two the record names: left for the resume service.
        older = self.data / 'installer/releases/v0.1.0'
        upgrade.record('failed', self.journal, to=NEWER, previous=str(older))
        self.assertFalse(upgrade.settle_after_setup(config, self.said.append, self.journal))
        # At a start, the release it had is set up again even when it looks whole: the
        # new one may be half set up.
        upgrade.record('installing', self.journal, to=NEWER, previous=str(self.previous), before=['storage'])
        self.assertEqual(upgrade.resume(self.settings, self.said.append, self.setup(0), self.journal), 1)
        self.assertEqual(self.ran[-1][0], 'v' + VERSION)

    def test_going_back_from_an_older_upgraders_record_removes_the_new_dashboard_socket(self):
        """Seen on the rig: 0.46.2's record has no list of units, so 0.46.3's dashboard
        socket stayed when 0.46.2 was put back, and held its port against its dashboard."""
        older = self.data / 'installer/releases/v0.46.2'
        older.mkdir()
        for name in ('nekoserver-dashboard.service', 'nekoserver-dashboard.socket'):
            (self.units / name).write_text('x\n')
        saved = {'to': NEWER}
        self.assertEqual(upgrade.units_before(saved, older), {'nekoserver-dashboard.service'})
        self.assertEqual(len(upgrade.units_before(saved, older.with_name('v0.46.3'))), 2)
        self.assertEqual(upgrade.units_before({'units': ['a.timer']}, older), {'a.timer'})
        upgrade.record('failed', self.journal, to=NEWER, previous=str(older))
        ran = []

        def systemctl(argv, **k):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, 3 if 'is-active' in argv else 0, '', '')

        with mock.patch.object(upgrade.subprocess, 'run', side_effect=systemctl):
            upgrade.resume(self.settings, self.said.append, self.setup(0), self.journal)
        self.assertEqual([p.name for p in self.units.iterdir()], ['nekoserver-dashboard.service'])
        self.assertIn(['systemctl', 'disable', '--now', 'nekoserver-dashboard.socket'], ran)

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

    def test_the_service_fails_only_when_the_upgrade_needs_you(self):
        """Seen on the rig (0.47.5): an upgrade cut off and put back at the next start ended
        its service with 1. The service stayed failed, and the dashboard showed "Its
        service could not run (exit-code, exit 1)", urgent, in place of the report's
        "This server runs 0.48.2 again"."""
        for code in (0, 1, 2):  # upgraded, put back, refused: the report says which
            self.assertEqual(upgrade.service_code(code, self.journal), 0)
        self.journal.write_text('{}')  # the record is still there: it needs you
        self.assertEqual(upgrade.service_code(2, self.journal), 2)
        self.assertIn('return service_code(reported(', (ROOT / 'upgrade.py').read_text())

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
