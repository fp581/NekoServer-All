"""Use the graphics chip for video in Jellyfin and Immich.

Finds the graphics chip, installs its video drivers on the server, asks the chip from
inside each app which formats it decodes and whether it can encode (with a short test
encode), then turns on hardware transcoding in Jellyfin and Immich with their saved
administrator logins, for what the chip can do. A setting you already chose
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

# The decoding formats Jellyfin can hand to the chip, by the VA-API profile that offers it.
VA_CODECS = (('VAProfileH264', 'h264'), ('VAProfileHEVC', 'hevc'), ('VAProfileMPEG2', 'mpeg2video'),
             ('VAProfileVC1', 'vc1'), ('VAProfileVP8', 'vp8'), ('VAProfileVP9', 'vp9'), ('VAProfileAV1', 'av1'))
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


FFMPEG = '/usr/lib/jellyfin-ffmpeg/ffmpeg'


def video_support(vainfo):
    """{'codecs': formats the chip decodes, 'encoder': whether it lists an encoder,
    'low_power_only': whether its only H.264 encoder is the low-power one} from vainfo's
    profile lines ("VAProfileH264Main : VAEntrypointVLD")."""
    codecs, entries = [], set()
    for line in vainfo.splitlines():
        profile, _, entry = (part.strip() for part in line.partition(':'))
        if entry == 'VAEntrypointVLD':
            codecs += [name for prefix, name in VA_CODECS if profile.startswith(prefix) and name not in codecs]
        elif entry.startswith('VAEntrypointEncSlice') and profile.startswith('VAProfileH264'):
            entries.add(entry)
    return {'codecs': [name for _, name in VA_CODECS if name in codecs], 'encoder': bool(entries),
            'low_power_only': entries == {'VAEntrypointEncSliceLP'}}


# Asks the encoder for its low-power mode (Intel's only H.264 encoder on some chips,
# Jasper Lake and Elkhart Lake for example): without it such a chip fails the test.
LOW_POWER = ['-low_power', '1']


def test_encode(cid, node, run, low_power=False):
    """One second of test video encoded on the chip (H.264), inside the app's container:
    a chip that lists an encoder but cannot use it does not pass."""
    result = run(['docker', 'exec', cid, FFMPEG, '-hide_banner', '-loglevel', 'error', '-init_hw_device', 'vaapi=va:' + node,
                  '-filter_hw_device', 'va', '-f', 'lavfi', '-i', 'testsrc=duration=1:size=320x240:rate=10',
                  '-vf', 'format=nv12,hwupload', '-c:v', 'h264_vaapi', *(LOW_POWER if low_power else []), '-f', 'null', '-'],
                 capture_output=True, text=True, timeout=120)
    return result.returncode == 0


def test_qsv(cid, node, run, low_power=False):
    """The same test encode through Quick Sync. Intel chips before Broadwell (2014)
    pass the VA-API test but cannot do Quick Sync on Linux; those use VA-API."""
    result = run(['docker', 'exec', cid, FFMPEG, '-hide_banner', '-loglevel', 'error', '-init_hw_device', 'vaapi=va:' + node,
                  '-init_hw_device', 'qsv=qs@va', '-filter_hw_device', 'qs', '-f', 'lavfi',
                  '-i', 'testsrc=duration=1:size=320x240:rate=10', '-vf', 'format=nv12,hwupload=extra_hw_frames=64',
                  '-c:v', 'h264_qsv', *(LOW_POWER if low_power else []), '-f', 'null', '-'],
                 capture_output=True, text=True, timeout=120)
    return result.returncode == 0


def encode_check(cid, node, run, support):
    """{'encode': bool} from the test encodes, with 'low_power': True when only the
    low-power encoder worked (tried only when it is the chip's only H.264 encoder) and
    'qsv' (the Quick Sync test) whenever an encode passed."""
    if not support['encoder']:
        return {'encode': False}
    found = {'encode': test_encode(cid, node, run)}
    if not found['encode'] and support.get('low_power_only') and test_encode(cid, node, run, low_power=True):
        found = {'encode': True, 'low_power': True}
    if found['encode']:
        found['qsv'] = test_qsv(cid, node, run, low_power=found.get('low_power', False))
    return found


def probe(app, node, run=subprocess.run):
    """What the chip does for video, asked from inside this app's container:
    {'codecs': [...], 'encode': bool}, or None when it decodes nothing there. Some chips
    decode without an encoder (AMD's RX 6400 and 6500): then only decoding is used."""
    cid = container(*CONTAINERS[app], run)
    if not cid:
        return None
    result = run(['docker', 'exec', cid, VAINFO, '--display', 'drm', '--device', node],
                 capture_output=True, text=True, timeout=60)
    support = video_support(result.stdout) if result.returncode == 0 else {'codecs': []}
    if not support['codecs']:
        return None
    return {'codecs': support['codecs'], **encode_check(cid, node, run, support)}


def login_jellyfin(client, entry):
    result = client.call('/Users/AuthenticateByName', 'POST', {'Username': entry['username'], 'Pw': entry['password']})
    token = result.get('AccessToken')
    if not token or result.get('User', {}).get('Policy', {}).get('IsAdministrator') is not True:
        raise RuntimeError('Saved Jellyfin login is not an administrator')
    client.headers['X-Emby-Token'] = token
    client.headers['Authorization'] = client.headers['Authorization'].split(', Token=')[0] + ', Token="' + token + '"'


def unset_device(current, method, device, ours, several):
    """An app already set to hardware video whose device is "choose by yourself": 'set'
    when NekoServer turned it on (an older release didn't pick the device) and it still
    uses that method; 'ask' when someone else set it and this server has several chips to
    choose from; otherwise 'keep' (your choice, and nothing to get wrong)."""
    if current != method or device not in ('', 'auto', None):
        return 'keep'
    if ours:
        return 'set'
    return 'ask' if several else 'keep'


def ask_for(app, method, node):
    return (f'set to {method} outside NekoServer and left to pick its graphics device by itself, on a server with '
            f'more than one: choose {node} in {app}\'s settings (see HARDWARE-VIDEO.md)')


# Jellyfin keeps the device for each method in its own setting.
JELLYFIN_DEVICE = {'qsv': 'QsvDevice', 'vaapi': 'VaapiDevice'}


def configure_jellyfin(entry, method, node, client, video, ours=False, several=False, was_on=False, replace=False):
    """Turn on hardware transcoding unless you already picked a setting yourself: decoding
    of the formats the chip decodes, and encoding only when the chip passed the test encode.
    `was_on`: it was on at the last run, so "off" now is your choice and stays."""
    login_jellyfin(client, entry)
    device = JELLYFIN_DEVICE[method]
    try:
        encoding = client.call('/System/Configuration/encoding')
        current = encoding.get('HardwareAccelerationType') or 'none'
        if current == 'none' and was_on:
            return SWITCHED_OFF
        if current != 'none' and not (replace and current == 'qsv'):
            action = unset_device(current, method, encoding.get(device), ours, several)
            if action == 'ask':
                return ask_for('Jellyfin', method, node)
            if action == 'keep':
                return 'kept your setting (' + current + ')'
        encoding.update({'HardwareAccelerationType': method, device: node, 'EnableHardwareEncoding': video['encode'],
                         'HardwareDecodingCodecs': video['codecs']})
        if video.get('low_power'):
            # The chip encodes H.264 only in its low-power mode, which Jellyfin uses only when told.
            encoding['EnableIntelLowPowerH264HwEncoder'] = True
        client.call('/System/Configuration/encoding', 'POST', encoding)
        return 'on (' + method + ')' + ('' if video['encode'] else ', decoding only: the chip has no working video encoder')
    finally:
        client.call('/Sessions/Logout', 'POST')


def immich_cannot(video, method):
    """Why Immich stays in software with this chip, or None: its hardware setting
    encodes too, so it needs a working encoder, and it has no low-power setting for
    VA-API (its Quick Sync picks that mode by itself)."""
    if not video['encode']:
        return NO_ENCODER
    if video.get('low_power') and method == 'vaapi':
        return LOW_POWER_ONLY
    return None


def configure_immich(entry, method, node, client, video, ours=False, several=False, was_on=False, replace=False):
    """Turn on hardware transcoding unless you already picked a setting yourself, on the
    chip that passed the check. Immich otherwise picks a device by itself, and on a
    computer with two chips that can be the other one (an AMD chip with Intel's qsv).
    Immich's hardware setting encodes too, so a chip without a working encoder stays off."""
    unusable = immich_cannot(video, method)
    if unusable:
        return unusable
    session = client.call('/api/auth/login', 'POST', {'email': entry['username'], 'password': entry['password']})
    if session.get('isAdmin') is not True or not session.get('accessToken'):
        raise RuntimeError('Saved Immich login is not an administrator')
    client.headers['Authorization'] = 'Bearer ' + session['accessToken']
    try:
        settings = client.call('/api/system-config')
        current = settings.get('ffmpeg', {}).get('accel') or 'disabled'
        if current == 'disabled' and was_on:
            return SWITCHED_OFF
        if current != 'disabled' and not (replace and current == 'qsv'):
            action = unset_device(current, method, settings['ffmpeg'].get('preferredHwDevice'), ours, several)
            if action == 'ask':
                return ask_for('Immich', method, node)
            if action == 'keep':
                return 'kept your setting (' + current + ')'
        settings['ffmpeg'].update(accel=method, accelDecode=True)
        if node.startswith('/dev/dri/'):
            settings['ffmpeg']['preferredHwDevice'] = node
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


GPU_DEVICE = Path('/dev/dri')


def drop_immich_chip(config, log):
    """The graphics device is gone from this PC (the chip removed or switched off, its
    driver not loading, the drives moved to another PC): Docker refuses to start a
    container whose device is missing, so Immich's addition is taken out, also for the
    monthly update. Immich itself was already started without it by the photos step;
    the next run with the chip back adds it again."""
    target = Path(config['data_mount']) / 'compose/immich' / HARDWARE
    if target.is_file() and not target.is_symlink():
        target.unlink()
        log('Immich: the graphics device is not on this PC any more, so it was taken out of Immich '
            '(it converts video in software until the chip is back)')
        return True
    return False


def configure_app(app, chip, entry, client, video, before=None, several=False):
    """Turn hardware transcoding on in one app, with this chip's method, for what the
    chip does (`video`, from probe). `before`: what the last run recorded for this app
    ("on (qsv)" when NekoServer turned it on)."""
    _, node, jellyfin_method, immich_method = chip
    method = jellyfin_method if app == 'jellyfin' else immich_method
    # Quick Sync that NekoServer set on a chip that can't do it (releases before 0.39.5
    # did not test it): replaced by VA-API. Quick Sync someone else set stays theirs.
    replace = method == 'qsv' and video.get('qsv') is False and str(before or '').startswith('on (qsv')
    if method == 'qsv' and video.get('qsv') is False:
        method = 'vaapi'
    ours = str(before or '').startswith('on (' + method)
    # On at the last run (NekoServer's setting or yours), or already left off by you.
    was_on = str(before or '').startswith(('on', 'kept')) or before == SWITCHED_OFF
    configure = configure_jellyfin if app == 'jellyfin' else configure_immich
    return configure(entry, method, node, client, video, ours, several, was_on, replace)


SOFTWARE = 'converts video in software: the chip did not pass the video check inside it'
SWITCHED_OFF = 'off: you switched hardware video off in the app, so it is left off'
NO_ENCODER = 'converts video in software: the chip decodes video but has no working encoder, which Immich needs'
LOW_POWER_ONLY = ('converts video in software: the chip encodes only in its low-power mode, which Immich '
                  'does not use with VA-API')
# Results that are an answer (nothing left for you to do), not a step still to finish.
ANSWERS = (SOFTWARE, SWITCHED_OFF, LOW_POWER_ONLY)


def decisive(state):
    """What NekoServer last knew for sure about an app's hardware video: turned on (by it
    or by you), or switched off by you. A run that couldn't tell (the app wasn't running,
    didn't answer, or the chip check failed this time) is not."""
    return isinstance(state, str) and (state.startswith(('on', 'kept')) or state == SWITCHED_OFF)


def apps_on(config, choices, log, client_factory=None, apps=None, check=probe, before=None):
    """Switch the selected apps over, each to the first chip (preferred first) that passes
    the video check inside that app's own container; returns {app: what happened}."""
    import account_setup
    import account_store
    client_factory = client_factory or account_setup.Client
    done = {}
    for app in selected_apps(config) if apps is None else apps:
        found = next(((choice, video) for choice in choices if (video := check(app, choice[1]))), None)
        if found is None:
            done[app] = SOFTWARE
            log(app.capitalize() + ' hardware video: ' + done[app])
            continue
        entry = account_store.get(config, app) if account_store.enabled(config) else None
        if not entry:
            done[app] = 'needs your login: turn it on in the app (see HARDWARE-VIDEO.md)'
            continue
        client = client_factory(app)
        try:
            account_setup.wait(app, client, 180)
            done[app] = configure_app(app, found[0], entry, client, found[1], (before or {}).get(app), len(choices) > 1)
        except (RuntimeError, OSError, KeyError, ValueError) as exc:
            done[app] = 'not changed (' + str(exc) + ')'
        finally:
            client.close()
        log(app.capitalize() + ' hardware video: ' + done[app])
    return done


def switch_apps(ctx, choices, hold=None, check_storage=None, apps=None, before=None):
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
        return apps_on(ctx.config, choices, ctx.log, apps=apps, before=before)


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
    if not GPU_DEVICE.is_dir():
        drop_immich_chip(ctx.config, ctx.log)
    if not choices:
        why = ('NVIDIA graphics need their own driver and container toolkit, which NekoServer does not install'
               if 'nvidia' in vendors else 'no Intel or AMD graphics chip with a video device was found')
        ctx.log('Hardware video: ' + why + '; Jellyfin and Immich convert video in software (works, just slower)')
        # What you chose in the apps is remembered for when the chip is back.
        memory = {k: v for k, v in previous(report).items() if decisive(v)}
        atomic_json(report, {'status': 'software', 'reason': why, 'vendors': vendors, 'memory': memory})
        return
    for vendor in dict.fromkeys(c[0] for c in choices):
        install_drivers(vendor, ctx.log)
    before = previous(report)
    return outcome(ctx, report, choices, switch_apps(ctx, choices, apps=apps, before=before), before)


def previous(report):
    """What the last run did in each app ({app: "on (qsv)"}), or {}. Where that run
    couldn't tell (the app wasn't running, say), the last state it knew for sure is used
    (`memory`): one such run must not forget that you switched hardware video off."""
    try:
        value = json.loads(Path(report).read_text())
    except (OSError, ValueError):
        return {}
    value = value if isinstance(value, dict) else {}
    found = {}
    for key in ('apps', 'memory'):
        part = value.get(key)
        if isinstance(part, dict):
            found.update({k: v for k, v in part.items() if isinstance(v, str) and (key == 'apps' or decisive(v))})
    return found


def remembered(done, before):
    """{app: the last decisive state}: this run's where it is one, else the earlier one."""
    memory = {}
    for app, state in done.items():
        known = state if decisive(state) else (before or {}).get(app)
        if decisive(known):
            memory[app] = known
    return memory


def outcome(ctx, report, choices, done, before=None):
    """Record what each app now uses (and the last state known for sure, see previous());
    the step waits only for an app the chip works with but that couldn't be switched over."""
    using = [a for a, s in done.items() if s.startswith(('on', 'kept'))]
    atomic_json(report, {'status': 'hardware' if using else 'software', 'chips': [c[0] for c in choices],
                         'devices': [c[1] for c in choices], 'apps': done, 'memory': remembered(done, before)})
    # Waiting for you: an app the chip works with but that couldn't be switched over.
    # A chip that fails an app's own check is an answer, not a step left to do.
    waiting = [a for a, s in done.items() if not s.startswith(('on', 'kept')) and s not in ANSWERS]
    if waiting:
        return {'status': 'needs_setup', 'reason': 'Graphics chip works; turn on hardware transcoding in: ' +
                ', '.join(waiting) + ' (see HARDWARE-VIDEO.md)'}
    if using:
        ctx.log('Hardware video: ' + ' and '.join(a.capitalize() for a in using) + ' now use the graphics chip')
