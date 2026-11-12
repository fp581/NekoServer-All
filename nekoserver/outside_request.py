"""One test request from setup to a service outside this home: the phone-alert server
(ntfy) or the outage monitor. It follows the rules the scheduled Rust jobs follow
(common.rs deliver): HTTPS only, no proxy, no redirect followed, a time limit.

The address and any access token are secrets. They are used for the request and nothing
else: this module logs nothing, prints nothing, starts no program (so nothing is in a
command line) and returns only a word for what happened and the answer's number.
"""

import http.client
import json
import ssl
import urllib.error
import urllib.request

from neko_common import NoRedirect, atomic_json

# The longest one request may take, as the scheduled jobs' own limit (curl --max-time 20).
TIME_LIMIT_SECONDS = 20

# What a machine check found, and what a person saw (they are never mixed up).
ACCEPTED = 'accepted'  # the server answered yes (a 2xx answer)
REFUSED = 'refused'  # the server answered no; the answer's number says more
REDIRECTED = 'redirected'  # the server pointed somewhere else; that is never followed
TIMED_OUT = 'timed_out'  # no answer within the time limit
UNREACHABLE = 'unreachable'  # no connection: no internet, a wrong name, a closed door
UNTRUSTED = 'untrusted'  # the server's certificate could not be checked
NOT_SENT = 'not_sent'  # nothing was sent (the address or the token was not acceptable)
OBSERVED = 'observed'  # a person saw it happen
NOT_TESTED = 'not_tested'  # nobody has seen it happen yet

# What a "no" from the server means, told apart because each asks something else of the
# person (gate run C12): only a wrong address asks for the address again.
WRONG_ADDRESS = 'wrong_address'  # 404 or 410: nothing is at this address
BUSY = 'busy'  # 429 or 503: the server asks to try again later
SERVER_ERROR = 'server_error'  # any other 5xx: their server has a problem right now


def kind(happened, status):
    """The word for what happened, with a "no" told apart by its number: a wrong
    address, a busy server, a server error. Any other "no" stays REFUSED, and a request
    with no answer keeps its own word (TIMED_OUT, UNREACHABLE, UNTRUSTED)."""
    if happened == ACCEPTED or status is None:
        return happened
    if status in (404, 410):
        return WRONG_ADDRESS
    if status in (429, 503):
        return BUSY
    if 500 <= status <= 599:
        return SERVER_ERROR
    return happened


# What the last test of a saved address found is kept beside the address, in the same
# root-only file, so that a retry tests it again instead of calling the step done
# (gate run C12), and asks for the address again only when the address was wrong.


def _saved(private):
    try:
        saved = json.loads(private.read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def untested(private):
    """The saved address has no passed test: a run cut off after saving it, a test the
    server did not take, or a destination changed since. Addresses saved before 0.50.0
    have no record either way: those stand."""
    return _saved(private).get('tested') is False


def wrong_address(private):
    """The last test of the saved address found nothing at that address."""
    return _saved(private).get('answer') == WRONG_ADDRESS


def mark_tested(private, answer):
    """Keep what the test of the saved address found (its word, never the address)."""
    saved = _saved(private)
    if not saved:
        return
    saved.update(tested=answer == ACCEPTED, answer=answer)
    atomic_json(private, saved)


def kept_test(private, **destination):
    """The test record to save with `destination`: the earlier one when the destination
    is the same as before, otherwise "not tested yet", so a new address is always tested."""
    saved = _saved(private)
    if saved and all(saved.get(key, '') == value for key, value in destination.items()):
        return {key: saved[key] for key in ('tested', 'answer') if key in saved}
    return {'tested': False}


def observed(seen):
    """A person's answer to "did you see it?" as it is recorded: observed only for a
    plain yes. No is not a failure of the server's part, so it stays not tested."""
    return OBSERVED if seen is True else NOT_TESTED


def careful_opener():
    """Sends requests directly (no proxy from the environment) and follows no redirect."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())


def _why_not(error):
    """The word for a request that got no answer."""
    reason = getattr(error, 'reason', error)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return UNTRUSTED
    if isinstance(reason, TimeoutError):
        return TIMED_OUT
    return UNREACHABLE


def send(url, method='GET', headers=None, body=None, opener=None):
    """Send one request. Returns (what happened, the answer's number or None).

    `url` must already have passed its own check (notifications.endpoint or
    heartbeat.endpoint); anything that is not https:// is not sent at all. `opener` is
    for tests: anything with open(request, timeout=...).
    """
    if not isinstance(url, str) or not url.startswith('https://'):
        return NOT_SENT, None
    request = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with (opener or careful_opener()).open(request, timeout=TIME_LIMIT_SECONDS) as answer:
            status = answer.status
    except urllib.error.HTTPError as error:
        error.close()
        return (REDIRECTED if 300 <= error.code < 400 else REFUSED), error.code
    except (OSError, http.client.HTTPException) as error:  # URLError is an OSError
        return _why_not(error), None
    return (ACCEPTED if 200 <= status < 300 else REFUSED), status
