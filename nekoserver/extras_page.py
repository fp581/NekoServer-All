"""What the extras page of setup shows for the AI assistant, the TV, and a change of the
phone-alert topic (wizard specification P06, parts C, D and the last lines of A).

Plain functions that only look and describe; the two that save something say so in their
names (choose_assistant, record_tv_test). The alert test and the outage test of parts A
and B are in modules/notifications.py and modules/heartbeat.py.
"""

import json
import os
import shutil
import time
from pathlib import Path
from urllib.parse import urlsplit

from modules import assistant, notifications, tv
from neko_common import atomic_json, media_group, safe_path

# ----- C: the AI assistant's model

PLAIN_NAMES = ('Small', 'Medium', 'Large')  # the models of assistant.MODELS, smallest first
LATER = 'later'
# Said with the choices. It runs on this PC and shares it with the apps: no promise that
# they never slow down, and none that nothing leaves the home (the model is downloaded).
WHAT_IT_COSTS = (
    'The assistant runs on this PC and shares its processor and memory with your apps: while it answers, a film '
    'being converted or new photos being sorted go slower. '
    f'{assistant.IDLE_MINUTES} minutes after the last question its memory is free again. '
    'Its model is downloaded once from the Ollama library on the internet; your questions and its answers stay on this PC.'
)


def _gb(amount):
    return f'{amount:g} GB'


def assistant_choices(config, memory=None, own_use=None, apps_started=True, usage=shutil.disk_usage, checks=None):
    """The models a person can pick from, each as they can judge it.

    memory: (total, available) in GiB, read from this PC when left out. apps_started:
    False when the chosen apps are not running yet (a first install before its apps are
    up): what is free then says nothing sure about later, so a model that fits now is
    'unknown', never 'yes'; one that does not fit even now is 'no' either way.

    Returns {'possible': True | False,       can an assistant run on this PC at all
             'reason': why not ('' when possible),
             'memory': {'total_gb', 'free_gb', 'measured': 'with_apps' | 'before_apps'},
             'choices': [{'id': the model as setup saves it,
                          'name': 'Small' | 'Medium' | 'Large',
                          'line': one sentence a person can judge it by,
                          'technical': the model's own name, for small print,
                          'download_gb', 'memory_gb',
                          'downloaded': True | False,
                          'fits': 'yes' | 'no' | 'unknown',
                          'room_on_drive': True | False,
                          'why_not': '' or why it cannot be picked (or cannot be promised),
                          'suggested': True | False}, ...],
             'suggested': the id of the suggested one, or None,
             'chosen': the id saved before, or None,
             'later': {'id': 'later', 'name', 'line'},     always a valid answer
             'costs': what it costs the apps, to show with the choices}
    """
    total, available = memory if memory is not None else assistant.memory_now()
    own = assistant.own_use_gib(config) if own_use is None else own_use
    room = assistant.memory_for_model(total, available, own)
    reason = ''
    try:
        for check in (
            checks if checks is not None else (assistant.processor_ok, lambda: assistant.port_free_for_us(config))
        ):
            check()
    except assistant.NotHere as exc:
        reason = f'The assistant cannot run on this PC: {exc}.'
    if not reason and total <= 0:
        reason = "Setup could not read how much memory this PC has, so it cannot tell which model fits."
    choices = []
    for name, (model, download, need, answers_like) in zip(PLAIN_NAMES, assistant.MODELS):
        one = {
            'id': model,
            'name': name,
            'line': f'{name}: {answers_like}. Uses about {_gb(need)} of memory while it answers '
            f'and {_gb(download)} of drive space.',
            'technical': model,
            'download_gb': download,
            'memory_gb': need,
            'downloaded': assistant.downloaded(config, model),
            'room_on_drive': True,
            'why_not': '',
            'suggested': False,
        }
        if need > room:
            one['fits'] = 'no'
            one['why_not'] = (
                f'It needs about {_gb(need)} of free memory. This PC has {max(room, 0):.1f} GB free for it'
                + (' with your apps running' if apps_started else '')
                + ' and room kept for them to grow.'
            )
        elif apps_started:
            one['fits'] = 'yes'
        else:
            one['fits'] = 'unknown'
            one['why_not'] = (
                'It fits now, but your apps are not running yet. Setup measures again when it installs the '
                'assistant, and says so if this model no longer fits.'
            )
        try:
            assistant.check_space(config, model, download, usage)
        except assistant.NotHere as exc:
            one['room_on_drive'] = False
            one['why_not'] = (one['why_not'] + ' ' if one['fits'] == 'no' else '') + f'Not enough drive space: {exc}.'
        except OSError:
            one['room_on_drive'] = False
            one['why_not'] = 'Setup could not read how much space the app drive has.'
        choices.append(one)
    usable = [one for one in choices if one['fits'] != 'no' and one['room_on_drive']] if not reason else []
    if not reason and not usable:
        reason = 'No model fits on this PC now. ' + choices[0]['why_not']
    # The same rule as the terminal's list (assistant.choices): the biggest that fits.
    # Bigger is slower too; the lines say so, and the person picks.
    suggested = usable[-1]['id'] if usable else None
    for one in choices:
        one['suggested'] = one['id'] == suggested
    saved = assistant.options(config)
    chosen = saved.get('model') if saved.get('wanted') is True else None
    return {
        'possible': not reason,
        'reason': reason,
        'memory': {
            'total_gb': round(total, 1),
            'free_gb': round(max(room, 0), 1),
            'measured': 'with_apps' if apps_started else 'before_apps',
        },
        'choices': choices,
        'suggested': suggested,
        'chosen': chosen if chosen in [one['id'] for one in choices] else None,
        'later': {
            'id': LATER,
            'name': 'Set up later',
            'line': 'Set up later: nothing is downloaded now, and everything else works without it. '
            'Setup offers it again the next time it runs.',
        },
        'costs': WHAT_IT_COSTS,
    }


def choose_assistant(ctx, choice):
    """Save the person's pick: a model's id, or 'later'. Later saves nothing at all, so the
    choice stays open (it is not "no assistant": that is said on the first page).
    Returns {'status': 'chosen', 'model'} or {'status': 'later'}; ValueError for anything
    that is not one of the offered answers (a value from the window is not trusted)."""
    if choice == LATER:
        return {'status': LATER}
    if not isinstance(choice, str) or choice not in [model for model, *_ in assistant.MODELS]:
        raise ValueError('Not one of the offered models')
    assistant.save_choice(ctx, True, choice)
    return {'status': 'chosen', 'model': choice}


# ----- D: the TV

# What a person has to do themselves; setup opens the right window for each and waits.
TV_NOT_AUTOMATIC = (
    'Signing in to Steam: your own account and its sign-in approval (Steam Guard). Setup never sees or keeps it.',
    'Remote Play from a gaming PC: it is switched on in Steam on that other PC, which setup cannot reach.',
    'Pairing a controller: put it in pairing mode and pair it in the Bluetooth settings, or plug it in by USB. '
    'Some controllers and dongles need a driver of their own.',
    'The picture: the resolution and refresh rate for your TV are chosen in the display settings.',
    'The sound: the TV (HDMI) is chosen as the sound output in the sound settings.',
)
TV_TESTS = {
    'picture': (
        'Picture on the TV',
        'The desktop fills the TV at the resolution you chose, and a video plays smoothly.',
    ),
    'sound': ('Sound from the TV', 'The sound of that video comes out of the TV, not the PC.'),
    'controller': ('Controller', 'The controller moves through Steam Big Picture: buttons and both sticks.'),
    'steam': ('Steam', 'Steam opens in Big Picture, you are signed in, and a game or Remote Play starts.'),
}
NOT_TESTED, WORKED, DID_NOT_WORK = 'not_tested', 'worked', 'did_not_work'
# The tests the TV step of earlier runs kept (reports/tv-setup.json) and the rows they stand for.
EARLIER_TV_CHECKS = {
    'tv:display_audio_playback': ('picture', 'sound'),
    'tv:controller': ('controller',),
    'tv:steam_login_remote_play': ('steam',),
}


def tv_plan(config, family=None, has_desktop=None, vendors=None, has_browser=None):
    """What the TV extra will install on this PC, and what it cannot set up by itself.

    The PC is looked at when the last four are left out (tests give them).
    Returns {'possible': True | False, 'reason': why not ('' when possible),
             'installs': [plain lines], 'packages': [package names, for small print],
             'needs_first': '' or what the person must switch on before Steam can be installed,
             'not_automatic': [plain lines: what only the person can do]}
    """
    import host

    plan = {'possible': False, 'reason': '', 'installs': [], 'packages': [], 'needs_first': ''}
    plan['not_automatic'] = list(TV_NOT_AUTOMATIC)
    opts = config.get('tv', {})
    if not isinstance(opts, dict):
        opts = {}
    if not media_group(config, 'movies'):
        plan['reason'] = 'The TV mode opens Jellyfin, and Movies and shows is switched off on this server.'
        return plan
    if family is None:
        family = host.current() if host.detect() is not None else None
    if family is None:
        plan['reason'] = 'The TV extra is not supported on this Linux yet.'
        return plan
    no_steam = tv.unavailable_reason(family)
    if no_steam:
        plan['reason'] = no_steam + '.'
        return plan
    if not (tv.has_desktop() if has_desktop is None else has_desktop):
        plan['reason'] = tv.NO_DESKTOP + '.'
        return plan
    vendors = tv.graphics() if vendors is None else vendors
    has_browser = bool(tv.browser()) if has_browser is None else has_browser
    plan['possible'] = True
    plan['packages'] = tv.wanted_packages(opts, vendors, has_browser)
    plan['installs'] = ['Steam, with its Big Picture mode for a TV and a controller.']
    if tv.vulkan_packages(vendors):
        plan['installs'].append('The graphics driver part games need (Vulkan) for this PC\'s graphics.')
    elif 'nvidia' in vendors:
        plan['not_automatic'].append("NVIDIA graphics: its maker's driver, installed your Linux's own way.")
    if not has_browser:
        plan['installs'].append('The Firefox browser (this PC has no browser yet).')
    if opts.get('bluetooth', True):
        plan['installs'].append('Bluetooth, switched on, for a wireless controller.')
    plan['installs'].append('Two shortcuts: "NekoServer Steam Big Picture" and "NekoServer YouTube".')
    if opts.get('keep_awake', True):
        plan['installs'].append(
            'A power setting so this PC does not go to sleep while it is plugged in (the server jobs keep running).'
        )
    if not tv.steam_ready(family) or not family.available('steam'):
        plan['needs_first'] = (
            'Steam is not offered by the software sources switched on here. To get it, '
            + tv.steam_source(family)
            + '. NekoServer changes no software source itself.'
        )
    return plan


def _tv_tests_path(config):
    return safe_path(Path(config['data_mount']) / 'reports' / 'tv-tests.json')


def _saved_tv_tests(config):
    try:
        value = json.loads(_tv_tests_path(config).read_text())
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return {}
    tests = value.get('tests') if isinstance(value, dict) and value.get('format') == 'neko-tv-tests-v1' else None
    return tests if isinstance(tests, dict) else {}


def tv_tests(config):
    """The four tests of the ready page, each on its own: picture, sound, controller,
    Steam. Each is {'id', 'title', 'look_for', 'status': 'not_tested' | 'worked' |
    'did_not_work', 'by': 'person'}. They are a person's own observations on the real TV;
    a test the TV step of an earlier run was told had worked counts as worked."""
    saved = _saved_tv_tests(config)
    try:
        earlier = tv.previous_checks(config['data_mount'])
    except (KeyError, TypeError):
        earlier = set()
    told = {test for check, tests in EARLIER_TV_CHECKS.items() if check in earlier for test in tests}
    rows = []
    for test, (title, look_for) in TV_TESTS.items():
        kept = saved.get(test)
        status = kept.get('status') if isinstance(kept, dict) else None
        if status not in (WORKED, DID_NOT_WORK):
            status = WORKED if test in told else NOT_TESTED
        rows.append({'id': 'tv:' + test, 'title': title, 'look_for': look_for, 'status': status, 'by': 'person'})
    return rows


def record_tv_test(config, test, result, now=None):
    """Save what the person saw for one test: 'worked', 'did_not_work', or 'not_tested'
    to take an answer back. The other three are left as they are. ValueError for a test
    or a result that is not one of these (a value from the window is not trusted)."""
    if test not in TV_TESTS or result not in (NOT_TESTED, WORKED, DID_NOT_WORK):
        raise ValueError('Not a TV test or not one of its results')
    saved = _saved_tv_tests(config)
    saved[test] = {'status': result, 'said_at': now or time.time()}
    atomic_json(_tv_tests_path(config), {'format': 'neko-tv-tests-v1', 'tests': saved})
    return tv_tests(config)


# ----- A: a change of the phone-alert topic, shown without showing it

HIDDEN = '…'


def redacted(address):
    """An alert topic's address as it may be shown: the server's name, and of the topic
    only its first two and last two characters when it is long enough for the rest to stay
    a secret (16 or more), else nothing of it. '' for anything that is not a topic address."""
    if not notifications.endpoint(address):
        return ''
    parts = urlsplit(address)
    topic = parts.path.lstrip('/')
    shown = topic[:2] + HIDDEN + topic[-2:] if len(topic) >= 16 else HIDDEN
    return f'https://{parts.netloc}/{shown} ({len(topic)} characters)'


def _saved_alerts(private):
    """(address, token) of the alerts set up on this server, ('', '') when there are none,
    or None when the root-only file cannot be read."""
    try:
        if not os.path.exists(private):
            return '', ''
        value = json.loads(Path(private).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    address, token = value.get('endpoint', ''), value.get('token', '')
    return (address if isinstance(address, str) else ''), (token if isinstance(token, str) else '')


def alerts_change(new_address, new_token='', private=notifications.PRIVATE_CONFIG):
    """The pair the form shows before its one Save, on a server that has alerts already.
    Nothing is saved here, and neither address is shown whole.

    Returns {'has_current': True | False | None (None: the saved one cannot be read),
             'current': its redacted address ('' when none or unreadable),
             'new': the redacted new address ('' when it is not an acceptable one),
             'new_ok': True | False,
             'changes': 'nothing' | 'topic' | 'token' | 'first' | 'unknown',
             'token': 'kept' | 'new' | 'removed' | 'none'}
    """
    saved = _saved_alerts(private)
    new_ok = notifications.endpoint(new_address) and notifications.valid_token(new_token)
    result = {
        'has_current': None if saved is None else bool(saved[0]),
        'current': redacted(saved[0]) if saved else '',
        'new': redacted(new_address) if new_ok else '',
        'new_ok': bool(new_ok),
        'changes': 'unknown',
        'token': 'none',
    }
    if saved is None or not new_ok:
        return result
    address, token = saved
    if new_token:
        result['token'] = 'kept' if new_token == token else 'new'
    elif token:
        result['token'] = 'removed'
    if not address:
        result['changes'] = 'first'
    elif new_address != address:
        result['changes'] = 'topic'
    elif new_token != token:
        result['changes'] = 'token'
    else:
        result['changes'] = 'nothing'
    return result
