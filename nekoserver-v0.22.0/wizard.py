"""One-command guided setup: `sudo bash install.sh`.

Creates the settings file on first run, explains what will happen, runs the full
installation (with every guided pause, including Tailscale and the phone), then
shows a finishing screen. Running it again continues where it stopped.
"""
import argparse
import json
import os
from pathlib import Path
import pwd

from neko_common import VERSION, atomic_json

ROOT = Path(__file__).resolve().parent
SETTINGS = 'my-config.json'
CREDENTIALS = 'secrets/accounts/APP-CREDENTIALS.txt'
DASHBOARD = 'http://127.0.0.1:8787'

STEPS = {
    'system': 'System check', 'storage': 'Drives', 'capacity': 'Drive space',
    'recovery': 'Recovery copy of NekoServer', 'account_credentials': 'App passwords',
    'server_os': 'No-sleep and log limits', 'docker': 'Docker',
    'recovery_checkpoint': 'Recovery checkpoint', 'media': 'Movie, TV and music apps',
    'photos': 'Immich (photos)', 'photo_preservation': 'Weekly photo backup to the HDD',
    'photo_backup': 'Weekly backup to the USB drive', 'nextcloud': 'Nextcloud (files)',
    'accounts': 'App accounts', 'onboarding': 'Connecting the apps together',
    'music': 'Music', 'gateway_lan': 'Access at home', 'tailscale': 'Tailscale',
    'gateway_tailnet': 'Access from anywhere and phone setup', 'samba': 'Shared folders',
    'media_monitor': 'App health checks', 'dashboard': 'Dashboard', 'tv': 'TV setup',
    'app_backup': 'Nightly app backup', 'notifications': 'Phone alerts',
    'heartbeat': 'Server heartbeat', 'host_backup': 'Nightly settings backup',
    'maintenance': 'Monthly updates',
}

WELCOME = [
    f'Welcome to NekoServer {VERSION}.',
    '',
    'This sets up your home media server. It will:',
    '  1. Ask you to pick your SSD, HDD and USB drive, and check they have enough space.',
    '  2. Install the apps and create an account for each one.',
    '  3. Connect the apps to each other.',
    '  4. Set up Tailscale so you can use your apps from anywhere, privately.',
    '  5. Walk you through setting up each app on your phone.',
    '  6. Schedule backups and monthly updates.',
    '',
    'Have ready: your three data drives plugged in (already formatted), your phone,',
    'and about an hour. It pauses whenever it needs you and explains what to do.',
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
    settings = json.loads((Path(root) / 'config.json').read_text())
    if account is not None:
        settings.setdefault('media', {}).update(uid=account.pw_uid, gid=account.pw_gid)
    atomic_json(path, settings)
    if account is not None:
        # Readable and editable by the person who ran sudo, nobody else.
        os.chown(path, account.pw_uid, account.pw_gid)
    os.chmod(path, 0o600)
    return path, True


def ask(question, prompt=input, output=print):
    while True:
        try:
            answer = prompt(question + ' [yes/no]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            output('')
            return False
        if answer in ('yes', 'y'):
            return True
        if answer in ('no', 'n'):
            return False
        output('Please type yes or no.')


def read_json(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def state_file(config, default_state):
    persistent = Path(config.get('data_mount', '/srv/nekoserver')) / 'installer/state/state.json'
    return persistent if persistent.exists() else Path(default_state) / 'state.json'


def finishing_screen(config, states, page=None, output=print):
    """The summary shown at the end. Returns True when nothing is left to do."""
    data = Path(config.get('data_mount', '/srv/nekoserver'))
    plan = [name for name in STEPS if name in states]
    done = [n for n in plan if states[n].get('status') == 'success']
    waiting = [n for n in plan if states[n].get('status') == 'needs_setup']
    broken = [n for n in plan if states[n].get('status') in ('failed', 'blocked', 'running')]
    complete = not waiting and not broken
    line = '=' * 64
    output('\n' + line)
    output('  NekoServer is ready!' if complete else '  NekoServer is almost ready')
    output(line)
    output(f'\nDone: {len(done)} of {len(plan)} steps.')
    if waiting:
        output('\nStill needs you:')
        for name in waiting:
            output(f'  - {STEPS[name]}: {states[name].get("reason", "unfinished")}')
    if broken:
        output('\nDid not finish:')
        for name in broken:
            reason = states[name].get('reason', 'stopped before finishing')
            output(f'  - {STEPS[name]}: {reason}')
    lan = read_json(data / 'reports/gateway-lan.json').get('portal')
    links = read_json(data / 'reports/gateway-tailnet.json').get('links') or {}
    if lan or links:
        output('\nYour apps:')
        if lan:
            output(f'  At home (app menu):  {lan}')
        if links:
            output('  Anywhere, with Tailscale on:')
            for app, url in links.items():
                output(f'    {app.capitalize():<10} {url}')
    output('\nPasswords:          sudo cat ' + str(data / CREDENTIALS))
    if page and Path(page).exists():
        output('Phone setup page:   ' + str(page) + '  (open it in a browser on this PC)')
    output('Dashboard:          ' + DASHBOARD + '  (on this PC)')
    output('\nWhat next:')
    if complete:
        output('  Everything is set up. Backups and monthly updates run on their own;')
        output('  the dashboard shows how they are doing.')
    else:
        output('  Fix the items above, then run  sudo bash install.sh  again.')
        output('  It continues where it stopped and keeps everything already done.')
    output('  If something goes wrong later:  sudo bash install.sh --recover')
    output(line + '\n')
    return complete


def run(execute, default_state, prompt=input, output=print, root=ROOT):
    """`execute` is installer.execute; injected so this module stays testable."""
    if os.geteuid() != 0:
        output('Run it with sudo:  sudo bash install.sh')
        return 2
    path, created = prepare_settings(root, sudo_account())
    for line in WELCOME:
        output(line)
    output('\nYour settings are in ' + str(path) +
           (' (just created for you; you do not need to edit it).' if created else '.'))
    if not ask('\nReady to start?', prompt, output):
        output('Nothing was changed. Run  sudo bash install.sh  when you are ready.')
        return 0
    args = argparse.Namespace(all=False, profile='full', module=None, retry_failed=False,
                              apply=True, check=False, dry_run=False, config=str(path),
                              state_dir=str(default_state))
    code = execute(args)
    config = read_json(path)
    states = read_json(state_file(config, default_state))
    try:
        import phone_guide
        page = phone_guide.PAGE
    except ImportError:
        page = None
    complete = finishing_screen(config, states, page, output)
    return 0 if complete and code == 0 else 1

