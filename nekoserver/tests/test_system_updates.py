"""system_updates.py: what the System tab shows, read from each family's package manager
without changing anything. The apt output below is the shape a real Ubuntu 24.04 lab
machine gave (122 updates, as apt itself counted)."""

import io
import json
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
            [{'name': 'samba', 'from': '4.19', 'to': '4.20', 'security': None}],  # not known
        )
        self.assertEqual(s.host.pacman_updates('linux 6.11.1-1 -> 6.11.2-1\n')[0]['to'], '6.11.2-1')
        # Only queries: no install, remove or refresh of the package lists.
        for argv, *_ in s.host.UPDATE_QUERIES.values():
            self.assertFalse({'install', 'upgrade', 'update', '-Sy', '-Syu', 'refresh'} & set(argv), argv)
        self.assertIn('-s', s.host.UPDATE_QUERIES['debian'][0])  # apt-get only simulates

    def test_security_fixes_are_told_where_the_system_says_and_not_known_elsewhere(self):
        def status(family, answers):
            def run(argv, timeout=600):
                return answers.get(argv[0], (127, ''))

            with mock.patch.object(s.host, 'detect', return_value=mock.Mock()) as detect:
                detect.return_value.name = family
                return s.status(run=run, release={'ID': family}, running='x', modules=Path('/nonexistent'))

        dnf = {
            'dnf': (100, 'openssl-libs.x86_64  1:3.2.4-1.fc42  updates\nbash.x86_64  5.2.37-1.fc42  updates\n'),
            'needs-restarting': (0, ''),
        }
        advisories = 'FEDORA-2026-1a2b3c  Important/Sec.  openssl-libs-1:3.2.4-1.fc42.x86_64\n'
        real = dnf['dnf']

        def dnf_run(argv, timeout=600):
            if argv[:3] == ['dnf', '-q', 'updateinfo']:
                return 0, advisories
            return real if argv[0] == 'dnf' else dnf.get(argv[0], (127, ''))

        with mock.patch.object(s.host, 'detect', return_value=mock.Mock()) as detect:
            detect.return_value.name = 'fedora'
            found = s.status(run=dnf_run, release={}, running='x', modules=Path('/nonexistent'))
        by_name = {u['name']: u['security'] for u in found['updates']}
        self.assertEqual(by_name, {'openssl-libs': True, 'bash': False})
        self.assertNotIn('security_note', found)
        # Arch without arch-audit, and openSUSE: "not known", never "no".
        arch = status('arch', {'checkupdates': (0, 'openssl 3.4.0-1 -> 3.4.1-1\n')})
        self.assertIsNone(arch['updates'][0]['security'])
        self.assertIn('install arch-audit', arch['security_note'])
        audited = status(
            'arch',
            {
                'checkupdates': (0, 'openssl 3.4.0-1 -> 3.4.1-1\nbash 5.2-1 -> 5.3-1\n'),
                'arch-audit': (1, 'Package openssl is affected by CVE-2026-0001. High risk! Update to 3.4.1-1!\n'),
            },
        )
        self.assertEqual([u['security'] for u in audited['updates']], [True, False])
        suse = status(
            'suse',
            {
                'zypper': (
                    0,
                    'S | Repository | Name | Current | Available | Arch\nv | Main | samba | 4.19 | 4.20 | x86_64\n',
                )
            },
        )
        self.assertIsNone(suse['updates'][0]['security'])
        self.assertIn('security patches, not the packages', suse['security_note'])
        # Debian says it in the list itself: no extra query, no note.
        self.assertNotIn('debian', s.host.SECURITY_QUERIES)

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
            self.assertEqual(s.restart_needed('debian', quiet, '6.8.0-49-generic', modules, flag), (s.NOT_REQUIRED, []))
            state, why = s.restart_needed('debian', quiet, '6.8.0-45-generic', modules, flag)
            self.assertEqual(state, s.REQUIRED)
            self.assertIn('replaced by an update', why[0])
            flag.write_text('')
            self.assertEqual(s.restart_needed('debian', quiet, '6.8.0-49-generic', modules, flag)[0], s.REQUIRED)
            self.assertEqual(
                s.restart_needed('suse', lambda argv, timeout=600: (102, ''), '6.8.0-49-generic', modules)[0],
                s.REQUIRED,
            )

    def test_a_newer_debian_kernel_waiting_means_a_restart(self):
        """The review of 0.46.1: Debian keeps the running kernel's modules when a newer
        kernel is installed, and with no update-notifier there is no flag either."""
        with tempfile.TemporaryDirectory() as tmp:
            modules = Path(tmp) / 'modules'
            for name in ('6.1.0-25-amd64', '6.1.0-26-amd64', '6.1.0-27-rt-amd64'):
                (modules / name).mkdir(parents=True)
            flag = Path(tmp) / 'reboot-required'
            quiet = lambda argv, timeout=600: (0, '')  # noqa: E731
            state, why = s.restart_needed('debian', quiet, '6.1.0-25-amd64', modules, flag)
            self.assertEqual(state, s.REQUIRED)
            self.assertIn('6.1.0-26-amd64', why[0])
            # The newest of its kind already runs (another kind's newer one doesn't count).
            self.assertEqual(s.restart_needed('debian', quiet, '6.1.0-26-amd64', modules, flag), (s.NOT_REQUIRED, []))
        self.assertEqual(s.newer_kernel('6.10.2-1-amd64', ['6.9.12-1-amd64', '6.10.11-1-amd64']), '6.10.11-1-amd64')
        self.assertIsNone(s.newer_kernel('weird', ['6.1.0-1-amd64']))

    def test_the_answer_is_never_written_where_the_ssd_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'config.json'
            ssd = Path(tmp) / 'ssd'  # the folder of an SSD that is not mounted
            ssd.mkdir()
            settings.write_text(
                json.dumps({'data_mount': str(ssd), 'storage': {'data_ssd': {'uuid': 'ssd-0001', 'fstype': 'ext4'}}})
            )
            with (
                mock.patch.object(s, 'settings_path', lambda p: Path(p)),
                mock.patch.object(s, 'mount_path', lambda p: Path(p)),
                mock.patch.object(s, 'status', return_value={'count': 0}),
                self.assertRaisesRegex(RuntimeError, 'Storage identity mismatch'),
            ):
                s.check(settings)
            self.assertFalse((ssd / 'backups').exists())
            # From the command line (the timer): one line, exit code 1, no traceback.
            err = io.StringIO()
            with (
                mock.patch.object(sys, 'argv', ['system_updates.py', 'check', '--config', str(settings)]),
                mock.patch.object(s, 'checked_settings', lambda v: v),
                mock.patch.object(s.os, 'geteuid', return_value=0),
                mock.patch.object(s, 'settings_path', lambda p: Path(p)),
                mock.patch.object(s, 'mount_path', lambda p: Path(p)),
                mock.patch.object(sys, 'stderr', err),
            ):
                self.assertEqual(s.main(), 1)
            self.assertIn('System: not checked: Storage identity mismatch', err.getvalue())

    def test_a_restart_check_that_cannot_answer_is_not_known_never_not_needed(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules = Path(tmp) / 'modules'
            (modules / '6.8.0-49-generic').mkdir(parents=True)
            for family, code in (('fedora', 127), ('rhel', 2), ('suse', 127), ('suse', 4)):
                with self.subTest(family=family, code=code):
                    state, why = s.restart_needed(
                        family, lambda argv, timeout=600: (code, ''), '6.8.0-49-generic', modules
                    )
                    self.assertEqual(state, s.UNKNOWN)
                    self.assertTrue(why and ('not installed' in why[0] or 'did not answer' in why[0]), why)
            # Their own "not needed": not needed.
            self.assertEqual(
                s.restart_needed('fedora', lambda argv, timeout=600: (0, ''), '6.8.0-49-generic', modules)[0],
                s.NOT_REQUIRED,
            )
            # A needed restart wins over a check that could not answer.
            state, _ = s.restart_needed('fedora', lambda argv, timeout=600: (127, ''), '6.8.0-1-generic', modules)
            self.assertEqual(state, s.REQUIRED)
            # No kernel folders to compare: not known either.
            self.assertEqual(s.restart_needed('arch', lambda a, t=0: (0, ''), 'x', Path(tmp) / 'none')[0], s.UNKNOWN)

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
