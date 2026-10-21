import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import capabilities
import host


def release(folder, text):
    path = Path(folder) / 'etc/os-release'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


class FakePacman:
    """Answers pacman -Si/-Q/-S from sets of offered and installed packages."""
    def __init__(self, offered=(), installed=(), fail=False):
        self.offered, self.installed, self.fail, self.calls = set(offered), set(installed), fail, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == '-Si':
            code = 0 if argv[2] in self.offered else 1
        elif argv[1] == '-Q':
            code = 0 if argv[2] in self.installed else 1
        else:
            code = 1 if self.fail else 0
            if not self.fail:
                self.installed.update(argv[4:])
        return subprocess.CompletedProcess(argv, code, '', 'error: target not found' if code else '')


class DetectionTest(unittest.TestCase):
    def test_arch_and_nyarch_and_derivatives_are_arch(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in ('ID=arch\n', 'ID="nyarch"\n', 'ID=endeavouros\nID_LIKE=arch\n'):
                self.assertIs(host.detect(release(tmp, text)), host.Arch, text)

    def test_other_systems_are_refused_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = release(tmp, 'ID=fedora\nPRETTY_NAME="Fedora Linux 41"\n')
            self.assertIsNone(host.detect(path))
            self.assertFalse(host.supported(path))
            with self.assertRaisesRegex(host.Unsupported, 'Fedora Linux 41 is not supported yet'):
                host.current(path=path)
            self.assertIs(host.family_or_default(path), host.Arch)

    def test_missing_os_release_is_unsupported(self):
        self.assertEqual(host.os_release('/nonexistent/os-release'), {})
        self.assertIsNone(host.detect('/nonexistent/os-release'))

    def test_debian_ubuntu_and_derivatives_are_debian(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in ('ID=debian\n', 'ID=ubuntu\nID_LIKE=debian\n', 'ID=linuxmint\nID_LIKE="ubuntu debian"\n',
                         'ID=Ubuntu\n'):
                self.assertIs(host.detect(release(tmp, text)), host.Debian, text)

    def test_service_names(self):
        self.assertEqual(host.Arch.service('samba'), 'smb.service')
        self.assertEqual(host.Debian.service('samba'), 'smbd.service')
        for family in host.FAMILIES:
            self.assertEqual(family.service('tailscale'), 'tailscaled.service')
        self.assertEqual(host.Arch.service('docker'), 'docker.service')


class InstallTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch('host.shutil.which', return_value='/usr/bin/pacman')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_installs_only_what_is_missing_without_upgrading(self):
        pacman = FakePacman(offered={'a', 'b'}, installed={'a'})
        self.assertEqual(host.Arch(pacman).install(['a', 'b']), ['b'])
        installs = [argv for argv in pacman.calls if argv[1] == '-S']
        self.assertEqual(installs, [['pacman', '-S', '--needed', '--noconfirm', 'b']])
        self.assertFalse(any('-Syu' in argv or '-Sy' in argv for argv in pacman.calls))

    def test_nothing_missing_runs_no_install(self):
        pacman = FakePacman(installed={'a'})
        self.assertEqual(host.Arch(pacman).install(['a']), [])
        self.assertFalse(any(argv[1] == '-S' for argv in pacman.calls))

    def test_failure_names_the_packages_and_the_reason(self):
        with self.assertRaisesRegex(RuntimeError, r'Installing b failed: error: target not found'):
            host.Arch(FakePacman(fail=True)).install(['b'])

    def test_available(self):
        family = host.Arch(FakePacman(offered={'caddy'}))
        self.assertTrue(family.available('caddy'))
        self.assertFalse(family.available('missing'))

    def test_missing_package_manager(self):
        with mock.patch('host.shutil.which', return_value=None):
            family = host.Arch(FakePacman())
            self.assertFalse(family.available('caddy'))
            with self.assertRaisesRegex(RuntimeError, 'pacman.*not found'):
                family.install(['caddy'])


class FakeApt:
    """Answers apt-get, apt-cache, dpkg-query and systemctl from sets of offered and
    installed packages."""
    def __init__(self, offered=(), installed=(), fail=False):
        self.offered, self.installed, self.fail, self.calls = set(offered), set(installed), fail, []
        self.policy_during_install = None

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        out, code = '', 0
        if argv[0] == 'apt-cache':
            out = 'Candidate: 1.0\n' if argv[2] in self.offered else ('Candidate: (none)\n' if argv[2] == 'gone' else '')
        elif argv[0] == 'dpkg-query':
            code = 0 if argv[3] in self.installed else 1
            out = 'install ok installed' if not code else ''
        elif argv[:2] == ['apt-get', 'install']:
            self.policy_during_install = self.root and (self.root / 'usr/sbin/policy-rc.d').read_text()
            code = 100 if self.fail else 0
            if not self.fail:
                self.installed.update(argv[5:])
        return subprocess.CompletedProcess(argv, code, out, 'E: Unable to locate package' if code else '')

    root = None


class DebianTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for path in ('usr/sbin', 'etc/apt/sources.list.d', 'etc/samba'):
            (self.root / path).mkdir(parents=True)
        patcher = mock.patch('host.shutil.which', return_value='/usr/bin/apt-get')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keys = []

    def family(self, apt, release=None):
        apt.root = self.root
        family = host.Debian(apt, release or {'ID': 'ubuntu', 'ID_LIKE': 'debian', 'VERSION_CODENAME': 'noble',
                                              'UBUNTU_CODENAME': 'noble'},
                             fetch=lambda url: self.keys.append(url) or b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n')
        family.root = self.root
        return family

    def test_refreshes_lists_once_and_installs_only_what_is_missing(self):
        apt = FakeApt(offered={'samba', 'hdparm'}, installed={'hdparm'})
        family = self.family(apt)
        self.assertTrue(family.available('samba'))
        self.assertEqual(family.install(['hdparm', 'samba']), ['samba'])
        self.assertEqual(apt.calls.count(['apt-get', 'update']), 1)
        installs = [argv for argv in apt.calls if argv[:2] == ['apt-get', 'install']]
        self.assertEqual(installs, [['apt-get', 'install', '-y', '-o', 'Dpkg::Options::=--force-confold', 'samba']])
        self.assertFalse(any('upgrade' in argv or 'dist-upgrade' in argv for argv in apt.calls))

    def test_packages_do_not_start_their_services_and_are_left_off(self):
        apt = FakeApt()
        self.family(apt).install(['samba', 'caddy'])
        self.assertIn('exit 101', apt.policy_during_install)
        self.assertFalse((self.root / 'usr/sbin/policy-rc.d').exists())
        disabled = [argv for argv in apt.calls if argv[:2] == ['systemctl', 'disable']]
        self.assertEqual(len(disabled), 1)
        for unit in ('smbd.service', 'nmbd.service', 'caddy.service'):
            self.assertIn(unit, disabled[0])

    def test_someone_elses_policy_is_kept(self):
        (self.root / 'usr/sbin/policy-rc.d').write_text('#!/bin/sh\nexit 0\n')
        self.family(FakeApt()).install(['hdparm'])
        self.assertEqual((self.root / 'usr/sbin/policy-rc.d').read_text(), '#!/bin/sh\nexit 0\n')

    def test_docker_comes_from_dockers_repository(self):
        apt = FakeApt()
        self.assertEqual(self.family(apt).install(['docker', 'docker-compose']), ['docker', 'docker-compose'])
        self.assertEqual(self.keys, ['https://download.docker.com/linux/ubuntu/gpg'])
        listing = (self.root / 'etc/apt/sources.list.d/docker.list').read_text()
        self.assertEqual(listing, 'deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] '
                                  'https://download.docker.com/linux/ubuntu noble stable\n')
        self.assertTrue((self.root / 'etc/apt/keyrings/docker.asc').is_file())
        for name in ('docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-compose-plugin'):
            self.assertIn(name, apt.installed)
        self.assertNotIn('docker.io', apt.installed)
        disabled = next(argv for argv in apt.calls if argv[:2] == ['systemctl', 'disable'])
        self.assertIn('docker.service', disabled)
        self.assertIn('docker.socket', disabled)

    def test_debians_own_docker_is_never_mixed_in(self):
        apt = FakeApt(installed={'docker.io'})
        with self.assertRaisesRegex(RuntimeError, r'own Docker packages installed \(docker.io\)'):
            self.family(apt).install(['docker'])
        self.assertFalse(any(argv[:2] == ['apt-get', 'install'] for argv in apt.calls))
        self.assertFalse((self.root / 'etc/apt/sources.list.d/docker.list').exists())

    def test_an_existing_repository_list_is_left_alone(self):
        (self.root / 'etc/apt/sources.list.d/tailscale.list').write_text('# mine\n')
        self.family(FakeApt()).install(['tailscale'])
        self.assertEqual(self.keys, [])
        self.assertEqual((self.root / 'etc/apt/sources.list.d/tailscale.list').read_text(), '# mine\n')

    def test_tailscale_on_debian_uses_the_debian_codename(self):
        self.family(FakeApt(), {'ID': 'debian', 'VERSION_CODENAME': 'bookworm'}).install(['tailscale'])
        self.assertEqual(self.keys, ['https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg'])
        self.assertIn('https://pkgs.tailscale.com/stable/debian bookworm main',
                      (self.root / 'etc/apt/sources.list.d/tailscale.list').read_text())

    def test_package_names(self):
        ubuntu = self.family(FakeApt())
        debian = self.family(FakeApt(), {'ID': 'debian', 'VERSION_CODENAME': 'bookworm'})
        self.assertEqual(debian.names('libva-utils'), ['vainfo'])
        self.assertEqual(debian.names('intel-media-driver'), ['intel-media-va-driver'])
        self.assertEqual(debian.names('firefox'), ['firefox-esr'])
        self.assertEqual(ubuntu.names('firefox'), ['firefox'])
        self.assertEqual(debian.names('samba'), ['samba'])

    def test_packaged_example_samba_settings_are_moved_aside(self):
        apt = FakeApt()
        family = self.family(apt)
        real_install = family._install

        def install(names, timeout):
            (self.root / 'etc/samba/smb.conf').write_text('# example\n')
            return real_install(names, timeout)
        family._install = install
        family.install(['samba'])
        self.assertFalse((self.root / 'etc/samba/smb.conf').exists())
        self.assertEqual((self.root / 'etc/samba/smb.conf.packaged').read_text(), '# example\n')

    def test_existing_samba_settings_are_never_moved(self):
        (self.root / 'etc/samba/smb.conf').write_text('# mine\n')
        self.family(FakeApt()).install(['samba'])
        self.assertEqual((self.root / 'etc/samba/smb.conf').read_text(), '# mine\n')

    def test_unavailable_packages(self):
        family = self.family(FakeApt(offered={'vainfo'}))
        self.assertTrue(family.available('libva-utils'))
        self.assertFalse(family.available('openrgb'))
        self.assertFalse(family.available('gone'))

    def test_failure_names_the_reason(self):
        with self.assertRaisesRegex(RuntimeError, 'Installing samba failed: E: Unable to locate package'):
            self.family(FakeApt(fail=True)).install(['samba'])
        self.assertFalse((self.root / 'usr/sbin/policy-rc.d').exists())

    def test_pending_restart(self):
        self.assertFalse(host.Debian.reboot_pending(self.root))
        (self.root / 'run').mkdir()
        (self.root / 'run/reboot-required').write_text('')
        self.assertTrue(host.Debian.reboot_pending(self.root))
        self.assertFalse(host.Arch.reboot_pending(self.root))

    def test_keys_only_over_https(self):
        with self.assertRaisesRegex(RuntimeError, 'https'):
            host._fetch('http://example.com/key')


class BootstrapTest(unittest.TestCase):
    def test_installs_every_app_package(self):
        installed = []
        family = mock.Mock(install=installed.extend)
        host.bootstrap(family, log=lambda line: None)
        self.assertEqual(installed, list(host.APP_PACKAGES))

    def test_command_line(self):
        self.assertEqual(host.main([]), 2)
        with mock.patch('host.bootstrap', side_effect=RuntimeError('no')):
            self.assertEqual(host.main(['bootstrap']), 1)


class NoDirectPackageManagerTest(unittest.TestCase):
    def test_only_the_adapter_calls_pacman(self):
        root = Path(__file__).resolve().parents[1]
        offenders = [str(path.relative_to(root)) for path in [*root.glob('*.py'), *root.glob('modules/*.py')]
                     if path.name != 'host.py' and any(word in path.read_text()
                                                       for word in ("'pacman'", "'apt-get'", "'dpkg'"))]
        self.assertEqual(offenders, [])


class CapabilitiesTest(unittest.TestCase):
    def fake(self, systemd='systemd 256 (256.7-1-arch)', docker='27.3.1', target='multi-user.target'):
        def run(argv, **kwargs):
            out = {'--version': systemd, 'version': docker, 'get-default': target}.get(argv[1], '')
            if argv[:2] == ['docker', 'compose']:
                out = '2.29.7'
            return subprocess.CompletedProcess(argv, 0 if out else 1, out, '')
        return run

    def rows(self, tmp, text='ID=arch\n', machine='x86_64', python=(3, 13), **kwargs):
        release(tmp, text)
        return capabilities.detect(self.fake(**kwargs), root=tmp, machine=machine, python=python,
                                   which=lambda name: '/usr/bin/' + name if name in ('systemctl', 'docker', 'nft') else None)

    def test_arch_server_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(Path(tmp) / 'sys/fs/cgroup')
            (Path(tmp) / 'sys/fs/cgroup/cgroup.controllers').write_text('cpu memory\n')
            rows = self.rows(tmp)
            self.assertEqual(capabilities.missing(rows), [])
            capabilities.require(rows)
            self.assertEqual(capabilities.value(rows, 'systemd'), '256')
            self.assertEqual(capabilities.value(rows, 'docker'), '27.3.1')
            self.assertEqual(capabilities.value(rows, 'compose'), '2.29.7')
            self.assertEqual(capabilities.value(rows, 'firewall'), 'off')
            self.assertEqual(capabilities.value(rows, 'desktop'), 'no (server)')
            self.assertEqual(capabilities.value(rows, 'selinux'), 'off')

    def test_pending_restart_stops_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'run').mkdir()
            (Path(tmp) / 'run/reboot-required').write_text('')
            rows = self.rows(tmp, text='ID=debian\n')
            self.assertEqual(capabilities.value(rows, 'family'), 'Debian / Ubuntu')
            self.assertIn('No restart pending', ' '.join(capabilities.missing(rows)))

    def test_switched_on_firewall_is_a_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'etc/ufw').mkdir(parents=True)
            (Path(tmp) / 'etc/ufw/ufw.conf').write_text('ENABLED=yes\n')
            rows = self.rows(tmp)
            self.assertIn('ufw is on', capabilities.value(rows, 'firewall'))
            self.assertEqual(capabilities.missing(rows), [])

    def test_fixed_requirements_stop_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self.rows(tmp, text='ID=fedora\n', machine='aarch64', python=(3, 10), systemd='systemd 245')
            problems = ' '.join(capabilities.missing(rows))
            for words in ('x86-64', 'Python 3.11', 'systemd 247', 'Supported Linux family'):
                self.assertIn(words, problems)
            with self.assertRaisesRegex(RuntimeError, 'cannot run NekoServer setup'):
                capabilities.require(rows)

    def test_security_modules_and_desktop_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path, text in (('sys/fs/selinux/enforce', '1'), ('sys/module/apparmor/parameters/enabled', 'Y')):
                (Path(tmp) / path).parent.mkdir(parents=True)
                (Path(tmp) / path).write_text(text)
            rows = self.rows(tmp, target='graphical.target', docker='')
            self.assertEqual(capabilities.value(rows, 'selinux'), 'enforcing')
            self.assertEqual(capabilities.value(rows, 'apparmor'), 'on')
            self.assertEqual(capabilities.value(rows, 'desktop'), 'yes')
            self.assertIn('setup installs it', capabilities.value(rows, 'docker'))

    def test_report_marks_missing_requirements(self):
        lines = []
        capabilities.report([capabilities.row('x', 'Thing', 'none', False, True)], lines.append)
        self.assertEqual(lines[1], '  MISSING Thing: none')

    def test_this_sandbox_is_detected(self):
        # Whatever runs the tests: detection never crashes and always fills every row.
        rows = capabilities.detect()
        self.assertEqual(len({row['name'] for row in rows}), len(rows))


if __name__ == '__main__':
    unittest.main()
