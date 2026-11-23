#!/usr/bin/env python3
"""Makes the README and wiki pictures in docs/media from the REAL pages, filled with
made-up data for a healthy server (nine apps running, two drives, last night's backups
done, backup cards in):

  looks-side-by-side.gif  Moonlit Neko beside Professional on the same pages, dark then light (animated)
  setup-window.webp    The setup window: apps, review, progress, a question, finish (animated)
  setup-terminal.webp  The terminal setup's first page
  dashboard-tour.webp  Home, drives, backups, backup cards, media folders, Ask Neko, updates (animated)

nekoserver-overview.webp is drawn by hand and is not made here.

The dashboard is the runtime's own web server (`nekoserver-runtime serve`, as
nekoserver/tests/test_dashboard_browser.py runs it) with a made-up status file and a
stand-in control service; Ask Neko's answer is a fixed one (no model runs). The setup
window is setup_gui.Window itself, fed setup's own progress events; its words come from
explanations.py, questions.py and finish_page.py. The terminal page is wizard.py's welcome
and first page, printed by the real functions with a prompt that answers nothing.

Needs:
  * Linux and Python 3.11+ (standard library only) for the capture step;
  * a built runtime: cd nekoserver/runtime && cargo build --release --locked
    (the binary is nekoserver/runtime/target/release/nekoserver-runtime; or --runtime PATH);
  * Node 18+ with Playwright and its Chromium (NODE_PATH, PLAYWRIGHT_BROWSERS_PATH as usual);
  * ffmpeg with libwebp (animated WebP) for the assemble step: on PATH, or FFMPEG=/path/to/ffmpeg.
  * Not as root (the dashboard's web server refuses to run as root).

Run (from the repository):
  python3 docs/media/make-pictures.py                    # capture + assemble, every picture
  python3 docs/media/make-pictures.py --only looks       # one of them (terminal, setup, tour, looks)
  python3 docs/media/make-pictures.py --step capture --work /tmp/neko-pics
  python3 docs/media/make-pictures.py --step assemble --work /tmp/neko-pics
The two steps can run on different computers (ffmpeg on one, Linux and Chromium on the
other) when --work is a folder both can read: capture writes PNG frames and a plan.json
there; assemble turns them into the WebP (and GIF) files in docs/media.
"""

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SERVER = REPO / 'nekoserver'
DRIVER = HERE / 'make-pictures.mjs'
VERSION = '0.52.3'

MOVING = {'width': 1000, 'height': 680}  # the animations' frames
SIDE = {'width': 800, 'height': 620}  # each look of looks-side-by-side, shown at 640 px wide
PANEL = 640
MOST_BYTES = 3 * 1024 * 1024 - 64 * 1024  # each animation stays under 3 MB
MOST_STILL = 1024 * 1024  # and each still under 1 MB
sys.path[:0] = [str(SERVER), str(SERVER / 'tests')]  # NekoServer's own modules and the browser tests


# ----------------------------------------------------------------- the made-up server


def healthy_status(now):
    """test_dashboard_browser.snapshot(), made healthy and filled in for every page."""
    import test_dashboard_browser as browser_tests

    status = browser_tests.snapshot()
    day, hour = 86400, 3600
    about = {
        'jellyfin': ('Movies & shows', 8096, 0.6, 410e6),
        'immich': ('Your photos & memories', 2283, 1.8, 1.2e9),
        'navidrome': ('Music', 4533, 0.1, 62e6),
        'nextcloud': ('Your files', 8080, 0.4, 230e6),
        'seerr': ('Ask for movies & shows', 5055, 0.1, 180e6),
        'sonarr': ('Finds shows', 8989, 0.2, 160e6),
        'radarr': ('Finds movies', 7878, 0.2, 150e6),
        'lidarr': ('Finds music', 8686, 0.1, 140e6),
        'prowlarr': ('Search sites', 9696, 0.1, 120e6),
    }
    apps = []
    for app in status['applications']:
        words, port, cpu, memory = about[app['id']]
        apps.append(
            {
                **app,
                'description': words,
                'port': port,
                'url': f'http://127.0.0.1:{port}/',
                'health': 'healthy',
                'held': False,
                'cpu_percent': cpu,
                'memory_used': memory,
            }
        )
    tb = 1e12

    def smart(temperature, wear=None):
        return {
            'status': 'pass',
            'reason': 'SMART health passed',
            'checked_at': now - 2 * hour,
            'temperature': temperature,
            'wear_percent': wear,
        }

    storage = [
        {
            'id': 'os', 'name': 'OS SSD', 'mount': '/', 'status': 'mounted',
            'total': 0.5 * tb, 'used': 0.06 * tb, 'used_percent': 12, 'smart': smart(36, 3),
        },
        {
            'id': 'data_ssd', 'name': 'Application SSD', 'mount': '/srv/nekoserver', 'status': 'mounted',
            'photos': True, 'total': 1 * tb, 'used': 0.38 * tb, 'used_percent': 38, 'smart': smart(39, 6),
        },
        {
            'id': 'media_hdd', 'name': 'Media HDD', 'mount': '/mnt/nekoserver-media', 'status': 'mounted',
            'total': 8 * tb, 'used': 4.2 * tb, 'used_percent': 52, 'smart': smart(34),
        },
    ]  # fmt: skip

    def tonight(at_hour, minute=0, days=0):
        start = time.localtime(now)
        moment = time.mktime((*start[:3], at_hour, minute, 0, 0, 0, -1)) + days * day
        return moment + day if moment <= now else moment

    def job(ident, name, unit, last, next_at, restore=()):
        return {
            'id': ident,
            'name': name,
            'unit': unit,
            'status': 'pass',
            'reason': None,
            'last_attempt': last,
            'last_success': last,
            'restore': list(restore),
            'timer': {
                'active': True,
                'installed': True,
                'next_run': time.strftime('%a %Y-%m-%d %H:%M:%S', time.localtime(next_at)),
                'next_run_at': next_at,
            },
        }

    backups = [
        job('applications', 'Media application snapshot', 'nekoserver-app-backup.service',
            now - 9 * hour, tonight(2, 15), ['app_restore']),
        job('host', 'Host and user snapshot', 'nekoserver-host-backup.service',
            now - 8 * hour, tonight(3, 15), ['host_restore']),
        job('database', 'Immich database', 'nekoserver-immich-db-backup.service',
            now - 10 * hour, tonight(1, 15), ['independent_restore']),
        job('archive', 'Photo archive', 'nekoserver-immich-db-backup.service',
            now - 10 * hour, tonight(1, 15), ['independent_restore']),
        job('external', 'USB copy (photos and files)', 'nekoserver-photo-backup.service',
            now - 7 * hour, tonight(4, 0), ['independent_restore', 'cloud_restore']),
        job('music', 'Music library backup', 'nekoserver-music-backup.service',
            now - 9 * hour, tonight(2, 45), ['independent_restore']),
        job('cards', 'Backup cards', 'nekoserver-cards.service', now - 20 * hour, tonight(5, 0)),
        job('maintenance', 'Monthly check and snapshot', 'nekoserver-maintenance.service',
            now - 12 * day, now + 18 * day),
        job('update_check', 'Daily update check', 'nekoserver-update-check.service',
            now - 5 * hour, tonight(6, 0)),
        job('updates', 'Weekly app updates', 'nekoserver-updates.service', now - 3 * day, now + 4 * day),
    ]  # fmt: skip
    cards = {
        'enabled': True,
        'set': {
            'set_name': 'Photo cards',
            'updated_at': now - 20 * hour,
            'last_write_at': now - 20 * hour,
            'files_on_cards': 18_420,
            'files_waiting': 0,
            'cards': [
                {'number': 1, 'plugged': False, 'files': 11_900, 'full': True, 'state': 'in_use'},
                {'number': 2, 'plugged': True, 'files': 6_520, 'full': False, 'state': 'in_use'},
            ],
        },
    }
    startup = [
        ('Apps (Docker)', 'docker.service'),
        ('Phone access (Tailscale)', 'tailscaled.service'),
        ('This dashboard', 'nekoserver-dashboard.service'),
        ('Dashboard readings', 'nekoserver-dashboard-collect.timer'),
        ('Fan & light control', 'nekoserver-control.service'),
        ('Shared folders (Samba)', 'smbd.service'),
        ('Home network gateway (HTTPS)', 'nekoserver-gateway-lan.service'),
        ('Nightly app backup', 'nekoserver-app-backup.timer'),
        ('Nightly host backup', 'nekoserver-host-backup.timer'),
        ('USB photo backup', 'nekoserver-photo-backup.timer'),
        ('Monthly check', 'nekoserver-maintenance.timer'),
    ]
    status.update(
        {
            'generated_at': now,
            'late_after': 630,
            'collect_interval': 60,
            'version': VERSION,
            'acceptance': {
                'status': 'complete',
                'profile': 'full',
                'generated_at': now - 6 * day,
                'automatic': {},
                'manual': {},
                # Every restore tried by hand and recorded (acceptance.py --manual).
                'manual_checks': [
                    {'id': ident, 'status': 'pass'}
                    for ident in ('app_restore', 'host_restore', 'independent_restore', 'cloud_restore')
                ],
            },
            'alerts': [],
            'applications': apps,
            'storage': storage,
            'arrays': [],
            'backups': backups,
            'backup_key': True,
            'backup_place': {'offline': False, 'same_drive': False},
            'integrity': [
                {
                    'id': 'photos',
                    'name': 'Photo archive',
                    'at': now - 10 * hour,
                    'full_at': now - 12 * day,
                    'files': 48_211,
                    'damaged': 0,
                    'missing': 0,
                    'unverified': 0,
                    'repaired': 0,
                },
                {
                    'id': 'usb_photos',
                    'name': 'USB copy of the photos',
                    'at': now - 7 * hour,
                    'full_at': now - 12 * day,
                    'files': 48_211,
                    'damaged': 0,
                    'missing': 0,
                    'unverified': 0,
                },
            ],
            'recovery': {
                'status': 'pass',
                'checkpoint': True,
                'reason': None,
                'latest_rehearsal': {'at': now - 6 * day, 'status': 'pass'},
            },
            'heartbeat': {
                'status': 'pass',
                'last_attempt': now - 240,
                'last_success': now - 240,
                'provider_notifications_verified': True,
                'timer': {'active': True, 'installed': True, 'next_run': None},
            },
            'notifications': {
                'status': 'pass',
                'last_attempt': now - 3 * day,
                'last_success': now - 3 * day,
                'pending': 0,
                'reason': None,
                'timer': {'active': True, 'installed': True, 'next_run': None},
            },
            'media': {
                'status': 'current',
                'age_seconds': 1800,
                'counts': {},
                'checks': [
                    {'id': 'jellyfin', 'name': 'Jellyfin libraries', 'status': 'pass'},
                    {'id': 'seerr', 'name': 'Seerr requests reach Sonarr and Radarr', 'status': 'pass'},
                    {'id': 'prowlarr', 'name': 'Prowlarr search sites', 'status': 'pass'},
                    {'id': 'downloads', 'name': 'Downloads land in the library', 'status': 'pass'},
                ],
            },
            'startup': [{'name': n, 'unit': u, 'active': 'active', 'enabled': True} for n, u in startup],
            'tailscale': {'state': 'Running', 'signed_in_before': True, 'days_left': 160},
            'home_network': {'status': 'none'},
            'setup': {'unfinished': []},
            'assistant': {'set_up': True, 'model': 'qwen3.5:4b', 'memory': '5g', 'cpus': '2', 'question_chars': 500},
            'cards': cards,
            'metrics': {
                'cpu_model': 'Example 8-core processor',
                'cpu_percent': 6.0,
                'memory_percent': 31.0,
                'memory_total': 32e9,
                'memory_used': 9.9e9,
                'temperature': 47.0,
                'uptime_seconds': 12 * day,
            },
            'history': [
                {'at': now - 60 * n, 'cpu': 5 + (n * 7) % 9, 'memory': 30 + n % 3, 'temperature': 45 + n % 4}
                for n in range(30, -1, -1)
            ],
            'nekoserver_release': {},
            'system_updates': {},
            'updates': [],
        }
    )
    return status


def live_readings(now):
    """What /api/live answers (live.rs), for a quiet evening."""
    return {
        'generated_at': now,
        'cpu': {
            'percent': 7.5,
            'name': 'Example 8-core processor',
            'logical_cpus': 16,
            'clock_mhz': 4200,
            'cores': [4, 12, 6, 3, 9, 5, 2, 18, 7, 4, 3, 11, 6, 2, 8, 5],
        },
        'memory': {'percent': 31.0, 'used': 9.9e9, 'total': 32e9, 'swap_used': 0},
        'gpus': [],
        'network': [
            {
                'id': 'enp5s0',
                'kind': 'ethernet',
                'receive_bps': 2.4e6,
                'send_bps': 0.9e6,
                'speed_mbps': 1000,
                'up': True,
                'received': 41e9,
            },
            {
                'id': 'tailscale0',
                'kind': 'tailscale',
                'receive_bps': 0.2e6,
                'send_bps': 1.6e6,
                'up': True,
                'received': 3e9,
            },
        ],
        'disks': [
            {
                'id': 'nvme0n1',
                'kind': 'nvme',
                'model': 'Example NVMe SSD',
                'size': 0.5e12,
                'busy_percent': 1,
                'read_bps': 0.2e6,
                'write_bps': 0.1e6,
            },
            {
                'id': 'nvme1n1',
                'kind': 'nvme',
                'model': 'Example NVMe SSD',
                'size': 1e12,
                'busy_percent': 3,
                'read_bps': 1.1e6,
                'write_bps': 0.4e6,
            },
            {
                'id': 'sda',
                'kind': 'hdd',
                'model': 'Example 8 TB NAS drive',
                'size': 8e12,
                'busy_percent': 6,
                'read_bps': 2.3e6,
                'write_bps': 0,
            },
        ],
        'sensors': [{'name': 'CPU', 'kind': 'cpu', 'celsius': 47.0}],
    }


def media_answer():
    """The Media folders page's answer: the library on the Media HDD, and an older drive's
    films and music used where they are, all connected."""
    kinds = []
    older = {'movies': '/mnt/old-drive/Films', 'music': '/mnt/old-drive/Music'}
    for kind, words, folder in (('movies', 'films', 'movies'), ('tv', 'shows', 'tv'), ('music', 'music', 'music')):
        kinds.append(
            {
                'kind': kind,
                'words': words,
                'library': '/mnt/nekoserver-media/library/' + folder,
                'downloads': True,
                'target': 'library',
                'folders': [{'slot': kind, 'use': 'in_place', 'path': older[kind], 'new_files': False, 'ready': True,
                             'reason': ''}] if kind in older else [],
                'copies': [],
                'room': 7,
                'full': None,
            }
        )  # fmt: skip
    result = {'format': 'neko-media-folders-v1', 'state': 'done', 'message': 'Done.', 'notes': [], 'next': []}
    return {'ok': True, 'working': False, 'result': {**result, 'kinds': kinds}}


ASK = {
    'question': 'Is my server fine? Anything I should do this week?',
    # Short enough to be shown whole (the page folds an answer longer than 240 characters).
    'answer': (
        'Yes, your server is fine. All nine apps are running, both drives passed their health check, '
        "and last night's backups all worked. Nothing needs you this week: Jellyfin 10.11.2 installs "
        'by itself on the next update night.'
    ),
}


# ----------------------------------------------------------------- the dashboard


@contextlib.contextmanager
def dashboard(runtime, folder):
    """The runtime's web server with the made-up readings (kept fresh every 20 s) and a
    stand-in control service that is signed in: yields the page's address."""
    import test_dashboard_browser as browser_tests

    status_file = folder / 'status.json'
    stop = threading.Event()

    def keep_fresh():
        while not stop.is_set():
            data = healthy_status(time.time())
            temporary = status_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(data))
            temporary.replace(status_file)
            stop.wait(20)

    status_file.write_text(json.dumps(healthy_status(time.time())))
    fresher = threading.Thread(target=keep_fresh, daemon=True)
    fresher.start()

    class Control(browser_tests.StandInControl):
        def answer(self, request):
            if request.get('op') == 'app_media_state':
                return media_answer()
            return super().answer(request)

    control = Control(folder / 'control.sock')
    control.start()
    browser_tests.RUNTIME = Path(runtime)
    server, address = browser_tests.serve(status_file, folder / 'control.sock')
    try:
        yield address
    finally:
        stop.set()
        server.terminate()
        server.wait(timeout=10)
        control.server.close()


TOUR = [
    ('home', 'Home', None),
    ('health', 'Server health: drives', None),
    ('backups', 'Backups', None),
    ('backups', 'Backups: checksums and backup cards', 'scroll:#integrity'),
    ('cards', 'Backup cards', None),
    ('media', 'Media folders', None),
    ('ask', 'Ask Neko', 'ask'),
    ('updates', 'Updates', None),
]


def drive(job):
    """Runs make-pictures.mjs with one job; returns what it printed (JSON)."""
    done = subprocess.run(
        ['node', str(DRIVER), json.dumps(job)], capture_output=True, text=True, timeout=600, check=False
    )
    if done.returncode != 0:
        raise SystemExit('The browser step failed:\n' + done.stderr[-4000:])
    return json.loads(done.stdout or 'null')


# The pages both looks walk, in step: (route, what is done there).
SIDE_PAGES = [
    ('home', None),
    ('apps', None),
    ('backups', None),
    ('hardware', 'scroll:section:has(#network)'),  # Network & drives
    ('ask', 'ask'),
    ('home', 'settings'),
]
SIDE_LOOKS = [('neko', 'Moonlit Neko'), ('professional', 'Professional')]


# Pass 1 dark, pass 2 light: both panels always in the same mode.
SIDE_THEMES = ('dark', 'light')


def tour_frame(n):
    return f'tour-{n:02d}.png'


def side_frame(theme, n, look=None):
    """One frame of looks-side-by-side, or (with `look`) one of its two halves."""
    return f'side-{theme}-{n}-{look}.png' if look else f'side-{theme}-{n}.png'


def tour_shots(work):
    return [
        {'route': route, 'look': 'neko', 'theme': 'dark', **MOVING, 'action': action, 'out': str(work / tour_frame(n))}
        for n, (route, _, action) in enumerate(TOUR)
    ]


def side_shots(work):
    return [
        {'route': route, 'look': look, 'theme': theme, **SIDE, 'action': action,
         'out': str(work / side_frame(theme, n, look))}
        for theme in SIDE_THEMES
        for n, (route, action) in enumerate(SIDE_PAGES)
        for look, _ in SIDE_LOOKS
    ]  # fmt: skip


def warn_spoiled(found):
    """A warning for each picture that shows an error, an issue or late readings."""
    for shot in found:
        spoiled = {k: shot[k] for k in ('issues', 'errors', 'pageErrors') if shot[k]}
        if shot['stale']:
            spoiled['stale'] = True
        if spoiled:
            print(f'Warning: {shot["out"]} shows {spoiled}', file=sys.stderr)


def put_side_by_side(work):
    """Each pair of looks into one frame, each look under its name."""
    pages = []
    for theme in SIDE_THEMES:
        for n in range(len(SIDE_PAGES)):
            html = work / f'side-{theme}-{n}.html'
            html.write_text(side_page([work / side_frame(theme, n, look) for look, _ in SIDE_LOOKS]))
            pages.append({'url': html.as_uri(), 'out': str(work / side_frame(theme, n))})
    drive({'kind': 'pages', 'pages': pages, 'width': 2 * PANEL + 3 * 12, 'height': 200, 'full': True})


def capture_dashboard(runtime, work, parts):
    """The dashboard's pictures: the tour and/or the two looks side by side. Returns what
    the browser found on each page."""
    with tempfile.TemporaryDirectory() as folder, dashboard(runtime, Path(folder)) as address:
        shots = (tour_shots(work) if 'tour' in parts else []) + (side_shots(work) if 'looks' in parts else [])
        found = drive({'kind': 'dashboard', 'address': address, 'live': live_readings(time.time()), 'ask': ASK,
                       'shots': shots})  # fmt: skip
    warn_spoiled(found)
    if 'looks' in parts:
        put_side_by_side(work)
    return found


def side_page(images):
    """One frame of looks-side-by-side: the two looks, each under its name."""
    cells = ''.join(
        f'<figure><figcaption>{name}</figcaption><img src="{path.as_uri()}" alt=""></figure>'
        for path, (_, name) in zip(images, SIDE_LOOKS)
    )
    return (
        '<!doctype html><meta charset="utf-8"><style>'
        'html,body{margin:0;background:#101018}'
        f'body{{display:grid;grid-template-columns:{PANEL}px {PANEL}px;gap:12px;padding:12px;width:max-content}}'
        'figure{margin:0;border-radius:10px;overflow:hidden;background:#1c1c28}'
        f'img{{display:block;width:{PANEL}px;height:auto}}'
        'figcaption{font:700 20px/1.2 system-ui,"DejaVu Sans",sans-serif;color:#f0eef6;padding:10px 14px}'
        '</style>' + cells
    )


# ----------------------------------------------------------------- the setup window


def setup_config(folder):
    """A first install's settings (the template), on a PC that has no server yet, whatever
    this one has."""
    import neko_common

    neko_common.server_exists = lambda _config: False
    config = json.loads((SERVER / 'config.json').read_text(encoding='utf-8'))
    path = folder / 'my-config.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    return path, config


FINISH_SUMMARY = {
    'outcome': 'ready',
    'apps': [
        {'id': ident, 'name': name, 'url': f'https://neko.example.ts.net:{port}', 'local_url': '', 'test': test,
         'states': {'installed': {'state': 'done', 'reason': None, 'action': None},
                    'login': {'state': 'done', 'reason': None, 'action': None},
                    'address': {'state': 'done', 'reason': None, 'action': None},
                    'tried': {'state': 'not_tested', 'reason': 'nobody_tried_yet', 'action': None}}}
        for ident, name, port, test in (
            ('jellyfin', 'Jellyfin', 8442, 'play_video'),
            ('immich', 'Immich', 8443, 'upload_photo'),
            ('navidrome', 'Navidrome', 8444, 'play_song'),
            ('nextcloud', 'Nextcloud', 8445, 'upload_file'),
        )
    ],
    'server': {'backup': {'state': 'done', 'reason': None, 'action': None},
               'restore': {'state': 'done', 'reason': None, 'action': None}},
    'tests': [],
    'attention': [],
    'pending': [],
}  # fmt: skip

SETUP_FRAME = {'width': 1000, 'height': 800}
SWITCHED_ON = ('media_photos', 'media_movies', 'media_music', 'nextcloud', 'notifications', 'heartbeat',
               'assistant', 'backup_cards')  # fmt: skip


# The setup window's frames, in the order they show.
SETUP_FRAMES = ('apps', 'review', 'progress', 'question', 'finish')


def setup_frame(name):
    return f'setup-{SETUP_FRAMES.index(name)}-{name}.png'


def capture_setup(work):
    import finish_page
    import questions
    import setup_gui
    import setup_progress
    import wizard

    with tempfile.TemporaryDirectory() as folder:
        path, config = setup_config(Path(folder))
        # The steps a server without a TV runs.
        steps = [name for name in config.get('profiles', {}).get('full', []) if name != 'tv']
        window = setup_gui.Window(path, config, wizard.STEPS, steps, version=VERSION, owners={os.getuid()})
        setup_progress.enable()

        def shoot(name, wait=1800):
            drive(
                {'kind': 'page', 'url': window.url, **SETUP_FRAME, 'wait': wait, 'out': str(work / setup_frame(name))}
            )

        def run_until(stop):
            """Every step before `stop` done (those done already left alone), `stop` running."""
            done = {e['name'] for e in setup_progress.events() if e['kind'] == 'step' and e['status'] == 'success'}
            for name in steps[: steps.index(stop)]:
                if name not in done:
                    setup_progress.step(name, 'running')
                    setup_progress.step(name, 'success')
            setup_progress.step(stop, 'running')

        try:
            # 1. The first page: the apps and extras, with their switches.
            window.show('apps')
            window.choose(dict.fromkeys(SWITCHED_ON, True))
            shoot('apps')
            # 2. The review, with the drives the storage page chose.
            window.show(
                'review',
                [
                    'NekoServer and your apps: drive 2 (Samsung SSD 870, 1 TB, about 5 % full)',
                    'Backups: drive 3 (WDC WD80EFZZ, 8 TB, about 40 % full, holds files)',
                    'Offline backup on a removable drive (optional): drive 4 (SanDisk USB, 64 GB, empty)',
                ],
            )
            shoot('review')
            # 3. Setup at work: the steps done so far, Docker installing its packages.
            window.choose({}, start=True)
            setup_progress.event('plan', steps=steps)
            run_until('docker')
            setup_progress.packages(['docker', 'docker-compose'], ['docker.io', 'docker-compose-v2'])
            setup_progress.note('Downloading the container engine (about 90 MB)…')
            shoot('progress')
            # 4. A question during the phone alerts step, as questions.yes_no asks it.
            setup_progress.step('docker', 'success')
            run_until('notifications')
            release = threading.Event()

            def waiting_prompt(text):
                setup_progress.question(text)
                release.wait(120)
                setup_progress.answered()
                return 'yes'

            asking = threading.Thread(
                target=questions.yes_no,
                args=('alert_arrived', waiting_prompt, lambda *_: None),
                kwargs={'said': 'The alert server accepted the test message.'},
                daemon=True,
            )
            asking.start()
            time.sleep(0.5)
            shoot('question')
            release.set()
            asking.join(10)
            # 5. The finishing page.
            setup_progress.step('notifications', 'success')
            run_until(steps[-1])
            setup_progress.step(steps[-1], 'success')
            wizard.dashboard_port = lambda _config: 8787
            page = finish_page.page(config, FINISH_SUMMARY, pin_set=True)
            setup_progress.event(
                'finished', lines=['NekoServer is ready.'], complete=True, title='NekoServer is ready.', page=page
            )
            shoot('finish', 2500)
        finally:
            window.stop()
            setup_progress.disable()


# ----------------------------------------------------------------- the terminal


def terminal_text():
    """wizard.py's welcome and first page, as a first install prints them."""
    import wizard

    printed = ['$ sudo bash install.sh', '…']
    printed += list(wizard.WELCOME)
    printed.append(
        '\nYour settings are in /home/you/nekoserver/my-config.json (just created for you; you do not need to edit it).'
    )
    with tempfile.TemporaryDirectory() as folder:
        path, _ = setup_config(Path(folder))
        asked = []

        def prompt(text):
            asked.append(text)
            raise EOFError  # nobody answers: the page has been shown, that is all we want

        try:
            wizard.first_page(path, (prompt, printed.append, None, None))
        except EOFError:
            pass
        printed.append(asked[0] if asked else '')
    return '\n'.join(printed).replace('\n\n\n', '\n\n')


TERMINAL_FRAME = 'setup-terminal.png'


def capture_terminal(work):
    import html

    words = html.escape(terminal_text())
    page = work / 'terminal.html'
    page.write_text(
        '<!doctype html><meta charset="utf-8"><style>'
        'html,body{margin:0;background:#16161e}'
        'body{width:1300px;padding:18px;box-sizing:border-box}'
        '.bar{display:flex;gap:8px;padding:10px 14px;background:#24242f;border-radius:10px 10px 0 0}'
        '.bar i{width:13px;height:13px;border-radius:50%;background:#ff5f57}.bar i+i{background:#febc2e}.bar i+i+i{background:#28c840}'
        '.bar b{margin-left:12px;font:600 14px system-ui,"DejaVu Sans",sans-serif;color:#b8b6c6}'
        'pre{margin:0;padding:16px 20px 20px;background:#1e1e28;color:#e6e4ee;border-radius:0 0 10px 10px;'
        'font:15px/1.42 "DejaVu Sans Mono","Liberation Mono",monospace;white-space:pre-wrap}'
        '.cursor{display:inline-block;width:9px;height:17px;background:#e6e4ee;vertical-align:-3px}'
        '</style><div class="bar"><i></i><i></i><i></i><b>NekoServer setup</b></div>'
        f'<pre>{words}<span class="cursor"></span></pre>',
        encoding='utf-8',
    )
    drive({'kind': 'page', 'url': page.as_uri(), 'width': 1300, 'height': 400, 'full': True,
           'out': str(work / TERMINAL_FRAME)})  # fmt: skip


# ----------------------------------------------------------------- assembling


def ffmpeg():
    found = os.environ.get('FFMPEG') or shutil.which('ffmpeg')
    if not found:
        raise SystemExit(
            'ffmpeg was not found. Install ffmpeg (with libwebp), put it on PATH or set FFMPEG=/path/to/ffmpeg, '
            'then run this again with --step assemble --work <the same folder>.'
        )
    return found


# What assemble makes, every name and frame fixed here: plan.json only says which parts
# capture made frames for.
def stills(parts):
    """(picture, frame) for each still."""
    return [('setup-terminal', TERMINAL_FRAME)] if 'terminal' in parts else []


def animations(parts):
    """(picture, [(frame, seconds)], whether a GIF is tried first) for each animation."""
    made = []
    if 'setup' in parts:
        made.append(('setup-window', [(setup_frame(name), 2.0) for name in SETUP_FRAMES], False))
    if 'tour' in parts:
        made.append(('dashboard-tour', [(tour_frame(n), 1.8) for n in range(len(TOUR))], False))
    if 'looks' in parts:
        frames = [(side_frame(theme, n), 2.0) for theme in SIDE_THEMES for n in range(len(SIDE_PAGES))]
        made.append(('looks-side-by-side', frames, True))
    return made


def captured_parts(work):
    plan = json.loads((work / 'plan.json').read_text(encoding='utf-8'))
    return [part for part in PARTS if part in plan['parts']]


# Lossless while it fits (sharp text, no colour fringes); lossy (-q:v, ffmpeg's quality
# for libwebp: its own -quality option is not that) when a picture would be too big.
WEBP_TRIES = (['-lossless', '1'], ['-lossless', '0', '-q:v', '88'], ['-lossless', '0', '-q:v', '75'],
              ['-lossless', '0', '-q:v', '60'])  # fmt: skip


def smallest_webp(before, after, out, most):
    """ffmpeg `before` + each of WEBP_TRIES + `after` into `out`, until it is at most `most` bytes."""
    for how in WEBP_TRIES:
        subprocess.run([*before, *how, *after, str(out)], check=True)
        if out.stat().st_size <= most:
            break
    return out


def make_still(quiet, work, name, frame):
    return smallest_webp(
        [*quiet, '-i', str(work / frame), '-c:v', 'libwebp'], ['-compression_level', '6'], HERE / f'{name}.webp',
        MOST_STILL,
    )  # fmt: skip


def concat_list(work, name, frames):
    """The frames as a list for ffmpeg's concat demuxer, each for its seconds."""
    lines = []
    for frame, seconds in frames:
        lines += [f"file '{(work / frame).as_posix()}'", f'duration {seconds}']
    # The concat demuxer's last line: the last frame stays twice as long before the loop.
    lines.append(f"file '{(work / frames[-1][0]).as_posix()}'")
    listing = work / f'{name}.txt'
    listing.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return listing


def fits_as_gif(source, gif):
    """A GIF where it fits (it plays everywhere): a palette of its own for each frame,
    so the dark and the light frames each keep their colours, and an ordered dither,
    so the Moonlit Neko glow does not band. Each frame once, for as long as it shows
    (no frame rate: repeated frames would each be stored whole with their own palette)."""
    palette = 'split[a][b];[a]palettegen=stats_mode=single[p];[b][p]paletteuse=new=1:dither=bayer:bayer_scale=3'
    subprocess.run(
        [*source, '-filter_complex', palette, '-fps_mode', 'passthrough', '-loop', '0', str(gif)], check=True
    )
    if gif.stat().st_size <= MOST_BYTES:
        return True
    gif.unlink()
    return False


def make_animation(quiet, work, name, frames, gif_first):
    out = HERE / f'{name}.webp'
    source = [*quiet, '-f', 'concat', '-safe', '0', '-i', str(concat_list(work, name, frames))]
    if gif_first and fits_as_gif(source, out.with_suffix('.gif')):
        out.unlink(missing_ok=True)
        return out.with_suffix('.gif')
    return smallest_webp(
        [*source, '-vf', 'fps=10', '-fps_mode', 'vfr', '-c:v', 'libwebp_anim'],
        ['-compression_level', '6', '-loop', '0'], out, MOST_BYTES,
    )  # fmt: skip


def assemble(work):
    parts = captured_parts(work)
    quiet = [ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y']
    made = [make_still(quiet, work, name, frame) for name, frame in stills(parts)]
    made += [make_animation(quiet, work, *animation) for animation in animations(parts)]
    for out in made:
        print(f'{out.relative_to(REPO)}: {out.stat().st_size // 1024} KB')


PARTS = ('terminal', 'setup', 'tour', 'looks')


def check_capture(args):
    """Says what capturing lacks, before minutes of capturing, not after."""
    if args.step == 'all':
        ffmpeg()
    if not sys.platform.startswith('linux') or os.geteuid() == 0:
        raise SystemExit('Capture on Linux, not as root (the dashboard refuses to run as root).')
    if not args.runtime.is_file():
        raise SystemExit(f'No runtime at {args.runtime}: cd nekoserver/runtime && cargo build --release --locked')
    if not shutil.which('node'):
        raise SystemExit('Node was not found (and Playwright with its Chromium is needed too).')


def capture(args, work):
    check_capture(args)
    parts = [part for part in PARTS if part in (args.only or PARTS)]
    plan = {'parts': parts}
    if 'terminal' in parts:
        capture_terminal(work)
    if 'setup' in parts:
        capture_setup(work)
    if 'tour' in parts or 'looks' in parts:
        plan['dashboard'] = capture_dashboard(args.runtime, work, parts)
    (work / 'plan.json').write_text(json.dumps(plan, indent=1), encoding='utf-8')
    print('Frames in', work)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--step', choices=('all', 'capture', 'assemble'), default='all')
    parser.add_argument('--work', type=Path, help='folder for the frames (a temporary one when left out)')
    parser.add_argument(
        '--runtime',
        type=Path,
        default=SERVER / 'runtime/target/release/nekoserver-runtime',
        help='the built nekoserver-runtime',
    )
    parser.add_argument('--only', choices=PARTS, nargs='+', help='make only these pictures (all of them when left out)')
    args = parser.parse_args(argv)
    if args.step != 'all' and not args.work:
        parser.error('--step capture and --step assemble need --work')
    with contextlib.ExitStack() as stack:
        work = args.work or Path(stack.enter_context(tempfile.TemporaryDirectory()))
        work = work.resolve()
        work.mkdir(parents=True, exist_ok=True)
        if args.step in ('all', 'capture'):
            capture(args, work)
        if args.step in ('all', 'assemble'):
            assemble(work)


if __name__ == '__main__':
    main()
