"""Private Tailscale HTTPS with guided DNS, certificate and phone handoffs."""

import guided_setup
import gateway_tools
import phone_guide
from neko_common import atomic_json
from pathlib import Path
import json

DEPENDENCIES = ['storage', 'recovery', 'tailscale']
DESCRIPTION = 'Private Tailscale addresses for the everyday apps, with phone setup for each app'


def refresh(config, log):
    """After every setup run: a dashboard moved to another port keeps its phone address
    working (only that route changes). Nothing happens without NekoServer's routes."""
    return gateway_tools.refresh_dashboard_route(config, log)


PHONE_QUESTION = 'phone_ready'  # its words are in questions.py


def phone_steps(ctx, result, done=()):
    """The phone, as one part: Tailscale, then each app (the QR page has every address),
    then a check away from home, listed in order with one question at the end. In the
    setup window each step has its own sliding yes. `done`: the steps confirmed in an
    earlier run for these same addresses, which are not asked for again. Returns the ids
    of the steps confirmed; the phone is set up when that is all of them (phone_ids)."""
    links = result['links']
    # What setup can see for itself, before anyone is asked anything (phone_status.py).
    status = phone_rows(ctx)
    if any(row['id'] == 'phone' and row['status'] == 'pass' for row in status) and 'tailscale' not in done:
        # A phone is signed in to the account: that step is seen, not asked about.
        done = [*done, 'tailscale']
        ctx.log('Phone: a phone is signed in to your Tailscale account (seen by setup, not asked)')
    try:
        page = phone_guide.write_page(ctx.config, links)
        page_target = [('page', str(page))]
        first = (
            'A page with QR codes opened on this PC. Keep it open: every app below has a code '
            'you can scan with the phone camera instead of typing the address.'
        )
    except (OSError, RuntimeError) as exc:
        page_target = []
        ctx.log(f'Phone setup page not written ({exc}); the addresses are typed in by hand instead')
        first = (
            'The addresses to type into the phone are shown below, app by app '
            '(the QR-code page could not be made on this PC).'
        )
    steps = [
        {
            'id': 'tailscale',
            'title': 'Tailscale on the phone',
            'lines': [
                'On your PHONE install "Tailscale" from the App Store or Google Play '
                '(or scan the first QR code on the page).',
                'Open Tailscale on the phone, allow it to add a VPN, and sign in with the SAME '
                'account you just used for this server.',
                'Turn Tailscale on. It must be on whenever you use your apps, at home or away.',
                'Done when the Tailscale app on the phone shows "Connected".',
            ],
        }
    ]
    for app in phone_guide.APPS:
        if app in links:
            steps.append(
                {
                    'id': app,
                    'title': phone_guide.APPS[app]['title'],
                    'lines': phone_guide.instructions(ctx.config, app, links[app], asks=False),
                    'logins': [phone_guide.login_of(ctx.config, app)] if app in phone_guide.LOGIN else [],
                    # The same step in the setup window: short, with buttons instead of commands.
                    'window': phone_guide.window_step(ctx.config, app, links[app]),
                }
            )
    steps.append(
        {
            'id': AWAY,
            'title': 'Check it works away from home',
            'lines': [
                'Turn OFF Wi-Fi on the phone so it uses mobile data. Keep Tailscale on.',
                'Open one or two of the apps and load something new: a photo, a song or a video.',
                'If that works, the phone reached this server without your home Wi-Fi. Turn Wi-Fi back on.',
                'Setup cannot see this for you: only you can say that it worked.',
            ],
        }
    )
    confirmed = guided_setup.part(
        ctx,
        'Use your apps away from home',
        steps,
        PHONE_QUESTION,
        done=done,
        intro=[first, *status_lines(status)],
        targets=[*page_target, ('url', phone_guide.TAILSCALE_DOWNLOAD)],
        status=status,
    )
    if AWAY in confirmed and AWAY not in done:
        observed(ctx, links)
    return confirmed


# What each status row is called on the phone page (an app's row has its own title).
STATUS_TITLES = {
    'server': 'This server is signed in to Tailscale',
    'https': 'Private HTTPS addresses',
    'phone': 'A phone in your Tailscale account',
}
STATUS_MARKS = {'pass': 'OK', 'fail': 'NOT YET', 'unknown': 'NOT KNOWN'}


def phone_rows(ctx):
    """The phone page's status rows, as setup checked them just now: this server signed
    in, the private HTTPS addresses, a phone in the account, each app. Every row is
    pass, fail or unknown with its reason and what to do next; unknown is never shown as
    a pass. [] when the check itself cannot run (said in the log, never a failure of
    the step)."""
    import phone_status

    try:
        rows = phone_status.check(ctx.config)['rows']
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        ctx.log(
            f'Phone: setup could not check the connection itself ({type(exc).__name__}); the steps are shown as they are'
        )
        return []
    return [
        {
            'id': row['id'],
            'status': row['status'],
            'title': STATUS_TITLES.get(row['id']) or row.get('title') or row['id'],
            'reason': row.get('reason', ''),
            'next': row.get('next', ''),
            'shows': row.get('shows', ''),
        }
        for row in rows
    ]


def status_lines(rows):
    """The status rows as the terminal lists them."""
    lines = []
    for row in rows:
        lines.append(f"  [{STATUS_MARKS.get(row['status'], 'NOT KNOWN')}] {row['title']}: {row['reason']}")
        for more in (row['next'], row['shows']):
            if more:
                lines.append('        ' + more)
    return ['What setup checked itself:', *lines] if lines else []


def observed(ctx, links):
    """The person said an app opened on the phone with Wi-Fi off: kept as their
    observation (phone_status), which no check of setup's can stand in for."""
    import phone_status

    try:
        addresses = {app: links[app] for app in gateway_tools.selected(ctx.config) if app in links}
        phone_status.record_observation(ctx.config, True, addresses)
    except (OSError, RuntimeError, ValueError, KeyError):
        ctx.log(
            'Phone: your "it worked away from home" could not be saved for the status page; it is asked again next time'
        )


AWAY = 'away_from_home'


def phone_ids(links):
    """Every step of the phone part for these addresses, in order."""
    return ['tailscale', *[app for app in phone_guide.APPS if app in links], AWAY]


def steps_done_before(config, links):
    """The phone steps confirmed in an earlier run that was left part-way (the window's
    switches), for these same addresses: they are not asked for again (the setup review,
    S5: leaving one app for later made every confirmed step come round again)."""
    try:
        old = json.loads(report_path(config).read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(old, dict) or old.get('links') != links:
        return []
    kept = old.get('phone_steps_done')
    return [step for step in kept if step in phone_ids(links)] if isinstance(kept, list) else []


def report_path(config):
    return Path(config['data_mount']) / 'reports/gateway-tailnet.json'


def confirmed_links(config):
    """The app addresses a phone was confirmed for in an earlier run, or None."""
    try:
        old = json.loads(report_path(config).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(old, dict) and old.get('client_acceptance') == 'user_confirmed':
        return old.get('links')
    return None


def keep_confirmed(config, earlier):
    """The routes were set up again for the addresses the phone was confirmed for, and the
    step now waits for something else (Tailscale's certificate): the phone stays confirmed,
    so the next run does not ask for the phone steps again."""
    try:
        report = json.loads(report_path(config).read_text())
    except (OSError, ValueError):
        return
    if (
        isinstance(report, dict)
        and report.get('client_acceptance') != 'user_confirmed'
        and phone_done(earlier, report.get('links') or {})
    ):
        report['client_acceptance'] = 'user_confirmed'
        report['client_acceptance_evidence'] = 'Confirmed by you in the terminal; no independent phone probe'
        atomic_json(report_path(config), report)


def answering(result):
    """Routes in place are not enough (S03): without Tailscale's certificate every
    address hangs at the TLS handshake, so the step waits for setup instead."""
    if result.get('status') == 'configured':
        problem = gateway_tools.https_problem(result.get('links') or {})
        if problem:
            return {'status': 'needs_setup', 'reason': problem}
    return result


def secure_addresses(ctx):
    """Turn on Tailscale HTTPS addresses for the apps; asks only if HTTPS is not on yet."""
    result = {}

    def configure_routes():
        nonlocal result
        result = gateway_tools.apply_tailnet(ctx.config)
        return result.get('status') == 'configured'

    interactive = guided_setup.interactive(ctx)
    if interactive and gateway_tools.https_ready():
        # MagicDNS and HTTPS are on: a failure here is something else (an app not
        # running, a route someone else made), so it is reported as it is rather than
        # sending you to the DNS settings page again.
        try:
            if configure_routes():
                checked = answering(result)
                if checked is result:
                    ctx.log('Tailscale: MagicDNS and HTTPS already on; secure addresses updated')
                return checked
        except RuntimeError as exc:
            ctx.log('Tailscale addresses: ' + str(exc))
            return {'status': 'needs_setup', 'reason': 'Secure addresses not set up: ' + str(exc)}
    if not interactive:
        configure_routes()
        return answering(result)
    if guided_setup.checkpoint(
        ctx,
        'Tailscale: turn on secure addresses',
        [
            'A Tailscale settings page (DNS) opened in the browser.',
            'Find "MagicDNS" and make sure it is ON (click "Enable MagicDNS" if you see it).',
            'Scroll to "HTTPS Certificates" and click "Enable HTTPS". Tailscale explains '
            'that your server\'s name becomes visible in public certificate logs; your '
            'files and apps do not. Accept to continue.',
            'Nothing else to change. Your apps stay private to the devices on your '
            'Tailscale account; nothing is opened to the public internet.',
            'Answer yes. The installer then creates a secure address for each app. '
            'If that fails, check both settings above are on and answer no to try again.',
        ],
        [('url', 'https://console.tailscale.com/admin/dns')],
        verify=configure_routes,
        ask='tailscale_https',
    ):
        return answering(result)
    return {'status': 'needs_setup', 'reason': 'Private HTTPS configuration pending; reapply to resume'}


def phone_done(earlier, links):
    """The phone was confirmed for these app addresses. An address added since then
    that needs no app (the dashboard) does not mean starting the phone over."""
    if not earlier:
        return False
    same = all(links.get(app) == url for app, url in earlier.items())
    return same and set(links) - set(earlier) <= {'dashboard'}


def phone_confirmed(ctx, earlier, result):
    """Whether the phone is set up for these addresses: confirmed in an earlier run (not
    asked again), or every phone step confirmed now. The steps done so far are kept in
    `result` for the next run when some are left."""
    links = result.get('links') or {}
    if phone_done(earlier, links):
        try:
            phone_guide.write_page(ctx.config, result['links'])
        except (OSError, RuntimeError):
            pass
        ctx.log('Phone: already set up for these addresses; not asking again')
        return True
    done = phone_steps(ctx, result, steps_done_before(ctx.config, links))
    confirmed = all(step in done for step in phone_ids(links))
    if not confirmed:
        # Kept for the next run, with what is done already.
        result['phone_steps_done'] = [step for step in phone_ids(links) if step in done]
    return confirmed


def run(ctx):
    gateway_tools.selected(ctx.config)
    if not ctx.apply:
        ctx.log('Private gateway preview: browser/phone instructions and prompts run only during interactive apply')
        return
    if not gateway_tools.configure(ctx, 'tailnet'):
        return {'status': 'needs_setup', 'reason': gateway_tools.waiting_said(ctx.config)}
    # Read before the routes are applied: applying rewrites the report.
    earlier = confirmed_links(ctx.config)
    result = secure_addresses(ctx)
    if result.get('status') == 'needs_setup':
        keep_confirmed(ctx.config, earlier)
        return result
    ctx.log('Private application links saved in reports/gateway-tailnet.json')
    confirmed = phone_confirmed(ctx, earlier, result)
    result['client_acceptance'] = 'user_confirmed' if confirmed else 'needs_setup'
    result['client_acceptance_evidence'] = 'Confirmed by you in the terminal; no independent phone probe'
    atomic_json(report_path(ctx.config), result)
    if not confirmed:
        return {
            'status': 'needs_setup',
            'reason': 'Private routes configured; actual phone setup/mobile-data test remains pending',
        }
    return result
