"""What setup is doing right now, as events the setup window shows: steps starting and
ending, packages being installed, a question waiting, the finishing screen. Kept in
memory for the window's server; also appended to a file when enabled.

A question setup asks in the terminal is asked in the window too, with the terminal's
last lines as its context (the list of drives to choose from, say), and the answer can
come from either: the first one wins and the other is told. Nothing secret is kept: an
answer is never recorded, and a hidden entry (a password, a PIN) is reported only as
"a hidden entry"; its answer from the window travels on this PC's loopback alone."""
import builtins
import collections
import contextlib
import getpass
import json
import os
import re
import select
import sys
import termios
import threading
import time

_events = []
_lock = threading.Lock()
_path = None
_step = None          # the step running now
_recent = collections.deque(maxlen=40)   # the terminal's last lines, for a question's context
_pending = None       # {'seq', 'event', 'answer'} while a question waits
CONTEXT_LINES = 14
POLL = 0.3            # seconds between looks at the terminal and the window while waiting


def enable(path=None):
    """Start recording (optionally to a file, 0600, one JSON object per line)."""
    global _path
    with _lock:
        _events.clear()
        _recent.clear()
        _path = str(path) if path else None
        if _path:
            with open(_path, 'w', encoding='utf-8') as handle:
                os.fchmod(handle.fileno(), 0o600)


def disable():
    global _path, _step, _pending
    with _lock:
        _events.clear()
        _recent.clear()
        _path = None
        _step = None
        _pending = None


def event(kind, **fields):
    """Record one event; returns it. `step` names the step running at the time."""
    with _lock:
        record = {'seq': len(_events) + 1, 'at': time.time(), 'kind': kind, 'step': _step, **fields}
        _events.append(record)
        if _path:
            with open(_path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(record) + '\n')
    return record


def events(since=0):
    """Every event with a sequence number above `since`."""
    with _lock:
        return [e for e in _events if e['seq'] > since]


def last(kind):
    with _lock:
        return next((e for e in reversed(_events) if e['kind'] == kind), None)


def step(name, status, reason=''):
    """A step started ('running') or ended (success, needs_setup, unavailable, failed,
    blocked)."""
    global _step
    with _lock:
        _step = name if status == 'running' else None
    if status == 'running':
        _cut()   # a question in this step shows what this step said, not the one before
    event('step', name=name, status=status, reason=str(reason or '')[:400])


def packages(wanted, names=()):
    """Packages being installed, by NekoServer's names and the family's own."""
    event('packages', wanted=list(wanted), names=list(names))


def note(text):
    """A line of the step's own log."""
    event('note', text=str(text)[:600])


# ----- questions

def question_kind(text, secret):
    if secret:
        return 'secret'
    return 'yesno' if '[yes/no]' in text else 'text'


CUT = '\x00'   # in _recent: where the last answered question, or the step before, ended
STAMP = re.compile(r'\[\d{4}-\d\d-\d\dT[^\]]*\] ')
MARKERS = ('START ', 'PASS ', 'FAILED ', 'NEEDS SETUP ', 'UNAVAILABLE ', 'BLOCKED ')   # the installer's step lines


def _cut():
    """The prompt just answered (written without a newline, so still pending in the
    terminal's tee) goes before the cut, not after it."""
    tee = sys.stdout
    with _lock:
        if isinstance(tee, _Tee) and tee._partial.strip():
            _recent.append(tee._partial[:300])
            tee._partial = ''
        _recent.append(CUT)


def question_context():
    """What the terminal said since the last question was answered (or since the step
    started): the drives it listed, the step's own explanation. The installer's time
    stamps are taken off its lines, its step markers left out, and a heading
    ("=== ... ===") within that span starts the context."""
    with _lock:
        raw = list(_recent)
    if CUT in raw:
        raw = raw[len(raw) - raw[::-1].index(CUT):]
    lines = []
    for line in raw:
        line = STAMP.sub('', line).rstrip()
        if line.strip() and not line.startswith(MARKERS):
            lines.append(line)
    heading = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith('===')), None)
    if heading is not None:
        lines = lines[heading:]
    return lines[-CONTEXT_LINES:]


def question(text, secret=False):
    """A question setup is asking; the window may answer it (answer_from_window)."""
    global _pending
    shown = 'a hidden entry' if secret else str(text).strip()[:300]
    record = event('question', text=shown, secret=bool(secret), form=question_kind(shown, secret), context=question_context())
    with _lock:
        _pending = {'seq': record['seq'], 'event': threading.Event(), 'answer': None}
    return record


def answered():
    global _pending
    with _lock:
        _pending = None
    _cut()
    event('answered')


def pending_question():
    """The question setup is waiting on, or None."""
    with _lock:
        for record in reversed(_events):
            if record['kind'] == 'answered':
                return None
            if record['kind'] == 'question':
                return record
    return None


def answer_from_window(seq, text):
    """The window's answer to the question with this sequence number. True when it was
    the one waiting (and not answered already)."""
    with _lock:
        if _pending is None or _pending['seq'] != seq or _pending['event'].is_set():
            return False
        _pending['answer'] = str(text)
        _pending['event'].set()
        return True


@contextlib.contextmanager
def in_terminal(what):
    """A program of its own asks questions now (onboarding, music setup, smbpasswd):
    its prompts reach only the terminal, so the window says to answer there."""
    event('terminal', text=str(what)[:300])
    try:
        yield
    finally:
        event('terminal_done')


def terminal_handoff():
    """What is being answered in the terminal right now, or None."""
    with _lock:
        for record in reversed(_events):
            if record['kind'] == 'terminal_done':
                return None
            if record['kind'] == 'terminal':
                return record['text']
    return None


class _Unwatchable(Exception):
    """The terminal cannot be watched (not a real stream): ask the plain way."""


def _terminal(stream):
    """The file descriptor of a terminal, or None for a pipe, a file or a fake stream."""
    try:
        fd = stream.fileno()
        return fd if os.isatty(fd) else None
    except (AttributeError, OSError, ValueError):
        return None


@contextlib.contextmanager
def _no_echo(fd, quiet):
    """No echo while a password is typed in the terminal."""
    saved = None
    if quiet:
        try:
            saved = termios.tcgetattr(fd)
            new = termios.tcgetattr(fd)
            new[3] &= ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
        except termios.error:
            saved = None
    try:
        yield
    finally:
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _from_window(pending, secret):
    """The window's answer, once it has one (None until then)."""
    if pending is None or not pending['event'].is_set():
        return None
    sys.stdout.write(('••••' if secret else pending['answer']) + '  (from the setup window)\n')
    sys.stdout.flush()
    return pending['answer']


def _from_terminal(stream, quiet):
    """A line typed within POLL seconds (None when none came). EOFError at Ctrl+D."""
    try:
        readable, _, _ = select.select([stream], [], [], POLL)
    except (OSError, ValueError):
        raise _Unwatchable from None
    if not readable:
        return None
    line = stream.readline()
    if quiet:
        sys.stdout.write('\n')
        sys.stdout.flush()
    if line == '':
        raise EOFError
    return line.rstrip('\n')


def _wait_for_answer(stream, secret):
    """A line from the terminal, or the window's answer, whichever comes first (None:
    nothing can be read from the terminal; the caller asks the plain way)."""
    with _lock:
        pending = _pending
    fd = _terminal(stream)
    if fd is None:
        return None
    try:
        with _no_echo(fd, secret):
            while True:
                answer = _from_window(pending, secret)
                if answer is not None:
                    _drop_typed(fd)
                    return answer
                answer = _from_terminal(stream, secret)
                if answer is not None:
                    return answer
    except _Unwatchable:
        return None


def _drop_typed(fd):
    """When the window answered first, a line typed in the terminal meanwhile would be
    read as the answer to the next question: it is dropped."""
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except termios.error:
        pass


class _Tee:
    """sys.stdout with the last lines remembered, so a question in the window can show
    what the terminal said just before it (the numbered drives, say)."""

    def __init__(self, inner):
        self._inner = inner
        self._partial = ''

    def write(self, text):
        self._inner.write(text)
        self._partial += text
        if '\n' in self._partial:
            *lines, self._partial = self._partial.split('\n')
            with _lock:
                _recent.extend(line[:300] for line in lines)
        return len(text)

    def __getattr__(self, name):
        return getattr(self._inner, name)


@contextlib.contextmanager
def watching_prompts():
    """While this is active, every input() and getpass() setup asks is also asked in the
    window, and either can answer it."""
    real_input, real_getpass, real_stdout = builtins.input, getpass.getpass, sys.stdout
    sys.stdout = _Tee(real_stdout)

    def asked(prompt, secret):
        sys.stdout.write(prompt)
        sys.stdout.flush()
        question(prompt, secret=secret)
        try:
            answer = _wait_for_answer(sys.stdin, secret)
        except EOFError:
            answered()
            raise
        if answer is None:
            # The terminal can't be watched (not a real stream): the plain way, prompt shown already.
            try:
                return real_getpass('') if secret else real_input('')
            finally:
                answered()
        answered()
        return answer

    builtins.input = lambda prompt='': asked(str(prompt), False)
    getpass.getpass = lambda prompt='Password: ', stream=None: asked(str(prompt), True)
    try:
        yield
    finally:
        builtins.input, getpass.getpass, sys.stdout = real_input, real_getpass, real_stdout
