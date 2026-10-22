"""Use the graphics chip for video in Jellyfin and Immich.

Finds the graphics chip, installs its video drivers on the server, checks from inside
Jellyfin that the chip really decodes video, then turns on hardware transcoding in
Jellyfin and Immich with their saved administrator logins. A setting you already chose
in either app is left alone. Without a usable chip both apps keep converting video in
software, which works, just more slowly.
"""
import json
from pathlib import Path
import subprocess

import gpu
import host
from neko_common import atomic_json, atomic_write, compose_image_override

DEPENDENCIES = ['media', 'photos', 'accounts']
DESCRIPTION = 'Use the graphics chip for video in Jellyfin and Immich'

# Codecs practically every Intel (2015+) and AMD chip decodes; others fall back to software.
JELLYFIN_CODECS = ['h264', 'hevc', 'mpeg2video', 'vc1']
HARDWARE = 'hardware.yaml'


def install_drivers(vendor, log, run=subprocess.run):
    """The chip's video drivers and vainfo, where this system's repositories have them."""
    family = host.current(run)
    wanted = [p for p in gpu.VIDEO.get(vendor, []) if family.available(p)]
    family.install(wanted)
    log('Video drivers for the ' + vendor.upper() + ' graphics chip: ' + (', '.join(wanted) or 'built into the system'))


def container(project, service, run=subprocess.run):
    out = run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + project,
               '--filter', 'label=com.docker.compose.service=' + service],
              capture_output=True, text=True, timeout=30).stdout.split()
    return out[0] if len(out) == 1 else None


# Each app's container: (Compose project, service). Both images ship Jellyfin's ffmpeg build.
CONTAINERS = {'jellyfin': ('neko-jellyfin', 'jellyfin'), 'immich': ('neko-immich', 'immich-server')}
VAINFO = '/usr/lib/jellyfin-ffmpeg/vainfo'


def selected_apps(config):
    """The apps this server runs that can use the graphics chip, in a fixed order."""
    apps = []
    if 'jellyfin' in config.get('media', {}).get('services', []):
        apps.append('jellyfin')
    if config.get('photos', {}).get('deploy_immich', False):
        apps.append('immich')
    return apps


def decodes(app, node, run=subprocess.run):
    """Ask the chip, from inside this app's container, whether it decodes video."""
    cid = container(*CONTAINERS[app], run)
    if not cid:
        return False
    result = run(['docker', 'exec', cid, VAINFO, '--display', 'drm', '--device', node],
                 capture_output=True, text=True, timeout=60)
    return result.returncode == 0 and 'VAEntrypointVLD' in result.stdout


def jellyfin_decodes(node, run=subprocess.run):
    return decodes('jellyfin', node, run)


def login_jellyfin(client, entry):
    result = client.call('/Users/AuthenticateByName', 'POST', {'Username': entry['username'], 'Pw': entry['password']})
    token = result.get('AccessToken')
    if not token or result.get('User', {}).get('Policy', {}).get('IsAdministrator') is not True:
        raise RuntimeError('Saved Jellyfin login is not an administrator')
    client.headers['X-Emby-Token'] = token
    client.headers['Authorization'] = client.headers['Authorization'].split(', Token=')[0] + ', Token="' + token + '"'


def configure_jellyfin(entry, method, node, client):
    """Turn on hardware transcoding unless you already picked a setting yourself."""
    login_jellyfin(client, entry)
    try:
        encoding = client.call('/System/Configuration/encoding')
        current = encoding.get('HardwareAccelerationType') or 'none'
        if current != 'none':
            return 'kept your setting (' + current + ')'
        encoding.update(HardwareAccelerationType=method, VaapiDevice=node, EnableHardwareEncoding=True,
                        HardwareDecodingCodecs=JELLYFIN_CODECS)
        client.call('/System/Configuration/encoding', 'POST', encoding)
        return 'on (' + method + ')'
    finally:
        client.call('/Sessions/Logout', 'POST')


def configure_immich(entry, method, client):
    """Turn on hardware transcoding unless you already picked a setting yourself."""
    session = client.call('/api/auth/login', 'POST', {'email': entry['username'], 'password': entry['password']})
    if session.get('isAdmin') is not True or not session.get('accessToken'):
        raise RuntimeError('Saved Immich login is not an administrator')
    client.headers['Authorization'] = 'Bearer ' + session['accessToken']
    try:
        settings = client.call('/api/system-config')
        current = settings.get('ffmpeg', {}).get('accel') or 'disabled'
        if current != 'disabled':
            return 'kept your setting (' + current + ')'
        settings['ffmpeg'].update(accel=method, accelDecode=True)
        client.call('/api/system-config', 'PUT', settings)
        return 'on (' + method + ')'
    finally:
        client.call('/api/auth/logout', 'POST')


def give_immich_the_chip(config, log, run=subprocess.run):
    """Add the graphics device to Immich's server (kept across monthly updates)."""
    folder = Path(config['data_mount']) / 'compose/immich'
    compose, env = folder / 'compose.yaml', folder / '.env'
    if not compose.is_file():
        return False
    addition = json.dumps({'services': {'immich-server': {'devices': ['/dev/dri:/dev/dri']}}}, indent=2) + '\n'
    target = folder / HARDWARE
    if not target.exists() or target.read_text() != addition:
        atomic_write(target, addition, 0o600)
    base = ['docker', 'compose', '-p', 'neko-immich', '-f', str(compose), '--env-file', str(env),
            *compose_image_override(compose)]
    run([*base, 'config', '-q'], check=True, capture_output=True, timeout=60)
    run([*base, 'up', '-d'], check=True, capture_output=True, timeout=1200)
    log('Immich: graphics device added')
    return True


SOFTWARE = 'converts video in software: the chip did not pass the video check inside it'


def apps_on(config, choices, log, client_factory=None, apps=None, check=decodes):
    """Switch the selected apps over, each to the first chip (preferred first) that passes
    the video check inside that app's own container; returns {app: what happened}."""
    import account_setup
    import account_store
    client_factory = client_factory or account_setup.Client
    done = {}
    for app in selected_apps(config) if apps is None else apps:
        chip = next((choice for choice in choices if check(app, choice[1])), None)
        if chip is None:
            done[app] = SOFTWARE
            log(app.capitalize() + ' hardware video: ' + done[app])
            continue
        _, node, jellyfin_method, immich_method = chip
        configure = {'jellyfin': lambda e, c: configure_jellyfin(e, jellyfin_method, node, c),
                     'immich': lambda e, c: configure_immich(e, immich_method, c)}[app]
        entry = account_store.get(config, app) if account_store.enabled(config) else None
        if not entry:
            done[app] = 'needs your login: turn it on in the app (see HARDWARE-VIDEO.md)'
            continue
        client = client_factory(app)
        try:
            account_setup.wait(app, client, 180)
            done[app] = configure(entry, client)
        except (RuntimeError, OSError, KeyError, ValueError) as exc:
            done[app] = 'not changed (' + str(exc) + ')'
        finally:
            client.close()
        log(app.capitalize() + ' hardware video: ' + done[app])
    return done


def switch_apps(ctx, choices, hold=None, check_storage=None, apps=None):
    """Recreate Immich with the chip (when it runs here) and switch the selected apps
    over while holding the lock that backups and monthly maintenance hold, so Immich is
    never restarted in the middle of their consistent snapshot or a rollback."""
    from recovery_tools import lock, mounts
    from neko_common import verify_storage
    apps = selected_apps(ctx.config) if apps is None else apps
    if hold is None:
        mounts(ctx.config)
    with (hold or lock)(ctx.config):
        (check_storage or verify_storage)(ctx.config)
        if 'immich' in apps:
            give_immich_the_chip(ctx.config, ctx.log)
        return apps_on(ctx.config, choices, ctx.log, apps=apps)


def run(ctx):
    if not ctx.apply:
        ctx.log('Hardware video: finds the graphics chip and turns on hardware transcoding in Jellyfin and Immich')
        return
    apps = selected_apps(ctx.config)
    if not apps:
        ctx.log('Hardware video: neither Jellyfin nor Immich runs on this server; nothing to switch over')
        return
    vendors = gpu.graphics()
    choices = gpu.video_choices(gpu.render_nodes())
    report = Path(ctx.config['data_mount']) / 'reports/hardware-video.json'
    report.parent.mkdir(parents=True, exist_ok=True)
    if not choices:
        why = ('NVIDIA graphics need their own driver and container toolkit, which NekoServer does not install'
               if 'nvidia' in vendors else 'no Intel or AMD graphics chip with a video device was found')
        ctx.log('Hardware video: ' + why + '; Jellyfin and Immich convert video in software (works, just slower)')
        atomic_json(report, {'status': 'software', 'reason': why, 'vendors': vendors})
        return
    for vendor, _, _, _ in choices:
        install_drivers(vendor, ctx.log)
    return outcome(ctx, report, choices, switch_apps(ctx, choices, apps=apps))


def outcome(ctx, report, choices, done):
    """Record what each app now uses; the step waits only for an app the chip works with
    but that couldn't be switched over."""
    using = [a for a, s in done.items() if s.startswith(('on', 'kept'))]
    atomic_json(report, {'status': 'hardware' if using else 'software', 'chips': [c[0] for c in choices],
                         'devices': [c[1] for c in choices], 'apps': done})
    # Waiting for you: an app the chip works with but that couldn't be switched over.
    # A chip that fails an app's own check is an answer, not a step left to do.
    waiting = [a for a, s in done.items() if not s.startswith(('on', 'kept')) and s != SOFTWARE]
    if waiting:
        return {'status': 'needs_setup', 'reason': 'Graphics chip works; turn on hardware transcoding in: ' +
                ', '.join(waiting) + ' (see HARDWARE-VIDEO.md)'}
    if using:
        ctx.log('Hardware video: ' + ' and '.join(a.capitalize() for a in using) + ' now use the graphics chip')
