"""One-command guided setup: `sudo bash install.sh`.

Creates the settings file on first run, explains what will happen, runs the full
installation (with every guided pause, including Tailscale and the phone), then
shows a finishing screen. Running it again continues where it stopped.
"""
import argparse
import builtins
import json
import os
from pathlib import Path
import pwd
import re
import select
import sys

import guided_setup
import setup_gui
import setup_progress
from neko_common import VERSION, atomic_json, dashboard_port, mount_path, safe_path, settings_path

ROOT = Path(__file__).resolve().parent
SETTINGS = 'my-config.json'
TEMPLATE = 'config.json'   # the settings template; also the name of the copy on the SSD
RELEASE = re.compile(r'v(\d{1,9})\.(\d{1,9})\.(\d{1,9})')
CREDENTIALS = 'secrets/accounts/APP-CREDENTIALS.txt'

STEPS = {
    'system': 'System check', 'storage': 'Drives', 'capacity': 'Drive space',
    'recovery': 'Recovery copy of NekoServer', 'account_credentials': 'App passwords',
    'server_os': 'No-sleep and log limits', 'docker': 'Docker',
    'recovery_checkpoint': 'Recovery checkpoint', 'media': 'Movie, TV and music apps',
    'photos': 'Immich (photos)', 'photo_preservation': 'Weekly photo backup to the HDD',
    'photo_backup': 'Weekly backup to the USB drive', 'nextcloud': 'Nextcloud (files)',
    'accounts': 'App accounts', 'hardware_video': 'Video using the graphics chip', 'onboarding': 'Connecting the apps together',
    'music': 'Music', 'gateway_lan': 'Access at home', 'tailscale': 'Tailscale',
    'gateway_tailnet': 'Access from anywhere and phone setup', 'samba': 'Shared folders',
    'media_monitor': 'App health checks', 'dashboard': 'Dashboard', 'tv': 'TV setup',
    'desktop': 'Desktop shortcuts',
    'app_backup': 'Nightly app backup', 'notifications': 'Phone alerts',
    'heartbeat': 'Server heartbeat', 'host_backup': 'Nightly settings backup',
    'maintenance': 'Monthly updates',
}

WELCOME = [
    f'Welcome to NekoServer {VERSION}.',
    '',
    'The system is up to date and the packages the apps need are installed.',
    'Next, this sets up your home media server. It will:',
    '  1. Connect this server to Tailscale, so you can use your apps from anywhere, privately.',
    '  2. Ask you to pick your SSD, HDD and USB drive, and check they have enough space.',
    '  3. Install the apps and create an account for each one.',
    '  4. Connect the apps to each other.',
    '  5. Give each app a private Tailscale address and walk you through it on your phone.',
    '  6. Schedule backups and monthly updates, then run a first checkup and backup.',
    '',
    'Have ready: your three data drives plugged in (already formatted), your phone,',
    'a 6-digit PIN for the dashboard (you choose it during setup), and about an hour.',
    'It pauses whenever it needs you and explains what to do.',
    'Nothing is formatted or deleted. You can stop with Ctrl+C and run it again later;',
    'it continues where it stopped.',
]


def sudo_account():
    name = os.environ.get('SUDO_USER', '')
    if not name or name == 'root':
        return None
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def prepare_settings(root=ROOT, account=None):
    """Use my-config.json, creating it from the template the first time."""
    path = Path(root) / SETTINGS
    if path.exists():
        return path, False
    settings = json.loads((Path(root) / TEMPLATE).read_text())
    if account is not None:
        settings.setdefault('media', {}).update(uid=account.pw_uid, gid=account.pw_gid)
    atomic_json(path, settings)
    if account is not None:
        # Readable and editable by the person who ran sudo, nobody else.
        os.chown(path, account.pw_uid, account.pw_gid)
    os.chmod(path, 0o600)
    return path, True


def release_version(name, prefix=''):
    """(major, minor, patch) of a folder named prefix + 'v1.2.3', else None."""
    match = RELEASE.fullmatch(name[len(prefix):]) if name.startswith(prefix) else None
    return tuple(int(n) for n in match.groups()) if match else None


def earlier_candidates(root=ROOT):
    """The settings files of the server already set up on this PC, for a release folder
    without settings yet: the person's own files first, newest release first, then
    setup's copies on the SSD, newest first. Before 0.38.0 each release had a folder
    named for its version, so `git pull` left my-config.json (git does not track it) in
    the earlier one; setup also keeps a copy on the SSD with the release it ran."""
    root = Path(root)
    own, kept = [], []
    for folder in root.parent.iterdir() if root.parent.is_dir() else ():
        version = release_version(folder.name, 'nekoserver-')
        if version and folder != root and (folder / SETTINGS).is_file():
            own.append((version, folder / SETTINGS))
    releases = data_folder(read_json(root / TEMPLATE)) / 'installer/releases'
    for folder in releases.iterdir() if releases.is_dir() else ():
        version = release_version(folder.name)
        if version and (folder / TEMPLATE).is_file():
            kept.append((version, folder / TEMPLATE))
    return [path for _, path in sorted(own, reverse=True)] + [path for _, path in sorted(kept, reverse=True)]


def earlier_settings(root=ROOT, output=None):
    """The newest of earlier_candidates that can be read, or None. A newer one that cannot
    be read is named through output rather than passed over silently."""
    for path in earlier_candidates(root):
        if read_json(path):
            return path
        if output is not None:
            output('The settings at ' + str(path) + ' cannot be read (not a settings file, or damaged), so they are not used.')
    return None


def take_earlier_settings(root=ROOT, account=None, prompt=input, output=print):
    """Offer the settings earlier_settings finds; True when they were copied in as
    my-config.json. Without them an update would set the server up from the start."""
    source = earlier_settings(root, output)
    if source is None:
        return False
    output('This PC already has a NekoServer server set up. Its settings are in ' + str(source) + '.')
    if not ask('Use them for this release (yes when updating)?', prompt, output):
        output('A new settings file is made instead: setup asks for the drives and passwords again.')
        return False
    path = Path(root) / SETTINGS
    atomic_json(path, read_json(source))
    if account is not None:
        os.chown(path, account.pw_uid, account.pw_gid)
    output('Copied them to ' + str(path) + '.')
    return True


def ask(question, prompt=input, output=print):
    """yes -> True; no, Ctrl+D or Ctrl+C -> False."""
    return bool(guided_setup.yes_no(question, prompt, output))


def setup_window(path, output):
    """The setup window beside the terminal (see setup_gui), or None when it cannot be
    served; when it cannot be opened on a desktop, its address is printed instead."""
    config = read_json(path)
    if not config:
        return None
    try:
        window = setup_gui.Window(path, config, STEPS, config.get('profiles', {}).get('full', []))
    except (OSError, RuntimeError, ValueError):
        return None
    if setup_gui.open_window(config, window.url):
        output('\nA setup window opened in your browser: it explains each step as it happens. Put it where you like.'
               '\nClosed it by mistake? Open  ' + window.url + '  again in a browser on this PC.')
    else:
        output('\nTo follow along in a window, open  ' + window.url + '  in a browser on this PC.')
    return window


def _typed(stdin, output):
    """One line typed at "Ready to start?": True, False, or None when it was neither yes
    nor no (the question is put again)."""
    line = stdin.readline()
    if not line:            # Ctrl+D
        output('')
        return False
    answer = line.strip().lower()
    if answer in ('yes', 'y'):
        return True
    if answer in ('no', 'n'):
        return False
    sys.stdout.write('Please type yes or no.\nReady to start? [yes/no]: ')
    sys.stdout.flush()
    return None


def wait_to_start(prompt, output, window, stdin=None):
    """"Ready to start?": yes in the terminal, or Start pressed in the setup window."""
    if window is None or prompt is not builtins.input:
        return ask('\nReady to start?', prompt, output)
    stdin = sys.stdin if stdin is None else stdin
    sys.stdout.write('\nReady to start? [yes/no] (or press Start in the setup window): ')
    sys.stdout.flush()
    while not window.started.is_set():
        try:
            readable, _, _ = select.select([stdin], [], [], 0.5)
        except KeyboardInterrupt:
            output('')
            return False
        except (OSError, ValueError):
            return ask('\nReady to start?', prompt, output)
        if readable:
            answer = _typed(stdin, output)
            if answer is not None:
                return answer
    output('yes (Start pressed in the setup window)')
    return True


def checked_path(value):
    """An absolute path with no '..' and no symlinks along it, or ValueError."""
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or '\x00' in str(path):
        raise ValueError('Expected an absolute path without ".."')
    return safe_path(path)


def read_json(path):
    """A JSON object from a validated path, or {} if missing or unreadable."""
    try:
        value = json.loads(checked_path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, RuntimeError):
        return {}


def data_folder(config):
    """The configured data SSD mount, validated like the installer does."""
    try:
        return mount_path(config.get('data_mount', '/srv/nekoserver'))
    except (RuntimeError, ValueError, TypeError):
        return Path('/srv/nekoserver')


def state_file(config, default_state):
    persistent = data_folder(config) / 'installer/state/state.json'
    return persistent if persistent.exists() else checked_path(default_state) / 'state.json'


def show_steps(title, names, states, fallback, output):
    if names:
        output('\n' + title)
        for name in names:
            output(f'  - {STEPS[name]}: {states[name].get("reason", fallback)}')


# Where the dashboard's PIN is kept (modules/dashboard.py CONTROL_CONFIG).
PIN_FILE = Path('/etc/nekoserver-control/pin.json')


def show_dashboard(config, output, pin_file=PIN_FILE):
    port = dashboard_port(config)
    if port:
        output(f'Dashboard:          http://127.0.0.1:{port}  (on this PC)')
        try:
            pin = 'set' if pin_file.exists() else None
        except OSError:
            # The PIN's folder is root-only; without root the answer is unknown, and
            # the finishing screen is still shown.
            pin = 'unknown (only root can see it: sudo bash install.sh)'
        output('Dashboard PIN:      ' + (pin or
               'not set: phones can only look, and apps can\'t be restarted from it. '
               'Run  sudo bash install.sh  again to choose one.'))


def show_addresses(data, output):
    lan = read_json(data / 'reports/gateway-lan.json').get('portal')
    links = read_json(data / 'reports/gateway-tailnet.json').get('links') or {}
    if not (lan or links):
        return
    output('\nYour apps:')
    if lan:
        output(f'  At home (app menu):  {lan}')
    if links:
        output('  Anywhere, with Tailscale on:')
        for app, url in links.items():
            output(f'    {app.capitalize():<10} {url}')


NEXT_DONE = ['  Everything is set up. Backups and monthly updates run on their own;',
             '  the dashboard shows how they are doing.']
NEXT_PENDING = ['  Fix the items above, then run  sudo bash install.sh  again.',
                '  It continues where it stopped and keeps everything already done.']


def finished(plan, by_status, checkup):
    """Nothing left to do: every step done (or not available here) and the first checkup
    and backup (first_checkup.run; None when it didn't run) found nothing."""
    return bool(plan) and not by_status['waiting'] and not by_status['broken'] and checkup is not False


def show_checkup(checkup, output):
    if checkup is False:
        output('\nFirst checkup and backup: something needs a look (listed above, under "First checkup and backup").')


def finishing_screen(config, states, page=None, output=print, checkup=None):
    """The summary shown at the end. Returns True when nothing is left to do."""
    data = data_folder(config)
    # Judge against the steps the installation is meant to run, not only the ones that
    # left a state behind: missing or unreadable state must never read as "ready".
    expected = [n for n in config.get('profiles', {}).get('full', []) if n in STEPS]
    plan = expected or [name for name in STEPS if name in states]
    states = {n: states.get(n) if isinstance(states.get(n), dict) else
              {'status': 'missing', 'reason': 'did not run (no record of this step)'} for n in plan}
    by_status = {status: [n for n in plan if states[n].get('status') in group]
                 for status, group in (('done', ('success',)), ('waiting', ('needs_setup',)),
                                       ('unavailable', ('unavailable',)),
                                       ('broken', ('failed', 'blocked', 'running', 'missing')))}
    sorted_out = {n for names in by_status.values() for n in names}
    unknown = [n for n in plan if n not in sorted_out]
    by_status['broken'] += unknown
    complete = finished(plan, by_status, checkup)
    line = '=' * 64
    output('\n' + line)
    output('  NekoServer is ready!' if complete else '  NekoServer is almost ready')
    output(line)
    output(f'\nDone: {len(by_status["done"])} of {len(plan) - len(by_status["unavailable"])} steps.')
    show_steps('Not available on this system:', by_status['unavailable'], states, 'not available', output)
    show_steps('Still needs you:', by_status['waiting'], states, 'unfinished', output)
    show_steps('Did not finish:', by_status['broken'], states, 'stopped before finishing', output)
    show_checkup(checkup, output)
    show_addresses(data, output)
    output('\nPasswords:          sudo cat ' + str(data / CREDENTIALS))
    if page and Path(page).exists():
        output('Phone setup page:   ' + str(page) + '  (open it in a browser on this PC)')
    show_dashboard(config, output)
    if 'onboarding' in plan and states['onboarding'].get('status') == 'success' \
            and not (data / 'reports/onboarding/playback.json').exists():
        output('\nPlayback verification pending: the apps are set up and their automatic checks')
        output('passed, but no real playback was tested yet. Play something once to be sure.')
    output('\nWhat next:')
    for text in NEXT_DONE if complete else NEXT_PENDING:
        output(text)
    output('  If something goes wrong later:  sudo bash install.sh --recover')
    output(line + '\n')
    return complete


def settings_for_run(config, root, prompt, output):
    """(path, created) of the settings this run uses, or None when a file named with
    --config is missing."""
    if config:
        path = settings_path(config)
        if not path.is_file():
            output('Settings file not found: ' + str(path))
            return None
        return path, False
    if not (Path(root) / SETTINGS).exists():
        take_earlier_settings(root, sudo_account(), prompt, output)
    return prepare_settings(root, sudo_account())


def after_setup(code, path, default_state, output, window):
    """The first checkup when everything finished, then the finishing screen (in the
    window too). The exit code: 0 only when every step is done."""
    config = read_json(path)
    states = read_json(state_file(config, default_state))
    checkup = None
    if code == 0 and config:
        # Every step finished: back up and check now rather than waiting for the night.
        import first_checkup
        checkup = first_checkup.run(config, output)
    try:
        import phone_guide
        page = phone_guide.PAGE
    except ImportError:
        page = None
    lines = []

    def shown(line=''):
        lines.append(str(line))
        output(line)
    complete = finishing_screen(config, states, page, shown, checkup)
    if window is not None:
        # The window shows the same summary; give it a moment to fetch it.
        setup_progress.event('finished', lines=lines, complete=complete)
        window.delivered.wait(3)
    return 0 if complete and code == 0 else 1


def run(execute, default_state, prompt=input, output=print, root=ROOT, config=None):
    """`execute` is installer.execute; injected so this module stays testable.
    `config`: a settings file chosen with --config; otherwise my-config.json here."""
    if os.geteuid() != 0:
        output('Run it with sudo:  sudo bash install.sh')
        return 2
    settings = settings_for_run(config, root, prompt, output)
    if settings is None:
        return 2
    path, created = settings
    for line in WELCOME:
        output(line)
    output('\nYour settings are in ' + str(path) +
           (' (just created for you; you do not need to edit it).' if created else '.'))
    setup_progress.enable()
    window = setup_window(path, output) if prompt is builtins.input else None
    try:
        if wait_to_start(prompt, output, window):
            if window is not None:
                window.freeze()
            default_state = checked_path(default_state)
            args = argparse.Namespace(all=False, profile='full', module=None, retry_failed=False,
                                      apply=True, check=False, dry_run=False, config=str(path),
                                      state_dir=str(default_state))
            with setup_progress.watching_prompts():
                code = execute(args)
            exit_code = after_setup(code, path, default_state, output, window)
        else:
            output('No apps were set up. (The system update and the packages NekoServer needs are '
                   'already installed; that is all.) Run  sudo bash install.sh  when you are ready.')
            exit_code = 0
    finally:
        if window is not None:
            window.stop()
        setup_progress.disable()
    return exit_code

