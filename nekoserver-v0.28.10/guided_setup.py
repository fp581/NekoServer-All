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
from neko_common import safe_path


def yes_no(question, prompt=None, output=None):
    """Ask until the answer is yes or no. Returns True or False, or None when the person
    stopped with Ctrl+D or Ctrl+C. The one yes/no question used across NekoServer."""
    prompt = prompt or input
    output = output or print
    while True:
        try:
            answer = prompt(question + ' [yes/no]: ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            output('')
            return None
        if answer in ('yes', 'y'):
            return True
        if answer in ('no', 'n'):
            return False
        output('Please type yes or no.')


def interactive(ctx):
    return bool(getattr(ctx, 'apply', False) and
                ctx.config.get('guided_setup', {}).get('enabled', True) and sys.stdin.isatty())


def desktop_command(config, argv):
    """Use the invoking desktop user and a minimal environment, including its bus."""
    name = os.environ.get('SUDO_USER')
    if name and name != 'root':
        user = pwd.getpwnam(name)
        if os.environ.get('SUDO_UID') and str(user.pw_uid) != os.environ['SUDO_UID']:
            raise RuntimeError('Desktop identity does not match sudo user')
    else:
        selected = config.get('tv', {}).get('user', 'auto')
        user = (pwd.getpwnam(selected) if selected != 'auto' else
                pwd.getpwuid(config.get('media', {}).get('uid', 1000)))
    home = safe_path(user.pw_dir)
    if (user.pw_uid < 1000 or user.pw_name == 'root' or
            user.pw_shell.endswith(('nologin', 'false')) or not home.is_dir() or
            home.stat().st_uid != user.pw_uid):
        raise RuntimeError('An existing, logged-in non-root desktop user is required')
    runtime = safe_path(Path('/run/user') / str(user.pw_uid))
    if not runtime.is_dir() or runtime.stat().st_uid != user.pw_uid:
        raise RuntimeError('No active desktop runtime for the selected user')
    env = ['HOME=' + str(home), 'USER=' + user.pw_name, 'LOGNAME=' + user.pw_name,
           'PATH=/usr/local/bin:/usr/bin:/bin', 'XDG_RUNTIME_DIR=' + str(runtime)]
    bus = runtime / 'bus'
    if bus.exists():
        env.append('DBUS_SESSION_BUS_ADDRESS=unix:path=' + str(bus))
    display = os.environ.get('DISPLAY', '')
    wayland = os.environ.get('WAYLAND_DISPLAY', '')
    if re.fullmatch(r':[0-9]+(?:\.[0-9]+)?', display):
        env.append('DISPLAY=' + display)
    if re.fullmatch(r'wayland-[0-9]+', wayland) and (runtime / wayland).exists():
        env.append('WAYLAND_DISPLAY=' + wayland)
    elif not wayland:
        # sudo may strip WAYLAND_DISPLAY; use an actual socket in this user's runtime.
        sockets = sorted(p for p in runtime.glob('wayland-*')
                         if re.fullmatch(r'wayland-[0-9]+', p.name) and p.is_socket())
        if len(sockets) == 1:
            env.append('WAYLAND_DISPLAY=' + sockets[0].name)
    auth = os.environ.get('XAUTHORITY', '')
    if auth:
        candidate = safe_path(auth)
        if (candidate.is_file() and candidate.stat().st_uid == user.pw_uid and
                (home in candidate.parents or runtime in candidate.parents)):
            env.append('XAUTHORITY=' + str(candidate))
    return ['runuser', '-u', user.pw_name, '--', 'env', '-i', *env, *argv]


def valid_url(url):
    try:
        parsed = urlsplit(url)
        return (isinstance(url, str) and len(url) < 4096 and
                not any(ord(ch) < 33 or ord(ch) == 127 for ch in url) and
                not parsed.username and not parsed.password and bool(parsed.hostname) and
                (parsed.scheme == 'https' or
                 (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost'))))
    except (TypeError, ValueError):
        return False


PAGE_ROOT = Path('/usr/local/share/nekoserver-pages')


def valid_page(path, root=None):
    """A local page NekoServer wrote itself: a regular file directly inside PAGE_ROOT."""
    root = Path(root or PAGE_ROOT)
    try:
        path = Path(path)
        return (path.is_absolute() and path.parent == root and path.suffix == '.html'
                and not path.is_symlink() and not root.is_symlink() and path.is_file())
    except (TypeError, ValueError, OSError):
        return False


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
        commands = {'steam': ['/usr/bin/steam', '-gamepadui'],
                    'bluetooth': ['/usr/bin/blueman-manager'],
                    'settings': ['/usr/bin/gnome-control-center', 'display']}
        if kind not in commands:
            raise ValueError('Unsupported desktop application')
        argv = commands[kind]
        if kind == 'bluetooth' and Path('/usr/bin/gnome-control-center').is_file():
            argv = ['/usr/bin/gnome-control-center', 'bluetooth']
        if not Path(argv[0]).is_file():
            return False
    try:
        command = desktop_command(ctx.config, argv)
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, cwd='/')
        try:
            return child.wait(timeout=2) == 0
        except subprocess.TimeoutExpired:
            return True  # Long-lived app was launched; confirmation is still required.
    except (OSError, RuntimeError, KeyError, ValueError):
        return False


def checkpoint(ctx, title, instructions, targets=(), verify=None, prepare=None,
               prompt=None, output=None):
    """No repeats instructions/opening; yes must also pass any supplied verifier.

    EOF/Ctrl+C leaves this step pending. Background/read-only runs never launch or
    prompt. Callers retain machine and witnessed evidence as separate concepts.
    """
    if not interactive(ctx):
        return False
    prompt = prompt or input
    output = output or print
    try:
        while True:
            output('\n=== ' + title + ' ===')
            for number, instruction in enumerate(instructions, 1):
                output(str(number) + '. ' + instruction)
            output('Return to the terminal afterward. No reopens this step; Ctrl+C leaves it pending.')
            current = list(targets)
            if prepare:
                try:
                    extra = prepare()
                    if extra:
                        current.extend(extra)
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
                    output('The launch preparation did not complete. Follow the instructions manually or answer no to retry.')
            for kind, target in current:
                opened = launch(ctx, target, kind)
                if kind in ('url', 'page'):
                    # Print only to the user's console, not the durable installer logger.
                    output(('Opened: ' if opened else 'Open this address manually: ') + target)
                elif not opened:
                    output('Automatic launch unavailable. Open ' + kind + ' from your desktop menu.')
            answer = yes_no('Have you completed this step successfully?', prompt, output)
            if answer is None:
                raise KeyboardInterrupt
            if not answer:
                output('Reopening this step and repeating its instructions.')
                continue
            if verify:
                why = ''
                try:
                    verified = verify()
                except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                    verified, why = False, str(exc)
                if not verified:
                    output('The automatic check has not passed' + (': ' + why if why else '') + '. Review the step, then try again.')
                    continue
            return True
    except (EOFError, KeyboardInterrupt):
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
        except (EOFError, KeyboardInterrupt):
            return ''
        if not value or valid(value):
            return value
        output(hint)
    return ''


def save_settings(ctx):
    if hasattr(ctx, 'persist_config'):
        ctx.persist_config()


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
        output('Skipped. To set it up later, change "wanted" to true under "' + section +
               '" in my-config.json and run  sudo bash install.sh  again.')
    return options['wanted']


def app_security(ctx, apps, ports):
    def verify_accounts():
        import account_setup
        from recovery_tools import lock
        with lock(ctx.config):
            return not account_setup.provision(ctx.config, apps, output=ctx.log)
    import account_store
    verify = verify_accounts if account_store.enabled(ctx.config) else None
    return checkpoint(ctx, 'Check application logins and access permissions', [
        'Sign into each opened app using its own saved username/email and password.',
        'Read the root-only APP-CREDENTIALS.txt on the server for those app logins. '
        'Email-shaped local logins do not sign you into an external mail provider.',
        'If an app explicitly requests email verification, open your own email app, '
        'follow its verification link, then return. Enter external passwords only on the provider page.',
        'Verify administrator access, disable unwanted public registration where available, '
        'and check that personal files are not shared with unintended users.',
        'Do not confirm until every selected app can be signed into.'
    ], [('url', 'http://127.0.0.1:' + str(ports[app]) + '/') for app in apps], verify=verify)
