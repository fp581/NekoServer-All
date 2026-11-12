"""Wizard specification T09: with a drive or a network share missing, no job writes into
the empty folder it would have been mounted on (that folder is on the system disk).

Every job of the runtime (each command of main.rs) is listed here with the chain of
calls that takes it to the drive check: backup::guard (the SSD, the HDD and every other
drive or share in use are the ones setup chose, each mounted), guard_data (the SSD only,
for the two jobs that need only it) or validate_external (the USB drive). Each link of a
chain must be a call written in the code. A job with no drive check is listed with the
reason, and a job missing from both lists fails the test.

What breaks each test (one line each):
- removing `guard(&cfgpath, &data, &media)?;` from backup::open_job, or
  `backup::guard(&config, &data, &media)?;`-style lines from maintenance::mounts,
  media::run, nas_watch::run or os_policy::run fails
  test_every_job_that_writes_to_a_drive_checks_the_drives_first;
- a new command in main.rs that is in neither list fails
  test_every_command_of_the_runtime_is_accounted_for;
- removing the loop over common::drives_in_use from backup::guard fails
  test_the_check_covers_both_drives_and_every_share_in_use;
- a unit whose ExecStart runs `archive`, `copy` or `database` fails
  test_the_commands_without_a_check_are_never_run_by_a_timer.

The check itself refusing a folder with nothing mounted is tested in the runtime
(backup.rs drive_check: "Another drive, or nothing mounted: refused as before").
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import spec_sources  # noqa: E402

GUARD = ('backup', 'guard')
SSD_ONLY = ('backup', 'guard_data')
# Bringing the apps back (gate run C9): the SSD and the HDD as GUARD checks them, and a
# drive of a group's own either the one setup chose or away (its apps stay stopped).
PRESENT = ('backup', 'guard_present')
USB = ('backup', 'validate_external')
COORDINATE = [('backup', 'coordinate'), GUARD]  # the shared lock of the backup jobs: checks, then locks

# command of the runtime: the calls from its entry to the drive check.
CHECKED = {
    'maintenance': [('maintenance', 'run'), ('maintenance', 'mounts'), GUARD],
    'maintenance-resume': [('maintenance', 'run'), ('maintenance', 'mounts'), GUARD],
    'update-check': [('maintenance', 'check_updates'), ('maintenance', 'mounts'), GUARD],
    'update-apps': [('maintenance', 'update_apps'), ('maintenance', 'mounts'), GUARD],
    'hdd-policy': [('os_policy', 'run'), GUARD],
    'music-backup': [('music_backup', 'run'), *COORDINATE],
    'storage-guard': [('startup', 'run'), ('backup', 'open_job'), GUARD],
    'gateway-guard': [('startup', 'run'), ('backup', 'open_job'), GUARD],
    'drive-return': [('drive_return', 'run'), ('drive_return', 'start_again'), PRESENT],
    'nas-watch': [('nas_watch', 'run'), GUARD],
    'host-snapshot': [('host_snapshot', 'run'), *COORDINATE],
    'host-resume': [('host_snapshot', 'resume_command'), *COORDINATE],
    'app-snapshot': [('app_snapshot', 'run'), *COORDINATE],
    'app-resume': [('app_snapshot', 'resume_command'), *COORDINATE],
    'media-health': [('media', 'run'), GUARD],
    'preservation': [('backup', 'preservation'), GUARD],
    'seal-passwords': [('backup', 'seal_passwords'), GUARD],
    'weekly': [('backup', 'weekly'), ('backup', 'no_monthly_run'), *COORDINATE],
    'immich-resume': [
        ('backup', 'immich_resume'),
        ('backup', 'resume_paused'),
        ('backup', 'coordinate_data'),
        SSD_ONLY,
    ],
    'cloud-resume': [('backup', 'cloud_resume'), ('backup', 'resume_paused'), ('backup', 'coordinate_data'), SSD_ONLY],
    'control': [('control', 'run'), ('apps', 'finish_restarts_when_free'), ('apps', 'shared_lock'), GUARD],
}
# The weekly job also writes to the USB drive: that drive has its own check.
USB_CHAIN = [('backup', 'weekly'), ('backup', 'usb_copy'), ('backup', 'copy_to_usb'), USB]
# Commands with no drive check, and why that is right (or what to know about them).
NOT_CHECKED = {
    'serve': 'the dashboard: it shows what the collector saved and writes to no drive',
    'collect': "the dashboard's collector: it reads the drives' state and saves it under /run",
    'job-states': 'a read for the dashboard',
    'notify': 'phone alerts: its state is under /var/lib and /run, on the system disk by design',
    'heartbeat': 'the outage check-in: its state is under /run, and it must work with no drive at all',
    'check-drives': 'it is the drive check, run by setup',
    'check-compose': "a check of an app's compose file, run by setup",
    'archive': 'run by hand with the folders given on the command line; no timer runs it',
    'copy': 'run by hand; with --apply the USB drive is checked by its UUID (validate_external in main.rs)',
    'database': 'run by hand with the folders given on the command line; no timer runs it',
}
BY_HAND = ('archive', 'copy', 'database')


class MissingDriveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = spec_sources.rust_functions()
        cls.calls = spec_sources.rust_calls(cls.functions)
        main = spec_sources.rust_code('main.rs')
        arms = r'^\s*"([a-z-]+)" => (?:\{\s*)?(?:if [^{]*\{[^}]*\}\s*)?(\w+)::(\w+)\('
        cls.commands = {command: (file, name) for command, file, name in re.findall(arms, main, re.M)}

    def assert_chain(self, what, chain):
        for caller, called in zip(chain, chain[1:]):
            self.assertIn(caller, self.functions, what)
            self.assertIn(called, self.calls[caller], f'{what}: {caller[0]}::{caller[1]} no longer calls {called[1]}')

    def test_every_command_of_the_runtime_is_accounted_for(self):
        self.assertGreaterEqual(len(self.commands), 31)
        self.assertEqual(set(self.commands), set(CHECKED) | set(NOT_CHECKED))
        self.assertEqual(set(CHECKED) & set(NOT_CHECKED), set())
        for command, chain in CHECKED.items():
            self.assertEqual(self.commands[command], chain[0], command)  # the chain starts at the command

    def test_every_job_that_writes_to_a_drive_checks_the_drives_first(self):
        for command, chain in CHECKED.items():
            self.assertIn(chain[-1], (GUARD, SSD_ONLY, PRESENT), command)
            self.assert_chain(command, chain)
        self.assert_chain('the weekly copy to the USB drive', USB_CHAIN)
        # In the jobs that check at once, the check is their first step with the drives:
        # nothing is made, copied or locked on a drive before it.
        early = r'common::directory\(|common::atomic\w*\(|common::lock\w*\(|fs::write\(|create_dir\w*\(|copy_\w+\('
        for function, check in (
            (('backup', 'open_job'), 'guard('),
            (('backup', 'coordinate'), 'guard('),
            (('backup', 'coordinate_data'), 'guard_data('),
            (('maintenance', 'mounts'), 'backup::guard('),
            (('apps', 'shared_lock'), 'backup::guard('),
            (('media', 'run'), 'backup::guard('),
            (('os_policy', 'run'), 'backup::guard('),
            (('nas_watch', 'run'), 'backup::guard('),
            (('backup', 'seal_passwords'), 'guard('),
            (('drive_return', 'start_again'), 'backup::guard_present('),
        ):
            body = self.functions[function]
            self.assertIn(check, body, function)
            self.assertEqual(re.findall(early, body[: body.find(check)]), [], function)

    def test_the_usb_copy_checks_its_drive_before_and_holds_it_while_copying(self):
        body = self.functions[('backup', 'copy_to_usb')]
        first_copy = body.find('mirror(')
        for check in ('require_private_mount(dest', 'run.drives()', 'common::expect_drive(dest)'):
            self.assertTrue(0 <= body.find(check) < first_copy, check)

    def test_the_check_covers_both_drives_and_every_share_in_use(self):
        guard = self.functions[GUARD]
        main = self.functions[('backup', 'main_drives')]
        self.assertIn('[("data_ssd", data), ("media_hdd", media)]', main)
        self.assertIn('checked_drive(c, key, path)?', main)
        for body in (guard, self.functions[PRESENT]):
            self.assertIn('main_drives(&c, data, media)?', body)
            self.assertIn('for name in common::drives_in_use(&c)', body)  # other drives and NFS shares
            self.assertIn('checked_drive(&c, &name, &folder)?', body)
        # Only bringing the apps back lets a placed drive be away: nothing mounted there.
        self.assertIn('Ok(None)', self.functions[PRESENT])
        self.assertNotIn('Ok(None)', guard)
        self.assertIn('checked_drive(&c, "data_ssd", data)', self.functions[SSD_ONLY])
        # A folder the job holds (expect_drive) is looked at again before each write below it.
        self.assertIn('on_drive(path)?', self.functions[('common', 'directory')])
        for job in (('music_backup', 'run'), ('app_snapshot', 'snapshot'), ('host_snapshot', 'snapshot')):
            self.assertIn('common::expect_drive(', self.functions[job], job)

    def test_the_commands_without_a_check_are_never_run_by_a_timer(self):
        started = set()
        for unit in sorted((spec_sources.PROJECT / 'tests' / 'golden').glob('*/*.service')):
            started.update(re.findall(r'Exec\w+=\S*nekoserver-runtime ([a-z-]+)', unit.read_text(encoding='utf-8')))
        self.assertGreaterEqual(len(started), 15)
        self.assertLessEqual(started, set(self.commands))
        self.assertEqual(started & set(BY_HAND), set())


if __name__ == '__main__':
    unittest.main()
