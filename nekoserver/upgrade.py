#!/usr/bin/env python3
"""Upgrade NekoServer itself to a newer release, as the dashboard's Updates page starts it
(the NekoServer tab, "Upgrade NekoServer"): a background service runs this, so closing
the page stops nothing.

  sudo python3 upgrade.py run --tag v0.47.2Beta    the upgrade (nekoserver-upgrade@<tag>)
  sudo python3 upgrade.py resume                   after a restart: put a cut-off one right

What it does, in order (a record in /etc/nekoserver-backups/upgrade.json says how far
it got, so a power cut at any point is put right at the next start):
1. Checks nothing else is at work: no setup, no unfinished app update or monthly check,
   no other upgrade. App updates and the monthly check wait while the record is there.
2. Downloads the release from GitHub (over HTTPS, from this project's tag) and checks it
   names the version it should. Releases are not signed yet: whoever can push a tag to
   the project's GitHub can publish a release (as for `git pull`).
3. Runs that release's setup with your saved settings, without questions. It never
   updates the system packages (that is a separate job you start yourself); on Arch a
   release that needs a new package stops and says to run setup in a terminal.
4. Checks the result: every step that was done before is done again, the services run
   the new release, and the dashboard answers with the new version.
5. If setup failed or the check did not pass, runs the previous release's setup again
   (kept on the SSD), so the server is back on the release it had."""

import argparse
import fcntl
import http.client
import json
import os
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from neko_common import (
    RECOVERY,
    SETUP_LOCK,
    DASHBOARD_DEFAULT_PORT,
    MAINTENANCE_JOURNAL,
    OPERATION_LOCK,
    VERSION,
    atomic_json,
    atomic_write,
    checked_settings,
    dashboard_port,
    default_settings,
    ensure_directory,
    fetch,
    hold_operation,
    mount_path,
    safe_path,
    settings_path,
)

JOURNAL = RECOVERY['upgrade'][0]
ROOT = Path(__file__).resolve().parent
MIGRATE_JOURNAL = Path('/etc/nekoserver-backups/migrate.json')  # migrate.py JOURNAL
FORMAT = 'neko-upgrade-v1'
ARCHIVE = 'https://codeload.github.com/fp581/NekoServer-All/tar.gz/refs/tags/{tag}'
TAG = re.compile(r'v(\d+)\.(\d+)\.(\d+)(Beta|-beta)?')  # a beta written either way (0.46.1 was v0.46.1-beta)
# The status the installer records for a step that worked (installer.py
# complete_status); a step waiting for you (Tailscale's sign-in, say) was waiting
# before too and is not the upgrade's doing.
DONE = 'success'
UNITS = Path('/etc/systemd/system')
# Where each release's files are kept on the SSD (modules/recovery.py).
RELEASES = 'installer/releases'
LIMIT = 200 * 1024 * 1024  # the release archive is a few MB


def numbers(text):
    """(0, 44, 0) from "v0.47.2Beta" or "0.47.2"; None for anything else."""
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
    if Path(MIGRATE_JOURNAL).exists():
        return 'A move to another drive is not finished yet (sudo python3 migrate.py resume)'
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
    """The release, unpacked into `work`/<tag>, checked to name that version (not signed:
    see the top of this file): returns its nekoserver folder."""
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
    releases = ssd(config) / RELEASES
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


def setup_profile(config, modules=None):
    """The profile this server's setup runs ("full" from the guided setup; a smaller one,
    or None for the steps switched on in the settings, when set up so by hand), as setup
    recorded it. The upgrade and the way back run the same one: they ran "full" for
    everyone, which set up apps a smaller installation never had (the review of 0.47.2).
    Not recorded yet (set up before 0.47.3): the smallest profile that covers every step
    done, so nothing is left out and nothing is added."""
    import installer

    profiles = config.get('profiles') or {}
    try:
        saved = json.loads((ssd(config) / 'installer/state' / installer.PROFILE_FILE).read_text())
    except (OSError, ValueError, RuntimeError, KeyError):
        saved = None
    if isinstance(saved, dict) and saved.get('format') == installer.PROFILE_FORMAT:
        chosen = saved.get('profile')
        if chosen is None or chosen in profiles:
            return chosen
    done = steps_done(config)
    modules = modules or installer.load_modules(ROOT / 'modules')
    fits = []
    for name, selected in profiles.items():
        try:
            plan = installer.ordered(modules, selected)
        except (KeyError, installer.InstallerError):
            continue
        if done <= set(plan):
            fits.append((len(plan), name != 'full', name))
    return min(fits)[2] if fits else 'full'


def run_setup(project, settings, account, log_file, run=subprocess.run):
    """That release's setup, with the saved settings, without questions and without
    updating the system, for the steps this server is set up with (setup_profile).
    Returns its exit code (1 also when a step only waits for you)."""
    profile = setup_profile(json.loads(settings_path(settings).read_text()))
    chosen = ['--profile', profile] if profile else []
    atomic_write(project / 'my-config.json', Path(settings).read_bytes(), 0o600)
    env = {**os.environ, 'NEKOSERVER_KEEP_SYSTEM': '1'}
    if account:
        env['SUDO_USER'] = account
    with open(log_file, 'ab') as log:
        return run(
            ['bash', str(project / 'install.sh'), *chosen, '--apply'],
            cwd=project,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=6 * 3600,
            check=False,
        ).returncode


def setup_problem(setup, project, settings, account, log_file):
    """Why a release's setup did not work, or None. Setup that could not be started or
    that ran past its time (six hours) is a failure like any other, never an exception
    that would skip putting the previous release back."""
    try:
        code = setup(project, settings, account, log_file)
    except subprocess.TimeoutExpired:
        return f'its setup ran for more than six hours and was stopped (the log: {log_file})'
    except (OSError, subprocess.SubprocessError) as exc:
        return f'its setup could not run ({exc}; the log: {log_file})'
    return f'its setup stopped (exit code {code}; the log says why: {log_file})' if code not in (0, 1) else None


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
        saved = json.loads((ssd(config) / RELEASES / ('v' + version) / 'config.json').read_text())
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
    for unit in installed_units(units):
        try:
            text = unit.read_text(errors='replace') if unit.is_file() else ''
        except OSError:
            continue
        if any(found != version for found in RELEASE_IN_PATH.findall(text)):
            behind.append(unit.name)
    return behind


UNIT_KINDS = ('.service', '.timer', '.socket')  # as the uninstaller (uninstaller.UNIT_KINDS)


def restart_pending(root='/'):
    """Why the upgrade can't start: an earlier system update waits for a restart. Setup
    then stops before changing anything (install.sh, exit 3), the new release's and the
    old one's alike, so the upgrade would only fail, then fail to go back (the audit of
    0.46.2). None when none waits, or it can't be told."""
    import host

    try:
        family = host.current()
    except (host.Unsupported, OSError):
        return None
    if family.reboot_pending(root):
        return 'This computer waits for a restart after a system update (restart it first)'
    return None


def installed_units(units=None):
    """NekoServer's unit files (`nekoserver-*` services, timers and sockets): never the
    copies kept beside them (`.before-<time>`, the audit of 0.46.2: such a copy names an old
    release and failed every upgrade's check)."""
    return sorted(p for p in Path(units or UNITS).glob('nekoserver-*') if p.suffix in UNIT_KINDS and p.is_file())


def stopped_for_good(name, run):
    """`name` switched off and no longer running. A template (`x@.service`) is no unit of its
    own: systemd can't stop it by that name, so it is switched off and its instances
    (`x@<anything>.service`) are stopped instead (the review of 0.47.0: the stop of the
    template's own name always failed, so the way back never passed)."""
    quiet = {'capture_output': True, 'text': True, 'check': False}
    if '@.' in name:
        instances = name.replace('@.', '@*.')
        off = run(['systemctl', 'disable', name], timeout=120, **quiet)
        stop = run(['systemctl', 'stop', instances], timeout=120, **quiet)
        left = run(
            ['systemctl', 'list-units', '--state=active,activating,deactivating', '--no-legend', '--plain', instances],
            timeout=60,
            **quiet,
        )
        return not off.returncode and not stop.returncode and not left.returncode and not (left.stdout or '').strip()
    stop = run(['systemctl', 'disable', '--now', name], timeout=120, **quiet)
    alive = run(['systemctl', 'is-active', '--quiet', name], timeout=60, **quiet)
    return not stop.returncode and alive.returncode != 0


def added_units(before, units=None, run=None):
    """The units the newer release added (not there before the upgrade), switched off and
    removed when its release is put back: the older setup writes only its own, so the new
    ones would keep running the newer release's program. Returns (removed, stuck): a unit
    that did not stop keeps its file, so it is still seen and the way back does not pass
    (the review of 0.46.3: a failed stop was ignored and the file removed, so the check
    after it missed the newer service that still ran)."""
    run = run or subprocess.run
    removed, stuck = [], []
    for unit in installed_units(units):
        if unit.name in before:
            continue
        if not stopped_for_good(unit.name, run):
            stuck.append(unit.name)
            continue
        unit.unlink(missing_ok=True)
        removed.append(unit.name)
    if removed:
        run(['systemctl', 'daemon-reload'], capture_output=True, text=True, timeout=120, check=False)
    return removed, stuck


KEEP_LOGS = 10


def tidy(work, journal=JOURNAL, keep_logs=KEEP_LOGS):
    """Once an upgrade has finished (its record is gone), its download and the release
    unpacked from it go, with the release's build folder (gigabytes): the release it set up
    keeps its own copy on the SSD (installer/releases). Only the newest logs stay. They
    stayed for good before (the review of 0.47.2). Nothing else in the folder is touched,
    and nothing while an upgrade is unfinished."""
    import shutil

    if Path(journal).exists() or not Path(work).is_dir():
        return []
    removed = []
    for entry in sorted(Path(work).iterdir()):
        archive = entry.name.endswith('.tar.gz') and TAG.fullmatch(entry.name[: -len('.tar.gz')])
        unpacked = TAG.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink()
        if archive and entry.is_file() and not entry.is_symlink():
            entry.unlink()
        elif unpacked:
            shutil.rmtree(entry)
        else:
            continue
        removed.append(entry.name)
    logs = sorted(
        (p for p in Path(work).glob('*.log') if p.is_file() and not p.is_symlink()), key=lambda p: p.stat().st_mtime
    )
    for old in logs[:-keep_logs] if keep_logs else logs:
        old.unlink()
        removed.append(old.name)
    return removed


BUSY_MOVE = 'A drive move (migrate.py) or another upgrade is at work'


def upgrade(tag, settings, out=print, **more):
    """The whole upgrade (see the top of this file), holding the operation lock from start
    to end, so a drive move started meanwhile (from a terminal too) waits. Returns 0 when
    the server runs the new release, 1 when it was put back on this one, 2 when it needs you."""
    held = hold_operation(OPERATION_LOCK)
    if held is None:
        out(BUSY_MOVE + '; the upgrade did not start, nothing was changed. Try again later.')
        return 2
    with held:
        code = upgrade_held(tag, settings, out, **more)
        tidy_after(settings, more.get('journal', JOURNAL), out)
        return code


def tidy_after(settings, journal, out=print):
    """tidy for this server's upgrade folder; a folder that can't be tidied never fails
    the upgrade."""
    try:
        config = json.loads(settings_path(settings).read_text())
        tidy(ssd(config) / 'installer/upgrades', journal)
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        out(f'The finished upgrade\'s files could not be tidied ({exc}); they stay in installer/upgrades')


def upgrade_held(
    tag, settings, out=print, get=fetch, setup=run_setup, check=None, journal=JOURNAL, recovery=None, lock=SETUP_LOCK
):
    config = json.loads(settings_path(settings).read_text())
    if not numbers(tag) or numbers(tag) <= numbers(VERSION):
        out(f'{tag} is not newer than this release ({VERSION}); nothing was changed')
        return 2
    busy = nothing_at_work(lock=lock, journal=journal, recovery=recovery) or restart_pending()
    if busy:
        out(busy + '; the upgrade did not start, nothing was changed. Try again later.')
        return 2
    work = ssd(config) / 'installer/upgrades'
    previous = kept_release(config, ssd(config) / RELEASES / ('v' + VERSION))
    if not (previous / 'install.sh').is_file():
        out(
            f'This release\'s files are not kept on the SSD ({previous}), so there would be no way back; run  sudo bash install.sh  first'
        )
        return 2
    account = owner(config)
    before = steps_done(config)
    log_file = work / (tag + '.log')
    record(
        'downloading',
        journal,
        to=tag,
        previous=str(previous),
        log=str(log_file),
        before=sorted(before),
        units=[unit.name for unit in installed_units()],
    )
    try:
        project = download(tag, work, get)
    except DOWNLOAD_ERRORS as exc:
        # A download cut off part-way (a short archive, a connection closed early) is one
        # of these too: the record goes, and app updates and the monthly check go on.
        journal.unlink(missing_ok=True)
        out(f'{exc or type(exc).__name__}; this server stays on {VERSION}')
        return 1
    out(f'Setting up {tag} (this takes a while: it builds the background program). The log: {log_file}')
    record('installing', journal, project=str(project))
    problem = setup_problem(setup, project, settings, account, log_file)
    if problem is None:
        record('verifying', journal)
        problem = checked(check or (lambda: verify(config, tag, before)))
    if problem is None:
        journal.unlink(missing_ok=True)
        out(f'NekoServer {tag} is installed and checked')
        return 0
    record('failed', journal, problem=problem)
    out(f'The upgrade to {tag} did not pass: {problem}. Putting {VERSION} back...')
    return go_back(previous, settings, account, log_file, out, setup, journal, before)


# What a download, an archive or a check over the network can raise when it fails part-way.
DOWNLOAD_ERRORS = (RuntimeError, OSError, tarfile.TarError, EOFError, ValueError, http.client.HTTPException)


def checked(check):
    """The check's answer: why the server does not pass it, or None. A check that itself
    fails (the dashboard answering half a reply, say) is a failed check, never an error
    that would leave the upgrade's record behind without putting the release back."""
    try:
        return check()
    except DOWNLOAD_ERRORS as exc:
        return f'the check could not be made ({exc or type(exc).__name__})'


def verify(config, tag, before):
    """The real check of the server for release `tag`."""
    return works_on(config, tag, before, lambda wanted: dashboard_version(config, wanted), services_behind)


def go_back(previous, settings, account, log_file, out=print, setup=run_setup, journal=JOURNAL, before=(), check=None):
    """The previous release's setup again, from its copy on the SSD, then the same check
    as after an upgrade: every step that worked before works again, and the dashboard and
    every service run that release. Only then is the upgrade's record removed; otherwise
    it stays, and nothing else (app updates, the monthly check) starts until you have
    finished it."""
    saved = record('going_back', journal)
    config = json.loads(settings_path(settings).read_text())
    stuck = []
    if isinstance(saved.get('units'), list):
        removed, stuck = added_units(set(saved['units']))
        for name in removed:
            out(f'Removed {name}: the newer release added it')
    if stuck:
        # The older setup never runs beside a newer release's service still at work (it
        # ran first before, and the failure was only said after it: the review of 0.47.2).
        out(
            'These services of the newer release did not stop: '
            + ', '.join(stuck)
            + f'. Nothing else was changed. Stop them (sudo systemctl stop {" ".join(stuck)}), then run  '
            'sudo python3 upgrade.py resume  to put the release back. App updates and the monthly check wait until then.'
        )
        return 2
    problem = setup_problem(setup, previous, settings, account, log_file)
    if problem is None:
        problem = checked(check or (lambda: verify(config, previous.name, before)))
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
    release it had (the new one may be half set up). It waits for the operation lock."""
    with hold_operation(OPERATION_LOCK, wait=True):
        code = resume_held(settings, out, setup, journal)
        tidy_after(settings, journal, out)
        return code


def resume_held(settings, out, setup, journal):
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


# The upgrade's report, beside the other jobs' (the dashboard's "last upgrade" line and
# its alert read it), and the service each kind of run is (the dashboard asks systemd
# whether a run said to be at work still is).
REPORT = 'reports/jobs/upgrade.json'
RESUME_UNIT = 'nekoserver-upgrade-resume'


def reported(config, unit, work, journal=JOURNAL, now=time.time, out=print):
    """Run work(out) and keep its report as the other jobs keep theirs: "running" while at
    work, then "pass" (upgraded), "partial" (nothing changed, or put back on this
    release: the apps work) or "fail" (it needs you: the record is still there). The
    reason is the last thing the upgrade said. The report is written only to the SSD
    when it is mounted, and a report that can't be written never stops the upgrade."""
    path = ssd(config) / REPORT
    try:
        old = json.loads(path.read_text())
    except (OSError, ValueError):
        old = {}
    said = []

    def told(line):
        said.append(str(line))
        out(line)

    row = {
        'job': 'upgrade',
        'unit': unit,
        'status': 'running',
        'started_at': now(),
        'finished_at': None,
        'last_success': old.get('last_success') if isinstance(old, dict) else None,
    }
    keep_report(path, row)
    code = 2
    try:
        code = work(told)
    except Exception as exc:
        said.append(f'the upgrade stopped on an error ({type(exc).__name__}: {exc})')
        raise
    finally:
        last = said[-1] if said else 'the upgrade stopped without saying why'
        row['finished_at'] = now()
        if code == 0:
            row.update(status='pass', last_success=row['finished_at'])
        elif code == 2 and journal.exists():
            row.update(status='fail', reason=last)
        else:
            row.update(status='partial', missing=[last], last_partial=row['finished_at'])
        keep_report(path, row)
    return code


def keep_report(path, row):
    if not os.path.ismount(path.parents[2]):
        return
    try:
        ensure_directory(path.parent)
        atomic_json(path, row)
    except OSError as exc:
        print(f'The upgrade report could not be written ({exc}); the upgrade goes on', file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('run', 'resume'))
    parser.add_argument('--tag', help='with run: the release, such as v0.47.2Beta')
    parser.add_argument(
        '--config', type=lambda v: Path(checked_settings(v)), default=default_settings(Path(__file__).parent)
    )
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run it with sudo')
    if args.action == 'run' and not (args.tag and TAG.fullmatch(args.tag)):
        parser.error('run needs --tag with a release, such as v0.47.2Beta')
    config = json.loads(settings_path(args.config).read_text())
    if args.action == 'resume':
        return reported(config, RESUME_UNIT, lambda out: resume(args.config, out))
    recovery = ssd(config) / 'installer/recovery.lock'
    return reported(
        config, 'nekoserver-upgrade@' + args.tag, lambda out: upgrade(args.tag, args.config, out, recovery=recovery)
    )


if __name__ == '__main__':
    sys.exit(main())
