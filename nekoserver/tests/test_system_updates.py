"""system_updates.py: what the System tab shows, read from each family's package manager
without changing anything. The apt output below is the shape a real Ubuntu 24.04 lab
machine gave (122 updates, as apt itself counted)."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import system_updates as s  # noqa: E402

APT = """NOTE: This is only a simulation!
Inst libc6 [2.39-0ubuntu8] (2.39-0ubuntu8.9 Ubuntu:24.04/noble-updates, Ubuntu:24.04/noble-security [amd64])
Inst base-files [13ubuntu10] (13ubuntu10.5 Ubuntu:24.04/noble-updates [amd64])
Inst docker.io [26.1.3-0ubuntu1] (27.5.1-0ubuntu3 Ubuntu:24.04/noble-updates [amd64])
Inst linux-image-6.8.0-49-generic (6.8.0-49.49 Ubuntu:24.04/noble-security [amd64])
Conf libc6 (2.39-0ubuntu8.9 Ubuntu:24.04/noble-updates [amd64])
"""


class ReadTests(unittest.TestCase):
    def test_each_family_is_read_without_changing_anything(self):
        found = s.host.apt_updates(APT)
        self.assertEqual(
            [u['name'] for u in found], ['libc6', 'base-files', 'docker.io', 'linux-image-6.8.0-49-generic']
        )
        self.assertEqual(
            (found[0]['from'], found[0]['to'], found[0]['security']), ('2.39-0ubuntu8', '2.39-0ubuntu8.9', True)
        )
        self.assertEqual(found[3]['from'], '')  # a new kernel package: nothing installed before
        self.assertEqual(
            s.host.dnf_updates('\nkernel.x86_64   6.11.4-301.fc41   updates\nObsoleting Packages\n')[0]['name'],
            'kernel',
        )
        self.assertEqual(
            s.host.zypper_updates(
                'S | Repository | Name | Current | Available | Arch\nv | Main | samba | 4.19 | 4.20 | x86_64\n'
            ),
            [{'name': 'samba', 'from': '4.19', 'to': '4.20', 'security': False}],
        )
        self.assertEqual(s.host.pacman_updates('linux 6.11.1-1 -> 6.11.2-1\n')[0]['to'], '6.11.2-1')
        # Only queries: no install, remove or refresh of the package lists.
        for argv, *_ in s.host.UPDATE_QUERIES.values():
            self.assertFalse({'install', 'upgrade', 'update', '-Sy', '-Syu', 'refresh'} & set(argv), argv)
        self.assertIn('-s', s.host.UPDATE_QUERIES['debian'][0])  # apt-get only simulates

    def test_what_an_update_means_is_said(self):
        found, why = s.pending('debian', run=lambda argv, timeout=600: (0, APT))
        self.assertIsNone(why)
        by_name = {u['name']: u for u in found}
        self.assertTrue(by_name['libc6']['reboot'] and by_name['linux-image-6.8.0-49-generic']['reboot'])
        self.assertFalse(by_name['base-files']['reboot'])
        self.assertEqual(by_name['docker.io']['restarts'], ['Docker (every app restarts)'])
        # Found in the lab: systemd-dev is not systemd, and needs no restart.
        self.assertFalse(s.REBOOT.match('systemd-dev'))
        self.assertTrue(s.REBOOT.match('systemd'))
        # Arch without pacman-contrib: said how to see them.
        found, why = s.pending('arch', run=lambda argv, timeout=600: (127, ''))
        self.assertIsNone(found)
        self.assertIn('pacman-contrib', why)

    def test_a_replaced_kernel_or_the_systems_own_flag_means_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules = Path(tmp) / 'modules'
            (modules / '6.8.0-49-generic').mkdir(parents=True)
            flag = Path(tmp) / 'reboot-required'
            quiet = lambda argv, timeout=600: (0, '')  # noqa: E731
            self.assertEqual(s.restart_needed('debian', quiet, '6.8.0-49-generic', modules, flag), (False, []))
            needed, why = s.restart_needed('debian', quiet, '6.8.0-45-generic', modules, flag)
            self.assertTrue(needed)
            self.assertIn('replaced by an update', why[0])
            flag.write_text('')
            self.assertEqual(len(s.restart_needed('debian', quiet, '6.8.0-49-generic', modules, flag)[1]), 1)
            self.assertTrue(
                s.restart_needed('suse', lambda argv, timeout=600: (102, ''), '6.8.0-49-generic', modules)[0]
            )

    def test_the_status_counts_every_update_and_lists_the_first_ones(self):
        many = ''.join(f'Inst p{n} [1] (2 Ubuntu:24.04/noble-updates [amd64])\n' for n in range(400))
        with mock.patch.object(s.host, 'detect', return_value=mock.Mock(name='debian')) as detect:
            detect.return_value.name = 'debian'
            result = s.status(
                run=lambda argv, timeout=600: (0, many),
                release={'PRETTY_NAME': 'Ubuntu 24.04 LTS'},
                running='x',
                modules=Path('/nonexistent'),
            )
        self.assertEqual((result['count'], len(result['updates'])), (400, s.SHOWN))
        self.assertEqual(result['system'], 'Ubuntu 24.04 LTS')


if __name__ == '__main__':
    unittest.main()
