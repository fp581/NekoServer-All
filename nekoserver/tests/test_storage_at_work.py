"""T1-6 (1.0.1 VM drill): every move from the Drives page ended after 200 s with
"nekoserver-storage.service is at work (a backup or another job)". The job's own oneshot
is "activating" while it runs, and a LAN gateway waiting for its address (the T1-R18 state)
counted as a job at work too. A Drives job never waits for its own units, and only the
services that write NekoServer's data hold a move up (migrate.QUIET names the others)."""

import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import migrate  # noqa: E402
import storage_jobs  # noqa: E402

# Every other service of NekoServer's writes its data, or changes the drives or the apps,
# while it is at work: a move waits for it.
WRITE = {
    'nekoserver-app-backup.service',
    'nekoserver-app-resume.service',
    'nekoserver-cards.service',
    'nekoserver-drive-return.service',
    'nekoserver-favourite-shows.service',
    'nekoserver-host-backup.service',
    'nekoserver-host-resume.service',
    'nekoserver-immich-db-backup.service',
    'nekoserver-immich-resume.service',
    'nekoserver-immich-snapshot-resume.service',
    'nekoserver-maintenance.service',
    'nekoserver-maintenance-resume.service',
    'nekoserver-maintenance-retry.service',
    'nekoserver-media-folders.service',
    'nekoserver-mirror-start.service',
    'nekoserver-music-backup.service',
    'nekoserver-nextcloud-resume.service',
    'nekoserver-photo-backup.service',
    'nekoserver-photo-preservation.service',
    'nekoserver-storage.service',
    'nekoserver-storage-resume.service',
    'nekoserver-update-now@.service',
    'nekoserver-updates.service',
    'nekoserver-upgrade@.service',
    'nekoserver-upgrade-resume.service',
}


def unit_names():
    """Every nekoserver-*.service the code names (Python, Rust, shared.json), tests aside."""
    found = set()
    sources = [*ROOT.glob('*.py'), *(ROOT / 'modules').glob('*.py'), *(ROOT / 'runtime/src').rglob('*.rs')]
    for path in [*sources, ROOT / 'runtime/src/shared.json']:
        text = path.read_text(encoding='utf-8')
        if path.suffix == '.rs':
            text = text.split('#[cfg(test)]', 1)[0]
        found.update(re.findall(r'nekoserver-[a-z0-9-]+@?\.service', text))
    return found


class Systemd:
    """systemctl as a move sees it: `at_work` activating, `running` active, and the
    commands it was given."""

    def __init__(self, at_work=(), running=()):
        self.at_work, self.running, self.calls = list(at_work), list(running), []

    def units(self, states='active'):
        shown = self.running if 'active' in states.split(',') else []
        if 'activating' in states:
            shown = shown + self.at_work
        return list(shown)

    def run(self, argv, timeout=None, check=True, capture=True):
        self.calls.append(argv)
        if argv[:2] == ['systemctl', 'stop']:
            for listed in (self.running, self.at_work):
                if argv[2] in listed:
                    listed.remove(argv[2])
        return mock.Mock(returncode=1, stdout='')


class AtWorkTests(unittest.TestCase):
    def stop(self, systemd, own=None):
        with (
            mock.patch.object(migrate, 'nekoserver_units', systemd.units),
            mock.patch.object(migrate, 'run', systemd.run),
        ):
            own = storage_jobs.left_alone() if own is None else own
            return migrate.stop_writers(note=lambda units: None, sleep=lambda seconds: None, own=own)

    def test_a_drives_job_never_waits_for_its_own_unit_nor_a_gateway_waiting_for_its_address(self):
        systemd = Systemd(
            at_work=['nekoserver-storage.service', 'nekoserver-gateway-lan.service'],
            running=['nekoserver-immich-resume.service'],
        )
        stopped = self.stop(systemd)
        self.assertEqual(stopped, ['nekoserver-immich-resume.service', 'nekoserver-gateway-lan.service'])
        self.assertNotIn(['systemctl', 'stop', 'nekoserver-storage.service'], systemd.calls)
        self.assertEqual(systemd.at_work, ['nekoserver-storage.service'])  # the gateway was stopped

    def test_the_dashboard_keeps_showing_a_drives_job_while_the_apps_are_stopped(self):
        page = ['nekoserver-dashboard.service', 'nekoserver-dashboard-collect.service']
        # The control service and the other quiet ones are stopped: an app button pressed
        # meanwhile (restart, update) would start an app between the last copy and the switch.
        others = ['nekoserver-control.service', 'nekoserver-system-check.service', 'nekoserver-cards.service']
        systemd = Systemd(at_work=['nekoserver-storage.service'], running=[*page, *others])
        self.assertEqual(self.stop(systemd), others)
        self.assertEqual(systemd.running, page)
        # A whole-drive move stops them too: the drive they run from goes away.
        self.assertEqual(self.stop(Systemd(running=list(page)), own=()), page)
        self.assertFalse({u for u in storage_jobs.PAGE_UNITS if u.endswith('.service')} - migrate.QUIET)

    def test_the_finish_after_docker_never_waits_for_itself(self):
        self.stop(Systemd(at_work=['nekoserver-storage-finish.service']))

    def test_a_backup_at_work_still_holds_the_move_up(self):
        for unit in ('nekoserver-app-backup.service', 'nekoserver-something-new.service'):
            systemd = Systemd(at_work=['nekoserver-storage.service', unit])
            busy = re.escape(unit) + ' is at work'
            with self.assertRaisesRegex(RuntimeError, busy):
                self.stop(systemd)

    def test_a_whole_drive_move_still_waits_for_a_drives_job(self):
        systemd = Systemd(at_work=['nekoserver-storage.service'])
        with self.assertRaisesRegex(RuntimeError, 'nekoserver-storage.service is at work'):
            self.stop(systemd, own=())

    def test_every_unit_of_nekoserver_is_either_quiet_or_waited_for(self):
        named = unit_names() - {'nekoserver-foo.service', 'nekoserver-no-such-resume.service'}
        self.assertEqual(sorted(named - migrate.QUIET - WRITE), [])
        self.assertEqual(migrate.QUIET & WRITE, set())
        self.assertFalse(set(storage_jobs.OWN_UNITS) & migrate.QUIET)


if __name__ == '__main__':
    unittest.main()
