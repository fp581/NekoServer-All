"""Install private ntfy delivery with a Rust timer independent of SSD availability."""

import getpass
import guided_setup
import questions
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import string
import subprocess
import sys

import native_runtime
import outside_request
from neko_common import VERSION, atomic_json, safe_path, check_units, write_units

DEPENDENCIES = ['dashboard']
DESCRIPTION = 'Background ntfy alerts, grouped retries and recovery notices (Rust)'
PRIVATE_CONFIG = '/etc/nekoserver-notifications/config.json'


def options(config):
    opts = config.get('notifications', {})
    if not isinstance(opts, dict):
        raise RuntimeError('notifications must be an object')
    result = {}
    for key, default, low, high in (
        ('debounce_seconds', 180, 0, 3600),
        ('repeat_seconds', 43200, 3600, 604800),
        ('minimum_interval_seconds', 300, 60, 3600),
    ):
        value = opts.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise RuntimeError('Invalid notification timing')
        result[key] = value
    return result


def endpoint(value):
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r'https://[A-Za-z0-9][A-Za-z0-9.-]*(?::([0-9]{1,5}))?/[A-Za-z0-9_-]{1,128}', value)
    return bool(match and (match[1] is None or 0 < int(match[1]) <= 65535))


def valid_token(token):
    return isinstance(token, str) and len(token) <= 512 and re.fullmatch(r'[A-Za-z0-9._-]*', token) is not None


# The public ntfy server, for people who run none of their own.
PUBLIC_SERVER = 'https://ntfy.sh'
# What the test says on the phone. Nothing about this server is in it.
TEST_TITLE = 'NekoServer test'
TEST_MESSAGE = 'This is a test from NekoServer setup. If you can read it on your phone, alerts will reach you.'


def new_topic_name():
    """A topic name nobody can guess, for people who have none: "neko-" and 24 random
    letters and digits. A name that is hard to guess is not a lock: on a server where
    anyone may read (reading_test says "open"), whoever learns the name reads the alerts."""
    letters = string.ascii_lowercase + string.digits
    return 'neko-' + ''.join(secrets.choice(letters) for _ in range(24))


def topic_address(name, server=PUBLIC_SERVER):
    """The full topic address for a topic name on a server, or '' when the two do not
    make an acceptable one (endpoint)."""
    if not isinstance(name, str) or not isinstance(server, str):
        return ''
    address = server.rstrip('/') + '/' + name
    return address if endpoint(address) else ''


def _token_header(token):
    return {'Authorization': 'Bearer ' + token} if token else {}


def _not_sent(address, token):
    """What a test starts as: which of the two entries are acceptable, nothing sent yet."""
    return {
        'address': 'ok' if endpoint(address) else 'invalid',
        'token': 'none' if token == '' else 'ok' if valid_token(token) else 'invalid',
        'server': outside_request.NOT_SENT,
        'http_status': None,
    }


def _answer_word(happened, status, yes):
    """ntfy's answer in one word: 401 means it does not know the token (or wants one),
    403 means it knows who asks and does not allow it."""
    if happened == outside_request.ACCEPTED:
        return yes
    if status == 401:
        return 'wrong_token'
    if status == 403:
        return 'not_allowed'
    return outside_request.kind(happened, status)


def send_test(address, token='', opener=None):
    """Send one test message to the topic and say what its server answered.

    Returns {'address': 'ok' | 'invalid',
             'token': 'ok' | 'invalid' | 'none',
             'server': 'accepted' | 'wrong_token' | 'not_allowed' | 'wrong_address' | 'busy'
                       | 'server_error' | 'refused' | 'redirected' | 'timed_out' | 'unreachable'
                       | 'untrusted' | 'not_sent',
             'http_status': the answer's number, or None when there was no answer,
             'phone': 'not_tested'}
    "accepted" means the server took the message, not that a phone showed it: whether it
    arrived is the person's answer (outside_request.observed), kept apart from this.
    The address and the token are in no log, no command line and not in the result.
    """
    result = {**_not_sent(address, token), 'phone': outside_request.NOT_TESTED}
    if result['address'] != 'ok' or result['token'] == 'invalid':
        return result
    headers = {'Title': TEST_TITLE, 'Content-Type': 'text/plain; charset=utf-8', 'Priority': '3'}
    headers.update(_token_header(token))
    happened, status = outside_request.send(address, 'POST', headers, TEST_MESSAGE.encode(), opener)
    result.update(server=_answer_word(happened, status, 'accepted'), http_status=status)
    return result


def reading_test(address, token='', opener=None):
    """Ask the topic's server who may read the topic (ntfy answers that at <topic>/auth;
    nothing is published).

    With no token: 'open' means anyone who knows the address reads the alerts without
    signing in. With a token: 'allowed' means that token may read, so it can be used in
    the phone app; 'not_allowed' means it may only send, and must not be put in the phone.

    Returns {'address': 'ok' | 'invalid',
             'token': 'ok' | 'invalid' | 'none',
             'server': 'open' | 'allowed' | 'wrong_token' | 'not_allowed' | 'refused'
                       | 'redirected' | 'timed_out' | 'unreachable' | 'untrusted' | 'not_sent',
             'http_status': the answer's number, or None when there was no answer}
    """
    result = _not_sent(address, token)
    if result['address'] != 'ok' or result['token'] == 'invalid':
        return result
    happened, status = outside_request.send(address + '/auth', 'GET', _token_header(token), None, opener)
    result.update(server=_answer_word(happened, status, 'allowed' if token else 'open'), http_status=status)
    return result


def set_up_later():
    """The step's result when the person chooses "Set up alerts later": it stays on the
    list of things still to do, nothing is installed and nothing is claimed."""
    return {
        'status': 'needs_setup',
        'reason': 'Phone alerts are not set up yet. The dashboard on the server still shows '
        'everything. Run setup again when you want them.',
    }


def asked_privately(key, valid, hint, ask=None):
    """A private value (an address, a token) asked in the words of questions.py: a
    password box in the setup window, typed hidden in the terminal."""
    with questions.shown(key):
        return guided_setup.private_entry(questions.terminal(key), valid, hint, ask=ask)


def ask_token(ask=None):
    return asked_privately(
        'alerts_token',
        valid_token,
        'That is not an access token: it has only letters, digits, ".", "_" and "-" '
        '(such as tk_abc123...). Copy it again from ntfy and paste it.',
        ask=ask,
    )


def switch(context, configured, current, token, prompt=None, ask=None):
    """The topic in my-config.json when alerts don't go there yet: (address, token), or a
    needs_setup result. It gets its own access token; a change from another topic is
    confirmed first, and never made with nobody there to confirm it."""
    if not guided_setup.interactive(context):
        if current:
            return {
                'status': 'needs_setup',
                'reason': 'The ntfy topic in the settings differs from the one alerts go '
                'to now. To switch, run  sudo bash install.sh --module notifications --apply  in a terminal',
            }
        return configured, token
    if current and not questions.yes_no('alerts_switch', prompt):
        return {
            'status': 'needs_setup',
            'reason': 'Alerts still go to the earlier ntfy topic. To switch to the new one, run setup again '
            'and answer yes when it asks',
        }
    return configured, ask_token(ask)


def first_destination(context):
    """The first topic, typed in now: (address, token), with '' when it is left for later;
    None when phone alerts are skipped by choice; or a needs_setup result."""
    if (
        guided_setup.wanted(
            context,
            'notifications',
            'Phone alerts when something fails',
            [
                'NekoServer can send a message to your phone when a backup or app fails.',
                'It uses ntfy, a free notification app. Setting it up takes about 10 minutes.',
                'You can skip this; the dashboard on the server still shows everything.',
            ],
        )
        is False
    ):
        return None
    if not sys.stdin.isatty():
        return '', ''
    return alerts_form(context)


FORM_TITLE = 'Send alerts to my phone'
LATER = 'Leave the address empty, or press Skip for now, to set this up later. Nothing else waits for it.'
ADDRESS_HINT = (
    'That is not a topic address. It starts with https:// and ends with the topic name, '
    'such as https://ntfy.example.com/my-server-alerts. Copy it again and paste it.'
)
TOKEN_HINT = (
    'That is not an access token: it has only letters, digits, ".", "_" and "-" '
    '(such as tk_abc123...). Copy it again from ntfy and paste it.'
)


def form_lines(suggested):
    """What to do on the phone, said with the form (the terminal prints it, the window
    shows it above the two boxes)."""
    return [
        '1. On your phone, install the free app "ntfy" and open it.',
        '2. In the app, add a topic to follow. Its name is all that keeps others from reading your alerts '
        'unless your ntfy server uses access tokens, so take a name nobody could guess. One made for you:',
        '   ' + suggested,
        '3. Put the same address in the first box here. If your ntfy server gave you an access token, put it '
        'in the second box; most people leave that empty.',
        'Setup then sends one test message, and asks you whether it arrived.',
    ]


def alerts_form(context, ask=None):
    """The alerts topic and its access token, on one page: (address, token), or ('', '')
    when it is left for later (Enter alone, the window's Skip, three entries that are not
    an address). Both are typed hidden and are in no log."""
    import setup_progress

    ask = ask or getpass.getpass
    lines = form_lines(topic_address(new_topic_name()))
    for line in lines:
        print(line)
    rows = [
        {'key': 'address', 'label': 'The address of your alerts topic', 'secret': True},
        {'key': 'token', 'label': 'Access token (only if your ntfy server gave you one)', 'secret': True},
    ]
    with setup_progress.page(
        'fields', {'address': '', 'token': ''}, what=FORM_TITLE, rows=rows, lines=lines, note=LATER
    ):
        for _ in range(3):
            try:
                address = setup_progress.page_field(
                    'address', ask, 'The address of your alerts topic (hidden; Enter alone sets alerts up later): '
                )
                address = address.strip().rstrip('/')
                if not address:
                    return '', ''
                token = setup_progress.page_field(
                    'token', ask, 'Its access token (hidden; Enter alone when it has none): '
                ).strip()
            except EOFError:
                return '', ''
            if endpoint(address) and valid_token(token):
                return address, token
            print(TOKEN_HINT if endpoint(address) else ADDRESS_HINT)
    return '', ''


# What each answer of the alert server means, in the person's words (send_test).
TEST_SAID = {
    'accepted': 'The alert server accepted the test message.',
    'wrong_token': 'The alert server did not recognise the access token. Copy it again from ntfy, or leave it empty '
    'if your topic needs none.',
    'not_allowed': 'The alert server knows this topic but does not let NekoServer send to it: it needs an access '
    'token that is allowed to send to it.',
    'wrong_address': "The alert server has no topic at this address (it answered \"not found\"). Copy the "
    "address again from the ntfy app: it ends with your topic's name. Then enter it again.",
    'refused': 'The alert server turned the test message down. Try again in a few minutes; if it keeps happening, '
    "look at the topic's settings in the ntfy app or on the server's website.",
    'busy': 'The alert server is busy right now. Try again in a few minutes: retry this step then. Your address '
    'can stay as it is.',
    'server_error': 'Their server has a problem right now (the alert server answered with an error). This is not '
    'your mistake: retry this step later. Your address can stay as it is.',
    'redirected': 'This address points somewhere else, and NekoServer does not follow that. Use the final address '
    'of the topic.',
    'timed_out': 'The alert server did not answer in time. That is usually a network problem: check that this '
    'server is connected to the internet, then retry this step.',
    'unreachable': 'NekoServer got no answer from the alert server: a network problem. Check that this server is '
    'connected to the internet, then retry this step.',
    'untrusted': "The alert server's security certificate could not be checked, so nothing was sent.",
    'not_sent': 'No test was sent: the address is not one NekoServer can send to.',
}
OPEN_TOPIC = (
    'Anyone who knows this address can read your alerts without signing in. A long random name makes it hard to '
    'guess; it is not a lock.'
)


def test_alert(context, address, token, prompt=None):
    """One test message sent to the topic just saved, and what came of it: the server's
    answer (machine-checked), who may read the topic, and whether it arrived on the
    phone (the person's own word, kept apart). None when the server took it; a
    needs_setup result with the reason when it did not, so alerts are never shown as
    working on the strength of a saved address."""
    result = send_test(address, token)
    said = TEST_SAID.get(result['server'], TEST_SAID['not_sent'])
    context.log('Phone alerts test: ' + said)
    outside_request.mark_tested(safe_path(PRIVATE_CONFIG), result['server'])
    if result['server'] != 'accepted':
        return {
            'status': 'needs_setup',
            'reason': 'Phone alerts are saved, but the test message was not taken. ' + said,
        }
    if reading_test(address, token)['server'] == 'open':
        context.log('Phone alerts: ' + OPEN_TOPIC)
    if guided_setup.interactive(context):
        seen = questions.yes_no('alert_arrived', prompt, said=said)
        context.log(
            'Phone alerts test: you saw it arrive on your phone'
            if outside_request.observed(seen) == 'observed'
            else 'Phone alerts test: not seen on a phone yet. Check that the ntfy app follows the same address, '
            'and that the phone lets it show notifications'
        )
    return None


def definitions(binary, config):
    return {
        'nekoserver-notifications.service': (
            '[Unit]\nDescription=NekoServer background health notifications\n'
            'Wants=nekoserver-dashboard-collect.service\nAfter=nekoserver-dashboard-collect.service network-online.target\n'
            '[Service]\nType=oneshot\nUser=root\nUMask=0077\n'
            'StateDirectory=nekoserver-notifications\nStateDirectoryMode=0700\n'
            'RuntimeDirectory=nekoserver-notifications\nRuntimeDirectoryMode=0700\nRuntimeDirectoryPreserve=yes\n'
            f'ExecStart={binary} notify --config {config} --snapshot /run/nekoserver-dashboard/status.json '
            '--state /var/lib/nekoserver-notifications --runtime /run/nekoserver-notifications\n'
            'TimeoutStartSec=45\nNoNewPrivileges=yes\nProtectHome=yes\nProtectSystem=strict\nPrivateTmp=yes\n'
            'PrivateDevices=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n'
            'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n'
            'InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock\n'
        ),
        'nekoserver-notifications.timer': (
            '[Unit]\nDescription=NekoServer scheduled health notifications\n'
            '[Timer]\nOnBootSec=90s\nOnUnitInactiveSec=60s\n'
            '[Install]\nWantedBy=timers.target\n'
        ),
    }


def current_destination(context, private):
    """(address, token) set up before, or the one the settings name instead (switch);
    a needs_setup result when that switch is not finished."""
    existing = json.loads(private.read_text()) if private.exists() else {}
    address, token = existing.get('endpoint', ''), existing.get('token', '')
    configured = context.config.get('notifications', {}).get('endpoint', '')
    if configured and configured != address:
        return switch(context, configured, address, token)
    return address, token


def run(context):
    options(context.config)
    if not context.apply:
        context.log('Background notifications available with --apply; configure your own HTTPS ntfy topic')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Notification installation requires root')
    from modules._schedules import switched_off

    if switched_off(context.config, 'notifications', 'nekoserver-notifications', context.log):
        context.log('Phone alerts: off by choice')
        return
    private = safe_path(PRIVATE_CONFIG)
    chosen = current_destination(context, private)
    if isinstance(chosen, dict):
        return chosen
    address, token = chosen
    if not address:
        chosen = first_destination(context)
        if chosen is None:
            context.log('Phone alerts: skipped by choice')
            return
        if isinstance(chosen, dict):
            return chosen
        address, token = chosen
        if address:
            # Just typed in: saved, then tried for real before anything calls it set up.
            return install(context, address, token) or test_alert(context, address, token)
    if not address:
        return set_up_later()
    if outside_request.wrong_address(private) and guided_setup.interactive(context):
        return asked_again(context)
    return install(context, address, token) or tested_again(context, private, address, token)


def tested_again(context, private, address, token):
    """A saved destination whose test did not pass (cut off, not taken, or changed since:
    install keeps the record only for the same destination) is tested again: a retry
    never passes without a request the server took."""
    if outside_request.untested(private):
        return test_alert(context, address, token)
    return None


def asked_again(context):
    """The alert server had no topic at the saved address: the form again, then a test."""
    context.log('Phone alerts: the saved address was not found at the alert server, so it is asked again.')
    address, token = alerts_form(context)
    if not address:
        return set_up_later()
    return install(context, address, token) or test_alert(context, address, token)


def install(context, address, token=''):
    """Save the destination (root-only) and start the alert timer. For a caller that has
    the address and the token already (the setup window's alerts form): nothing is asked."""
    timing = options(context.config)
    private = safe_path(PRIVATE_CONFIG)
    if not endpoint(address) or not valid_token(token):
        raise RuntimeError('Invalid private notification destination or token')
    project = Path(__file__).resolve().parents[1]
    binary = native_runtime.deploy(
        project, context.config, Path('/usr/local/lib/nekoserver-notifications') / ('v' + VERSION)
    )
    if not shutil.which('curl'):
        raise RuntimeError('Install curl and system CA certificates before enabling HTTPS delivery')
    units = definitions(binary, private)
    check_units(units, 'notification')
    kept = outside_request.kept_test(private, endpoint=address, token=token)
    atomic_json(private, {'endpoint': address, 'token': token, **timing, **kept})
    write_units(units)
    for cmd in (['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', 'nekoserver-notifications.timer']):
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    context.log(
        'Background notification timer enabled; verify delivery on your subscribed phone using the documented native test command.'
    )
