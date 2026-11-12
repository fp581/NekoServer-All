"""Configure an OS-local Rust heartbeat to an independently hosted monitor."""

import guided_setup
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import native_runtime
import outside_request
from neko_common import VERSION, atomic_json, safe_path, check_units, write_units

DEPENDENCIES = ['system', 'recovery']
DESCRIPTION = 'Rust HTTPS heartbeat for independent missing-server alerts'
PRIVATE = '/etc/nekoserver-heartbeat/config.json'


def endpoint(value):
    if not isinstance(value, str):
        return False
    match = re.fullmatch(r'https://[A-Za-z0-9][A-Za-z0-9.-]*(?::(\d{1,5}))?/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*', value)
    return bool(
        match
        and len(value) <= 2048
        and value.rsplit('/', 1)[-1] not in ('start', 'fail')
        and (match[1] is None or 0 < int(match[1]) <= 65535)
    )


def interval(config):
    opts = config.get('heartbeat', {})
    if not isinstance(opts, dict):
        raise RuntimeError('heartbeat must be an object')
    value = opts.get('interval_seconds', 300)
    if type(value) is not int or not 60 <= value <= 3600:
        raise RuntimeError('heartbeat interval must be 60..3600 seconds')
    return value


def plain_time(seconds):
    """A number of seconds as a person sets it at the monitor: "5 minutes", "90 seconds"."""
    if seconds % 3600 == 0:
        number, unit = seconds // 3600, 'hour'
    elif seconds % 60 == 0:
        number, unit = seconds // 60, 'minute'
    else:
        number, unit = seconds, 'second'
    return f'{number} {unit}' + ('' if number == 1 else 's')


def schedule(config):
    """What to set at the outside monitor so it agrees with this server's check-ins.

    The period is the timer's own (OnUnitInactiveSec in definitions, from
    heartbeat.interval_seconds; five minutes unless changed). The grace time is twice
    the period: one check-in may fail and the next one follows a period and up to 40
    seconds later (the job's time limit), so a single lost check-in raises no warning.
    The monitor warns about period + grace after the last check-in it got.

    Returns {'interval_seconds', 'grace_seconds', 'interval_words', 'grace_words',
             'warns_after_seconds', 'warns_after_words'}.
    """
    period = interval(config)
    grace = 2 * period
    return {
        'interval_seconds': period,
        'grace_seconds': grace,
        'interval_words': plain_time(period),
        'grace_words': plain_time(grace),
        'warns_after_seconds': period + grace,
        'warns_after_words': plain_time(period + grace),
    }


def send_test_checkin(address, opener=None):
    """Send one check-in to the monitor's ping address, as the scheduled job does, and
    say what the monitor answered.

    Returns {'address': 'ok' | 'invalid',
             'server': 'accepted' | 'rejected' | 'timed_out' | 'unreachable' | 'untrusted'
                       | 'redirected' | 'not_sent',
             'http_status': the answer's number, or None when there was no answer,
             'timing': 'not_tested',
             'warning': 'not_tested'}
    "accepted" means the monitor took one check-in. Whether its period and grace time
    are set as schedule() says, and whether its warning reaches the person, are theirs
    to look at and are kept apart (outside_request.observed).
    The address is in no log, no command line and not in the result.
    """
    result = {
        'address': 'ok' if endpoint(address) else 'invalid',
        'server': outside_request.NOT_SENT,
        'http_status': None,
        'timing': outside_request.NOT_TESTED,
        'warning': outside_request.NOT_TESTED,
    }
    if result['address'] != 'ok':
        return result
    happened, status = outside_request.send(address, 'GET', None, None, opener)
    from modules.notifications import busy_or_broken

    word = 'rejected' if happened == outside_request.REFUSED else happened
    result.update(server=busy_or_broken(status) or word, http_status=status)
    return result


def set_up_later():
    """The step's result when the person chooses "Set up outage warnings later": it stays
    on the list of things still to do, nothing is installed and nothing is claimed."""
    return {
        'status': 'needs_setup',
        'reason': 'Outage warnings are not set up yet. Everything else works without them. '
        'Run setup again when you want them.',
    }


FORM_TITLE = 'Warn me if the server stops checking in'
LATER = 'Leave the address empty, or press Skip for now, to set this up later. Nothing else waits for it.'
ADDRESS_HINT = (
    'That is not a check-in address. It starts with https:// and looks like https://hc-ping.com/1a2b3c4d-... '
    "Copy the Ping URL from the check's details, not the web page's address."
)


def form_lines(config):
    """What to do at the monitoring service, said with the form."""
    timing = schedule(config)
    return [
        '1. Open an account at a monitoring service outside your home, such as Healthchecks.io (free).',
        f"2. Add a check there. Period: {timing['interval_words']}. Grace time: {timing['grace_words']}. This server "
        f"checks in every {timing['interval_words']}; the service warns you when no check-in came for about "
        f"{timing['warns_after_words']}.",
        '3. Tell the service how to reach you (email, or its phone app). NekoServer cannot do that for you.',
        "4. Copy the check's Ping URL and put it in the box here.",
        'Setup then sends one test check-in.',
    ]


def monitor_form(context, ask=None):
    """The check-in address of the outside monitor, on one page with the steps: the
    address, or '' when it is left for later. Typed hidden and in no log."""
    import getpass
    import setup_progress

    ask = ask or getpass.getpass
    lines = form_lines(context.config)
    for line in lines:
        print(line)
    rows = [{'key': 'address', 'label': 'Check-in address (the Ping URL of your check)', 'secret': True}]
    with setup_progress.page('fields', {'address': ''}, what=FORM_TITLE, rows=rows, lines=lines, note=LATER):
        for _ in range(3):
            try:
                address = setup_progress.page_field(
                    'address', ask, 'The check-in address (hidden; Enter alone sets this up later): '
                )
            except EOFError:
                return ''
            address = address.strip().rstrip('/')
            if not address or endpoint(address):
                return address
            print(ADDRESS_HINT)
    return ''


# What each answer of the monitor means, in the person's words (send_test_checkin).
TEST_SAID = {
    'accepted': 'The monitor accepted a check-in. Open the check at the monitor and look for it in its log. '
    'Whether its warning reaches you is not tested yet: pause this server for a while, or use the '
    "monitor's own test, to see it.",
    'rejected': "The monitor refused the check-in. Copy the Ping URL from the check's details again; it is not "
    "the web page's address.",
    'busy': 'The monitor is busy and asked to try later. Retry this step in a few minutes.',
    'server_error': 'The monitor had a problem of its own (it answered with an error). Retry this step later; the '
    'address may be right.',
    'redirected': 'This address points somewhere else, and NekoServer does not follow that. Use the Ping URL itself.',
    'timed_out': 'The monitor did not answer in time. If it keeps happening, check the address.',
    'unreachable': "NekoServer could not reach the monitor. Check this server's internet connection and the "
    'spelling of the address.',
    'untrusted': "The monitor's security certificate could not be checked, so nothing was sent.",
    'not_sent': 'No test check-in was sent: the address is not one NekoServer can send to.',
}


def test_checkin(context, address):
    """One test check-in sent to the address just saved. None when the monitor took it
    (said with what that does and does not show); a needs_setup result with the reason
    when it did not."""
    from modules.notifications import mark_tested

    result = send_test_checkin(address)
    said = TEST_SAID.get(result['server'], TEST_SAID['not_sent'])
    context.log('Outage warning test: ' + said)
    mark_tested(safe_path(PRIVATE), result['server'] == 'accepted')
    if result['server'] == 'accepted':
        return None
    return {
        'status': 'needs_setup',
        'reason': 'The check-in address is saved, but the test check-in was not taken. ' + said,
    }


def definitions(config, binary, private):
    return {
        'nekoserver-heartbeat.service': (
            '[Unit]\nDescription=NekoServer independent availability heartbeat\nAfter=network-online.target\n'
            '[Service]\nType=oneshot\nUser=root\nUMask=0077\n'
            'RuntimeDirectory=nekoserver-heartbeat\nRuntimeDirectoryMode=0755\nRuntimeDirectoryPreserve=yes\n'
            f'ExecStart={binary} heartbeat --config {private}\n'
            'TimeoutStartSec=40\nNoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\nPrivateTmp=yes\n'
            'PrivateDevices=yes\nProtectKernelTunables=yes\nProtectKernelModules=yes\nProtectControlGroups=yes\n'
            'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX\n'
            'InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock\n'
        ),
        'nekoserver-heartbeat.timer': (
            '[Unit]\nDescription=NekoServer availability heartbeat schedule\n'
            f'[Timer]\nOnBootSec=90s\nOnUnitInactiveSec={interval(config)}s\nAccuracySec=1s\n'
            '[Install]\nWantedBy=timers.target\n'
        ),
    }


def saved_endpoint(private):
    """The ping URL set up before ('' when none), from its root-only file."""
    if not private.exists():
        return ''
    if private.stat().st_uid != 0 or private.stat().st_mode & 0o077:
        raise RuntimeError('Existing heartbeat configuration must be root-owned and private')
    return json.loads(private.read_text()).get('endpoint', '')


def run(context):
    interval(context.config)
    if not context.apply:
        context.log('Independent heartbeat available with --apply; configure a monitor outside this PC')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Heartbeat requires root')
    from modules._schedules import installed, switched_off

    # Skipped at the question saves "wanted": false too: only a monitor that was set up has
    # an outside check to pause (seen after an upgrade of a server that never had one).
    was_set_up = installed('nekoserver-heartbeat')
    if switched_off(context.config, 'heartbeat', 'nekoserver-heartbeat', context.log):
        context.log(
            'Outage monitor: off by choice. This server no longer checks in, so pause (or delete) the '
            'check at your outside monitor too, or it will report an outage it is expecting.'
            if was_set_up
            else 'Outage monitor: skipped by choice'
        )
        return
    private = safe_path(PRIVATE)
    address = saved_endpoint(private)
    if (
        not address
        and guided_setup.wanted(
            context,
            'heartbeat',
            'Outage alerts if the server goes offline',
            [
                'An outside service (such as Healthchecks.io) can email you when this server stops',
                'checking in, for example after a power cut. It needs a free account there.',
                'This is for advanced users; most people can skip it.',
            ],
        )
        is False
    ):
        context.log('Outage monitor: skipped by choice')
        return
    fresh = False
    if not address and sys.stdin.isatty():
        address = monitor_form(context)
        fresh = bool(address)
    if not address:
        return set_up_later()
    from modules.notifications import untested

    # An address just typed in, or one saved at a run whose test was not taken, is tried
    # for real before anything calls it set up.
    again = fresh or untested(private)
    return install(context, address) or (test_checkin(context, address) if again else None)


def install(context, address):
    """Save the ping address (root-only) and start the check-in timer. For a caller that
    has the address already (the setup window's outage form): nothing is asked."""
    private = safe_path(PRIVATE)
    if not endpoint(address):
        raise RuntimeError('Invalid heartbeat destination')
    if not shutil.which('curl'):
        raise RuntimeError('Install curl and system CA certificates before enabling heartbeat')
    project = Path(__file__).resolve().parents[1]
    binary = native_runtime.deploy(
        project, context.config, Path('/usr/local/lib/nekoserver-heartbeat') / ('v' + VERSION)
    )
    units = definitions(context.config, binary, private)
    check_units(units, 'heartbeat')
    atomic_json(private, {'endpoint': address})
    write_units(units)
    for args in (['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', 'nekoserver-heartbeat.timer']):
        subprocess.run(args, check=True, capture_output=True, timeout=30)
    context.log(
        'Independent heartbeat timer enabled. Verify the monitor receives pings and sends a missed-ping/recovery alert; provider alert configuration is separate.'
    )
