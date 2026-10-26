"""The setup window: a small page in the PC's own browser, beside the terminal that runs
`sudo bash install.sh`. Before setup starts it explains what will be installed and lets
the person switch the optional parts on or off; while setup runs it shows the step under
way, the packages it installs and what each one is for, and asks every question setup
asks (with the terminal's last lines as context) so it can be answered in either place;
at the end it shows the finishing screen.

It is served by setup itself on 127.0.0.1, on a port picked at random, behind a path
nobody can guess (a token), and only for as long as setup runs."""
import ipaddress
import json
import os
import pwd
import re
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import explanations
import guided_setup
import setup_progress
from neko_common import VERSION, atomic_json, find_program, settings_path

ROOT = Path(__file__).resolve().parent
FILES = ROOT / 'setup-gui'
INDEX = 'index.html'
PLAIN = 'text/plain'
JSON = 'application/json'
HTML = 'text/html; charset=utf-8'
STATIC = {'setup.css': ('setup.css', 'text/css; charset=utf-8'),
          'setup.js': ('setup.js', 'text/javascript; charset=utf-8'),
          'logo.svg': ('../dashboard/logo.svg', 'image/svg+xml')}
NOT_FOUND = (404, b'Not found', PLAIN)


def refusal(title, text):
    """A short page that says why the window did not open (a page, so it wraps to the
    browser's width and reads as words, not as code)."""
    page = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width,initial-scale=1"><title>NekoServer setup</title></head><body>'
            f'<h1>{title}</h1><p>{text}</p></body></html>')
    return 403, page.encode(), HTML


# A request that is not the page itself, without this browser's cookie (an older window
# after the address was opened again elsewhere, say), or the page loaded from another
# account than the one that opened the window first.
SPENT = refusal('This setup window is open somewhere else',
                'It was opened again in another browser window, which is the one that works now. Use that one, '
                'or load this address again from the same account on this PC to use it here instead.')
# Another account (or one that can't be told) asks for the page before the window does.
OTHER_ACCOUNT = refusal('Only the account that runs setup can open this window',
                        'Open it from that account\'s desktop on this PC (or as root).')
COOKIE = 'neko_setup'
# The kernel's lists of TCP connections; each line names the account that owns it.
TCP_TABLES = (Path('/proc/net/tcp'), Path('/proc/net/tcp6'))
# The browsers that open a page as a small window of its own (Chromium's --app), in the
# order tried; any other browser gets a normal window.
APP_BROWSERS = ('chromium', 'chromium-browser', 'google-chrome-stable', 'google-chrome', 'brave', 'brave-browser',
                'microsoft-edge-stable', 'microsoft-edge')
WINDOW = ('1180', '480')       # the small window's size; it is placed low on the screen
LARGEST_BODY = 10_000
# The extras setup asks about when it gets to their step, as long as nothing was chosen
# for them (Nextcloud is not asked: it is set up unless switched off).
ASKED_LATER = ('tv', 'notifications', 'heartbeat')


def step_title(name):
    """A step's name in words when it has no title of its own (music_backup: Music backup)."""
    return name.replace('_', ' ').capitalize()


def family_packages():
    """{'names': this Linux's package names for one of NekoServer's names, 'vulkan': the
    graphics driver packages for this PC's chips}. NekoServer's own names (and Intel's
    and AMD's drivers) when this Linux or the chips can't be told."""
    try:
        import host
        names = host.current().names
    except (OSError, RuntimeError, ValueError):
        def names(wanted):
            return [wanted]
    try:
        import gpu
        vulkan = gpu.vulkan_packages(gpu.graphics()) or []
    except (OSError, RuntimeError, ValueError):
        vulkan = ['vulkan-intel', 'lib32-vulkan-intel']
    return {'names': names, 'vulkan': vulkan}


def _package_rows(names):
    return [{'name': p, 'explanation': explanations.PACKAGES.get(p, '')} for p in dict.fromkeys(names)]


def _plain(host):
    """An address as text, with 127.0.0.1 written the IPv6 way (::ffff:127.0.0.1) as plain IPv4."""
    ip = ipaddress.ip_address(host)
    return str(ip.ipv4_mapped or ip) if ip.version == 6 else str(ip)


def table_address(field):
    """(address, port) from the connection tables' "0100007F:1F90" (IPv4) or 32 hex
    digits and a port (IPv6). Each group of 8 digits is 4 bytes in this computer's order."""
    host, _, port = field.partition(':')
    if len(host) not in (8, 32):
        raise ValueError('not an address')
    raw = b''.join(int(host[i:i + 8], 16).to_bytes(4, sys.byteorder) for i in range(0, len(host), 8))
    return _plain(str(ipaddress.ip_address(raw))), int(port, 16)


def _owner_in(line, wanted):
    """The uid column of a table line describing exactly `wanted` (client end, server end)."""
    fields = line.split()
    try:
        return int(fields[7]) if (table_address(fields[1]), table_address(fields[2])) == wanted else None
    except (IndexError, ValueError):
        return None


def peer_uid(client, server, tables=TCP_TABLES):
    """The account that owns the client end of a connection made on this PC: the line of
    the client's own socket (its address is `client`, connected to `server`) in the
    connection tables. None when it is not listed."""
    wanted = ((_plain(client[0]), client[1]), (_plain(server[0]), server[1]))
    for table in tables:
        try:
            lines = Path(table).read_text().splitlines()[1:]
        except OSError:
            continue
        found = next((uid for uid in (_owner_in(line, wanted) for line in lines) if uid is not None), None)
        if found is not None:
            return found
    return None


def expected_owners(config):
    """The accounts whose browser may open the setup window: root (setup itself), the
    account that ran sudo, and the desktop user the window is opened for."""
    owners = {0, os.geteuid()}
    if os.environ.get('SUDO_UID', '').isdigit():
        owners.add(int(os.environ['SUDO_UID']))
    try:
        owners.add(pwd.getpwnam(guided_setup.desktop_command(config, ['true'])[2]).pw_uid)
    except (OSError, RuntimeError, KeyError, ValueError, IndexError):
        pass
    return owners


class Window:
    """The server behind the setup window, running in a thread of setup's own process."""

    def __init__(self, config_path, config, titles, plan, version=VERSION, owners=None, tables=TCP_TABLES):
        self.config_path = Path(config_path)
        self.config = config
        self.titles = dict(titles)
        self.plan = [name for name in plan if name in self.titles]
        self.version = version
        self.token = secrets.token_urlsafe(24)
        # The address (with the token) is in the browser's command line, which any account
        # on this PC can read. So it works once: the first page load gets this session's
        # cookie, and every request after it needs that cookie (a reload in the same
        # browser has it). Another account that reads the address later gets nothing.
        # Another account that reads it first can't take it either: only a connection
        # owned by root or the desktop user (looked up in the kernel's connection
        # tables) may load the page that first time.
        self.session = secrets.token_urlsafe(32)
        self.opened = False
        # The account whose browser opened it: the same account (or root) may open the
        # address again later, in a new browser window, and that one gets a new session;
        # the old window's cookie stops working then (it says so).
        self.claimant = None
        self.owners = expected_owners(config) if owners is None else set(owners)
        self.tables = tables
        self.packages = None                 # this Linux's package names (family_packages), once asked
        self.started = threading.Event()     # Start was pressed in the window
        self.frozen = False                  # setup has started: the choices are fixed
        self.delivered = threading.Event()   # the page has shown the finishing screen
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        self.server.daemon_threads = True
        # Nothing about one request (a window closed mid-reply, a broken one) is printed:
        # the setup terminal is asking its own questions meanwhile.
        self.server.handle_error = lambda request, address: None
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='setup-window')
        self.thread.start()

    @property
    def url(self):
        return 'http://127.0.0.1:' + str(self.server.server_address[1]) + '/t/' + self.token + '/'

    def admit(self, route, cookie, method='GET', owner=lambda: None):
        """(allowed, new session cookie to send or None, the refusal otherwise) for a
        request with this cookie header. The page itself, the first time it is loaded
        (GET) from a connection of an expected account (`owner` looks it up), is let in
        and given the cookie; a request of any other kind never claims it."""
        sent = {}
        for part in (cookie or '').split(';'):
            name, _, value = part.strip().partition('=')
            sent[name] = value
        if secrets.compare_digest(sent.get(COOKIE, ''), self.session):
            return True, None, None
        if method != 'GET' or route not in ('', INDEX):
            return False, None, SPENT
        uid = owner()
        if uid not in self.owners:
            return False, None, (SPENT if self.opened else OTHER_ACCOUNT)
        with self.lock:
            if not self.opened:
                self.opened, self.claimant = True, uid
                return True, self.session, None
            if uid in (self.claimant, 0):
                # Opened again (the window was closed, or a browser lost its cookie when it
                # closed): a new session for this browser, the old one's ends.
                self.session = secrets.token_urlsafe(32)
                return True, self.session, None
        return False, None, SPENT

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ----- what the page shows

    def _steps(self):
        """Every step of the plan with its latest state, packages and notes. Once the
        installer has said which steps it runs (its plan, with the steps the others need,
        such as the music backup), those are the list: the same steps the terminal and
        the log count."""
        latest, packages, notes = {}, {}, {}
        for record in setup_progress.events():
            if record['kind'] == 'step':
                latest[record['name']] = record
            elif record['kind'] == 'packages' and record.get('step'):
                packages.setdefault(record['step'], []).extend(record['wanted'])
            elif record['kind'] == 'note' and record.get('step'):
                notes.setdefault(record['step'], []).append(record['text'])
        ran = setup_progress.last('plan')
        plan = list(dict.fromkeys([*(ran['steps'] if ran else ()), *self.plan]))
        return [{'id': name, 'title': self.titles.get(name) or step_title(name),
                 'explanation': explanations.STEP_EXPLANATIONS.get(name, ''),
                 'status': latest.get(name, {}).get('status', 'pending'),
                 'reason': latest.get(name, {}).get('reason', ''),
                 'packages': _package_rows(packages.get(name, [])),
                 'notes': notes.get(name, [])[-6:]} for name in plan]

    def _extras(self):
        with self.lock:
            return [{'id': e['id'], 'title': e['title'], 'explanation': e['explanation'],
                     'installs': self._installs(e),
                     'on': explanations.extra_state(self.config, e),
                     'asked': e['id'] in ASKED_LATER and not isinstance(self.config.get(e['section'], {}).get(e['key']), bool)}
                    for e in explanations.EXTRAS]

    def _installs(self, extra):
        """What an extra installs, as this Linux names the packages (Ubuntu's
        mesa-vulkan-drivers, not Arch's vulkan-intel), and for the TV the graphics driver
        of this PC's own chip (as the TV step picks it), each with what it is for."""
        if self.packages is None:
            self.packages = family_packages()
        names = list(extra['installs'])
        if extra['id'] == 'tv':
            names = [n for n in names if 'vulkan' not in n]
            names[2:2] = self.packages['vulkan']
        rows, seen = [], set()
        for name in names:
            for own in self.packages['names'](name):
                if own not in seen:
                    seen.add(own)
                    rows.append({'name': own, 'explanation': explanations.PACKAGES.get(name, '')})
        return rows

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
        # Counted as the terminal's summary and the log count them: steps done, out of the
        # steps this PC can have (one not available here counts in neither).
        here = [s for s in steps if s['status'] != 'unavailable']
        return {'version': self.version, 'phase': phase, 'extras': self._extras(), 'steps': steps,
                'current': current['id'] if current else None,
                'done': sum(1 for s in here if s['status'] == 'success'), 'total': len(here),
                'question': question, 'terminal': setup_progress.terminal_handoff(),
                'finish': finished['lines'] if finished else [],
                'title': (finished.get('title') or '') if finished else '',
                'stopped': bool(finished and finished.get('stopped') is True),
                'complete': bool(finished and finished.get('complete') is True)}

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
        if route in ('', INDEX):
            return 200, (FILES / INDEX).read_bytes(), HTML
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
                port = str(window.server.server_address[1])
                # Only this PC's own address: a web page elsewhere can't reach it by a
                # name it points at 127.0.0.1 (DNS rebinding).
                if self.headers.get('Host') not in ('127.0.0.1:' + port, 'localhost:' + port):
                    return None
                if not self.path.startswith(prefix):
                    return None
                return self.path[len(prefix):].split('?', 1)[0]

            def handle_one_request(self):
                # A window closed in the middle of a reply is not an error to print on
                # the setup terminal (in the middle of a question, say).
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    self.close_connection = True

            def admitted(self, route):
                def owner():
                    return peer_uid(self.client_address, self.connection.getsockname(), window.tables)
                allowed, given, self.refusal = window.admit(route, self.headers.get('Cookie'), self.command, owner)
                self.given = given
                return allowed

            def reply(self, answer):
                code, body, kind = answer
                self.send_response(code)
                if getattr(self, 'given', None):
                    self.send_header('Set-Cookie', f'{COOKIE}={self.given}; Path=/t/{window.token}/; HttpOnly; SameSite=Strict')
                self.send_header('Content-Type', kind)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:")
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                route = self.route()
                if route is None:
                    return self.reply(NOT_FOUND)
                self.reply(window.get(route) if self.admitted(route) else self.refusal)

            def do_POST(self):
                route = self.route()
                if route is None:
                    return self.reply(NOT_FOUND)
                if not self.admitted(route):
                    return self.reply(self.refusal)
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    return self.reply((400, b'Bad length', PLAIN))
                if length < 0:
                    return self.reply((400, b'Bad length', PLAIN))
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


# ----- setup stopped before the wizard

STOPPED_TITLE = 'Setup stopped before it began'
STOP_WAIT = 30      # seconds the window may take to open and show it
NOTHING_TOUCHED = 'Nothing on your drives was touched.'
AGAIN = 'Once that is fixed, run  sudo bash install.sh  again.'


def stop_lines(reason):
    """The stopped window's words: the reason once, and what to do once (the reason
    usually says it already: "…then run  sudo bash install.sh  again")."""
    return [reason, '', NOTHING_TOUCHED + ('' if 'install.sh' in reason else ' ' + AGAIN)]


def _settings(path):
    """The settings (who the desktop user is), or {} when there are none yet."""
    try:
        value = json.loads(settings_path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, RuntimeError):
        return {}
    return value if isinstance(value, dict) else {}


def show_stopped(note, settings=ROOT / 'my-config.json', output=print, wait=STOP_WAIT, opener=None):
    """Setup stopped before the wizard (install.sh's package step: a site that could not be
    reached, say). Its reason, the file `note` host.py wrote, is shown in the setup window
    too, on a PC with a desktop: the window otherwise opens only after the packages and
    the build. Served until the window has shown it (its finishing screen then stays on
    screen), at most `wait` seconds. True when the window showed it."""
    try:
        text = Path(note).read_text(encoding='utf-8').strip()
    except (OSError, UnicodeDecodeError):
        return False
    if not text:
        return False
    config = _settings(settings)
    setup_progress.enable()
    try:
        window = Window(settings, config, {}, [])
    except OSError:
        setup_progress.disable()
        return False
    try:
        window.freeze()
        setup_progress.event('finished', title=STOPPED_TITLE, lines=stop_lines(text), complete=False, stopped=True)
        if not (opener or open_window)(config, window.url):
            return False
        output('The setup window shows this too.')
        shown = window.delivered.wait(wait)
        if shown:
            window.delivered.clear()
            window.delivered.wait(2)       # the page's own files (its style, the logo) are fetched too
        return shown
    finally:
        window.stop()
        setup_progress.disable()


def main(argv):
    if len(argv) != 2 or argv[0] != 'stopped':
        print('usage: python3 setup_gui.py stopped <file with the reason>', file=sys.stderr)
        return 2
    show_stopped(argv[1])
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
