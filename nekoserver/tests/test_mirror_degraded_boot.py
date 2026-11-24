"""1.0 round T1-R19: a server on one drive that is an mdadm mirror (md0, RAID1 over two
disks, both roles on it) did not start when one disk of the mirror was missing at power-on:
emergency mode, root locked, no ssh, though STORAGE.md says the array "runs on its
remaining drive by itself". Two things went wrong:
- the boot's own scripts (Debian's and Ubuntu's initramfs) put the remaining disk in md0
  and leave it waiting ("inactive"); they start a waiting array only while they wait for
  the disk Linux runs from, and mdadm's own 30-second last resort is never asked for: in
  the initramfs there is no systemd to ask, and after it the disk is already taken. So md0
  never started and its mount timed out after 90 s;
- the two folders of that drive (the apps', the media's) are bind-mounted from fstab lines
  without `nofail`, so their failed mount failed local-fs.target, and the boot with it.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules import server_os, storage  # noqa: E402

APPS = storage.SHARED_FOLDERS['data_ssd']
OLD_LINE = f'{APPS} /srv/nekoserver none bind,x-systemd.requires-mounts-for={storage.ONE_DRIVE} 0 0'


class BindLinesTests(unittest.TestCase):
    def test_a_folder_of_a_late_or_missing_drive_never_stops_the_pc_from_starting(self):
        line = storage.bind_line(APPS, '/srv/nekoserver')
        options = line.split()[3].split(',')
        self.assertIn('nofail', options)
        self.assertIn(f'x-systemd.requires-mounts-for={storage.ONE_DRIVE}', options)

    def test_setup_run_again_puts_its_new_line_in_place_of_the_one_it_wrote_before(self):
        lines = ['UUID=1234 / ext4 defaults 0 1', OLD_LINE]
        entry = storage.bind_line(APPS, '/srv/nekoserver')
        self.assertNotEqual(entry, OLD_LINE)
        self.assertEqual(storage.bind_line_index(lines, '/srv/nekoserver', entry), 1)
        self.assertTrue(storage.own_bind_line(entry.split()))


class MirrorStartTests(unittest.TestCase):
    def setUp(self):
        self.config = {'storage': {'data_ssd': {'uuid': 'aaaa'}, 'media_hdd': {'uuid': 'aaaa'}}}

    def install(self, mdadm, before=None):
        calls, written, removed = [], {}, []
        run = lambda argv, **_: calls.append(argv) or mock.Mock(stdout='dev-x.device\n', returncode=0)
        units = Path('/etc/systemd/system')

        def exists(path):
            return before is not None and path.name in before

        with (
            mock.patch.object(server_os.native_runtime, 'deploy', return_value=Path('/rt/nekoserver-runtime')),
            mock.patch.object(server_os.native_runtime, 'storage_config', return_value={}),
            mock.patch.object(server_os, 'check_units'),
            mock.patch.object(server_os, 'safe_path', side_effect=lambda p: Path(p)),
            mock.patch.object(server_os, 'atomic_json'),
            mock.patch.object(server_os, 'write_units', side_effect=lambda u: written.update(u)),
            mock.patch.object(server_os.shutil, 'which', side_effect=lambda name: mdadm if name == 'mdadm' else None),
            mock.patch.object(server_os, 'own_unit', side_effect=lambda path: exists(path)),
            mock.patch.object(Path, 'unlink', lambda path, missing_ok=False: removed.append(str(path))),
        ):
            server_os.install_drive_return(self.config, Path('/project'), run=run)
        return calls, written, removed

    def test_a_mirror_left_waiting_for_a_missing_disk_is_started_on_the_disk_left(self):
        calls, written, _ = self.install('/usr/sbin/mdadm')
        service = written[server_os.MIRROR_START]
        timer = written[server_os.MIRROR_START_TIMER]
        self.assertTrue(service.startswith('[Unit]\nDescription=NekoServer '))
        self.assertTrue(timer.startswith('[Unit]\nDescription=NekoServer '))
        # mdadm's own way to start the arrays it put together part-way; nothing waiting: no change.
        self.assertIn('ExecStart=-/usr/sbin/mdadm --incremental --run --scan\n', service)
        # Early enough that the drive's mount (90 s) still finds it; started with the boot
        # itself, not after the mounts it is there to let through.
        self.assertIn('OnBootSec=60\n', timer)
        self.assertIn('DefaultDependencies=no\n', timer)
        self.assertIn('WantedBy=sysinit.target\n', timer)
        self.assertIn(['systemctl', 'reenable', server_os.MIRROR_START_TIMER], calls)

    def test_a_pc_without_mdadm_gets_none_and_loses_one_left_from_before(self):
        calls, written, removed = self.install(None, before={server_os.MIRROR_START, server_os.MIRROR_START_TIMER})
        self.assertNotIn(server_os.MIRROR_START, written)
        self.assertIn(['systemctl', 'disable', server_os.MIRROR_START_TIMER], calls)
        self.assertEqual(
            sorted(Path(p).name for p in removed), sorted([server_os.MIRROR_START, server_os.MIRROR_START_TIMER])
        )


if __name__ == '__main__':
    unittest.main()
