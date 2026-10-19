"""Private Tailscale HTTPS with guided DNS, certificate and phone handoffs."""
import guided_setup
import gateway_tools
import phone_guide
from neko_common import atomic_json
from pathlib import Path

DEPENDENCIES = ['storage', 'recovery', 'tailscale']
DESCRIPTION = 'Private Tailscale addresses for the everyday apps, with phone setup for each app'


def phone_steps(ctx, result):
    """Guide the phone, one app at a time, using the QR page for every address."""
    links = result['links']
    try:
        page = phone_guide.write_page(ctx.config, links)
        page_target = [('page', str(page))]
    except (OSError, RuntimeError):
        page_target = []
    if not guided_setup.checkpoint(ctx, 'Phone: install and connect Tailscale', [
        'A page with QR codes opened on this PC. Keep it open: every app below has a code '
        'you can scan with the phone camera instead of typing the address.',
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


def run(ctx):
    gateway_tools.selected(ctx.config)
    if not ctx.apply:
        ctx.log('Private gateway preview: browser/phone instructions and prompts run only during interactive apply')
        return
    if not gateway_tools.configure(ctx, 'tailnet'):
        return {'status': 'needs_setup', 'reason': 'Selected app security checks/confirmation still pending'}
    result = None

    def configure_routes():
        nonlocal result
        result = gateway_tools.apply_tailnet(ctx.config)
        return result.get('status') == 'configured'

    if guided_setup.interactive(ctx):
        done = guided_setup.checkpoint(ctx, 'Tailscale: turn on secure addresses', [
            'A Tailscale settings page (DNS) opened in the browser.',
            'Find "MagicDNS" and make sure it is ON (click "Enable MagicDNS" if you see it).',
            'Scroll to "HTTPS Certificates" and click "Enable HTTPS". Tailscale explains '
            'that your server\'s name becomes visible in public certificate logs; your '
            'files and apps do not. Accept to continue.',
            'Nothing else to change. Your apps stay private to the devices on your '
            'Tailscale account; nothing is opened to the public internet.',
            'Answer yes. The installer then creates a secure address for each app. '
            'If that fails, check both settings above are on and answer no to try again.',
        ], [('url', 'https://console.tailscale.com/admin/dns')], verify=configure_routes)
        if not done:
            return {'status': 'needs_setup', 'reason': 'Private HTTPS configuration pending; reapply to resume'}
    else:
        configure_routes()
    ctx.log('Private application links saved in reports/gateway-tailnet.json')
    confirmed = phone_steps(ctx, result)
    result['client_acceptance'] = 'user_confirmed' if confirmed else 'needs_setup'
    result['client_acceptance_evidence'] = 'Konsole user confirmation; no independent phone probe'
    atomic_json(Path(ctx.config['data_mount']) / 'reports/gateway-tailnet.json', result)
    if not confirmed:
        return {'status': 'needs_setup', 'reason': 'Private routes configured; actual phone setup/mobile-data test remains pending'}
    return result
