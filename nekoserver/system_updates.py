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
from neko_common import (
    atomic_json,
    checked_settings,
    default_settings,
    ensure_directory,
    mount_path,
    settings_path,
    verify_storage,
)

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


def security_fixes(family, run=call):
    """The names of the waiting packages that fix a security issue, or (None, why) when
    that is not known here. Debian and Ubuntu: None too, as their list says it itself."""
    if family not in host.SECURITY_QUERIES:
        return None, host.SECURITY_UNKNOWN.get(family)
    argv, good, read, missing = host.SECURITY_QUERIES[family]
    code, text = run(argv)
    if code == 127:
        return None, missing or f'{argv[0]} is not installed'
    if code not in good:
        return None, f'{" ".join(argv[:3])} did not answer (exit code {code})'
    return read(text), None


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


# What the system's own restart check answers, by family: the command, and its exit
# codes for "a restart is needed" and "it is not". Any other answer (the tool missing,
# an error) is "not known", never "not needed".
RESTART_CHECKS = {
    'fedora': (['needs-restarting', '-r'], 1, 0),
    'rhel': (['needs-restarting', '-r'], 1, 0),
    'suse': (['zypper', '--non-interactive', 'needs-rebooting'], 102, 0),
}
REQUIRED, NOT_REQUIRED, UNKNOWN = 'required', 'not_required', 'unknown'


def restart_needed(family, run=call, running=None, modules=Path('/usr/lib/modules'), flag=Path('/run/reboot-required')):
    """Whether the computer needs a restart now (an update installed earlier waits for
    one): REQUIRED, NOT_REQUIRED or UNKNOWN, with the reasons (why it is needed, or why
    it could not be told)."""
    needed, unknown = [], []
    kernel = running or os.uname().release
    if not modules.is_dir():
        unknown.append(f'{modules} is not there, so the installed kernels cannot be compared with the running one')
    elif not (modules / kernel).is_dir():
        needed.append(f'the running Linux kernel ({kernel}) was replaced by an update')
    elif family == 'debian':
        # Debian keeps the running kernel's modules when a newer kernel is installed, and
        # writes /run/reboot-required only with update-notifier: a newer kernel of the same
        # kind waiting is a restart needed too (the review of 0.46.1).
        newer = newer_kernel(kernel, [p.name for p in modules.iterdir() if p.is_dir()])
        if newer:
            needed.append(f'a newer Linux kernel ({newer}) is installed and starts at the next restart')
    if family == 'debian' and Path(flag).exists():
        needed.append('the system says so (/run/reboot-required)')
    if family in RESTART_CHECKS:
        argv, yes, no = RESTART_CHECKS[family]
        code = run(argv, 120)[0]
        if code == yes:
            needed.append(f'the system says so ({" ".join(argv[:: len(argv) - 1])})')
        elif code == 127:
            unknown.append(f'{argv[0]} is not installed, so the system\'s own check could not be asked')
        elif code != no:
            unknown.append(f'{" ".join(argv)} did not answer (exit code {code})')
    if needed:
        return REQUIRED, needed
    return (UNKNOWN, unknown) if unknown else (NOT_REQUIRED, [])


def newer_kernel(running, installed):
    """The newest of `installed` (kernel versions, as /usr/lib/modules names them) that is
    of the running kernel's kind (amd64, rt-amd64, generic...) and newer than it, or None."""

    def parts(name):
        """((6, 1, 0, 26), 'amd64') from '6.1.0-26-amd64' (Debian 12, Ubuntu), and
        ((6, 12, 48, 0), 'amd64') from '6.12.48+deb13-amd64' (Debian 13: its release tag
        after a +, the review of 0.46.3); None for another form."""
        version, _, rest = name.partition('-')
        version = version.partition('+')[0]
        numbers = version.split('.')
        if not all(n.isdigit() for n in numbers):
            return None
        build, _, kind = rest.partition('-')
        if not build.isdigit():
            build, kind = '0', rest
        return (*map(int, numbers), int(build)), kind

    mine = parts(running)
    if mine is None:
        return None
    newer = [(p[0], name) for name in installed if (p := parts(name)) and p[1] == mine[1] and p[0] > mine[0]]
    return max(newer)[1] if newer else None


def mark_security(family, found, run=call):
    """Mark which of the updates `found` fix a security issue: True or False as the
    system says, None where it can't say (shown as "not known", never as "no"). Returns
    why it is not known, or None. Debian and Ubuntu say it in the list itself."""
    if not found or (family not in host.SECURITY_QUERIES and family not in host.SECURITY_UNKNOWN):
        return None
    fixes, unknown = security_fixes(family, run)
    for package in found:
        package['security'] = None if fixes is None else package['name'] in fixes
    return None if fixes is not None else 'Which updates fix a security issue is not known: ' + unknown


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
    note = mark_security(name, found, run)
    if note:
        result['security_note'] = note
    result['updates'] = None if found is None else found[:SHOWN]
    result['count'] = None if found is None else len(found)
    result['note'] = why
    result['restart'], result['restart_reasons'] = restart_needed(name, run, **restart)
    result['restart_needed'] = result['restart'] == REQUIRED
    return result


def check(settings):
    config = json.loads(settings_path(settings).read_text())
    # The SSD must be the one the settings name, mounted: with it missing, its folder is
    # a plain one on the system disk, and the answer would be written there.
    verify_storage(config, keys=('data_ssd',))
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
        + {REQUIRED: '; a restart is needed', UNKNOWN: '; whether a restart is needed is not known'}.get(
            result.get('restart'), ''
        )
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
    try:
        return check(args.config)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        # Said in a line (the timer's log), not as a Python traceback: an SSD not mounted
        # is the usual one; the next daily check tries again.
        print(f'System: not checked: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())


# SonarCloud probe (temporary, taken out again in the next commit).
