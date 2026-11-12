"""Private, durable installer credentials; never put secrets in public reports."""

import contextlib
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import stat
import string

import setup_progress
import questions
from neko_common import (
    APPS as CATALOG,
    atomic_json,
    atomic_write,
    ensure_directory,
    immich_wanted,
    media_services,
    safe_path,
)

# Each account: (what it is, the app's local port).
ADMIN = ' administrator'
APPS = {
    account: (CATALOG[app][0] + role, CATALOG[app][2])
    for account, app, role in (
        ('jellyfin', 'jellyfin', ADMIN),
        ('navidrome', 'navidrome', ADMIN),
        ('navidrome_listener', 'navidrome', ' listener (non-admin)'),
        *(
            (app, app, ADMIN)
            for app in ('sonarr', 'radarr', 'lidarr', 'prowlarr', 'qbittorrent', 'seerr', 'immich', 'nextcloud')
        ),
    )
}
EMAIL_LOGINS = {'immich', 'seerr'}
STATES = {'planned', 'started', 'verified', 'needs_setup'}
# Secrets of the server itself, chosen on the accounts page with the app logins (0.50.0):
# made for you, or typed, and shown again with "Show login". {key: what it is}.
OTHERS = {'dashboard_pin': 'Dashboard PIN', 'shared_folders': 'Shared folders password'}
# The accounts page asked before the one approval (ask_ahead), for the steps of the same
# run: {'mode', 'apps': {app: entry}, 'others': {key: value}}. Written to the store once
# the app drive is mounted (collect), never before.
AHEAD = {}


def enabled(config):
    opts = config.get('accounts', {})
    if not isinstance(opts, dict) or type(opts.get('enabled', False)) is not bool:
        raise RuntimeError('accounts.enabled must be a boolean')
    return opts.get('enabled', False)


# Said when a folder worked out from the settings' data_mount does not lie on the SSD.
OUTSIDE = 'Account storage must lie on the app SSD'


def folder(config):
    return safe_path(Path(config['data_mount']) / 'secrets' / 'accounts')


def check_parent(root):
    parent = safe_path(root.parent)
    if parent.exists():
        info = parent.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Account storage parent must be root-owned and not writable by other users')


def private(path, directory=False):
    path = safe_path(path)
    info = path.stat()
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not expected or info.st_uid != 0 or info.st_mode & 0o077 or (not directory and info.st_nlink != 1):
        raise RuntimeError('Account storage must be private, root-owned and free of linked files')


def control(text):
    return any(ord(ch) < 32 or 127 <= ord(ch) < 160 for ch in text)


def existing_name_ok(app, username):
    """A login you already have in the app (NekoServer only signs in with it): the names
    the apps themselves allow, such as `mary.smith`, `Mary Smith`, `1stuser` or Immich's
    `admin@localhost`. Still refused: control characters, spaces at either end, and for
    Nextcloud a character its own names can't have (such as `:`, which would split the
    HTTP sign-in) or a leading `-` (Nextcloud's command line would read it as an option)."""
    if not 1 <= len(username) <= 128 or control(username) or username != username.strip():
        return False
    if app in EMAIL_LOGINS:
        return bool(re.fullmatch(r'[^\s@]+@[^\s@]+', username))
    # Nextcloud's own rule for a name (modules/nextcloud.py ACCOUNT_NAME), and never a
    # leading "-" (its command line would read it as an option).
    return app != 'nextcloud' or bool(re.fullmatch(r"[a-zA-Z0-9 _.@'-]+", username) and username[0] != '-')


def name_ok(app, username, existing):
    """An existing login is taken as the app has it; one NekoServer creates follows its
    own stricter pattern."""
    if existing and isinstance(username, str):
        return existing_name_ok(app, username)
    if app in EMAIL_LOGINS:
        return bool(re.fullmatch(r'[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', username)) and len(username) <= 128
    return bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', username))


def validate_login(app, username, password, existing=False):
    valid = name_ok(app, username, existing)
    if not valid and existing:
        raise ValueError(
            'Enter the login exactly as the app has it (1-128 characters, no control characters'
            + (
                "; for Nextcloud only letters, digits, spaces and _ . @ ' -, not starting with -"
                if app == 'nextcloud'
                else ''
            )
            + ')'
        )
    if not valid:
        raise ValueError(
            'Enter a valid email login'
            if app in EMAIL_LOGINS
            else 'Username: start with a letter; use 1-32 letters, digits, underscores or hyphens'
        )
    if (
        not isinstance(password, str)
        or not (1 if existing else 12) <= len(password) <= 128
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in password)
    ):
        raise ValueError('Password must contain 12-128 characters and no control characters')


def generate_other(key):
    """A PIN of 8 digits, or a password of 32 letters and digits."""
    if key == 'dashboard_pin':
        return ''.join(secrets.choice(string.digits) for _ in range(8))
    return generate('shared')[1]


def validate_other(key, value):
    """The dashboard PIN: 6 to 8 digits (0-9), or nothing (no PIN). The shared folders'
    password: 12 to 128 characters and no control characters."""
    if key == 'dashboard_pin':
        if value and not re.fullmatch(r'\d{6,8}', value, re.ASCII):
            raise ValueError('A PIN is 6 to 8 digits (0-9)')
        return
    if not isinstance(value, str) or not 12 <= len(value) <= 128 or control(value):
        raise ValueError('Password must contain 12-128 characters and no control characters')


def other(config, key):
    """A server secret chosen on the accounts page: in this run's answers, else in the
    store; None when there is none."""
    if key in AHEAD.get('others', {}):
        return AHEAD['others'][key] or None
    try:
        return load(config).get('others', {}).get(key) or None
    except (RuntimeError, OSError, KeyError):
        return None


def generate(app):
    username = 'neko_' + secrets.token_hex(6)
    if app in EMAIL_LOGINS:
        username += '@server.invalid'
    alphabet = string.ascii_letters + string.digits + '-_'
    while True:
        password = ''.join(secrets.choice(alphabet) for _ in range(32))
        if all(
            any(ch in group for ch in password)
            for group in (string.ascii_lowercase, string.ascii_uppercase, string.digits)
        ):
            break
    return username, password


def drop_unfinished(root):
    """Remove the unfinished write a cut-off first save left (".credentials.json-xxxx",
    from atomic_write): it was never the store, and it may hold passwords."""
    for item in root.iterdir():
        if (
            item.name.startswith(('.credentials.json-', '.APP-CREDENTIALS.txt-'))
            and item.is_file()
            and not item.is_symlink()
        ):
            item.unlink()


def load(config):
    root = folder(config)
    # The SSD's path comes from the settings file: the store is read only once its folder
    # is proved to lie on the SSD with every link followed.
    if not os.path.realpath(root).startswith(os.path.realpath(config['data_mount']) + os.sep):
        raise RuntimeError(OUTSIDE)
    check_parent(root)
    if not root.exists():
        return {'format': 1, 'mode': 'per-app', 'apps': {}}
    private(root, directory=True)
    path = root / 'credentials.json'
    if not path.exists():
        drop_unfinished(root)
        if any(root.iterdir()):
            raise RuntimeError('Incomplete account store; preserve files and inspect before retrying')
        return {'format': 1, 'mode': 'per-app', 'apps': {}}
    private(path)
    if path.stat().st_size > 128_000:
        raise RuntimeError('Account store exceeds expected size')
    try:
        value = json.loads(path.read_text())
        if (
            value.get('format') != 1
            or value.get('mode') not in ('generate', 'custom', 'per-app')
            or not isinstance(value.get('apps'), dict)
        ):
            raise ValueError
        others = value.get('others', {})
        if not isinstance(others, dict) or any(key not in OTHERS for key in others):
            raise ValueError
        for key, secret in others.items():
            validate_other(key, secret)
        for app, entry in value['apps'].items():
            if (
                app not in APPS
                or not isinstance(entry, dict)
                or entry.get('state') not in STATES
                or type(entry.get('allow_create')) is not bool
            ):
                raise ValueError
            validate_login(app, entry['username'], entry['password'], existing=not entry['allow_create'])
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeError('Invalid private account store; contents preserved') from None
    return value


def save(config, value):
    root = folder(config)
    # As in load: written only to a folder proved to lie on the SSD.
    if not os.path.realpath(root).startswith(os.path.realpath(config['data_mount']) + os.sep):
        raise RuntimeError(OUTSIDE)
    check_parent(root)
    if root.exists():
        private(root, directory=True)
    else:
        ensure_directory(root, 0o700)
    for name in ('credentials.json', 'APP-CREDENTIALS.txt'):
        if (root / name).exists():
            private(root / name)
        safe_path(root / name)
    atomic_json(root / 'credentials.json', value)
    lines = [
        'NEKOSERVER APP CREDENTIALS',
        'Sensitive plaintext - root only. Keep any offline copy private.',
        'Only VERIFIED entries have passed an authenticated account check.',
        'Generated @server.invalid logins cannot receive email recovery messages.',
        '',
    ]
    for app in APPS:
        if app not in value['apps']:
            continue
        entry = value['apps'][app]
        lines.extend(
            [
                f'=== {APPS[app][0]} ===',
                f'Local URL: http://127.0.0.1:{APPS[app][1]}',
                f'Status: {entry["state"].upper()}',
                f'Username: {entry["username"]}',
                f'Password: {entry["password"]}',
                '',
            ]
        )
    for key, secret in value.get('others', {}).items():
        if secret:
            lines.extend([f'=== {OTHERS[key]} ===', f'Secret: {secret}', ''])
    atomic_write(root / 'APP-CREDENTIALS.txt', '\n'.join(lines) + '\n', 0o600)


def get(config, app, verified=False):
    """An app's saved login; for a server secret (OTHERS) its value as the password."""
    if app in OTHERS:
        secret = other(config, app)
        return {'username': '', 'password': secret, 'state': 'verified'} if secret else None
    entry = load(config)['apps'].get(app)
    if entry and (not verified or entry['state'] == 'verified'):
        return entry
    return None


def update(config, app, **changes):
    value = load(config)
    value['apps'][app].update(changes)
    save(config, value)


def selected(config, plan):
    result = []
    if 'media' in plan:
        result.extend(media_services(config))
        if 'navidrome' in result:
            result.insert(result.index('navidrome') + 1, 'navidrome_listener')
    if 'photos' in plan and immich_wanted(config):
        result.append('immich')
    if 'nextcloud' in plan and config.get('nextcloud', {}).get('enabled', True):
        result.append('nextcloud')
    return [app for app in APPS if app in result]


def ask_login(app, existing, value, prompt, password_prompt, output, on_page=False):
    """A login typed in: the user name (or email), the password and the password again.
    `on_page`: the accounts page is open (accounts_page), where the window shows every
    app's row at once and sends them together; alone, each entry is a page of its own."""
    name = APPS[app][0]
    while True:
        key = 'login_email' if app in EMAIL_LOGINS else 'login_name'
        if on_page:
            username = setup_progress.page_field('user:' + app, prompt, questions.terminal(key, app=name)).strip()
            password = setup_progress.page_field(
                'pass:' + app, password_prompt, f'Password for {name} (it is not shown while you type): '
            )
            confirmation = setup_progress.page_field('again:' + app, password_prompt, 'The same password once more: ')
        else:
            username = questions.ask(key, prompt, app=name).strip()
            # In the setup window the password and its second entry are one page.
            with setup_progress.password_page(name + ': password'):
                password = setup_progress.page_field(
                    'password', password_prompt, f'Password for {name} (it is not shown while you type): '
                )
                confirmation = setup_progress.page_field('again', password_prompt, 'The same password once more: ')
        try:
            validate_login(app, username, password, existing=existing)
            if app == 'navidrome_listener' and value['apps'].get('navidrome', {}).get('username') == username:
                raise ValueError('The music listener must have a different username from the administrator')
            if password != confirmation:
                raise ValueError('Passwords did not match; try again')
        except ValueError as exc:
            output(str(exc))
            if on_page:
                setup_progress.page_set('problem', f'{name}: {exc}')  # said on the page, beside its rows
            continue
        return username, password


PAGE_TITLE = 'How would you like to set up your app accounts?'
PAGE_HINT = (
    'Each selected app has a separate account. NekoServer stores its required credentials privately so it can '
    'connect and check the apps.'
)
EXISTING = 'This app has data already: enter its current login. The account in the app is not changed or reset.'


def fresh(config, app):
    """The app has no data yet, so its first account is made by setup. With data there
    already, the login of the account it has is entered instead. Its data is looked for
    on the SSD and, for an app installed on a drive of its own (placement, 0.50.0), on
    that drive too: the accounts page is asked before setup mounts that drive's folder at
    the SSD's place, and an app whose settings were kept there (the SSD replaced, gate
    run C10) must never be given a login it does not know."""
    actual_app = 'navidrome' if app == 'navidrome_listener' else app
    data = safe_path(Path(config['data_mount']) / 'appdata' / actual_app)
    # Looked at only once it is proved to lie on the SSD (its path comes from the settings).
    if not os.path.realpath(data).startswith(os.path.realpath(config['data_mount']) + os.sep):
        raise RuntimeError(OUTSIDE)
    kept = kept_elsewhere(config, actual_app)
    places = [data] + ([kept] if kept else [])
    return all(not place.exists() or (place.is_dir() and not any(place.iterdir())) for place in places)


def kept_elsewhere(config, app):
    """Where an app installed on a drive of its own keeps its settings on that drive
    (<drive>/nekoserver/appdata/<app>, which setup mounts at the SSD's place), or None for
    an app on the SSD. Checked to lie on that drive."""
    import placement
    from neko_common import drive_folder

    drive = placement.apps_elsewhere(config).get(app)
    if not drive:
        return None
    root = os.path.realpath(drive_folder(config, drive))
    folder = os.path.realpath(os.path.join(root, 'nekoserver', 'appdata', app))
    if not folder.startswith(root + os.sep):
        raise RuntimeError(OUTSIDE)
    return Path(folder)


# Where the recovery menu unpacks a list of app passwords ("The list of app passwords"):
# a new folder of root's, /root/nekoserver-passwords-<time>.
RESTORED = Path('/root')
RESTORED_GLOB = 'nekoserver-passwords-*/secrets/accounts/credentials.json'


def restored_login(app, root=RESTORED):
    """The login `app` had in the newest list of app passwords the recovery menu restored
    (root's own and private), as (username, password); None without one."""
    try:
        lists = sorted(root.glob(RESTORED_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in lists:
        try:
            private(path)
            entry = json.loads(path.read_text()).get('apps', {}).get(app)
            if isinstance(entry, dict):
                validate_login(app, entry['username'], entry['password'], existing=True)
                return entry['username'], entry['password']
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            continue  # a list that can't be read, or without this app: the next one
    return None


LIST_IN_BACKUP = (
    '  A list of your app passwords is in a backup: to look this login up, run  sudo bash install.sh --recover  '
    'and choose "The list of app passwords"; run setup again after that, and the login is taken from it.'
)


def existing_login(config, app, value, prompt, password_prompt, output, on_page=False):
    """The login an app with data already has: from a list of app passwords the recovery
    menu restored, when it holds this app; otherwise typed, after saying where a backup's
    list is when there is one. Never one made up: the app would not know it."""
    found = restored_login(app)
    if found:
        output(f'  Its login is taken from the list of app passwords you restored: username {found[0]}.')
        return found
    if password_lists(config):
        output(LIST_IN_BACKUP)
    return ask_login(app, True, value, prompt, password_prompt, output, on_page=on_page)


# Where the backups keep the lists of app passwords on the HDD (recovery_wizard.py
# MONTHLY_DIR and PASSWORD_LISTS: each list is <run>/secrets.tar.cms). Only looked at here:
# the recovery menu itself is started by  install.sh --recover  alone.
PASSWORD_LIST_FOLDERS = ('backups/monthly', 'backups/password-lists')


def password_lists(config):
    """There is a list of app passwords in a backup on the HDD, for the recovery menu to open."""
    try:
        media = Path(config['media_mount'])
        return any(any(media.joinpath(folder).glob('*/secrets.tar.cms')) for folder in PASSWORD_LIST_FOLDERS)
    except (OSError, KeyError, TypeError):
        return False


def accounts_page(config, apps, others=()):
    """While this is active, the logins of these apps are one page in the setup window:
    the one choice (strong passwords made for you, or your own logins) and a row for
    each app, typed in only where the choice or the app needs it. The window sends them
    together; the terminal is asked one at a time. No password is ever in an event: the
    page's fields carry no values."""
    fields = {'mode': '', 'problem': ''}
    rows = []
    for app in apps:
        fields.update({'user:' + app: '', 'pass:' + app: '', 'again:' + app: ''})
        rows.append({'id': app, 'name': APPS[app][0], 'email': app in EMAIL_LOGINS, 'existing': not fresh(config, app)})
    for key in others:
        # A secret of the server: a password (or a PIN) and nothing else to type.
        fields.update({'user:' + key: '', 'pass:' + key: '', 'again:' + key: ''})
        rows.append({'id': key, 'name': OTHERS[key], 'secret_only': True, 'pin': key == 'dashboard_pin'})
    return setup_progress.page(
        'accounts',
        fields,
        title=PAGE_TITLE,
        hint=PAGE_HINT,
        choices=[list(choice) for choice in questions.Q['logins_who']['choices']],
        apps=rows,
        existing=EXISTING,
    )


def collect(config, apps, prompt=input, password_prompt=getpass.getpass, output=print, allow_corrections=True):
    """Persist each answer before deployment; retain saved answers on retry."""
    value = load(config)
    if allow_corrections:
        for app in apps:
            entry = value['apps'].get(app)
            if not entry or entry['state'] != 'needs_setup':
                continue
            output(APPS[app][0] + ': saved login was not verified.')
            if entry['allow_create'] and not fresh(config, app):
                output(
                    f'  {APPS[app][0]} has its own account from before (its settings were kept), so the login setup '
                    'made for it is not one it knows. Choose c and type the login it has now.'
                )
            elif entry['allow_create']:
                # Account creation did not finish. If you completed the app's first-run
                # setup yourself with another login, switch to that existing account.
                output(
                    '  If you finished this app\'s setup yourself with a different login, choose c to enter it '
                    '(the account in the app is not changed or reset).'
                )
            while True:
                answer = questions.ask('login_retry', prompt, app=APPS[app][0]).strip().lower()
                if answer in ('', 'c'):
                    break
                output('Press Enter alone, or type c.')
            if answer == 'c':
                username, password = existing_login(config, app, value, prompt, password_prompt, output)
                entry.update(
                    username=username, password=password, state='planned', source='custom-existing', allow_create=False
                )
                save(config, value)
    if AHEAD.get('others'):
        # The PIN and the shared folders' password chosen on the page before the approval.
        value.setdefault('others', {}).update({k: v for k, v in AHEAD['others'].items() if v})
    missing = [app for app in apps if app not in value['apps']]
    # A login made on the accounts page for an app found since to have data of its own
    # (its drive mounted by the storage step meanwhile) is not one the app knows: asked.
    ahead = {
        app: AHEAD['apps'][app]
        for app in missing
        if app in AHEAD.get('apps', {}) and not (AHEAD['apps'][app]['allow_create'] and not fresh(config, app))
    }
    if ahead:
        # Chosen on the accounts page before the approval: saved now, nothing is asked.
        value['mode'] = AHEAD.get('mode', value['mode'])
        value['apps'].update(ahead)
        missing = [app for app in missing if app not in ahead]
    if not missing:
        save(config, value)  # Repair a missing human-readable export from JSON.
        return
    # The first time: one page for the choice and every app's row (accounts_page). The
    # choice made on the accounts page before the approval is not asked again.
    first = not value['apps']
    chosen = AHEAD.get('mode') in ('generate', 'custom')
    if first and chosen:
        value['mode'] = AHEAD['mode']
    with accounts_page(config, missing) if first else contextlib.nullcontext():
        if first and not chosen:
            while True:
                answer = setup_progress.page_field('mode', prompt, questions.terminal('logins_who')).strip()
                # Enter alone takes the first, the recommended one (gate run B1).
                if answer in ('', '1', '2'):
                    value['mode'] = 'custom' if answer == '2' else 'generate'
                    break
                output('Type 1 or 2.')
        for app in missing:
            output(APPS[app][0])
            is_fresh = fresh(config, app)
            # One choice for all of them ("per-app", asked app by app until 0.50.0, reads as your own).
            mode = 'generate' if value['mode'] == 'generate' else 'custom'
            if not is_fresh:
                output('  Existing app data: enter the current login; it will not be reset.')
                mode = 'custom'
            if mode == 'generate':
                username, password = generate(app)
                output(
                    f'  Made and saved: username {username}, with a strong password. Wherever setup asks you to '
                    'sign in, "Show login" shows it (in the setup window, or as a question in the terminal).'
                )
            elif not is_fresh:
                username, password = existing_login(config, app, value, prompt, password_prompt, output, on_page=first)
            else:
                username, password = ask_login(app, False, value, prompt, password_prompt, output, on_page=first)
            value['apps'][app] = {
                'username': username,
                'password': password,
                'state': 'planned',
                'allow_create': is_fresh,
                'source': mode,
            }
            save(config, value)
    output('Account choices saved privately; passwords are not printed or written to installer logs.')


def others_wanted(config):
    """The server secrets this server has not chosen yet: the dashboard PIN (none set) and
    the shared folders' password (the shared folders switched on and no password yet)."""
    import shutil
    import subprocess

    wanted = []
    if not Path('/etc/nekoserver-control/pin.json').exists():
        wanted.append('dashboard_pin')
    if config.get('samba', {}).get('enabled', True) is not False:
        user = os.environ.get('SUDO_USER') or 'neko'
        known = ''
        if shutil.which('pdbedit'):
            known = subprocess.run(['pdbedit', '-L', '-u', user], capture_output=True, text=True, timeout=15).stdout
        if not any(line.startswith(user + ':') for line in known.splitlines()):
            wanted.append('shared_folders')
    return wanted


def asked_other(key, mode, prompt, password_prompt, output):
    """A server secret on the accounts page: made for you, or typed twice (a PIN may be
    left empty: no PIN)."""
    if mode == 'generate':
        output(f'  {OTHERS[key]}: made and saved for you; "Show login" shows it.')
        return generate_other(key)
    for _ in range(3):
        value = setup_progress.page_field(
            'pass:' + key, password_prompt, f'{OTHERS[key]} (it is not shown while you type): '
        )
        again = setup_progress.page_field('again:' + key, password_prompt, 'The same once more: ')
        try:
            validate_other(key, value)
            if value != again:
                raise ValueError('The two did not match; try again')
        except ValueError as exc:
            output(str(exc))
            setup_progress.page_set('problem', f'{OTHERS[key]}: {exc}')
            continue
        return value
    return ''


def ask_ahead(config, prompt=input, password_prompt=getpass.getpass, output=print):
    """The accounts page, asked before the one approval (the user: nothing typed after it
    but what needs the running server): the choice, each new app's login and the server's
    own secrets, kept in AHEAD for collect(). Nothing is written. A server whose apps all
    have their logins asks only for a secret it lacks."""
    if not enabled(config):
        return
    try:
        stored = load(config)
    except RuntimeError:
        return  # a store that cannot be read is said by the accounts step itself
    apps = [app for app in selected(config, ('media', 'photos', 'nextcloud')) if app not in stored['apps']]
    others = others_wanted(config)
    if not apps and not others:
        return
    AHEAD.clear()
    AHEAD.update(mode=stored['mode'], apps={}, others={})
    with accounts_page(config, apps, others):
        if not stored['apps']:
            while True:
                answer = setup_progress.page_field('mode', prompt, questions.terminal('logins_who')).strip()
                # Enter alone takes the first, the recommended one (gate run B1).
                if answer in ('', '1', '2'):
                    AHEAD['mode'] = 'custom' if answer == '2' else 'generate'
                    break
                output('Type 1 or 2.')
        mode = 'generate' if AHEAD['mode'] == 'generate' else 'custom'
        for app in apps:
            output(APPS[app][0])
            is_fresh = fresh(config, app)
            if mode == 'generate' and is_fresh:
                username, password = generate(app)
                output(f'  Made: username {username}, with a strong password. "Show login" shows it.')
            elif not is_fresh:
                username, password = existing_login(config, app, stored, prompt, password_prompt, output, on_page=True)
            else:
                username, password = ask_login(app, False, stored, prompt, password_prompt, output, on_page=True)
            AHEAD['apps'][app] = {
                'username': username,
                'password': password,
                'state': 'planned',
                'allow_create': is_fresh,
                'source': mode if is_fresh else 'custom',
            }
        for key in others:
            AHEAD['others'][key] = asked_other(key, mode, prompt, password_prompt, output)
