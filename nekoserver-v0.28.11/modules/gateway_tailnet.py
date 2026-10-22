"""Private Tailscale HTTPS with guided DNS, certificate and phone handoffs."""
import guided_setup
import gateway_tools
import phone_guide
from neko_common import atomic_json
from pathlib import Path
import json

DEPENDENCIES = ['storage', 'recovery', 'tailscale']
DESCRIPTION = 'Private Tailscale addresses for the everyday apps, with phone setup for each app'


def phone_steps(ctx, result):
    """Guide the phone, one app at a time, using the QR page for every address."""
    links = result['links']
    try:
        page = phone_guide.write_page(ctx.config, links)
        page_target = [('page', str(page))]
        first = ('A page with QR codes opened on this PC. Keep it open: every app below has a code '
                 'you can scan with the phone camera instead of typing the address.')
    except (OSError, RuntimeError) as exc:
        page_target = []
        ctx.log(f'Phone setup page not written ({exc}); the addresses are typed in by hand instead')
        first = ('The addresses to type into the phone are shown below, app by app '
                 '(the QR-code page could not be made on this PC).')
    if not guided_setup.checkpoint(ctx, 'Phone: install and connect Tailscale', [
        first,
        'On your PHONE install "Tailscale" from the App Store or Google Play '
        '(or scan the first QR code on the page).',
        'Open Tailscale on the phone, allow it to add a VPN, and sign in with the SAME '
        'account you just used for this server.',
        'Turn Tailscale on. It must be on whenever you use your apps, at home or away.',
        'Answer yes once the Tailscale app on the phone shows "Connected".',
    ], [*page_target, ('url', phone_guide.TAILSCALE_DOWNLOAD)]):
        return False
    for app in phone_guide.APPS:
        if app not in links:
            continue
        if not guided_setup.checkpoint(ctx, 'Phone: ' + phone_guide.APPS[app]['title'],
                                       phone_guide.instructions(ctx.config, app, links[app])):
            return False
    return guided_setup.checkpoint(ctx, 'Phone: check it works away from home', [
        'Turn OFF Wi-Fi on the phone so it uses mobile data. Keep Tailscale on.',
        'Open one or two of the apps and load something new: a photo, a song or a video.',
        'If that works, your apps work anywhere. Turn Wi-Fi back on.',
        'Answer yes once it worked on mobile data.',
    ], page_target)


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
                ctx.log('Tailscale: MagicDNS and HTTPS already on; secure addresses updated')
                return result
        except RuntimeError as exc:
            ctx.log('Tailscale addresses: ' + str(exc))
            return {'status': 'needs_setup', 'reason': 'Secure addresses not set up: ' + str(exc)}
    if not interactive:
        configure_routes()
        return result
    if guided_setup.checkpoint(ctx, 'Tailscale: turn on secure addresses', [
        'A Tailscale settings page (DNS) opened in the browser.',
        'Find "MagicDNS" and make sure it is ON (click "Enable MagicDNS" if you see it).',
        'Scroll to "HTTPS Certificates" and click "Enable HTTPS". Tailscale explains '
        'that your server\'s name becomes visible in public certificate logs; your '
        'files and apps do not. Accept to continue.',
        'Nothing else to change. Your apps stay private to the devices on your '
        'Tailscale account; nothing is opened to the public internet.',
        'Answer yes. The installer then creates a secure address for each app. '
        'If that fails, check both settings above are on and answer no to try again.',
    ], [('url', 'https://console.tailscale.com/admin/dns')], verify=configure_routes):
        return result
    return {'status': 'needs_setup', 'reason': 'Private HTTPS configuration pending; reapply to resume'}


def phone_done(earlier, links):
    """The phone was confirmed for these app addresses. An address added since then
    that needs no app (the dashboard) does not mean starting the phone over."""
    if not earlier:
        return False
    same = all(links.get(app) == url for app, url in earlier.items())
    return same and set(links) - set(earlier) <= {'dashboard'}


def run(ctx):
    gateway_tools.selected(ctx.config)
    if not ctx.apply:
        ctx.log('Private gateway preview: browser/phone instructions and prompts run only during interactive apply')
        return
    if not gateway_tools.configure(ctx, 'tailnet'):
        return {'status': 'needs_setup', 'reason': 'Selected app security checks/confirmation still pending'}
    # Read before the routes are applied: applying rewrites the report.
    earlier = confirmed_links(ctx.config)
    result = secure_addresses(ctx)
    if result.get('status') == 'needs_setup':
        return result
    ctx.log('Private application links saved in reports/gateway-tailnet.json')
    if phone_done(earlier, result.get('links') or {}):
        try:
            phone_guide.write_page(ctx.config, result['links'])
        except (OSError, RuntimeError):
            pass
        ctx.log('Phone: already set up for these addresses; not asking again')
        confirmed = True
    else:
        confirmed = phone_steps(ctx, result)
    result['client_acceptance'] = 'user_confirmed' if confirmed else 'needs_setup'
    result['client_acceptance_evidence'] = 'Confirmed by you in the terminal; no independent phone probe'
    atomic_json(report_path(ctx.config), result)
    if not confirmed:
        return {'status': 'needs_setup', 'reason': 'Private routes configured; actual phone setup/mobile-data test remains pending'}
    return result
