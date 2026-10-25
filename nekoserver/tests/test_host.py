import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import MappingProxyType
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
            # SUSE's enterprise system names only suse, and has other repositories.
            path = release(tmp, 'ID=sles\nID_LIKE="suse"\nPRETTY_NAME="SUSE Linux Enterprise Server 16.0"\n')
            self.assertIsNone(host.detect(path))
            self.assertFalse(host.supported(path))
            with self.assertRaisesRegex(host.Unsupported, 'SUSE Linux Enterprise Server 16.0 is not supported yet'):
                host.current(path=path)
            self.assertIs(host.family_or_default(path), host.Arch)

    def test_missing_os_release_is_unsupported(self):
        self.assertEqual(host.os_release('/nonexistent/os-release'), {})
        self.assertIsNone(host.detect('/nonexistent/os-release'))

    def test_debian_ubuntu_and_derivatives_are_debian(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in ('ID=debian\n', 'ID=ubuntu\nID_LIKE=debian\n', 'ID=linuxmint\nID_LIKE="ubuntu debian"\n',
                         'ID=Ubuntu\n', 'ID=elementary\nID_LIKE=ubuntu\n'):
                self.assertIs(host.detect(release(tmp, text)), host.Debian, text)

    def test_opensuse_is_suse_and_sles_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in ('ID=opensuse-leap\nID_LIKE="suse opensuse"\n', 'ID=opensuse-tumbleweed\nID_LIKE="opensuse suse"\n',
                         'ID=opensuse-slowroll\nID_LIKE="opensuse suse"\n', 'ID=geckolinux\nID_LIKE="opensuse suse"\n'):
                self.assertIs(host.detect(release(tmp, text)), host.Suse, text)
            self.assertIsNone(host.detect(release(tmp, 'ID=sles\nID_LIKE="suse"\n')))

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
        # pacman's lock as this test's own, whatever this machine's pacman is doing.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / 'var/lib/pacman/db.lck'
        self.lock.parent.mkdir(parents=True)
        lock = mock.patch.object(host.Arch, 'root', Path(self.tmp.name))
        lock.start()
        self.addCleanup(lock.stop)

    def test_waits_while_another_pacman_works(self):
        pacman = FakePacman(offered={'b'})
        family = host.Arch(pacman)
        self.lock.write_text('')
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 3:
                self.lock.unlink()           # the other pacman (an updater) finished
        family.sleep = sleep
        self.assertEqual(family.install(['b']), ['b'])
        self.assertEqual(slept, [5, 5, 5])
        self.assertIn(['pacman', '-S', '--needed', '--noconfirm', 'b'], pacman.calls)

    def test_docker_installed_as_a_snap_stops_before_docker_installs(self):
        for folder in ('snap/docker', 'var/lib/snapd/snap/docker'):   # Ubuntu's, other systems'
            with self.subTest(folder=folder):
                snap = Path(self.tmp.name) / folder
                snap.mkdir(parents=True)
                pacman = FakePacman(offered={'docker', 'docker-compose', 'caddy'})
                family = host.Arch(pacman)
                with self.assertRaisesRegex(RuntimeError, r'Docker is installed here as a snap.*sudo snap remove docker'):
                    family.install(['docker', 'caddy'])
                self.assertFalse(any(argv[1] == '-S' for argv in pacman.calls))
                self.assertEqual(family.install(['caddy']), ['caddy'])     # anything else still installs
                snap.rmdir()
        self.assertFalse(host.snap_docker(self.tmp.name))

    def test_a_lock_that_stays_stops_with_the_steps(self):
        pacman = FakePacman(offered={'b'})
        family = host.Arch(pacman)
        family.sleep, family.wait = (lambda seconds: None), 600
        self.lock.write_text('')
        with self.assertRaisesRegex(RuntimeError, r'pacman is still locked \(/var/lib/pacman/db.lck\) after 10 '
                                                  r'minutes.*sudo rm /var/lib/pacman/db.lck'):
            family.install(['b'])
        self.assertFalse(any(argv[1] == '-S' for argv in pacman.calls))

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
        family = host.Arch(FakePacman(fail=True))
        with self.assertRaisesRegex(RuntimeError, r'Installing b failed: error: target not found'):
            family.install(['b'])

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
UNITS = {'samba': ('smbd.service', 'nmbd.service', 'winbind.service'), 'caddy': ('caddy.service',),
         'docker-ce': ('docker.service', 'docker.socket'), 'tailscale': ('tailscaled.service',)}


def compare_versions(argv):
    """dpkg --compare-versions for plain dotted numbers."""
    have, op, wanted = argv[2:5]
    have, wanted = (tuple(int(x) for x in v.split('.')) for v in (have, wanted))
    ok = {'<<': have < wanted, '<=': have <= wanted, '=': have == wanted, '>=': have >= wanted, '>>': have > wanted}[op]
    return subprocess.CompletedProcess(argv, 0 if ok else 1, '', '')


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
        self.masked = set()
        self.masked_during_install = None
        self.install_error = None
        self.unconfigured = []    # packages dpkg --audit reports as not set up
        self.configured = []      # each dpkg --configure call's packages
        self.configure_fails = False
        self.protected_while_configuring = None

    def __call__(self, argv, **kwargs):
        if argv[0] == 'apt-get':
            # Every apt-get waits for other package jobs; recorded without that option.
            assert argv[1:3] == host.APT_WAIT, argv
            argv = [argv[0], *argv[3:]]
        self.calls.append(argv)
        self.envs.append(kwargs.get('env', {}))
        handler = {'apt-cache': self.cache, 'dpkg-query': self.dpkg, 'systemctl': self.systemctl,
                   'dpkg': self.dpkg_tool}.get(argv[0])
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
        if argv[2].startswith('-f=${db:Status-Abbrev}'):
            def status(name):
                return 'iU ' if name in self.unconfigured else 'hi ' if name in self.held else 'ii '
            lines = [status(name) + '|' + name + '|' + self.versions.get(name, '1.0') + '|' + ', '.join(self.provides.get(name, []))
                     for name in sorted(self.installed | set(self.unconfigured))]
            return subprocess.CompletedProcess(argv, 0, ''.join(line + '\n' for line in lines), '')
        if argv[2].startswith('-f=${Pre-Depends}'):
            return subprocess.CompletedProcess(argv, 0, ', ' + ', '.join(self.depends.get(argv[3], [])), '')
        ok = argv[3] in self.installed
        status = ('hold' if argv[3] in self.held else 'install') + ' ok installed'
        return subprocess.CompletedProcess(argv, 0 if ok else 1, status if ok else '', '')

    held = ()

    architectures = ''        # dpkg --print-foreign-architectures

    def dpkg_tool(self, argv):
        if argv[1] == '--compare-versions':
            return compare_versions(argv)
        if argv[1] == '--print-foreign-architectures':
            return subprocess.CompletedProcess(argv, 0, self.architectures, '')
        if argv[1] == '--audit':
            if self.audit_fails:
                return subprocess.CompletedProcess(argv, 2, '', 'dpkg: error: dpkg frontend lock is locked')
            out = ('The following packages are only half configured, probably due to problems\n'
                   'dpkg --configure <package> or the configure menu option in dselect:\n'
                   + ''.join(' ' + n + ':amd64          some description\n' for n in self.unconfigured))
            return subprocess.CompletedProcess(argv, 1 if self.unconfigured else 0, out if self.unconfigured else '', '')
        policy = self.root / 'usr/sbin/policy-rc.d'
        self.protected_while_configuring = (set(self.masked), policy.exists())
        self.configured.append(argv[2:])
        if self.configure_fails:
            return subprocess.CompletedProcess(argv, 1, '', 'dpkg: error processing package samba (--configure)')
        self.unconfigured = [n for n in self.unconfigured if n not in argv[2:]]
        return subprocess.CompletedProcess(argv, 0, '', '')

    audit_fails = False
    versions = MappingProxyType({})
    depends = MappingProxyType({})
    provides = MappingProxyType({})

    def install(self, argv):
        policy = self.root / 'usr/sbin/policy-rc.d'
        self.policy_during_install = policy.read_text() if policy.exists() else None
        self.masked_during_install = set(self.masked)
        names = argv[5:]
        done = names if self.fail_after is None else names[:self.fail_after]
        for name in done:
            self.installed.add(name)
            for unit in UNITS.get(name, ()):
                self.units[unit] = {'LoadState': 'loaded', 'UnitFileState': 'enabled', 'ActiveState': 'inactive'}
        if self.on_install:
            self.on_install(done)
        if self.install_error:
            raise self.install_error
        failed = len(done) < len(names)
        return subprocess.CompletedProcess(argv, 100 if failed else 0, '', 'E: Sub-process /usr/bin/dpkg returned an error code (1)' if failed else '')

    def systemctl(self, argv):
        if argv[1] == 'show':
            units = [a for a in argv[2:] if not a.startswith('--')]
            blocks = []
            for unit in units:
                state = self.units.get(unit, {'LoadState': 'not-found', 'UnitFileState': '', 'ActiveState': 'inactive'})
                if unit in self.masked:
                    state = {**state, 'LoadState': 'masked', 'UnitFileState': 'masked'}
                blocks.append('Id=' + unit + '\n' + '\n'.join(k + '=' + v for k, v in state.items()))
            return subprocess.CompletedProcess(argv, 0, '\n\n'.join(blocks) + '\n', '')
        if argv[1] in ('mask', 'unmask'):
            units = {a for a in argv[2:] if not a.startswith('--')}
            self.masked = self.masked | units if argv[1] == 'mask' else self.masked - units
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


# Holds dpkg's lock the way apt does, as if a cut-off installation were still working.
HOLD_DPKG_LOCK = ('import fcntl, sys, time\n'
                  'f = open(sys.argv[1], "a"); fcntl.lockf(f, fcntl.LOCK_EX)\n'
                  'print("held", flush=True); time.sleep(30)')


def stop(process):
    process.kill()
    process.wait()
    if process.stdout:
        process.stdout.close()


KEY = b'-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBF\n-----END PGP PUBLIC KEY BLOCK-----\n'


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

    def packaged_example(self, text='# example\n'):
        """smb.conf as Samba's package writes it, with ucf's record of it."""
        self.file('etc/samba/smb.conf', text)
        self.file('var/lib/ucf/hashfile', hashlib.md5(text.encode()).hexdigest() + '  /etc/samba/smb.conf\n')

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

    def test_lists_the_system_is_refreshing_are_waited_for(self):
        # Tried on Ubuntu: while apt-daily holds the lists' lock, apt-get update fails at
        # once ("Could not get lock /var/lib/apt/lists/lock"), whatever DPkg::Lock::Timeout says.
        lock = self.root / 'var/lib/apt/lists/lock'
        lock.parent.mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, '-c', HOLD_DPKG_LOCK, str(lock)], stdout=subprocess.PIPE)
        self.addCleanup(stop, holder)
        self.assertEqual(holder.stdout.readline().strip(), b'held')
        apt = FakeApt(offered={'samba'})
        family = self.family(apt)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            if len(slept) == 2:
                stop(holder)                 # the system's own refresh finished
        family.sleep = sleep
        family.refresh()
        self.assertEqual(slept, [5, 5])
        self.assertEqual(apt.calls.count(['apt-get', 'update']), 1)

    def test_output_that_is_read_is_in_english(self):
        apt = FakeApt(offered={'caddy'})
        self.family(apt).available('caddy')
        for argv, env in zip(apt.calls, apt.envs):
            self.assertEqual(env.get('LC_ALL'), 'C', argv)
            # Ubuntu's needrestart never restarts running services behind setup's back.
            self.assertEqual(env.get('NEEDRESTART_SUSPEND'), '1', argv)

    def test_a_held_package_counts_as_installed(self):
        apt = FakeApt(installed={'docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin'})
        apt.held = {'docker-ce'}
        family = self.family(apt)
        self.assertTrue(family.installed('docker'))
        self.assertEqual(family.install(['docker']), [])
        self.assertFalse(any(argv[:2] == ['apt-get', 'install'] for argv in apt.calls))

    def test_packages_do_not_start_their_services_and_are_left_off(self):
        apt = FakeApt()
        self.family(apt).install(['samba', 'caddy'])
        self.assertIn('exit 101', apt.policy_during_install)
        self.assertFalse((self.root / 'usr/sbin/policy-rc.d').exists())
        self.assertEqual(apt.enabled(), [])
        self.assertEqual(sorted(apt.units), ['caddy.service', 'nmbd.service', 'smbd.service', 'winbind.service'])

    def test_lmde_uses_the_debian_release_name(self):
        family = self.family(release={'ID': 'linuxmint', 'ID_LIKE': 'debian', 'VERSION_CODENAME': 'faye',
                                      'DEBIAN_CODENAME': 'bookworm'})
        self.assertEqual(family.vendor_release(), ('debian', 'bookworm'))
        mint = self.family(release={'ID': 'linuxmint', 'ID_LIKE': 'ubuntu debian', 'VERSION_CODENAME': 'wilma',
                                    'UBUNTU_CODENAME': 'noble'})
        self.assertEqual(mint.vendor_release(), ('ubuntu', 'noble'))

    def test_packaged_example_needs_the_package_record(self):
        family = self.family()
        self.assertFalse(family.packaged_example('/etc/samba/smb.conf'))   # no file, no record
        self.packaged_example()
        self.assertTrue(family.packaged_example('/etc/samba/smb.conf'))
        self.file('etc/samba/smb.conf', '# edited\n')
        self.assertFalse(family.packaged_example('/etc/samba/smb.conf'))
        self.assertFalse(host.Arch(FakePacman()).packaged_example('/etc/samba/smb.conf'))

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
        apt.on_install = lambda done: 'samba' in done and self.packaged_example()
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
        self.packaged_example()
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
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'Could not switch off docker.socket'):
            family.install(['docker'])

    def test_units_that_are_not_installed_or_masked_are_left_alone(self):
        apt = FakeApt()
        apt.units['samba-ad-dc.service'] = {'LoadState': 'masked', 'UnitFileState': 'masked', 'ActiveState': 'inactive'}
        self.family(apt).install(['samba'])
        disabled = next(argv for argv in apt.calls if argv[:2] == ['systemctl', 'disable'])
        self.assertNotIn('samba-ad-dc.service', disabled)

    def test_new_services_are_protected_until_switched_off(self):
        apt = FakeApt()
        self.family(apt).install(['samba'])
        self.assertEqual(apt.masked_during_install, {'smbd.service', 'nmbd.service', 'winbind.service'})
        self.assertIn('exit 101', apt.policy_during_install)
        self.assertEqual(apt.masked, set())
        self.assertEqual(apt.enabled(), [])
        self.assertFalse((self.root / 'usr/sbin/policy-rc.d').exists())

    def test_a_mask_the_package_set_itself_stays(self):
        apt = FakeApt()
        self.file('var/lib/systemd/deb-systemd-helper-masked/nmbd.service')
        self.family(apt).install(['samba'])
        self.assertEqual(apt.masked, {'nmbd.service'})

    def test_timed_out_installation_is_tidied_once_the_tools_stop(self):
        apt = FakeApt()
        apt.install_error = subprocess.TimeoutExpired(['apt-get'], 900)
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'Installing samba did not finish within 15 minutes'):
            family.install(['samba'])
        self.assertEqual(apt.enabled(), [])
        self.assertEqual(apt.masked, set())
        self.assertFalse(self.journal().exists())
        self.assertFalse((self.root / 'usr/sbin/policy-rc.d').exists())

    def test_protection_stays_while_the_tools_are_still_busy(self):
        apt = FakeApt()
        apt.install_error = subprocess.TimeoutExpired(['apt-get'], 900)
        family = self.family(apt)
        family.wait = 0
        lock = self.file('var/lib/dpkg/lock-frontend')
        holder = subprocess.Popen([sys.executable, '-c', HOLD_DPKG_LOCK, str(lock)], stdout=subprocess.PIPE)
        self.addCleanup(stop, holder)
        holder.stdout.readline()
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            family.install(['samba'])
        # Nothing was tidied while dpkg may still be working: all protection is still in place.
        self.assertTrue(self.journal().exists())
        self.assertEqual(apt.masked, {'smbd.service', 'nmbd.service', 'winbind.service'})
        self.assertEqual((self.root / 'usr/sbin/policy-rc.d').read_text(), host.OUR_POLICY)
        stop(holder)
        self.family(apt).reconcile()
        self.assertFalse(self.journal().exists())
        self.assertEqual(apt.masked, set())
        self.assertFalse((self.root / 'usr/sbin/policy-rc.d').exists())

    def test_packages_left_unset_up_are_set_up_while_still_protected(self):
        apt = FakeApt()
        apt.install_error = subprocess.TimeoutExpired(['apt-get'], 900)
        apt.on_install = lambda done: setattr(apt, 'unconfigured', ['samba'])
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            family.install(['samba'])
        self.assertEqual(apt.protected_while_configuring, ({'smbd.service', 'nmbd.service', 'winbind.service'}, True))
        self.assertEqual(apt.unconfigured, [])
        self.assertEqual(apt.masked, set())
        self.assertEqual(apt.enabled(), [])
        self.assertFalse(self.journal().exists())

    def test_set_up_that_fails_keeps_everything_protected(self):
        apt = FakeApt()
        apt.install_error = subprocess.TimeoutExpired(['apt-get'], 900)
        apt.on_install = lambda done: setattr(apt, 'unconfigured', ['samba'])
        apt.configure_fails = True
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            family.install(['samba'])
        self.assertTrue(self.journal().exists())
        self.assertEqual(apt.masked, {'smbd.service', 'nmbd.service', 'winbind.service'})
        self.assertEqual((self.root / 'usr/sbin/policy-rc.d').read_text(), host.OUR_POLICY)
        retry = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'dpkg could not finish setting up'):
            retry.reconcile()

    def test_dpkg_still_working_after_apt_stopped_is_waited_for(self):
        apt = FakeApt()
        apt.install_error = subprocess.TimeoutExpired(['apt-get'], 900)
        family = self.family(apt)
        family.wait = 0
        lock = self.file('var/lib/dpkg/lock')    # dpkg's own lock; apt's is free
        holder = subprocess.Popen([sys.executable, '-c', HOLD_DPKG_LOCK, str(lock)], stdout=subprocess.PIPE)
        self.addCleanup(stop, holder)
        holder.stdout.readline()
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            family.install(['samba'])
        self.assertTrue(self.journal().exists())
        self.assertEqual(apt.masked, {'smbd.service', 'nmbd.service', 'winbind.service'})

    def test_someone_elses_half_set_up_packages_are_left_to_them(self):
        apt = FakeApt()
        apt.unconfigured = ['postfix']
        self.family(apt).install(['hdparm'])
        self.assertFalse(any(argv[:2] == ['dpkg', '--configure'] for argv in apt.calls))
        self.assertEqual(apt.unconfigured, ['postfix'])

    def test_only_our_packages_and_what_they_need_are_set_up(self):
        apt = FakeApt()
        apt.depends = {'samba': ['samba-common-bin (= 2:4.19)', 'libc6 (>= 2.38) | libc6-alt'],
                       'samba-common-bin': ['samba-common']}
        apt.install_error = subprocess.TimeoutExpired(['apt-get'], 900)
        apt.on_install = lambda done: setattr(apt, 'unconfigured', ['samba', 'postfix', 'samba-common-bin', 'samba-common'])
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            family.install(['samba'])
        self.assertEqual(apt.configured, [['samba', 'samba-common', 'samba-common-bin']])
        self.assertEqual(apt.unconfigured, ['postfix'])   # someone else's stays as it was
        self.assertFalse(any(argv[:3] == ['dpkg', '--configure', '-a'] for argv in apt.calls))

    def test_a_choice_already_met_adds_nothing_unfinished(self):
        # samba needs "libwbclient | libwbclient-alt", and "mail-transport-agent" (a name
        # exim4 provides). Both are already met, so the unfinished alternatives stay as they are.
        apt = FakeApt(installed={'libwbclient', 'exim4'})
        apt.provides = {'exim4': ['mail-transport-agent']}
        apt.depends = {'samba': ['libwbclient-alt (>= 1) | libwbclient', 'mail-transport-agent | postfix',
                                 'samba-common | samba-common-alt']}
        apt.install_error = subprocess.TimeoutExpired(['apt-get'], 900)

        def interrupted(done):
            apt.unconfigured = ['samba', 'libwbclient-alt', 'postfix', 'samba-common', 'samba-common-alt']
        apt.on_install = interrupted
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            family.install(['samba'])
        # Of a choice nothing met yet, only the first unfinished one is set up.
        self.assertEqual(apt.configured, [['samba', 'samba-common']])
        self.assertEqual(apt.unconfigured, ['libwbclient-alt', 'postfix', 'samba-common-alt'])

    def interrupted_samba(self, apt):
        apt.install_error = subprocess.TimeoutExpired(['apt-get'], 900)
        apt.on_install = lambda done: setattr(apt, 'unconfigured', ['samba', 'libnew'])
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'did not finish'):
            family.install(['samba'])
        return apt.configured

    def test_a_version_too_old_does_not_meet_a_choice(self):
        # samba needs "libold (>= 2.0) | libnew"; libold 1.0 is installed, which is not
        # enough, so the unfinished libnew is what apt chose and is set up too.
        apt = FakeApt(installed={'libold'})
        apt.versions = {'libold': '1.0'}
        apt.depends = {'samba': ['libold (>= 2.0) | libnew']}
        self.assertEqual(self.interrupted_samba(apt), [['libnew', 'samba']])
        # libold 2.1 is enough: libnew is someone else's to finish.
        apt = FakeApt(installed={'libold'})
        apt.versions = {'libold': '2.1'}
        apt.depends = {'samba': ['libold (>= 2.0) | libnew']}
        self.assertEqual(self.interrupted_samba(apt), [['samba']])

    def test_a_held_package_meets_a_choice(self):
        apt = FakeApt(installed={'libold'})
        apt.held = {'libold'}
        apt.depends = {'samba': ['libold | libnew']}
        self.assertEqual(self.interrupted_samba(apt), [['samba']])

    def test_an_unversioned_provide_never_meets_a_versioned_choice(self):
        apt = FakeApt(installed={'other'})
        apt.provides = {'other': ['libold']}
        apt.depends = {'samba': ['libold (>= 2.0) | libnew']}
        self.assertEqual(self.interrupted_samba(apt), [['libnew', 'samba']])

    def test_dpkg_that_cannot_report_keeps_protection(self):
        apt = FakeApt()
        apt.audit_fails = True
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'dpkg could not say which packages are unfinished'):
            family.install(['caddy'])
        self.assertTrue(self.journal().exists())
        self.assertEqual(apt.masked, {'caddy.service'})
        self.assertEqual((self.root / 'usr/sbin/policy-rc.d').read_text(), host.OUR_POLICY)

    def test_a_new_setup_run_waits_for_a_cut_off_installation(self):
        # An earlier run was cut off; its dpkg is still working when setup starts again.
        apt = FakeApt(installed={'samba'})
        apt.masked = {'smbd.service'}
        self.file(host.Debian.journal, json.dumps({'format': 'neko-package-install-v1', 'packages': ['samba'],
                                                    'before': {'new_units': ['smbd.service']}}))
        self.file('usr/sbin/policy-rc.d', host.OUR_POLICY)
        lock = self.file('var/lib/dpkg/lock')
        holder = subprocess.Popen([sys.executable, '-c', HOLD_DPKG_LOCK, str(lock)], stdout=subprocess.PIPE)
        self.addCleanup(stop, holder)
        holder.stdout.readline()
        family = self.family(apt)
        family.wait = 0
        with self.assertRaisesRegex(RuntimeError, 'apt or dpkg is still working'):
            family.reconcile()
        self.assertTrue(self.journal().exists())
        self.assertEqual(apt.masked, {'smbd.service'})
        self.assertEqual((self.root / 'usr/sbin/policy-rc.d').read_text(), host.OUR_POLICY)
        stop(holder)
        self.family(apt).reconcile()
        self.assertFalse(self.journal().exists())
        self.assertFalse((self.root / 'usr/sbin/policy-rc.d').exists())

    def test_leftover_policy_stays_while_dpkg_works(self):
        self.file('usr/sbin/policy-rc.d', host.OUR_POLICY)
        lock = self.file('var/lib/dpkg/lock-frontend')
        holder = subprocess.Popen([sys.executable, '-c', HOLD_DPKG_LOCK, str(lock)], stdout=subprocess.PIPE)
        self.addCleanup(stop, holder)
        holder.stdout.readline()
        family = self.family()
        family.wait = 0
        with self.assertRaisesRegex(RuntimeError, 'still working'):
            family.reconcile()
        self.assertTrue((self.root / 'usr/sbin/policy-rc.d').exists())

    def test_failed_switch_off_keeps_services_masked(self):
        apt = FakeApt()
        apt.disable_fails = True
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'Could not switch off'):
            family.install(['caddy'])
        self.assertEqual(apt.masked, {'caddy.service'})
        self.assertEqual((self.root / 'usr/sbin/policy-rc.d').read_text(), host.OUR_POLICY)

    def test_another_setups_installation_is_never_touched(self):
        # The first setup is still installing: its record, policy and lock are live.
        self.file(host.Debian.journal, json.dumps({'format': 'neko-package-install-v1', 'packages': ['samba'],
                                                    'before': {'new_units': ['smbd.service']}}))
        self.file('usr/sbin/policy-rc.d', host.OUR_POLICY)
        lock = self.file('run/nekoserver-packages.lock')
        holder = subprocess.Popen(['flock', str(lock), 'sleep', '30'])
        self.addCleanup(stop, holder)
        for _ in range(100):
            if subprocess.run(['flock', '-n', str(lock), 'true'], check=False).returncode:
                break
            time.sleep(0.05)
        second = self.family()
        second.wait = 0
        with self.assertRaisesRegex(RuntimeError, 'Another NekoServer setup is installing packages'):
            second.reconcile()
        with self.assertRaisesRegex(RuntimeError, 'Another NekoServer setup is installing packages'):
            second.install(['hdparm'])
        self.assertTrue(self.journal().exists())
        self.assertTrue((self.root / 'usr/sbin/policy-rc.d').exists())

    def test_unreadable_record_stops(self):
        self.file(host.Debian.journal, '{broken')
        family = self.family()
        with self.assertRaisesRegex(RuntimeError, 'Unreadable record'):
            family.install(['hdparm'])

    # Samba's packaged example

    def test_existing_samba_settings_are_never_moved(self):
        self.file('etc/samba/smb.conf', '# mine\n')
        self.family().install(['samba'])
        self.assertEqual((self.root / 'etc/samba/smb.conf').read_text(), '# mine\n')

    def test_changed_example_is_someones_settings(self):
        apt = FakeApt()

        def written(done):
            self.packaged_example()
            self.file('etc/samba/smb.conf', '# example, edited\n')
        apt.on_install = written
        self.family(apt).install(['samba'])
        self.assertEqual((self.root / 'etc/samba/smb.conf').read_text(), '# example, edited\n')

    def test_unchanged_example_matches_its_record(self):
        apt = FakeApt()

        apt.on_install = lambda done: self.packaged_example()
        self.family(apt).install(['samba'])
        self.assertTrue((self.root / 'etc/samba/smb.conf.packaged').exists())

    def test_without_a_package_record_the_file_stays(self):
        # Interrupted installation, then someone edits smb.conf; ucf has no record of it.
        self.file('etc/samba/smb.conf', '# edited by hand\n')
        for hashfile in (None, 'abc123  /etc/other.conf\n'):
            with self.subTest(hashfile=hashfile):
                self.file(host.Debian.journal, json.dumps({'format': 'neko-package-install-v1', 'packages': ['samba'],
                                                            'before': {'samba_config_existed': False}}))
                if hashfile:
                    self.file('var/lib/ucf/hashfile', hashfile)
                self.family().reconcile()
                self.assertEqual((self.root / 'etc/samba/smb.conf').read_text(), '# edited by hand\n')
                self.assertFalse((self.root / 'etc/samba/smb.conf.packaged').exists())

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
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, 'would let docker.service, docker.socket start'):
            family.install(['docker'])
        self.assertFalse(any(argv[:2] == ['apt-get', 'install'] for argv in apt.calls))
        self.assertEqual(policy.read_text(), '#!/bin/sh\nexit 0\n')

    def test_a_policy_that_hangs_refuses_nothing(self):
        policy = self.file('usr/sbin/policy-rc.d', '#!/bin/sh\nsleep 60\n')
        policy.chmod(0o755)
        apt = FakeApt()
        family = self.family(apt)

        def run(argv, **kwargs):
            if argv[0].endswith('policy-rc.d'):
                raise subprocess.TimeoutExpired(argv, 30)
            return apt(argv, **kwargs)
        family.run = run
        with self.assertRaisesRegex(RuntimeError, 'would let caddy.service start'):
            family.install(['caddy'])

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
        family = self.family(apt)
        with self.assertRaisesRegex(RuntimeError, r'own Docker packages installed \(docker.io\)'):
            family.install(['docker'])
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

    def test_deb822_values_on_their_own_lines_count(self):
        self.file('etc/apt/sources.list.d/docker.sources',
                  'Types: deb\nuris:\n https://mirror.example/\n https://download.docker.com/linux/ubuntu\n'
                  'Suites: noble\nComponents: stable\nSigned-By:\n -----BEGIN PGP PUBLIC KEY BLOCK-----\n .\n'
                  ' abc\n -----END PGP PUBLIC KEY BLOCK-----\n')
        self.family().install(['docker'])
        self.assertEqual(self.keys, [])
        self.assertFalse((self.root / 'etc/apt/sources.list.d/docker.list').exists())

    def test_repair_removes_our_duplicate_before_apt_runs(self):
        family = self.family()
        family.add_repository(host.TAILSCALE)
        family.add_repository(host.DOCKER)
        self.file('etc/apt/sources.list.d/tailscale.sources', 'URIs: https://pkgs.tailscale.com/stable/ubuntu\n')
        apt = FakeApt()
        self.family(apt).repair()
        self.assertFalse((self.root / 'etc/apt/sources.list.d/tailscale.list').exists())
        self.assertTrue((self.root / 'etc/apt/sources.list.d/docker.list').exists())   # the only one: kept
        self.assertEqual(apt.calls, [])   # no apt command needed

    def test_upgrade_with_everything_installed_moves_the_tailscale_key(self):
        legacy_key = self.file('usr/share/keyrings/tailscale-archive-keyring.gpg', 'old key')
        self.file('etc/apt/sources.list.d/tailscale.list',
                  'deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] '
                  'https://pkgs.tailscale.com/stable/ubuntu noble main\n')
        apt = FakeApt(installed={real for name in host.APP_PACKAGES for real in self.family().names(name)})
        family = self.family(apt)
        host.bootstrap(family, log=lambda line: None)
        self.assertFalse(any(argv[:2] == ['apt-get', 'install'] for argv in apt.calls))   # nothing to install
        self.assertIn('signed-by=/etc/apt/keyrings/tailscale-archive-keyring.gpg',
                      (self.root / 'etc/apt/sources.list.d/tailscale.list').read_text())
        self.assertEqual((self.root / 'etc/apt/keyrings/tailscale-archive-keyring.gpg').read_bytes(), KEY)
        self.assertTrue(legacy_key.exists())   # not ours to delete: apt simply stops using it

    def test_offline_upgrade_keeps_a_working_definition(self):
        self.file('usr/share/keyrings/tailscale-archive-keyring.gpg', KEY.decode())
        legacy = ('deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] '
                  'https://pkgs.tailscale.com/stable/ubuntu noble main\n')
        listing = self.file('etc/apt/sources.list.d/tailscale.list', legacy)
        family = self.family()
        family.fetch = mock.Mock(side_effect=OSError('network unreachable'))
        logged = []
        family.repair(logged.append)
        self.assertEqual(listing.read_text(), legacy)
        self.assertIn('network unreachable', logged[0])

    def test_restored_definition_without_its_key_needs_the_network(self):
        self.family().add_repository(host.DOCKER)
        (self.root / 'etc/apt/keyrings/docker.asc').unlink()
        family = self.family()
        family.fetch = mock.Mock(side_effect=OSError('network unreachable'))
        with self.assertRaisesRegex(RuntimeError, 'docker repository key is missing and could not be downloaded'):
            family.repair()

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
        family = self.family()
        with self.assertRaisesRegex(RuntimeError, 'docker.list is someone else'):
            family.install(['docker'])
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
        self.assertEqual(host.main(['other']), 2)
        family = mock.Mock()
        with mock.patch('host.current', return_value=family):
            self.assertEqual(host.main(['repair']), 0)
        family.repair.assert_called_once_with()
        with mock.patch('host.bootstrap', side_effect=RuntimeError('no')):
            self.assertEqual(host.main(['bootstrap']), 1)


class UnavailableTvTest(unittest.TestCase):
    """On a Linux without a Steam package (RHEL and its rebuilds, openSUSE Leap) the TV
    step is neither done nor waiting: it is unavailable, and says why."""

    def test_tv_step_reports_unavailable(self):
        from modules import tv
        ctx = mock.Mock(apply=True)
        rhel = host.Rhel(run=lambda *args, **kwargs: None, release={'ID': 'rocky', 'ID_LIKE': 'rhel', 'VERSION_ID': '10.1'})
        with mock.patch.object(tv.os, 'geteuid', return_value=0), \
                mock.patch.object(tv.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(host, 'supported', return_value=True), \
                mock.patch.object(host, 'current', return_value=rhel):
            result = tv.run(ctx)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('Rocky Linux', result['reason'])
        self.assertIn('Flatpak', result['reason'])
        # Debian has a Steam package: the step goes on there (here to the desktop check).
        debian = host.Debian(run=lambda *args, **kwargs: None, release={'ID': 'debian'}, fetch=lambda url: b'')
        with mock.patch.object(tv.os, 'geteuid', return_value=0), \
                mock.patch.object(tv.platform, 'machine', return_value='x86_64'), \
                mock.patch.object(host, 'supported', return_value=True), \
                mock.patch.object(host, 'current', return_value=debian), \
                mock.patch.object(tv.shutil, 'which', side_effect=lambda name: '/usr/sbin/runuser' if name == 'runuser' else None):
            result = tv.run(ctx)
        self.assertEqual(result, {'status': 'unavailable', 'reason': tv.NO_DESKTOP})

    def test_installer_counts_it_apart(self):
        import argparse

        import installer
        root = Path(__file__).resolve().parents[1]
        step = mock.Mock(run=lambda ctx: {'status': 'unavailable', 'reason': 'Not on this Linux'})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(installer, 'load_modules', return_value={'tv': {'object': step, 'deps': []}}), \
                mock.patch('builtins.print') as printed:
            # A copy: a run with --apply writes its settings file back (never the template).
            settings = Path(tmp) / 'my-config.json'
            settings.write_text((root / 'config.json').read_text())
            args = argparse.Namespace(config=str(settings), state_dir=tmp, profile=None, module='tv',
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

    def test_acceptance_uses_the_steps_own_decision_and_its_record(self):
        import acceptance
        # A server without a desktop: the step skips TV, and acceptance asks nothing of it.
        tv = acceptance.tv
        with mock.patch.object(tv.host, "detect", return_value=object()), \
                mock.patch.object(tv.host, 'current', return_value=mock.Mock(name='debian')), \
                mock.patch.object(tv, 'unavailable_reason', return_value=None), \
                mock.patch.object(tv, 'has_desktop', return_value=False):
            self.assertFalse(tv.available())
            self.assertEqual(acceptance.accepted_modules({}, ['storage', 'tv']), ['storage'])
        # What setup recorded as not available is left out too.
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'installer/state'
            state.mkdir(parents=True)
            (state / 'state.json').write_text(json.dumps({'photo_backup': {'status': 'unavailable'}, 'storage': {'status': 'success'}}))
            with mock.patch.object(acceptance.tv, 'available', return_value=True):
                self.assertEqual(acceptance.accepted_modules({'data_mount': tmp}, ['storage', 'photo_backup']), ['storage'])


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

    def test_little_memory_is_a_note_never_a_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            meminfo = Path(tmp) / 'proc/meminfo'
            meminfo.parent.mkdir()
            for total, ok in (('3906250', False), ('7864320', True), ('16117248', True)):   # 4, 8 and 16 GB PCs
                with self.subTest(total=total):
                    meminfo.write_text(f'MemTotal:       {total} kB\nMemFree:  1000 kB\n')
                    rows = self.rows(tmp)
                    found = capabilities.value(rows, 'memory')
                    self.assertEqual(next(r for r in rows if r['name'] == 'memory')['ok'], ok, found)
                    self.assertEqual('8 GB those can be stopped' in found, not ok)
                    self.assertNotIn('memory', ' '.join(capabilities.missing(rows)).lower())
            meminfo.unlink()
            self.assertEqual(capabilities.value(self.rows(tmp), 'memory'), 'unknown')

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
            self.assertIn('sudo ufw allow', capabilities.value(rows, 'firewall'))
            self.assertEqual(capabilities.missing(rows), [])
        # Fedora's firewalld gets its own command.
        hint = capabilities.firewall_hint(['firewalld'])
        self.assertIn('sudo firewall-cmd --permanent --add-service=samba && sudo firewall-cmd --permanent '
                      '--add-port=8442-8447/tcp --add-port=8450-8499/tcp && sudo firewall-cmd --reload', hint)
        self.assertNotIn('ufw', hint)
        # firewall-cmd refuses --add-service and --add-port in one call (tried on Fedora 44).
        for call in hint.split('&&'):
            self.assertFalse('--add-service' in call and '--add-port' in call, call)


    def test_each_firewall_gets_its_own_command(self):
        self.assertIn('sudo ufw allow', capabilities.firewall_hint(['ufw']))
        nft = capabilities.firewall_hint(['nftables'])
        self.assertIn('sudo nft add rule inet filter input', nft)
        self.assertNotIn('ufw', nft)
        self.assertIn('/etc/nftables.conf', nft)
        self.assertIn('from a phone on your home Wi-Fi', nft)
    def test_fixed_requirements_stop_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self.rows(tmp, text='ID=gentoo\n', machine='aarch64', python=(3, 10), systemd='systemd 245')
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
            # Not tested with SELinux yet: setup stops and says how to try permissive mode.
            problems = ' '.join(capabilities.missing(rows))
            self.assertIn('SELinux is enforcing', problems)
            self.assertIn('sudo setenforce 0', problems)
            self.assertIn('ausearch -m AVC', problems)
            (Path(tmp) / 'sys/fs/selinux/enforce').write_text('0')
            rows = self.rows(tmp, target='graphical.target', docker='')
            self.assertEqual(capabilities.value(rows, 'selinux'), 'permissive')
            self.assertFalse(any('SELinux' in p for p in capabilities.missing(rows)))
            self.assertEqual(capabilities.value(rows, 'apparmor'), 'on')
            self.assertEqual(capabilities.value(rows, 'desktop'), 'yes')
            # docker is on the PATH but not running: the normal state before its step.
            self.assertIn('not started yet', capabilities.value(rows, 'docker'))
            release(tmp, 'ID=arch\n')
            rows = capabilities.detect(self.fake(docker=''), root=tmp, machine='x86_64', python=(3, 13),
                                       which=lambda name: '/usr/bin/' + name if name == 'systemctl' else None)
            self.assertIn('setup installs it', capabilities.value(rows, 'docker'))

    def test_a_clock_the_network_does_not_set_is_a_note(self):
        def run(argv, **kwargs):
            if argv[0] == 'timedatectl':
                return subprocess.CompletedProcess(argv, 0, answer + '\n', '')
            return self.fake()(argv, **kwargs)

        def which(name):
            return '/usr/bin/' + name if name in ('systemctl', 'timedatectl') else None
        with tempfile.TemporaryDirectory() as tmp:
            release(tmp, 'ID=arch\n')
            for answer, ok in (('no', False), ('yes', True)):
                rows = capabilities.detect(run, root=tmp, machine='x86_64', python=(3, 13), which=which)
                clock = next(row for row in rows if row['name'] == 'clock')
                self.assertEqual((clock['ok'], clock['required']), (ok, False), answer)
                self.assertEqual('sudo timedatectl set-ntp true' in clock['found'], not ok)
                self.assertNotIn('Clock', ' '.join(capabilities.missing(rows)))
            self.assertEqual(capabilities.value(self.rows(tmp), 'clock'), 'unknown')   # no timedatectl

    def test_a_download_refused_for_a_wrong_clock_says_so(self):
        import ssl
        import urllib.error

        import neko_common

        def opener(request, timeout):
            raise urllib.error.URLError(ssl.SSLCertVerificationError(
                1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: certificate is not yet valid'))
        with self.assertRaisesRegex(RuntimeError, r"clock looks wrong.*sudo timedatectl set-ntp true"):
            neko_common.fetch('https://example.org/x', 'Immich', 100, opener=opener)

        def offline(request, timeout):
            raise urllib.error.URLError(OSError('Network is unreachable'))
        with self.assertRaisesRegex(RuntimeError, 'connected to the internet') as caught:
            neko_common.fetch('https://example.org/x', 'Immich', 100, opener=offline)
        self.assertNotIn('clock', str(caught.exception))

    def test_report_marks_missing_requirements(self):
        lines = []
        capabilities.report([capabilities.row('x', 'Thing', 'none', False, True)], lines.append)
        self.assertEqual(lines[1], '  MISSING Thing: none')

    def test_this_sandbox_is_detected(self):
        # Whatever runs the tests: detection never crashes and always fills every row.
        rows = capabilities.detect()
        self.assertEqual(len({row['name'] for row in rows}), len(rows))


class RepositoryKeyErrorTests(unittest.TestCase):
    """A missing repository key says what is wrong (no internet; a release not packaged yet)."""
    def family(self, fetch):
        return host.Debian(FakeApt(), {'ID': 'ubuntu', 'VERSION_CODENAME': 'resolute', 'UBUNTU_CODENAME': 'resolute'}, fetch=fetch)

    def test_a_release_the_maker_does_not_package_yet(self):
        import urllib.error

        def missing(url):
            raise urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        family = self.family(missing)
        with self.assertRaisesRegex(RuntimeError, 'Tailscale does not offer packages for this release yet \\(ubuntu "resolute"'):
            family.fetch_key(host.TAILSCALE, 'ubuntu', 'resolute')

    def test_no_internet(self):
        import urllib.error

        def offline(url):
            raise urllib.error.URLError('Temporary failure in name resolution')
        family = self.family(offline)
        with self.assertRaisesRegex(RuntimeError, 'Downloading the Docker repository key failed \\(Temporary failure in name resolution\\). Check that this PC is connected'):
            family.fetch_key(host.DOCKER, 'ubuntu', 'resolute')


FEDORA_UNITS = {'samba': ('smb.service', 'nmb.service'), 'caddy': ('caddy.service',),
                'docker-ce': ('docker.service', 'docker.socket'), 'tailscale': ('tailscaled.service',)}


class FakeDnf(FakeApt):
    """dnf and rpm over sets of offered and installed packages; systemctl as FakeApt.
    `changed` holds settings files rpm -V reports as edited; `owners` maps a file to the
    package that owns it; installing samba writes its example smb.conf."""
    def __init__(self, offered=(), installed=(), fail_after=None, root=None):
        super().__init__(offered, installed, fail_after, root)
        self.changed, self.owners = set(), {'/etc/samba/smb.conf': 'samba-common'}

    def __call__(self, argv, **kwargs):
        if argv[0] == 'dnf' and argv[1] == 'install':
            self.calls.append(argv)
            self.envs.append(kwargs.get('env', {}))
            return self.dnf_install(argv)
        if argv[0] == 'dnf':
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, argv[-1] + '\n' if argv[-1] in self.offered else '', '')
        if argv[0] == 'rpm':
            self.calls.append(argv)
            return self.rpm(argv)
        return super().__call__(argv, **kwargs)

    def rpm(self, argv):
        if argv[1] == '-q':
            return subprocess.CompletedProcess(argv, 0 if argv[-1] in self.installed else 1, '', '')
        if argv[1] == '-qf':
            owner = self.owners.get(argv[-1])
            return subprocess.CompletedProcess(argv, 0 if owner else 1, (owner or '') + '\n', '')
        changed = ''.join('S.5....T.  c ' + path + '\n' for path in self.changed)
        return subprocess.CompletedProcess(argv, 1 if changed else 0, changed, '')

    def dnf_install(self, argv):
        self.masked_during_install = set(self.masked)
        names = argv[3:]
        done = names if self.fail_after is None else names[:self.fail_after]
        for name in done:
            self.installed.add(name)
            for unit in FEDORA_UNITS.get(name, ()):
                self.units[unit] = {'LoadState': 'loaded', 'UnitFileState': 'enabled', 'ActiveState': 'inactive'}
            if name == 'samba':
                example = self.root / 'etc/samba/smb.conf'
                if not example.exists():
                    example.write_text('# example\n')
        failed = len(done) < len(names)
        return subprocess.CompletedProcess(argv, 1 if failed else 0, '', 'Error: Transaction failed' if failed else '')


class FedoraTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for path in ('etc/yum.repos.d', 'etc/samba', 'run'):
            (self.root / path).mkdir(parents=True)
        patcher = mock.patch('host.shutil.which', return_value='/usr/bin/dnf')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fetched = []

    def family(self, dnf=None, fetch=None):
        dnf = dnf or FakeDnf()
        dnf.root = self.root
        family = host.Fedora(dnf, {'ID': 'fedora', 'VERSION_ID': '44'},
                             fetch=fetch or (lambda url: self.fetched.append(url) or b'<repomd/>'))
        family.root = self.root
        return family

    def test_fedora_and_the_rhel_family_are_told_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in ('ID=fedora\n', 'ID=fedora\nVARIANT_ID=server\n'):
                self.assertIs(host.detect(release(tmp, text)), host.Fedora, text)
            # RHEL's rebuilds name fedora too, but have other repositories.
            for text in ('ID=rocky\nID_LIKE="rhel centos fedora"\n', 'ID=almalinux\nID_LIKE="rhel centos fedora"\n',
                         'ID="rhel"\nID_LIKE="fedora"\n', 'ID="centos"\nID_LIKE="rhel fedora"\n'):
                self.assertIs(host.detect(release(tmp, text)), host.Rhel, text)
            # Oracle Linux names only fedora, and has its own repositories: not covered.
            self.assertIsNone(host.detect(release(tmp, 'ID="ol"\nID_LIKE="fedora"\n')))
        self.assertEqual(host.Fedora.service('samba'), 'smb.service')

    def test_installs_only_what_is_missing_and_leaves_new_services_off(self):
        dnf = FakeDnf(offered={'samba', 'caddy', 'hdparm'}, installed={'hdparm'})
        family = self.family(dnf)
        self.assertTrue(family.available('samba'))
        self.assertFalse(family.available('openrgb'))
        self.assertEqual(family.install(['hdparm', 'samba', 'caddy']), ['samba', 'caddy'])
        installs = [argv for argv in dnf.calls if argv[:2] == ['dnf', 'install']]
        self.assertEqual(installs, [['dnf', 'install', '-y', 'samba', 'caddy']])
        self.assertFalse(any('upgrade' in argv for argv in dnf.calls))
        # Kept from starting while installing, then switched off and unmasked.
        self.assertLessEqual({'smb.service', 'caddy.service'}, dnf.masked_during_install)
        self.assertEqual(dnf.enabled(), [])
        self.assertEqual(dnf.masked, set())
        self.assertFalse((self.root / host.Fedora.journal.lstrip('/')).exists())

    def test_docker_comes_from_dockers_repository(self):
        dnf = FakeDnf()
        self.family(dnf).install(['docker', 'docker-compose'])
        repo = (self.root / 'etc/yum.repos.d/nekoserver-docker-ce.repo').read_text()
        self.assertTrue(repo.startswith('# Managed by NekoServer\n'))
        self.assertIn('baseurl=https://download.docker.com/linux/fedora/$releasever/$basearch/stable', repo)
        self.assertIn('gpgkey=https://download.docker.com/linux/fedora/gpg', repo)
        self.assertEqual(self.fetched, ['https://download.docker.com/linux/fedora/44/x86_64/stable/repodata/repomd.xml'])
        installs = [argv for argv in dnf.calls if argv[:2] == ['dnf', 'install']]
        self.assertEqual(installs, [['dnf', 'install', '-y', 'docker-ce', 'docker-ce-cli', 'containerd.io',
                                     'docker-buildx-plugin', 'docker-compose-plugin']])
        self.assertEqual(dnf.enabled(), [])      # Docker never starts before its drive is ready

    def test_someone_elses_repository_definition_is_used_as_it_is(self):
        theirs = self.root / 'etc/yum.repos.d/docker-ce.repo'
        theirs.write_text('[docker-ce-stable]\nbaseurl=https://download.docker.com/linux/fedora/$releasever/$basearch/stable\n')
        self.family().install(['docker'])
        self.assertFalse((self.root / 'etc/yum.repos.d/nekoserver-docker-ce.repo').exists())
        self.assertEqual(self.fetched, [])

    def test_a_fedora_release_docker_does_not_package_yet_is_named(self):
        def missing(url):
            raise host.urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        family = self.family(fetch=missing)
        with self.assertRaisesRegex(RuntimeError, 'Docker does not offer packages for Fedora 44 yet'):
            family.install(['docker'])
        self.assertFalse((self.root / 'etc/yum.repos.d/nekoserver-docker-ce.repo').exists())

    def test_fedoras_own_docker_is_never_mixed_in(self):
        dnf = FakeDnf(installed={'moby-engine'})
        family = self.family(dnf)
        with self.assertRaisesRegex(RuntimeError, 'its own container packages.*moby-engine.*sudo dnf remove moby-engine'):
            family.install(['docker'])
        self.assertFalse(any(argv[:2] == ['dnf', 'install'] for argv in dnf.calls))

    def test_samba_example_is_set_aside_only_when_unchanged_and_new(self):
        dnf = FakeDnf(offered={'samba'})
        self.family(dnf).install(['samba'])
        self.assertFalse((self.root / 'etc/samba/smb.conf').exists())
        self.assertEqual((self.root / 'etc/samba/smb.conf.packaged').read_text(), '# example\n')
        # A file rpm reports as changed is someone's settings.
        (self.root / 'etc/samba/smb.conf').write_text('[global]\n')
        dnf.changed = {'/etc/samba/smb.conf'}
        self.assertFalse(self.family(dnf).packaged_example('/etc/samba/smb.conf'))
        dnf.changed = set()
        self.assertTrue(self.family(dnf).packaged_example('/etc/samba/smb.conf'))
        dnf.owners = {}
        self.assertFalse(self.family(dnf).packaged_example('/etc/samba/smb.conf'))


class RhelTest(unittest.TestCase):
    """RHEL and its rebuilds: Fedora's adapter with other makers' repositories and EPEL.
    The release values are AlmaLinux 10.2's and RHEL 10's own."""
    ALMA = {'ID': 'almalinux', 'ID_LIKE': 'rhel centos fedora', 'VERSION_ID': '10.2', 'NAME': 'AlmaLinux'}
    RHEL = {'ID': 'rhel', 'ID_LIKE': 'fedora', 'VERSION_ID': '10.0', 'NAME': 'Red Hat Enterprise Linux'}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for path in ('etc/yum.repos.d', 'etc/samba', 'run'):
            (self.root / path).mkdir(parents=True)
        patcher = mock.patch('host.shutil.which', return_value='/usr/bin/dnf')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.fetched = []

    def family(self, dnf=None, release=None, fetch=None):
        dnf = dnf or FakeDnf()
        dnf.root = self.root
        family = host.Rhel(dnf, release or self.ALMA,
                           fetch=fetch or (lambda url: self.fetched.append(url) or b'<repomd/>'))
        family.root = self.root
        return family

    def repo(self, name):
        return (self.root / f'etc/yum.repos.d/nekoserver-{name}.repo').read_text()

    def installs(self, dnf):
        return [argv[3:] for argv in dnf.calls if argv[:2] == ['dnf', 'install']]

    def test_a_rebuild_gets_dockers_centos_packages_and_tailscales_for_its_major_release(self):
        dnf = FakeDnf()
        self.family(dnf).install(['docker', 'docker-compose', 'tailscale'])
        self.assertIn('baseurl=https://download.docker.com/linux/centos/$releasever/$basearch/stable', self.repo('docker-ce'))
        self.assertIn('baseurl=https://pkgs.tailscale.com/stable/rhel/10/$basearch', self.repo('tailscale'))
        # "10.2" is release 10 to both makers.
        self.assertEqual(self.fetched, ['https://download.docker.com/linux/centos/10/x86_64/stable/repodata/repomd.xml',
                                        'https://pkgs.tailscale.com/stable/rhel/10/x86_64/repodata/repomd.xml'])
        self.assertEqual(dnf.enabled(), [])      # nothing starts before its drive is ready

    def test_rhel_itself_gets_dockers_rhel_packages(self):
        self.family(release=self.RHEL).install(['docker'])
        self.assertIn('baseurl=https://download.docker.com/linux/rhel/$releasever/$basearch/stable', self.repo('docker-ce'))
        self.assertIn('gpgkey=https://download.docker.com/linux/rhel/gpg', self.repo('docker-ce'))

    def test_caddy_and_qrencode_come_from_epel_added_first(self):
        dnf = FakeDnf(offered={'caddy', 'qrencode', 'samba'})
        self.family(dnf).install(['samba', 'caddy', 'qrencode'])
        # A rebuild's own repositories carry EPEL's release package.
        self.assertEqual(self.installs(dnf), [['epel-release'], ['samba', 'caddy', 'qrencode']])

    def test_rhel_itself_gets_epel_from_epels_site(self):
        dnf = FakeDnf(offered={'caddy'})
        self.family(dnf, release=self.RHEL).install(['caddy'])
        self.assertEqual(self.installs(dnf)[0],
                         ['https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm'])

    def test_epel_already_there_is_used_as_it_is(self):
        for installed, repo in (({'epel-release'}, None),
                                (set(), 'metalink=https://mirrors.fedoraproject.org/metalink?repo=epel-10&arch=$basearch\n')):
            with self.subTest(installed=installed):
                if repo:
                    (self.root / 'etc/yum.repos.d/epel.repo').write_text('[epel]\n' + repo)
                dnf = FakeDnf(offered={'caddy'}, installed=installed)
                self.family(dnf).install(['caddy'])
                self.assertEqual(self.installs(dnf), [['caddy']])

    def test_nothing_from_epel_needed_adds_no_epel(self):
        dnf = FakeDnf(offered={'samba'})
        self.family(dnf).install(['samba'])
        self.assertEqual(self.installs(dnf), [['samba']])

    def test_runc_and_podman_docker_are_never_mixed_with_dockers_packages(self):
        for clash in ('runc', 'podman-docker'):
            with self.subTest(clash=clash):
                dnf = FakeDnf(installed={clash})
                family = self.family(dnf)
                with self.assertRaisesRegex(RuntimeError, f'its own container packages.*{clash}.*sudo dnf remove {clash}'):
                    family.install(['docker'])
                self.assertEqual(self.installs(dnf), [])

    def test_a_release_docker_does_not_package_yet_is_named(self):
        def missing(url):
            raise host.urllib.error.HTTPError(url, 404, 'Not Found', {}, None)
        future = dict(self.ALMA, VERSION_ID='11.0')
        family = self.family(release=future, fetch=missing)
        with self.assertRaisesRegex(RuntimeError, 'Docker does not offer packages for AlmaLinux 11 yet'):
            family.install(['docker'])


class FakeZypper(FakeDnf):
    """zypper over sets of offered and installed packages; rpm and systemctl as FakeDnf."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.zypper_envs = []     # the environment of each zypper call, in order

    def __call__(self, argv, **kwargs):
        if argv[0] == 'zypper':
            self.calls.append(argv)
            self.zypper_envs.append(kwargs.get('env') or {})
            if 'install' in argv:
                return self.zypper_install(argv)
            # search --match-exact: 104 when nothing matches.
            return subprocess.CompletedProcess(argv, 0 if argv[-1] in self.offered else 104, '', '')
        return super().__call__(argv, **kwargs)

    def zypper_install(self, argv):
        self.masked_during_install = set(self.masked)
        names = argv[argv.index('--no-recommends') + 1:]
        done = names if self.fail_after is None else names[:self.fail_after]
        for name in done:
            self.installed.add(name)
            for unit in FEDORA_UNITS.get(name, ()):
                self.units[unit] = {'LoadState': 'loaded', 'UnitFileState': 'enabled', 'ActiveState': 'inactive'}
            if name == 'samba':
                example = self.root / 'etc/samba/smb.conf'
                if not example.exists():
                    example.write_text('# example\n')
        failed = len(done) < len(names)
        return subprocess.CompletedProcess(argv, 4 if failed else 0, '', 'Problem: nothing provides' if failed else '')


class SuseTest(unittest.TestCase):
    """openSUSE: Fedora's rpm handling with zypper, and only openSUSE's own repositories."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for path in ('etc/zypp/repos.d', 'etc/samba', 'run'):
            (self.root / path).mkdir(parents=True)
        patcher = mock.patch('host.shutil.which', return_value='/usr/bin/zypper')
        patcher.start()
        self.addCleanup(patcher.stop)

    def family(self, zypper=None):
        zypper = zypper or FakeZypper()
        zypper.root = self.root
        family = host.Suse(zypper, {'ID': 'opensuse-leap', 'ID_LIKE': 'suse opensuse', 'VERSION_ID': '16.0'},
                           fetch=lambda url: self.fail('nothing is downloaded on openSUSE: ' + url))
        family.root = self.root
        return family

    def test_everything_comes_from_opensuses_own_repositories(self):
        zypper = FakeZypper(offered={'docker', 'docker-buildx', 'docker-compose', 'tailscale', 'caddy', 'samba'})
        family = self.family(zypper)
        self.assertTrue(family.available('docker'))
        self.assertTrue(family.available('tailscale'))
        self.assertFalse(family.available('openrgb'))
        self.assertEqual(family.install(['docker', 'docker-compose', 'tailscale', 'caddy', 'samba']),
                         ['docker', 'docker-compose', 'tailscale', 'caddy', 'samba'])
        installs = [argv for argv in zypper.calls if 'install' in argv]
        self.assertEqual(installs, [['zypper', '--non-interactive', 'install', '--no-recommends',
                                     'docker', 'docker-buildx', 'docker-compose', 'tailscale', 'caddy', 'samba']])
        self.assertEqual(list((self.root / 'etc/zypp/repos.d').iterdir()), [])   # no maker's repository written
        # New services stay off, as on Fedora.
        self.assertLessEqual({'smb.service', 'caddy.service', 'docker.service'}, zypper.masked_during_install)
        self.assertEqual(zypper.enabled(), [])
        self.assertEqual(zypper.masked, set())

    def test_lighting_is_installed_under_opensuses_spelling(self):
        # Tumbleweed offers OpenRGB; zypper's search would find it for "openrgb" too (it
        # ignores case), and the install would then fail with "Did you mean OpenRGB?".
        zypper = FakeZypper(offered={'OpenRGB'})
        family = self.family(zypper)
        self.assertTrue(family.available('openrgb'))
        self.assertEqual(family.install(['openrgb']), ['openrgb'])
        searches = [argv for argv in zypper.calls if 'search' in argv]
        self.assertEqual(searches[0][-1], 'OpenRGB')
        self.assertIn('--case-sensitive', searches[0])
        self.assertEqual([argv for argv in zypper.calls if 'install' in argv],
                         [['zypper', '--non-interactive', 'install', '--no-recommends', 'OpenRGB']])

    def test_zypper_is_told_to_wait_for_another_zypper(self):
        zypper = FakeZypper(offered={'samba'})
        family = self.family(zypper)
        self.assertTrue(family.available('samba'))
        family.install(['samba'])
        self.assertEqual(len(zypper.zypper_envs), 2)      # the search and the install
        for env in zypper.zypper_envs:
            self.assertEqual(env.get('ZYPP_LOCK_TIMEOUT'), '600')
            self.assertEqual(env.get('LC_ALL'), 'C')

    def test_a_failed_install_names_the_packages(self):
        family = self.family(FakeZypper(offered={'samba', 'caddy'}, fail_after=1))
        with self.assertRaisesRegex(RuntimeError, 'Installing samba, caddy failed: Problem: nothing provides'):
            family.install(['samba', 'caddy'])


class Flaky:
    """A package manager whose first answers fail with a given message (a mirror that
    did not answer, say), then behaves like the real fake."""
    def __init__(self, inner, failures, message, when=lambda argv: True):
        self.inner, self.failures, self.message, self.when = inner, failures, message, when
        self.failed = 0

    def __call__(self, argv, **kwargs):
        if self.failed < self.failures and self.when(argv):
            self.failed += 1
            return subprocess.CompletedProcess(argv, 1, '', self.message)
        return self.inner(argv, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


class RetryTest(unittest.TestCase):
    """A package source that did not answer is asked again a little later; any other
    failure is reported at once."""
    def setUp(self):
        patcher = mock.patch('host.shutil.which', return_value='/usr/bin/pacman')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for path in ('var/lib/pacman', 'usr/sbin', 'etc/apt/sources.list.d', 'etc/samba', 'var/lib/ucf'):
            (Path(self.tmp.name) / path).mkdir(parents=True)
        root = mock.patch.object(host.Arch, 'root', Path(self.tmp.name))
        root.start()
        self.addCleanup(root.stop)
        self.slept, self.said = [], []

    def debian(self, apt):
        family = host.Debian(apt, {'ID': 'debian', 'VERSION_CODENAME': 'trixie'}, fetch=lambda url: b'')
        family.root = Path(self.tmp.name)
        family.sleep = self.slept.append
        family.say = self.said.append
        return family

    def family(self, run):
        family = host.Arch(run)
        family.sleep = self.slept.append
        family.say = self.said.append
        return family

    def test_a_mirror_that_did_not_answer_is_tried_again(self):
        pacman = Flaky(FakePacman(offered={'caddy'}), 1,
                       "error: failed retrieving file 'extra.db' from mirror.example : Connection timed out\nerror: failed to synchronize all databases",
                       when=lambda argv: argv[1] == '-S')
        family = self.family(pacman)
        self.assertEqual(family.install(['caddy']), ['caddy'])
        self.assertEqual(self.slept, [host.RETRY_PAUSE])
        self.assertEqual(len(self.said), 1)
        self.assertIn('did not answer', self.said[0])
        self.assertIn('2 of 3', self.said[0])
        self.assertEqual(len([argv for argv in pacman.calls if argv[1] == '-S']), 1)   # the second attempt reached pacman

    def test_any_other_failure_is_reported_at_once(self):
        pacman = Flaky(FakePacman(offered={'caddy'}), 5, 'error: target not found: caddy', when=lambda argv: argv[1] == '-S')
        family = self.family(pacman)
        with self.assertRaisesRegex(RuntimeError, 'Installing caddy failed: error: target not found: caddy'):
            family.install(['caddy'])
        self.assertEqual(self.slept, [])
        self.assertEqual(self.said, [])

    def test_a_source_that_never_answers_gives_up_after_three_attempts(self):
        pacman = Flaky(FakePacman(offered={'caddy'}), 5, 'error: failed to download: Could not resolve host: mirror.example',
                       when=lambda argv: argv[1] == '-S')
        family = self.family(pacman)
        with self.assertRaisesRegex(RuntimeError, 'Installing caddy failed: .*Could not resolve host'):
            family.install(['caddy'])
        self.assertEqual(self.slept, [host.RETRY_PAUSE, host.RETRY_PAUSE])
        self.assertEqual(pacman.failed, host.RETRIES)

    def test_what_counts_as_a_source_not_answering(self):
        for text in ('E: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  Temporary failure resolving',
                     'Errors during downloading metadata for repository \'updates\':\n  - Curl error (28): Timeout was reached',
                     "Download (curl) error for 'http://download.opensuse.org/x.rpm':\nError code: HTTP response: 404",
                     'error: failed retrieving file \'core.db\' from mirror : Operation too slow',
                     'E: Failed to fetch http://x  Hash Sum mismatch'):
            self.assertTrue(host.transient(subprocess.CompletedProcess([], 1, '', text)), text)
        for text in ('error: target not found: caddy', 'E: Unable to locate package caddy',
                     'Problem: nothing provides libfoo needed by caddy', 'E: Sub-process /usr/bin/dpkg returned an error code (1)'):
            self.assertFalse(host.transient(subprocess.CompletedProcess([], 1, '', text)), text)
        self.assertFalse(host.transient(subprocess.CompletedProcess([], 0, 'Connection timed out but retried fine', '')))

    def test_the_apt_refresh_is_tried_again_too(self):
        apt = Flaky(FakeApt(offered={'samba'}, root=Path(self.tmp.name)), 1,
                    'E: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  Could not resolve',
                    when=lambda argv: argv[-1] == 'update')
        family = self.debian(apt)
        with mock.patch('host.shutil.which', return_value='/usr/bin/apt-get'):
            self.assertTrue(family.available('samba'))
        self.assertEqual(self.slept, [host.RETRY_PAUSE])
        self.assertEqual(apt.calls.count(['apt-get', 'update']), 1)

    def test_two_names_for_one_package_are_installed_once(self):
        apt = FakeApt(offered={'mesa-vulkan-drivers', 'bluez'}, root=Path(self.tmp.name))
        family = self.debian(apt)
        with mock.patch('host.shutil.which', return_value='/usr/bin/apt-get'):
            # Intel and AMD graphics both: Mesa's one Vulkan package; bluez twice over.
            family.install(['vulkan-intel', 'vulkan-radeon', 'bluez', 'bluez-utils'])
        installs = [argv for argv in apt.calls if argv[:2] == ['apt-get', 'install']]
        self.assertEqual(installs[0][5:], ['mesa-vulkan-drivers', 'bluez'])


# A package manager that, asked to stop as Ctrl+C asks, notes it and stops; SIGTERM too.
POLITE_TOOL = ('#!' + sys.executable + '\n'
               'import pathlib, signal, sys, time\n'
               'def stop(number, frame):\n'
               '    pathlib.Path(sys.argv[1]).write_text(signal.Signals(number).name)\n'
               '    sys.exit(130)\n'
               'signal.signal(signal.SIGINT, stop if sys.argv[2] == "int" else signal.SIG_IGN)\n'
               'signal.signal(signal.SIGTERM, stop)\n'
               'print("working", flush=True)\n'
               'time.sleep(30)\n')


class PatientStopTest(unittest.TestCase):
    """A package manager that runs out of time is asked to stop and waited for, never
    killed: killed, pacman leaves its lock behind and dnf or zypper stop half-way."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tool = Path(self.tmp.name) / 'pacman'
        self.tool.write_text(POLITE_TOOL)
        self.tool.chmod(0o755)
        self.note = Path(self.tmp.name) / 'asked'

    def test_a_package_manager_that_runs_out_of_time_is_asked_to_stop(self):
        family = host.Arch(release={})
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            family._call([str(self.tool), str(self.note), 'int'], 2)
        self.assertEqual(self.note.read_text(), 'SIGINT')        # asked as Ctrl+C asks, not killed
        self.assertIn('working', caught.exception.stdout)

    def test_one_that_ignores_ctrl_c_gets_sigterm_after_the_grace_time(self):
        with mock.patch.object(host, 'STOP_GRACE', 1), self.assertRaises(subprocess.TimeoutExpired):
            host.run_command([str(self.tool), str(self.note), 'ignore'], capture_output=True, text=True, timeout=2)
        self.assertEqual(self.note.read_text(), 'SIGTERM')

    def test_other_commands_and_finished_ones_are_as_with_subprocess_run(self):
        done = host.run_command(['true'], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0)
        quick = Path(self.tmp.name) / 'dnf'
        quick.write_text('#!/bin/sh\necho listed\nexit 3\n')
        quick.chmod(0o755)
        result = host.run_command([str(quick)], capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual((result.returncode, result.stdout), (3, 'listed\n'))
        with self.assertRaises(subprocess.CalledProcessError):
            host.run_command([str(quick)], capture_output=True, text=True, timeout=30, check=True)


class HangupTest(unittest.TestCase):
    """host.py run by install.sh: a dropped SSH connection (SIGHUP) or a shutdown (SIGTERM)
    stops it as Ctrl+C does, so its cleanup still runs."""
    def test_a_hangup_runs_the_cleanup_and_stops_as_ctrl_c_does(self):
        import signal
        for number in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signal=number.name):
                self.hang_up(number)

    def hang_up(self, number):
        import signal
        cleaned, seen = [], []

        def bootstrap():
            try:
                os.kill(os.getpid(), number)
                time.sleep(5)            # the signal arrives here
            finally:
                cleaned.append(True)

        def mine(*args):
            seen.append(args)
        # Without host.py's own handler the signal only lands here (instead of ending the tests).
        before = signal.signal(number, mine)
        self.addCleanup(signal.signal, number, before)
        with mock.patch.object(host, 'bootstrap', bootstrap), mock.patch('sys.stderr'):
            self.assertEqual(host.main(['bootstrap']), 130)
        self.assertEqual(cleaned, [True])
        self.assertEqual(seen, [])
        self.assertIs(signal.getsignal(number), mine)   # put back as it was


class StaleLockTest(unittest.TestCase):
    """pacman's lock that no program has open was left by a pacman that was cut off: it
    goes at once instead of being waited for in silence."""
    def setUp(self):
        patcher = mock.patch('host.shutil.which', return_value='/usr/bin/pacman')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lock = self.root / 'var/lib/pacman/db.lck'
        self.lock.parent.mkdir(parents=True)
        self.lock.write_text('')
        self.said, self.slept = [], []

    def process(self, pid, *targets):
        fds = self.root / 'proc' / str(pid) / 'fd'
        fds.mkdir(parents=True)
        for number, target in enumerate(targets):
            (fds / str(number)).symlink_to(target)

    def family(self, pacman):
        family = host.Arch(pacman)
        family.root, family.say, family.sleep = self.root, self.said.append, self.slept.append
        return family

    def test_a_lock_no_program_has_open_is_removed_and_the_install_runs(self):
        self.process(1, '/dev/null', '/var/log/pacman.log')
        pacman = FakePacman(offered={'caddy'})
        self.assertEqual(self.family(pacman).install(['caddy']), ['caddy'])
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.slept, [])
        self.assertIn('was left by a pacman that was cut off', self.said[0])

    def test_a_lock_a_program_has_open_is_waited_for_and_said_so(self):
        self.process(1, '/dev/null')
        self.process(2, '/dev/null', host.PACMAN_LOCK)        # an updater using libalpm
        family = self.family(FakePacman(offered={'caddy'}))

        def sleep(seconds):
            self.slept.append(seconds)
            if len(self.slept) == 3:
                self.lock.unlink()
        family.sleep = sleep
        self.assertEqual(family.install(['caddy']), ['caddy'])
        self.assertEqual(self.slept, [5, 5, 5])
        self.assertEqual(len(self.said), 1)
        self.assertIn('waiting for it to finish (up to 10 minutes)', self.said[0])

    def test_without_a_look_at_the_processes_the_lock_is_waited_for(self):
        family = self.family(FakePacman(offered={'caddy'}))   # no /proc to look at
        family.wait = 0
        with self.assertRaisesRegex(RuntimeError, 'pacman is still locked'):
            family.install(['caddy'])
        self.assertTrue(self.lock.exists())


class Answers:
    """Wraps a fake package manager: answers given in sequence for matching commands
    (exit code, what it said), then the inner fake's own; every call's input recorded."""
    def __init__(self, inner, when, answers):
        self.inner, self.when, self.answers, self.inputs = inner, when, list(answers), []

    def __call__(self, argv, **kwargs):
        self.inputs.append((argv, kwargs.get('input')))
        if self.answers and self.when(argv):
            code, said = self.answers.pop(0)
            return subprocess.CompletedProcess(argv, code, '', said)
        return self.inner(argv, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def __setattr__(self, name, value):
        if name in ('inner', 'when', 'answers', 'inputs'):
            object.__setattr__(self, name, value)
        else:
            setattr(self.inner, name, value)


def is_update(argv):
    return argv[0] == 'apt-get' and argv[-1] == 'update'


class PackageReviewTest(unittest.TestCase):
    """apt: other programs' locks waited out, sources that only warned tried again, keys
    checked, Steam's question answered, a wrong clock named."""
    setUp, family, file, journal = DebianTest.setUp, DebianTest.family, DebianTest.file, DebianTest.journal

    def quiet(self, family):
        family.said, family.slept = [], []
        family.say, family.sleep = family.said.append, family.slept.append
        return family

    def test_a_lock_held_by_automatic_updates_is_waited_out(self):
        busy = 'E: Could not get lock /var/lib/apt/lists/lock. It is held by process 4242 (unattended-upgr)'
        apt = Answers(FakeApt(offered={'samba'}), is_update, [(100, busy)] * 4)
        family = self.quiet(self.family(apt))
        family.refresh()
        updates = [argv for argv, _ in apt.inputs if is_update(argv)]
        self.assertEqual(len(updates), 5)                       # four waits, then it ran (more than 3 attempts)
        self.assertEqual(family.slept, [host.RETRY_PAUSE] * 4)
        self.assertEqual(len(family.said), 1)
        self.assertIn('Another program is installing or updating packages', family.said[0])

    def test_dnf5s_transaction_lock_is_waited_out_too(self):
        self.assertTrue(host.busy(subprocess.CompletedProcess([], 1, '', 'Failed to obtain rpm transaction lock. '
                                                                          'Another transaction is in progress.')))
        self.assertTrue(host.busy(subprocess.CompletedProcess([], 1, '', 'error: failed to init transaction '
                                                                          '(unable to lock database)')))
        self.assertFalse(host.busy(subprocess.CompletedProcess([], 0, 'Could not get lock', '')))

    def test_a_source_apt_only_warned_about_is_tried_again(self):
        warned = ('W: Failed to fetch https://download.docker.com/linux/ubuntu/dists/noble/InRelease  Could not '
                  'resolve download.docker.com\nW: Some index files failed to download. They have been ignored, '
                  'or old ones used instead.')
        apt = Answers(FakeApt(offered={'samba'}), is_update, [(0, warned)])
        family = self.quiet(self.family(apt))
        family.refresh()
        self.assertEqual(len([argv for argv, _ in apt.inputs if is_update(argv)]), 2)
        self.assertEqual(family.slept, [host.RETRY_PAUSE])
        # One that never answers: three attempts, then setup goes on with the lists from before, and says so.
        apt = Answers(FakeApt(offered={'samba'}), is_update, [(0, warned)] * 3)
        family = self.quiet(self.family(apt))
        family.refresh()
        self.assertEqual(len([argv for argv, _ in apt.inputs if is_update(argv)]), 3)
        self.assertIn('Some package sources could not be reached (Failed to fetch https://download.docker.com',
                      family.said[-1])
        self.assertTrue(family.refreshed)

    def test_a_damaged_key_file_is_downloaded_again(self):
        family = self.family()
        family.add_repository(host.DOCKER)
        key = self.root / 'etc/apt/keyrings/docker.asc'
        for damaged in (b'<html>Sign in to the Wi-Fi</html>', KEY[:40], b''):
            with self.subTest(damaged=damaged):
                key.write_bytes(damaged)
                self.assertTrue(self.family().add_repository(host.DOCKER))
                self.assertEqual(key.read_bytes(), KEY)
        self.assertFalse(self.family().add_repository(host.DOCKER))
        # A binary key (Tailscale's noarmor.gpg) is a key too.
        self.assertTrue(host.valid_key(b'\x99\x02\x0d\x04abc'))
        self.assertTrue(host.valid_key(b'\xc6\x33\x04abc'))
        self.assertFalse(host.valid_key(b'\x00\x01'))

    def test_a_downloaded_page_that_is_no_key_is_refused(self):
        family = self.family()
        family.fetch = lambda url: b'<html><body>Please log in</body></html>'
        with self.assertRaisesRegex(RuntimeError, 'is not a key file'):
            family.add_repository(host.DOCKER)
        self.assertFalse((self.root / 'etc/apt/keyrings/docker.asc').exists())

    def test_a_key_apt_rejects_is_downloaded_again_and_the_lists_refreshed(self):
        family = self.family()
        family.add_repository(host.DOCKER)
        self.keys.clear()
        rejected = ('W: GPG error: https://download.docker.com/linux/ubuntu noble InRelease: The following signatures '
                    "couldn't be verified because the public key is not available: NO_PUBKEY 7EA0A9C3F273FCD8\n"
                    "E: The repository 'https://download.docker.com/linux/ubuntu noble InRelease' is not signed.")
        apt = Answers(FakeApt(offered={'samba'}), is_update, [(100, rejected)])
        family = self.quiet(self.family(apt))
        family.refresh()
        self.assertEqual(self.keys, ['https://download.docker.com/linux/ubuntu/gpg'])
        self.assertEqual(len([argv for argv, _ in apt.inputs if is_update(argv)]), 2)
        self.assertIn('downloading the key again', family.said[0])
        # Someone else's repository's key is never touched.
        self.keys.clear()
        other = rejected.replace('download.docker.com', 'ppa.example.org')
        apt = Answers(FakeApt(), is_update, [(100, other)])
        family = self.quiet(self.family(apt))
        with self.assertRaisesRegex(RuntimeError, 'Refreshing the package lists failed'):
            family.refresh()
        self.assertEqual(self.keys, [])

    def test_steams_licence_question_is_answered_before_it_installs(self):
        apt = Answers(FakeApt(offered={'steam-installer'}), lambda argv: False, [])
        family = self.quiet(self.family(apt))
        family.install(['steam'])
        calls = [argv for argv, _ in apt.inputs]
        answer = calls.index(['debconf-set-selections'])
        self.assertLess(answer, next(i for i, argv in enumerate(calls) if argv[0] == 'apt-get' and 'install' in argv))
        self.assertIn('steam/question select I AGREE', apt.inputs[answer][1])
        self.assertIn('Steam licence', family.said[0])
        # Nothing to answer for packages that ask nothing.
        apt = Answers(FakeApt(offered={'caddy'}), lambda argv: False, [])
        self.quiet(self.family(apt)).install(['caddy'])
        self.assertNotIn(['debconf-set-selections'], [argv for argv, _ in apt.inputs])

    def test_steam_left_half_set_up_is_answered_before_it_is_set_up_again(self):
        apt = Answers(FakeApt(installed={'steam-installer'}), lambda argv: False, [])
        apt.unconfigured = ['steam-installer']
        self.file(host.Debian.journal, json.dumps({'format': 'neko-package-install-v1', 'packages': ['steam'],
                                                    'before': {}}))
        self.quiet(self.family(apt)).reconcile()
        calls = [argv for argv, _ in apt.inputs]
        self.assertLess(calls.index(['debconf-set-selections']), calls.index(['dpkg', '--configure', 'steam-installer']))
        self.assertFalse(self.journal().exists())

    def test_a_package_that_cannot_be_set_up_is_named_with_the_way_out(self):
        apt = FakeApt(installed={'steam-installer'})
        apt.unconfigured = ['steam-installer']
        apt.configure_fails = True
        self.file(host.Debian.journal, json.dumps({'format': 'neko-package-install-v1', 'packages': ['steam'],
                                                    'before': {}}))
        family = self.quiet(self.family(apt))
        with self.assertRaisesRegex(RuntimeError, r'could not finish setting up steam-installer \(left unfinished by '
                                                  r'an earlier installation\).*sudo apt-get remove steam-installer'):
            family.install(['caddy'])

    def test_a_wrong_clock_is_named(self):
        not_yet = ('E: Release file for http://deb.debian.org/debian/dists/trixie/InRelease is not valid yet '
                   '(invalid for another 5h 2min).')
        apt = Answers(FakeApt(), is_update, [(100, not_yet)])
        family = self.quiet(self.family(apt))
        with self.assertRaisesRegex(RuntimeError, r"clock looks wrong.*sudo timedatectl set-ntp true"):
            family.refresh()
        family = self.family()
        family.fetch = mock.Mock(side_effect=OSError('[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: '
                                                     'certificate is not yet valid'))
        with self.assertRaisesRegex(RuntimeError, 'clock looks wrong'):
            family.add_repository(host.DOCKER)
        self.assertEqual(host.clock_hint('E: Unable to locate package caddy'), '')

    def test_long_waits_are_said_when_they_start(self):
        lock = self.root / host.PACKAGE_LOCK.lstrip('/')
        lock.parent.mkdir(parents=True, exist_ok=True)
        import fcntl
        with lock.open('a') as other:                       # another setup's package job
            fcntl.flock(other, fcntl.LOCK_EX)
            family = self.quiet(self.family())

            def sleep(seconds):
                family.slept.append(seconds)
                if len(family.slept) == 2:
                    fcntl.flock(other, fcntl.LOCK_UN)
            family.sleep = sleep
            with family.locked():
                pass
        self.assertEqual(len(family.said), 1)
        self.assertIn('Another NekoServer setup is installing packages; waiting', family.said[0])


if __name__ == '__main__':
    unittest.main()
