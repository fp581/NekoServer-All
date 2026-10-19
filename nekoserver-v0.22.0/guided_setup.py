"""One-time, interactive desktop handoffs. Never run a browser as root."""
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from neko_common import safe_path


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
        missing = [pkg for pkg, binary in (('xdg-utils', 'xdg-open'), ('firefox', 'firefox'))
                   if not shutil.which(binary)]
        if missing and os.geteuid() == 0 and shutil.which('pacman'):
            # The installer bootstrap already restricts apply to Arch/Nyarch.
            try:
                subprocess.run(['pacman', '-S', '--needed', '--noconfirm', *missing],
                               check=True, capture_output=True, timeout=900)
            except (OSError, subprocess.SubprocessError):
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
            output('Return to Konsole afterward. No reopens this step; Ctrl+C leaves it pending.')
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
            while True:
                answer = prompt('Have you completed this step successfully? [yes/no]: ').strip().lower()
                if answer in ('yes', 'y', 'no', 'n'):
                    break
                output('Please type yes or no.')
            if answer in ('no', 'n'):
                output('Reopening this step and repeating its instructions.')
                continue
            if verify:
                try:
                    verified = verify()
                except (OSError, RuntimeError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                    verified = False
                if not verified:
                    output('The automatic check has not passed. Review the step, then try again.')
                    continue
            return True
    except (EOFError, KeyboardInterrupt):
        output('\nStep remains pending. Reapply this profile to resume; saved choices are kept.')
        return False


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
