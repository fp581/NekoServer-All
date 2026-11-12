"""One-time, interactive desktop handoffs. Never run a browser as root."""

import getpass
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

import host
from neko_common import VERSION, find_program, safe_path


def yes_no(question, prompt=None, output=None, how=' [yes/no]: '):
    """Ask until the answer is yes or no. Returns True or False, or None when the person
    pressed Ctrl+D (leave it for now). Ctrl+C stops setup (KeyboardInterrupt goes on),
    as it does everywhere else. The one yes/no question used across NekoServer."""
    prompt = prompt or input
    output = output or print
    while True:
        try:
            answer = prompt(question + how).strip().lower()
        except EOFError:
            output('')
            return None
        except KeyboardInterrupt:
            output('')
            raise
        if answer in ('yes', 'y'):
            return True
        if answer in ('no', 'n'):
            return False
        output('Please type yes or no.')


def interactive(ctx):
    return bool(
        getattr(ctx, 'apply', False) and ctx.config.get('guided_setup', {}).get('enabled', True) and sys.stdin.isatty()
    )


def desktop_command(config, argv):
    """Use the invoking desktop user and a minimal environment, including its bus."""
    name = os.environ.get('SUDO_USER')
    if name and name != 'root':
        user = pwd.getpwnam(name)
        if os.environ.get('SUDO_UID') and str(user.pw_uid) != os.environ['SUDO_UID']:
            raise RuntimeError('Desktop identity does not match sudo user')
    else:
        selected = config.get('tv', {}).get('user', 'auto')
        user = pwd.getpwnam(selected) if selected != 'auto' else pwd.getpwuid(config.get('media', {}).get('uid', 1000))
    home = safe_path(user.pw_dir)
    if (
        user.pw_uid < 1000
        or user.pw_name == 'root'
        or user.pw_shell.endswith(('nologin', 'false'))
        or not home.is_dir()
        or home.stat().st_uid != user.pw_uid
    ):
        raise RuntimeError('An existing, logged-in non-root desktop user is required')
    runtime = safe_path(Path('/run/user') / str(user.pw_uid))
    if not runtime.is_dir() or runtime.stat().st_uid != user.pw_uid:
        raise RuntimeError('No active desktop runtime for the selected user')
    env = [
        'HOME=' + str(home),
        'USER=' + user.pw_name,
        'LOGNAME=' + user.pw_name,
        'PATH=/usr/local/bin:/usr/bin:/bin',
        'XDG_RUNTIME_DIR=' + str(runtime),
    ]
    bus = runtime / 'bus'
    if bus.exists():
        env.append('DBUS_SESSION_BUS_ADDRESS=unix:path=' + str(bus))
    display = os.environ.get('DISPLAY', '')
    wayland = os.environ.get('WAYLAND_DISPLAY', '')
    if re.fullmatch(r':\d+(?:\.\d+)?', display):
        env.append('DISPLAY=' + display)
    if re.fullmatch(r'wayland-\d+', wayland) and (runtime / wayland).exists():
        env.append('WAYLAND_DISPLAY=' + wayland)
    elif not wayland:
        # sudo may strip WAYLAND_DISPLAY; use an actual socket in this user's runtime.
        sockets = sorted(p for p in runtime.glob('wayland-*') if re.fullmatch(r'wayland-\d+', p.name) and p.is_socket())
        if len(sockets) == 1:
            env.append('WAYLAND_DISPLAY=' + sockets[0].name)
    auth = os.environ.get('XAUTHORITY', '')
    if auth:
        candidate = safe_path(auth)
        if (
            candidate.is_file()
            and candidate.stat().st_uid == user.pw_uid
            and (home in candidate.parents or runtime in candidate.parents)
        ):
            env.append('XAUTHORITY=' + str(candidate))
    return ['runuser', '-u', user.pw_name, '--', 'env', '-i', *env, *argv]


def valid_url(url):
    try:
        parsed = urlsplit(url)
        return (
            isinstance(url, str)
            and len(url) < 4096
            and not any(ord(ch) < 33 or ord(ch) == 127 for ch in url)
            and not parsed.username
            and not parsed.password
            and bool(parsed.hostname)
            and (
                parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost'))
            )
        )
    except (TypeError, ValueError):
        return False


PAGE_ROOT = Path('/usr/local/share/nekoserver-pages')


def valid_page(path, root=None):
    """A local page NekoServer wrote itself: a regular file directly inside PAGE_ROOT."""
    root = Path(root or PAGE_ROOT)
    try:
        path = Path(path)
        return (
            path.is_absolute()
            and path.parent == root
            and path.suffix == '.html'
            and not path.is_symlink()
            and not root.is_symlink()
            and path.is_file()
        )
    except (TypeError, ValueError, OSError):
        return False


# GNOME's settings window, which also has the Bluetooth page.
SETTINGS_APP = '/usr/bin/gnome-control-center'


def launch(ctx, target, kind='url'):
    """Targets are passed as argv, never as shell text. No GUI secrets in logs."""
    if not interactive(ctx):
        return False
    try:
        desktop_command(ctx.config, ['/usr/bin/true'])
    except (OSError, RuntimeError, KeyError, ValueError):
        return False
    if kind == 'page':
        if not valid_page(target):
            raise ValueError('Unsupported local page')
        target = Path(target).as_uri()
        kind = 'url'
    elif kind == 'url' and not valid_url(target):
        raise ValueError('Unsupported browser URL')
    if kind == 'url':
        # xdg-open opens the browser you already use; a browser is never installed here.
        if not shutil.which('xdg-open') and os.geteuid() == 0 and host.supported():
            try:
                host.current().install(['xdg-utils'])
            except (OSError, subprocess.SubprocessError, RuntimeError):
                return False
        if shutil.which('xdg-open'):
            argv = ['/usr/bin/xdg-open', target]
        elif shutil.which('firefox'):
            argv = ['/usr/bin/firefox', '--new-window', target]
        else:
            return False
    else:
        # Steam lives in /usr/games on Debian and Ubuntu, in /usr/bin elsewhere.
        commands = {
            'steam': [find_program('steam') or '/usr/bin/steam', '-gamepadui'],
            'bluetooth': ['/usr/bin/blueman-manager'],
            'settings': [SETTINGS_APP, 'display'],
        }
        if kind not in commands:
            raise ValueError('Unsupported desktop application')
        argv = commands[kind]
        if kind == 'bluetooth' and Path(SETTINGS_APP).is_file():
            argv = [SETTINGS_APP, 'bluetooth']
        if not Path(argv[0]).is_file():
            return False
    return launch_quietly(ctx.config, argv)


def launch_quietly(config, argv):
    """Start a program on the desktop, detached and silent (argv, never shell text). True
    when it succeeds within two seconds or is still running then (a long-lived app was
    launched; any confirmation is still asked for); False when it failed or could not start."""
    try:
        command = desktop_command(config, argv)
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd='/',
        )
        try:
            return child.wait(timeout=2) == 0
        except subprocess.TimeoutExpired:
            return True
    except (OSError, RuntimeError, KeyError, ValueError):
        return False


KEYS = 'Ctrl+D leaves it pending and setup goes on; Ctrl+C stops setup.'
SKIPPABLE = 'Ctrl+C or Ctrl+D skips it and setup goes on.'  # a step that catches Ctrl+C itself (playback)


def show_step(output, title, instructions, keys=KEYS):
    output('\n=== ' + title + ' ===')
    for number, instruction in enumerate(instructions, 1):
        output(str(number) + '. ' + instruction)
    output('Return to the terminal afterward. No reopens this step; ' + keys)


def prepared_targets(targets, prepare, output):
    """The step's targets and what `prepare` adds to them (nothing when it fails, said)."""
    current = list(targets)
    if prepare:
        try:
            current.extend(prepare() or ())
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            output('The launch preparation did not complete. Follow the instructions manually or answer no to retry.')
    return current


def open_targets(ctx, targets, output):
    for kind, target in targets:
        opened = launch(ctx, target, kind)
        if kind in ('url', 'page'):
            # Print only to the user's console, not the durable installer logger.
            output(('Opened: ' if opened else 'Open this address manually: ') + target)
        elif not opened:
            output('Automatic launch unavailable. Open ' + kind + ' from your desktop menu.')


def check_passed(verify, output):
    """Whether the step's own check passes (no check: yes); a failure is said with its reason."""
    if not verify:
        return True
    why = ''
    try:
        passed = verify()
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        passed, why = False, str(exc)
    if not passed:
        output(
            'The automatic check has not passed' + (': ' + why if why else '') + '. Review the step, then try again.'
        )
    return passed


# Moves to the top, clears the screen and, in most terminals, the lines scrolled out of view too.
CLEAR_SCREEN = '\033[H\033[2J\033[3J'


def saved_logins(config, apps):
    """[{'id', 'title', 'username'}] for the accounts among `apps` (account_store's names)
    that have a saved login. Empty when the logins cannot be read (no SSD yet)."""
    import account_store

    found = []
    for app in apps:
        if app not in account_store.APPS:
            continue  # an app with no login of its own here (the dashboard, say)
        try:
            entry = account_store.get(config, app)
        except (OSError, RuntimeError, ValueError, KeyError):
            return []
        if entry:
            found.append({'id': app, 'title': account_store.APPS[app][0], 'username': entry['username']})
    return found


def qr_image(text):
    """`text` as a QR code the setup window can show (an SVG picture as a data: address),
    or '' without qrencode. The text goes in on qrencode's input, never on its command
    line, where other accounts on this PC could read it."""
    import base64

    if not shutil.which('qrencode'):
        return ''
    try:
        made = subprocess.run(
            ['qrencode', '-t', 'SVG', '-m', '2', '-o', '-'],
            input=text,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    if made.returncode or '<svg' not in made.stdout:
        return ''
    picture = made.stdout[made.stdout.index('<svg') :]
    return 'data:image/svg+xml;base64,' + base64.b64encode(picture.encode()).decode()


def terminal_qr(text):
    """`text` as a QR code drawn in the terminal, or '' without qrencode (its input, not
    its command line, as qr_image)."""
    if not shutil.which('qrencode'):
        return ''
    try:
        made = subprocess.run(
            ['qrencode', '-t', 'ANSIUTF8', '-m', '2'],
            input=text,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    return made.stdout if made.returncode == 0 else ''


def show_logins(config, apps, prompt=None, output=None, screen=None, wait=None, qr=terminal_qr):
    """At a step that asks the person to sign in to apps whose logins setup made or saved
    (0.50.0): says that they are saved, and offers to show one. In the terminal the
    username, the password and a QR code of it go to the screen alone (`screen`), until
    Enter, and the screen is cleared after; nothing of it reaches the log, a report or
    the window's events. In the setup window the same step is a page with a "Show login"
    button for each app, which fetches the login when pressed (setup_gui.Window.login).
    Does nothing when none of the apps has a saved login."""
    import account_store
    import setup_progress

    logins = saved_logins(config, apps)
    if not logins:
        return
    prompt, output = prompt or input, output or print
    screen, wait = screen or setup_progress.screen_only, wait or setup_progress.ask_terminal
    output('Your login for this step is saved on this server (never in a log):')
    for number, login in enumerate(logins, 1):
        output(f"  {number}) {login['title']}: {login['username']}")
    with setup_progress.page('logins', {'answer': ''}, apps=logins):
        setup_progress.page_asks('answer')
        while True:
            answer = prompt('Show a login now? Type its number (Enter alone: go on): ').strip()
            if not answer:
                return
            if not (answer.isdigit() and 1 <= int(answer) <= len(logins)):
                output(f'Type a number from 1 to {len(logins)}, or press Enter alone to go on.')
                continue
            login = logins[int(answer) - 1]
            entry = account_store.get(config, login['id'])
            code = qr(entry['password'])
            screen(
                f"\n  {login['title']}\n  Username: {entry['username']}\n  Password: {entry['password']}\n"
                + (
                    f'\n  Or scan this code with the phone camera: it shows the password as text.\n{code}'
                    if code
                    else ''
                )
                + '  Capital letters matter: type it exactly as shown.\n'
            )
            try:
                wait('  Press Enter to hide it (the screen is cleared): ')
            finally:
                screen(CLEAR_SCREEN)
            output('The login is hidden again.')


def checkpoint(
    ctx,
    title,
    instructions,
    targets=(),
    verify=None,
    prepare=None,
    prompt=None,
    output=None,
    keys=KEYS,
    logins=(),
    question=None,
    ask=None,
    values=None,
):
    """No repeats instructions/opening; yes must also pass any supplied verifier. `logins`:
    the saved logins (account_store's names) this step asks the person to sign in with,
    offered to be shown before the question (show_logins). `question`: what yes means
    here, in the step's own words ("Does the Tailscale app on the phone show
    Connected?"); every step says its own, so no step ends in a question that could be
    any step's.

    EOF (Ctrl+D) leaves this step pending; Ctrl+C stops setup. Background/read-only runs never launch or
    prompt. Callers retain machine and witnessed evidence as separate concepts.
    """
    import setup_progress

    if not interactive(ctx):
        return False
    prompt = prompt or input
    output = output or print
    try:
        while True:
            show_step(output, title, instructions, keys)
            open_targets(ctx, prepared_targets(targets, prepare, output), output)
            show_logins(ctx.config, logins, prompt, output)
            if ask:
                # The step's own question, in the words of questions.py: the window may
                # leave it for later ("Skip for now", as Ctrl+D here).
                import questions

                answer = questions.yes_no(ask, prompt, output, leave=True, **(values or {}))
            else:
                with setup_progress.page(CONFIRM_STEP, {'answer': ''}):
                    setup_progress.page_asks('answer')
                    answer = yes_no(question or f'Is this done: {title}?', prompt, output)
            if answer is None:
                raise EOFError
            if not answer:
                output('Reopening this step and repeating its instructions.')
            elif check_passed(verify, output):
                return True
    except EOFError:
        output('\nStep remains pending. Reapply this profile to resume; saved choices are kept.')
        return False


# The page a step's own yes/no is asked on (the window: a question that can be left).
CONFIRM_STEP = 'confirm_step'


def part(ctx, title, steps, question, done=(), intro=(), targets=(), prompt=None, output=None, status=()):
    """Several steps about one thing (setting up the phone, the TV), shown as one part
    with ONE question at the end, instead of a yes/no after every step.
    `steps`: [{'id', 'title', 'lines', 'logins'}]; `done`: the ids confirmed in an
    earlier run, which are listed as done and not asked for again.
    The terminal lists every step in order and asks `question` once: yes confirms all of
    them. The setup window gives each step a sliding "yes" of its own and one Continue:
    with every one on yes the part is confirmed; with some still off, those stay for the
    next run and the ones marked are remembered. No (or Ctrl+D) keeps the part for the
    next run. Returns the ids confirmed, `done` among them. `status`: rows setup
    checked itself ([{'status', 'title', 'reason', 'next', 'shows'}]), shown above the
    steps in the window (the terminal lists them in `intro`)."""
    import setup_progress

    confirmed = {step['id'] for step in steps if step['id'] in done}
    todo = [step for step in steps if step['id'] not in confirmed]
    if not todo or not interactive(ctx):
        return confirmed
    prompt = prompt or input
    output = output or print
    part_said(title, intro, steps, confirmed, output)
    open_targets(ctx, list(targets), output)
    show_logins(ctx.config, [login for step in todo for login in step.get('logins', ())], prompt, output)
    shown = [window_step(step, step['id'] in confirmed) for step in steps]
    import questions

    asked = questions.words(question)  # the part's one question, by its key in questions.py
    with setup_progress.page(
        'part',
        {'answer': '', 'done': ''},
        title=title,
        question=asked['title'],
        hint=asked['hint'],
        steps=shown,
        status=list(status),
    ):
        setup_progress.page_asks('answer')
        answer = yes_no('\n' + asked['title'], prompt, output, how='\n' + asked['how'] + ' ')
        marked = setup_progress.page_answer('done')  # the window's switches, when it answered
    confirmed |= part_confirmed(todo, answer, marked)
    left = [step['title'] for step in steps if step['id'] not in confirmed]
    if left:
        output('Kept for the next run of setup. Still to do: ' + '; '.join(left) + '.')
    return confirmed


def window_step(step, done):
    """A step of a part as the setup window shows it. `step['window']` (optional): its
    own short lines there, an address shown with its QR code, and the saved login a
    "Show login" button fetches; without it the window lists the terminal's lines."""
    own = step.get('window') or {}
    shown = {'id': step['id'], 'title': step['title'], 'lines': list(own.get('lines', step['lines'])), 'done': done}
    if own.get('address'):
        shown['address'] = own['address']
        shown['qr'] = qr_image(own['address'])
    if own.get('login'):
        shown['login'] = own['login']
    return shown


def part_said(title, intro, steps, confirmed, output):
    """A part as the terminal lists it: its title, what setup found, then every step in
    order with its lines; one confirmed in an earlier run is only named."""
    output('\n=== ' + title + ' ===')
    for line in intro:
        output(line)
    for number, step in enumerate(steps, 1):
        mark = '  (done last time)' if step['id'] in confirmed else ''
        output(f"\nStep {number} of {len(steps)}: {step['title']}{mark}")
        for line in () if mark else step['lines']:
            output('  - ' + line)
    output('\nDo the steps in order, then come back here. ' + KEYS)


def part_confirmed(todo, answer, marked):
    """The steps of a part confirmed by its one answer: all of them with yes; with
    anything else, the ones the setup window's switches marked (when it answered)."""
    if answer:
        return {step['id'] for step in todo}
    if marked and marked[0] == 'answer':
        return {step['id'] for step in todo if step['id'] in str(marked[1]).split()}
    return set()


def typed_or_left(ask, question):
    """An answer as typed, in small letters; None when the question was left (Ctrl+D)."""
    try:
        return ask(question).strip().lower()
    except EOFError:
        return None


def retry_answer(answer, one, answers, leave):
    """What a typed answer chooses on the retry page: "yes" for the one step, the number
    of a step in the list, `leave`; None for an answer that is none of these."""
    if one and answer in ('y', 'yes'):
        return 'yes'
    if answer in answers and not one:
        return answer
    if answer in ('', 'n', 'no', leave):
        return leave
    return None


def retry_question(rows, leave='', ask=None, output=None):
    """Steps that did not finish, each with its reason, the fix setup knows for it
    (known_fixes.py) and a way to try it again. `rows`: [{'answer', 'title', 'reason',
    'fix'}], where 'answer' is what choosing that step returns: "yes" for the one step
    that just failed, its number in the list at the end of a run. Returns the answer
    chosen, or `leave` ("no", or '' for Enter alone) when none is; Ctrl+D leaves too.
    In the setup window this is one page: each step with a "Retry this step" button."""
    import known_fixes
    import setup_progress

    ask, output = ask or input, output or print
    one = len(rows) == 1 and rows[0]['answer'] == 'yes'
    for row in rows:
        output(('' if one else f"  {row['answer']}) ") + f"{row['title']} did not finish: {row['reason']}")
        for line in known_fixes.lines(row['fix']) if row.get('fix') else ():
            output(line)
    question = 'Retry this step now? [yes/no]: ' if one else 'Retry one now? Type its number (Enter alone: finish): '
    answers = {row['answer'] for row in rows}
    with setup_progress.page('retry', {'answer': ''}, steps=rows, leave=leave):
        setup_progress.page_asks('answer')
        for _ in range(3):
            answer = typed_or_left(ask, question)
            if answer is None:
                return leave
            chosen = retry_answer(answer, one, answers, leave)
            if chosen is not None:
                return chosen
            output('Type yes or no.' if one else 'Type one of the numbers above, or press Enter alone to finish.')
    return leave


def new_password(what, ask=None, output=print, tries=3):
    """A password chosen now and typed twice, hidden. In the setup window the two are one
    page (two hidden fields that must match before they are sent); in the terminal they
    are asked one after the other. Returns it; None when the two differed (or were
    empty) `tries` times, or the question was left (Ctrl+D, the window's Skip for now).
    It is never written anywhere: the caller hands it on."""
    import setup_progress

    ask = ask or getpass.getpass
    with setup_progress.password_page(what):
        for attempt in range(tries):
            try:
                first = setup_progress.page_field('password', ask, f'{what}: ')
                again = setup_progress.page_field('again', ask, 'The same password again: ')
            except EOFError:
                return None
            if first and first == again:
                return first
            if attempt < tries - 1:
                output('The two did not match (or were empty). Try again.')
    return None


def private_entry(question, valid, hint, ask=None, output=print, attempts=3):
    """A private value typed without showing it (a URL or token). A typo is explained and
    asked again, up to `attempts` times; blank, or still not valid, returns '' (the step
    waits for you instead of failing). A trailing "/" from copying is dropped."""
    ask = ask or getpass.getpass
    for _ in range(attempts):
        try:
            value = ask(question).strip().rstrip('/')
        except EOFError:
            return ''
        if not value or valid(value):
            return value
        output(hint)
    return ''


def save_settings(ctx):
    if hasattr(ctx, 'persist_config'):
        ctx.persist_config()


def login_command(config, app='<app>'):
    """The command that shows an app's login with a QR code (account-setup.py show), from
    the copy of this release kept on the SSD: it stays when this folder is updated or
    removed."""
    data = str(config.get('data_mount') or '/srv/nekoserver').rstrip('/')
    return f'sudo python3 {data}/installer/releases/v{VERSION}/account-setup.py show {app}'


def wanted(ctx, section, question, explain=(), prompt=None, output=None):
    """Ask once whether an optional extra is wanted and save the answer.

    Returns True/False, or None when nobody is there to ask. The answer is saved as
    "wanted" under `section` in the settings file; edit it there to change your mind.
    """
    options = ctx.config.setdefault(section, {})
    if isinstance(options.get('wanted'), bool):
        return options['wanted']
    if not interactive(ctx):
        return None
    prompt = prompt or input
    output = output or print
    output('\n=== Optional: ' + question + ' ===')
    for line in explain:
        output(line)
    answer = yes_no(question, prompt, output)
    if answer is None:
        return None
    options['wanted'] = answer
    save_settings(ctx)
    if not options['wanted']:
        output(
            'Skipped. To set it up later, change "wanted" to true under "'
            + section
            + '" in my-config.json and run  sudo bash install.sh  again.'
        )
    return options['wanted']


def optional_kept(ctx, asked, prompt=None, output=None):
    """Setup run again: shows the last answer to every optional question (the backup
    times, the download sources, the indexers, the playback test) and asks once in the
    run whether to ask them again. Returns the questions in `asked` not to ask: none
    when no answer was kept yet (the first run, or 0.47.8 and before) or on yes; the
    ones with a kept answer on no (Ctrl+D counts as no). A question with no kept answer
    is asked either way. The decision is kept on `ctx` for the later steps of the run."""
    from neko_common import OPTIONAL_QUESTIONS, optional_answers

    kept = optional_answers(ctx.config['data_mount'])
    shown = [question for question in asked if question in kept]
    if not shown:
        return []
    again = getattr(ctx, 'ask_optional_again', None)
    if again is None:
        prompt = prompt or input
        output = output or print
        output('')
        output('Last time, the optional questions were answered this way:')
        for question in OPTIONAL_QUESTIONS:
            if question in kept:
                output(f'  {OPTIONAL_QUESTIONS[question]}: {answer_shown(ctx, question, kept[question])}')
        import questions

        again = bool(questions.yes_no('ask_again', prompt, output))
        output('')
        ctx.log('Optional questions: ' + ('asked again' if again else 'kept as answered last time'))
        ctx.ask_optional_again = again
    return [] if again else shown


# The media apps, asked group by group (the user, 2026-11-11): what each adds, in plain
# words. The sizes are measured on a fresh install with all three (TESTING.md, the 0.50.0 gate).
MEDIA_QUESTION = 'Which media apps to install?'
MEDIA_EXPLAINED = (
    'The media apps come in groups, and each can be left out: answer yes or no for each one.',
    'All three together take about 6 GB on the SSD (downloaded once) and about 2 GB of memory when idle.',
    'Nextcloud, the dashboard, the backups, Tailscale, the shared folders, the alerts and the AI assistant',
    'are set up either way. Answering no later never deletes anything: the apps of that group are stopped,',
    'their settings, data and media are kept, and yes brings them back.',
)
GROUP_EXPLAINED = {
    'photos': ('', 'Photos: Immich, your own photo backup and gallery, with its archive and its weekly USB copy.'),
    'movies': (
        '',
        'Movies and shows: Jellyfin to watch them, Seerr to ask for new ones, and Sonarr, Radarr and Prowlarr',
        'with qBittorrent, which find and download them; with video on the graphics chip.',
    ),
    'music': (
        '',
        'Music: Navidrome, your music player, with the music backup, and Lidarr with Prowlarr and qBittorrent,',
        'which find and download music.',
    ),
}


def settle_no_with_apps(ctx):
    """The answer no (from the first page, a question or the settings file) on a server
    that already has media apps: said once, here, with what happens to them. The apps
    whose group is now off are stopped by the media steps (media.left_out, the photo
    steps' switch_off); their settings, your photos, music and films and every backup
    stay where they are, and yes brings them back. Returns the apps found."""
    from neko_common import APPS, app_wanted, media_apps_installed

    # The apps this server has whose group is now off (an app of two groups as soon as
    # one of them is).
    installed = [app for app in media_apps_installed(ctx.config) if not app_wanted(ctx.config, app)]
    if not installed:
        return []
    names = ', '.join(APPS[app][0] for app in installed)
    ctx.log(
        f'Media apps: you switched off a group this server already has ({names}). They are stopped in '
        'this run; their settings, your photos, music and films and every backup stay where they are. '
        'Nothing is deleted, and answering yes at a later run brings them back.'
    )
    return installed


def choose_media_apps(ctx, prompt=None, output=None):
    """The media question asked (asked_media_apps), then what no means on a server that
    already has the apps (settle_no_with_apps)."""
    asked_media_apps(ctx, prompt, output)
    settle_no_with_apps(ctx)


def asked_media_apps(ctx, prompt=None, output=None):
    """Ask which media groups to install (Photos, Movies and shows, Music: a yes or no
    each) and save the answers under "media_apps". Setup's first page
    (wizard.wait_to_start, the setup window) saves every one of them, so this asks only
    when setup runs without that page (installer.py itself), at the drives step, before
    the drives are chosen. A group left alone (Ctrl+D, or nobody at the terminal) is on
    and not saved, so it is asked again next time."""
    from neko_common import GROUP_NAMES, MEDIA_GROUPS, media_group_chosen

    options = checked_media_options(ctx.config)
    if not interactive(ctx):
        return
    # A group chosen already (on setup's first page, or by hand in the settings) is not
    # asked: only setup run without that page (installer.py itself) gets here with one
    # open. Finding and downloading comes with its group; its switch is on the first page.
    to_ask = [group for group in MEDIA_GROUPS if not media_group_chosen(ctx.config, group)]
    if not to_ask:
        return
    prompt = prompt or input
    output = output or print
    output('\n=== Optional: ' + MEDIA_QUESTION + ' ===')
    for line in MEDIA_EXPLAINED:
        output(line)
    names = GROUP_NAMES
    answered = groups_answered(to_ask, prompt, output)
    if not answered:
        return
    options.update(answered)
    if all(isinstance(options.get(group), bool) for group in MEDIA_GROUPS):
        options.pop('wanted', None)  # every group has its own answer: the one yes/no decides nothing more
    save_settings(ctx)
    said = ', '.join(f"{names[group]}: {'yes' if on else 'no, left out'}" for group, on in answered.items())
    ctx.log('Media apps: ' + said)


def checked_media_options(config):
    """The "media_apps" section of the settings (made when it is not there), as far as a
    settings file can be trusted: an object whose answers are true or false."""
    from neko_common import FINDING, MEDIA_GROUPS, MEDIA_SECTION

    options = config.setdefault(MEDIA_SECTION, {})
    if not isinstance(options, dict):
        raise RuntimeError('"media_apps" in my-config.json must be an object, such as {"photos": true}')
    for key in ('wanted', *MEDIA_GROUPS, *FINDING.values()):
        if key in options and type(options[key]) is not bool:
            raise RuntimeError(f'"{key}" under "media_apps" must be true or false')
    return options


def groups_answered(to_ask, prompt, output):
    """{group: yes or no} for the groups asked about now; one left alone (Ctrl+D) is not
    among them: it is set up, and asked about again next time."""
    import questions
    from neko_common import GROUP_NAMES

    answered = {}
    for group in to_ask:
        about = ' '.join(line for line in GROUP_EXPLAINED[group] if line)
        answer = questions.yes_no('install_group', prompt, output, group=GROUP_NAMES[group], about=about)
        if answer is None:
            output('Left for now: it is set up (yes), and this is asked again next time.')
            continue
        answered[group] = answer
    return answered


def remember_media_apps(ctx):
    """The media answer kept with the other optional answers, once the SSD that keeps
    them is mounted (never before: a file written there first would sit on the system
    disk under the SSD's folder)."""
    from neko_common import MEDIA_GROUPS, MEDIA_SECTION, media_group, media_group_chosen, remember_answer

    if any(media_group_chosen(ctx.config, group) for group in MEDIA_GROUPS):
        on = [group for group in MEDIA_GROUPS if media_group(ctx.config, group)]
        remember_answer(ctx.config['data_mount'], MEDIA_SECTION, ', '.join(on) if on else 'none')


def answer_shown(ctx, question, kept):
    """A kept answer as the rerun lists it: the backup times as the settings hold them now
    (the file may have been made again since, with other times), any other as given."""
    from neko_common import BACKUP_TIMES, backup_times_said

    return backup_times_said(ctx.config) if question == BACKUP_TIMES else kept


def ask_choice(question, default, valid, hint, prompt=input, output=print, attempts=3):
    """One short answer typed at `question` (which says the default): Enter, or Ctrl+D,
    keeps `default`; an answer `valid` refuses is explained with `hint` and asked again,
    up to `attempts` times, after which the default stands and the output says so."""
    for _ in range(attempts):
        try:
            answer = prompt(question).strip()
        except EOFError:
            output('')
            return default
        if not answer:
            return default
        if valid(answer):
            return answer
        output(hint)
    output(f'Kept as {default}.')
    return default


def ask_on_page(key, question, default, valid, hint, prompt=input, output=print, normalise=None):
    """ask_choice for one field of a page the setup window shows whole (setup_progress.page):
    when the window already sent this field, that answer is used (checked like a typed
    one) and the terminal is not asked; the window's "Skip for now" keeps the default.
    `normalise` puts an accepted answer in the form the page knows ("sat" as "Sat") before
    it is recorded for the page and returned."""
    import setup_progress

    normalise = normalise or (lambda answer: answer)
    given = setup_progress.page_answer(key)
    if given is not None:
        kind, value = given
        if kind == 'skip':
            return default
        value = value.strip()
        if not value:
            return default  # left empty in the window: what Enter alone is in the terminal
        if valid(value):
            output(f'{question}{value}  (from the setup window)')
            value = normalise(value)
            setup_progress.page_set(key, value)
            return value
        output(hint)
    setup_progress.page_asks(key)
    answer = normalise(ask_choice(question, default, valid, hint, prompt, output))
    setup_progress.page_set(key, answer)
    return answer


def revisits_every_job(ctx):
    """True when this run writes every scheduled job's timer again: a full setup run, or
    one whose plan has every timer-writing step installed so far (ctx.previous: each step
    as the last run left it). A run of one step (install.sh --module host_backup,
    --profile, --retry-failed) reaches the drives step too, since nearly every step needs
    it, but would rewrite only its own timer at new times and leave the rest at the old."""
    plan, previous = getattr(ctx, 'plan', None), getattr(ctx, 'previous', None)
    if not isinstance(plan, (list, tuple)) or not isinstance(previous, dict):
        return True
    from modules.storage import JOB_STEPS

    installed = {step for step in JOB_STEPS if previous.get(step, {}).get('status') == 'success'}
    return installed <= set(plan)


def choose_backup_times(ctx, prompt=None, output=None):
    """Ask when the backups run (the daily jobs, the weekly USB copy, the monthly check)
    and save the answers as "backup_times" in the settings file; every timer is written
    from them (neko_common.job_times). Enter keeps the time shown: the server's current
    times, so a server set up before 0.48.0 keeps its schedule (its settings untouched
    when every answer is Enter and its other jobs had times of their own). Asked once; a run after
    that shows the answer and asks again only on request (optional_kept). Setup without
    a person at the terminal (an upgrade), or a run that would not write every timer
    again (revisits_every_job), changes nothing. The setup window shows the
    three as one page (setup_progress.page) and may answer them together."""
    import setup_progress
    from neko_common import BACKUP_TIMES, WEEKDAYS, backup_times, backup_times_said, job_times, optional_answers

    if not interactive(ctx):
        return
    prompt = prompt or input
    output = output or print
    if not revisits_every_job(ctx):
        said = backup_times_said(ctx.config)
        output(f'Backup times are changed only by a full setup run (sudo bash install.sh); this run keeps {said}.')
        ctx.log('Backup times kept, not a full run: ' + said)
        return
    remembered = optional_answers(ctx.config['data_mount']).get(BACKUP_TIMES)
    if remembered is not None and remembered != backup_times_said(ctx.config):
        # The settings file was made again since (from the template, say): the times in
        # it are the ones the timers would get, so they are asked, not offered to keep.
        output(
            f'The backup times in the settings ({backup_times_said(ctx.config)}) are not the ones chosen last '
            f'time ({remembered}), so they are asked again.'
        )
    elif remembered is not None and BACKUP_TIMES in optional_kept(ctx, [BACKUP_TIMES], prompt, output):
        ctx.log('Backup times kept: ' + backup_times_said(ctx.config))
        return
    now = job_times(ctx.config)
    night_end = ctx.config.get('maintenance', {}).get('night_end_hour', 6)
    own = {'daily': now['app'], 'weekly_day': now['usb_day'], 'weekly': now['usb'], 'monthly': now['monthly']}
    counts = backup_counts(ctx.config)
    shown = {**own, 'monthly': monthly_offered(now['monthly'], night_end)}
    shown.update({KEEP_FIELD + field['group']: count_offered(field) for field in counts})
    page = setup_progress.page(
        BACKUP_TIMES,
        shown,
        weekdays=list(WEEKDAYS),
        night_end=night_end,
        counts=counts,
        table=backup_rows(ctx.config),
        daily_words=daily_words(ctx.config),
        weekly_copy=weekly_copy(ctx.config),
        weekly_words=weekly_words(ctx.config),
    )
    with page:
        chosen = ask_backup_times(ctx, now, night_end, prompt, output)
        kept = ask_backup_counts(ctx, counts, prompt, output)
    keep_counts(ctx, kept, output)
    # Settings from before 0.48.0 (no "backup_times" yet) gave each job a time of its own.
    moved = jobs_moved(ctx.config, now, chosen) if backup_times(ctx.config) is None else []
    if moved and chosen == own:
        # Every answer kept the time shown, yet saved as "backup_times" they would move
        # the music, settings or database copy to its fixed distance from the daily and
        # weekly times (job_times): the server keeps the times it has.
        times_settled(ctx, output, 'kept as they were')
        return
    if moved:
        output('With these times ' + ', '.join(moved) + '.')
    ctx.config[BACKUP_TIMES] = chosen
    save_settings(ctx)
    times_settled(ctx, output, 'chosen')


# How many nightly backups are kept (0.50.0, the user: "7 backups is insane do max 7
# default 3"): a number from 1 to 7 for the app backups and one for the settings backups,
# on the same page as the times. The numbers, their limits and what a change does are
# backup_plan's; a count from before that setup no longer offers stays as it is, and
# nothing is removed, until a number is chosen here.
KEEP_FIELD = 'keep_'


def backup_counts(config):
    """The "keep the latest [N]" fields this server has (backup_plan.count_field): the
    app backups only with media apps, the settings backups always."""
    import backup_plan

    groups = [row['group'] for row in backup_plan.table(config) if row.get('group')]
    return [backup_plan.count_field(config, group) for group in dict.fromkeys(groups)]


def backup_rows(config):
    """What is backed up, where it goes, when, and how many are kept: the page's table."""
    import backup_plan

    return [
        {key: row.get(key) for key in ('key', 'what', 'drive', 'when', 'kept', 'covers', 'same_drive', 'note')}
        for row in backup_plan.table(config)
    ]


def count_offered(field):
    """What a count's question offers: the number it has, or nothing for an earlier count
    outside the limits, so that Enter leaves it as it is."""
    return '' if field['earlier'] else str(field['value'])


def ask_backup_counts(ctx, counts, prompt, output):
    """One question for each count, on the backups page: {group: the number typed, '' for
    Enter on a count that stays as it is}."""
    chosen = {}
    for field in counts:
        least, most = field['least'], field['most']
        output(
            f"Every night: the newest {field['what']} are kept, and an older one is removed only after a new "
            f'backup has succeeded. From {least} to {most}. This never deletes your photos, films or music.'
        )
        if field['review']:
            output(field['review'])
        offered = count_offered(field)
        chosen[field['group']] = ask_on_page(
            KEEP_FIELD + field['group'],
            f"How many {field['what']} should be kept? ({least} to {most}, Enter "
            + (f'for {offered}' if offered else 'leaves them as they are')
            + '): ',
            offered,
            lambda answer, least=least, most=most: answer.isdigit() and least <= int(answer) <= most,
            f'Type a whole number from {least} to {most}.',
            prompt,
            output,
        )
    return chosen


def keep_counts(ctx, chosen, output):
    """The numbers chosen written to the settings (backup_plan.set_count), once, and said.
    Nothing is removed here: the scheduled job does that after its next backup that
    succeeds."""
    import backup_plan

    changed = False
    for group, answer in chosen.items():
        if not answer:
            continue
        keep, earlier = backup_plan.count(ctx.config, group)
        if earlier or keep != int(answer):
            backup_plan.set_count(ctx.config, group, int(answer))
            changed = True
            what = backup_plan.COUNTED[group][0]
            output(
                f'The newest {answer} {what} are kept from now on; nothing is removed before the next backup succeeds.'
            )
            ctx.log(f'Backups kept: {answer} {what}')
    if changed:
        save_settings(ctx)


def times_settled(ctx, output, how):
    """The backup times as they now stand: remembered as the answer to this optional
    question, said, and logged (`how`: chosen, or kept as they were)."""
    from neko_common import BACKUP_TIMES, backup_times_said, remember_answer

    said = backup_times_said(ctx.config)
    remember_answer(ctx.config['data_mount'], BACKUP_TIMES, said)
    output(f'Backups run {said}. To change this later, run  sudo bash install.sh  again and say yes to asking again.')
    ctx.log(f'Backup times {how}: {said}')


def jobs_moved(config, now, chosen):
    """On a server set up before 0.48.0 the music copy, the copy of this PC's own settings
    and Immich's database copy had times of their own; from "backup_times" they keep a
    fixed distance from the daily and weekly times (job_times). Each one `chosen` moves,
    in words ("the music copy moves from 03:45 to 22:30"), so nothing changes unsaid."""
    from neko_common import BACKUP_TIMES, job_times

    after = job_times({**config, BACKUP_TIMES: chosen})
    photos = config.get('photos', {})
    weekly = isinstance(photos, dict) and photos.get('backup_schedule', 'legacy') == 'weekly'
    database = 'photo_database_weekly' if weekly else 'photo_database'
    moved = []
    for job, words in (
        ('music', 'the music copy'),
        ('host', "the copy of this PC's own settings"),
        (database, "Immich's database copy"),
    ):
        before, later = job_said(now, job), job_said(after, job)
        if before != later:
            moved.append(f'{words} moves from {before} to {later}')
    return moved


def job_said(times, job):
    """A job's time from job_times, with its day when it has one of its own."""
    if job == 'photo_database_weekly':
        return f"{times['photo_database_weekly_day']} {times[job]}"
    return times[job]


def monthly_offered(current, night_end):
    """The monthly time to offer as the default: the one the server has, unless it is no
    longer before `night_end` (maintenance.night_end_hour lowered since), then the latest
    time with the same minutes that is, since Enter must never keep a time the maintenance
    step refuses (modules/maintenance.py options)."""
    if int(current[:2]) < night_end:
        return current
    return f'{max(night_end - 1, 0):02d}:{current[3:]}'


def daily_words(config):
    """What runs at the daily time, for this server only (gate run B2: the music copy was
    named with Music off)."""
    from neko_common import media_group

    parts = []
    if any(media_group(config, group) for group in ('movies', 'music')):
        parts.append("the media apps' snapshot at the time you choose")
    if media_group(config, 'music') and config.get('music', {}).get('backup_enabled', True) is not False:
        parts.append('the music copy 30 minutes before it')
    parts.append("the copy of this PC's own settings " + ('an hour after it' if parts else 'at the time you choose'))
    if len(parts) == 1:
        return parts[0]
    return ', '.join(parts[:-1]) + ' and ' + parts[-1]


def weekly_copy(config):
    """The weekly USB copy runs on this server: there are photos or Nextcloud files to
    copy, and the offline drive was not left out (gate run B4: its day and time were asked
    with neither)."""
    from modules import photo_backup

    return any(photo_backup.copies(config)) and config.get('photos', {}).get('external_backup_wanted') is not False


def weekly_words(config):
    """What the weekly USB copy takes, for this server only."""
    from modules import photo_backup

    photos, cloud = photo_backup.copies(config)
    what = ' and '.join(name for name, on in (('your photos', photos), ('your Nextcloud files', cloud)) if on)
    return what + (", with Immich's database copy 45 minutes before it" if photos else '')


def ask_weekly(now, prompt, output):
    """The weekly USB copy's day and time."""
    from neko_common import WEEKDAYS, valid_time

    days = ', '.join(WEEKDAYS)
    weekday = ask_on_page(
        'weekly_day',
        f'On which day should the weekly USB copy run? ({days}; Enter for {now["usb_day"]}): ',
        now['usb_day'],
        lambda answer: answer.capitalize() in WEEKDAYS,
        'Type the day as one of ' + days + '.',
        prompt,
        output,
        str.capitalize,
    )
    weekly = ask_on_page(
        'weekly',
        f'At what time on that day? (HH:MM, Enter for {now["usb"]}): ',
        now['usb'],
        valid_time,
        'Type a time of day as HH:MM, for example 03:00.',
        prompt,
        output,
    )
    return weekday, weekly


def ask_backup_times(ctx, now, night_end, prompt, output):
    """The questions in the terminal (the window answers through ask_on_page); the chosen
    "backup_times" settings. The weekly copy's are asked only on a server that has it."""
    from neko_common import host_timezone, valid_time

    output('\n=== When the backups run ===')
    output(
        f'Times are on this PC\'s clock ({host_timezone(ctx.config)}). Press Enter to keep the time shown. '
        'Choose hours when nobody uses the server: the apps pause for a moment while they are copied.'
    )
    output('Every night: ' + daily_words(ctx.config) + '.')
    daily = ask_on_page(
        'daily',
        f'When should the daily backups run? (HH:MM, Enter for {now["app"]}): ',
        now['app'],
        valid_time,
        'Type a time of day as HH:MM, for example 04:15.',
        prompt,
        output,
    )
    if weekly_copy(ctx.config):
        output('Every week: the USB copy of ' + weekly_words(ctx.config) + '.')
        weekday, weekly = ask_weekly(now, prompt, output)
    else:
        # No weekly copy on this server: its day and time stay as they are, unasked.
        weekday, weekly = now['usb_day'], now['usb']
    output(
        'Every month, on the 1st: the check of every backup copy, a snapshot of every app and a restore '
        f'test; the weekly app updates start at this time too. It must start at night, before {night_end:02d}:00.'
    )
    offered = monthly_offered(now['monthly'], night_end)
    why = '' if offered == now['monthly'] else f'; {now["monthly"]} is not before {night_end:02d}:00'
    monthly = ask_on_page(
        'monthly',
        f'At what time should the monthly check start? (HH:MM before {night_end:02d}:00, Enter for {offered}{why}): ',
        offered,
        lambda answer: valid_time(answer) and int(answer[:2]) < night_end,
        f'Type a time of day as HH:MM before {night_end:02d}:00, for example 02:30.',
        prompt,
        output,
    )
    return {'daily': daily, 'weekly_day': weekday, 'weekly': weekly, 'monthly': monthly}
