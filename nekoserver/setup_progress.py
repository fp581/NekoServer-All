"""What setup is doing right now, as events the setup window shows: steps starting and
ending, packages being installed, a question waiting, the finishing screen. Kept in
memory for the window's server; also appended to a file when enabled.

A question setup asks in the terminal is asked in the window too, with the terminal's
last lines as its context (the list of drives to choose from, say), and the answer can
come from either: the first one wins and the other is told. Nothing secret is kept: an
answer is never recorded: a hidden entry (a password, a PIN) is reported by its prompt
alone ("Same PIN again:"), and its answer from the window travels on this PC's loopback
alone."""

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
_step = None  # the step running now
_recent = collections.deque(maxlen=40)  # the terminal's last lines, for a question's context
_pending = None  # {'seq', 'event', 'answer'} while a question waits
_page = None  # a group of questions the window shows as one page (page()), while it is asked
CONTEXT_LINES = 14
LONGEST_ANSWER = 4000  # an answer from the window (a provider's cookie can be long)
POLL = 0.3  # seconds between looks at the terminal and the window while waiting


def enable(path=None):
    """Start recording (optionally to a file, 0600, one JSON object per line)."""
    global _path
    with _lock:
        _events.clear()
        _recent.clear()
        _asked.update(text=None, context=[])
        _path = str(path) if path else None
        if _path:
            with open(_path, 'w', encoding='utf-8') as handle:
                os.fchmod(handle.fileno(), 0o600)


def disable():
    global _path, _step, _pending, _page
    with _lock:
        _events.clear()
        _recent.clear()
        _asked.update(text=None, context=[])
        _path = None
        _step = None
        _pending = None
        _page = None


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
        _cut()  # a question in this step shows what this step said, not the one before
    # (Long enough for a whole reason: a site that could not be reached names the site,
    # the likely causes and what to do.)
    event('step', name=name, status=status, reason=str(reason or '')[:1000])


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
    # "[yes/no]", and the short "[y/N]": the window answers both with Yes and No.
    return 'yesno' if '[yes/no]' in text or re.search(r'\[[yY]/[nN]\]', text) else 'text'


CUT = '\x00'  # in _recent: where the last answered question, or the step before, ended
STAMP = re.compile(r'\[\d{4}-\d\d-\d\dT[^\]]*\] ')
MARKERS = ('START ', 'PASS ', 'FAILED ', 'NEEDS SETUP ', 'UNAVAILABLE ', 'BLOCKED ')  # the installer's step lines


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
        raw = raw[len(raw) - raw[::-1].index(CUT) :]
    lines = []
    for line in raw:
        line = STAMP.sub('', line).rstrip()
        if line.strip() and not line.startswith(MARKERS):
            lines.append(line)
    heading = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].startswith('===')), None)
    if heading is not None:
        lines = lines[heading:]
    return lines[-CONTEXT_LINES:]


def recent_lines(count=CONTEXT_LINES):
    """The last lines the terminal printed (the installer's time stamps taken off): what a
    window that follows a menu of its own, the recovery menu or the uninstaller, shows
    between two questions."""
    with _lock:
        raw = [line for line in _recent if line != CUT]
    lines = [STAMP.sub('', line).rstrip() for line in raw]
    return [line for line in lines if line.strip()][-count:]


_asked = {'text': None, 'context': []}  # the question asked last, and what was shown with it
RETRY_LINES = 2  # at most this much new text: the same question asked again after a wrong answer


def context_for(shown, fresh):
    """What to show with the question `shown`, given what the terminal said since the last
    answer (`fresh`). Asked again after a wrong answer, only the error is new, and the
    list to choose from was printed before the first ask: it is kept, with the error after
    it (seen in the window on a VM: "Choose another one from the list" with no list). The
    same words asking a new question come with a list of their own, and stand alone."""
    if shown == _asked['text'] and len(fresh) <= RETRY_LINES:
        fresh = (_asked['context'] + [line for line in fresh if line not in _asked['context']])[-CONTEXT_LINES:]
    _asked.update(text=shown, context=fresh)
    return fresh


def question(text, secret=False):
    """A question setup is asking; the window may answer it (answer_from_window)."""
    global _pending
    # A hidden entry's own words are shown too ("PIN:", "Same PIN again:"): only its answer is secret.
    shown = str(text).strip()[:300]
    # A question of several lines (its hint, its choices by number, how to answer in the
    # terminal) has printed them already: they are the question, not what came before it.
    own = {line.strip() for line in str(text).splitlines()}
    context = [line for line in context_for(shown, question_context()) if line.strip() not in own]
    record = event(
        'question',
        text=shown,
        secret=bool(secret),
        form=question_kind(shown, secret),
        context=context,
        page=page_shown(),
    )
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


def answer_from_window(seq, text, skip=False, fields=None):
    """The window's answer to the question with this sequence number. True when it was
    the one waiting (and not answered already). `skip` is the window's "Skip for now":
    the question is left unanswered, as Ctrl+D in the terminal leaves it. `fields` are
    the answers to the other questions of the page this one is on (page())."""
    with _lock:
        if _pending is None or _pending['seq'] != seq or _pending['event'].is_set():
            return False
        _pending['answer'] = str(text)
        _pending['skip'] = bool(skip)
        _pending['event'].set()
        if _page is not None:
            if skip:
                _page['skipped'] = True
            elif isinstance(fields, dict):
                _page['answers'].update(
                    {key: str(value)[:LONGEST_ANSWER] for key, value in fields.items() if key in _page['fields']}
                )
        return True


# ----- a page: related questions the window shows and answers together


@contextlib.contextmanager
def page(form, fields, **options):
    """While this is active, the questions setup asks belong to one page (`form` says
    which, "backup_times"): the window shows every field with its current value and
    may answer all of them at once, while the terminal is still asked one at a time.
    Before each prompt, page_asks names the field; page_answer gives what the window
    sent for a field, so that prompt is not asked."""
    global _page
    with _lock:
        _page = {
            'form': form,
            'fields': dict(fields),
            'options': options,
            'asking': None,
            'answers': {},
            'skipped': False,
        }
    try:
        yield
    finally:
        with _lock:
            _page = None


def page_shown():
    """What the window draws for the question being asked: the page's form, the fields
    with their current values and which one this question stands for; None off a page."""
    with _lock:
        if _page is None:
            return None
        return {'form': _page['form'], 'fields': dict(_page['fields']), 'asking': _page['asking'], **_page['options']}


def page_asks(key):
    """The field the next question stands for."""
    with _lock:
        if _page is not None:
            _page['asking'] = key


def page_set(key, value):
    """A field's value as answered (shown on the page with the next question)."""
    with _lock:
        if _page is not None:
            _page['fields'][key] = value


def page_answer(key):
    """('answer', text) when the window sent this field with an earlier question of the
    page, ('skip', None) when its "Skip for now" left the whole page, None when the
    field is to be asked."""
    with _lock:
        if _page is None:
            return None
        if _page['skipped']:
            return ('skip', None)
        if key in _page['answers']:
            return ('answer', _page['answers'].pop(key))
        return None


def screen_only(text):
    """Words for the person at the terminal alone (a saved login shown on request): written
    straight to the terminal, never kept among the last lines a question shows in the
    window, so they are in no event."""
    stream = sys.stdout._inner if isinstance(sys.stdout, _Tee) else sys.stdout
    stream.write(text)
    stream.flush()


def ask_terminal(prompt):
    """A question for the terminal alone ("Press Enter to hide it"): the window is not asked."""
    screen_only(prompt)
    return (_plain['input'] if _watching else _real_input)('')


def page_field(key, ask, prompt):
    """One field of the page being asked: what the window sent for it with an earlier
    field's question, else the question itself (asked in both places). EOFError when the
    window left the whole page ("Skip for now"), as Ctrl+D in the terminal raises."""
    sent = page_answer(key)
    if sent is not None:
        if sent[0] == 'skip':
            raise EOFError
        return sent[1]
    page_asks(key)
    answer = ask(prompt)
    page_answer(key)  # the window sends every field: this one's copy is not kept for a second ask
    return answer


@contextlib.contextmanager
def password_page(what, **rules):
    """While this is active, a password (or PIN) chosen now and its second entry are one
    page in the window: two hidden fields that must match before they are sent. The
    terminal is still asked twice (page_field 'password', then 'again'). `rules` are what
    the window checks before sending, as the caller checks them itself: `least` and
    `most` (its length), `digits` (0-9 only), `optional` (both may be left empty).
    Neither entry is ever in an event: the page's fields carry no values."""
    with page('new_password', {'password': '', 'again': ''}, what=what, **rules):
        yield


def run_program(script, args=()):
    """One of NekoServer's own programs (onboard.py, media-connect.py, music-setup.py)
    run inside this process, as `python3 script args` would run it. What it prints is
    the terminal's, and every question it asks goes the way setup's own do, so the
    window asks it too. (Until 0.50.0 these ran as programs of their own, whose
    questions reached only the terminal, and the window said "answer in the terminal".)
    Returns its exit code: what it ended with, 1 for an error it did not handle (said
    in the terminal, as Python says it)."""
    import runpy
    import traceback

    saved = sys.argv
    sys.argv = [str(script), *(str(arg) for arg in args)]
    try:
        runpy.run_path(str(script), run_name='__main__')
        return 0
    except SystemExit as stop:
        if stop.code is None or isinstance(stop.code, int):
            return stop.code or 0
        print(stop.code, file=sys.stderr)  # sys.exit('a message'), as Python ends it
        return 1
    except Exception:  # any error of the program ends that program, not setup
        traceback.print_exc()
        return 1
    finally:
        sys.argv = saved


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
    if pending.get('skip'):
        sys.stdout.write('  (left for now, from the setup window)\n')
        sys.stdout.flush()
        raise EOFError  # what Ctrl+D in the terminal raises
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
                    if _claimed_by_terminal(pending, answer):
                        return answer
                    return _from_window(pending, secret)  # the window's came in the same moment
    except _Unwatchable:
        return None


def _claimed_by_terminal(pending, answer):
    """The terminal's answer taken for the question, unless the window's got there first:
    either way only one of them is told its answer was used."""
    if pending is None:
        return True
    with _lock:
        if pending['event'].is_set():
            return False
        pending['answer'] = answer
        pending['event'].set()
        return True


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


# ----- every question setup asks goes through these two

_real_input, _real_getpass = builtins.input, getpass.getpass
_plain = {'input': _real_input, 'getpass': _real_getpass}  # how to ask without the window
_watching = False


def asking_input(prompt=''):
    """input(), asked in the window too while setup watches its prompts."""
    return _ask_both(str(prompt), False) if _watching else _real_input(prompt)


def asking_getpass(prompt='Password: ', stream=None):
    """getpass.getpass(), asked in the window too (hidden there as well)."""
    return _ask_both(str(prompt), True) if _watching else _real_getpass(prompt, stream)


def own_prompts():
    """Called first thing by a program that runs setup (installer.py, uninstaller.py),
    before any other of NekoServer's files is loaded: input() and getpass() are the two
    functions above from then on. A function that takes `input` as a default when its
    file is loaded (`def ask(prompt=input)`) then holds this one, so its question
    reaches the window whenever its file was loaded: before 0.50.0 that depended on the
    file being loaded after the window started watching. Without a window watching
    they ask the plain way."""
    builtins.input, getpass.getpass = asking_input, asking_getpass


def _ask_both(prompt, secret):
    """A question put in the terminal and in the window; the first answer is the answer."""
    sys.stdout.write(prompt)
    sys.stdout.flush()
    question(prompt, secret=secret)
    try:
        answer = _wait_for_answer(sys.stdin, secret)
    except BaseException:  # Ctrl+D, Ctrl+C: the window stops showing the question too
        answered()
        raise
    if answer is None:
        # The terminal can't be watched (not a real stream): the plain way, prompt shown already.
        try:
            return _plain['getpass']('') if secret else _plain['input']('')
        finally:
            answered()
    answered()
    return answer


@contextlib.contextmanager
def watching_prompts():
    """While this is active, every input() and getpass() setup asks is also asked in the
    window, and either can answer it."""
    global _watching
    before = (builtins.input, getpass.getpass, sys.stdout)
    # Asked the plain way when the terminal can't be watched: whatever input() and
    # getpass() were before (setup's own two stand for the real ones).
    _plain.update(
        input=_real_input if before[0] is asking_input else before[0],
        getpass=_real_getpass if before[1] is asking_getpass else before[1],
    )
    sys.stdout = _Tee(before[2])
    builtins.input, getpass.getpass = asking_input, asking_getpass
    _watching = True
    try:
        yield
    finally:
        _watching = False
        builtins.input, getpass.getpass, sys.stdout = before
