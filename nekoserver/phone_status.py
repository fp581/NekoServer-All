"""The status rows of the "connect your phone" page (wizard specification P08): what setup
can check by itself about Tailscale, the private HTTPS addresses, a phone and each app.

Every check gives a row: {'id', 'status', 'reason', 'next'} and, for some, more fields.
status is 'pass', 'fail' or 'unknown'. Unknown means setup could not tell; it is never
shown or counted as pass. reason says what was found, next what to do about it.

What these checks do and do not do:
- They only read: `tailscale status --json`, `tailscale serve status --json`, the saved
  logins' "verified" mark, and one HTTPS connection to each app's own address from this
  PC. Nothing is switched on or off here, and no route is changed.
- `tailscale cert` is never run, here or anywhere in setup. Tailscale fetches the
  certificate itself on the first HTTPS connection to an address that has a route
  (gateway_tools.https_problem makes that connection in the routes step). The connection
  made here is the same kind, and is only made to an address whose route is on.
- A phone seen in the Tailscale account shows that a phone signed in. It does not show
  that the phone opened an app, and nothing here can tell whether a phone is on Wi-Fi or
  mobile data: that one is a person's own observation, kept apart (record_observation).

The machine name and the addresses are private: they are in the rows the page shows (an
app's address has to be shown), never in a reason, a log line or anything printed.
"""

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import gateway_tools
from neko_common import atomic_json, safe_path

PASS, FAIL, UNKNOWN = 'pass', 'fail', 'unknown'
OBSERVED, NOT_TESTED = 'observed', 'not_tested'  # a person's observation, never a machine check
PHONE_SYSTEMS = {'ios': 'iPhone or iPad', 'android': 'Android phone'}
DNS_PAGE = 'https://login.tailscale.com/admin/dns'
MACHINES_PAGE = 'https://login.tailscale.com/admin/machines'
MACHINE_NAME = re.compile(r'[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+\.ts\.net')  # as gateway_tools.tailnode
SETUP_AGAIN = 'Run  sudo bash install.sh  again'
# One try for each address when the page checks (the routes step waits up to 90 seconds
# for the first certificate; the page says "not yet" and can be asked again).
ANSWER_SECONDS = 8


def row(row_id, status, reason, next_step='', **more):
    return {'id': row_id, 'status': status, 'reason': reason, 'next': next_step, **more}


# ----- reading Tailscale


def read_status(command=gateway_tools.command):
    """`tailscale status --json` as a dict, or None when it cannot be asked or its answer
    is not what Tailscale writes. Never raises."""
    try:
        value = json.loads(command(['tailscale', 'status', '--json']))
    except (RuntimeError, ValueError, TypeError, OSError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get('BackendState'), str) else None


def read_routes(command=gateway_tools.command):
    """The Tailscale ports that have a private HTTPS route now, as a set of text numbers,
    or None when that cannot be read (or the routes are of a kind NekoServer leaves alone,
    such as a Funnel)."""
    try:
        state = gateway_tools.canonical(json.loads(command(['tailscale', 'serve', 'status', '--json'])))
    except (RuntimeError, ValueError, TypeError, OSError, AttributeError):
        return None
    tcp = state.get('TCP')
    if not isinstance(tcp, dict):
        return None
    return {str(port) for port, value in tcp.items() if isinstance(value, dict) and value.get('HTTPS') is True}


def _own(status):
    own = status.get('Self') if isinstance(status, dict) else None
    return own if isinstance(own, dict) else {}


def machine_name(status):
    """This server's Tailscale name ('' when it has none yet)."""
    name = _own(status).get('DNSName')
    name = name.rstrip('.') if isinstance(name, str) else ''
    return name if MACHINE_NAME.fullmatch(name) else ''


# ----- row 1: this server is signed in

NOT_ASKED = (
    'Setup could not ask Tailscale how it is doing.',
    f'{SETUP_AGAIN}: its Tailscale step installs and starts Tailscale. Then check again.',
)
WHY_NOT_RUNNING = {
    'NeedsLogin': (
        FAIL,
        'This server is not signed in to Tailscale.',
        'Use "Sign in to Tailscale" here (or run  sudo tailscale up  and open the link it prints), '
        'with the account you will also use on your phone.',
    ),
    'NeedsMachineAuth': (
        FAIL,
        'This server signed in, and your Tailscale account still has to approve it.',
        f'Open {MACHINES_PAGE} , find this server and choose "Approve". Then check again.',
    ),
    'Stopped': (
        FAIL,
        'Tailscale is switched off on this server.',
        'Run  sudo tailscale up  to switch it on. Then check again.',
    ),
    'Starting': (UNKNOWN, 'Tailscale is still starting on this server.', 'Wait a minute, then check again.'),
    'NoState': (UNKNOWN, 'Tailscale is still starting on this server.', 'Wait a minute, then check again.'),
}


def server_signed_in(status):
    """Row 1. `status`: what read_status gave."""
    if status is None:
        return row('server', UNKNOWN, *NOT_ASKED)
    state = status['BackendState']
    if state != 'Running':
        known = WHY_NOT_RUNNING.get(state)
        if known:
            return row('server', *known)
        return row('server', UNKNOWN, 'Tailscale gave a state setup does not know.', 'Wait a minute, then check again.')
    if not _own(status):
        return row('server', UNKNOWN, 'Tailscale runs, but did not say which device this server is.', 'Check again.')
    return row('server', PASS, 'This server is signed in to Tailscale.')


# ----- row 2: private HTTPS addresses


def app_addresses(config, status):
    """{app: its private HTTPS address} for the selected apps, as the routes step writes
    them into reports/gateway-tailnet.json; {} while this server has no Tailscale name."""
    name = machine_name(status)
    if not name:
        return {}
    return {app: f'https://{name}:{gateway_tools.APPS[app][2]}/' for app in gateway_tools.selected(config)}


def _answers(addresses, routes, status, probe):
    """For each address: PASS when it answers over HTTPS with a certificate this PC trusts,
    FAIL when it was tried and did not, UNKNOWN when it was not tried (no route yet: a
    try would prove nothing, and must not be what makes Tailscale ask for a certificate)."""
    own_address = gateway_tools.tailscale_address(status)
    found = {}
    for app, address in addresses.items():
        parts = urlsplit(address)
        if routes is None or str(parts.port) not in routes:
            found[app] = UNKNOWN
        else:
            found[app] = PASS if probe(parts.hostname, parts.port, own_address, ANSWER_SECONDS) else FAIL
    return found


def private_https(status, addresses, routes, probe=gateway_tools.https_answers, why=None):
    """Row 2. `addresses`: app_addresses; `routes`: read_routes. `why()` gives Tailscale's
    own reason for a missing certificate ('' when it says none); default: its log.

    Extra fields: 'name_service' (MagicDNS on), 'certificates_allowed' (HTTPS switched on
    for the account), 'certificate' (one was really presented for this server's name),
    each pass / fail / unknown, and 'answers': {app: pass / fail / unknown}.
    """
    parts = {'name_service': UNKNOWN, 'certificates_allowed': UNKNOWN, 'certificate': UNKNOWN, 'answers': {}}
    if status is None or status['BackendState'] != 'Running':
        return row(
            'https',
            UNKNOWN,
            'This is checked once this server is signed in to Tailscale.',
            'Do the step above first.',
            **parts,
        )
    name = machine_name(status)
    tailnet = status.get('CurrentTailnet')
    switched = tailnet.get('MagicDNSEnabled') if isinstance(tailnet, dict) else None
    parts['name_service'] = PASS if switched is True and name else FAIL if switched is False else UNKNOWN
    if parts['name_service'] == FAIL:
        return row(
            'https',
            FAIL,
            'MagicDNS is off in your Tailscale account, so this server has no name the phone can use.',
            f'Open {DNS_PAGE} and choose "Enable MagicDNS". Then check again.',
            **parts,
        )
    if parts['name_service'] == UNKNOWN:
        return row(
            'https', UNKNOWN, 'Tailscale did not say whether MagicDNS is on, or gave this server no name yet.',
            f'Open {DNS_PAGE} and make sure MagicDNS is on. Then check again.', **parts,
        )  # fmt: skip
    domains = status.get('CertDomains')
    parts['certificates_allowed'] = PASS if isinstance(domains, list) and name in domains else FAIL
    if parts['certificates_allowed'] == FAIL:
        return row(
            'https',
            FAIL,
            'HTTPS certificates are not switched on in your Tailscale account.',
            f'Open {DNS_PAGE} , scroll to "HTTPS Certificates" and choose "Enable HTTPS". This server\'s Tailscale '
            'name then appears in a public list of certificates (the name only: no files, no apps). Then check again.',
            **parts,
        )
    parts['answers'] = _answers(addresses, routes, status, probe)
    tried = [answer for answer in parts['answers'].values() if answer != UNKNOWN]
    if not addresses or not tried:
        return row(
            'https',
            UNKNOWN,
            'MagicDNS and HTTPS are on. No app address is switched on yet, so no certificate could be checked.',
            f'{SETUP_AGAIN}: its phone step switches the addresses on once every app login is verified.',
            **parts,
        )
    parts['certificate'] = PASS if PASS in tried else FAIL
    if parts['certificate'] == FAIL:
        said = (why or _tailscale_says)()
        return row(
            'https',
            FAIL,
            'Tailscale has not got the HTTPS certificate for this server yet, so the app addresses do not open.'
            + (' ' + said if said else ''),
            'The first one can take a minute. Check again; if it stays like this, check again in an hour.',
            **parts,
        )
    silent = [gateway_tools.APPS[app][0] for app, answer in parts['answers'].items() if answer == FAIL]
    if silent:
        return row(
            'https',
            FAIL,
            'The certificate is there, but the address of ' + ', '.join(silent) + ' does not answer.',
            f'Open the app on this PC to see that it runs. {SETUP_AGAIN} if it does not.',
            **parts,
        )
    waiting = [gateway_tools.APPS[app][0] for app, answer in parts['answers'].items() if answer == UNKNOWN]
    if waiting:
        return row(
            'https',
            UNKNOWN,
            'The addresses that are switched on answer over HTTPS. Not switched on yet: ' + ', '.join(waiting) + '.',
            'See each app below for why its address is off.',
            **parts,
        )
    return row('https', PASS, "Every selected app's private HTTPS address answers from this PC.", **parts)


def _tailscale_says():
    """Tailscale's own word on a missing certificate (its limit of certificates a week,
    or its health message), without this server's name in it."""
    import datetime as dt

    status = gateway_tools.tailscale_status()
    name = machine_name(status)
    if not name:
        return ''
    now = dt.datetime.now(dt.timezone.utc)
    return gateway_tools.certificate_why(name, status, gateway_tools.tailscale_log(), now).replace(name, 'this server')


# ----- row 3: a phone in the same Tailscale account

PHONE_HONESTLY = (
    'This shows that a phone signed in to Tailscale. It does not show that the phone opened an app, '
    'and setup cannot see whether a phone is on Wi-Fi or on mobile data.'
)


def phone_in_tailnet(status):
    """Row 3. Extra fields: 'phones' (how many phones and tablets are signed in),
    'connected_now' (how many of them are connected at this moment), 'same_account' (how
    many are signed in as the person this server is signed in as) and 'shows' (what this
    row does and does not prove, to be shown with it)."""
    extra = {'phones': 0, 'connected_now': 0, 'same_account': 0, 'shows': PHONE_HONESTLY}
    if status is None or status['BackendState'] != 'Running':
        return row(
            'phone',
            UNKNOWN,
            'This is checked once this server is signed in to Tailscale.',
            'Do the first step first.',
            **extra,
        )
    peers = status.get('Peer')
    if peers is None:
        peers = {}  # Tailscale leaves the list out when no other device is signed in
    if not isinstance(peers, dict):
        return row(
            'phone', UNKNOWN, 'Tailscale did not list the other devices of your account.', 'Check again.', **extra
        )
    owner = _own(status).get('UserID')
    kinds = []
    for peer in peers.values():
        system = peer.get('OS') if isinstance(peer, dict) else None
        if isinstance(system, str) and system.lower() in PHONE_SYSTEMS:
            kinds.append(PHONE_SYSTEMS[system.lower()])
            extra['phones'] += 1
            extra['connected_now'] += peer.get('Online') is True
            extra['same_account'] += owner is not None and peer.get('UserID') == owner
    if not kinds:
        return row(
            'phone',
            FAIL,
            'No phone is signed in to your Tailscale account yet.',
            'On the phone, install Tailscale, sign in with the SAME account as this server and switch it on. '
            'Then check again.',
            **extra,
        )
    what = ' and '.join(sorted(set(kinds)))
    if extra['connected_now']:
        reason = f'A phone is signed in to your Tailscale account and connected right now ({what}).'
    else:
        reason = f'A phone is signed in to your Tailscale account ({what}). It is not connected at this moment.'
    step = '' if extra['connected_now'] else 'To use the apps, switch Tailscale on in its app on the phone.'
    if not extra['same_account']:
        step = (step + ' ' if step else '') + (
            "It is signed in as another person of the account; that works when the account's rules let that "
            'person reach this server.'
        )
    return row('phone', PASS, reason, step, **extra)


# ----- row 4: each selected app


def login_check(config, app):
    """(status, reason) of the app's saved login: PASS only when the accounts step signed
    in with it. Not verified is not safe, so anything else keeps the app's address off."""
    import account_store

    name = gateway_tools.APPS[app][0]
    try:
        if not account_store.enabled(config):
            return UNKNOWN, 'NekoServer does not keep the logins on this server, so it cannot check that one.'
        entry = account_store.get(config, app)
    except (OSError, RuntimeError, KeyError, ValueError, TypeError):
        return UNKNOWN, f'The saved login of {name} could not be read.'
    if not isinstance(entry, dict):
        return UNKNOWN, f'{name} has no saved login yet.'
    if entry.get('state') == 'verified':
        return PASS, f'Setup signed in to {name} with its saved login.'
    return FAIL, f'Setup could not sign in to {name} with its saved login.'


def app_rows(config, status, routes, answers=None):
    """Row 4: one row for each selected app, in the order of the phone page.

    Each has 'app', 'title', 'address' (its private HTTPS address, '' while this server
    has no Tailscale name), 'login' (pass / fail / unknown), 'route' ('on', 'off' or
    'unknown': what Tailscale has now) and 'route_reason' (why it is off, '' when on).
    status is PASS only with the login verified, the route on and, when `answers` (from
    private_https) is given, the address answering. An address is never said to be on, or
    safe to switch on, while its login check is unknown or failed.
    """
    addresses = app_addresses(config, status)
    rows = []
    for app in gateway_tools.selected(config):
        title, _, port = gateway_tools.APPS[app]
        login, login_reason = login_check(config, app)
        route = 'unknown' if routes is None else 'on' if str(port) in routes else 'off'
        more = {'app': app, 'title': title, 'address': addresses.get(app, ''), 'login': login, 'route': route}
        answer = (answers or {}).get(app)
        if login != PASS:
            off = (
                login_reason + ' Its address stays off until its login is verified.'
                if route != 'on'
                else login_reason + ' Its address is on from an earlier run; run setup again so the login is checked.'
            )
            step = f'{SETUP_AGAIN}: the accounts step checks the logins and says what is wrong with one.'
            rows.append(row('app:' + app, login, off, step, route_reason=off if route != 'on' else '', **more))
        elif route == 'unknown':
            why = 'Setup could not read which addresses Tailscale has switched on.'
            rows.append(row('app:' + app, UNKNOWN, why, 'Check again.', route_reason=why, **more))
        elif route == 'off':
            why = f'The login of {title} is verified. Its address is not switched on yet.'
            step = f'{SETUP_AGAIN}: its phone step switches the address on.'
            rows.append(row('app:' + app, FAIL, why, step, route_reason=why, **more))
        elif answer == FAIL:
            why = f'The address of {title} is on, but does not answer from this PC.'
            rows.append(row('app:' + app, FAIL, why, 'See "private HTTPS addresses" above.', route_reason='', **more))
        else:
            why = f'{title}: login verified, address on' + (' and answering.' if answer == PASS else '.')
            rows.append(row('app:' + app, PASS, why, route_reason='', **more))
    return rows


def route_may_open(app_row):
    """T19: an app's address may be switched on only when its login check passed."""
    return app_row.get('login') == PASS


# ----- rows 5 and 6: what is kept between runs, and the person's own observation


def saved_path(config):
    return safe_path(Path(config['data_mount']) / 'reports' / 'phone-status.json')


def _saved(config):
    try:
        value = json.loads(saved_path(config).read_text())
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return {}
    if not isinstance(value, dict) or value.get('format') != 'neko-phone-status-v1':
        return {}
    return value


def depends_on(row_id, addresses):
    """What a kept row stands on. The app rows and the observation stand on the addresses
    (as gateway_tailnet.phone_done: the dashboard's does not count, it needs no app); the
    rows about Tailscale itself stand on nothing kept, and are asked again each time."""
    if row_id.startswith('app:'):
        return addresses.get(row_id[4:], '')
    if row_id in ('https', 'observation'):
        return '\n'.join(f'{app} {address}' for app, address in sorted(addresses.items()) if app != 'dashboard')
    return ''


def remember(config, rows, addresses, now=None):
    """Keep the rows just checked, each with what it stands on and when it was checked;
    a row that passed before on the same addresses keeps the time it last passed
    ('last_passed'). What was checked now is what is saved: a check that fails or cannot
    tell is never covered by an older pass. Returns the rows as saved, for the page."""
    saved = _saved(config)
    before = saved.get('rows') if isinstance(saved.get('rows'), dict) else {}
    moment = now or time.time()
    kept = {}
    for fresh in rows:
        stands_on = depends_on(fresh['id'], addresses)
        old = before.get(fresh['id'])
        same = isinstance(old, dict) and old.get('stands_on') == stands_on
        passed = moment if fresh['status'] == PASS else old.get('last_passed') if same else None
        kept[fresh['id']] = {**fresh, 'stands_on': stands_on, 'checked_at': moment, 'last_passed': passed}
    atomic_json(saved_path(config), {**saved, 'format': 'neko-phone-status-v1', 'rows': kept})
    return [_shown(one) for one in kept.values()]


def _shown(saved_row):
    return {key: value for key, value in saved_row.items() if key != 'stands_on'}


def remembered(config, addresses):
    """The rows kept from the last check that still stand on the same addresses, by id:
    what the page shows when it is opened again, before anything is checked (each has
    'checked_at'). A row whose addresses changed since is left out: that one has to be
    checked again, the others do not (T23)."""
    rows = _saved(config).get('rows')
    found = {}
    for row_id, old in (rows if isinstance(rows, dict) else {}).items():
        if isinstance(old, dict) and old.get('stands_on') == depends_on(row_id, addresses):
            found[row_id] = _shown(old)
    return found


def record_observation(config, seen, addresses, now=None):
    """The one thing only a person can say: "I opened an app on mobile data with Tailscale
    connected." Kept apart from every machine check, for these addresses. Only a plain
    yes (True) is recorded as observed; anything else leaves it not tested."""
    saved = _saved(config)
    saved['observation'] = {
        'status': OBSERVED if seen is True else NOT_TESTED,
        'stands_on': depends_on('observation', addresses),
        'said_at': now or time.time(),
    }
    atomic_json(saved_path(config), {**saved, 'format': 'neko-phone-status-v1'})
    return observation(config, addresses)


def observation(config, addresses):
    """The observation row: {'id': 'observation', 'status': 'observed' | 'not_tested',
    'reason', 'next', 'by': 'person'}. Observed while it was said for these same addresses,
    here or in the phone step of an earlier run (modules/gateway_tailnet.py keeps that in
    reports/gateway-tailnet.json: steps_done_before, confirmed_links, keep_confirmed)."""
    said = _saved(config).get('observation')
    here = (
        isinstance(said, dict)
        and said.get('status') == OBSERVED
        and said.get('stands_on') == depends_on('observation', addresses)
    )
    if here or _said_in_the_phone_step(config, addresses):
        return row('observation', OBSERVED, 'You said you opened an app on mobile data.', by='person')
    return row(
        'observation',
        NOT_TESTED,
        'Nobody has tried an app on mobile data yet. Setup cannot check this for you.',
        'On the phone, switch Wi-Fi off, keep Tailscale on, and open one of the apps.',
        by='person',
    )


def _said_in_the_phone_step(config, addresses):
    """The phone step of an earlier run was told the same, for these same app addresses:
    its whole part confirmed, or its "away from home" step (the dashboard's address does
    not count, as in gateway_tailnet.phone_done)."""
    from modules import gateway_tailnet

    try:
        report = json.loads(gateway_tailnet.report_path(config).read_text())
    except (OSError, ValueError, KeyError, TypeError):
        return False
    links = report.get('links') if isinstance(report, dict) else None
    if not isinstance(links, dict) or not addresses:
        return False
    if {app: url for app, url in links.items() if app != 'dashboard'} != addresses:
        return False
    steps = report.get('phone_steps_done')
    return report.get('client_acceptance') == 'user_confirmed' or (
        isinstance(steps, list) and gateway_tailnet.AWAY in steps
    )


# ----- the whole page


def check(config, command=gateway_tools.command, probe=gateway_tools.https_answers, why=None, now=None, again=True):
    """Check the rows and keep the results.

    again=True ("Check connection and continue", and any visible retest): every row is
    checked now. again=False (the page opened again after a skip or a restart): what
    Tailscale and the saved logins say is read again, which costs nothing, but the HTTPS
    connections are not made again while the addresses are the ones that passed last time
    and their routes are still on; the kept HTTPS row is shown with 'kept': True.

    Returns {'rows': [server, https, phone, each app...], 'observation': row,
             'unresolved': [ids of the rows that did not pass]}.
    """
    status = read_status(command)
    routes = read_routes(command) if status is not None and status['BackendState'] == 'Running' else None
    addresses = app_addresses(config, status)
    kept = None if again else remembered(config, addresses).get('https')
    ports = {str(urlsplit(address).port) for address in addresses.values()}
    if kept and kept['status'] == PASS and addresses and routes is not None and ports <= routes:
        https = {**kept, 'kept': True}
    else:
        https = {**private_https(status, addresses, routes, probe, why), 'kept': False}
    rows = [
        server_signed_in(status),
        https,
        phone_in_tailnet(status),
        *app_rows(config, status, routes, https['answers']),
    ]
    if https['kept']:
        # Nothing new was learnt about it: its saved time stays the time it was checked.
        shown = remember(config, [one for one in rows if one is not https], addresses, now)
        shown.insert(1, https)
    else:
        shown = remember(config, rows, addresses, now)
    return {
        'rows': shown,
        'observation': observation(config, addresses),
        'unresolved': [one['id'] for one in shown if one['status'] != PASS],
    }
