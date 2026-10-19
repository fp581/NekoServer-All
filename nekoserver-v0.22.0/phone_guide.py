"""Phone setup for each everyday app, and a local page with QR codes for their addresses.

Used by the installer's guided phone steps. Usernames come from the private account
store; passwords are never printed or written to the page.
"""
import html
from pathlib import Path
import shutil
import subprocess

from guided_setup import PAGE_ROOT
from neko_common import atomic_write

PAGE = PAGE_ROOT / 'phone-setup.html'
CREDENTIALS = '/srv/nekoserver/secrets/accounts/APP-CREDENTIALS.txt'
TAILSCALE_DOWNLOAD = 'https://tailscale.com/download'

# Which login each app uses on the phone (account_store keys).
LOGIN = {'jellyfin': 'jellyfin', 'seerr': 'seerr', 'immich': 'immich',
         'navidrome': 'navidrome_listener', 'nextcloud': 'nextcloud'}

APPS = {
    'jellyfin': {
        'title': 'Jellyfin: movies and TV',
        'iphone': 'Jellyfin (App Store)',
        'android': 'Jellyfin (Google Play)',
        'steps': [
            'Install the app and open it.',
            'Tap "Add Server" (or "Connect"), enter the server address below, and tap "Connect".',
            'Sign in with your Jellyfin username and password.',
            'Play a short video to check it works.',
        ],
    },
    'seerr': {
        'title': 'Seerr: request movies and shows',
        'iphone': 'No app needed: use Safari',
        'android': 'No app needed: use Chrome',
        'steps': [
            'Open the server address below in your phone\'s browser.',
            'Sign in with your Seerr email address and password.',
            'Add it to your home screen: in Safari tap Share > "Add to Home Screen"; '
            'in Chrome tap the menu > "Add to Home screen".',
        ],
    },
    'immich': {
        'title': 'Immich: photo and video backup',
        'iphone': 'Immich (App Store)',
        'android': 'Immich (Google Play)',
        'steps': [
            'Install the app and open it.',
            'In "Server Endpoint URL" enter the server address below and tap "Next".',
            'Sign in with your Immich email address and password.',
            'Allow access to your photos when the phone asks.',
            'Tap the cloud icon at the top, choose the albums to back up (e.g. Recents), '
            'and turn backup on. Optionally allow backup on mobile data.',
        ],
    },
    'navidrome': {
        'title': 'Navidrome: music',
        'iphone': 'Amperfy (App Store)',
        'android': 'Substreamer or Ultrasonic (Google Play)',
        'steps': [
            'Install the app and open it.',
            'Add a server: enter the server address below as the server URL.',
            'Sign in with your Navidrome listener username and password.',
            'Play a song to check it works.',
        ],
    },
    'nextcloud': {
        'title': 'Nextcloud: your files',
        'iphone': 'Nextcloud (App Store)',
        'android': 'Nextcloud (Google Play)',
        'steps': [
            'Install the app and tap "Log in".',
            'Enter the server address below and continue.',
            'A login page opens: sign in with your Nextcloud username and password, '
            'then tap "Grant access".',
            'Optional: turn on "Auto upload" for your camera folder.',
        ],
    },
}


def username(config, app):
    """The saved username for an app's phone login, or None if not set up yet."""
    try:
        import account_store
        entry = account_store.get(config, LOGIN[app])
        return entry['username'] if entry else None
    except (ImportError, OSError, RuntimeError, KeyError):
        return None


def instructions(config, app, url):
    """Numbered console steps for one app on the phone."""
    info = APPS[app]
    user = username(config, app)
    login = ('Username: ' + user) if user else 'Username: see the password list'
    return [
        'Phone app: iPhone: ' + info['iphone'] + '. Android: ' + info['android'] + '.',
        'Make sure Tailscale is ON on the phone.',
        *[step.replace('the server address below', 'this server address: ' + url) for step in info['steps']],
        login + '. Password: in the password list on the server '
        '(sudo cat ' + CREDENTIALS + ').',
        'Return here and answer yes once it works on the phone.',
    ]


def qr_svg(text):
    """An inline SVG QR code, or '' if qrencode is unavailable."""
    if not shutil.which('qrencode'):
        return ''
    try:
        result = subprocess.run(['qrencode', '-t', 'SVG', '-m', '1', '-o', '-', '--', text],
                                capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return ''
    out = result.stdout
    if result.returncode or '<svg' not in out:
        return ''
    return out[out.index('<svg'):]


def ensure_qrencode():
    if shutil.which('qrencode') or not shutil.which('pacman'):
        return
    try:
        subprocess.run(['pacman', '-S', '--needed', '--noconfirm', 'qrencode'],
                       capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        pass


def card(title, url, lines, qr):
    items = ''.join('<li>' + html.escape(line) + '</li>' for line in lines)
    code = ('<div class="qr" aria-label="QR code for ' + html.escape(url) + '">' + qr + '</div>') if qr else ''
    return ('<section><div class="text"><h2>' + html.escape(title) + '</h2>'
            '<p class="url"><a href="' + html.escape(url) + '">' + html.escape(url) + '</a></p>'
            '<ol>' + items + '</ol></div>' + code + '</section>')


def page_html(config, links, qr=qr_svg):
    """The phone setup page: Tailscale first, then one card per app."""
    cards = [card('1. Tailscale: connect your phone to your server', TAILSCALE_DOWNLOAD, [
        'Scan this code with the phone camera, or search "Tailscale" in the app store.',
        'Open Tailscale, allow the VPN, and sign in with the same account as this server.',
        'Turn Tailscale on. Leave it on whenever you want to use your apps.',
    ], qr(TAILSCALE_DOWNLOAD))]
    for number, (app, url) in enumerate(((a, links[a]) for a in APPS if a in links), 2):
        info = APPS[app]
        user = username(config, app)
        steps = [s.replace('the server address below', 'the address above') for s in info['steps']]
        lines = ['iPhone: ' + info['iphone'], 'Android: ' + info['android'], *steps,
                 ('Username: ' + user) if user else 'Username: in the password list',
                 'Password: in the password list on the server (not shown here).']
        cards.append(card(str(number) + '. ' + info['title'], url, lines, qr(url)))
    style = ('body{margin:0;background:#11101b;color:#f3efff;font:17px/1.5 system-ui,sans-serif}'
             'main{max-width:900px;margin:0 auto;padding:24px 16px}h1{font-size:1.6em}'
             'section{display:flex;gap:20px;align-items:flex-start;background:#1d1b2d;'
             'border-radius:14px;padding:18px;margin:16px 0}.text{flex:1;min-width:0}'
             'h2{margin:0 0 6px;font-size:1.15em}.url a{color:#b9a7ff;word-break:break-all}'
             '.qr{flex:0 0 170px;background:#fff;border-radius:10px;padding:8px}'
             '.qr svg{width:100%;height:auto;display:block}code{word-break:break-all}'
             '@media(max-width:600px){section{flex-direction:column}.qr{flex-basis:auto;width:170px}}')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Phone setup</title><style>' + style + '</style></head><body><main>'
            '<h1>Set up your phone</h1><p>Do these in order. Scan each code with your '
            'phone camera to open the address. Passwords are in the password list on the '
            'server: <code>sudo cat ' + html.escape(CREDENTIALS) + '</code></p>'
            + ''.join(cards) + '</main></body></html>\n')


def write_page(config, links, root=PAGE_ROOT):
    """Write the page where the desktop user can open it; returns its path."""
    ensure_qrencode()
    root = Path(root)
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    root.chmod(0o755)
    path = root / PAGE.name
    atomic_write(path, page_html(config, links), 0o644)
    return path
