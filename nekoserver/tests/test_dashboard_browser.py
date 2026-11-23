"""The dashboard in a real browser: the runtime's own web server (runtime/bin) serves
dashboard/ and a stand-in snapshot, and Chromium (Playwright) opens it. Skipped where
Node, Playwright, Chromium or a built runtime is missing, and as root (the web server
refuses to run as root)."""

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSER = Path(__file__).resolve().parent / 'dashboard_browser.mjs'
RUNTIME = ROOT / 'runtime/bin/nekoserver-runtime'
NODE = shutil.which('node')


def playwright_works():
    if not NODE or not RUNTIME.is_file() or os.geteuid() == 0:
        return False
    probe = "require('playwright').chromium.launch().then(b => b.close())"
    try:
        return subprocess.run([NODE, '-e', probe], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def snapshot():
    """A server with ten apps, two drives (one nearly full, with a SMART warning) and an
    app update waiting: enough for every page to have its cards and buttons."""
    now = time.time()
    names = ['Jellyfin', 'Immich', 'Navidrome', 'Nextcloud', 'Seerr', 'Sonarr', 'Radarr', 'Lidarr', 'Prowlarr']
    apps = [{'id': n.lower(), 'name': n, 'state': 'running', 'responding': True} for n in names]
    return {
        'format': 'neko-dashboard-v1',
        'generated_at': now,
        'late_after': 630,
        'version': '0.51.2',
        'acceptance': {'status': 'complete'},
        'applications': apps,
        'storage': [
            {'id': 'data_ssd', 'name': 'Application SSD', 'status': 'mounted', 'total': 1e12, 'used': 4e11},
            {
                'id': 'media_hdd',
                'name': 'Media HDD',
                'status': 'mounted',
                'total': 8e12,
                'used': 7.7e12,
                'used_percent': 96,
                'smart': {'status': 'warning', 'reason': 'Reallocated sectors: 8'},
            },
        ],
        'heartbeat': {'status': 'pass'},
        'notifications': {'status': 'pass'},
        'startup': [{'unit': 'docker.service', 'name': 'Docker', 'enabled': False}],
        'waiting_updates': {
            'settle_days': 7,
            'apps': [
                {
                    'app': 'jellyfin',
                    'installed': '10.11.1',
                    'waiting': [
                        {'version': '10.11.2', 'first_seen': now - 9 * 86400, 'installs_from': now - 2 * 86400},
                        {'version': '10.11.3', 'first_seen': now - 86400, 'installs_from': now + 6 * 86400},
                    ],
                }
            ],
        },
    }


def media_answer():
    """media_locations.py's last answer, as the stand-in control service hands it over."""
    films = {
        'kind': 'movies',
        'words': 'films',
        'library': '/mnt/nekoserver-media/library/movies',
        'downloads': True,
        'target': 'library',
        'folders': [
            {
                'slot': 'movies',
                'use': 'in_place',
                'path': '/mnt/nas/a-very-long-shared-folder-name-from-the-nas/films',
                'new_files': False,
                'ready': False,
                'reason': 'Your films are not there now (nas:/volume1/a-very-long-shared-folder-name is not connected).',
            }
        ],
        'copies': [],
        'room': 7,
        'full': None,
    }
    shows = {**films, 'kind': 'tv', 'words': 'shows', 'folders': [], 'room': 8}
    result = {'format': 'neko-media-folders-v1', 'state': 'done', 'message': 'Done.', 'notes': [], 'next': []}
    return {'ok': True, 'working': False, 'result': {**result, 'kinds': [films, shows]}}


class StandInControl(threading.Thread):
    """The control service, signed in, answering the Media folders page; every request
    it was given is kept in `asked`."""

    BROWSE = {
        '': {
            'ok': True,
            'path': None,
            'parent': None,
            'more': False,
            'folders': [{'name': '/mnt/nas', 'path': '/mnt/nas', 'mount': True, 'share': True}],
        },
        '/mnt/nas': {
            'ok': True,
            'path': '/mnt/nas',
            'parent': '/mnt',
            'more': False,
            'folders': [{'name': 'films', 'path': '/mnt/nas/films', 'mount': False}],
        },
    }

    def __init__(self, path):
        super().__init__(daemon=True)
        self.asked = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(16)

    def answer(self, request):
        op = request.get('op')
        if op == 'status':
            access = {'local': True, 'pin_set': True, 'unlocked': True, 'signed_in': True}
            return {
                'ok': True,
                'format': 'neko-control-v1',
                'fans': {'enabled': False, 'outputs': []},
                'lights': {'status': 'disabled', 'devices': []},
                'access': access,
                'apps': {'available': True, 'actions': []},
            }
        if op == 'app_media_state':
            return media_answer()
        if op == 'app_media_browse':
            return self.BROWSE.get(request.get('path') or '', {'ok': False, 'error': 'E42', 'message': 'No.'})
        return {'ok': True, 'id': 'abc', 'message': 'Working on it.'}

    def run(self):
        while True:
            try:
                connection, _ = self.server.accept()
            except OSError:
                return
            with connection, connection.makefile('rw') as stream:
                request = json.loads(stream.readline())
                self.asked.append(request)
                stream.write(json.dumps(self.answer(request)) + '\n')
                stream.flush()


def serve(status_file, control):
    """The runtime's web server for the page, on a free port: (process, address)."""
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    command = [str(RUNTIME), 'serve', '--assets', str(ROOT / 'dashboard'), '--status', str(status_file)]
    command += ['--port', str(port), '--control', str(control)]
    server = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            socket.create_connection(('127.0.0.1', port), timeout=1).close()
            break
        except OSError:
            time.sleep(0.05)
    return server, f'http://127.0.0.1:{port}/'


@unittest.skipUnless(playwright_works(), 'no browser here (Node, Playwright, Chromium or a built runtime missing)')
class DashboardInBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        status = Path(cls.folder.name) / 'status.json'
        status.write_text(json.dumps(snapshot()))
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        command = [str(RUNTIME), 'serve', '--assets', str(ROOT / 'dashboard'), '--status', str(status)]
        command += ['--port', str(port), '--control', str(Path(cls.folder.name) / 'no-control.sock')]
        cls.server = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.address = f'http://127.0.0.1:{port}/'
        for _ in range(100):
            try:
                socket.create_connection(('127.0.0.1', port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.folder.cleanup()

    def browse(self, *checks):
        result = subprocess.run(
            [NODE, str(BROWSER), self.address, json.dumps(checks)],
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )
        return json.loads(result.stdout)

    def test_keyboard_focus_stays_put_when_the_page_refreshes(self):
        # Deep check 156: the page redraws itself every few seconds; a keyboard user who
        # had Tabbed to an app's ⋯ (Your apps, every 3 s) or an update's button (Updates,
        # every 15 s) was sent back to the top of the page each time.
        checks = [
            ('apps', 'Actions for Sonarr', 'control'),
            ('home', 'Actions for Immich', 'control'),
            ('updates', 'Install at the next update night', 'status'),
            ('updates', 'Check for updates', 'status'),
            ('media', 'Unlock media folders', 'control'),
        ]
        answers = self.browse(*[('focusAfterRefresh', {'route': r, 'label': l, 'reload': k}) for r, l, k in checks])
        for (route, label, _), answer in zip(checks, answers):
            with self.subTest(route=route, label=label):
                self.assertEqual(answer, {'before': label, 'after': label})

    def test_a_phone_at_200_percent_zoom_needs_no_sideways_scrolling(self):
        # Deep check 170: a 390 px phone at 200% zoom is 195 CSS pixels wide. The cards'
        # badges ("Won't start after a restart", "Not checked yet") stuck out of their
        # cards, and the page scrolled sideways (Server health 281 px, Backups 246 px).
        routes = ['home', 'apps', 'backups', 'hardware', 'sensors', 'cooling', 'processes', 'health', 'updates']
        routes += ['media', 'attention', 'help']
        for width in (390, 320, 195):
            answers = self.browse(*[('pageWidth', {'route': r, 'width': width}) for r in routes])
            for route, (scroll, window, overlap) in zip(routes, answers):
                with self.subTest(route=route, width=width):
                    self.assertLessEqual(scroll, window)
                    self.assertFalse(overlap, "the title of the page is under its buttons")


@unittest.skipUnless(playwright_works(), 'no browser here (Node, Playwright, Chromium or a built runtime missing)')
class MediaFoldersInBrowserTests(unittest.TestCase):
    """The Media folders page, signed in (a stand-in control service answers it)."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        status = Path(cls.folder.name) / 'status.json'
        status.write_text(json.dumps(snapshot()))
        cls.control = StandInControl(Path(cls.folder.name) / 'control.sock')
        cls.control.start()
        cls.server, cls.address = serve(status, Path(cls.folder.name) / 'control.sock')

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.control.server.close()
        cls.folder.cleanup()

    browse = DashboardInBrowserTests.browse

    def test_a_folder_is_picked_and_added_with_the_keyboard_alone(self):
        (answer,) = self.browse(('mediaKeyboard', {'width': 390}))
        self.assertEqual(answer['picked'], '/mnt/nas')
        self.assertEqual(
            answer['steps'],
            ['media-pick', 'Open /mnt/nas', 'media-use', 'media-path', 'media-downloads', 'Connect folder'],
        )
        sent = [r for r in self.control.asked if r.get('op') == 'app_media_change' and r.get('change') == 'add']
        self.assertEqual(
            {k: sent[-1][k] for k in ('kind', 'path', 'how', 'downloads')},
            {'kind': 'movies', 'path': '/mnt/nas/new films', 'how': 'in_place', 'downloads': True},
        )

    def test_the_page_fits_a_phone(self):
        for width in (390, 320):
            (scroll, window, overlap) = self.browse(('pageWidth', {'route': 'media', 'width': width}))[0]
            with self.subTest(width=width):
                self.assertLessEqual(scroll, window)
                self.assertFalse(overlap)


# Built-ins newer than what the page needs. All of it works from Chrome 98, Firefox 98 and
# Safari 15.4 (structuredClone, for a fan curve, and <dialog>, for the PIN); Ask Neko from
# Chrome 93 (Object.hasOwn). Taken out, the page must work the same.
NEWER_BUILTINS = [
    'Array.prototype.findLast',
    'Array.prototype.findLastIndex',
    'Array.prototype.toSorted',
    'Array.prototype.toReversed',
    'Array.prototype.toSpliced',
    'Array.prototype.with',
    'Array.fromAsync',
    'Object.groupBy',
    'Map.groupBy',
    'Promise.withResolvers',
    'String.prototype.isWellFormed',
    'String.prototype.toWellFormed',
    'Set.prototype.union',
    'Set.prototype.intersection',
    'Set.prototype.difference',
]


@unittest.skipUnless(playwright_works(), 'no browser here (Node, Playwright, Chromium or a built runtime missing)')
class OlderBrowserTests(unittest.TestCase):
    """The page in a browser that lacks the newest built-ins, signed in, with the assistant set up."""

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory()
        status = Path(cls.folder.name) / 'status.json'
        assistant = {'set_up': True, 'model': 'qwen3:4b', 'memory': '6g', 'cpus': 2}
        status.write_text(json.dumps({**snapshot(), 'assistant': assistant}))
        cls.control = StandInControl(Path(cls.folder.name) / 'control.sock')
        cls.control.start()
        cls.server, cls.address = serve(status, Path(cls.folder.name) / 'control.sock')

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)
        cls.control.server.close()
        cls.folder.cleanup()

    browse = DashboardInBrowserTests.browse
    maxDiff = None

    def test_ask_neko_sends_the_question_without_array_find_last(self):
        # 0.52.2 test round (T3-01): a clean-up gave Ask Neko Array findLast (Chrome 97,
        # Firefox 104). In Chrome 93-96 and Firefox 98-103, pressing Ask emptied the box and
        # sent nothing.
        (answer,) = self.browse(('askSent', {'missing': ['Array.prototype.findLast']}))
        self.assertEqual(answer['errors'], [])
        self.assertEqual(
            answer['sent'],
            [
                {'question': 'Is my server fine?'},
                {
                    'question': 'And how do I check?',
                    'before': {'question': 'Is my server fine?', 'answer': 'An answer.'},
                },
            ],
        )

    def test_no_page_needs_a_built_in_newer_than_the_page_does(self):
        routes = ['home', 'ask', 'apps', 'backups', 'hardware', 'sensors', 'cooling', 'processes', 'health']
        routes += ['updates', 'media', 'cards', 'attention', 'help']
        (errors,) = self.browse(('pageErrors', {'routes': routes, 'missing': NEWER_BUILTINS}))
        self.assertEqual(errors, {route: [] for route in routes})
