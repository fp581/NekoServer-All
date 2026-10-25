"""Shortcuts on the server's own desktop: the dashboard, the web browser, Steam and every
app NekoServer installed, on any supported Linux. They also appear in the app menu.

Nothing starts by itself when you log in. A shortcut NekoServer made earlier is brought
up to date; one you made or changed yourself is left alone.
"""
import os
import shutil
import subprocess
from pathlib import Path

from modules import tv
from modules.dashboard import LAUNCHER
from neko_common import APPS, MEDIA_APPS, atomic_write, dashboard_port, find_program, safe_path

DEPENDENCIES = ['dashboard']
DESCRIPTION = 'Desktop shortcuts for the dashboard, the web browser, Steam and your apps'
MANAGED = 'X-NekoServer-Managed=true'
# Before 0.28.7 the TV step could start Steam Big Picture at every login.
OLD_AUTOSTART = '.config/autostart/nekoserver-steam.desktop'
# Debian's own Firefox is called firefox-esr there.
BROWSERS = ('firefox', 'firefox-esr', 'chromium', 'google-chrome-stable', 'google-chrome', 'brave', 'microsoft-edge-stable')


def entry(name, exec_line, icon, comment=''):
    return ('[Desktop Entry]\nType=Application\nName=' + name + '\n' +
            ('Comment=' + comment + '\n' if comment else '') +
            'Exec=' + exec_line + '\nIcon=' + icon + '\nTerminal=false\n' + MANAGED + '\n')


def installed_apps(config):
    """The apps set up on this server, in the dashboard's order."""
    data = Path(config['data_mount'])
    services = config.get('media', {}).get('services', [])
    if not config.get('music', {}).get('create_desktop_shortcut', True):
        services = [s for s in services if s != 'navidrome']
    found = []
    for app in APPS:
        if app in MEDIA_APPS:
            present = app in services
        else:   # Immich and Nextcloud have their own folder once set up.
            present = (data / 'compose' / app / 'compose.yaml').is_file()
        if present:
            found.append(app)
    return found


def shortcuts(config, which=find_program, launcher=LAUNCHER):
    """{file name: contents} of every shortcut this server should have. (`which` also
    looks in the games folders, where Debian and Ubuntu keep Steam.)"""
    files = {}
    port = dashboard_port(config)
    if port:
        files['nekoserver-dashboard.desktop'] = entry(
            'NekoServer Dashboard', str(launcher) if launcher.is_file() else f'xdg-open http://127.0.0.1:{port}/',
            'utilities-system-monitor', 'Your home server: apps, backups, drives and hardware')
    browser = next((which(b) for b in BROWSERS if which(b)), None)
    if browser:
        files['nekoserver-browser.desktop'] = entry('Web Browser', browser, 'web-browser', 'Browse the web')
    steam = which('steam')
    if steam:
        files['nekoserver-steam-desktop.desktop'] = entry('Steam', steam, 'steam', 'Your games')
    for app in installed_apps(config):
        name, purpose, app_port = APPS[app]
        files[f'nekoserver-{app}.desktop'] = entry(
            name, f'xdg-open http://127.0.0.1:{app_port}/', 'applications-multimedia', purpose)
    return files


def ours(path):
    """A shortcut NekoServer wrote (and nobody changed into something else)."""
    try:
        return MANAGED in path.read_text(errors='replace')
    except OSError:
        return False


def place(path, text, user, home, log):
    path = safe_path(path)
    if path.exists():
        if path.is_file() and path.read_text(errors='replace') == text:
            return
        # NekoServer's own shortcut still owned by root (a run cut off between writing it
        # and handing it over) is written again; one of yours is never touched.
        if not path.is_file() or not ours(path) or path.stat().st_uid not in (user.pw_uid, 0):
            log(f'Desktop: kept your own {path.name}')
            return
    tv.ensure_user_dir(path.parent, home, user)
    atomic_write(path, text, 0o755)
    os.chown(path, user.pw_uid, user.pw_gid)
    if shutil.which('gio'):
        # GNOME shows a desktop launcher only once it is marked as trusted.
        subprocess.run(tv.user_command(user, home, ['gio', 'set', str(path), 'metadata::trusted', 'true']),
                       capture_output=True, timeout=30, check=False)


def run(ctx):
    if not ctx.apply:
        ctx.log('Desktop shortcuts for the dashboard, browser, Steam and apps are added with --apply')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    if not tv.has_desktop():
        ctx.log('Desktop shortcuts: this PC has no desktop (a server install); every app opens in a browser on your other devices')
        return {'status': 'unavailable', 'reason': tv.NO_DESKTOP}
    try:
        user, home = tv.desktop_user(ctx.config)
    except (KeyError, RuntimeError) as exc:
        ctx.log(f'Desktop shortcuts skipped ({exc}); every app also opens in any browser')
        return {'status': 'unavailable', 'reason': 'No desktop user on this server'}
    old = safe_path(home / OLD_AUTOSTART)
    if old.is_file() and ours(old):
        old.unlink()
        ctx.log('Desktop: Steam Big Picture no longer opens by itself at login')
    files = shortcuts(ctx.config)
    folders = [home / '.local/share/applications']
    if ctx.config.get('tv', {}).get('desktop_shortcuts', True):
        desktop = tv.desktop_folder(user, home)
        if desktop is None:
            ctx.log('Desktop shortcuts: this desktop has no Desktop folder (it is set to your home folder), '
                    'so the apps are in the menu only')
        else:
            folders.append(desktop)
    for folder in folders:
        for name, text in files.items():
            place(folder / name, text, user, home, ctx.log)
    ctx.log('Desktop shortcuts: ' + ', '.join(sorted(t.split('\nName=', 1)[1].split('\n', 1)[0] for t in files.values())))
