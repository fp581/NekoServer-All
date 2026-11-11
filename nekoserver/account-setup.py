#!/usr/bin/env python3
"""Interactive account setup for already deployed NekoServer apps."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import account_store
import account_setup
from modules.accounts import finish_pending
from types import SimpleNamespace
from neko_common import (
    safe_path,
    settings_path,
    verify_storage,
    checked_settings,
    default_settings,
    media_services,
    navidrome_login,
)
from recovery_tools import lock


SHOW_HELP = (
    'To see one app\'s username and password on this screen, one at a time:  '
    'sudo python3 account-setup.py show jellyfin  (leave out the app name to choose from a list).'
)
SHOW_WARNING = (
    'This puts a password on the screen. Check that nobody else can see it, and do not take a '
    'photo or screenshot of it.'
)
# Moves to the top, clears the screen and, in most terminals, the lines scrolled out of view too.
CLEAR_SCREEN = '\033[H\033[2J\033[3J'
# A word on Navidrome's two logins, whose names alone do not say which one is yours.
# A server set up before 0.50.0: the phones play music with the listener.
SHOW_HINT = {
    'navidrome_listener': 'Phones and computers set up so far use this one: it is the account for playing '
    'music (not an administrator). Playlists and favourites belong to the login that made them.',
    'navidrome': 'The administrator, for managing Navidrome. Phones set up so far play music with the other '
    'Navidrome login, the listener.',
}
# A server whose Navidrome logins were made by 0.50.0 or later: the administrator is yours.
SHOW_HINT_ADMINISTRATOR = {
    'navidrome': 'Your Navidrome login, on your phone and computer too.',
    'navidrome_listener': "NekoServer's own login for checking the music library (not an administrator). "
    'Yours is the Navidrome administrator.',
}


def show_hints(settings):
    """The hints for this server's Navidrome logins (neko_common.navidrome_login)."""
    try:
        config = json.loads(settings_path(settings).read_text())
    except (OSError, ValueError, RuntimeError):
        return SHOW_HINT
    return SHOW_HINT_ADMINISTRATOR if navidrome_login(config) == 'navidrome' else SHOW_HINT


def show_refused(screen):
    """Why the logins cannot be shown here, or ''. They are shown only to root (the file
    they come from is root's alone) and only on a terminal screen: never into a pipe, a
    file or a log, where a password would stay behind."""
    if os.geteuid() != 0:
        return (
            'Run it with sudo (only the administrator may see the passwords):  sudo python3 '
            + str(Path(__file__).resolve())
            + ' show'
        )
    if not screen.isatty():
        return (
            'The passwords are shown only on a screen, never into a file, a log or another program. '
            'Run this in a terminal window.'
        )
    return ''


def saved_logins(settings):
    """The saved logins, from the SSD the settings name (checked to be that drive)."""
    config = json.loads(settings_path(settings).read_text())
    verify_storage(config, keys=('data_ssd',))
    return account_store.load(config)['apps']


def terminal_qr(text):
    """`text` as a QR code drawn in the terminal, or '' without qrencode. The text goes in
    on qrencode's input, not on its command line, where other accounts could read it."""
    if not shutil.which('qrencode'):
        return ''
    try:
        result = subprocess.run(
            ['qrencode', '-t', 'ANSIUTF8', '-m', '2'],
            input=text,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    return result.stdout if result.returncode == 0 else ''


def show_entry(app, entry, prompt, output, qr, hints=SHOW_HINT):
    """One login on the screen until Enter, then the screen is cleared."""
    output(SHOW_WARNING + '\n')
    output(account_store.APPS[app][0])
    if app in hints:
        output('  ' + hints[app])
    if entry['state'] != 'verified':
        output('  (Setup has not confirmed this login yet: it may not work until setup has finished.)')
    output('  Username: ' + entry['username'])
    output('  Password: ' + entry['password'])
    code = qr(entry['password'])
    if code:
        output(
            '\n  Or scan this code with the phone camera: it shows the password as text you can copy and '
            'paste into the app.\n' + code
        )
    output('  Capital letters matter: type it exactly as shown.')
    try:
        prompt('\nPress Enter to hide it (the screen is cleared). ')
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        output(CLEAR_SCREEN)


def show_menu(logins, prompt, output, qr, hints=SHOW_HINT):
    """A numbered list of the saved logins; each number shows one. Enter stops."""
    names = [app for app in account_store.APPS if app in logins]
    while True:
        output('Saved logins:')
        for number, app in enumerate(names, 1):
            output(f'  {number}. {account_store.APPS[app][0]}')
        try:
            answer = prompt('Type a number to show that login (press Enter to stop): ').strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not answer:
            return
        if answer.isdecimal() and 1 <= int(answer) <= len(names):
            show_entry(names[int(answer) - 1], logins[names[int(answer) - 1]], prompt, output, qr, hints)
        else:
            output('Type one of the numbers above, or press Enter to stop.')


def show(settings, app=None, prompt=input, output=print, qr=terminal_qr, screen=sys.stdout):
    """`account-setup.py show [APP]`: one app's saved login on this screen, as text and
    as a QR code, so it can go onto a phone without reading the whole password file.
    Nothing is written to a log or a file."""
    problem = show_refused(screen)
    if problem:
        output(problem)
        return 2
    try:
        logins = saved_logins(settings)
    except RuntimeError as exc:
        output('The saved logins could not be read: ' + str(exc) + '. Check that the SSD is connected, then try again.')
        return 2
    except (OSError, ValueError, KeyError, TypeError):
        output(
            'The saved logins could not be read. Check that the SSD is connected and that the settings file '
            'is the one setup uses (add  --config /path/to/my-config.json), then try again.'
        )
        return 2
    if not logins:
        output('No app logins are saved yet. Setup makes them at its "App passwords" step:  sudo bash install.sh')
        return 1
    hints = show_hints(settings)
    if app is None:
        show_menu(logins, prompt, output, qr, hints)
        return 0
    key = app.strip().lower().replace('-', '_')
    if key not in logins:
        output(
            'There is no saved login called "'
            + app
            + '". Saved: '
            + ', '.join(name for name in account_store.APPS if name in logins)
            + '.'
        )
        return 2
    show_entry(key, logins[key], prompt, output, qr, hints)
    return 0


# What `signin` answers (the weekly update reads the exit code).
SIGNED_IN, REFUSED, NO_LOGIN = 0, 1, 3


def signin(settings, app, output=print):
    """`account-setup.py signin APP`: whether the app takes its saved login now. The weekly
    update asks before it installs a new version and again after: an update that breaks
    signing in is undone. Nothing is made or changed, and no password is printed. Exit
    code 0: signed in; 1: refused (the reason is printed); 3: no saved login to try."""
    if os.geteuid() != 0:
        output('Run it with sudo: only the administrator may use the saved logins')
        return NO_LOGIN
    try:
        config = json.loads(settings_path(settings).read_text())
        verify_storage(config, keys=('data_ssd',))
        if not account_store.enabled(config) or app not in account_store.APPS:
            return NO_LOGIN
        answer = account_setup.signs_in(config, app)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        output(app + ': the saved login was not accepted (' + (str(exc) or type(exc).__name__) + ')')
        return REFUSED
    if answer is None:
        output(app + ': no saved login could be tried (none is saved, or two-factor sign-in is on)')
        return NO_LOGIN
    output(app + ': signed in with its saved login')
    return SIGNED_IN


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=SHOW_HELP)
    parser.add_argument(
        'action',
        nargs='?',
        choices=('show', 'signin'),
        help='show: put one app\'s login on this screen (and as a QR code), one at a time; '
        'signin: check that an app takes its saved login (nothing is changed or shown)',
    )
    parser.add_argument('app', nargs='?', help='with show: the app, such as jellyfin (leave out for a list)')
    parser.add_argument(
        '--config', type=lambda v: Path(checked_settings(v)), default=default_settings(Path(__file__).parent)
    )
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.action == 'show':
        return show(args.config, args.app)
    if args.action == 'signin':
        if not args.app:
            parser.error('signin needs the app, such as jellyfin')
        return signin(args.config, args.app.strip().lower())
    if not args.apply:
        print('Read-only: use --apply interactively to choose and configure accounts. No secrets printed.')
        return 0
    if os.geteuid() != 0 or not sys.stdin.isatty():
        parser.error('Run with sudo in an interactive terminal')
    config = json.loads(safe_path(args.config).read_text())
    config['accounts'] = {**config.get('accounts', {}), 'enabled': True}
    verify_storage(config)
    apps = []
    for app in account_store.selected(config, ('media', 'photos', 'nextcloud')):
        deployment = 'navidrome' if app == 'navidrome_listener' else app
        if safe_path(Path(config['data_mount']) / 'compose' / deployment / 'compose.yaml').is_file():
            apps.append(app)
    if not apps:
        parser.error('No selected deployed app manifests found; run the installer first')
    before = account_store.get(config, 'qbittorrent')
    with lock(config):
        account_store.collect(config, apps)
        pending = account_setup.provision(config, apps)
    pending = finish_pending(SimpleNamespace(config=config, apply=True, log=print), pending)
    passed_on = pass_on_qbittorrent(args.config, config, apps, before)
    print('Private credential inventory: ' + str(account_store.folder(config) / 'APP-CREDENTIALS.txt'))
    return 1 if pending or not passed_on else 0


def pass_on_qbittorrent(settings, config, apps, before, run=subprocess.run):
    """A qBittorrent login changed here (c: enter the current login) is passed on to the
    download clients setup made in Sonarr, Radarr and Lidarr. They keep their own copy of
    it and would go on using the old one: downloads would stop without a word. True when
    nothing was left to do or it was passed on."""
    now = account_store.get(config, 'qbittorrent', verified=True)
    if (
        'qbittorrent' not in apps
        or not before
        or not now
        or (now['username'], now['password']) == (before['username'], before['password'])
    ):
        return True
    managers = [app for app in ('sonarr', 'radarr', 'lidarr') if app in media_services(config)]
    if not managers:
        return True
    print('qBittorrent: its login changed; passing it on to the download client in ' + ', '.join(managers))
    script = Path(__file__).with_name('media-connect.py')
    if run(
        [sys.executable, str(script), '--config', str(settings), '--pass-on-login', '--apply'], check=False
    ).returncode:
        print(
            'qBittorrent: the new login is not in every app yet (see above). Enter it in each app '
            '(Settings, Download Clients, Neko-qBittorrent), or run  sudo bash install.sh  again'
        )
        return False
    return True


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError, KeyboardInterrupt, EOFError):
        print('Account setup incomplete; saved choices preserved. No credentials printed.', file=sys.stderr)
        raise SystemExit(1) from None
