"""What this computer can do, found by looking rather than by the distribution's name.

Setup stops unless the computer has an x86-64 processor, systemd 247 or newer,
Python 3.11 or newer, a supported Linux family and no restart left pending by an
earlier update. Docker Engine is required too; setup installs it when missing. It also
stops while SELinux is enforcing, which NekoServer is not tested with yet (Fedora has
it on by default), and says how to try permissive mode and report what it logs.
Everything else (AppArmor, firewall, desktop) is reported so a beginner can see what
was found.
"""
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import host

MIN_PYTHON = (3, 11)
MIN_SYSTEMD = 247       # first release with every unit option NekoServer writes
# Firewalls that are switched on block Samba and the home gateway until they allow them
# (ports Docker publishes skip them, which is why the apps listen only on 127.0.0.1).
FIREWALLS = ('firewalld', 'nftables')
# Not tested with SELinux yet: Samba, the home gateway (Caddy on ports 8442-8447) and the
# runtime the services start from the SSD may all be refused, in ways that are hard to see.
SELINUX_ENFORCING = ('SELinux is enforcing, and NekoServer has not been tested with SELinux yet (the shared folders, '
                     'the home gateway and the backup jobs may be refused in ways that are hard to see). To try it, '
                     'switch SELinux to permissive: sudo setenforce 0, and SELINUX=permissive in /etc/selinux/config '
                     'so it stays after a restart, then run setup again. Permissive mode only logs what SELinux would '
                     'have refused: please send the output of  sudo ausearch -m AVC -ts recent  with your report, so '
                     'the right rules can be added')


def firewall_hint(firewalls):
    """How to let the home network reach the shared folders and the home addresses."""
    if 'firewalld' in firewalls:
        example = ('sudo firewall-cmd --permanent --add-service=samba --add-port=8442-8447/tcp '
                   '--add-port=8450-8499/tcp && sudo firewall-cmd --reload')
    else:
        example = ('sudo ufw allow from 192.168.1.0/24 to any port 445,8442:8447,8450:8499 proto tcp  '
                   '(your network instead of 192.168.1.0/24)')
    return (', '.join(firewalls) + ' is on: allow your home network to reach shared folders (port 445) and the home '
            'addresses (ports 8442-8447, or 8450-8499 when one of those is taken), for example  ' + example
            + '. Tailscale needs no rule')


def _read(root, path):
    try:
        return (Path(root) / path.lstrip('/')).read_text(errors='replace').strip()
    except OSError:
        return ''


def _output(run, argv):
    try:
        result = run(argv, capture_output=True, text=True, timeout=15, check=False, env=host.environment())
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout.strip() if result.returncode == 0 else ''


def systemd_version(run=subprocess.run):
    """systemd's release number (0 when systemctl is missing or says something unexpected)."""
    match = re.match(r'systemd (\d+)', _output(run, ['systemctl', '--version']))
    return int(match.group(1)) if match else 0


def docker_versions(run=subprocess.run, which=shutil.which):
    """Docker Engine's and Compose's versions ('' for each that isn't installed or running)."""
    docker = _output(run, ['docker', 'version', '--format', '{{.Server.Version}}']) if which('docker') else ''
    return docker, _output(run, ['docker', 'compose', 'version', '--short']) if docker else ''


def firewall(run=subprocess.run, root='/'):
    """The host firewalls that are switched on."""
    on = ['ufw'] if re.search(r'(?m)^ENABLED=yes', _read(root, '/etc/ufw/ufw.conf')) else []
    return on + [name for name in FIREWALLS if _output(run, ['systemctl', 'is-active', name + '.service']) == 'active']


def row(name, label, found, ok, required=False, problem=None):
    """`problem`: what setup says when a required row is not met (default: "is required")."""
    return {'name': name, 'label': label, 'found': found, 'ok': bool(ok), 'required': required, 'problem': problem}


def docker_rows(docker, compose, installed):
    """Installed but not running is the normal state before the Docker step: the
    packages are installed switched off, and that step starts it on the SSD."""
    missing = 'not installed yet (setup installs it)'
    return [
        row('docker', 'Docker Engine',
            docker or ('installed, not started yet (the Docker step starts it)' if installed else missing), True),
        row('compose', 'Docker Compose', compose or ('checked once Docker runs' if installed else missing), True),
    ]


def detect(run=subprocess.run, root='/', machine=None, python=None, which=shutil.which):
    """One row per finding: name, label, what was found, whether it is fine, and whether
    setup must stop without it."""
    machine = machine or platform.machine()
    python = python or sys.version_info[:2]
    family = host.detect(str(Path(root) / 'etc/os-release'))
    systemd = systemd_version(run) if which('systemctl') else 0
    selinux = _read(root, '/sys/fs/selinux/enforce')
    apparmor = _read(root, '/sys/module/apparmor/parameters/enabled')
    firewalls = firewall(run, root)
    restart = family is not None and family.reboot_pending(root)
    docker, compose = docker_versions(run, which)
    target = _output(run, ['systemctl', 'get-default']) if systemd else ''
    return [
        row('architecture', 'x86-64 processor', machine, machine == 'x86_64', True),
        row('python', 'Python 3.11 or newer', '.'.join(map(str, python)), tuple(python) >= MIN_PYTHON, True),
        row('systemd', 'systemd ' + str(MIN_SYSTEMD) + ' or newer', str(systemd) if systemd else 'not found',
            systemd >= MIN_SYSTEMD, True),
        row('family', 'Supported Linux family', family.label if family else 'not supported yet', family, True),
        row('restart', 'No restart pending from an earlier update',
            'restart needed (sudo reboot)' if restart else 'none', not restart, True),
        row('cgroup', 'Control groups v2', 'yes' if _read(root, '/sys/fs/cgroup/cgroup.controllers') else 'no',
            _read(root, '/sys/fs/cgroup/cgroup.controllers')),
        *docker_rows(docker, compose, bool(which('docker'))),
        row('selinux', 'SELinux', {'1': 'enforcing', '0': 'permissive'}.get(selinux, 'off'), selinux != '1', True,
            problem=SELINUX_ENFORCING),
        row('apparmor', 'AppArmor', 'on' if apparmor.upper().startswith('Y') else 'off', True),
        row('firewall', 'Firewall', firewall_hint(firewalls) if firewalls else 'off', not firewalls),
        row('desktop', 'Desktop', 'yes' if target == 'graphical.target' else 'no (server)', True),
    ]


def value(rows, name):
    return next(row['found'] for row in rows if row['name'] == name)


def missing(rows):
    """The required things this computer lacks, as plain sentences."""
    return [row.get('problem') or row['label'] + ' is required (found: ' + row['found'] + ')'
            for row in rows if row['required'] and not row['ok']]


def report(rows, log=print):
    log('This computer:')
    for row in rows:
        log('  ' + ('ok   ' if row['ok'] else 'MISSING ' if row['required'] else 'note ')
            + row['label'] + ': ' + row['found'])


def require(rows):
    problems = missing(rows)
    if problems:
        raise RuntimeError('This computer cannot run NekoServer setup: ' + '; '.join(problems))
