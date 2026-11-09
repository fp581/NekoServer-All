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


def yes_no(question, prompt=None, output=None):
    """Ask until the answer is yes or no. Returns True or False, or None when the person
    pressed Ctrl+D (leave it for now). Ctrl+C stops setup (KeyboardInterrupt goes on),
    as it does everywhere else. The one yes/no question used across NekoServer."""
    prompt = prompt or input
    output = output or print
    while True:
        try:
            answer = prompt(question + ' [yes/no]: ').strip().lower()
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
    try:
        command = desktop_command(ctx.config, argv)
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
            return True  # Long-lived app was launched; confirmation is still required.
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


def checkpoint(ctx, title, instructions, targets=(), verify=None, prepare=None, prompt=None, output=None, keys=KEYS):
    """No repeats instructions/opening; yes must also pass any supplied verifier.

    EOF (Ctrl+D) leaves this step pending; Ctrl+C stops setup. Background/read-only runs never launch or
    prompt. Callers retain machine and witnessed evidence as separate concepts.
    """
    if not interactive(ctx):
        return False
    prompt = prompt or input
    output = output or print
    try:
        while True:
            show_step(output, title, instructions, keys)
            open_targets(ctx, prepared_targets(targets, prepare, output), output)
            answer = yes_no('Have you completed this step successfully?', prompt, output)
            if answer is None:
                raise EOFError
            if not answer:
                output('Reopening this step and repeating its instructions.')
            elif check_passed(verify, output):
                return True
    except EOFError:
        output('\nStep remains pending. Reapply this profile to resume; saved choices are kept.')
        return False


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
        output('Answer no to keep these answers; yes asks the questions again.')
        again = bool(yes_no('Ask these questions again?', prompt, output))
        output('')
        ctx.log('Optional questions: ' + ('asked again' if again else 'kept as answered last time'))
        ctx.ask_optional_again = again
    return [] if again else shown


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


def ask_on_page(key, question, default, valid, hint, prompt=input, output=print):
    """ask_choice for one field of a page the setup window shows whole (setup_progress.page):
    when the window already sent this field, that answer is used (checked like a typed
    one) and the terminal is not asked; the window's "Skip for now" keeps the default."""
    import setup_progress

    given = setup_progress.page_answer(key)
    if given is not None:
        kind, value = given
        if kind == 'skip':
            return default
        value = value.strip()
        if valid(value):
            output(f'{question}{value}  (from the setup window)')
            setup_progress.page_set(key, value)
            return value
        output(hint)
    setup_progress.page_asks(key)
    answer = ask_choice(question, default, valid, hint, prompt, output)
    setup_progress.page_set(key, answer)
    return answer


def choose_backup_times(ctx, prompt=None, output=None):
    """Ask when the backups run (the daily jobs, the weekly USB copy, the monthly check)
    and save the answers as "backup_times" in the settings file; every timer is written
    from them (neko_common.job_times). Enter keeps the time shown: the server's current
    times, so a server set up before 0.48.0 keeps its schedule (its settings untouched
    when every answer is Enter and its other jobs had times of their own). Asked once; a run after
    that shows the answer and asks again only on request (optional_kept). Setup without
    a person at the terminal (an upgrade) changes nothing. The setup window shows the
    three as one page (setup_progress.page) and may answer them together."""
    import setup_progress
    from neko_common import BACKUP_TIMES, WEEKDAYS, backup_times, backup_times_said, job_times, optional_answers

    if not interactive(ctx):
        return
    prompt = prompt or input
    output = output or print
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
    shown = {'daily': now['app'], 'weekly_day': now['usb_day'], 'weekly': now['usb'], 'monthly': now['monthly']}
    with setup_progress.page(BACKUP_TIMES, shown, weekdays=list(WEEKDAYS), night_end=night_end):
        chosen = ask_backup_times(ctx, now, night_end, prompt, output)
    # Settings from before 0.48.0 (no "backup_times" yet) gave each job a time of its own.
    moved = jobs_moved(ctx.config, now, chosen) if backup_times(ctx.config) is None else []
    if moved and chosen == shown:
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


def ask_backup_times(ctx, now, night_end, prompt, output):
    """The three questions in the terminal (the window answers through ask_on_page);
    the chosen "backup_times" settings."""
    from neko_common import WEEKDAYS, host_timezone, valid_time

    days = ', '.join(WEEKDAYS)
    output('\n=== When the backups run ===')
    output(
        f'Times are on this PC\'s clock ({host_timezone(ctx.config)}). Press Enter to keep the time shown. '
        'Choose hours when nobody uses the server: the apps pause for a moment while they are copied.'
    )
    output(
        'Every night: the media apps\' snapshot at the time you choose, the music copy 30 minutes '
        'before it and the copy of this PC\'s own settings an hour after it.'
    )
    daily = ask_on_page(
        'daily',
        f'When should the daily backups run? (HH:MM, Enter for {now["app"]}): ',
        now['app'],
        valid_time,
        'Type a time of day as HH:MM, for example 04:15.',
        prompt,
        output,
    )
    output(
        'Every week: the USB copy of your photos and Nextcloud files, with Immich\'s database copy '
        '45 minutes before it.'
    )
    weekday = ask_on_page(
        'weekly_day',
        f'On which day should the weekly USB copy run? ({days}; Enter for {now["usb_day"]}): ',
        now['usb_day'],
        lambda answer: answer.capitalize() in WEEKDAYS,
        'Type the day as one of ' + days + '.',
        prompt,
        output,
    ).capitalize()
    weekly = ask_on_page(
        'weekly',
        f'At what time on that day? (HH:MM, Enter for {now["usb"]}): ',
        now['usb'],
        valid_time,
        'Type a time of day as HH:MM, for example 03:00.',
        prompt,
        output,
    )
    output(
        'Every month, on the 1st: the check of every backup copy, a snapshot of every app and a restore '
        f'test; the weekly app updates start at this time too. It must start at night, before {night_end:02d}:00.'
    )
    monthly = ask_on_page(
        'monthly',
        f'At what time should the monthly check start? (HH:MM before {night_end:02d}:00, Enter for {now["monthly"]}): ',
        now['monthly'],
        lambda answer: valid_time(answer) and int(answer[:2]) < night_end,
        f'Type a time of day as HH:MM before {night_end:02d}:00, for example 02:30.',
        prompt,
        output,
    )
    return {'daily': daily, 'weekly_day': weekday, 'weekly': weekly, 'monthly': monthly}


def app_security(ctx, apps, ports):
    def verify_accounts():
        import account_setup
        from recovery_tools import lock

        with lock(ctx.config):
            return not account_setup.provision(ctx.config, apps, output=ctx.log)

    import account_store

    verify = verify_accounts if account_store.enabled(ctx.config) else None
    return checkpoint(
        ctx,
        'Check application logins and access permissions',
        [
            'Sign into each opened app using its own saved username/email and password.',
            'See an app\'s login (with a QR code) in a terminal on the server:  ' + login_command(ctx.config) + '  '
            'Email-shaped local logins do not sign you into an external mail provider.',
            'If an app explicitly requests email verification, open your own email app, '
            'follow its verification link, then return. Enter external passwords only on the provider page.',
            'Verify administrator access, disable unwanted public registration where available, '
            'and check that personal files are not shared with unintended users.',
            'Do not confirm until every selected app can be signed into.',
        ],
        [('url', 'http://127.0.0.1:' + str(ports[app]) + '/') for app in apps],
        verify=verify,
    )
