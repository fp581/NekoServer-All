#!/usr/bin/env python3
"""Upgrade NekoServer itself to a newer release, as the dashboard's Updates page starts it
(the NekoServer tab, "Upgrade NekoServer"): a background service runs this, so closing
the page stops nothing.

  sudo python3 upgrade.py run --tag v0.45.1Beta    the upgrade (nekoserver-upgrade@<tag>)
  sudo python3 upgrade.py resume                   after a restart: put a cut-off one right

What it does, in order (a record in /etc/nekoserver-backups/upgrade.json says how far
it got, so a power cut at any point is put right at the next start):
1. Checks nothing else is at work: no setup, no unfinished app update or monthly check,
   no other upgrade. App updates and the monthly check wait while the record is there.
2. Downloads the release from GitHub and checks it is the release it says it is.
3. Runs that release's setup with your saved settings, without questions. It never
   updates the system packages (that is a separate job you start yourself); on Arch a
   release that needs a new package stops and says to run setup in a terminal.
4. Checks the result: every step that was done before is done again, the services run
   the new release, and the dashboard answers with the new version.
5. If setup failed or the check did not pass, runs the previous release's setup again
   (kept on the SSD), so the server is back on the release it had."""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from neko_common import (
    DASHBOARD_DEFAULT_PORT,
    MAINTENANCE_JOURNAL,
    VERSION,
    atomic_json,
    atomic_write,
    checked_settings,
    dashboard_port,
    default_settings,
    ensure_directory,
    fetch,
    mount_path,
    safe_path,
    settings_path,
)

JOURNAL = Path('/etc/nekoserver-backups/upgrade.json')
FORMAT = 'neko-upgrade-v1'
ARCHIVE = 'https://codeload.github.com/fp581/NekoServer-All/tar.gz/refs/tags/{tag}'
TAG = re.compile(r'v(\d+)\.(\d+)\.(\d+)(Beta)?')
SETUP_LOCK = Path('/run/nekoserver-setup.lock')
# The status the installer records for a step that worked (installer.py
# complete_status); a step waiting for you (Tailscale's sign-in, say) was waiting
# before too and is not the upgrade's doing.
DONE = 'success'
UNITS = Path('/etc/systemd/system')
LIMIT = 200 * 1024 * 1024  # the release archive is a few MB


def numbers(text):
    """(0, 44, 0) from "v0.45.1Beta" or "0.45.1"; None for anything else."""
    match = TAG.fullmatch(text if text.startswith('v') else 'v' + text)
    return tuple(int(n) for n in match.groups()[:3]) if match else None


def record(phase, journal=JOURNAL, **more):
    saved = read(journal) or {'format': FORMAT, 'from': VERSION, 'started_at': time.time()}
    saved.update(phase=phase, at=time.time(), **more)
    ensure_directory(journal.parent, 0o700)
    atomic_json(journal, saved)
    return saved


def read(journal=JOURNAL):
    try:
        saved = json.loads(Path(journal).read_text())
    except (OSError, ValueError):
        return None
    return saved if isinstance(saved, dict) and saved.get('format') == FORMAT else None


def nothing_at_work(lock=SETUP_LOCK, journal=JOURNAL, maintenance=None, recovery=None):
    """Why the upgrade cannot start now, or None. Setup holds its lock while it runs;
    an unfinished app update or monthly check is finished first; an upgrade already
    recorded is not started twice. The backups' lock is tried too: a backup or an app
    update at work is waited for by setup, but an upgrade would then sit for hours."""
    if Path(journal).exists():
        return 'A NekoServer upgrade is already at work (or one was cut off and is being put right)'
    if Path(maintenance or MAINTENANCE_JOURNAL).exists():
        return 'An app update or the monthly check is not finished yet; it is finished by itself first'
    for path, what in (
        (lock, 'Setup (sudo bash install.sh) is running'),
        (recovery, 'A backup or an app update is running'),
    ):
        if path is None:
            continue
        try:
            with open(path, 'a') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return what
        except OSError:
            continue
    return None


def owner(config):
    """The account the server was set up for (its shares, its TV), which setup needs as
    SUDO_USER when it runs without you; None when no earlier run recorded one."""
    from modules import samba, tv

    return samba.previous_account() or tv.previous_user(config)


def download(tag, work, get=fetch):
    """The release, unpacked into `work`/<tag>, checked to be that release: returns its
    nekoserver folder."""
    ensure_directory(work, 0o700)
    archive = safe_path(work / (tag + '.tar.gz'))
    payload = get(ARCHIVE.format(tag=tag), 'NekoServer ' + tag, LIMIT + 1, timeout=300)
    if len(payload) > LIMIT:
        raise RuntimeError(f'The download of {tag} is larger than any NekoServer release; nothing was changed')
    atomic_write(archive, payload, 0o600)
    target = safe_path(work / tag)
    if target.exists():
        import shutil

        shutil.rmtree(target)
    ensure_directory(target, 0o700)
    with tarfile.open(archive) as bundle:
        # The 'data' filter refuses links out of the folder, devices and absolute paths.
        bundle.extractall(target, filter='data')
    tops = [p for p in target.iterdir() if p.is_dir()]
    if len(tops) != 1 or not (tops[0] / 'nekoserver/install.sh').is_file():
        raise RuntimeError(f'The download of {tag} is not a NekoServer release; nothing was changed')
    project = tops[0] / 'nekoserver'
    found = re.search(r"^VERSION = '([0-9.]+)'$", (project / 'neko_common.py').read_text(), re.MULTILINE)
    if not found or numbers(found[1]) != numbers(tag):
        raise RuntimeError(
            f'The download says it is {found[1] if found else "no release"}, not {tag}; nothing was changed'
        )
    return project


def ssd(config):
    """The application SSD the settings name, checked to be a safe mount folder."""
    return mount_path(config['data_mount'])


def kept_release(config, folder):
    """A release's copy on the SSD (installer/releases/v<numbers>), checked to be one."""
    releases = ssd(config) / 'installer/releases'
    path = safe_path(Path(folder))
    if path.parent != releases or not re.fullmatch(r'v\d+\.\d+\.\d+', path.name):
        raise RuntimeError(f'Not a kept NekoServer release: {folder}')
    return path


def steps_done(config):
    """The setup steps recorded as done (the record on the SSD)."""
    try:
        states = json.loads((ssd(config) / 'installer/state/state.json').read_text())
    except (OSError, ValueError, KeyError, RuntimeError):
        return set()
    return {name for name, state in states.items() if isinstance(state, dict) and state.get('status') == DONE}


def run_setup(project, settings, account, log_file, run=subprocess.run):
    """That release's setup, with the saved settings, without questions and without
    updating the system. Returns its exit code (1 also when a step only waits for you)."""
    atomic_write(project / 'my-config.json', Path(settings).read_bytes(), 0o600)
    env = {**os.environ, 'NEKOSERVER_KEEP_SYSTEM': '1'}
    if account:
        env['SUDO_USER'] = account
    with open(log_file, 'ab') as log:
        return run(
            ['bash', str(project / 'install.sh'), '--profile', 'full', '--apply'],
            cwd=project,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=6 * 3600,
            check=False,
        ).returncode


def works_on(config, tag, before, version_now, running_now):
    """Why the server does not pass its check for release `tag` (after the upgrade, or
    after the previous release was put back), or None: a step that worked before does not
    now, the dashboard does not answer with that release, or an installed service still
    runs another one."""
    lost = sorted(set(before) - steps_done(config))
    if lost:
        return 'these steps worked before and do not now: ' + ', '.join(lost)
    wanted = '.'.join(str(n) for n in numbers(tag))
    if version_now(wanted) != wanted:
        return f'the dashboard does not answer with {wanted}'
    behind = running_now(wanted)
    if behind:
        return f'these services do not run {wanted}: ' + ', '.join(behind)
    return None


def dashboard_port_of(config, version):
    """The dashboard's port as that release's setup left it (setup picks another one when
    8787 is taken and keeps it in the settings it saves with the release on the SSD)."""
    try:
        saved = json.loads((ssd(config) / 'installer/releases' / ('v' + version) / 'config.json').read_text())
    except (OSError, ValueError):
        saved = config
    return dashboard_port(saved) or dashboard_port(config) or DASHBOARD_DEFAULT_PORT


def dashboard_version(config, version, timeout=10):
    """The version the dashboard answers with, on the port that release uses."""
    import urllib.request

    port = dashboard_port_of(config, version)
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status', timeout=timeout) as answer:
            return json.loads(answer.read(4_000_000)).get('version')
    except (OSError, ValueError):
        return None


RELEASE_IN_PATH = re.compile(r'/v(\d+\.\d+\.\d+)/')


def services_behind(version, units=UNITS):
    """NekoServer's installed services and timers whose definition names another
    release's files (each release has its own folder: /v<version>/): every backup worker,
    the dashboard and the rest must run `version`."""
    behind = []
    for unit in sorted(Path(units).glob('nekoserver-*')):
        try:
            text = unit.read_text(errors='replace') if unit.is_file() else ''
        except OSError:
            continue
        if any(found != version for found in RELEASE_IN_PATH.findall(text)):
            behind.append(unit.name)
    return behind


def upgrade(
    tag, settings, out=print, get=fetch, setup=run_setup, check=None, journal=JOURNAL, recovery=None, lock=SETUP_LOCK
):
    """The whole upgrade (see the top of this file). Returns 0 when the server runs the
    new release, 1 when it was put back on this one, 2 when it needs you."""
    config = json.loads(settings_path(settings).read_text())
    if not numbers(tag) or numbers(tag) <= numbers(VERSION):
        out(f'{tag} is not newer than this release ({VERSION}); nothing was changed')
        return 2
    busy = nothing_at_work(lock=lock, journal=journal, recovery=recovery)
    if busy:
        out(busy + '; the upgrade did not start, nothing was changed. Try again later.')
        return 2
    work = ssd(config) / 'installer/upgrades'
    previous = kept_release(config, ssd(config) / 'installer/releases' / ('v' + VERSION))
    if not (previous / 'install.sh').is_file():
        out(
            f'This release\'s files are not kept on the SSD ({previous}), so there would be no way back; run  sudo bash install.sh  first'
        )
        return 2
    account = owner(config)
    before = steps_done(config)
    log_file = work / (tag + '.log')
    record('downloading', journal, to=tag, previous=str(previous), log=str(log_file), before=sorted(before))
    try:
        project = download(tag, work, get)
    except (RuntimeError, OSError, tarfile.TarError) as exc:
        journal.unlink(missing_ok=True)
        out(f'{exc}; this server stays on {VERSION}')
        return 1
    out(f'Setting up {tag} (this takes a while: it builds the background program). The log: {log_file}')
    record('installing', journal, project=str(project))
    code = setup(project, settings, account, log_file)
    problem = f'its setup stopped (exit code {code}; the log says why: {log_file})' if code not in (0, 1) else None
    if problem is None:
        record('verifying', journal)
        problem = (check or (lambda: verify(config, tag, before)))()
    if problem is None:
        journal.unlink(missing_ok=True)
        out(f'NekoServer {tag} is installed and checked')
        return 0
    out(f'The upgrade to {tag} did not pass: {problem}. Putting {VERSION} back...')
    return go_back(previous, settings, account, log_file, out, setup, journal, before)


def verify(config, tag, before):
    """The real check of the server for release `tag`."""
    return works_on(config, tag, before, lambda wanted: dashboard_version(config, wanted), services_behind)


def go_back(previous, settings, account, log_file, out=print, setup=run_setup, journal=JOURNAL, before=(), check=None):
    """The previous release's setup again, from its copy on the SSD, then the same check
    as after an upgrade: every step that worked before works again, and the dashboard and
    every service run that release. Only then is the upgrade's record removed; otherwise
    it stays, and nothing else (app updates, the monthly check) starts until you have
    finished it."""
    record('going_back', journal)
    config = json.loads(settings_path(settings).read_text())
    code = setup(previous, settings, account, log_file)
    problem = f'its setup stopped (exit code {code})' if code not in (0, 1) else None
    if problem is None:
        problem = (check or (lambda: verify(config, previous.name, before)))()
    if problem is None:
        journal.unlink(missing_ok=True)
        out(f'This server runs {previous.name.lstrip("v")} again, as before the upgrade, and passed its check')
        return 1
    out(
        f'Putting {previous.name} back did not pass either: {problem}. Run  sudo bash install.sh  in {previous} '
        f'(it carries on where it stopped); the log: {log_file}. App updates and the monthly check wait until then.'
    )
    return 2


def resume(settings, out=print, setup=run_setup, journal=JOURNAL):
    """At boot: an upgrade that was cut off. One cut off while it downloaded changed
    nothing; one cut off while it set up or checked the new release is put back on the
    release it had (the new one may be half set up)."""
    saved = read(journal)
    if saved is None:
        if journal.exists():
            out(f'The upgrade record {journal} is not readable; look at it, then remove it')
            return 2
        return 0
    if saved.get('phase') == 'downloading':
        journal.unlink(missing_ok=True)
        out('An upgrade was cut off while it downloaded; nothing had changed')
        return 0
    config = json.loads(settings_path(settings).read_text())
    previous = kept_release(config, saved.get('previous', ''))
    out('An upgrade was cut off before it was checked; putting ' + previous.name + ' back')
    log_file = ssd(config) / 'installer/upgrades' / ((saved.get('to') or 'resume') + '.log')
    before = [step for step in saved.get('before') or [] if isinstance(step, str)]
    return go_back(previous, settings, owner(config), log_file, out, setup, journal, before)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('run', 'resume'))
    parser.add_argument('--tag', help='with run: the release, such as v0.45.1Beta')
    parser.add_argument(
        '--config', type=lambda v: Path(checked_settings(v)), default=default_settings(Path(__file__).parent)
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run it with sudo')
    if args.action == 'resume':
        return resume(args.config)
    if not args.tag:
        parser.error('run needs --tag')
    config = json.loads(settings_path(args.config).read_text())
    return upgrade(args.tag, args.config, recovery=ssd(config) / 'installer/recovery.lock')


if __name__ == '__main__':
    sys.exit(main())
