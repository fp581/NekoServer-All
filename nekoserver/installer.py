#!/usr/bin/env python3
"""NekoServer modular, standard-library-only orchestration framework."""

import argparse
import fcntl
import stat
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
import capabilities
import guided_setup
import host
import setup_progress
from neko_common import (
    atomic_json,
    mount_path,
    relative_path,
    VERSION,
    safe_path,
    ensure_directory,
    settings_path,
    pending_maintenance,
    default_settings,
    NO_SETTINGS,
    BUNDLE_MANIFEST,
)

# The installer's own log lines (a step's status), as against a step's own words.
MARKERS = ('START ', 'PASS ', 'FAILED ', 'NEEDS SETUP ', 'UNAVAILABLE ', 'BLOCKED ', 'Finished:')

ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = 'config.json'  # the example settings that come with NekoServer
DEFAULT_STATE = Path.home() / '.local' / 'state' / 'nekoserver'


class InstallerError(Exception):
    pass


class Context:
    def __init__(self, config, logger, apply=False, config_path=None):
        self.config = config
        self.log = logger
        self.apply = apply
        self.config_path = Path(config_path) if config_path else None
        self.original_config_path = self.config_path

    def persist_config(self):
        """Save the settings (drives chosen, say) to the settings file. The file keeps its
        owner: the wizard makes my-config.json yours to edit, and setup runs as root."""
        for path in dict.fromkeys(p for p in (self.original_config_path, self.config_path) if p is not None):
            owner = (path.stat().st_uid, path.stat().st_gid) if path.exists() else None
            atomic_json(path, self.config)
            if owner is not None and os.geteuid() == 0:
                os.chown(path, *owner)


def read_json(path):
    """A settings or state file (.json): absolute, no '..', no symlinks along the way."""
    try:
        checked = settings_path(path)
    except RuntimeError as exc:
        raise InstallerError(str(exc)) from None
    with checked.open(encoding='utf-8') as f:
        return json.load(f)


def load_modules(directory):
    modules = {}
    for path in sorted(directory.glob('*.py')):
        if path.name.startswith('_'):
            continue
        spec = importlib.util.spec_from_file_location(f'nekoserver_{path.stem}', path)
        obj = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(obj)
        deps = list(getattr(obj, 'DEPENDENCIES', []))
        if not hasattr(obj, 'run'):
            raise InstallerError(f'Module {path.stem} has no run(context)')
        modules[path.stem] = {'object': obj, 'deps': deps}
    return modules


def ordered(modules, selected):
    result, visited = [], {}

    def visit(name):
        if name not in modules:
            raise InstallerError(f'Unknown module or dependency: {name}')
        if visited.get(name) == 'active':
            raise InstallerError(f'Dependency cycle at {name}')
        if visited.get(name) == 'done':
            return
        visited[name] = 'active'
        for dep in modules[name]['deps']:
            visit(dep)
        visited[name] = 'done'
        result.append(name)

    for name in selected:
        visit(name)
    return result


def failure_reason(exc):
    """What a failed step says. An error with no words of its own (Ctrl+D at a
    question, say) still gets a reason."""
    if isinstance(exc, subprocess.CalledProcessError):
        # The command's own last words, not "returned non-zero exit status 1".
        said = (
            (exc.stderr or exc.stdout or '')
            if isinstance(exc.stderr or exc.stdout, str)
            else (exc.stderr or exc.stdout or b'').decode(errors='replace')
        ).strip()
        command = ' '.join(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else str(exc.cmd)
        return f'{command} failed' + (f': {said[-500:]}' if said else f' (exit status {exc.returncode}, no details)')
    if isinstance(exc, subprocess.TimeoutExpired):
        command = ' '.join(exc.cmd) if isinstance(exc.cmd, (list, tuple)) else str(exc.cmd)
        return f'{command} did not finish within {int(exc.timeout)} seconds'
    if str(exc):
        return str(exc)
    if isinstance(exc, EOFError):
        return 'Stopped at a question (end of input); run setup again in a terminal'
    return type(exc).__name__


def stopped_by_hand(states, name, state_path, log):
    """Stopped with Ctrl+C: recorded as such (not "running"), so the next run starts this
    step again; the setup then ends without a traceback."""
    states[name] = {'status': 'failed', 'reason': 'Stopped with Ctrl+C'}
    atomic_json_write(state_path, states)
    log(f'STOPPED {name}: Ctrl+C')


def move_log(old_log, log_path):
    """The log kept before the SSD was mounted goes on to the SSD's log: moved, not
    copied, or a later run would copy the same lines again."""
    if old_log != log_path and old_log.exists():
        with safe_path(log_path).open('a') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(old_log.read_text())
        old_log.unlink()
    os.chmod(log_path, 0o600)


def atomic_json_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, data)


# Settings every installation needs, with what they are for (shown when one is missing).
REQUIRED = {
    'data_mount': 'where the app SSD is mounted',
    'media_mount': 'where the media HDD is mounted',
    'external_backup_mount': 'where the USB backup drive is mounted',
}


def require_settings(config):
    """The settings every installation needs are there, or which one is missing and how
    to get it back."""
    if not isinstance(config, dict):
        raise InstallerError('Configuration must be an object')
    missing = next((key for key in REQUIRED if not isinstance(config.get(key), str) or not config[key]), None)
    if missing:
        raise InstallerError(
            f'The settings file has no "{missing}" ({REQUIRED[missing]}). Copy it from config.json in this folder, '
            'or delete my-config.json and run  sudo bash install.sh  to make a new one'
        )


# Backup counts whose template default changed in v0.30.0. A settings file from an earlier
# template may hold the old default without anyone having chosen it, but a settings file
# cannot tell that from a choice, so fewer backups are never kept without asking: setup
# asks once (when someone is at the keyboard), and the answer is kept.
# (group, key, earlier default, current default, what is counted)
RETIRED_DEFAULTS = (
    ('app_backup', 'keep_snapshots', 14, 7, 'nightly app backups'),
    ('host_backup', 'keep_snapshots', 14, 7, 'nightly settings backups'),
    ('photos', 'database_keep', 8, 4, 'weekly Immich database copies'),
)

# Set in the settings once the earlier defaults were reviewed: a number seen after that is
# someone's choice, also when it equals an earlier default.
RETENTION_REVIEWED = 'retention_reviewed'


def retire_old_defaults(config, ask=None):
    """Backup counts still at an earlier template default, reviewed once: with `ask` (a
    yes/no question to the person at the keyboard) each one moves to the current default
    only when they say so; without it each one stays. Returns (lines for the log, whether
    the settings changed). Never asked again once reviewed."""
    if config.get(RETENTION_REVIEWED) is True:
        return [], False
    lines = []
    for group, key, old, new, what in RETIRED_DEFAULTS:
        options = config.get(group)
        if not (isinstance(options, dict) and type(options.get(key)) is int and options[key] == old):
            continue
        if (
            ask is not None
            and ask(
                f'Your settings keep the newest {old} {what}, the default before v0.30.0 '
                f'(now {new}). Keep fewer from now on, the newest {new}?'
            )
            is True
        ):
            options[key] = new
            lines.append(f'Backups: the newest {new} {what} are kept from now on, not {old}')
        else:
            lines.append(
                f'Backups: the newest {old} {what} stay kept (the default is {new} now; '
                f'set "{key}" under "{group}" in my-config.json to change it)'
            )
    if ask is None and lines:
        return lines, False  # asked when someone is at the keyboard
    config[RETENTION_REVIEWED] = True
    return lines, True


def review_retention(ctx, apply):
    """The earlier default backup counts reviewed (retire_old_defaults), asking only
    when someone is at the keyboard; the answer saved with --apply."""
    lines, changed = retire_old_defaults(ctx.config, guided_setup.yes_no if apply and sys.stdin.isatty() else None)
    for line in lines:
        ctx.log(line)
    if changed and apply:
        ctx.persist_config()


def validate_config(config):
    require_settings(config)
    for group in ('runtime', 'modules', 'profiles', 'storage', 'media', 'photos', 'docker'):
        if not isinstance(config.get(group, {}), dict):
            raise InstallerError(group + ' must be an object')
    # Saved pre-v0.18 configurations lack these switches. Apply current defaults
    # in memory so reusing confirmed disk settings cannot revive retired jobs.
    for group, defaults in (
        ('monitoring', {'enabled': False}),
        ('music', {'backup_enabled': False}),
        ('photos', {'backup_schedule': 'weekly'}),
    ):
        options = config.setdefault(group, {})
        if not isinstance(options, dict):
            raise InstallerError(group + ' must be an object')
        for key, value in defaults.items():
            options.setdefault(key, value)
    for entry in config.get('modules', {}).values():
        if not isinstance(entry, dict) or type(entry.get('enabled', False)) is not bool:
            raise InstallerError('Module enable switches must be booleans')
    for names in config.get('profiles', {}).values():
        if (
            not isinstance(names, list)
            or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names)
        ):
            raise InstallerError('Profiles must contain unique module names')
    runtime = config.get("runtime", {})
    # "python" is accepted only so older configs still load; it now means Rust.
    if not isinstance(runtime, dict) or runtime.get("backend", "rust") not in ("python", "rust"):
        raise InstallerError("runtime.backend must be rust")
    data, media = mount_path(config['data_mount']), mount_path(config['media_mount'])
    if data == media or data in media.parents or media in data.parents:
        raise InstallerError('Data and media mounts must be separate and non-nested')
    backup = mount_path(config['external_backup_mount'])
    if any(backup == p or backup in p.parents or p in backup.parents for p in (data, media)):
        raise InstallerError('External backup mount must be separate from data and media')
    # Older settings files carry docker.persistent_root; it can only be the app SSD.
    if config.get('docker', {}).get('persistent_root', str(data)) != str(data):
        raise InstallerError(
            '"persistent_root" under "docker" must be the same as "data_mount" (or delete that line: Docker always uses the app SSD)'
        )
    for key, default in [('archive_dir', 'photos/archive'), ('inbox_dir', 'photos/inbox')]:
        relative_path(config.get('photos', {}).get(key, default))
    archive = media / relative_path(config.get('photos', {}).get('archive_dir', 'photos/archive'))
    # Reserve the former HDD live tree too: never adopt it as an archive on upgrade.
    managed = media / 'photos' / 'immich-library'
    if archive == managed or archive in managed.parents or managed in archive.parents:
        raise InstallerError('Photo archive must be separate from the Immich upload tree')
    selected = config.get('media', {}).get('services', [])
    allowed = {'jellyfin', 'navidrome', 'sonarr', 'radarr', 'lidarr', 'prowlarr', 'seerr', 'qbittorrent'}
    if (
        not isinstance(selected, list)
        or not selected
        or len(selected) != len(set(selected))
        or not set(selected) <= allowed
    ):
        raise InstallerError('media.services must contain unique known services')


def template_steps(config):
    """config_checks.merge_profiles with the template next to the installer."""
    from config_checks import merge_profiles

    return merge_profiles(config, template_settings(ROOT))


def template_settings(root=ROOT):
    """The template next to the installer (config.json), or {} when it can't be read."""
    try:
        value = read_json(Path(root) / SETTINGS_FILE)
    except (OSError, ValueError, InstallerError):
        return {}
    return value if isinstance(value, dict) else {}


def save_profiles(ctx, added, changed, apply):
    """The steps merge_profiles added, said in the log (on the screen alone for a read-only
    plan: ctx None), and the settings saved (--apply)."""
    for line in added:
        if ctx is None:
            print(line + ' (not saved: read-only)')
        else:
            ctx.log(line)
    if changed and apply:
        ctx.persist_config()


def _execute(args, root=ROOT):
    config = read_json(Path(args.config))
    validate_config(config)
    # A settings file keeps the profiles it was made with: the steps a newer release added
    # are added now (said in the log, and saved with --apply, once the log is open below).
    added, profiles_changed = template_steps(config)
    modules = load_modules(root / 'modules')
    enabled = [k for k in modules if config.get('modules', {}).get(k, {}).get('enabled', False)]
    state_dir = Path(args.state_dir).expanduser()
    if not state_dir.is_absolute() or '..' in state_dir.parts:
        raise InstallerError('--state-dir must be an absolute path without ".."')
    state_dir = safe_path(state_dir)
    if state_dir == DEFAULT_STATE and os.path.ismount(config['data_mount']):
        state_dir = Path(config['data_mount']) / 'installer' / 'state'
    state_path = safe_path(state_dir / 'state.json')
    safe_path(state_dir / 'install.log')
    previous = read_json(state_path) if state_path.exists() else {}
    if not isinstance(previous, dict) or any(not isinstance(value, dict) for value in previous.values()):
        raise InstallerError('Installer state must map module names to state objects')
    if args.retry_failed and not previous:
        raise InstallerError('No prior installer state; mount the data SSD with the foundation profile before retrying')
    if args.profile:
        if args.profile not in config.get('profiles', {}):
            raise InstallerError('Unknown profile: ' + args.profile)
        selected = config['profiles'][args.profile]
    elif args.module:
        selected = [args.module]
    elif args.retry_failed:
        selected = [
            name
            for name in modules
            if previous.get(name, {}).get('status') in ('failed', 'blocked', 'needs_setup', 'running')
        ]
    else:
        selected = enabled
    plan = ordered(modules, selected)
    from config_checks import validate

    validate(config, plan)
    if args.apply and set(plan) & {
        'docker',
        'server_os',
        'dashboard',
        'gateway_lan',
        'media_monitor',
        'photo_preservation',
        'photo_backup',
        'app_backup',
        'host_backup',
        'notifications',
        'heartbeat',
        'maintenance',
    }:
        from native_runtime import payload

        payload(ROOT, config)
    if args.check or args.dry_run:
        save_profiles(None, added, False, False)
        print(f'NekoServer v{VERSION} — read-only plan (no changes):')
        for name in plan:
            print(f"  {name}: {getattr(modules[name]['object'], 'DESCRIPTION', '')}")
        if args.check:
            print('Configuration and dependency graph valid.')
        return 0
    ensure_directory(state_dir, 0o700)
    if not args.apply:
        state_path = safe_path(state_dir / 'inspection-state.json')
    log_path = safe_path(state_dir / ('install.log' if args.apply else 'inspection.log'))
    states = dict(previous) if args.apply else {}
    complete_status = 'success' if args.apply else 'inspected'
    failures = []
    unavailable = []

    def log(message):
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        text = f'[{stamp}] {message}'
        print(text)
        with safe_path(log_path).open('a', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text + '\n')
        if not message.startswith(MARKERS):
            setup_progress.note(message)  # a step's own words, for the setup window

    ctx = Context(config, log, apply=args.apply, config_path=args.config)
    ctx.plan = plan
    save_profiles(ctx, added, profiles_changed, args.apply)
    review_retention(ctx, args.apply)
    # The steps this run goes through (with the ones they need), for the setup window and
    # the finishing screen to count the same steps as the log.
    setup_progress.event('plan', steps=list(plan))
    for name in plan:
        held = waiting_for(name, modules, states, complete_status)
        if held:
            states[name] = blocked(held)
            log(f'BLOCKED {name}: {states[name]["reason"]}')
            setup_progress.step(name, 'blocked', states[name]['reason'])
            failures.append(name)
            atomic_json_write(state_path, states)
            continue
        states[name] = {
            'status': 'running',
            'version': VERSION,
            'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        atomic_json_write(state_path, states)
        log(f'START {name}')
        setup_progress.step(name, 'running')
        try:
            result = modules[name]['object'].run(ctx)
        except KeyboardInterrupt:
            stopped_by_hand(states, name, state_path, log)
            raise
        except Exception as exc:
            reason = failure_reason(exc)
            states[name] = {'status': 'failed', 'reason': reason}
            log(f'FAILED {name}: {reason}')
            # Avoid tracebacks from providers that may contain sensitive arguments.
            failures.append(name)
        else:
            states[name] = outcome(result, complete_status)
            if states[name]['status'] == 'needs_setup':
                failures.append(name)
                log(f'NEEDS SETUP {name}: {states[name]["reason"]}')
            elif states[name]['status'] == UNAVAILABLE:
                unavailable.append(name)
                log(f'UNAVAILABLE {name}: {states[name]["reason"]}')
            else:
                log(f'PASS {name}')
        setup_progress.step(name, states[name]['status'], states[name].get('reason', ''))
        if (
            name == 'storage'
            and states[name]['status'] == 'success'
            and args.apply
            and Path(args.state_dir).expanduser() == DEFAULT_STATE
        ):
            persistent = Path(config['data_mount']) / 'installer' / 'state'
            ensure_directory(persistent, 0o700)
            next_state = persistent / 'state.json'
            previous_persistent = read_json(next_state) if next_state.exists() else {}
            states = moved_states(previous_persistent, states, plan[: plan.index(name) + 1])
            old_log, old_state = log_path, state_path
            state_dir = persistent
            state_path = next_state
            log_path = persistent / 'install.log'
            move_log(old_log, log_path)
            retire(old_state)
        atomic_json_write(state_path, states)
    for name in REFRESHED:
        if args.apply and states.get(name, {}).get('status') == complete_status and name in modules:
            # Later steps (or a newer release) can change what these keep: written again
            # from the final configuration, even when only another step ran.
            try:
                modules[name]['object'].refresh(config, log)
            except (OSError, RuntimeError, ValueError) as exc:
                log(f'{name}: settings not refreshed: {exc}')
    if args.apply:
        # Also after a run with a step still pending (Tailscale not signed in, say), or
        # earlier releases' files would pile up with every upgrade: only folders no service
        # or drop-in names any more go.
        tidy_old_releases(config, states, log)
    # Counted as the finishing screen and the setup window count: steps done, out of the
    # steps this PC can have.
    here = len(plan) - len(unavailable)
    log(
        f'Finished: {here - len(failures)} of {here} steps done; '
        + (f'{len(unavailable)} not available on this system; ' if unavailable else '')
        + f'{len(failures)} failed, waiting or unfinished; log={log_path}'
    )
    return 1 if failures else 0


def waiting_for(name, modules, states, complete_status):
    """The steps that hold this one up: for each step it needs that did not finish, the
    first one up the chain that is not itself waiting (Docker, say, for the apps the
    recovery checkpoint holds up). Empty when it can run."""
    found = []
    for dep in modules[name]['deps']:
        record = states.get(dep, {})
        if record.get('status') == complete_status:
            continue
        held = record.get('waiting_for') if record.get('status') == 'blocked' else None
        found += held if isinstance(held, list) and held else [dep]
    return list(dict.fromkeys(found))


def blocked(held):
    """A waiting step's record: what it waits for, in the steps' titles."""
    from wizard import title

    return {
        'status': 'blocked',
        'waiting_for': held,
        'reason': 'Waits for ' + ', '.join(title(n) for n in held) + ' to finish first',
    }


def outcome(result, complete_status):
    """A step's recorded state from what its run() returned: a needs_setup or unavailable
    result (an optional extra this Linux can't have yet: neither done nor waiting), else
    done."""
    if isinstance(result, dict) and result.get('status') == 'needs_setup':
        return {'status': 'needs_setup', 'reason': result.get('reason', 'Setup incomplete')}
    if isinstance(result, dict) and result.get('status') == UNAVAILABLE:
        return {'status': UNAVAILABLE, 'reason': result.get('reason', 'Not available on this system')}
    return {'status': complete_status, 'timestamp': dt.datetime.now(dt.timezone.utc).isoformat()}


def tidy_old_releases(config, states, log):
    """Once a run has finished with the drives mounted: the files of earlier releases that
    nothing uses any more go (native_runtime.retire_old)."""
    if 'storage' not in states:
        return
    # The SSD folder as the settings were checked to name it (under /srv or /mnt), and mounted.
    data = os.path.realpath(mount_path(config['data_mount']))
    if not data.startswith(('/srv/', '/mnt/')) or not os.path.ismount(data):
        return
    from native_runtime import retire_old

    try:
        retire_old(data, log)
    except (OSError, RuntimeError, KeyError) as exc:
        log(f'Earlier releases\' files not tidied: {exc}')


# What an optional step returns when this Linux can't have it (yet).
UNAVAILABLE = 'unavailable'

# Steps whose saved settings are brought up to date after every run.
REFRESHED = (
    'dashboard',
    'host_backup',
    'app_backup',
    'maintenance',
    'photo_backup',
    'media_monitor',
    'gateway_tailnet',
)


def moved_states(on_ssd, here, ran):
    """The step records once they move to the SSD: the SSD's (the newer ones, from the
    runs since the SSD was set up), with this run's own steps on top. The rest of the
    file on the system disk is from an earlier run and must not undo newer results."""
    return {**here, **on_ssd, **{name: here[name] for name in ran if name in here}}


def retire(old_state):
    """The record on the system disk, put aside once the SSD's is in use, so a later run
    that starts before the SSD is mounted can't take its old results for current ones."""
    try:
        old_state.rename(old_state.with_name(old_state.name + '.moved-to-ssd'))
    except OSError:
        pass


def execute(args, root=ROOT):
    if not args.apply:
        return _execute(args, root)
    if os.geteuid() != 0:
        raise InstallerError('--apply requires root')
    blocked = pending_maintenance()
    if blocked:
        raise InstallerError(blocked)
    found = capabilities.detect()
    capabilities.report(found)
    capabilities.require(found)
    path = safe_path('/run/nekoserver-install.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
            raise InstallerError('Installer lock must be a private root-owned regular file')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallerError('Another NekoServer installation is active') from None
        # Packages an earlier installation left half-done are tidied before any step runs
        # (under the package lock, so never another setup's installation in progress).
        host.current().reconcile()
        return _execute(args, root)
    finally:
        os.close(fd)


def settings_for(args, parser):
    """The settings file a command without --wizard/--recover uses (those two default to
    my-config.json themselves). Everything else uses it too once the wizard has made it:
    the template next to it (config.json, which git tracks) has no drives in it, and
    setup would save into it, so changes are never applied to the template."""
    found = default_settings(ROOT)
    # In the copy kept on the SSD (release-manifest.json beside it) config.json is your
    # settings, so it is used as they are; anywhere else it is only the template.
    chosen = args.config or (found if found != NO_SETTINGS else str(ROOT / SETTINGS_FILE))
    kept_copy = (ROOT / BUNDLE_MANIFEST).is_file()
    if args.apply and not kept_copy and Path(chosen).resolve() == (ROOT / SETTINGS_FILE).resolve():
        if has_drives(ROOT / SETTINGS_FILE):
            # Older docs had people edit the template itself.
            parser.error(
                'config.json holds your drives, but it is the template that git updates. Keep your settings '
                'as my-config.json instead: in this folder run  cp config.json my-config.json  and then '
                'git checkout config.json, and run the command again'
            )
        parser.error(
            'config.json is the template, not your settings. Run  sudo bash install.sh  first '
            '(it makes my-config.json with your drives and choices), or name a settings file with --config'
        )
    return chosen


def has_drives(path):
    """A settings file with an SSD or HDD chosen in it (a UUID under "storage")."""
    try:
        storage = json.loads(Path(path).read_text()).get('storage', {})
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(storage, dict) and any(isinstance(v, dict) and v.get('uuid') for v in storage.values())


def finish_update():
    """Only the unfinished monthly update, finished by this release (no other step runs)."""
    if os.geteuid() != 0:
        print('Run it with sudo:  sudo bash install.sh --finish-update', file=sys.stderr)
        return 1
    from modules import maintenance

    try:
        maintenance.finish_update()
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='NekoServer safe phase-one installer framework')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--all', action='store_true', help='Run all enabled modules (default)')
    mode.add_argument('--profile', metavar='NAME', help='Run named module profile')
    mode.add_argument('--module', metavar='NAME', help='Run a module and its dependencies')
    mode.add_argument(
        '--retry-failed', action='store_true', help='Retry failed or blocked modules and their dependencies'
    )
    mode.add_argument(
        '--wizard', action='store_true', help='Guided setup of everything (what `sudo bash install.sh` runs)'
    )
    mode.add_argument('--recover', action='store_true', help='Guided recovery from your backups')
    mode.add_argument(
        '--finish-update',
        action='store_true',
        help='Finish a monthly update an earlier release left unfinished, with this release (sudo bash install.sh --finish-update)',
    )
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Allow explicitly supported system changes (requires root for Docker install)',
    )
    parser.add_argument(
        '--check', action='store_true', help='Validate configuration and dependency graph without running'
    )
    parser.add_argument('--dry-run', action='store_true', help='Preview execution; writes nothing')
    parser.add_argument('--config', help='Settings file (default: my-config.json here, the one the wizard made)')
    parser.add_argument('--state-dir', default=str(DEFAULT_STATE))
    args = parser.parse_args(argv)
    if args.finish_update:
        return finish_update()
    if not (args.wizard or args.recover):
        args.config = settings_for(args, parser)
    if args.config:
        try:
            args.config = str(settings_path(args.config))
        except RuntimeError as exc:
            parser.error(str(exc))
    if args.apply and (args.check or args.dry_run):
        parser.error('--apply cannot be combined with --check or --dry-run')
    try:
        if args.wizard:
            import wizard

            return wizard.run(execute, Path(args.state_dir), config=args.config)
        if args.recover:
            import recovery_wizard

            return recovery_wizard.run(config_path=args.config)
        return execute(args)
    except KeyboardInterrupt:
        again = 'sudo bash install.sh --recover' if args.recover else 'sudo bash install.sh'
        print(
            f'\nStopped. Nothing that was finished is undone. Run  {again}  again '
            'when you are ready: it continues where it stopped.',
            file=sys.stderr,
        )
        return 130
    except (InstallerError, RuntimeError, ValueError, OSError, KeyError, TypeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
