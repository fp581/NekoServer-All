"""The setup window: a small page in the PC's own browser, beside the terminal that runs
`sudo bash install.sh`. Before setup starts it explains what will be installed and lets
the person switch the optional parts on or off; while setup runs it shows the step under
way, the packages it installs and what each one is for, and asks every question setup
asks (with the terminal's last lines as context) so it can be answered in either place;
at the end it shows the finishing screen.

It is served by setup itself on 127.0.0.1, on a port picked at random, behind a path
nobody can guess (a token), and only for as long as setup runs."""
import json
import os
import re
import secrets
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import explanations
import guided_setup
import setup_progress
from neko_common import VERSION, atomic_json, find_program, settings_path

ROOT = Path(__file__).resolve().parent
FILES = ROOT / 'setup-gui'
PLAIN = 'text/plain'
JSON = 'application/json'
HTML = 'text/html; charset=utf-8'
STATIC = {'setup.css': ('setup.css', 'text/css; charset=utf-8'),
          'setup.js': ('setup.js', 'text/javascript; charset=utf-8'),
          'logo.svg': ('../dashboard/logo.svg', 'image/svg+xml')}
NOT_FOUND = (404, b'Not found', PLAIN)
# The browsers that open a page as a small window of its own (Chromium's --app), in the
# order tried; any other browser gets a normal window.
APP_BROWSERS = ('chromium', 'chromium-browser', 'google-chrome-stable', 'google-chrome', 'brave', 'brave-browser',
                'microsoft-edge-stable', 'microsoft-edge')
WINDOW = ('1180', '480')       # the small window's size; it is placed low on the screen
LARGEST_BODY = 10_000
SETTLED = ('success', 'unavailable')


def _package_rows(names):
    return [{'name': p, 'explanation': explanations.PACKAGES.get(p, '')} for p in dict.fromkeys(names)]


class Window:
    """The server behind the setup window, running in a thread of setup's own process."""

    def __init__(self, config_path, config, titles, plan, version=VERSION):
        self.config_path = Path(config_path)
        self.config = config
        self.titles = dict(titles)
        self.plan = [name for name in plan if name in self.titles]
        self.version = version
        self.token = secrets.token_urlsafe(24)
        self.started = threading.Event()     # Start was pressed in the window
        self.frozen = False                  # setup has started: the choices are fixed
        self.delivered = threading.Event()   # the page has shown the finishing screen
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='setup-window')
        self.thread.start()

    @property
    def url(self):
        return 'http://127.0.0.1:' + str(self.server.server_address[1]) + '/t/' + self.token + '/'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ----- what the page shows

    def _steps(self):
        """Every step of the plan with its latest state, packages and notes."""
        latest, packages, notes = {}, {}, {}
        for record in setup_progress.events():
            if record['kind'] == 'step':
                latest[record['name']] = record
            elif record['kind'] == 'packages' and record.get('step'):
                packages.setdefault(record['step'], []).extend(record['wanted'])
            elif record['kind'] == 'note' and record.get('step'):
                notes.setdefault(record['step'], []).append(record['text'])
        return [{'id': name, 'title': self.titles[name],
                 'explanation': explanations.STEP_EXPLANATIONS.get(name, ''),
                 'status': latest.get(name, {}).get('status', 'pending'),
                 'reason': latest.get(name, {}).get('reason', ''),
                 'packages': _package_rows(packages.get(name, [])),
                 'notes': notes.get(name, [])[-6:]} for name in self.plan]

    def _extras(self):
        with self.lock:
            return [{'id': e['id'], 'title': e['title'], 'explanation': e['explanation'],
                     'installs': _package_rows(e['installs']),
                     'on': explanations.extra_state(self.config, e)} for e in explanations.EXTRAS]

    def _phase(self, finished, steps):
        if finished:
            return 'finished'
        if self.frozen or self.started.is_set() or any(s['status'] != 'pending' for s in steps):
            return 'running'
        return 'choose'

    def state(self):
        steps = self._steps()
        finished = setup_progress.last('finished')
        phase = self._phase(finished, steps)
        current = next((s for s in steps if s['status'] == 'running'), None)
        question = setup_progress.pending_question()
        if question is not None:
            question = {'seq': question['seq'], 'text': question['text'], 'secret': question['secret'],
                        'kind': question.get('form', 'text'), 'context': question.get('context', [])}
        if phase == 'finished':
            self.delivered.set()
        return {'version': self.version, 'phase': phase, 'extras': self._extras(), 'steps': steps,
                'current': current['id'] if current else None,
                'done': sum(1 for s in steps if s['status'] in SETTLED), 'total': len(steps),
                'question': question, 'terminal': setup_progress.terminal_handoff(),
                'finish': finished['lines'] if finished else []}

    def freeze(self):
        """Setup has started (in the terminal or the window): from now on the choices
        are fixed, so a late request from the window cannot change the settings file
        the installer is writing to."""
        with self.lock:
            self.frozen = True

    def _saved(self):
        """The settings file as it is now (what the installer may have added since the
        window opened), or the window's copy when it cannot be read."""
        try:
            value = json.loads(settings_path(self.config_path).read_text(encoding='utf-8'))
        except (OSError, ValueError, RuntimeError):
            return self.config
        return value if isinstance(value, dict) else self.config

    def choose(self, changes, start=False):
        """Switch extras on or off, saved to the settings file at once (owner kept):
        only the switches change, on the file as it is now. False once setup has
        started; with start, the choices are saved and setup starts in one step."""
        by_id = {e['id']: e for e in explanations.EXTRAS}
        chosen = {k: v for k, v in changes.items() if k in by_id and isinstance(v, bool)}
        with self.lock:
            if self.frozen:
                return False
            if chosen:
                config = self._saved()
                for key, value in chosen.items():
                    explanations.set_extra(config, by_id[key], value)
                owner = (self.config_path.stat().st_uid, self.config_path.stat().st_gid) if self.config_path.exists() else None
                atomic_json(self.config_path, config)
                if owner is not None and os.geteuid() == 0:
                    os.chown(self.config_path, *owner)
                self.config = config
            if start:
                self.frozen = True
                self.started.set()
        if chosen:
            setup_progress.event('chosen', **chosen)
        return True

    # ----- the requests

    def get(self, route):
        """(status, body, content type) for a GET inside the token's path."""
        if route in ('', 'index.html'):
            return 200, (FILES / 'index.html').read_bytes(), HTML
        if route.startswith('static/') and route[7:] in STATIC:
            name, kind = STATIC[route[7:]]
            return 200, (FILES / name).read_bytes(), kind
        if route == 'api/state':
            return 200, json.dumps(self.state()).encode(), JSON
        return NOT_FOUND

    def post(self, route, body):
        """(status, body, content type) for a POST with its JSON body."""
        if route == 'api/choose' and isinstance(body, dict):
            return self._fixed_or_ok(self.choose(body))
        if route == 'api/start':
            choices = body.get('choices') if isinstance(body, dict) else None
            return self._fixed_or_ok(self.choose(choices if isinstance(choices, dict) else {}, start=True))
        if route == 'api/answer' and isinstance(body, dict):
            # The window's answer to the question setup is waiting on; the terminal may
            # have answered first, in which case this says so.
            taken = (isinstance(body.get('seq'), int) and isinstance(body.get('text'), str)
                     and len(body['text']) <= 1000 and setup_progress.answer_from_window(body['seq'], body['text']))
            return 200, json.dumps({'ok': bool(taken)}).encode(), JSON
        return NOT_FOUND

    @staticmethod
    def _fixed_or_ok(taken):
        if taken:
            return 200, b'{"ok": true}', JSON
        return 409, b'{"ok": false, "reason": "Setup has started, so the choices are fixed."}', JSON

    # ----- the server

    def _handler(self):
        window = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'NekoServerSetup/' + window.version
            sys_version = ''

            def log_message(self, *args):   # nothing on the terminal, nothing in a log
                pass

            def route(self):
                prefix = '/t/' + window.token + '/'
                if not self.path.startswith(prefix):
                    return None
                return self.path[len(prefix):].split('?', 1)[0]

            def reply(self, answer):
                code, body, kind = answer
                self.send_response(code)
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:")
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                route = self.route()
                self.reply(NOT_FOUND if route is None else window.get(route))

            def do_POST(self):
                route = self.route()
                if route is None:
                    return self.reply(NOT_FOUND)
                length = int(self.headers.get('Content-Length') or 0)
                if length > LARGEST_BODY:
                    return self.reply((413, b'Too large', PLAIN))
                try:
                    body = json.loads(self.rfile.read(length) or b'{}')
                except ValueError:
                    return self.reply((400, b'Bad JSON', PLAIN))
                self.reply(window.post(route, body))

        return Handler


SCREEN = re.compile(r'current (\d+) x (\d+)')   # xrandr's line for the whole screen


def screen_size(config, run=subprocess.run):
    """(width, height) of the desktop user's screen from xrandr, or None (Wayland without
    xrandr, no desktop, a tool missing): the window is then left where the desktop puts it."""
    xrandr = find_program('xrandr')
    if not xrandr:
        return None
    try:
        result = run(guided_setup.desktop_command(config, [xrandr, '--current']), capture_output=True, text=True,
                     timeout=10, check=False)
    except (OSError, RuntimeError, KeyError, ValueError, subprocess.SubprocessError):
        return None
    match = SCREEN.search(result.stdout or '')
    return (int(match[1]), int(match[2])) if match and result.returncode == 0 else None


def window_position(screen, size=WINDOW, margin=60):
    """Where the small window goes: low on the screen, a little in from the left, and
    always wholly on the screen (a laptop's 768 rows are not a monitor's 1080)."""
    width, height = screen
    x = margin if width > int(size[0]) + margin else 0
    y = max(0, height - int(size[1]) - margin)
    return str(x) + ',' + str(y)


def open_window(config, url):
    """Open the page as a small window of the desktop user's browser. False when there is
    no desktop or no browser to open it with (setup goes on in the terminal alone)."""
    browser = next((found for found in (find_program(name) for name in APP_BROWSERS) if found), None)
    if browser:
        argv = [browser, '--app=' + url, '--window-size=' + ','.join(WINDOW), '--no-first-run', '--no-default-browser-check']
        screen = screen_size(config)
        if screen:
            argv.append('--window-position=' + window_position(screen))
    elif find_program('firefox'):
        argv = [find_program('firefox'), '--new-window', url, '--width', WINDOW[0], '--height', WINDOW[1]]
    elif find_program('xdg-open'):
        argv = [find_program('xdg-open'), url]
    else:
        return False
    try:
        command = guided_setup.desktop_command(config, argv)
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True, cwd='/')
        try:
            return child.wait(timeout=2) == 0
        except subprocess.TimeoutExpired:
            return True
    except (OSError, RuntimeError, KeyError, ValueError):
        return False
