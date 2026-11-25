#!/usr/bin/env python3
"""NekoServer modular, standard-library-only orchestration framework."""

import argparse
import contextlib
import copy
import fcntl
import stat
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import traceback
from pathlib import Path
import setup_progress

# Before any other of NekoServer's files is loaded: every question setup asks, in whatever
# file, then goes through setup_progress and reaches the setup window too.
setup_progress.own_prompts()

import capabilities
import guided_setup
import host
import known_fixes
from neko_common import (
    atomic_json,
    mount_path,
    relative_path,
    VERSION,
    safe_path,
    ensure_directory,
    settings_path,
    read_settings,
    pending_maintenance,
    default_settings,
    NO_SETTINGS,
    BUNDLE_MANIFEST,
    MEDIA_APPS,
    readable_output,
    stopping_on_hangup,
)

# The installer's own log lines (a step's status), as against a step's own words.
MARKERS = ('START ', 'PASS ', 'FAILED ', 'NEEDS SETUP ', 'UNAVAILABLE ', 'BLOCKED ', 'Finished:')
# What a setup run ends with (install.sh hands it on): 0 when every step is done, WAITING
# when a step only waits for you (Tailscale's sign-in, say) or for such a step, STEP_FAILED
# when a step failed. Until 0.48.1 a failed step ended with 1 as well, and the upgrade
# took it for a step that waits (upgrade.setup_problem). 2 is an error before any step
# ran and 130 Ctrl+C; install.sh's own refusals end with 1, 3 or 4 before this program
# starts, so STEP_FAILED is none of those.
WAITING, STEP_FAILED = 1, 5

ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = 'config.json'  # the example settings that come with NekoServer
DEFAULT_STATE = Path.home() / '.local' / 'state' / 'nekoserver'


class InstallerError(Exception):
    pass


class Context:
    def __init__(self, config, logger, apply=False, config_path=None, saved=None):
        self.config = config
        self.log = logger
        self.apply = apply
        self.config_path = Path(config_path) if config_path else None
        self.original_config_path = self.config_path
        # The settings as the file had them when this run read it (or last saved it): what
        # differs from these is what the run changed.
        self.saved = copy.deepcopy(config if saved is None else saved)

    def persist_config(self):
        """Save what this run changed in the settings (drives chosen, say) into the settings
        file as it is now. A change made to the file meanwhile is kept: the run never writes
        back the settings it read at the start over it (gate run C10: a fix made while the
        run went on was undone, and "Retry this step" failed again). The file keeps its
        owner, and setup runs as root."""
        for path in dict.fromkeys(p for p in (self.original_config_path, self.config_path) if p is not None):
            owner = (path.stat().st_uid, path.stat().st_gid) if path.exists() else None
            now = file_settings(path)
            atomic_json(path, self.config if now is None else merged(self.saved, self.config, now))
            if owner is not None and os.geteuid() == 0:
                os.chown(path, *owner, follow_symlinks=False)
        from neko_common import read_places, write_places

        # Once the Drives page has recorded the places, a change setup makes to them is
        # recorded too; a server that never used it gets no record (T1-17).
        if read_places() is not None:
            write_places(self.config)
        self.saved = copy.deepcopy(self.config)


def file_settings(path):
    """The settings file as it is now, or None when it is missing or can't be read."""
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def merged(before, after, now):
    """`now` (the file) with what changed from `before` to `after` (this run) put in, key
    by key inside sections: what the run did not change stays as the file has it."""
    result = copy.deepcopy(now)
    for key in [key for key in before if key not in after]:
        result.pop(key, None)
    for key, value in after.items():
        if key in before and before[key] == value:
            continue
        old, here = before.get(key), result.get(key)
        if isinstance(old, dict) and isinstance(value, dict) and isinstance(here, dict):
            result[key] = merged(old, value, here)
        else:
            result[key] = copy.deepcopy(value)
    return result


def read_json(path):
    """A settings or state file (.json): absolute, no '..', no symlinks along the way.
    Named, with what is wrong: Python's own words ("Expecting value: line 1 column 1",
    "[Errno 2] No such file or directory") were all setup said before."""
    try:
        return read_settings(path)
    except RuntimeError as exc:
        raise InstallerError(str(exc)) from None


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


# The profile the last setup run used (None: the steps switched on in the settings).
PROFILE_FILE = 'profile.json'
PROFILE_FORMAT = 'neko-setup-profile-v1'


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


# Errors of setup's own code: their text alone ("'data_ssd'" from a KeyError) tells a
# person nothing, so the reason says what happened and what to do (deep check 291).
CODE_ERRORS = (LookupError, TypeError, AttributeError, NameError, AssertionError, ArithmeticError, RecursionError)


def code_error_reason(exc, log_path=None):
    """Plain words for a slip in setup's own code, with the raw detail (and where it
    happened) kept in brackets for a bug report."""
    detail = type(exc).__name__ + (f' {exc}' if str(exc) else '')
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    if frames:
        where = Path(frames[-1].filename)
        here = Path(__file__).resolve().parent
        shown = where.relative_to(here) if where.is_relative_to(here) else where.name
        detail += f' in {shown}, line {frames[-1].lineno}'
    log = f'the setup log ({log_path})' if log_path else 'the setup log'
    return (
        f'Setup stopped on an unexpected problem in its own code ({detail}). Run setup again: '
        f'it starts this step over. If it stops here again, send {log} with a bug report.'
    )


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


# Every setup run adds to its log (each upgrade and "Run setup again" too), so it grew for
# good. Past this size it becomes <log>.1 (replacing the one before): the last runs stay.
LOG_LIMIT = 2 * 1024 * 1024


def roll_log(log_path, limit=LOG_LIMIT):
    """Start a new log when `log_path` has grown past `limit`; the old one is kept as .1."""
    try:
        if log_path.stat().st_size > limit:
            os.replace(log_path, safe_path(log_path.with_name(log_path.name + '.1')))
    except FileNotFoundError:
        pass


def move_log(old_log, log_path):
    """The log kept before the SSD was mounted goes on to the SSD's log: moved, not
    copied, or a later run would copy the same lines again."""
    roll_log(log_path)
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
    'data_mount': 'where the apps drive is mounted',
    'media_mount': 'where the media and backups drive is mounted',
    'external_backup_mount': 'where the offline backup drive is mounted',
}


def require_settings(config):
    """The settings every installation needs are there, or which one is missing and how
    to get it back."""
    if not isinstance(config, dict):
        raise InstallerError('The settings file holds no settings (it must be one JSON object, {...})')
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
# (The nightly app and settings backups, 14 before v0.30.0 and 7 until 0.50.0, are
# reviewed by review_backup_counts since 0.50.0: their count is 1 to 7 now, 3 by default.)
RETIRED_DEFAULTS = (('photos', 'database_keep', 8, 4, 'weekly Immich database copies'),)

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
        if ask is not None and ask(keep_fewer_words(old, new, what)) is True:
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


def review_backup_counts(config, ask=None, held=None):
    """0.50.0 keeps 3 nightly app backups and 3 nightly settings backups on a new server,
    and 1 to 7 at most. A server set up earlier may keep more (up to 3650), or every one
    (0). Such a count is never changed, and nothing is removed, without the person's yes:
    it is marked as the earlier setting and goes on working as it did. With `ask` (a
    yes/no question to the person at the keyboard) each one is shown once with what the
    change would do (backup_plan.review; `held(group)`: how many backups there are now)
    and moves to 3 only on a yes. Returns (lines for the log, whether the settings
    changed). Removing the older backups is the scheduled job's work, after its next
    backup that succeeds; setup removes none."""
    import backup_plan

    if config.get(backup_plan.REVIEWED) is True:
        return [], False
    marked = backup_plan.note_earlier(config)
    lines = []
    waiting = backup_plan.earlier_groups(config)
    for group in waiting:
        what = backup_plan.COUNTED[group][0]
        keep = backup_plan.count(config, group)[0]
        kept = backup_plan.kept_words(keep, what, group)
        said = backup_plan.review(config, group, backup_plan.DEFAULT, held(group) if held else None)
        if ask is not None and ask(f'{said}\nKeep the newest {backup_plan.DEFAULT} {what} from now on?') is True:
            backup_plan.set_count(config, group, backup_plan.DEFAULT)
            lines.append(
                f'Backups: the newest {backup_plan.DEFAULT} {what} are kept from now on, not {kept}. '
                + backup_plan.rule(group)
            )
        else:
            lines.append(
                f'Backups: {kept} stay kept, as before (new servers keep {backup_plan.DEFAULT}, at most '
                f'{backup_plan.MOST}); nothing was removed. The backup page of setup changes it'
            )
    if ask is None and waiting:
        return lines, marked  # asked when someone is at the keyboard
    config[backup_plan.REVIEWED] = True
    return lines, True


# Set in the settings once the release channel was reviewed (1.0.0): a "beta" seen after
# that is the owner's choice and is kept.
CHANNEL_REVIEWED = 'channel_reviewed'


def stable_channel_once(config):
    """1.0.0 makes "stable" the default channel. Before it, every settings file said
    "beta" (the template did), and a file cannot tell that from a choice, so a "beta"
    from before 1.0.0 moves to "stable" once, without a question (the dashboard's
    upgrade has nobody at the keyboard), with a line saying how to get betas back.
    Returns (lines for the log, whether the settings changed)."""
    if config.get(CHANNEL_REVIEWED) is True:
        return [], False
    options = config.get('maintenance')
    lines = []
    if isinstance(options, dict) and options.get('nekoserver_channel') == 'beta':
        options['nekoserver_channel'] = 'stable'
        lines.append(
            'Updates: this server is offered stable NekoServer releases only from now on '
            '(stable is the default since 1.0.0). To be offered betas again, set '
            '"nekoserver_channel": "beta" under "maintenance" in my-config.json and run setup again'
        )
    config[CHANNEL_REVIEWED] = True
    return lines, True


def review_retention(ctx, apply):
    """The earlier backup counts reviewed (retire_old_defaults, review_backup_counts),
    asking only when someone is at the keyboard, and the channel moved to stable once
    (stable_channel_once); the answers saved with --apply."""
    import backup_plan

    ask = guided_setup.yes_no if apply and sys.stdin.isatty() else None
    lines, changed = retire_old_defaults(ctx.config, ask)
    more, counts = review_backup_counts(ctx.config, ask, lambda group: backup_plan.held(ctx.config, group))
    moved, channel = stable_channel_once(ctx.config)
    for line in lines + more + moved:
        ctx.log(line)
    if (changed or counts or channel) and apply:
        ctx.persist_config()


def old_switches_defaulted(config):
    """Saved pre-v0.18 configurations lack these switches. Apply current defaults in memory
    so reusing confirmed disk settings cannot revive retired jobs."""
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


def check_steps_and_profiles(config):
    """Each step's switch is true or false, and each profile names steps once."""
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


def drives_apart(config):
    """The app SSD, the media drive and the USB backup drive: three folders, none inside
    another. Returns the media drive's."""
    data, media = mount_path(config['data_mount']), mount_path(config['media_mount'])
    if data == media or data in media.parents or media in data.parents:
        raise InstallerError('Data and media mounts must be separate and non-nested')
    backup = mount_path(config['external_backup_mount'])
    if any(backup == p or backup in p.parents or p in backup.parents for p in (data, media)):
        raise InstallerError('External backup mount must be separate from data and media')
    # Older settings files carry docker.persistent_root; it can only be the app SSD.
    if config.get('docker', {}).get('persistent_root', str(data)) != str(data):
        raise InstallerError(
            '"persistent_root" under "docker" must be the same as "data_mount" (or delete that line: Docker always uses the apps drive)'
        )
    return media


def validate_config(config):
    require_settings(config)
    for group in ('runtime', 'modules', 'profiles', 'storage', 'media', 'photos', 'docker'):
        if not isinstance(config.get(group, {}), dict):
            raise InstallerError(group + ' must be an object')
    old_switches_defaulted(config)
    check_steps_and_profiles(config)
    media = drives_apart(config)
    for key, default in [('archive_dir', 'photos/archive'), ('inbox_dir', 'photos/inbox')]:
        relative_path(config.get('photos', {}).get(key, default))
    archive = media / relative_path(config.get('photos', {}).get('archive_dir', 'photos/archive'))
    # Reserve the former HDD live tree too: never adopt it as an archive on upgrade.
    managed = media / 'photos' / 'immich-library'
    if archive == managed or archive in managed.parents or managed in archive.parents:
        raise InstallerError('Photo archive must be separate from the Immich upload tree')
    problem = services_problem(config.get('media', {}).get('services'))
    if problem:
        raise InstallerError(
            f'"services" under "media" in the settings file {problem}. It lists the media apps by name: '
            + ', '.join(MEDIA_APPS)
            + '. Fix that line, or copy it from config.json in this folder'
        )


def services_problem(selected):
    """What is wrong with the media apps a settings file lists (`media.services`), in
    words, or None. Named, so that a name mistyped by hand is found (plan 96: setup only
    said "must contain unique known services")."""
    if selected is None:
        return 'is missing'
    if not isinstance(selected, list) or not selected:
        return 'must be a list of at least one app, like ["jellyfin"]'
    unknown = [name for name in selected if name not in MEDIA_APPS]
    if unknown:
        return 'names an app NekoServer does not have: ' + ', '.join(str(name) for name in unknown)
    twice = sorted({name for name in selected if selected.count(name) > 1})
    if twice:
        return 'names an app twice: ' + ', '.join(twice)
    return None


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


def record_profile(path, profile, plan, previous, log):
    """The steps this server is set up with, for the upgrade to run the same ones
    (upgrade.py setup_profile): it ran "full" for everyone (the review of 0.47.2). A run
    narrower than the server (a repair with --profile foundation on a full installation)
    keeps the profile recorded before: recorded as the server's, the upgrade would set up
    the foundation alone and leave the installed apps out (the review of 0.47.9)."""
    done = {name for name, state in previous.items() if state.get('status') == 'success'}
    left_out = sorted(done - set(plan))
    if left_out:
        log(
            f'The recorded setup profile is kept: this run ({profile or "the steps switched on"}) leaves out '
            'steps already set up: ' + ', '.join(left_out)
        )
        return
    atomic_json(path, {'format': PROFILE_FORMAT, 'profile': profile})


INSTALL_LOG = 'install.log'
# The steps that run NekoServer's own program: its files are checked before they start.
RUNTIME_STEPS = {
    'docker',
    'server_os',
    'dashboard',
    'gateway_lan',
    'media_monitor',
    'photo_preservation',
    'photo_backup',
    'app_backup',
    'host_backup',
    'backup_cards',
    'notifications',
    'heartbeat',
    'maintenance',
}


def state_folders(args, config):
    """(the folder given, the folder the state is kept in, whether that is on the SSD).
    The folder given comes from the command line (or the default), never from the
    settings; with the default folder and the SSD mounted, the state is kept on the SSD."""
    given = Path(args.state_dir).expanduser()
    if not given.is_absolute() or '..' in given.parts:
        raise InstallerError('--state-dir must be an absolute path without ".."')
    given = safe_path(given)
    if given != DEFAULT_STATE:
        return given, given, False
    # The SSD's path comes from the settings file: it is looked at only by its real path.
    # A NUL byte names no folder, so no mounted SSD (realpath would raise on it).
    if '\x00' in str(config['data_mount']):
        return given, given, False
    data = os.path.realpath(config['data_mount'])
    if not data.startswith(os.sep) or not os.path.ismount(data):
        return given, given, False
    state_dir = Path(config['data_mount']) / 'installer' / 'state'
    # A mounted SSD named another way than its real path ("//srv/...", a link on the way)
    # is refused before any step runs, with the words the first state write gave before.
    if data != os.path.abspath(config['data_mount']):
        raise RuntimeError(f'Refusing symlink path: {state_dir / "state.json"}')
    # The SSD's path comes from the settings file: the folder is used only once it
    # is proved to lie on the SSD with every link followed.
    if not os.path.realpath(state_dir).startswith(os.path.realpath(config['data_mount']) + os.sep):
        raise InstallerError(f'The setup state folder must lie on the apps drive: {state_dir}')
    return given, state_dir, True


def previous_states(state_path, args):
    """Each step as the last run left it (state.json), checked."""
    previous = read_json(state_path) if state_path.exists() else {}
    if not isinstance(previous, dict) or any(not isinstance(value, dict) for value in previous.values()):
        raise InstallerError('Installer state must map module names to state objects')
    if args.retry_failed and not previous:
        raise InstallerError(
            'No prior installer state; mount the apps drive with the foundation profile before retrying'
        )
    return previous


def selected_steps(args, config, previous, modules):
    """The steps asked for: a profile, one step, a step to retry with those that waited
    for it, the failed ones again, or the ones switched on."""
    if args.profile:
        if args.profile not in config.get('profiles', {}):
            raise InstallerError('Unknown profile: ' + args.profile)
        return config['profiles'][args.profile]
    if args.module:
        return [args.module]
    if getattr(args, 'retry_step', None):
        # "Retry this step" at the end of a run: that step and the steps that waited for
        # it, and nothing that is already done.
        return retry_selection(args.retry_step, previous, modules)
    if args.retry_failed:
        return [
            name
            for name in modules
            if previous.get(name, {}).get('status') in ('failed', 'blocked', 'needs_setup', 'running')
        ]
    return [k for k in modules if config.get('modules', {}).get(k, {}).get('enabled', False)]


def read_only_plan(args, plan, modules, added):
    """--check and --dry-run: the plan said, nothing changed."""
    save_profiles(None, added, False, False)
    print(f'NekoServer v{VERSION} — read-only plan (no changes):')
    for name in plan:
        print(f"  {name}: {getattr(modules[name]['object'], 'DESCRIPTION', '')}")
    if args.check:
        print('Configuration and dependency graph valid.')
    return 0


def make_state_folder(on_the_ssd, state_dir, given, config):
    """The state folder made (only the folder given, or the one proved to be on the SSD)."""
    if not on_the_ssd:
        ensure_directory(given, 0o700)
        return
    # Its path comes from the settings: it is made as the path with every link
    # followed, once that is proved to lie on the SSD. safe_path refused links on the
    # way above, so on a real server this is the same path as state_dir.
    checked = os.path.realpath(state_dir)
    if not checked.startswith(os.path.realpath(config['data_mount']) + os.sep):
        raise InstallerError(f'The setup state folder must lie on the apps drive: {state_dir}')
    ensure_directory(Path(checked), 0o700)


class RunLog:
    """The run's log: each line on the screen and in the log file (`path`, which moves to
    the SSD once the storage step has mounted it), and a step's own words for the setup
    window."""

    def __init__(self, path):
        self.path = path

    def __call__(self, message):
        stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
        text = f'[{stamp}] {message}'
        print(text)
        with safe_path(self.path).open('a', encoding='utf-8') as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text + '\n')
        if not message.startswith(MARKERS):
            setup_progress.note(message)  # a step's own words, for the setup window


def tried_step(name, modules, ctx, states, state_path, log, apply):
    """One step run, again each time the person asks to retry it after it failed. Its
    result; when it failed, states[name] says so."""
    tried = 0
    while True:
        states[name] = {
            'status': 'running',
            'version': VERSION,
            'started_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        atomic_json_write(state_path, states)
        log(f'START {name}')
        setup_progress.step(name, 'running')
        try:
            return modules[name]['object'].run(ctx)
        except KeyboardInterrupt:
            stopped_by_hand(states, name, state_path, log)
            raise
        except Exception as exc:
            reason = code_error_reason(exc, log.path) if isinstance(exc, CODE_ERRORS) else failure_reason(exc)
            states[name] = {'status': 'failed', 'reason': reason}
            # Written now, before "Retry this step now?": Ctrl+C or a closed terminal at
            # that question would leave the step "running" on file, its reason lost.
            atomic_json_write(state_path, states)
            log(f'FAILED {name}: {reason}')
            # Avoid tracebacks from providers that may contain sensitive arguments.
            # With a person at the terminal: the reason, the fix when it is a known
            # one, and "Retry this step now?", up to MOST_RETRIES times.
            if apply and retry_now(name, reason, tried, log):
                tried += 1
                continue
            return None


class Tally:
    """The steps not done (failed, waiting for you, or held up by one of those), those
    among them that failed, and those this PC cannot have."""

    def __init__(self):
        self.failures = []
        self.failed = []
        self.unavailable = []


def record_outcome(name, result, states, complete_status, tally, log):
    """What a step came to, in its state and in the log."""
    if states[name]['status'] == 'failed':
        tally.failures.append(name)
        tally.failed.append(name)
        return
    states[name] = outcome(result, complete_status)
    if states[name]['status'] == 'needs_setup':
        tally.failures.append(name)
        log(f'NEEDS SETUP {name}: {states[name]["reason"]}')
    elif states[name]['status'] == UNAVAILABLE:
        tally.unavailable.append(name)
        log(f'UNAVAILABLE {name}: {states[name]["reason"]}')
    else:
        log(f'PASS {name}')


def state_to_the_ssd(config, states, plan, name, log, state_path):
    """Once the storage step has mounted the SSD: the state and the log move onto it.
    Returns (the states, the state file) from then on."""
    persistent = Path(config['data_mount']) / 'installer' / 'state'
    # Made as the path with every link followed, once that is proved to lie on
    # the SSD (ensure_directory refuses links on the way, so it is the same path).
    checked = os.path.realpath(persistent)
    if not checked.startswith(os.path.realpath(config['data_mount']) + os.sep):
        raise InstallerError(f'The setup state folder must lie on the apps drive: {persistent}')
    ensure_directory(Path(checked), 0o700)
    next_state = persistent / 'state.json'
    previous_persistent = read_json(next_state) if next_state.exists() else {}
    states = moved_states(previous_persistent, states, plan[: plan.index(name) + 1])
    old_log = log.path
    log.path = persistent / INSTALL_LOG
    move_log(old_log, log.path)
    retire(state_path)
    return states, next_state


def refresh_settings(args, states, complete_status, modules, config, log):
    """The steps whose settings later steps (or a newer release) can change: written again
    from the final configuration, even when only another step ran."""
    for name in REFRESHED:
        if args.apply and states.get(name, {}).get('status') == complete_status and name in modules:
            try:
                modules[name]['object'].refresh(config, log)
            except (OSError, RuntimeError, ValueError) as exc:
                log(f'{name}: settings not refreshed: {exc}')


def finished(plan, tally, log):
    """The last line, counted as the finishing screen and the setup window count (steps
    done, out of the steps this PC can have), and the exit code."""
    here = len(plan) - len(tally.unavailable)
    log(
        f'Finished: {here - len(tally.failures)} of {here} steps done; '
        + (f'{len(tally.unavailable)} not available on this system; ' if tally.unavailable else '')
        + f'{len(tally.failures)} failed, waiting or unfinished; log={log.path}'
    )
    if tally.failed:
        return STEP_FAILED
    return WAITING if tally.failures else 0


def checked_plan(args, config, modules, previous):
    """The steps of this run, in order, checked with the settings; and the lines about
    settings that look mistyped."""
    selected = selected_steps(args, config, previous, modules)
    plan = ordered(modules, selected)
    if getattr(args, 'retry_step', None):
        plan = [name for name in plan if name in selected]
    from config_checks import validate

    validate(config, plan)
    from config_checks import unknown_settings

    mistyped = unknown_settings(config, template_settings(ROOT), Path(args.config).name)
    return plan, mistyped


def opened_log(args, state_dir, plan, previous, mistyped):
    """The run's log, opened, with the profile run (a full run with --apply) and the
    mistyped settings written in it."""
    log = RunLog(safe_path(state_dir / (INSTALL_LOG if args.apply else 'inspection.log')))
    roll_log(log.path)
    if args.apply and not (args.module or args.retry_failed or getattr(args, 'retry_step', None)):
        record_profile(safe_path(state_dir / PROFILE_FILE), args.profile, plan, previous, log)
    for line in mistyped:
        log(line)
    return log


def run_context(args, config, log, as_read, plan, previous):
    """What each step is given: the settings, the log, and what the run is."""
    ctx = Context(config, log, apply=args.apply, config_path=args.config, saved=as_read)
    ctx.plan = plan  # the steps of this run (the drives step asks the backup times only when one writes a timer)
    ctx.only = args.module  # the one step asked for by name (install.sh --module), or None
    ctx.previous = dict(previous)  # each step as the last run left it
    return ctx


def moves_to_the_ssd(args, name, states):
    """Whether the state and the log move onto the SSD now: the storage step has just
    mounted it, in a run with --apply that keeps its state in the usual place."""
    return (
        name == 'storage'
        and states[name]['status'] == 'success'
        and args.apply
        and Path(args.state_dir).expanduser() == DEFAULT_STATE
    )


class Run:
    """One run's steps as they go: the states, the state file, and the tally."""

    def __init__(self, args, config, modules, ctx, states, state_path):
        self.args = args
        self.config = config
        self.modules = modules
        self.ctx = ctx
        self.states = states
        self.state_path = state_path
        self.complete_status = 'success' if args.apply else 'inspected'
        self.tally = Tally()

    def step(self, name, plan, log):
        """One step: held up by a step it needs, or run and recorded."""
        held = waiting_for(name, self.modules, self.states, self.complete_status)
        if held:
            self.states[name] = blocked(held)
            log(f'BLOCKED {name}: {self.states[name]["reason"]}')
            setup_progress.step(name, 'blocked', self.states[name]['reason'])
            self.tally.failures.append(name)
            atomic_json_write(self.state_path, self.states)
            return
        result = tried_step(name, self.modules, self.ctx, self.states, self.state_path, log, self.args.apply)
        record_outcome(name, result, self.states, self.complete_status, self.tally, log)
        setup_progress.step(name, self.states[name]['status'], self.states[name].get('reason', ''))
        if moves_to_the_ssd(self.args, name, self.states):
            self.states, self.state_path = state_to_the_ssd(self.config, self.states, plan, name, log, self.state_path)
        atomic_json_write(self.state_path, self.states)


def after_the_steps(args, config, states, log):
    """With --apply: the failed steps noted for the dashboard, and old releases tidied."""
    if args.apply:
        note_unfinished(config, states, log)
        # Also after a run with a step still pending (Tailscale not signed in, say), or
        # earlier releases' files would pile up with every upgrade: only folders no service
        # or drop-in names any more go.
        tidy_old_releases(config, states, log)
        settle_upgrade(config, log)


def settings_for_run(path, apply, out=print):
    """The settings file, with the places the Drives page recorded when the file is older
    than them (T1-17): said in one line, and saved into the file in a run with --apply."""
    from neko_common import take_places

    config = read_json(Path(path))
    take_places(config, [path] if apply else (), out=out)
    return config


def _execute(args, root=ROOT):
    config = settings_for_run(args.config, args.apply)
    as_read = copy.deepcopy(config)  # what the run changes is saved on the file as it is then
    validate_config(config)
    # A settings file keeps the profiles it was made with: the steps a newer release added
    # are added now (said in the log, and saved with --apply, once the log is open below).
    added, profiles_changed = template_steps(config)
    modules = load_modules(root / 'modules')
    given, state_dir, on_the_ssd = state_folders(args, config)
    state_path = safe_path(state_dir / 'state.json')
    safe_path(state_dir / INSTALL_LOG)
    previous = previous_states(state_path, args)
    plan, mistyped = checked_plan(args, config, modules, previous)
    if args.check or args.dry_run:
        for line in mistyped:
            print(line)
    if args.apply and set(plan) & RUNTIME_STEPS:
        from native_runtime import payload

        payload(ROOT, config)
    if args.check or args.dry_run:
        return read_only_plan(args, plan, modules, added)
    make_state_folder(on_the_ssd, state_dir, given, config)
    if not args.apply:
        state_path = safe_path(state_dir / 'inspection-state.json')
    log = opened_log(args, state_dir, plan, previous, mistyped)
    ctx = run_context(args, config, log, as_read, plan, previous)
    save_profiles(ctx, added, profiles_changed, args.apply)
    review_retention(ctx, args.apply)
    # The steps this run goes through (with the ones they need), for the setup window and
    # the finishing screen to count the same steps as the log.
    if not getattr(args, 'retry_step', None):
        setup_progress.event('plan', steps=list(plan))
    run = Run(args, config, modules, ctx, dict(previous) if args.apply else {}, state_path)
    for name in plan:
        run.step(name, plan, log)
    refresh_settings(args, run.states, run.complete_status, modules, config, log)
    after_the_steps(args, config, run.states, log)
    return finished(plan, run.tally, log)


def note_unfinished(config, states, log):
    """The steps that failed, written on the SSD for the dashboard (reports/setup-
    unfinished.json): its Needs attention then says each one with its reason, the fix
    setup knows (known_fixes.py) and the command to run. The dashboard never runs setup
    itself. Written after every run, so a step finished since is no longer said; left
    out while the SSD is not mounted (nowhere of NekoServer's to write)."""
    from wizard import title

    data = os.path.realpath(mount_path(config['data_mount']))
    if not os.path.ismount(data):
        return
    # The SSD's path comes from the settings file: the report is written only once its
    # folder is proved to lie on the SSD with every link followed.
    reports = os.path.realpath(os.path.join(data, 'reports'))
    if not reports.startswith(data + os.sep):
        raise InstallerError(f'The reports folder must lie on the apps drive: {reports}')
    steps = [
        {
            'id': name,
            'title': title(name),
            'reason': str(record.get('reason', ''))[:1000],
            'fix': known_fixes.fix_for(record.get('reason')),
        }
        for name, record in states.items()
        if record.get('status') == 'failed'
    ]
    try:
        ensure_directory(Path(reports), 0o700)
        atomic_json_write(
            Path(reports) / 'setup-unfinished.json',
            {'format': 'neko-setup-unfinished-v1', 'version': VERSION, 'steps': steps},
        )
    except (OSError, RuntimeError) as exc:
        log(f'The dashboard was not told which steps failed ({exc}); the terminal and the log have them')


# How often one step is tried again in one run before setup says to come back later.


def keep_fewer_words(old, new, what):
    """ "Keep fewer old backups from now on?" in the words of questions.py: the question,
    then the line that says what is kept now and what a new server keeps."""
    import questions

    asked = questions.words('keep_fewer', old=old, new=new, what=what)
    return asked['title'] + ' ' + asked['hint']


MOST_RETRIES = 3


def retry_now(name, reason, tried, log, interactive=None, asked=None):
    """A step just failed: with a person at the terminal, the fix setup knows for that
    reason (known_fixes.py) and "Retry this step now?", in the setup window as a
    button. True when the step is to run again. After MOST_RETRIES tries it is no
    longer asked: the step stays failed, and setup says to run it again later."""
    if not (sys.stdin.isatty() if interactive is None else interactive):
        return False
    if tried >= MOST_RETRIES:
        log(
            f'{name}: tried again {MOST_RETRIES} times and it still does not finish. Setup goes on with the '
            'steps that do not need it; run  sudo bash install.sh  again later for this one.'
        )
        return False
    from wizard import title

    row = {'answer': 'yes', 'title': title(name), 'reason': reason, 'fix': known_fixes.fix_for(reason)}
    return (asked or guided_setup.retry_question)([row], leave='no') == 'yes'


def retry_selection(name, states, modules):
    """What "Retry this step" runs at the end of a run: the step, then the steps that
    waited for it (a waiting step names the steps it waits for). Steps already done
    are not among them."""
    if name not in modules:
        raise InstallerError(f'Unknown step: {name}')
    waited = [
        other
        for other, record in states.items()
        if other in modules and record.get('status') == 'blocked' and name in (record.get('waiting_for') or [])
    ]
    return [name, *waited]


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


def settle_upgrade(config, log):
    """The record of an upgrade that did not finish, closed when this run (started by a
    person, not by an upgrade) left the server whole (upgrade.settle_after_setup)."""
    import upgrade

    try:
        upgrade.settle_after_setup(config, log)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        log(f'The unfinished upgrade\'s record was not looked at: {exc}')


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
    readable_output()
    # Named as people run it (install.sh hands its options on), not as this file.
    parser = argparse.ArgumentParser(
        prog='sudo bash install.sh',
        description='Set up NekoServer on this PC. With no options it runs the guided setup (--wizard).',
        epilog='The other options are for repairs and checks; README.md, "Other ways to run setup", lists each one.',
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        '--all', action='store_true', help='Run every step switched on in the settings, without questions'
    )
    mode.add_argument('--profile', metavar='NAME', help='Run a named set of steps (a profile, such as remote_access)')
    mode.add_argument(
        '--module', metavar='NAME', help='Run one step (a module, such as dashboard) and the steps it needs'
    )
    mode.add_argument(
        '--retry-failed',
        action='store_true',
        help='Run again only the steps that failed or waited last time, and the steps they need',
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
        help='Let this run change the system (needs sudo); without it a run only shows what it would do',
    )
    parser.add_argument(
        '--check', action='store_true', help='Only look: check that the settings file is valid and list the steps'
    )
    parser.add_argument('--dry-run', action='store_true', help='Only look: show what a run would do; changes nothing')
    parser.add_argument('--config', help='Settings file (default: my-config.json here, the one the wizard made)')
    parser.add_argument(
        '--state-dir', default=str(DEFAULT_STATE), help='Where setup records which steps are done (rarely needed)'
    )
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
    # A closed terminal or a dropped SSH connection ended setup on the spot, with no
    # cleanup: the USB drive mounted to measure it stayed mounted read-only (the weekly
    # copy could not use it), and the step stayed "running". It stops as Ctrl+C does now.
    try:
        with stopping_on_hangup():
            if args.wizard:
                import wizard

                return wizard.run(execute, Path(args.state_dir), config=args.config)
            if args.recover:
                import recovery_wizard

                return recovery_wizard.run(config_path=args.config)
            return execute(args)
    except KeyboardInterrupt:
        again = 'sudo bash install.sh --recover' if args.recover else 'sudo bash install.sh'
        with contextlib.suppress(OSError):  # the terminal may be gone
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
