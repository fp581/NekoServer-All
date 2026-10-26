"""How the pieces connect: the real install.sh preflight (real df), and every runtime
command the installer writes into a systemd unit, checked against the Rust program's
own option table and, when it matches its sources, the bundled binary itself."""
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gateway_tools  # noqa: E402
import host  # noqa: E402
from modules import (app_backup, dashboard, docker, heartbeat, host_backup, maintenance,  # noqa: E402
                     media_monitor, music_backup, notifications, photo_backup,
                     photo_preservation, server_os)

BIN = Path('/usr/local/lib/nekoserver/nekoserver-runtime')
RUNTIME = ROOT / 'runtime'


def config():
    cfg = json.loads((ROOT / 'config.json').read_text())
    cfg['storage'] = {'data_ssd': {'uuid': 'aaaa-1111', 'fstype': 'ext4'},
                      'media_hdd': {'uuid': 'bbbb-2222', 'fstype': 'ext4'}}
    cfg['timezone'] = 'UTC'
    return cfg


def unit_texts():
    """Every systemd unit text the installer generates that runs the runtime."""
    cfg = config()
    etc = Path('/etc/nekoserver-backups/x.json')
    texts = {}
    for name, made in (
        ('dashboard', dashboard.units(Path('/srv/nekoserver'), BIN.parent, etc, 8787, 60, 'neko.tail1.ts.net')),
        ('dashboard-local', dashboard.units(Path('/srv/nekoserver'), BIN.parent, etc, 8787, 60, None)),
        ('app_backup', app_backup.definitions(cfg, BIN, etc)),
        ('heartbeat', heartbeat.definitions(cfg, BIN, etc)),
        ('host_backup', host_backup.definitions(cfg, BIN, etc)),
        ('maintenance', maintenance.definitions(cfg, BIN, etc)),
        ('music_backup', music_backup.definitions(cfg, BIN, etc)),
        ('notifications', notifications.definitions(BIN, etc)),
    ):
        for unit, text in made.items():
            texts[name + ':' + unit] = text
    texts['server_os'] = server_os.unit(BIN, etc, cfg)
    texts['gateway'] = gateway_tools.unit(cfg, BIN.parent, Path('/srv/nekoserver/gateway/state'))
    texts['docker'] = docker.guard_contents(cfg, BIN.parent)
    texts['photo_preservation'] = 'ExecStart=' + photo_preservation.command_line(
        BIN, etc, '/srv/nekoserver', '/mnt/nekoserver-media', 600, 'photos/archive', 8) + ' --db-backup'
    texts['photo_preservation_resume'] = photo_preservation.resume_unit(BIN, etc, '/srv/nekoserver')
    texts['photo_preservation_service'] = photo_preservation.service_text(
        'nekoserver-immich-db-backup', photo_preservation.command_line(
            BIN, etc, '/srv/nekoserver', '/mnt/nekoserver-media', 600, 'photos/archive', 8) + ' --db-backup',
        '/srv/nekoserver', '/mnt/nekoserver-media', BIN, etc)
    texts['photo_backup'] = 'ExecStart=' + photo_backup.command_line(
        cfg, BIN, etc, 'cccc-3333', 'exfat', '/mnt/nekoserver-media/photos/archive', '/mnt/nekoserver-photos')
    texts['media_monitor'] = 'ExecStart=' + media_monitor.command_line(BIN, etc, '/srv/nekoserver/reports/media/latest.json')
    return texts


def runtime_commands():
    """(where, command, [--options...], full argv after the binary) for each runtime call."""
    out = []
    for where, text in unit_texts().items():
        for line in text.splitlines():
            match = re.match(r'(ExecStart|ExecStartPre|ExecStopPost)=[+-]?(.*)', line)
            if not match:
                continue
            argv = shlex.split(match.group(2))
            if not argv or not argv[0].endswith('nekoserver-runtime'):
                continue
            options = [a[2:] for a in argv[2:] if a.startswith('--')]
            out.append((where, argv[1], options, argv[1:]))
    return out


def option_table():
    """The runtime's own {command: allowed options}, read from main.rs."""
    source = (RUNTIME / 'src/main.rs').read_text()
    block = source[source.index('let allowed = match mode.as_str() {'):source.index('_ => return Err(Error::new("Unknown command"))')]
    table = {}
    for names, options in re.findall(r'((?:"[a-z-]+"\s*\|?\s*)+)=>\s*\{?\s*vec!\[([^\]]*)\]', block):
        for name in re.findall(r'"([a-z-]+)"', names):
            table[name] = set(re.findall(r'"([a-z-]+)"', options))
    return table


def bundled_binary_current():
    """The bundled binary, if build.json says it was built from these exact sources."""
    binary = RUNTIME / 'bin/nekoserver-runtime'
    if not (RUNTIME / 'build.json').is_file():
        return None            # not built here (install.sh or runtime/build.py builds it)
    manifest = json.loads((RUNTIME / 'build.json').read_text())
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    sources = {str(p.relative_to(RUNTIME)) for p in (RUNTIME / 'src').glob('*.rs')}
    if (not binary.is_file() or digest(binary) != manifest.get('binary_sha256')
            or sources - set(manifest.get('source_sha256', {}))
            or any(digest(RUNTIME / rel) != sha for rel, sha in manifest.get('source_sha256', {}).items())):
        return None
    return binary


class GeneratedCommandTests(unittest.TestCase):
    def test_a_backup_caught_up_at_boot_waits_for_its_resume_service(self):
        # Seen on Fedora: a night missed while the PC was off is caught up at boot, at the
        # same second the resume service runs with the same job lock; the backup failed
        # ("Job already running or unsafe lock") unless it is ordered after the resume.
        texts = unit_texts()
        for backup, resume in (('app_backup:nekoserver-app-backup.service', 'nekoserver-app-resume.service'),
                               ('host_backup:nekoserver-host-backup.service', 'nekoserver-host-resume.service')):
            with self.subTest(backup=backup):
                after = ' '.join(line.split('=', 1)[1] for line in texts[backup].splitlines()
                                 if line.startswith('After='))
                self.assertIn(resume, after.split())
                timer = texts[backup.replace('.service', '.timer')]
                self.assertIn('Persistent=true', timer)       # why the catch-up happens at boot

    def test_immich_is_started_again_however_its_weekly_backup_ends(self):
        # The weekly run pauses Immich: its unit's ExecStopPost and a boot-time service
        # start it again after a failure, a stop or a power cut.
        texts = unit_texts()
        self.assertIn('ExecStopPost=' + str(BIN) + ' immich-resume --config', texts['photo_preservation_service'])
        self.assertIn('WantedBy=multi-user.target', texts['photo_preservation_resume'])
        self.assertIn('After=docker.service', texts['photo_preservation_resume'])
        # It needs only the SSD (Immich's own drive): a HDD that dropped out mid-run does
        # not keep Immich stopped (QA-01).
        self.assertIn('RequiresMountsFor=/srv/nekoserver\n', texts['photo_preservation_resume'])
        self.assertNotIn('/mnt/nekoserver-media', texts['photo_preservation_resume'])

    def test_every_generated_runtime_command_uses_known_options(self):
        table = option_table()
        commands = runtime_commands()
        self.assertGreaterEqual({c for _, c, _, _ in commands},
                                {'serve', 'collect', 'weekly', 'preservation', 'app-snapshot', 'maintenance',
                                 'host-snapshot', 'hdd-policy', 'gateway-guard', 'storage-guard', 'media-health',
                                 'immich-resume'})
        for where, command, options, _ in commands:
            with self.subTest(unit=where, command=command):
                self.assertIn(command, table)
                self.assertLessEqual(set(options), table[command])

    def test_bundled_runtime_accepts_every_generated_command(self):
        binary = bundled_binary_current()
        if binary is None:
            self.skipTest('bundled runtime is being rebuilt for these sources (CI checks that separately)')
        if not os.access(binary, os.X_OK):
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        for where, command, _, argv in runtime_commands():
            with self.subTest(unit=where, command=command):
                # The parser validates every option before --help, then exits without running.
                result = subprocess.run([str(binary), *argv, '--help'], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)


# The deciding lines of real /etc/os-release files, and the family setup uses there
# (None: refused before any package manager runs).
RELEASES = (
    ('debian', 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nVERSION_ID="13"\nVERSION_CODENAME=trixie\nID=debian\n'),
    ('debian', 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\nID=debian\n'),
    ('debian', 'PRETTY_NAME="Ubuntu 24.04.3 LTS"\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\nID=ubuntu\nID_LIKE=debian\n'),
    ('debian', 'PRETTY_NAME="Ubuntu 26.04 LTS"\nVERSION_ID="26.04"\nID=ubuntu\nID_LIKE=debian\n'),
    ('debian', 'NAME="Linux Mint"\nID=linuxmint\nID_LIKE="ubuntu debian"\nVERSION_ID="22"\nUBUNTU_CODENAME=noble\n'),
    ('debian', 'NAME="LMDE"\nID=linuxmint\nID_LIKE=debian\nVERSION_ID="6"\n'),
    ('debian', 'NAME="Pop!_OS"\nID=pop\nID_LIKE="ubuntu debian"\nVERSION_ID="24.04"\n'),
    ('debian', 'NAME="elementary OS"\nID=elementary\nID_LIKE=ubuntu\nVERSION_ID="8"\n'),
    ('debian', 'NAME="Zorin OS"\nID=zorin\nID_LIKE="ubuntu debian"\nVERSION_ID="18"\n'),
    ('debian', 'NAME="Kali GNU/Linux"\nID=kali\nID_LIKE=debian\nVERSION_ID="2025.3"\n'),
    ('arch', 'NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\nID=arch\nBUILD_ID=rolling\n'),
    ('arch', 'NAME="Nyarch Linux"\nID=nyarch\nID_LIKE=arch\n'),
    ('arch', 'NAME="Manjaro Linux"\nID=manjaro\nID_LIKE=arch\nBUILD_ID=rolling\n'),
    ('arch', 'NAME="EndeavourOS"\nID=endeavouros\nID_LIKE=arch\nBUILD_ID=rolling\n'),
    ('arch', 'NAME="CachyOS Linux"\nID=cachyos\nID_LIKE=arch\nBUILD_ID=rolling\n'),
    ('arch', 'NAME="Garuda Linux"\nID=garuda\nID_LIKE=arch\n'),
    ('fedora', 'NAME="Fedora Linux"\nVERSION="44 (Workstation Edition)"\nID=fedora\nVERSION_ID=44\nVARIANT_ID=workstation\n'),
    ('fedora', 'NAME="Fedora Linux"\nVERSION="43 (Server Edition)"\nID=fedora\nVERSION_ID=43\nVARIANT_ID=server\n'),
    ('fedora', 'NAME="Fedora Linux"\nVERSION="42 (KDE Plasma Desktop Edition)"\nID=fedora\nVERSION_ID=42\nVARIANT_ID=kde\n'),
    ('rhel', 'NAME="Rocky Linux"\nID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="10.0"\n'),
    ('rhel', 'NAME="AlmaLinux"\nID="almalinux"\nID_LIKE="rhel centos fedora"\nVERSION_ID="10.2"\n'),
    ('rhel', 'NAME="Red Hat Enterprise Linux"\nID="rhel"\nID_LIKE="fedora"\nVERSION_ID="10.0"\n'),
    ('rhel', 'NAME="CentOS Stream"\nID="centos"\nID_LIKE="rhel fedora"\nVERSION_ID="10"\n'),
    (None, 'NAME="Oracle Linux Server"\nID="ol"\nID_LIKE="fedora"\nVERSION_ID="10.0"\n'),
    ('suse', 'NAME="openSUSE Leap"\nID="opensuse-leap"\nID_LIKE="suse opensuse"\nVERSION_ID="16.0"\n'),
    ('suse', 'NAME="openSUSE Tumbleweed"\nID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\nVERSION_ID="20261020"\n'),
    (None, 'NAME="SLES"\nID="sles"\nID_LIKE="suse"\nVERSION_ID="16.0"\n'),
    (None, 'NAME="Alpine Linux"\nID=alpine\nVERSION_ID=3.22.1\n'),
    (None, 'NAME=NixOS\nID=nixos\nVERSION_ID="25.05"\n'),
    (None, 'NAME=Gentoo\nID=gentoo\n'),
    (None, 'NAME="Void"\nID="void"\n'),
)


class InstallScriptTests(unittest.TestCase):
    """install.sh up to the point it starts the installer, with the real df and coreutils;
    only pacman, the network tools and the system identity are stood in for."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root, self.log = root, root / 'log'
        (root / 'bin').mkdir()
        (root / 'etc').mkdir()
        (root / 'modules' / os.uname().release).mkdir(parents=True)
        (root / 'etc/os-release').write_text('ID=arch\n')
        (root / 'stat').write_text('cpu  1 2 3 4\nbtime 1000000000\nprocesses 5\n')
        for name, body in (('pacman', 'echo "PACMAN $*" >> "$LOG"\nexit 0'), ('ip', 'exit 0'), ('lspci', 'exit 0'),
                           ('lslocks', 'exit 0'),   # no lock held, whatever this machine's apt is doing
                           ('openssl', 'exit 0'),   # a prerequisite a minimal system lacks (AlmaLinux)
                           ('systemctl', 'exit 0'), ('uname', 'if [ "$1" = -m ]; then echo x86_64; else /bin/uname "$@"; fi')):
            path = root / 'bin' / name
            path.write_text('#!/bin/sh\n' + body + '\n')
            path.chmod(0o755)

    def run_script(self, system_path, extra_path=None, args=(), replace=()):
        text = (ROOT / 'install.sh').read_text()
        for old, new in replace:
            self.assertIn(old, text)
            text = text.replace(old, new)
        # pacman's lock and the boot time as this test's own.
        text = text.replace('/var/lib/pacman/db.lck', str(self.root / 'db.lck'))
        text = text.replace('/proc/stat', str(self.root / 'stat'))
        text = text.replace('(( EUID != 0 ))', '(( 0 != 0 ))')
        text = text.replace('/etc/os-release', str(self.root / 'etc/os-release'))
        text = text.replace('/usr/lib/modules/', str(self.root / 'modules') + '/')
        text = text.replace('system_path=/var/cache/pacman', 'system_path=' + str(system_path))
        text = re.sub(r'(?m)^exec python3 .*$', 'echo "START $*" >> "$LOG"', text)
        text = re.sub(r'(?m)^    python3 "\$ROOT/host.py" bootstrap$', '    echo BOOTSTRAP >> "$LOG"', text)
        # The system check reads this machine; logged instead (capabilities' own tests check it).
        text = text.replace('python3 "$ROOT/capabilities.py" preflight || exit 1', 'echo PREFLIGHT >> "$LOG"')
        text = text.replace('/run/reboot-required', str(self.root / 'reboot-required'))
        text = text.replace('/run/ostree-booted', str(self.root / 'ostree-booted'))
        text = text.replace('/run/nekoserver-setup.lock', str(self.root / 'setup.lock'))
        text = re.sub(r'(?m)^        python3 "\$ROOT/host.py" repair$', '        echo REPAIR >> "$LOG"', text)
        # The runtime build: asked for with NEEDS_BUILD=1, and logged instead of run.
        text = text.replace('python3 "$ROOT/native_runtime.py" needs-build', '[ -n "${NEEDS_BUILD:-}" ]')
        text = text.replace('    python3 "$ROOT/host.py" toolchain', '    echo TOOLCHAIN >> "$LOG"')
        text = re.sub(r'(?m)^(\s*)(runuser -u "\$builder" -- env HOME="[^"]*" |NEKOSERVER_BUILD_AS_ROOT=1 )python3 "\$ROOT/runtime/build.py"$',
                      r'\1echo "BUILD \2" >> "$LOG"', text)
        text = text.replace('system_path=/var/cache/apt', 'system_path=' + str(system_path))
        text = text.replace('system_path=/var/cache/libdnf5', 'system_path=' + str(system_path))
        text = text.replace('system_path=/var/cache/dnf\n', 'system_path=' + str(system_path) + '\n')
        text = text.replace('system_path=/var/cache/zypp\n', 'system_path=' + str(system_path) + '\n')
        # No unfinished monthly update, whatever this test machine is running right now.
        text = text.replace('/etc/nekoserver-backups/maintenance-resume.json', str(self.root / 'maintenance-resume.json'))
        # The certificate bundle as present, whatever this test machine has.
        bundle = self.root / 'ca-bundle.pem'
        bundle.write_text('-----BEGIN CERTIFICATE-----\n')
        for path in ('/etc/ssl/certs/ca-certificates.crt', '/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem'):
            text = text.replace(path, str(bundle))
        script = self.root / 'install.sh'
        script.write_text(text)
        path = os.pathsep.join(p for p in (extra_path, str(self.root / 'bin'), os.environ['PATH']) if p)
        self.log.write_text('')
        result = subprocess.run(['bash', str(script), *args], capture_output=True, text=True, timeout=60,
                                env={**os.environ, 'PATH': path, 'LOG': str(self.log)})
        return result, self.log.read_text()

    def test_finish_update_builds_and_finishes_only_the_unfinished_update(self):
        # Setup refuses to run while a monthly update is unfinished; --finish-update is
        # the one way for a newer release's fix to finish it: no system update, no steps.
        finish = ('python3 "$ROOT/installer.py" --finish-update',
                  'echo "START --finish-update" >> "$LOG"')
        (self.root / 'maintenance-resume.json').write_text('{"version": "0.39.4", "phase": "restoring"}')
        result, log = self.run_script(self.root, args=('--finish-update',), replace=(finish,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('START --finish-update', log)
        self.assertNotIn('PACMAN', log)
        self.assertNotIn('BOOTSTRAP', log)
        # Without one, it says so and stops.
        (self.root / 'maintenance-resume.json').unlink()
        result, log = self.run_script(self.root, args=('--finish-update',), replace=(finish,))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('No monthly update is waiting', result.stdout)
        self.assertNotIn('START', log)

    def test_real_df_measures_and_the_setup_starts(self):
        self.assertTrue(shutil.which('df'))
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('Could not measure', result.stderr)
        self.assertIn('PACMAN -Syu --noconfirm', log)
        self.assertLess(log.index('PACMAN -Syu --noconfirm'), log.index('BOOTSTRAP'))
        self.assertIn('START --wizard', log)

    def use_debian(self, release='ID=ubuntu\nID_LIKE=debian\n'):
        (self.root / 'etc/os-release').write_text(release)
        (self.root / 'bin/pacman').unlink()
        for name in ('apt-get', 'dpkg-query'):
            path = self.root / 'bin' / name
            path.write_text('#!/bin/sh\necho "' + name.upper() + ' $*" >> "$LOG"\nexit 0\n')
            path.chmod(0o755)

    def test_too_old_a_release_stops_before_apt_runs(self):
        self.use_debian('ID=ubuntu\nVERSION_ID="22.04"\nPRETTY_NAME="Ubuntu 22.04 LTS"\n')
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn('needs Debian 12 or newer, or Ubuntu 24.04 or newer; this is Ubuntu 22.04 LTS', result.stderr)
        self.assertNotIn('APT-GET', log)
        (self.root / 'etc/os-release').write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_debian_refreshes_lists_without_upgrading(self):
        self.use_debian()
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('APT-GET -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold update', log)
        self.assertNotIn('upgrade', log)
        self.assertNotIn('PACMAN', log)
        self.assertLess(log.index('APT-GET -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold update'), log.index('BOOTSTRAP'))
        self.assertLess(log.index('BOOTSTRAP'), log.index('START --wizard'))

    def flaky_apt(self, message, failures='1'):
        """apt-get whose first `failures` runs of update fail with `message`."""
        counter = self.root / 'apt-failures'
        counter.write_text(failures)
        (self.root / 'bin/apt-get').write_text(
            '#!/bin/sh\necho "APT-GET $*" >> "$LOG"\nn=$(cat "$APT_FAILURES")\n'
            'case "$*" in *update) if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$APT_FAILURES"; echo "' + message + '" >&2; exit 100; fi;; esac\nexit 0\n')
        os.environ['APT_FAILURES'] = str(counter)
        self.addCleanup(os.environ.pop, 'APT_FAILURES', None)
        return counter

    def test_a_package_source_that_did_not_answer_is_tried_again(self):
        self.use_debian()
        counter = self.flaky_apt('E: Failed to fetch http://archive.ubuntu.com/ubuntu/dists/noble/InRelease  Temporary failure resolving archive.ubuntu.com')
        result, log = self.run_script(self.root, replace=[('sleep 20', 'sleep 0')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('did not answer; trying again in 20 seconds (2 of 3)'), 1)
        self.assertEqual(log.count('update'), 2)
        self.assertEqual(counter.read_text().strip(), '0')
        self.assertIn('START --wizard', log)
        # One that never answers: three attempts, then the failure as it was.
        self.flaky_apt('E: Failed to fetch http://archive.ubuntu.com/x  Connection timed out', failures='9')
        result, log = self.run_script(self.root, replace=[('sleep 20', 'sleep 0')])
        self.assertEqual(result.returncode, 100)
        self.assertEqual(result.stdout.count('trying again'), 2)
        self.assertEqual(log.count('update'), 3)
        self.assertNotIn('START', log)

    def test_any_other_apt_failure_stops_at_once(self):
        self.use_debian()
        self.flaky_apt('E: The repository http://example.invalid/debian trixie Release does not have a Release file.', failures='9')
        result, log = self.run_script(self.root, replace=[('sleep 20', 'sleep 0')])
        self.assertEqual(result.returncode, 100)
        self.assertNotIn('trying again', result.stdout)
        self.assertEqual(log.count('update'), 1)

    def test_the_runtime_is_built_on_this_pc_when_the_release_has_none(self):
        self.use_debian()
        os.environ['NEEDS_BUILD'] = '1'
        self.addCleanup(os.environ.pop, 'NEEDS_BUILD', None)
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Building NekoServer's background program for this PC", result.stdout)
        self.assertLess(log.index('BOOTSTRAP'), log.index('TOOLCHAIN'))      # Rust after the apps' packages
        self.assertLess(log.index('TOOLCHAIN'), log.index('BUILD'))
        self.assertLess(log.index('BUILD'), log.index('START --wizard'))
        self.assertIn('BUILD NEKOSERVER_BUILD_AS_ROOT=1', log)               # no sudo user in this test
        # With a runtime already built from these sources: nothing to build.
        os.environ.pop('NEEDS_BUILD')
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('TOOLCHAIN', log)
        self.assertNotIn('BUILD', log)
        self.assertNotIn('Building', result.stdout)

    def test_debian_waits_while_the_system_refreshes_its_lists(self):
        # apt-daily holds the lists' lock; apt-get update would fail at once beside it.
        self.use_debian()
        busy = self.root / 'busy'
        busy.write_text('2')
        (self.root / 'bin/lslocks').write_text(
            '#!/bin/sh\nn=$(cat "$BUSY")\nif [ "$n" -gt 0 ]; then echo $((n - 1)) > "$BUSY"; '
            'echo /var/lib/apt/lists/lock; fi\necho /run/other.lock\n')
        (self.root / 'bin/lslocks').chmod(0o755)
        os.environ['BUSY'] = str(busy)
        self.addCleanup(os.environ.pop, 'BUSY', None)
        result, log = self.run_script(self.root, replace=[('sleep 5', 'sleep 0')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('refreshing its package lists itself'), 1)
        self.assertEqual(busy.read_text().strip(), '0')                # asked until it was free
        self.assertIn('APT-GET -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold update', log)

    def test_debian_repairs_repositories_before_apt_first_runs(self):
        self.use_debian()
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(log.index('REPAIR'), log.index('APT-GET -o DPkg::Lock::Timeout=600 -o Dpkg::Options::=--force-confold update'))

    def test_recovery_never_needs_the_network_for_repositories(self):
        self.use_debian()
        result, log = self.run_script(self.root, args=('--recover',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('REPAIR', log)
        self.assertNotIn('APT-GET', log)
        self.assertIn('START --recover', log)

    def test_recovery_opens_without_building_the_runtime(self):
        # A reinstalled PC without the runtime (or one it cannot run): no Rust, no build.
        self.use_debian()
        os.environ['NEEDS_BUILD'] = '1'
        self.addCleanup(os.environ.pop, 'NEEDS_BUILD', None)
        result, log = self.run_script(self.root, args=('--recover',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('TOOLCHAIN', log)
        self.assertNotIn('BUILD', log)
        self.assertIn('START --recover', log)

    def test_a_second_setup_stops_before_any_change(self):
        lock = self.root / 'setup.lock'
        holder = subprocess.Popen(['flock', str(lock), 'sleep', '30'])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        for _ in range(50):
            if subprocess.run(['flock', '-n', str(lock), 'true'], check=False).returncode:
                break
            time.sleep(0.1)
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Another NekoServer setup is running', result.stderr)
        self.assertEqual(log, '')

    def test_debian_pending_restart_stops_before_any_change(self):
        self.use_debian('ID=debian\n')
        (self.root / 'reboot-required').write_text('')
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 3)
        self.assertIn('needs a restart first', result.stderr)
        self.assertEqual(log, '')

    def test_other_distributions_are_refused(self):
        (self.root / 'etc/os-release').write_text('ID=sles\nID_LIKE=suse\n')   # SUSE's enterprise system
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Debian / Ubuntu', result.stderr)
        self.assertEqual(log, '')

    def use_fedora(self, release='ID=fedora\nVERSION_ID=44\n'):
        (self.root / 'etc/os-release').write_text(release)
        (self.root / 'bin/pacman').unlink()
        path = self.root / 'bin/dnf'
        path.write_text('#!/bin/sh\necho "DNF $*" >> "$LOG"\nexit 0\n')
        path.chmod(0o755)

    def test_fedora_refreshes_lists_without_upgrading(self):
        self.use_fedora()
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DNF -q makecache', log)
        self.assertNotIn('upgrade', log)
        self.assertLess(log.index('DNF -q makecache'), log.index('BOOTSTRAP'))
        self.assertLess(log.index('BOOTSTRAP'), log.index('START --wizard'))

    def use_suse(self, release='ID=opensuse-leap\nID_LIKE="suse opensuse"\nVERSION_ID="16.0"\n'):
        (self.root / 'etc/os-release').write_text(release)
        (self.root / 'bin/pacman').unlink(missing_ok=True)     # called once per release in a loop
        path = self.root / 'bin/zypper'
        path.write_text('#!/bin/sh\necho "ZYPPER $*" >> "$LOG"\nexit 0\n')
        path.chmod(0o755)

    def test_suse_refreshes_lists_without_upgrading_and_waits_for_another_zypper(self):
        self.use_suse()
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ZYPPER --non-interactive --quiet refresh', log)
        self.assertNotIn('update', log)
        self.assertNotIn('PACMAN', log)
        self.assertLess(log.index('ZYPPER --non-interactive --quiet refresh'), log.index('BOOTSTRAP'))
        # zypper fails at once when another zypper runs, unless told how long to wait.
        self.assertIn('ZYPP_LOCK_TIMEOUT=600 zypper', (ROOT / 'install.sh').read_text())

    def test_without_sudo_it_says_how_to_get_it_or_how_to_run_as_root(self):
        # Debian set up with a root password: no sudo, so "Run it with sudo" led nowhere.
        no_sudo = (('if (( EUID != 0 )); then\n        if command -v sudo', 'if (( 1 != 0 )); then\n        if command -v no-such-sudo'),)
        result, log = self.run_script(self.root, replace=no_sudo)
        self.assertEqual(result.returncode, 1)
        self.assertIn('this PC has no sudo yet', result.stderr)
        self.assertIn('apt install sudo', result.stderr)
        self.assertIn('SUDO_USER=', result.stderr)
        self.assertNotIn('START', log)
        # As root without an account named, a first run says how to name one; with one, it does not.
        with mock.patch.dict(os.environ, {'SUDO_USER': ''}):
            result, _ = self.run_script(self.root)
        self.assertIn('Started as root, not with sudo', result.stdout)
        with mock.patch.dict(os.environ, {'SUDO_USER': 'neko'}):
            result, _ = self.run_script(self.root)
        self.assertNotIn('Started as root', result.stdout)

    def test_suse_goes_on_when_only_a_repository_was_skipped(self):
        # One third-party repository that does not answer (a Packman mirror, say) used to stop
        # setup before it started; a refresh where no repository answered still stops it.
        self.use_suse()
        zypper = self.root / 'bin/zypper'
        for code, last, ok in ((4, 'Some of the repositories have not been refreshed because of an error.', True),
                               (106, '', True),
                               (4, 'Could not refresh the repositories because of errors.', False)):
            with self.subTest(code=code, last=last):
                zypper.write_text('#!/bin/sh\necho "ZYPPER $*" >> "$LOG"\ncase "$*" in *refresh*) '
                                  "echo \"Skipping repository 'Packman' because of the above error.\" >&2; "
                                  f'echo "{last}" >&2; exit {code};; esac\nexit 0\n')
                result, log = self.run_script(self.root)
                self.assertEqual(result.returncode == 0, ok, result.stderr)
                self.assertEqual('BOOTSTRAP' in log, ok)
                self.assertEqual("The repository 'Packman' could not be reached and was skipped" in result.stderr, ok)   # warnings go to stderr

    def test_suse_updates_the_system_when_pythons_own_modules_do_not_load(self):
        # Seen on Leap 16: a fresh Python met a libexpat too old for it (openSUSE's
        # packages name the library, not its version), so xml.etree did not load.
        imports = "import ssl, sqlite3, pyexpat, xml.etree.ElementTree, zlib, lzma, bz2, ctypes, hashlib'"
        for release, command in (('ID=opensuse-leap\nID_LIKE="suse opensuse"\nVERSION_ID="16.0"\n', 'update'),
                                 ('ID=opensuse-tumbleweed\nID_LIKE="opensuse suse"\nVERSION_ID="20261020"\n', 'dist-upgrade')):
            with self.subTest(command=command):
                self.use_suse(release)
                result, log = self.run_script(self.root, replace=[(imports, "import no_such_module_for_this_test'")])
                self.assertEqual(result.returncode, 1)
                self.assertIn('Python is installed, but parts of it do not load', result.stdout)
                self.assertIn(f'ZYPPER --non-interactive {command} --no-recommends', log)
                self.assertIn('still do not load after the update', result.stderr)
                self.assertNotIn('BOOTSTRAP', log)

    def test_leap_15_stops_before_zypper_runs(self):
        self.use_suse('ID=opensuse-leap\nID_LIKE="suse opensuse"\nVERSION_ID="15.6"\nPRETTY_NAME="openSUSE Leap 15.6"\n')
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn('needs openSUSE Leap 16 or newer; this is openSUSE Leap 15.6', result.stderr)
        self.assertEqual(log, '')

    def test_version_9_of_the_rhel_family_stops_before_dnf_runs(self):
        # Its Python is 3.9: NekoServer needs 3.11, which version 10 brings.
        self.use_fedora('NAME="Rocky Linux"\nID="rocky"\nID_LIKE="rhel centos fedora"\nVERSION_ID="9.6"\n'
                        'PRETTY_NAME="Rocky Linux 9.6 (Blue Onyx)"\n')
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn('needs version 10 or newer of Rocky Linux; this is Rocky Linux 9.6 (Blue Onyx)', result.stderr)
        self.assertEqual(log, '')

    def test_too_old_a_fedora_stops_before_dnf_runs(self):
        self.use_fedora('ID=fedora\nVERSION_ID=41\nPRETTY_NAME="Fedora Linux 41 (Server Edition)"\n')
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn('needs Fedora 42 or newer; this is Fedora Linux 41 (Server Edition)', result.stderr)
        self.assertEqual(log, '')

    def test_a_pacman_lock_from_before_the_last_restart_is_removed_and_the_update_runs(self):
        lock = self.root / 'db.lck'
        lock.write_text('')
        os.utime(lock, (999_999_000, 999_999_000))          # before btime 1000000000
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock.exists())
        self.assertIn("pacman's lock was left by an update cut off before the last restart: removed", result.stdout)
        self.assertIn('PACMAN -Syu --noconfirm', log)

    def test_a_pacman_lock_held_since_this_boot_is_waited_for_then_explained(self):
        lock = self.root / 'db.lck'
        lock.write_text('')
        os.utime(lock, (1_000_000_500, 1_000_000_500))      # after this boot: someone holds it
        # Without the ten-minute wait.
        result, log = self.run_script(self.root, replace=[('(( waited < 600 ))', '(( waited < 0 ))')])
        self.assertEqual(result.returncode, 1)
        self.assertIn('pacman is still locked', result.stderr)
        self.assertIn('sudo rm ' + str(lock), result.stderr)
        self.assertTrue(lock.exists())
        self.assertEqual(log, 'PREFLIGHT\n')      # the system check only: pacman never ran

    def processes(self, *targets):
        """Stand-ins for /proc/<pid>/fd: one process with these files open."""
        fds = self.root / 'proc/77/fd'
        fds.mkdir(parents=True)
        for number, target in enumerate(targets):
            (fds / str(number)).symlink_to(target)
        return [('/proc/[0-9]*/fd', str(self.root / 'proc') + '/[0-9]*/fd'), ('sleep 5', 'sleep 0')]

    def test_a_pacman_lock_no_program_has_open_is_removed_at_once(self):
        # Left by a pacman killed since this boot (a timeout, say): nothing would ever remove it.
        lock = self.root / 'db.lck'
        lock.write_text('')
        os.utime(lock, (1_000_000_500, 1_000_000_500))      # after this boot
        result, log = self.run_script(self.root, replace=self.processes('/dev/null'))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock.exists())
        self.assertIn('no program has it open): removed', result.stdout)
        self.assertNotIn('waiting for it to finish', result.stdout)
        self.assertIn('PACMAN -Syu --noconfirm', log)

    def test_a_pacman_lock_a_program_has_open_is_kept(self):
        lock = self.root / 'db.lck'
        lock.write_text('')
        os.utime(lock, (1_000_000_500, 1_000_000_500))
        result, log = self.run_script(self.root, replace=self.processes('/dev/null', str(lock)))
        self.assertEqual(result.returncode, 1)
        self.assertTrue(lock.exists())
        self.assertIn('waiting for it to finish (up to 10 minutes)', result.stdout)
        self.assertIn('pacman is still locked', result.stderr)
        self.assertEqual(log, 'PREFLIGHT\n')      # the system check only: pacman never ran

    def test_a_source_apt_only_warned_about_is_tried_again(self):
        # apt-get update exits 0 when a source did not answer; its lists from before are used.
        self.use_debian()
        counter = self.root / 'apt-warnings'
        counter.write_text('1')
        (self.root / 'bin/apt-get').write_text(
            '#!/bin/sh\necho "APT-GET $*" >> "$LOG"\nn=$(cat "$APT_WARNINGS")\n'
            'case "$*" in *update) if [ "$n" -gt 0 ]; then echo $((n - 1)) > "$APT_WARNINGS"; '
            'echo "W: Failed to fetch https://download.docker.com/linux/ubuntu/dists/noble/InRelease  Could not resolve"; '
            'echo "W: Some index files failed to download. They have been ignored, or old ones used instead."; fi;; esac\nexit 0\n')
        os.environ['APT_WARNINGS'] = str(counter)
        self.addCleanup(os.environ.pop, 'APT_WARNINGS', None)
        result, log = self.run_script(self.root, replace=[('sleep 20', 'sleep 0')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.count('update'), 2)
        self.assertIn('trying again in 20 seconds (2 of 3)', result.stdout)
        self.assertNotIn('could not be reached', result.stdout)
        # One that never answers: three attempts, then setup goes on with the lists from before.
        counter.write_text('9')
        result, log = self.run_script(self.root, replace=[('sleep 20', 'sleep 0')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.count('update'), 3)
        self.assertIn('Some package sources could not be reached', result.stdout)
        self.assertIn('START --wizard', log)

    def test_a_lock_another_program_holds_is_waited_out(self):
        self.use_debian()
        self.flaky_apt('E: Could not get lock /var/lib/apt/lists/lock. It is held by process 4242 (packagekitd)', failures='4')
        result, log = self.run_script(self.root, replace=[('sleep 20', 'sleep 0')])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.count('update'), 5)          # more than three attempts: waiting is not failing
        self.assertEqual(result.stdout.count('waiting for it to finish (up to 10 minutes)'), 1)
        self.assertNotIn('did not answer', result.stdout)

    def test_a_wrong_clock_is_named(self):
        self.use_debian()
        self.flaky_apt('E: Release file for http://archive.ubuntu.com/ubuntu/dists/noble/InRelease is not valid yet '
                       '(invalid for another 5h 2min 3s). Updates for this repository will not be applied.', failures='9')
        result, _ = self.run_script(self.root, replace=[('sleep 20', 'sleep 0')])
        self.assertEqual(result.returncode, 100)
        self.assertIn("clock looks wrong", result.stderr)
        self.assertIn('sudo timedatectl set-ntp true', result.stderr)

    def test_image_based_systems_are_refused_before_any_package_manager_runs(self):
        # Fedora Silverblue / Kinoite (ostree): Fedora by name, dnf present, /usr read-only.
        self.use_fedora('ID=fedora\nVERSION_ID=44\nVARIANT_ID=silverblue\nPRETTY_NAME="Fedora Linux 44 (Silverblue)"\n')
        (self.root / 'ostree-booted').write_text('')
        result, log = self.run_script(self.root)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Fedora Linux 44 (Silverblue) keeps its system files read-only', result.stderr)
        self.assertEqual(log, '')
        # SteamOS: Arch by name, pacman present, the system mounted read-only.
        (self.root / 'ostree-booted').unlink()
        (self.root / 'etc/os-release').write_text('ID=steamos\nID_LIKE=arch\nPRETTY_NAME="SteamOS"\n')
        fake = self.root / 'readonly'
        fake.mkdir()
        (fake / 'findmnt').write_text('#!/bin/sh\necho "ro,relatime"\n')
        (fake / 'findmnt').chmod(0o755)
        (fake / 'pacman').write_text('#!/bin/sh\necho "PACMAN $*" >> "$LOG"\n')
        (fake / 'pacman').chmod(0o755)
        result, log = self.run_script(self.root, str(fake))
        self.assertEqual(result.returncode, 1)
        self.assertIn('SteamOS keeps its system files read-only', result.stderr)
        self.assertEqual(log, '')
        # A writable system whose options merely mention it (ext4's errors=remount-ro) goes on.
        (fake / 'findmnt').write_text('#!/bin/sh\necho "rw,relatime,errors=remount-ro,data=ordered"\n')
        (self.root / 'etc/os-release').write_text('ID=arch\n')
        result, log = self.run_script(self.root, str(fake))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('PACMAN -Syu --noconfirm', log)

    def test_setup_and_the_installer_agree_on_every_distribution(self):
        # install.sh picks the package manager and the installer (host.detect) everything
        # after it: a system one accepts and the other refuses would stop setup halfway.
        # Every package manager is present, so only os-release decides.
        for name in ('apt-get', 'dpkg-query', 'dnf', 'zypper'):
            path = self.root / 'bin' / name
            path.write_text('#!/bin/sh\necho "' + name.upper() + ' $*" >> "$LOG"\nexit 0\n')
            path.chmod(0o755)
        managers = {'arch': 'PACMAN', 'debian': 'APT-GET', 'fedora': 'DNF', 'rhel': 'DNF', 'suse': 'ZYPPER'}
        release = self.root / 'etc/os-release'
        for expected, text in RELEASES:
            with self.subTest(release=text.splitlines()[0]):
                release.write_text(text)
                result, log = self.run_script(self.root)
                family = host.detect(str(release))
                if expected is None:
                    self.assertIsNone(family)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn('Automatic setup supports', result.stderr)
                    self.assertEqual(log, '')
                else:
                    self.assertEqual(family.name if family else None, expected)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    used = {word for word in managers.values() if word + ' ' in log}
                    self.assertEqual(used, {managers[expected]})
                    self.assertIn('START --wizard', log)

    def test_unmeasurable_space_is_reported_not_silent(self):
        broken = self.root / 'brokendf'
        broken.mkdir()
        (broken / 'df').write_text('#!/bin/sh\necho "df: unsupported" >&2\nexit 1\n')
        (broken / 'df').chmod(0o755)
        result, log = self.run_script(self.root, str(broken))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Could not measure the free space', result.stderr)
        self.assertIn('START --wizard', log)

    def test_low_space_stops_before_any_change(self):
        fake = self.root / 'lowdf'
        fake.mkdir()
        (fake / 'df').write_text('#!/bin/sh\necho Avail\necho 3500M\n')   # 3.4 GB: df -BM prints MB
        (fake / 'df').chmod(0o755)
        result, log = self.run_script(self.root, str(fake))
        self.assertEqual(result.returncode, 1)
        self.assertIn('only 3 GB free', result.stderr)
        self.assertNotIn('PACMAN', log)
        self.assertNotIn('START', log)


if __name__ == '__main__':
    unittest.main()


class TimerTimezoneTests(unittest.TestCase):
    def test_every_calendar_timer_uses_the_configured_timezone(self):
        from neko_common import host_timezone
        zone = host_timezone(config())
        calendars = [(where, line) for where, text in unit_texts().items()
                     for line in text.splitlines() if line.startswith('OnCalendar=')]
        self.assertGreaterEqual(len(calendars), 4)
        for where, line in calendars:
            with self.subTest(unit=where):
                self.assertTrue(line.endswith(' ' + zone), line)


class TailnetAcceptanceTests(unittest.TestCase):
    def setUp(self):
        # A dashboard is installed (the route is offered only then).
        import gateway_tools
        from unittest import mock
        unit = tempfile.NamedTemporaryFile(suffix='.service')
        self.addCleanup(unit.close)
        patcher = mock.patch.object(gateway_tools, 'DASHBOARD_UNIT', Path(unit.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_check_accepts_exactly_what_the_installer_configures(self):
        from unittest import mock
        import gateway_checks
        cfg = config()
        cfg['nextcloud']['enabled'] = True
        cfg['photos']['deploy_immich'] = True
        host = 'neko.tail1234.ts.net'
        installed = gateway_tools.tailnet_routes(cfg, host)
        self.assertIn(str(gateway_tools.DASHBOARD_PORT), installed['TCP'])
        journal = {'format': 'neko-tailnet-owned-v1', 'host': host, 'status': 'configured', 'owned': installed}
        with mock.patch.object(gateway_checks.g, 'tailnode', return_value=host), \
                mock.patch.object(gateway_checks.g, 'serve_state', return_value=installed), \
                mock.patch.object(gateway_checks.health, 'read_json', return_value=journal):
            self.assertEqual(gateway_checks.check(cfg, 'tailnet')[0]['status'], 'pass')
            # A route set missing the dashboard (or anything else) is still a failure.
            partial = gateway_tools.desired(host, gateway_tools.selected(cfg))
            with mock.patch.object(gateway_checks.g, 'serve_state', return_value=partial):
                self.assertEqual(gateway_checks.check(cfg, 'tailnet')[0]['status'], 'fail')


class QbitMusicCategoryTests(unittest.TestCase):
    class Fake:
        def __init__(self, music_path, torrents):
            self.categories = {'tv': {'savePath': '/data/downloads'}, 'music': {'savePath': music_path}}
            self.torrents, self.edits = torrents, []
            self.preferences = {'save_path': '/data/downloads', 'auto_tmm_enabled': True}

        def login(self, *a):
            pass

        def close(self):
            pass

        def call(self, endpoint, form=None, json_result=False):
            if endpoint == '/app/preferences':
                return dict(self.preferences)
            if endpoint == '/app/setPreferences':
                self.preferences.update(json.loads(form['json']))
            if endpoint == '/torrents/categories':
                return self.categories
            if endpoint.startswith('/torrents/info'):
                return self.torrents
            if endpoint == '/torrents/editCategory':
                self.edits.append(form)
                self.categories[form['category']] = {'savePath': form['savePath']}
            if endpoint == '/torrents/createCategory':
                self.categories[form['category']] = {'savePath': form['savePath']}
            return {}

    def test_wrong_music_path_is_corrected_when_empty(self):
        import qbit_setup
        fake = self.Fake('/data/downloads', [])
        qbit_setup.configure('u', 'p', apply=True, client=fake, output=lambda *_: None)
        self.assertEqual(fake.edits, [{'category': 'music', 'savePath': '/data/downloads/music'}])
        self.assertEqual(fake.categories['music']['savePath'], '/data/downloads/music')

    def test_wrong_music_path_with_torrents_needs_a_person(self):
        import qbit_setup
        fake = self.Fake('/data/downloads', [{'name': 'album'}])
        with self.assertRaisesRegex(RuntimeError, 'not /data/downloads/music'):
            qbit_setup.configure('u', 'p', apply=True, client=fake, output=lambda *_: None)
        self.assertEqual(fake.edits, [])

    def test_new_torrents_start_in_automatic_mode_so_each_category_folder_is_used(self):
        # Manual mode (qBittorrent's default) ignores the category's folder: albums then
        # land on the HDD, where Lidarr cannot see them.
        import qbit_setup
        fake = self.Fake('/data/downloads/music', [{'name': 'a show'}])
        fake.preferences = {'save_path': '/data/downloads'}
        qbit_setup.configure('u', 'p', apply=True, client=fake, output=lambda *_: None)
        self.assertIs(fake.preferences['auto_tmm_enabled'], True)

    def test_a_qbittorrent_that_does_not_keep_automatic_mode_stops_setup(self):
        import qbit_setup
        fake = self.Fake('/data/downloads/music', [])
        fake.preferences = {'save_path': '/data/downloads'}
        real = fake.call

        def forgetful(endpoint, form=None, json_result=False):
            if endpoint == '/app/setPreferences':
                return {}
            return real(endpoint, form, json_result)
        fake.call = forgetful
        with self.assertRaisesRegex(RuntimeError, 'did not retain'):
            qbit_setup.configure('u', 'p', apply=True, client=fake, output=lambda *_: None)

    def test_shared_folder_is_fine_for_tv_and_movies(self):
        import qbit_setup
        self.assertTrue(qbit_setup.category_ok('tv', '/data/downloads'))
        self.assertFalse(qbit_setup.category_ok('music', '/data/downloads'))
        self.assertTrue(qbit_setup.category_ok('music', ''))


class NextcloudOffTests(unittest.TestCase):
    def test_turning_it_off_stops_an_installed_nextcloud_and_keeps_data(self):
        from unittest import mock
        from modules import nextcloud

        class Ctx:
            def __init__(self, data, apply=True):
                self.config, self.apply, self.logs = {'data_mount': data, 'nextcloud': {'enabled': False}}, apply, []

            def log(self, m):
                self.logs.append(m)
        with tempfile.TemporaryDirectory() as data:
            calls = []
            ctx = Ctx(data)
            nextcloud.stop_installed(ctx, calls.append)
            self.assertEqual(calls, [])  # never installed: nothing to stop
            folder = Path(data) / 'compose/nextcloud'
            folder.mkdir(parents=True)
            (Path(data) / 'installer').mkdir()     # as on a set-up SSD
            (folder / 'compose.yaml').write_text('{}')
            (folder / '.env').write_text('DB_PASSWORD=x\n')
            with mock.patch.object(nextcloud, 'compose_image_override', return_value=[]), \
                    mock.patch.object(nextcloud, 'MAINTENANCE_JOURNAL', Path(data) / 'no-journal.json'), \
                    mock.patch.object(nextcloud.neko_common, 'MAINTENANCE_JOURNAL', Path(data) / 'no-journal.json'), \
                    mock.patch.object(nextcloud, 'command', calls.append):
                self.assertIsNone(nextcloud.run(ctx))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][-1], 'stop')
            self.assertIn('neko-nextcloud', calls[0])
            self.assertNotIn('down', calls[0])  # containers stopped, never removed with their data
            self.assertTrue(any('files and database are kept' in m for m in ctx.logs))


    def test_it_is_stopped_only_under_the_maintenance_lock(self):
        from contextlib import contextmanager
        from unittest import mock
        from modules import nextcloud

        class Ctx:
            apply = True

            def __init__(self, data):
                self.config, self.logs = {'data_mount': data, 'nextcloud': {'enabled': False}}, []

            def log(self, m):
                self.logs.append(m)
        with tempfile.TemporaryDirectory() as data:
            folder = Path(data) / 'compose/nextcloud'
            folder.mkdir(parents=True)
            (folder / 'compose.yaml').write_text('{}')
            events = []

            @contextmanager
            def hold(config):
                events.append('locked')  # waits here while maintenance or a backup runs
                try:
                    yield
                finally:
                    events.append('released')
            journal = Path(data) / 'maintenance-resume.json'
            with mock.patch.object(nextcloud, 'compose_image_override', return_value=[]):
                nextcloud.stop_installed(Ctx(data), lambda args: events.append(args[-1]), hold, journal)
                self.assertEqual(events, ['locked', 'stop', 'released'])
                # An unfinished update would start Nextcloud again when it resumes: refuse.
                journal.write_text('{}')
                events.clear()
                with self.assertRaisesRegex(RuntimeError, 'monthly update did not finish'):
                    nextcloud.stop_installed(Ctx(data), lambda args: events.append(args[-1]), hold, journal)
                self.assertEqual(events, ['locked', 'released'])


class LedgerFormatTests(unittest.TestCase):
    def test_python_checksums_are_sha256(self):
        import neko_common
        with tempfile.TemporaryDirectory() as d:
            source, target = Path(d) / 'abc', Path(d) / 'copy'
            source.write_bytes(b'abc')
            expected = 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'  # FIPS 180-2
            self.assertEqual(neko_common.sha256_file(source), expected)
            self.assertEqual(neko_common.copy_checked(source, target, expected), expected)

    def test_python_reads_the_ledger_the_runtime_writes(self):
        import neko_common
        source = (RUNTIME / 'src/ledger.rs').read_text()
        self.assertIn(f'pub const LEDGER: &str = "{neko_common.LEDGER_NAME}";', source)
        self.assertIn('const FORMAT: &str = "neko-ledger-v1";', source)
        self.assertIn(f'const QUARANTINE: &str = "{neko_common.QUARANTINE}";', (RUNTIME / 'src/backup.rs').read_text())
        with tempfile.TemporaryDirectory() as d:
            # Exactly what Ledger::save writes.
            (Path(d) / '.neko-ledger.json').write_text(json.dumps({'format': 'neko-ledger-v1', 'entries': {
                'upload/u/a.jpg': {'sha256': 'ab' * 32, 'size': 4, 'verified_at': 1.5, 'how': 'copied'}}}))
            self.assertEqual(neko_common.read_ledger(d), {'upload/u/a.jpg': 'ab' * 32})


class InterruptedSetupTests(unittest.TestCase):
    """A setup stopped between writing its settings files must finish on the next run."""

    class Ctx:
        def __init__(self, config):
            self.config, self.apply, self.logs = config, True, []

        def log(self, m):
            self.logs.append(m)

        def persist_config(self):
            pass

    MANIFEST = b'services:\n  immich-server:\n    ports:\n      - 2283:2283\n# DB_DATA_LOCATION\n'

    def immich(self, data, interrupt=False):
        from contextlib import ExitStack
        from unittest import mock
        from modules import photos
        cfg = {'data_mount': str(data / 'ssd'), 'media_mount': str(data / 'hdd'), 'timezone': 'UTC',
               'photos': {'deploy_immich': True, 'immich_version': 'v1.2.3'}}
        urls, calls = [], []

        class Response:
            def __init__(self, request):
                urls.append(getattr(request, 'full_url', request))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return InterruptedSetupTests.MANIFEST
        writes = []
        real = photos.atomic_write

        def write(path, *a):
            writes.append(Path(path).name)
            if interrupt and len(writes) == 2:
                raise KeyboardInterrupt
            return real(path, *a)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(photos.os, 'geteuid', return_value=0))
            stack.enter_context(mock.patch.object(photos, 'ROOT_UID', os.getuid()))
            stack.enter_context(mock.patch.object(photos, 'verify_storage'))
            stack.enter_context(mock.patch.object(photos, 'checked_mount', side_effect=str))
            stack.enter_context(mock.patch.object(photos.shutil, 'which', return_value='/usr/bin/docker'))
            stack.enter_context(mock.patch.object(photos.subprocess, 'run', side_effect=lambda a, **k: calls.append(a)))
            stack.enter_context(mock.patch.object(photos, 'checked_run', side_effect=lambda a, *r, **k: calls.append(a)))
            stack.enter_context(mock.patch.object(photos, 'compose_up', side_effect=lambda base, *r, **k: calls.append([*base, 'up', '-d'])))
            stack.enter_context(mock.patch('neko_common.urllib.request.urlopen', side_effect=lambda u, **k: Response(u)))
            stack.enter_context(mock.patch.object(photos, 'compose_image_override', return_value=[]))
            stack.enter_context(mock.patch.object(photos, 'atomic_write', side_effect=write))
            photos._run(self.Ctx(cfg))
        return urls, calls, data / 'ssd/compose/immich'

    def test_immich_finishes_after_stopping_between_its_two_files(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            with self.assertRaises(KeyboardInterrupt):
                self.immich(data, interrupt=True)
            folder = data / 'ssd/compose/immich'
            self.assertTrue((folder / '.env').exists())          # .env goes first ...
            self.assertFalse((folder / 'compose.yaml').exists())  # ... compose.yaml marks a finished step
            password = [x for x in (folder / '.env').read_text().splitlines() if x.startswith('DB_PASSWORD=')]
            urls, calls, _ = self.immich(data)
            self.assertTrue((folder / 'compose.yaml').exists())
            self.assertIn('/v1.2.3/', urls[0])
            self.assertEqual(password, [x for x in (folder / '.env').read_text().splitlines() if x.startswith('DB_PASSWORD=')])
            self.assertEqual(calls[-1][-2:], ['up', '-d'])

    def test_immich_left_by_an_older_release_with_only_compose_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            folder = data / 'ssd/compose/immich'
            folder.mkdir(parents=True)
            (folder / 'compose.yaml').write_bytes(self.MANIFEST)
            _, calls, _ = self.immich(data)
            self.assertTrue((folder / '.env').exists())
            self.assertEqual(calls[-1][-2:], ['up', '-d'])
            # With a database already there the missing password matters: stop and keep files.
            (folder / '.env').unlink()
            (data / 'ssd/appdata/immich/postgres/PG_VERSION').write_text('16')
            with self.assertRaisesRegex(RuntimeError, 'database but its .env'):
                self.immich(data)
            self.assertTrue((folder / 'compose.yaml').exists())

    def nextcloud(self, data, interrupt_at=None):
        from contextlib import ExitStack, nullcontext
        from unittest import mock
        from modules import nextcloud
        cfg = {'data_mount': str(data / 'ssd'), 'media_mount': str(data / 'hdd'), 'nextcloud': {'image': 'nextcloud:30-apache'}}
        calls, writes = [], []
        real_write, real_json = nextcloud.atomic_write, nextcloud.atomic_json

        def track(real):
            def write(path, *a):
                writes.append(Path(path).name)
                if interrupt_at and Path(path).name == interrupt_at:
                    raise KeyboardInterrupt
                return real(path, *a)
            return write
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(nextcloud.os, 'geteuid', return_value=0))
            stack.enter_context(mock.patch.object(nextcloud, 'ROOT_UID', os.getuid()))
            stack.enter_context(mock.patch.object(nextcloud.os, 'chown'))  # www-data owner needs root
            stack.enter_context(mock.patch.object(nextcloud, 'verify_storage'))
            stack.enter_context(mock.patch.object(nextcloud, 'lock', side_effect=lambda c: nullcontext()))
            stack.enter_context(mock.patch.object(nextcloud.account_store, 'enabled', return_value=False))
            stack.enter_context(mock.patch.object(nextcloud, 'compose_image_override', return_value=[]))
            stack.enter_context(mock.patch.object(nextcloud, 'command', side_effect=calls.append))
            stack.enter_context(mock.patch.object(nextcloud, 'compose_up', side_effect=lambda base, *r, **k: calls.append(base + ['up', '-d'])))
            stack.enter_context(mock.patch.object(nextcloud, 'finish_install'))
            stack.enter_context(mock.patch.object(nextcloud, 'atomic_write', side_effect=track(real_write)))
            stack.enter_context(mock.patch.object(nextcloud, 'atomic_json', side_effect=track(real_json)))
            nextcloud.run(self.Ctx(cfg))
        return calls, writes

    def test_nextcloud_finishes_after_stopping_while_writing_its_settings(self):
        for stop_at in ('managed.json', 'compose.yaml'):
            with self.subTest(stop_at), tempfile.TemporaryDirectory() as d:
                data = Path(d)
                with self.assertRaises(KeyboardInterrupt):
                    self.nextcloud(data, interrupt_at=stop_at)
                calls, writes = self.nextcloud(data)
                self.assertEqual(writes[-1], 'compose.yaml')  # written last
                self.assertEqual(calls[-1][-2:], ['up', '-d'])
                self.assertEqual(sorted(p.name for p in (data / 'ssd/compose/nextcloud').iterdir()),
                                 ['.env', 'compose.yaml', 'managed.json'])

    def test_nextcloud_left_by_an_older_release_without_managed_json(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            self.nextcloud(data)
            folder = data / 'ssd/compose/nextcloud'
            (folder / 'managed.json').unlink()
            calls, _ = self.nextcloud(data)
            self.assertEqual(calls[-1][-2:], ['up', '-d'])
            # Once the database holds data, an incomplete setup is kept for a person to inspect.
            (folder / 'managed.json').unlink()
            (data / 'ssd/appdata/nextcloud/postgres/PG_VERSION').write_text('16')
            with self.assertRaisesRegex(RuntimeError, 'existing data'):
                self.nextcloud(data)
            self.assertTrue((folder / 'compose.yaml').exists())


class ConfigArgumentTests(unittest.TestCase):
    def test_settings_path_is_checked_before_reading(self):
        import installer
        import recovery_wizard
        from neko_common import settings_path
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / 'mine.json'
            good.write_text('{"data_mount": "/x"}')
            self.assertEqual(settings_path(str(good)), good)
            link = Path(d) / 'link.json'
            link.symlink_to(good)
            (Path(d) / 'shadow').write_text('secret')
            for bad in (str(link), d + '/../etc/passwd.json', d + '/shadow', 'a\x00.json'):
                with self.subTest(bad), self.assertRaises(RuntimeError):
                    settings_path(bad)
            with self.assertRaises(RuntimeError):
                recovery_wizard.settings(d + '/shadow')
            with self.assertRaises(SystemExit):
                installer.main(['--check', '--config', d + '/../x.json'])

    def test_wizard_and_recovery_use_the_chosen_settings_file(self):
        from unittest import mock
        import installer
        with mock.patch('wizard.run', return_value=0) as wizard_run, \
                mock.patch('recovery_wizard.run', return_value=0) as recovery_run:
            installer.main(['--wizard', '--config', '/srv/custom/settings.json'])
            installer.main(['--recover', '--config', '/srv/custom/settings.json'])
            installer.main(['--recover'])
        self.assertEqual(wizard_run.call_args.kwargs['config'], '/srv/custom/settings.json')
        self.assertEqual(recovery_run.call_args_list[0].kwargs['config_path'], '/srv/custom/settings.json')
        self.assertIsNone(recovery_run.call_args_list[1].kwargs['config_path'])

    def test_recovery_reads_that_file(self):
        import recovery_wizard
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text(json.dumps({'media_mount': '/mnt/custom-hdd'}))
            self.assertEqual(recovery_wizard.settings(str(path))['media_mount'], '/mnt/custom-hdd')
            with self.assertRaisesRegex(RuntimeError, 'not found'):
                recovery_wizard.settings(str(Path(folder) / 'missing.json'))


class AcceptanceReportCompatibilityTests(unittest.TestCase):
    """The Rust dashboard rejects a saved acceptance report with a manual check it does
    not know, so every check Python can write must be known to the Rust reader."""

    def rust_manual_ids(self):
        source = (RUNTIME / 'src/collect.rs').read_text()
        start = source.index('fn manual(key: &str)')
        return set(re.findall(r'"([a-z_]+)" =>', source[start:source.index('_ => None', start)]))

    def test_every_manual_check_python_writes_is_known_to_the_dashboard(self):
        import acceptance
        import acceptance_summary
        every_module = set(installer_modules())
        written = {row['id'] for row in acceptance.manual_tests(every_module, {'photos': {'deploy_immich': True}})}
        self.assertIn('cloud_client', written)
        self.assertLessEqual(written, self.rust_manual_ids())
        self.assertEqual(set(acceptance_summary.MANUAL), self.rust_manual_ids())


def installer_modules():
    import installer
    return installer.load_modules(ROOT / 'modules')


class AccountCorrectionTests(unittest.TestCase):
    def test_pending_new_account_can_switch_to_the_login_you_set_yourself(self):
        from unittest import mock
        import account_store
        value = {'format': 1, 'mode': 'per-app', 'apps': {
            'jellyfin': {'username': 'neko_ab12', 'password': 'x' * 32, 'state': 'needs_setup',
                         'allow_create': True, 'source': 'generate'}}}
        saved, out = [], []
        answers = iter(['c', 'dana'])
        passwords = iter(['my-own-password', 'my-own-password'])
        with mock.patch.object(account_store, 'load', return_value=value), \
                mock.patch.object(account_store, 'save', lambda config, v: saved.append(json.loads(json.dumps(v)))):
            account_store.collect({'data_mount': '/srv/nekoserver'}, ['jellyfin'],
                                  prompt=lambda q='': next(answers), password_prompt=lambda q='': next(passwords),
                                  output=out.append)
        entry = saved[-1]['apps']['jellyfin']
        self.assertEqual((entry['username'], entry['password']), ('dana', 'my-own-password'))
        self.assertEqual((entry['state'], entry['allow_create'], entry['source']), ('planned', False, 'custom-existing'))
        self.assertTrue(any('not changed or reset' in line for line in out))

    def test_enter_keeps_retrying_the_saved_login(self):
        from unittest import mock
        import account_store
        value = {'format': 1, 'mode': 'per-app', 'apps': {
            'jellyfin': {'username': 'neko_ab12', 'password': 'x' * 32, 'state': 'needs_setup',
                         'allow_create': True, 'source': 'generate'}}}
        with mock.patch.object(account_store, 'load', return_value=value), \
                mock.patch.object(account_store, 'save', return_value=None):
            account_store.collect({'data_mount': '/srv/nekoserver'}, ['jellyfin'], prompt=lambda q='': '',
                                  password_prompt=lambda q='': '', output=lambda s: None)
        self.assertTrue(value['apps']['jellyfin']['allow_create'])
        self.assertEqual(value['apps']['jellyfin']['username'], 'neko_ab12')


class RootUnitSandboxTests(unittest.TestCase):
    """IT-14: the root services that had no sandboxing get what each can take. Drive
    return unmounts a dead mount itself, so it gets nothing that makes a mount namespace
    of its own (the unmount would then never reach the real mount); nothing closes /dev
    (hdparm, blkid) or makes /etc read-only (the jobs' records)."""
    BASE = ('NoNewPrivileges=yes', 'RestrictRealtime=yes', 'RestrictSUIDSGID=yes', 'LockPersonality=yes',
            'SystemCallArchitectures=native')
    NAMESPACED = ('PrivateTmp=', 'ProtectHome=', 'ProtectKernelTunables=', 'ProtectControlGroups=')
    NEVER = ('ProtectSystem=', 'PrivateDevices=', 'ProtectClock=', 'DevicePolicy=', 'DeviceAllow=', 'CapabilityBoundingSet=',
             'PrivateMounts=', 'ProtectKernelModules=', 'ReadOnlyPaths=', 'InaccessiblePaths=', 'TemporaryFileSystem=',
             'BindPaths=', 'BindReadOnlyPaths=', 'RootDirectory=', 'MountFlags=', 'PrivateNetwork=')

    def units(self):
        texts = unit_texts()
        return {'drive-return': server_os.drive_unit(BIN, ['dev-disk-by\\x2duuid-aaaa.device']), 'hdd-policy': texts['server_os'],
                'host-backup': texts['host_backup:' + host_backup.BACKUP_UNIT],
                'host-resume': texts['host_backup:' + host_backup.RESUME_UNIT]}

    def service_lines(self, text):
        return text.split('[Service]\n', 1)[1].split('[Install]', 1)[0].splitlines()

    def test_each_gets_the_sandbox_it_can_take_and_nothing_that_breaks_it(self):
        for name, text in self.units().items():
            lines = self.service_lines(text)
            with self.subTest(unit=name):
                self.assertEqual(len(lines), len(set(lines)), lines)   # nothing set twice
                for option in self.BASE:
                    self.assertIn(option, lines)
                for option in self.NEVER:
                    self.assertFalse(any(line.startswith(option) for line in lines), option)
                namespaced = [line for line in lines if line.startswith(self.NAMESPACED)]
                self.assertEqual(namespaced, [] if name == 'drive-return' else
                                 ['PrivateTmp=yes', 'ProtectHome=' + ('yes' if name == 'hdd-policy' else 'read-only'),
                                  'ProtectKernelTunables=yes', 'ProtectControlGroups=yes'])

    def test_the_snapshot_may_still_read_the_home_it_backs_up(self):
        # Steam's files are in the chosen account's home: read-only, never hidden.
        text = self.units()['host-backup']
        self.assertIn('ProtectHome=read-only\n', text)
        self.assertNotIn('ProtectHome=yes', text)
