#!/usr/bin/env python3
"""What the Updates page's System tab shows: the Linux package updates waiting, whether
the computer needs a restart, and the system's release. Read-only: it never installs,
removes or refreshes a package (installing them is a separate job you will start
yourself; see MONTHLY-MAINTENANCE.md, "System updates").

  sudo python3 system_updates.py check     what nekoserver-system-check.timer runs daily

The answer is written to backups/updates/system.json on the SSD for the dashboard."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import host
from neko_common import atomic_json, checked_settings, default_settings, ensure_directory, mount_path, settings_path

FORMAT = 'neko-system-check-v1'
SHOWN = 300  # packages listed at most (the count is always exact)
# Packages whose update needs a restart of the computer to take effect.
REBOOT = re.compile(
    r'^(linux|linux-(lts|zen|hardened)|linux-image-\S+|kernel(-core|-default)?|systemd|glibc|libc6|intel-ucode|amd-ucode|linux-firmware)$'
)
# Packages whose update restarts a service NekoServer's apps depend on: Docker stops and
# starts every app, Samba the shared folders, Tailscale the remote access.
RESTARTS = (
    (re.compile(r'^(docker|docker-ce|docker\.io|moby|containerd)'), 'Docker (every app restarts)'),
    (re.compile(r'^(samba|smbd)'), 'Samba (the shared folders)'),
    (re.compile(r'^tailscale'), 'Tailscale (remote access)'),
    (re.compile(r'^(openssh|ssh)'), 'SSH'),
)


def call(argv, timeout=600):
    """A read-only query: (exit code, output). A program that is not there is code 127."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, ''
    except subprocess.TimeoutExpired:
        return 124, ''
    return done.returncode, done.stdout


def pending(family, run=call):
    """The package updates waiting, each with what it means; or None and why it is not known."""
    argv, good, read, missing = host.UPDATE_QUERIES[family]
    code, text = run(argv)
    if code == 127:
        return None, missing or f'{argv[0]} is not installed'
    if code not in good:
        return None, f'{" ".join(argv[:2])} did not answer (exit code {code})'
    found = read(text)
    for package in found:
        package['reboot'] = bool(REBOOT.match(package['name']))
        package['restarts'] = [what for pattern, what in RESTARTS if pattern.match(package['name'])]
    return found, None


def restart_needed(family, run=call, running=None, modules=Path('/usr/lib/modules'), flag=Path('/run/reboot-required')):
    """Whether the computer needs a restart now (an update installed earlier waits for
    one), and why."""
    reasons = []
    kernel = running or os.uname().release
    if modules.is_dir() and not (modules / kernel).is_dir():
        reasons.append(f'the running Linux kernel ({kernel}) was replaced by an update')
    if family == 'debian' and Path(flag).exists():
        reasons.append('the system says so (/run/reboot-required)')
    if family in ('fedora', 'rhel') and run(['needs-restarting', '-r'], 120)[0] == 1:
        reasons.append('the system says so (needs-restarting -r)')
    if family == 'suse' and run(['zypper', '--non-interactive', 'needs-rebooting'], 120)[0] == 102:
        reasons.append('the system says so (zypper needs-rebooting)')
    return bool(reasons), reasons


def status(run=call, release=None, now=None, **restart):
    """Everything the System tab shows."""
    release = release if release is not None else host.os_release()
    family = host.detect()
    name = family.name if family else None
    result = {
        'format': FORMAT,
        'checked_at': now or time.time(),
        'system': release.get('PRETTY_NAME') or release.get('ID') or 'unknown',
        'family': name,
    }
    if name not in host.UPDATE_QUERIES:
        result['note'] = 'This system has no NekoServer package support, so its updates are not listed'
        return result
    found, why = pending(name, run)
    result['updates'] = None if found is None else found[:SHOWN]
    result['count'] = None if found is None else len(found)
    result['note'] = why
    result['restart_needed'], result['restart_reasons'] = restart_needed(name, run, **restart)
    return result


def check(settings):
    config = json.loads(settings_path(settings).read_text())
    folder = mount_path(config['data_mount']) / 'backups/updates'
    ensure_directory(folder, 0o700)
    result = status()
    atomic_json(folder / 'system.json', result)
    count = result.get('count')
    print(
        'System: '
        + (
            f'{count} package updates waiting'
            if count is not None
            else 'updates not known (' + str(result['note']) + ')'
        )
        + ('; a restart is needed' if result.get('restart_needed') else '')
    )
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('check',))
    parser.add_argument(
        '--config', type=lambda v: Path(checked_settings(v)), default=default_settings(Path(__file__).parent)
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run it with sudo')
    return check(args.config)


if __name__ == '__main__':
    sys.exit(main())
