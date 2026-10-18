"""Private Tailscale HTTPS with guided DNS, certificate and phone handoffs."""
import guided_setup
import gateway_tools
from neko_common import atomic_json
from pathlib import Path

DEPENDENCIES = ['storage', 'recovery', 'tailscale']
DESCRIPTION = 'Guide private HTTPS prerequisites, configure app routes and confirm phone tests'


def phone_steps(ctx, result):
    if not guided_setup.checkpoint(ctx, 'Connect your phone to Tailscale', [
        'On the PHONE install the official Tailscale app. The browser page opened on '
        'this PC explains installation; it cannot grant phone permissions for you.',
        'Open Tailscale on the phone, allow its VPN configuration, and sign into the '
        'same account/tailnet as this server. Approve the device if required.',
        'Turn Tailscale on. Keep the home server and internet running. '
        'Use the same generated app addresses at home and away.',
        'Confirm yes only once Tailscale on the phone shows connected.'
    ], [('url', 'https://tailscale.com/download')]):
        return False
    for app, url in result['links'].items():
        instructions = [
            'On the PHONE open this exact server address: ' + url,
            'Use this app\'s own login from APP-CREDENTIALS.txt; not your Tailscale password.',
        ]
        if app == 'immich':
            instructions += ['Install the Immich phone app, enter this server address, '
                             'allow the intended photos/albums, and choose Wi-Fi/mobile upload settings.',
                             'Upload a disposable photo and video, then open them from the server. '
                             'Keep the app open for this first test.']
        elif app == 'nextcloud':
            instructions += ['Install the Nextcloud phone client and approve its login flow. '
                             'Upload a disposable document, then download/open it.']
        elif app == 'navidrome':
            instructions += ['Use a Navidrome/Subsonic-compatible phone client with the separate '
                             'Navidrome listener account, then play and seek in a track you own.']
        elif app == 'jellyfin':
            instructions += ['Use the Jellyfin phone client or browser and play an owned video. '
                             'Test seeking, audio and subtitles.']
        else:
            instructions += ['Use the phone browser for Seerr. Check the linked library and, '
                             'after choosing your providers, a permitted request/import/playback.']
        instructions += ['Return to Konsole and confirm yes only after the phone test succeeds.']
        if not guided_setup.checkpoint(ctx, 'Phone: ' + gateway_tools.APPS[app][0], instructions,
                                       [('url', url)]):
            return False
    return guided_setup.checkpoint(ctx, 'Prove outside-home phone access', [
        'Turn OFF phone Wi-Fi and turn ON mobile data. Leave Tailscale connected.',
        'Fetch a new photo/file and stream a track/video through the saved private URLs. '
        'Previously cached content is not proof of connectivity.',
        'If photos are Wi-Fi-only, test viewing now; upload when back on Wi-Fi.',
        'Disconnect Tailscale and verify a fresh private fetch fails; reconnect and verify it works.',
        'Answer yes only after the selected phone services work on mobile data. '
        'This records your witnessed confirmation, not an independent phone probe.'
    ], [('url', url) for url in result['links'].values()])


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
        done = guided_setup.checkpoint(ctx, 'Enable private Tailscale HTTPS', [
            'In the opened Tailscale DNS settings, select the correct tailnet.',
            'Enable MagicDNS and HTTPS certificates. Read/acknowledge the certificate '
            'transparency notice: machine/tailnet names appear publicly, your files do not.',
            'Check that only intended users/devices have access. Do not enable public Funnel '
            'or router port forwarding for this setup.',
            'Return here and answer yes. The installer attempts the private app routes. '
            'If that check fails, confirm DNS/HTTPS, app readiness and route ownership; '
            'do not delete unrelated routes.'
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
