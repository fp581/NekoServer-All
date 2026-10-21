import hashlib
import json
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


# Units each package brings, as Debian's packages enable them.
UNITS = {'samba': ('smbd.service', 'nmbd.service'), 'caddy': ('caddy.service',),
         'docker-ce': ('docker.service', 'docker.socket'), 'tailscale': ('tailscaled.service',)}


class FakeApt:
    """apt-get, apt-cache, dpkg-query and systemctl over sets of offered and installed
    packages and a table of systemd units. Installing can fail after some packages."""
    def __init__(self, offered=(), installed=(), fail_after=None, root=None):
        self.offered, self.installed, self.calls, self.envs = set(offered), set(installed), [], []
        self.fail_after, self.root, self.units = fail_after, root, {}
        self.policy_during_install = None
        self.disable_fails = False
        self.stays_enabled = set()
        self.on_install = None

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        self.envs.append(kwargs.get('env', {}))
        handler = {'apt-cache': self.cache, 'dpkg-query': self.dpkg, 'systemctl': self.systemctl}.get(argv[0])
        if handler:
            return handler(argv)
        if argv[:2] == ['apt-get', 'install']:
            return self.install(argv)
        if argv[0].endswith('policy-rc.d'):
            return subprocess.run(argv, capture_output=True, text=True, check=False)
        return subprocess.CompletedProcess(argv, 0, '', '')

    def cache(self, argv):
        out = 'Candidate: 1.0\n' if argv[2] in self.offered else ('Candidate: (none)\n' if argv[2] == 'gone' else '')
        return subprocess.CompletedProcess(argv, 0, out, '')

    def dpkg(self, argv):
        ok = argv[3] in self.installed
        return subprocess.CompletedProcess(argv, 0 if ok else 1, 'install ok installed' if ok else '', '')

    def install(self, argv):
        policy = self.root / 'usr/sbin/policy-rc.d'
        self.policy_during_install = policy.read_text() if policy.exists() else None
        names = argv[5:]
        done = names if self.fail_after is None else names[:self.fail_after]
        for name in done:
            self.installed.add(name)
            for unit in UNITS.get(name, ()):
                self.units[unit] = {'LoadState': 'loaded', 'UnitFileState': 'enabled', 'ActiveState': 'inactive'}
        if self.on_install:
            self.on_install(done)
        failed = len(done) < len(names)
        return subprocess.CompletedProcess(argv, 100 if failed else 0, '', 'E: Sub-process /usr/bin/dpkg returned an error code (1)' if failed else '')

    def systemctl(self, argv):
        if argv[1] == 'show':
            units = [a for a in argv[2:] if not a.startswith('--')]
            blocks = []
            for unit in units:
                state = self.units.get(unit, {'LoadState': 'not-found', 'UnitFileState': '', 'ActiveState': 'inactive'})
                blocks.append('Id=' + unit + '\n' + '\n'.join(k + '=' + v for k, v in state.items()))
            return subprocess.CompletedProcess(argv, 0, '\n\n'.join(blocks) + '\n', '')
        if argv[1] == 'disable':
            if self.disable_fails:
                return subprocess.CompletedProcess(argv, 1, '', 'Failed to disable unit: Access denied')
            for unit in (a for a in argv[2:] if not a.startswith('--')):
                if unit not in self.stays_enabled:
                    self.units[unit]['UnitFileState'] = 'disabled'
                self.units[unit]['ActiveState'] = 'inactive'
        return subprocess.CompletedProcess(argv, 0, '', '')

    def enabled(self):
        return sorted(u for u, s in self.units.items() if s['UnitFileState'] == 'enabled')


KEY = b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n'


class DebianTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for path in ('usr/sbin', 'etc/apt/sources.list.d', 'etc/samba', 'var/lib/ucf'):
            (self.root / path).mkdir(parents=True)
        patcher = mock.patch('host.shutil.which', return_value='/usr/bin/apt-get')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keys = []

    def family(self, apt=None, release=None):
        apt = apt or FakeApt()
        apt.root = self.root
        family = host.Debian(apt, release or {'ID': 'ubuntu', 'ID_LIKE': 'debian', 'VERSION_CODENAME': 'noble',
                                              'UBUNTU_CODENAME': 'noble'},
                             fetch=lambda url: self.keys.append(url) or KEY)
        family.root = self.root
        return family

    def file(self, path, text=''):
        target = self.root / path.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        return target

    def journal(self):
        return self.root / host.Debian.journal.lstrip('/')

    # Installing

    def test_refreshes_lists_once_and_installs_only_what_is_missing(self):
        apt = FakeApt(offered={'samba', 'hdparm'}, installed={'hdparm'})
        family = self.family(apt)
        self.assertTrue(family.available('samba'))
        self.assertEqual(family.install(['hdparm', 'samba']), ['samba'])
        self.assertEqual(apt.calls.count(['apt-get', 'update']), 1)
        installs = [argv for argv in apt.calls if argv[:2] == ['apt-get', 'install']]
        self.assertEqual(installs, [['apt-get', 'install', '-y', '-o', 'Dpkg::Options::=--force-confold', 'samba']])
        self.assertFalse(any('upgrade' in argv or 'dist-upgrade' in argv for argv in apt.calls))
        self.assertFalse(self.journal().exists())

    def test_output_that_is_read_is_in_english(self):
        apt = FakeApt(offered={'caddy'})
        self.family(apt).available('caddy')
        for argv, env in zip(apt.calls, apt.envs):
            self.assertEqual(env.get('LC_ALL'), 'C', argv)

    def test_packages_do_not_start_their_services_and_are_left_off(self):
        apt = FakeApt()
        self.family(apt).install(['samba', 'caddy'])
        self.assertIn('exit 101', apt.policy_during_install)
        self.assertFalse((self.root / 'usr/sbin/policy-rc.d').exists())
        self.assertEqual(apt.enabled(), [])
        self.assertEqual(sorted(apt.units), ['caddy.service', 'nmbd.service', 'smbd.service'])

    def test_package_names(self):
        ubuntu = self.family()
        debian = self.family(release={'ID': 'debian', 'VERSION_CODENAME': 'bookworm'})
        self.assertEqual(debian.names('libva-utils'), ['vainfo'])
        self.assertEqual(debian.names('intel-media-driver'), ['intel-media-va-driver'])
        self.assertEqual(debian.names('firefox'), ['firefox-esr'])
        self.assertEqual(ubuntu.names('firefox'), ['firefox'])
        self.assertEqual(debian.names('samba'), ['samba'])

    def test_unavailable_packages(self):
        family = self.family(FakeApt(offered={'vainfo'}))
        self.assertTrue(family.available('libva-utils'))
        self.assertFalse(family.available('openrgb'))
        self.assertFalse(family.available('gone'))

    def test_pending_restart(self):
        self.assertFalse(host.Debian.reboot_pending(self.root))
        self.file('run/reboot-required')
        self.assertTrue(host.Debian.reboot_pending(self.root))
        self.assertFalse(host.Arch.reboot_pending(self.root))

    # A failed or interrupted installation

    def test_partly_failed_installation_is_tidied_before_the_error(self):
        apt = FakeApt(fail_after=1)
        family = self.family(apt)
        apt.on_install = lambda done: 'samba' in done and self.file('etc/samba/smb.conf', '# example\n')
        with self.assertRaisesRegex(RuntimeError, 'Installing samba, caddy failed: E: Sub-process'):
            family.install(['samba', 'caddy'])
        self.assertIn('samba', apt.installed)
        self.assertEqual(apt.enabled(), [])
        self.assertFalse((self.root / 'etc/samba/smb.conf').exists())
        self.assertEqual((self.root / 'etc/samba/smb.conf.packaged').read_text(), '# example\n')
        self.assertFalse(self.journal().exists())

    def test_retry_finishes_an_installation_cut_off_earlier(self):
        # An earlier run was cut off after Samba installed: its record is all that's left.
        apt = FakeApt(installed={'samba'})
        apt.units['smbd.service'] = {'LoadState': 'loaded', 'UnitFileState': 'enabled', 'ActiveState': 'active'}
        self.file('etc/samba/smb.conf', '# example\n')
        self.file(host.Debian.journal, json.dumps({'format': 'neko-package-install-v1', 'packages': ['samba'],
                                                    'before': {'samba_config_existed': False}}))
        self.file('usr/sbin/policy-rc.d', host.OUR_POLICY)
        self.assertEqual(self.family(apt).install(['samba']), [])
        self.assertEqual(apt.enabled(), [])
        self.assertEqual(apt.units['smbd.service']['ActiveState'], 'inactive')
        self.assertTrue((self.root / 'etc/samba/smb.conf.packaged').exists())
        self.assertFalse(self.journal().exists())
        self.assertFalse((self.root / 'usr/sbin/policy-rc.d').exists())

    def test_tidying_that_fails_is_retried(self):
        apt = FakeApt()
        apt.disable_fails = True
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'Could not switch off .*Access denied'):
            family.install(['caddy'])
        self.assertTrue(self.journal().exists())
        apt.disable_fails = False
        self.family(apt).reconcile()
        self.assertEqual(apt.enabled(), [])
        self.assertFalse(self.journal().exists())

    def test_a_service_that_stays_enabled_is_an_error(self):
        apt = FakeApt()
        apt.stays_enabled = {'docker.socket'}
        with self.assertRaisesRegex(RuntimeError, 'Could not switch off docker.socket'):
            self.family(apt).install(['docker'])

    def test_units_that_are_not_installed_or_masked_are_left_alone(self):
        apt = FakeApt()
        apt.units['samba-ad-dc.service'] = {'LoadState': 'masked', 'UnitFileState': 'masked', 'ActiveState': 'inactive'}
        self.family(apt).install(['samba'])
        disabled = next(argv for argv in apt.calls if argv[:2] == ['systemctl', 'disable'])
        self.assertNotIn('samba-ad-dc.service', disabled)

    def test_unreadable_record_stops(self):
        self.file(host.Debian.journal, '{broken')
        with self.assertRaisesRegex(RuntimeError, 'Unreadable record'):
            self.family().install(['hdparm'])

    # Samba's packaged example

    def test_existing_samba_settings_are_never_moved(self):
        self.file('etc/samba/smb.conf', '# mine\n')
        self.family().install(['samba'])
        self.assertEqual((self.root / 'etc/samba/smb.conf').read_text(), '# mine\n')

    def test_changed_example_is_someones_settings(self):
        apt = FakeApt()

        def written(done):
            self.file('etc/samba/smb.conf', '# example\n')
            self.file('var/lib/ucf/hashfile', hashlib.md5(b'# example\n').hexdigest() + '  /etc/samba/smb.conf\n')
            self.file('etc/samba/smb.conf', '# example, edited\n')
        apt.on_install = written
        self.family(apt).install(['samba'])
        self.assertEqual((self.root / 'etc/samba/smb.conf').read_text(), '# example, edited\n')

    def test_unchanged_example_matches_its_record(self):
        apt = FakeApt()

        def written(done):
            self.file('etc/samba/smb.conf', '# example\n')
            self.file('var/lib/ucf/hashfile', hashlib.md5(b'# example\n').hexdigest() + '  /etc/samba/smb.conf\n')
        apt.on_install = written
        self.family(apt).install(['samba'])
        self.assertTrue((self.root / 'etc/samba/smb.conf.packaged').exists())

    # Starting services while installing

    def test_someone_elses_refusing_policy_is_kept(self):
        policy = self.file('usr/sbin/policy-rc.d', '#!/bin/sh\nexit 101\n')
        policy.chmod(0o755)
        self.family().install(['caddy'])
        self.assertEqual(policy.read_text(), '#!/bin/sh\nexit 101\n')

    def test_someone_elses_permissive_policy_stops_before_installing(self):
        policy = self.file('usr/sbin/policy-rc.d', '#!/bin/sh\nexit 0\n')
        policy.chmod(0o755)
        apt = FakeApt()
        with self.assertRaisesRegex(RuntimeError, 'would let docker.service, docker.socket start'):
            self.family(apt).install(['docker'])
        self.assertFalse(any(argv[:2] == ['apt-get', 'install'] for argv in apt.calls))
        self.assertEqual(policy.read_text(), '#!/bin/sh\nexit 0\n')

    def test_permissive_policy_is_fine_for_packages_without_services(self):
        policy = self.file('usr/sbin/policy-rc.d', '#!/bin/sh\nexit 0\n')
        policy.chmod(0o755)
        self.assertEqual(self.family().install(['hdparm']), ['hdparm'])

    # Repositories

    def test_docker_comes_from_dockers_repository(self):
        apt = FakeApt()
        self.assertEqual(self.family(apt).install(['docker', 'docker-compose']), ['docker', 'docker-compose'])
        self.assertEqual(self.keys, ['https://download.docker.com/linux/ubuntu/gpg'])
        listing = (self.root / 'etc/apt/sources.list.d/docker.list').read_text()
        self.assertEqual(listing, '# Managed by NekoServer\ndeb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] '
                                  'https://download.docker.com/linux/ubuntu noble stable\n')
        self.assertEqual((self.root / 'etc/apt/keyrings/docker.asc').read_bytes(), KEY)
        for name in ('docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-compose-plugin'):
            self.assertIn(name, apt.installed)
        self.assertNotIn('docker.io', apt.installed)
        self.assertEqual(apt.enabled(), [])

    def test_a_running_docker_only_gets_its_missing_part(self):
        apt = FakeApt(installed={'docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-compose-plugin'})
        for unit in ('docker.service', 'docker.socket'):
            apt.units[unit] = {'LoadState': 'loaded', 'UnitFileState': 'enabled', 'ActiveState': 'active'}
        self.file('etc/apt/sources.list.d/docker.list',
                  'deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable\n')
        self.assertEqual(self.family(apt).install(['docker', 'docker-compose']), ['docker'])
        installs = [argv for argv in apt.calls if argv[:2] == ['apt-get', 'install']]
        self.assertEqual(installs, [['apt-get', 'install', '-y', '-o', 'Dpkg::Options::=--force-confold',
                                     'docker-buildx-plugin']])
        self.assertFalse(any(argv[:2] == ['systemctl', 'disable'] for argv in apt.calls))
        self.assertEqual(apt.units['docker.service']['ActiveState'], 'active')
        self.assertEqual(apt.enabled(), ['docker.service', 'docker.socket'])

    def test_debians_own_docker_is_never_mixed_in(self):
        apt = FakeApt(installed={'docker.io'})
        with self.assertRaisesRegex(RuntimeError, r'own Docker packages installed \(docker.io\)'):
            self.family(apt).install(['docker'])
        self.assertFalse(any(argv[:2] == ['apt-get', 'install'] for argv in apt.calls))
        self.assertFalse((self.root / 'etc/apt/sources.list.d/docker.list').exists())

    def test_existing_definitions_in_any_file_or_format_are_left_alone(self):
        for name, text in (
                ('docker.sources', ('Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\n'
                                    'Components: stable\nSigned-By: /usr/share/keyrings/docker.gpg\n')),
                ('download_docker_com.list', ('deb [signed-by=/usr/share/keyrings/d.gpg] '
                                              'https://download.docker.com/linux/ubuntu noble stable\n'))):
            with self.subTest(name):
                source = self.file('etc/apt/sources.list.d/' + name, text)
                self.family().install(['docker'])
                self.assertEqual(self.keys, [])
                self.assertFalse((self.root / 'etc/apt/sources.list.d/docker.list').exists())
                source.unlink()

    def test_switched_off_or_commented_definitions_do_not_count(self):
        self.file('etc/apt/sources.list.d/old.sources', 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\n'
                                                        'Suites: noble\nEnabled: no\n')
        self.file('etc/apt/sources.list', '# deb https://download.docker.com/linux/ubuntu noble stable\n')
        self.family().install(['docker'])
        self.assertTrue((self.root / 'etc/apt/sources.list.d/docker.list').exists())

    def test_our_duplicate_of_someone_elses_definition_is_removed(self):
        self.family().install(['tailscale'])
        self.file('etc/apt/sources.list.d/tailscale.sources', 'URIs: https://pkgs.tailscale.com/stable/ubuntu\n')
        family = self.family(FakeApt())
        self.assertFalse(family.add_repository(host.TAILSCALE))
        self.assertFalse((self.root / 'etc/apt/sources.list.d/tailscale.list').exists())

    def test_someone_elses_file_with_our_name_is_never_overwritten(self):
        self.file('etc/apt/sources.list.d/docker.list', '# my notes\n')
        with self.assertRaisesRegex(RuntimeError, 'docker.list is someone else'):
            self.family().install(['docker'])
        self.assertEqual((self.root / 'etc/apt/sources.list.d/docker.list').read_text(), '# my notes\n')

    def test_tailscale_key_is_kept_with_the_backed_up_settings(self):
        self.family(release={'ID': 'debian', 'VERSION_CODENAME': 'bookworm'}).install(['tailscale'])
        self.assertEqual(self.keys, ['https://pkgs.tailscale.com/stable/debian/bookworm.noarmor.gpg'])
        listing = (self.root / 'etc/apt/sources.list.d/tailscale.list').read_text()
        self.assertIn('signed-by=/etc/apt/keyrings/tailscale-archive-keyring.gpg] '
                      'https://pkgs.tailscale.com/stable/debian bookworm main', listing)
        self.assertTrue(host.TAILSCALE.key_path.startswith('/etc/apt/keyrings/'))
        self.assertIn('/etc/apt/keyrings', host.Debian.settings)

    def test_restored_list_gets_its_missing_key_back(self):
        family = self.family()
        family.add_repository(host.DOCKER)
        (self.root / 'etc/apt/keyrings/docker.asc').unlink()   # a fresh system with restored lists
        self.assertTrue(self.family().add_repository(host.DOCKER))
        self.assertEqual((self.root / 'etc/apt/keyrings/docker.asc').read_bytes(), KEY)
        self.assertFalse(self.family().add_repository(host.DOCKER))   # nothing left to do

    def test_list_from_0_28_0_moves_its_key_into_the_backup(self):
        self.file('etc/apt/sources.list.d/tailscale.list',
                  'deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] '
                  'https://pkgs.tailscale.com/stable/ubuntu noble main\n')
        self.assertTrue(self.family().add_repository(host.TAILSCALE))
        self.assertIn('/etc/apt/keyrings/tailscale-archive-keyring.gpg',
                      (self.root / 'etc/apt/sources.list.d/tailscale.list').read_text())
        self.assertTrue((self.root / 'etc/apt/keyrings/tailscale-archive-keyring.gpg').is_file())

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


class UnavailableTvTest(unittest.TestCase):
    """On Debian/Ubuntu the TV step is neither done nor waiting: it is unavailable."""

    def test_tv_step_reports_unavailable(self):
        from modules import tv
        ctx = mock.Mock(apply=True)
        with mock.patch.object(tv.os, 'geteuid', return_value=0), \
                mock.patch.object(tv.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(host, 'detect', return_value=host.Debian):
            result = tv.run(ctx)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('Arch Linux or Nyarch', result['reason'])

    def test_installer_counts_it_apart(self):
        import argparse

        import installer
        root = Path(__file__).resolve().parents[1]
        step = mock.Mock(run=lambda ctx: {'status': 'unavailable', 'reason': 'Not on this Linux'})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(installer, 'load_modules', return_value={'tv': {'object': step, 'deps': []}}), \
                mock.patch('builtins.print') as printed:
            args = argparse.Namespace(config=str(root / 'config.json'), state_dir=tmp, profile=None, module='tv',
                                      retry_failed=False, apply=True, check=False, dry_run=False)
            self.assertEqual(installer._execute(args), 0)
            state = json.loads((Path(tmp) / 'state.json').read_text())
        self.assertEqual(state['tv'], {'status': 'unavailable', 'reason': 'Not on this Linux'})
        lines = ' '.join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn('UNAVAILABLE tv: Not on this Linux', lines)
        self.assertIn('0 passed; 1 not available on this system; 0 failed', lines)
        self.assertNotIn('PASS tv', lines)

    def test_finishing_screen_shows_it_without_blocking_ready(self):
        import wizard
        out = []
        with tempfile.TemporaryDirectory() as tmp:
            complete = wizard.finishing_screen(
                {'data_mount': tmp, 'profiles': {'full': ['storage', 'tv']}},
                {'storage': {'status': 'success'}, 'tv': {'status': 'unavailable', 'reason': 'Not on this Linux'}},
                None, out.append)
        text = '\n'.join(out)
        self.assertTrue(complete)
        self.assertIn('Done: 1 of 1 steps.', text)
        self.assertIn('Not available on this system:\n  - TV setup: Not on this Linux', text)

    def test_acceptance_leaves_it_out(self):
        import acceptance
        with mock.patch.object(acceptance.tv, 'available', return_value=False):
            self.assertEqual(acceptance.accepted_modules({}, ['storage', 'tv']), ['storage'])
        with mock.patch.object(acceptance.tv, 'available', return_value=True):
            self.assertEqual(acceptance.accepted_modules({}, ['storage', 'tv']), ['storage', 'tv'])
            self.assertEqual(acceptance.accepted_modules({'tv': {'wanted': False}}, ['tv']), [])


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
