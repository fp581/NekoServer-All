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
import termios

import explanations
import guided_setup
import neko_common
import setup_gui
import setup_progress
from neko_common import (
    ASSISTANT,
    VERSION,
    atomic_json,
    dashboard_port,
    media_services,
    mount_path,
    safe_path,
    settings_path,
)

ROOT = Path(__file__).resolve().parent
SETTINGS = 'my-config.json'
TEMPLATE = 'config.json'  # the settings template; also the name of the copy on the SSD
RELEASE = re.compile(r'v(\d{1,9})\.(\d{1,9})\.(\d{1,9})')

STEPS = {
    'system': 'System check',
    'network_storage': 'Drives on the NAS',
    'storage': 'Drives',
    'capacity': 'Drive space',
    'recovery': 'Recovery copy of NekoServer',
    'account_credentials': 'App passwords',
    'server_os': 'No-sleep and log limits',
    'docker': 'Docker',
    'recovery_checkpoint': 'Recovery checkpoint',
    'media': 'Movie, TV and music apps',
    'photos': 'Immich (photos)',
    'photo_preservation': 'Weekly photo backup to the HDD',
    'photo_backup': 'Weekly backup to the USB drive',
    'nextcloud': 'Nextcloud (files)',
    'accounts': 'App accounts',
    'hardware_video': 'Video using the graphics chip',
    'onboarding': 'Connecting the apps together',
    'music': 'Music',
    'assistant': 'AI assistant',
    'gateway_lan': 'Access at home',
    'tailscale': 'Tailscale',
    'gateway_tailnet': 'Access from anywhere and phone setup',
    'samba': 'Shared folders',
    'media_monitor': 'App health checks',
    'dashboard': 'Dashboard',
    'tv': 'TV setup',
    'desktop': 'Desktop shortcuts',
    'app_backup': 'Nightly app backup',
    'notifications': 'Phone alerts',
    'heartbeat': 'Server heartbeat',
    'host_backup': 'Nightly settings backup',
    'maintenance': 'Monthly updates',
}

WELCOME = [
    f'Welcome to NekoServer {VERSION}.',
    '',
    'The packages the apps need are installed (on Arch the whole system was updated first, as Arch needs).',
    'Next, this sets up your home media server. It will:',
    '  1. Connect this server to Tailscale, so you can use your apps from anywhere, privately.',
    '  2. Ask which drives hold the apps, your media and the backups (one drive is enough), and check the space.',
    '  3. Install the apps you choose below and create an account for each one.',
    '  4. Connect the apps to each other.',
    '  5. Give each app a private Tailscale address and walk you through it on your phone.',
    '  6. Schedule backups and monthly updates, then run a first checkup and backup.',
    '',
    'Have ready: the drives you want to use, plugged in and already formatted (one drive, or the',
    'disk Linux runs from, with about 70 GB free is enough; a USB drive of 16 GB or more for an',
    'offline copy is optional), your phone, and about an hour.',
    'It pauses whenever it needs you and explains what to do.',
    'Nothing is formatted or deleted. You can stop with Ctrl+C and run it again later;',
    'it continues where it stopped.',
]


# The same on a server that is set up already (an update or a change, gate run D).
UPDATE_WELCOME = [
    f'Welcome to NekoServer {VERSION}.',
    '',
    'NekoServer is set up on this PC. Your apps, accounts, settings and files are kept: the pages',
    'that follow show your saved answers, and nothing changes until you approve. You can stop with',
    'Ctrl+C and run it again later; it continues where it stopped.',
]


def welcome(config):
    """The welcome: a first install's, or the short one of a server set up already."""
    return UPDATE_WELCOME if neko_common.server_exists(config or {}) else WELCOME


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
    else:
        # Started as root, not with sudo from someone's account (Debian with a root password
        # and no sudo, say): the template's user ID is kept only when an account has it.
        # Otherwise the apps' files would belong to nobody and the shared folder could not
        # be set up, so setup stops before anything is written and says how to name yours.
        uid = settings.get('media', {}).get('uid', 1000)
        try:
            pwd.getpwuid(int(uid))
        except (KeyError, TypeError, ValueError):
            raise RuntimeError(
                'Setup does not know whose account this server is for: it was started as root, not with sudo '
                'from your own account, and this PC has no account with user ID ' + str(uid) + '. Run it from '
                'your own account with  sudo bash install.sh , or as root name your account:  SUDO_USER=<your '
                'account name> bash install.sh'
            ) from None
    atomic_json(path, settings)
    if account is not None:
        # Readable and editable by the person who ran sudo, nobody else.
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
    os.chmod(path, 0o600)
    return path, True


def release_version(name, prefix=''):
    """(major, minor, patch) of a folder named prefix + 'v1.2.3', else None."""
    match = RELEASE.fullmatch(name[len(prefix) :]) if name.startswith(prefix) else None
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
            output(
                'The settings at '
                + str(path)
                + ' cannot be read (not a settings file, or damaged), so they are not used.'
            )
    return None


def take_earlier_settings(root=ROOT, account=None, output=print):
    """The settings earlier_settings finds, copied in as my-config.json; True when they
    were. Without them an update would set the server up from the start. Not asked
    about here: the page before it (existing_installation) says what was found and
    takes the person's choice."""
    source = earlier_settings(root, output)
    if source is None:
        return False
    output('The settings of the server on this PC are in ' + str(source) + '.')
    path = Path(root) / SETTINGS
    atomic_json(path, read_json(source))
    if account is not None:
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
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
        output(
            '\nA setup window opened in your browser: it explains each step as it happens. Put it where you like.'
            '\nClosed it by mistake? Open  ' + window.url + '  again in a browser on this PC, from this account.'
        )
    else:
        output('\nTo follow along in a window, open  ' + window.url + '  in a browser on this PC.')
    return window


STARTED = object()  # the window's Install button was pressed while the terminal waited for a line
CONTINUED = object()  # its Continue button, on the first page
BACK = object()  # its Back button, on the review
PRESSED_SAID = {STARTED: 'yes', CONTINUED: 'Continue', BACK: 'Back'}


def window_pressed(window):
    """The button pressed in the setup window since the terminal last looked, or None."""
    if window.started.is_set():
        return STARTED
    return {'continue': CONTINUED, 'back': BACK}.get(window.pressed())


def typed_line(stdin, output, wait=0.5):
    """(a line typed in the terminal, True), (None, True) for Ctrl+D or Ctrl+C,
    (None, False) when nothing was typed in this half second. OSError or ValueError:
    the terminal cannot be watched."""
    try:
        readable, _, _ = select.select([stdin], [], [], wait)
    except KeyboardInterrupt:
        output('')
        return None, True
    if not readable:
        return None, False
    line = stdin.readline()
    if not line:  # Ctrl+D
        output('')
        return None, True
    return line, True


def line_or_start(question, prompt, output, window, stdin=None):
    """One line typed in the terminal; STARTED, CONTINUED or BACK when that button of the
    setup window was pressed meanwhile; None for Ctrl+D or Ctrl+C."""
    if window is None or prompt is not builtins.input:
        try:
            return prompt(question)
        except (EOFError, KeyboardInterrupt):
            output('')
            return None
    stdin = sys.stdin if stdin is None else stdin
    sys.stdout.write(question)
    sys.stdout.flush()
    pressed = window_pressed(window)
    while pressed is None:
        try:
            line, answered = typed_line(stdin, output)
        except (OSError, ValueError):
            return line_or_start('', prompt, output, None)
        if answered:
            return line
        pressed = window_pressed(window)
    # A line half-typed in the terminal meanwhile would be read as the next answer.
    try:
        termios.tcflush(stdin, termios.TCIFLUSH)
    except (termios.error, OSError, ValueError):
        pass
    output(PRESSED_SAID[pressed] + ' (pressed in the setup window)')
    return pressed


def show_first_page(config, output):
    """Setup's first page as the terminal shows it: every part that can be left out on
    a numbered line with its mark. Returns the extras in the order of their numbers."""
    output('')
    output('=== ' + explanations.PAGE_TITLE + ' ===')
    output(explanations.PAGE_HINT)
    now = explanations.states(config)
    part = None
    for number, extra in enumerate(explanations.shown(), 1):
        if extra['part'] != part:
            part = extra['part']
            output('')
            output(dict(explanations.PARTS)[part])
        on = now[extra['id']] and explanations.offered(config, extra)
        # A switch inside a card (finding and downloading) is listed under it, set in.
        indent = '     ' if extra.get('under') else ''
        output(f"{indent} [{'x' if on else ' '}] {number:2d}. {extra['title']}")
        output(indent + '        ' + extra['explanation'])
    return explanations.shown()


def show_review(config, storage_lines, output):
    """The review: what will be installed, where the apps and files are kept (as just
    chosen), and what setup still stops to ask."""
    output('')
    output(
        '=== '
        + (explanations.REVIEW_TITLE_AGAIN if neko_common.server_exists(config) else explanations.REVIEW_TITLE)
        + ' ==='
    )
    sections = explanations.review(config)
    if storage_lines:
        sections = [*sections, (explanations.STORAGE_HEADING, storage_lines)]
    for heading, lines in sections:
        output(heading + ':')
        for line in lines:
            output('  - ' + line)


def switched(config, extras, answer, output):
    """The switches a typed line changes: {id: on} for its numbers ("2", "2 5"), or None
    when it is not numbers of the list (said)."""
    changes = {}
    now = explanations.states(config)
    for token in answer.replace(',', ' ').split():
        if not token.isdecimal() or not 1 <= int(token) <= len(extras):
            output(f'Type the number of a line (1 to {len(extras)}), press Enter to go on, or type no to stop.')
            return None
        extra = extras[int(token) - 1]
        if not explanations.offered(config, extra):
            names = ' or '.join(e['title'] for e in extras if e['id'] in extra['needs'])
            output(f'"{extra["title"]}" needs one of these switched on first: {names}.')
            return None
        changes[extra['id']] = not now[extra['id']]
    return changes


def save_choices(path, window, changes, settle=False):
    """The switches saved: through the setup window when there is one (it holds the
    settings and shows them), else straight to the settings file."""
    if window is not None:
        return window.choose(changes, start=settle) is True
    config = read_json(path)
    if not config:
        return False
    setup_gui.save_choices(path, config, changes, settle)
    return True


APPROVED, STOPPED = 'approved', 'stopped'
GO_ON = 'go on'


def first_answer(config, extras, question, asking):
    """One answer on the first page: GO_ON (Enter, or Continue in the window), STOPPED,
    the switches it changes, or None for an answer that is none of these (said)."""
    prompt, output, window, stdin = asking
    line = line_or_start(question, prompt, output, window, stdin)
    if line in (CONTINUED, STARTED):
        return GO_ON
    if line is BACK:
        return {}
    answer = '' if line is None else line.strip().lower()
    if line is None or answer in ('no', 'n'):
        return STOPPED
    if answer in ('', 'yes', 'y'):
        return GO_ON
    return switched(config, extras, answer, output)


def first_page(path, asking):
    """The first page, shown until the person goes on: a number switches that line on
    or off (saved at once) and shows the page again. False when setup is to stop."""
    _, output, window, _ = asking
    # On a server that is set up already (an update, gate run D) its apps are installed:
    # what this page holds is that nothing changes before the approval.
    first = (
        'Nothing is installed yet.'
        if not neko_common.server_exists(read_json(path) or {})
        else ('Nothing changes until you approve.')
    )
    question = (
        f'\n{first} Type the number of a line to switch it on or off, press Enter to go on, '
        'or type no to stop' + (' (or use the setup window)' if window is not None else '') + ': '
    )
    while True:
        config = read_json(path) or {}
        show_stage(window, 'apps')
        extras = show_first_page(config, output)
        answer = None
        while answer is None:
            answer = first_answer(config, extras, question, asking)
        if answer in (GO_ON, STOPPED):
            return answer == GO_ON
        if answer and not save_choices(path, window, answer):
            output('That could not be saved: the settings file cannot be read right now.')


def review_answer(path, config, question, asking):
    """One answer on the review: APPROVED, STOPPED, BACK, or None for an answer that
    is none of these (said). Approved is what was read."""
    prompt, output, window, stdin = asking
    line = line_or_start(question, prompt, output, window, stdin)
    if line is STARTED:
        return APPROVED
    if line is BACK or line is CONTINUED:
        # The window's Back (or a Continue left over from the first page): a button, not text.
        return BACK
    answer = '' if line is None else line.strip().lower()
    if line is None or answer in ('no', 'n'):
        return STOPPED
    if answer in ('back', 'b'):
        return BACK
    if answer in ('yes', 'y'):
        if explanations.states(read_json(path) or {}) != explanations.states(config):
            # A switch changed since the review was read (another window): shown first.
            output('A choice was changed in the setup window meanwhile. Here they are as they stand now.')
            return BACK
        # False when the window's button was pressed in the same moment: it saved them.
        save_choices(path, window, {}, settle=True)
        return APPROVED
    output('Type yes, back or no. Enter alone does nothing here.')
    return None


def review_page(path, chosen, asking):
    """The review and the one approval: yes in the terminal, or Install pressed in the
    setup window. APPROVED, STOPPED or BACK (to the first page)."""
    _, output, window, _ = asking
    config = read_json(path) or {}
    show_review(config, chosen['lines'], output)
    show_stage(window, 'review', chosen['lines'])
    install = explanations.APPLY if neko_common.server_exists(config) else explanations.INSTALL
    question = (
        f'\n{install}? Type yes to go on, back to change your choices, or no to stop'
        + (' (or use the setup window)' if window is not None else '')
        + ': '
    )
    answer = None
    while answer is None:
        answer = review_answer(path, config, question, asking)
    return answer


def show_stage(window, stage, storage_lines=()):
    if window is not None:
        window.show(stage, storage_lines)


NOTHING_AHEAD = {'settings': {}, 'places': {}, 'lines': []}


def choose_storage(path, output):
    """Setup's storage page, before the review (modules/storage.choose_ahead): asked in
    the terminal and in the setup window. Nothing is mounted or saved. None when setup
    cannot go on, with the reason said."""
    from modules import storage

    try:
        with setup_progress.watching_prompts():
            return storage.choose_ahead(read_json(path) or {}, output)
    except RuntimeError as problem:
        output(f'\nSetup stopped: {problem}')
        return None
    except (EOFError, KeyboardInterrupt):
        output('')
        return None


def keep_storage(path, chosen):
    """The storage page's choices written to the settings file, now that they are approved."""
    if not chosen['settings'] and not chosen['places'] and not chosen.get('installs') and not chosen.get('lost'):
        return
    from modules import storage

    config = read_json(path)
    if config:
        storage.keep_ahead(config, chosen)
        setup_gui.save_choices(path, config, {})


def wait_to_start(path, prompt, output, window, stdin=None, storage=None):
    """Setup's pages before anything is installed: the first page, the storage page
    (`storage`: choose_storage; nothing is asked without it), then the review and the
    one approval. Back on the review starts over at the first page. True when setup is
    to start: the switches are then saved as they stand, each with an answer, and the
    drives as chosen, so no step asks about them again."""
    asking = (prompt, output, window, stdin)
    while True:
        if not first_page(path, asking):
            return False
        show_stage(window, 'storage')
        chosen = storage(path, output) if storage else NOTHING_AHEAD
        if chosen is None:
            return False
        answer = review_page(path, chosen, asking)
        if answer == APPROVED:
            keep_storage(path, chosen)
            return True
        if answer == STOPPED:
            return False


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


def title(name):
    """A step's title (a step another one needs but with no title of its own, such as
    music_backup, in words: "Music backup")."""
    return STEPS.get(name) or setup_gui.step_title(name)


def show_steps(heading, names, states, fallback, output):
    if names:
        output('\n' + heading)
        for name in names:
            output(f'  - {title(name)}: {states[name].get("reason", fallback)}')


# What a blocked step's record names as the steps it waits for: the installer's own list,
# or (in a record from before 0.40.2) the step names in its reason.
WAITED_FOR = re.compile(r"'([a-z0-9_]+)'")


def causes(name, states, seen=()):
    """The steps that stop a blocked step: the first ones up its chain that are not
    blocked themselves (Docker, say, for the apps waiting on the recovery checkpoint)."""
    record = states.get(name) or {}
    if record.get('status') != 'blocked' or name in seen:
        return [name]
    named = record.get('waiting_for')
    named = named if isinstance(named, list) else WAITED_FOR.findall(str(record.get('reason', '')))
    # (A step finished since, in a later run, holds nothing up any more.)
    found = [
        cause
        for dep in named
        if (states.get(dep) or {}).get('status') not in ('success', 'unavailable')
        for cause in causes(dep, states, (*seen, name))
    ]
    return list(dict.fromkeys(found)) or [name]


def show_waiting(names, states, output):
    """The blocked steps, by what they wait for: one line for everything Docker holds up,
    say, in the steps' own titles (never a list of step names)."""
    groups = {}
    for name in names:
        groups.setdefault(tuple(causes(name, states)), []).append(title(name))
    if groups:
        output('\nWaiting for those (they run by themselves once those are done):')
    for waited, titles in groups.items():
        output('  - After ' + ' and '.join(title(n) for n in waited) + ': ' + ', '.join(titles))


def headline(complete, done, total, problems):
    """The finishing screen's title: ready, almost ready, or (fewer than half the steps
    done) how many things stop it, so the screen does not call 1 of 30 "almost ready"."""
    if complete:
        return 'NekoServer is ready!'
    if problems and done * 2 < total:
        return f'Setup stopped: {problems} ' + ('thing needs you' if problems == 1 else 'things need you')
    return 'NekoServer is almost ready'


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
        output(
            'Dashboard PIN:      '
            + (
                pin
                or 'not set: phones can only look, and apps can\'t be restarted from it. '
                'Run  sudo bash install.sh  again to choose one.'
            )
        )


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


NEXT_DONE = [
    '  Everything is set up. Backups and monthly updates run on their own;',
    '  the dashboard shows how they are doing.',
]
NEXT_PENDING = [
    '  1. Fix the first item under "What needs you" (its line says how).',
    '  2. Then run  sudo bash install.sh  again: it continues where it stopped and keeps everything done.',
]
# Every step done, but the first checkup and backup found something: there is no "What
# needs you" list to point at (the 0.47.10 round: the screen said to fix its first item).
NEXT_CHECKUP = [
    '  1. Look at the items under "First checkup and backup" above; the checklist on the dashboard explains each one.',
    '  2. Backups and monthly updates run on their own. Run  sudo bash install.sh  again any time to repeat the checkup.',
]


def finished(plan, by_status, checkup):
    """Nothing left to do: every step done (or not available here) and the first checkup
    and backup (first_checkup.run; None when it didn't run) found nothing."""
    return bool(plan) and not by_status['waiting'] and not by_status['broken'] and checkup is not False


def show_checkup(checkup, output):
    if checkup is False:
        output('\nFirst checkup and backup: something needs a look (listed above, under "First checkup and backup").')


# Steps that never count against "every step finished": the optional AI assistant, whose
# step turns anything that goes wrong (a download cut off, too little memory) into "needs
# a look". It is still listed with its reason; the first checkup, the exit code and the
# headline do not wait for it (as upgrade.works_on leaves it out).
OPTIONAL = frozenset((ASSISTANT,))


def planned(config, states, ran=()):
    """(the steps this installation is meant to run, each one's record). Judged against
    the template's full profile, not only the steps that left a state behind: missing or
    unreadable state must never read as "ready". A settings file from an earlier release
    may lack steps added since; the steps you took out stay out, your own stay in."""
    from config_checks import expected_steps

    expected = [n for n in expected_steps(config, read_json(ROOT / TEMPLATE)) if n in STEPS]
    plan = list(dict.fromkeys([*ran, *expected])) or [name for name in STEPS if name in states]
    records = {
        n: states.get(n)
        if isinstance(states.get(n), dict)
        else {'status': 'missing', 'reason': 'did not run (no record of this step)'}
        for n in plan
    }
    return plan, records


def only_optional_left(config, states, ran=()):
    """Every step is done (or not available here) except optional ones: the installer's
    exit code says "not everything finished", yet nothing that matters is left."""
    plan, records = planned(config, states, ran)
    left = {n for n in plan if records[n].get('status') not in ('success', 'unavailable')}
    return bool(plan) and bool(left) and left <= OPTIONAL


def sorted_steps(plan, states):
    """The steps by how they ended: done, waiting for you, not available here, did not
    finish (failed, stopped, or never ran), and waiting for another step."""
    by_status = {
        status: [n for n in plan if states[n].get('status') in group]
        for status, group in (
            ('done', ('success',)),
            ('waiting', ('needs_setup',)),
            ('unavailable', ('unavailable',)),
            ('blocked', ('blocked',)),
            ('broken', ('failed', 'running', 'missing')),
        )
    }
    sorted_out = {n for names in by_status.values() for n in names}
    by_status['broken'] += [n for n in plan if n not in sorted_out]
    return by_status


# ---- the finishing page's states (0.50.0, the wizard: P09) --------------------------------
# What is so for each app, kept apart: installed, signed in, opens, tried by a person;
# and for the server: a backup made, a restore tried. Nothing here sums them into one
# "all done" (finish_states.py judges; these are the words).
STATE_WORDS = {'done': 'done', 'failed': 'needs fixing', 'not_tested': 'not tried yet', 'waiting': 'waiting'}
APP_STATES = (('installed', 'Installed'), ('login', 'Signed in'), ('address', 'Opens'), ('tried', 'Tried by you'))
REASONS = {
    'step_did_not_finish': 'its part of setup did not finish',
    'step_waits_for_you': 'waiting for something only you can do',
    'container_missing': 'it was not installed',
    'container_not_running': 'it is installed but not running',
    'still_starting': 'it is still starting; look again in a minute',
    'docker_did_not_answer': 'NekoServer could not ask the program that runs the apps',
    'saved_login_refused': 'the app did not take the saved login',
    'account_step_not_finished': 'its account is not made yet',
    'no_login_kept': 'NekoServer keeps no login for this app',
    'not_installed_yet': 'comes after it is installed',
    'no_answer': 'it did not answer when NekoServer opened it',
    'not_asked': 'NekoServer has not opened it yet',
    'nobody_tried_yet': 'nobody has tried this yet',
    'empty_library': 'your library is empty: add a video or a song you own, or leave this test for later',
    'you_chose_later': 'you chose to try this later',
    'you_said_it_did_not_work': 'you said this did not work',
    'has_not_run_yet': 'this backup has not run yet',
    'did_not_finish': 'the last run did not finish',
    'left_something_out': 'the last run left something out',
    'no_backup_set_up': 'no backup is set up',
    'no_backup_to_restore_from': 'there is no backup to restore from yet',
    'running_now': 'it is running now',
}
TEST_TITLES = {
    'play_video': 'Play a video in Jellyfin',
    'play_song': 'Play a song in Navidrome',
    'phone_photo': 'A photo taken on the phone appears in Immich',
    'cloud_file': 'Open a file in Nextcloud',
    'tv_picture': 'The TV shows the picture',
    'tv_sound': 'Sound comes from the TV',
    'tv_controller': 'The controller works',
    'tv_steam': 'Steam Big Picture opens',
}
OUTCOMES = {
    'ready': 'NekoServer is ready.',
    'pending': 'Your apps are installed. Some setup is still waiting for you.',
    'attention': 'Setup needs attention.',
}


def state_said(found):
    """One state in words, with its reason when it is not done."""
    word = STATE_WORDS.get(found.get('state'), 'not known')
    reason = REASONS.get(found.get('reason'))
    return f'{word} ({reason})' if reason and found.get('state') != 'done' else word


def states_lines(summary):
    """The finishing page's states as lines: each app, the server's backups and
    restores, and the tests a person can still try (which never hold anything up)."""
    lines = ['', 'How things stand: ' + OUTCOMES.get(summary.get('outcome'), OUTCOMES['pending'])]
    for app in summary.get('apps', []):
        said = [f'{label}: {state_said(app["states"][key])}' for key, label in APP_STATES if key in app['states']]
        lines.append(f"  {app['name']}. " + '. '.join(said) + '.')
    server = summary.get('server', {})
    if server:
        lines.append(
            f"  Backups. Backup made: {state_said(server.get('backup', {}))}. "
            f"Restore tried: {state_said(server.get('restore', {}))}."
        )
    open_tests = [test for test in summary.get('tests', []) if test.get('state') != 'done']
    if open_tests:
        lines.append('You can try these whenever you like; nothing waits for them:')
        for test in open_tests:
            title = TEST_TITLES.get(test['id'], str(test['id']).replace('_', ' ').capitalize())
            lines.append(f'  - {title}: {state_said(test)}')
    return lines


def read_states(config, plan):
    """What finish_states finds on this server, or None when it cannot be read (the page
    then says nothing rather than guess)."""
    import finish_states

    try:
        # Nothing to ask where no app was ever installed (Docker's folder is not on the app
        # drive): the checks would only wait for programs that are not there.
        if not (data_folder(config) / 'docker-engine').is_dir():
            return None
        return finish_states.summary(finish_states.collect(config, plan))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return None


def finishing_screen(config, states, page=None, output=print, checkup=None, ran=(), summary=None):
    """The summary shown at the end. Returns True when nothing is left to do. `ran`: the
    steps the installer ran this time (its plan, with the steps the others need), so the
    count is the one the log and the window give. `summary`: what finish_states found
    (read_states), shown as each app's states; left out when it could not be read."""
    data = data_folder(config)
    plan, states = planned(config, states, ran)
    by_status = sorted_steps(plan, states)
    by_status['waiting'] += [n for n in by_status['blocked'] if causes(n, states) == [n]]  # waits for nothing known
    # An optional step that did not finish is listed on its own and stops nothing.
    optional = [n for n in by_status['broken'] + by_status['waiting'] if n in OPTIONAL]
    for group in ('broken', 'waiting'):
        by_status[group] = [n for n in by_status[group] if n not in OPTIONAL]
    blocked = [n for n in by_status['blocked'] if n not in by_status['waiting']]
    complete = finished(plan, {'waiting': by_status['waiting'], 'broken': by_status['broken'] + blocked}, checkup)
    total = len(plan) - len(by_status['unavailable'])
    line = '=' * 64
    output('\n' + line)
    output(
        '  ' + headline(complete, len(by_status['done']), total, len(by_status['broken']) + len(by_status['waiting']))
    )
    output(line)
    output(f'\nDone: {len(by_status["done"])} of {total} steps.')
    show_steps(
        'What needs you (fix these first; the steps after them then run by themselves):',
        by_status['broken'] + by_status['waiting'],
        states,
        'stopped before finishing',
        output,
    )
    show_waiting(blocked, states, output)
    show_steps('Needs a look (optional: everything else works without it):', optional, states, 'did not finish', output)
    show_steps('Not available on this system:', by_status['unavailable'], states, 'not available', output)
    show_checkup(checkup, output)
    for said in started_empty_lines():
        output('\n' + said)
    if summary:
        for text in states_lines(summary):
            output(text)
    show_end(config, data, plan, states, page, output)
    output('\nWhat next:')
    steps_done = not (by_status['broken'] or by_status['waiting'] or blocked)
    if complete:
        what_next = NEXT_DONE
    elif steps_done:
        what_next = NEXT_CHECKUP
    else:
        what_next = NEXT_PENDING
    for text in what_next:
        output(text)
    output('  If something goes wrong later:  sudo bash install.sh --recover')
    output(line + '\n')
    return complete


def show_end(config, data, plan, states, page, output):
    """Where everything is: the apps' addresses, the passwords, the phone page, the dashboard."""

    def waiting(step):
        # A step this run set out to do that has not worked yet: what it makes isn't there
        # (seen on a new PC whose drives were not formatted: the screen pointed at a
        # dashboard and a passwords command that did not exist).
        return step in plan and states.get(step, {}).get('status') != 'success'

    show_addresses(data, output)
    if not waiting('account_credentials'):
        output(
            '\nPasswords:          '
            + guided_setup.login_command({'data_mount': str(data)})
            + '  (an app\'s login, with a QR code)'
        )
    if page and Path(page).exists():
        output('Phone setup page:   ' + str(page) + '  (open it in a browser on this PC)')
    if not waiting('dashboard'):
        show_dashboard(config, output)
    # Asked for only where there is a player (modules/onboarding.witness_playback).
    if (
        'onboarding' in plan
        and states['onboarding'].get('status') == 'success'
        and 'jellyfin' in media_services(config)
        and not (data / 'reports/onboarding/playback.json').exists()
    ):
        output('\nPlayback verification pending: the apps are set up and their automatic checks')
        output('passed, but no real playback was tested yet. Play something once to be sure.')


# ---- NekoServer is already on this PC (0.50.0, the wizard: P00) --------------------------
# Shown only when something of a server, or saved answers, is found (install_state.py):
# what it is, and the choices that apply. Never on a PC with nothing on it.
EXISTING_TITLE = 'NekoServer was found on this PC. What would you like to do?'
JOBS_CUT_OFF = {
    'upgrade': 'An update of NekoServer was cut off part-way. It has to finish before anything else.',
    'drive_move': 'Moving NekoServer to another drive was cut off part-way. It has to finish before anything else.',
}


MISSING_SAID = {
    'data_ssd': 'The drive that held the apps is not connected.',
    'media_hdd': 'The drive that held the media files and the backups is not connected.',
}
MISSING_NEXT = (
    'If it is only unplugged, connect it and run setup again. If you replaced it, choose the new one: '
    'setup asks for it on the storage page.'
)


# The finishing page's line for a group set up again on the drive that replaced its lost
# one (modules/storage.settings_start_empty, gate run C17).
STARTED_EMPTY_SAID = (
    '{what}: set up again on the drive that replaced the lost one, with empty settings. To bring their settings '
    'back from a backup: sudo bash install.sh --recover, then "App settings, from a nightly backup".'
)


def started_empty_lines():
    """The groups this run set up again with empty settings, in words."""
    import drive_table

    return [STARTED_EMPTY_SAID.format(what=said['what']) for said in drive_table.STARTED_EMPTY.values()]


def found_said(found):
    """The lines under the title: what is on this PC. A drive of the settings that is not
    connected comes first (gate run C10: a replaced SSD was not noticed)."""
    missing = [MISSING_SAID[key] for key in found.get('missing') or () if key in MISSING_SAID]
    # A drive of a group's own (gate run C17): named by what it holds.
    missing += [f'The drive for {words} is not connected.' for words in (found.get('missing_groups') or {}).values()]
    if missing:
        missing.append(MISSING_NEXT)
    return missing + what_found(found)


def installed_found(found):
    """What setup says of a NekoServer that is set up here: its version, where its apps
    and files are (not when the drive mounted there is not connected), what still waits."""
    version = f" {found['version']}" if found.get('version') else ''
    shown = found.get('location') and 'data_ssd' not in (found.get('missing') or ())
    place = f" Its apps and files are on {found['location']}." if shown else ''
    lines = [f'NekoServer{version} is set up on this PC.{place}']
    waiting = found.get('waiting') or []
    if waiting:
        count = '1 thing still waits' if len(waiting) == 1 else f'{len(waiting)} things still wait'
        lines.append(count + ' for you: ' + ', '.join(title(step) for step in waiting) + '.')
    return lines


def what_found(found):
    """What kind of NekoServer is on this PC, in words."""
    kind, job = found['kind'], found.get('job') or {}
    if kind == 'unfinished' and job.get('kind') in JOBS_CUT_OFF:
        return [JOBS_CUT_OFF[job['kind']]]
    if kind == 'unfinished':
        where = f" It stopped at: {title(job['step'])}." if job.get('step') else ''
        return ['Setup was started here and stopped before it finished.' + where]
    if kind == 'installed':
        return installed_found(found)
    if kind == 'older_folder':
        return [
            f"An earlier copy of NekoServer ({found.get('settings_from') or 'an earlier release'}) on this PC still "
            'has your settings. Nothing is installed from this copy yet.'
        ]
    return ['Your answers from an earlier start of setup are saved here. Nothing has been installed yet.']


def choice_said(found, choice):
    """(what a choice is called, the line under it)."""
    job = found.get('job') or {}
    if choice == 'continue' and job.get('kind') in JOBS_CUT_OFF:
        return 'Finish what was cut off', 'Nothing else can be changed until this is finished.'
    if choice == 'continue':
        where = f' Goes on at "{title(job["step"])}".' if job.get('step') else ''
        return 'Continue unfinished setup', 'What is already done stays done.' + where
    if choice == 'update':
        return (
            'Review this NekoServer update',
            f"From {found.get('version')} to {found.get('this_version')}. You see what changes before anything "
            'is installed.',
        )
    if choice == 'change' and found['kind'] in ('settings_only', 'older_folder'):
        return (
            'Use these saved answers',
            'Shows them first; you can change any of them before anything is installed.',
        )
    if choice == 'change':
        return 'Change apps or settings', 'Shows your saved answers. You change only what you want; the rest stays.'
    return (
        'Restore from a backup',
        'Brings files or settings back from a backup. Nothing is restored before you approve it.',
    )


def existing_installation(found, prompt=input, output=print):
    """The page: what was found, and the choices that apply, by number. Returns the
    choice ('continue', 'update', 'change', 'restore'), or None when the person stops
    (no, Ctrl+D). Enter alone takes the first, which is said."""
    choices = found.get('choices') or []
    output('')
    output('=== ' + EXISTING_TITLE + ' ===')
    for line in found_said(found):
        output(line)
    if not choices:
        return 'change'
    for number, choice in enumerate(choices, 1):
        name, under = choice_said(found, choice)
        output(f'  {number}) {name}')
        output('       ' + under)
    first = choice_said(found, choices[0])[0]
    question = f'Type its number, or no to stop (Enter alone: 1, {first}): '
    for _ in range(3):
        try:
            answer = prompt(question).strip().lower()
        except (EOFError, KeyboardInterrupt):
            output('')
            return None
        if answer in ('no', 'n'):
            return None
        if not answer:
            return choices[0]
        if answer.isdecimal() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        output(f'Type a number from 1 to {len(choices)}, or no.')
    return None


def what_is_here(root, config, default_state):
    """install_state.found for this run, or None when the PC cannot be read (setup then
    goes on as on a new PC, and says nothing it does not know)."""
    import install_state

    try:
        return install_state.found(root=root, settings=config, default_state=default_state)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        return None


def settings_for_run(config, root, output):
    """(path, created) of the settings this run uses, or None when a file named with
    --config is missing."""
    if config:
        path = settings_path(config)
        if not path.is_file():
            output('Settings file not found: ' + str(path))
            return None
        return path, False
    if not (Path(root) / SETTINGS).exists():
        take_earlier_settings(root, sudo_account(), output)
    return prepare_settings(root, sudo_account())


def unfinished(states, plan):
    """The steps of the run a person can try again: the ones that failed or wait for
    the person. A step that only waits for another one runs with that one."""
    return [name for name in plan if (states.get(name) or {}).get('status') in ('failed', 'needs_setup')]


def settled(states, plan):
    """Every step of the run is done (or is not available on this PC)."""
    return all((states.get(name) or {}).get('status') in ('success', 'unavailable') for name in plan)


def retry_unfinished(execute, args, code, default_state, asked=None, output=print, most=3):
    """After the run, while setup still holds its lock: every step that did not finish,
    with its reason, the fix setup knows (known_fixes.py) and "Retry this step". A retry
    runs that step and the steps that waited for it, nothing that is done; then the list
    is shown again. Each step is retried at most `most` times here. Returns the run's exit
    code: 0 once every step is done."""
    import known_fixes

    tried = {}
    while code != 0:
        states = read_json(state_file(read_json(args.config), default_state))
        ran = setup_progress.last('plan')
        plan = ran['steps'] if ran else list(states)
        if settled(states, plan):
            return 0
        names = [name for name in unfinished(states, plan) if tried.get(name, 0) < most]
        if not names:
            break
        output('\nThese steps did not finish. Each can be tried again now:')
        rows = [
            {
                'answer': str(number),
                'title': title(name),
                'reason': states[name].get('reason', 'no reason recorded'),
                'fix': known_fixes.fix_for(states[name].get('reason')),
            }
            for number, name in enumerate(names, 1)
        ]
        answer = (asked or guided_setup.retry_question)(rows)
        if not answer:
            break
        name = names[int(answer) - 1]
        tried[name] = tried.get(name, 0) + 1
        # That step and the steps that waited for it; the run's code only says how those went.
        code = execute(argparse.Namespace(**{**vars(args), 'profile': None, 'retry_step': name})) or 1
        after = read_json(state_file(read_json(args.config), default_state))
        if tried[name] >= most and (after.get(name) or {}).get('status') != 'success':
            output(
                f'\n{title(name)} was tried {most} times and still does not finish. Run  sudo bash install.sh  '
                'again later for it; everything that is done stays done.'
            )
    return code


def after_setup(code, path, default_state, output, window):
    """The first checkup when everything finished, then the finishing screen (in the
    window too). The exit code: 0 only when every step is done."""
    config = read_json(path)
    states = read_json(state_file(config, default_state))
    ran = setup_progress.last('plan')
    steps = ran['steps'] if ran else ()
    # The optional assistant needing a look is not "a step did not finish".
    finished_all = code == 0 or (bool(config) and only_optional_left(config, states, steps))
    checkup = None
    if finished_all and config:
        # Every step finished: back up and check now rather than waiting for the night.
        import first_checkup

        try:
            checkup = first_checkup.run(config, output)
        except KeyboardInterrupt:
            # Ctrl+C, as the checkup says it may be stopped: the finishing screen still comes.
            output(
                '\nFirst checkup stopped. The backups run at their usual times; to run the checkup now, '
                'run  sudo bash install.sh  again.'
            )
    try:
        import phone_guide

        page = phone_guide.PAGE
    except ImportError:
        page = None
    lines = []

    def shown(line=''):
        lines.append(str(line))
        output(line)

    summary = read_states(config, planned(config, states, steps)[0])
    complete = finishing_screen(config, states, page, shown, checkup, steps, summary)
    if window is not None:
        # The window shows the same summary; give it a moment to fetch it.
        heading = next((text.strip() for text in lines[1:2]), '')  # under the first ruled line
        setup_progress.event(
            'finished', lines=lines, complete=complete, title=heading, page=window_finish_page(config, summary)
        )
        window.delivered.wait(3)
        keep_window_open(output)
    return 0 if complete and finished_all else 1


def window_finish_page(config, summary):
    """The finishing page for the setup window (finish_page.py); None when it cannot be
    made, and the window shows the terminal's summary instead."""
    try:
        import finish_page

        return finish_page.page(config, summary, PIN_FILE.exists())
    except (ImportError, OSError, RuntimeError, KeyError, TypeError, ValueError):
        return None


def keep_window_open(output, stdin=None):
    """The setup window's buttons (Show login, the answers to a test) are answered by
    setup itself, so setup stays until the person is done with the page: Enter in the
    terminal ends it. With nobody at the terminal it ends at once, as before."""
    stdin = sys.stdin if stdin is None else stdin
    try:
        if not stdin.isatty():
            return
        output('')
        output('The setup window stays open for its buttons. Press Enter here when you are done with it.')
        stdin.readline()
    except (OSError, ValueError, KeyboardInterrupt):
        return


def chosen_here(found, prompt, output):
    """The choice on the "NekoServer was found on this PC" page ('change' when it is not
    shown); None, said, when the person stops; 'restore', said: restoring is a route of
    its own, never a turn inside setup (install.sh --recover starts it)."""
    if not (found and found.get('show')):
        return 'change'
    choice = existing_installation(found, prompt, output)
    if choice is None:
        output('Nothing was changed. Run  sudo bash install.sh  when you are ready.')
    elif choice == 'restore':
        output('To restore from a backup, run:  sudo bash install.sh --recover')
        output('Nothing was changed.')
    return choice


def goes_straight_on(found, choice):
    """Setup goes on with the steps left, asking nothing first: "Continue unfinished setup"
    on a server whose last run finished with steps waiting for the person (the Tailscale
    sign-in, say). A run that was cut off goes the usual way: the steps after the cut have
    no state yet, and a cut-off update or drive move has a way of its own."""
    found = found or {}
    return choice == 'continue' and bool(found.get('waiting')) and not found.get('job')


def run_steps(execute, path, default_state, straight, asking):
    """The setup run itself, once it is approved (run): the steps, the retries of the
    ones that did not finish, then the finishing page. `straight`: only the steps left.
    `asking`: (prompt, output, the setup window or None). Returns the exit code."""
    prompt, output, window = asking
    if window is not None:
        window.freeze()
    default_state = checked_path(default_state)
    args = argparse.Namespace(
        all=False,
        profile='full',
        module=None,
        retry_failed=straight,
        apply=True,
        check=False,
        dry_run=False,
        config=str(path),
        state_dir=str(default_state),
    )
    with setup_progress.watching_prompts():
        code = execute(args)
        if prompt is builtins.input:
            code = retry_unfinished(execute, args, code, default_state, output=output)
    return after_setup(code, path, default_state, output, window)


def run(execute, default_state, prompt=input, output=print, root=ROOT, config=None):
    """`execute` is installer.execute; injected so this module stays testable.
    `config`: a settings file chosen with --config; otherwise my-config.json here."""
    if os.geteuid() != 0:
        output('Run it with sudo:  sudo bash install.sh')
        return 2
    # Something of NekoServer on this PC already: said first, with the choices that apply.
    found = what_is_here(root, config, default_state)
    choice = chosen_here(found, prompt, output)
    if choice in (None, 'restore'):
        return 0
    settings = settings_for_run(config, root, output)
    if settings is None:
        return 2
    path, created = settings
    # `config` is None on a PC with no settings yet (a first install).
    for line in welcome(config):
        output(line)
    output(
        '\nYour settings are in '
        + str(path)
        + (' (just created for you; you do not need to edit it).' if created else '.')
    )
    setup_progress.enable()
    window = setup_window(path, output) if prompt is builtins.input else None
    # "Continue unfinished setup" goes straight to the steps left (gate runs B7 and J03:
    # the first page, the drive questions and "Change any of these answers?" came first).
    straight = goes_straight_on(found, choice)
    if straight:
        output('Going on with what was left: ' + ', '.join(title(step) for step in found.get('waiting') or []) + '.')
    try:
        if straight or wait_to_start(
            path, prompt, output, window, storage=choose_storage if prompt is builtins.input else None
        ):
            exit_code = run_steps(execute, path, default_state, straight, (prompt, output, window))
        else:
            output(
                'No apps were set up. (The system update and the packages NekoServer needs are '
                'already installed; that is all.) Run  sudo bash install.sh  when you are ready.'
            )
            exit_code = 0
    finally:
        if window is not None:
            window.stop()
        setup_progress.disable()
    return exit_code
