#!/usr/bin/env python3
"""Read-only deployment inspection. --save writes a report; --manual records witnessed tests."""

import argparse, hashlib, json, os, platform, re, shlex, subprocess, sys, time, uuid
from pathlib import Path
import status_checks as health
import installer, recovery_tools
import gateway_checks, native_runtime
import guided_setup
import host
import permission_checks
import os_checks
import neko_common
from modules import docker as docker_module, tv
from neko_common import (
    files_root,
    music_backed_up,
    music_downloads,
    photo_library,
    VERSION,
    mount_path,
    safe_path,
    atomic_json,
    reading_max_age,
    local_opener,
    app_port,
    DOCKER_STORAGE_DROPIN,
    dashboard_port,
    checked_settings,
    default_settings,
    immich_wanted,
    media_steps_left_out,
    media_services,
    verify_storage,
)
from acceptance_summary import scope_digest, with_data_scope

STATES = {'pass', 'fail', 'needs_setup', 'warning', 'blocked'}
OPENER = local_opener()
ROOT = Path(__file__).resolve().parent


def peer_port(inspect):
    """qBittorrent's TORRENTING_PORT from a container's environment, if set."""
    for item in inspect.get('Config', {}).get('Env') or []:
        if isinstance(item, str) and item.startswith('TORRENTING_PORT='):
            value = item.split('=', 1)[1]
            return int(value) if value.isdecimal() else None
    return None


def peer_bindings(peer, config):
    """The only non-loopback publications allowed: the configured qBittorrent peer port."""
    if peer is None or peer != config.get('media', {}).get('torrent_port'):
        return set()
    return {f'{peer}/tcp', f'{peer}/udp'}


# A container whose health check has not finished yet (Docker started it a moment ago).
STARTING = 'Still starting: its health check has not finished yet.'
# Where Immich's containers see the photo library and the database (shared.json, the list
# the snapshot's Compose rules use).
IMMICH_TARGETS = {
    service: tuple(targets) for service, targets in neko_common.SHARED['immich_targets'].items() if service != '_about'
}


def item(key, name, status, detail):
    if status not in STATES:
        raise ValueError('Invalid acceptance state')
    return {'id': key, 'name': name, 'status': status, 'detail': detail}


def boot_id():
    try:
        value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return value if re.fullmatch(r'[a-f0-9-]{36}', value) else None
    except OSError:
        return None


def fingerprint(config, profile):
    # Retain a digest, never arbitrary configuration values/credentials in the report.
    clean = {k: config.get(k) for k in ('data_mount', 'media_mount', 'external_backup_mount', 'storage')}
    for group, keys in {
        'media': ('services', 'uid', 'gid', 'bind_ip'),
        'photos': ('deploy_immich', 'immich_version', 'external_backup_uuid', 'external_backup_fstype', 'archive_dir'),
        'dashboard': ('port',),
        'runtime': ('backend',),
        'networking': ('share_mode', 'tailnet_only', 'samba_media_write', 'shares'),
        'gateway': ('services', 'lan_ip', 'lan_cidr', 'confirmed_services'),
        'tv': ('user', 'keep_awake', 'bluetooth'),
        'heartbeat': ('interval_seconds',),
        'host_backup': ('user', 'steam', 'samba', 'extra_paths', 'minimum_free_gb', 'time'),
        'server_os': ('hdd_idle_minutes', 'hdd_policy', 'journal_disk_mb', 'journal_ram_mb', 'journal_retention_days'),
    }.items():
        clean[group] = {k: config.get(group, {}).get(k) for k in keys}
    clean['profile'] = profile
    clean['modules'] = config['profiles'].get(profile)
    clean = with_data_scope(clean, config)
    return hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def properties(out):
    """`systemctl show`'s answer (one name=value per line) as a dict."""
    pairs = (line.split('=', 1) for line in out.splitlines() if '=' in line)
    return dict(pairs)


def retired_timer(unit):
    rc, out = health.run(['systemctl', 'show', unit, '--property=LoadState,ActiveState,UnitFileState'])
    fields = properties(out)
    return fields.get('LoadState') == 'not-found' or (
        rc == 0
        and fields.get('ActiveState') == 'inactive'
        and fields.get('UnitFileState') in ('disabled', 'masked', 'static')
    )


def service(unit):
    rc, out = health.run(['systemctl', 'show', unit, '--property=LoadState,ActiveState,UnitFileState'])
    fields = properties(out)
    return (
        rc == 0
        and fields.get('LoadState') == 'loaded'
        and fields.get('ActiveState') == 'active'
        and fields.get('UnitFileState') in ('enabled', 'enabled-runtime')
    )


# The name of the first automatic check, on the report and the dashboard.
CONFIGURATION_CHECK = 'Configuration and profile'


def load_json(path):
    return health.read_json(path)


def docker_info(config):
    rc, out = health.run(recovery_tools.DOCKER + ['info', '--format', '{{json .}}'], 15)
    try:
        value = json.loads(out)
    except ValueError:
        return False, None
    if rc != 0 or not isinstance(value, dict):
        return False, None
    valid = (
        value.get('DockerRootDir') == str(Path(config['data_mount']) / 'docker-engine')
        and value.get('Driver') == 'overlay2'
        and 'io.containerd.snapshotter' not in json.dumps(value.get('DriverStatus'))
    )
    return valid, value.get('ServerVersion') if isinstance(value.get('ServerVersion'), str) else None


def shown_units(value):
    """The unit names in a list `systemctl show` printed. A name with an escaped character
    (`mnt-nekoserver\\x2dmedia.mount`, the usual media drive's) is printed as it is by an
    older systemd and quoted, with the backslash doubled, by a newer one (seen on the rig,
    systemd 262: the Docker guard then failed this check on every such server)."""
    try:
        return set(value.split()) | set(shlex.split(value))
    except ValueError:
        return set(value.split())


def guard_check(config):
    try:
        text = safe_path(DOCKER_STORAGE_DROPIN).read_text()
        command = next(line.split('=', 1)[1] for line in text.splitlines() if line.startswith('ExecStartPre='))
        args = shlex.split(command)
        if len(args) != 4 or args[2] != '--config':
            return False
        if args[1] != 'storage-guard':
            return False
        script = safe_path(args[0])
        runtime = script.parent
        if script.name != native_runtime.NAME or script.read_bytes() != native_runtime.payload(ROOT, config):
            return False
        root = Path(config['data_mount']) / 'installer/guards'
        if (
            runtime.parent != root
            or not re.fullmatch(r'v\d+\.\d+\.\d+', runtime.name)
            or args[3] != str(runtime / 'storage.json')
        ):
            return False
        if load_json(runtime / 'storage.json') != {k: config[k] for k in ('data_mount', 'media_mount', 'storage')}:
            return False
        if text != docker_module.guard_contents(config, runtime):
            return False
        rc, out = health.run(
            ['systemctl', 'show', 'docker.service', '--property=ExecStartPre,RequiresMountsFor,BindsTo']
        )
        props = properties(out)
        units = [line.split('=', 1)[1] for line in text.splitlines() if line.startswith('BindsTo=')][0].split()
        return (
            rc == 0
            and command in props.get('ExecStartPre', '')
            and 'ignore_errors=no' in props.get('ExecStartPre', '')
            and all(config[k] in props.get('RequiresMountsFor', '').split() for k in ('data_mount', 'media_mount'))
            and all(u in shown_units(props.get('BindsTo', '')) for u in units)
        )
    except (OSError, RuntimeError, ValueError, KeyError, IndexError, StopIteration, subprocess.SubprocessError):
        return False


# Where an app sees its library or store inside its container.
DATA = '/data'


def drive_problem(config, keys):
    """Why the drives under `keys` are not the ones setup chose, or None: the runtime's own
    check (check-drives: UUID, filesystem, Btrfs subvolume, the NAS disk's WWN, no copy
    connected, two filesystems), the one every job makes. This compared the UUID and
    filesystem only, so it passed a drive every backup refused (the review of 0.47.1).
    The SSD and the HDD must also take new files: the right drive that Linux turned
    read-only after write errors passed as mounted (deep check 431). The USB drive is
    judged by its identity alone (the recovery menu leaves it mounted read-only)."""
    try:
        verify_storage(config, keys, writable='usb' not in keys)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return str(exc) or 'the drive check could not run'
    return None


def job_states(config, ask=None):
    """(states, None) or (None, why): the backups', the outage monitor's and the phone
    alerts' states as the dashboard shows them, worked out by the runtime
    (`nekoserver-runtime job-states`, collect.rs). This kept a copy of those rules
    (status_checks), which already disagreed: a worker systemd could not start, or a run
    cut off part-way, passed here (the review of 0.47.1)."""
    import tempfile

    try:
        with tempfile.TemporaryDirectory(prefix='neko-jobs-') as work:
            path = Path(work) / 'settings.json'
            path.write_text(json.dumps(config))
            code, said = (ask or native_runtime.ask_runtime)(ROOT, config, ['job-states', '--config', str(path)])
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
        return None, str(exc) or 'the runtime could not run'
    if code != 0:
        return None, native_runtime.reason_of(said, f'the runtime stopped (exit {code})')
    for line in reversed((said or '').splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value, None
    return None, 'the runtime said no states'


def verdict(good, bad):
    """A check's status: pass when good, fail when bad, else waiting for setup."""
    if good:
        return 'pass'
    if bad:
        return 'fail'
    return 'needs_setup'


def containers(config, photos=False):
    specs = []
    if photos:
        specs = [('immich', s) for s in ('immich-server', 'database', 'redis', 'immich-machine-learning')]
    else:
        specs = [(a, a) for a in media_services(config) if a in health.APPS and a != 'immich']
    results = []
    for app, svc in specs:
        state, starting = container_state(config, app, svc, photos)
        if state == 'pass':
            detail = 'Running state, persistent source mounts, network and localhost published ports verified.'
        elif starting:
            detail = STARTING
        else:
            detail = 'Inspect container health, bind mounts (its own data folder must be writable), network and published ports locally.'
        results.append(item('container:' + svc, health.APPS[app][0] + ' / ' + svc, state, detail))
    return results


def container_state(config, app, svc, photos):
    """(pass or fail, whether it is starting) for the one container of `app`'s service
    `svc`: found by setup's labels, then inspected."""
    rc, out = health.run(
        recovery_tools.DOCKER
        + [
            'ps',
            '-aq',
            '--filter',
            'label=com.docker.compose.project=neko-' + app,
            '--filter',
            'label=com.docker.compose.service=' + svc,
        ],
        15,
    )
    ids = out.splitlines()
    if not (rc == 0 and len(ids) == 1 and re.fullmatch(r'[a-f0-9]{12,64}', ids[0])):
        return 'fail', False
    rc, out = health.run(recovery_tools.DOCKER + ['inspect', ids[0]], 15)
    try:
        values = json.loads(out)
        v = values[0] if rc == 0 and isinstance(values, list) and len(values) == 1 else {}
        return inspected_state(config, app, svc, photos, v)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return 'fail', False


def inspected_state(config, app, svc, photos, v):
    """container_state from what `docker inspect` said of it (`v`)."""
    labels = v.get('Config', {}).get('Labels', {})
    running = v.get('State', {}).get('Status') == 'running'
    health_state = v.get('State', {}).get('Health', {}).get('Status', 'not_defined')
    healthy = health_state not in ('unhealthy', 'starting')
    exact = (
        labels.get('com.docker.compose.project') == 'neko-' + app and labels.get('com.docker.compose.service') == svc
    )
    binds = [m for m in v.get('Mounts', []) if isinstance(m, dict) and m.get('Type') == 'bind']
    mounts = mounts_ok(config, app, svc, binds)
    network = v.get('NetworkSettings', {})
    ports = ports_ok(config, app, svc, photos, v, network.get('Ports', {}))
    if not photos:
        mounts = mounts and 'nekoserver-media' in network.get('Networks', {})
    state = 'pass' if exact and running and healthy and mounts and ports else 'fail'
    # Right after Docker (re)started it: everything in place, the health check
    # not through yet. The gateway step waits for such a container.
    starting = exact and running and mounts and ports and health_state == 'starting'
    return state, starting


def bound(binds, source, targets, readonly=False, writable=False):
    """One of the container's bind mounts is `source` at one of `targets`. `writable`: the
    app must be able to write there (Docker's "RW")."""
    return any(
        m.get('Source') == source
        and m.get('Destination') in targets
        and (not readonly or m.get('RW') is False)
        and (not writable or m.get('RW') is True)
        for m in binds
    )


def mounts_ok(config, app, svc, binds):
    """The container has its own data folder, and the libraries it uses, bound as needed."""
    if app == 'immich':
        # The same bindings the snapshot checks (shared.json "immich_targets").
        targets = IMMICH_TARGETS.get(svc, ())
        if svc == 'immich-server':
            expected = str(photo_library(config))
        elif svc == 'database':
            expected = str(Path(config['data_mount']) / 'appdata/immich/postgres')
        else:
            expected = None
        # Photos are uploaded and the database written there: read-only, the
        # folder would be the right one and Immich still unusable (review of
        # 0.51.2: only the paths were checked). The same for an app's own data.
        return expected is None or bound(binds, expected, targets, writable=True)
    destination = {'seerr': '/app/config', 'navidrome': DATA}.get(app, '/config')
    mounts = bound(binds, str(Path(config['data_mount']) / 'appdata' / app), (destination,), writable=True)
    if app not in ('jellyfin', 'navidrome', 'sonarr', 'radarr', 'lidarr', 'qbittorrent'):
        return mounts
    library = files_root(config, 'music' if app in ('navidrome', 'lidarr') else 'movies') / 'library'
    # (folder, where the app sees it, whether the app only reads it)
    if app == 'jellyfin':
        needed = [('movies', '/data/movies', True), ('tv', '/data/tv', True)]
    elif app == 'navidrome':
        needed = [('music', '/music', True)]
    else:
        needed = [('', DATA, False)]
    # The others import, rename and download there: read-only (as a
    # monthly update checks them, and leaves them when it cannot open
    # them up again) they cannot work, though the folder is right.
    mounts = mounts and all(
        bound(binds, str(library / rel), (dest,), readonly=ro, writable=not ro) for rel, dest, ro in needed
    )
    if app == 'qbittorrent':
        mounts = mounts and bound(binds, str(music_downloads(config)), ('/data/downloads/music',), writable=True)
    return mounts


def ports_ok(config, app, svc, photos, v, bindings):
    """Every port the container publishes is on 127.0.0.1 (qBittorrent's peer port aside),
    and the app's own port is among them."""
    peer = peer_port(v) if app == 'qbittorrent' else None
    published = [p for key, rows in bindings.items() if rows and key not in peer_bindings(peer, config) for p in rows]
    ports = all(p.get('HostIp') == '127.0.0.1' for p in published)
    if not photos or svc == 'immich-server':
        ports = ports and any(
            p.get('HostPort') == str(health.APPS[app][2])
            for p in (bindings.get(str(health.APPS[app][2]) + '/tcp') or [])
        )
    return ports


MEDIA_CHECK = 'nekoserver-media-health.service'  # modules/media_monitor.py
MEDIA_REPORT = 'reports/media/latest.json'


# Checks of the end-to-end transfer and playback, done by the witnessed manual tests.
WITNESSED = (
    'end_to_end_media',
    'qbittorrent:authenticated_playback_or_transfer',
    'navidrome:authenticated_playback_or_transfer',
)


def media_audit(state, detail):
    """The one row that stands for the whole media check when it gives no rows of its own."""
    return [item('media:audit', 'Current media API checks', state, detail)]


def media_checks(config, run=None):
    """The media apps' configuration as the runtime's own check finds it (media.rs, the one
    the dashboard shows): it is run once now (it only reads), and its report is read. This
    kept a check of its own before (media-health.py), which already disagreed with the
    runtime's: a Prowlarr link or a root folder passed here and not there (the review of
    0.47.0)."""
    run = run or subprocess.run  # looked up now, so a test's stand-in is the one used
    quiet = {'capture_output': True, 'text': True, 'check': False}
    if run(['systemctl', 'cat', MEDIA_CHECK], timeout=30, **quiet).returncode != 0:
        return media_audit('needs_setup', 'The media check is not set up yet.')
    started = time.time()
    ran = run(['systemctl', 'start', MEDIA_CHECK], timeout=360, **quiet)
    try:
        report = json.loads(safe_path(mount_path(config['data_mount']) / MEDIA_REPORT).read_text())
        if not isinstance(report, dict):
            raise ValueError('not a report')
    except (OSError, ValueError, RuntimeError):
        return media_audit('fail', 'Current read-only media checks could not complete.')
    if not written_since(report, started):
        # The check stopped before it wrote its report (a drive its guard refused, its
        # lock, broken settings): the report there is an older run's, and its PASS rows
        # are not this check's (the review of 0.47.10). A check that found failing rows
        # also ends with an error, but after its report: that one is read.
        why = (getattr(ran, 'stderr', '') or getattr(ran, 'stdout', '') or '').strip()[:300]
        detail = 'The media check did not write a new report, so its last results are not shown as current'
        if getattr(ran, 'returncode', 0) != 0 and why:
            detail += ': ' + why
        return media_audit('fail', detail + '. Look at  sudo journalctl -u ' + MEDIA_CHECK + '  for why.')
    out = [row for group in ('services', 'integrations', 'readiness') for row in media_rows(group, report.get(group))]
    return out or media_audit('needs_setup', 'No current media configuration checks are available.')


def written_since(report, started):
    """The report's own time (generated_at, written by the check) is not before `started`."""
    import datetime

    # The runtime writes nanoseconds ("...:00.123456789+00:00"); Python reads six digits.
    stamp = re.sub(r'(\.\d{6})\d+', r'\1', str(report.get('generated_at')), flags=re.ASCII)
    try:
        written = datetime.datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return False
    return written >= started - 1  # the check writes whole seconds on some systems


def media_rows(group, values):
    """The report's results in `group` as rows (at most 50, plain names only)."""
    if not isinstance(values, dict):
        return []
    rows = []
    for key, value in list(values.items())[:50]:
        if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9_:]+', key) or not isinstance(value, dict):
            continue
        if key in WITNESSED or value.get('result') not in STATES:
            continue
        rows.append(
            item(
                'media:' + group + ':' + key,
                key.replace('_', ' ').replace(':', ' / '),
                value['result'],
                'Current authenticated configuration inspection; complete setup or inspect locally when pending.',
            )
        )
    return rows


def dashboard_check(port, now):
    try:
        with OPENER.open('http://127.0.0.1:' + str(port) + '/api/status', timeout=5) as response:
            if response.status != 200:
                return False
            payload = response.read(2000001)
        if len(payload) > 2000000:
            return False
        value = json.loads(payload)
        stamp = health.number(value.get('generated_at'), 0, now + 60)
        return (
            value.get('format') == 'neko-dashboard-v1' and stamp is not None and now - stamp <= reading_max_age(value)
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return False


CONTROL_UNIT = 'nekoserver-control.service'
OPENRGB_UNIT = 'nekoserver-openrgb.service'


def network_of(unit):
    """The network namespace a running service's main process is in (None when it does
    not run or that can't be read)."""
    rc, out = health.run(['systemctl', 'show', unit, '--property=MainPID', '--value'])
    pid = out.strip()
    if rc != 0 or not pid.isdecimal() or pid == '0':
        return None
    try:
        return os.readlink(f'/proc/{pid}/ns/net')
    except OSError:
        return None


def host_network():
    try:
        return os.readlink('/proc/1/ns/net')
    except OSError:
        return None


def control_check():
    """The control service (fans, lights, app controls) runs, on the computer's own
    network: after restarting an app it checks that the app answers on 127.0.0.1, which
    only works there (in a private network of its own, as 0.39.6 to 0.40.0 set it up,
    every restart from the dashboard ended as a failure)."""
    if not service(CONTROL_UNIT):
        return 'fail', 'The fan, light and app control service must be active and enabled.'
    own, host_net = network_of(CONTROL_UNIT), host_network()
    if own is None or host_net is None:
        return (
            'needs_setup',
            'Run the acceptance check with sudo, so it can see which network the control service uses.',
        )
    if own != host_net:
        return (
            'fail',
            'The control service runs in a private network, where restarted apps never answer: run setup again (sudo bash install.sh).',
        )
    return 'pass', 'Active and enabled, on the computer\'s own network, where it checks that a restarted app answers.'


def lighting_check():
    """With lighting, OpenRGB (root, drives hardware) must be in a private network of its
    own, never the computer's, where any program could reach it."""
    if not service(OPENRGB_UNIT):
        return (
            'fail',
            'OpenRGB (lighting) must be active and enabled; or set "rgb": false under "dashboard" and run setup again.',
        )
    own, host_net = network_of(OPENRGB_UNIT), host_network()
    if own is None or host_net is None:
        return 'needs_setup', 'Run the acceptance check with sudo, so it can see which network OpenRGB uses.'
    if own == host_net:
        return (
            'fail',
            'OpenRGB is on the computer\'s own network, where any program could reach it: restart the computer, then run setup again.',
        )
    return 'pass', 'OpenRGB is in a private network of its own; only the control service reaches it.'


def dashboard_checks(config, now):
    port = dashboard_port(config)
    good = port is not None and dashboard_check(port, now)
    out = [
        item(
            'dashboard:status',
            'Live dashboard status',
            'pass' if good else 'fail',
            'Local endpoint must return a current sanitized status snapshot.',
        )
    ]
    for unit in ('nekoserver-dashboard.service', 'nekoserver-dashboard-collect.timer'):
        out.append(
            item(
                unit,
                unit,
                'pass' if service(unit) else 'fail',
                'Dashboard web service and collection timer must be active and enabled.',
            )
        )
    out.append(item('dashboard:control', 'Fan, light and app control service', *control_check()))
    # Lighting on, and OpenRGB set up (setup skips it where the system has none).
    values = config.get('dashboard', {})
    if (
        values.get('fan_control', True)
        and values.get('rgb', True)
        and Path('/etc/systemd/system', OPENRGB_UNIT).is_file()
    ):
        out.append(item('dashboard:lighting', 'Lighting kept private (OpenRGB)', *lighting_check()))
    return out


OPTIONAL = ('tv', 'notifications', 'heartbeat')


def restore_tests(modules, config):
    """The witnessed restore tests of the backups set up (beside the photo restore): one per
    kind of data, as the dashboard's RESTORE TRIED asks for (status_checks.restore_tests)."""
    out = []
    if 'photo_backup' in modules and neko_common.nextcloud_enabled(config):
        out += [
            (
                'cloud_restore',
                'Nextcloud files restore from the USB copy',
                'Run  sudo bash install.sh --recover , choose "Nextcloud files" and restore one user into a new folder. Open a few files of different kinds and compare them with the originals. Uploading them back is your choice; nothing live is replaced.',
            )
        ]
    if 'music_backup' in modules and music_backed_up(config):
        out += [
            (
                'music_restore',
                'Music library restore',
                'In the release folder, run  sudo python3 music-recovery.py --backup <HDD>/backups/music --snapshot <HDD>/backups/music/snapshots/<newest> --destination <a new folder>  (<HDD> is your media drive, /mnt/nekoserver-media by default). Play a few restored songs and check the folders match. Nothing live is replaced.',
            )
        ]
    if 'host_backup' in modules:
        out += [
            (
                'host_restore',
                'Host and user restore rehearsal',
                'Run host-recovery.py verify/restore into a new directory. Compare selected save/share files and retained Unix identity/Samba data. Rehearse restoration independently; links remain inert and no live settings are automatically applied.',
            )
        ]
    if 'app_backup' in modules:
        out += [
            (
                'app_restore',
                'Media application restore rehearsal',
                'Run app-recovery.py verify and restore into a new directory. Independently rehearse app startup with blocked network access; verify accounts, settings and databases. Check failure/reboot restart recovery on the server.',
            )
        ]
    return out


def manual_tests(modules, config):
    out = [
        (
            'missing_drive',
            'Missing-drive startup protection',
            'With independent backups and the server shut down, test a deliberately unavailable data drive. Verify dependent services are blocked, then shut down and reconnect. Never unplug a live irreplaceable drive.',
        )
    ]
    if 'media' in modules:
        out += [
            (
                'media_workflow',
                'Media request, import and playback',
                'Using media you are allowed to acquire, complete the configured request/download/import/playback chain. Check Jellyfin hardware acceleration if wanted.',
            ),
            (
                'music_playback',
                'Music import and streaming',
                'Verify configured music imports and actual Navidrome playback from the intended client.',
            ),
        ]
    if 'nextcloud' in modules:
        out += [
            (
                'cloud_client',
                'Personal cloud upload/download and sharing',
                'Use the phone/desktop client through the intended HTTPS gateway; upload/download a disposable file, verify checksum and intended sharing permissions.',
            )
        ]
    if 'photos' in modules and immich_wanted(config):
        out += [
            (
                'photo_preservation',
                'Original uploads and deletion safety',
                'Use disposable test photos/videos. Verify original upload checksums, archive creation, and retention after phone/Immich deletions. Do not delete your only real originals.',
            ),
            (
                'independent_restore',
                'Full photo restore from an independent copy',
                'Restore a matched, independently copied snapshot into an isolated workspace. Open original photos/videos and albums; verify database and deletion behaviour. A started app alone does not pass.',
            ),
        ]
    if 'photo_backup' in modules:
        out += [
            (
                'external_backup',
                'External copy and reconnect',
                'Verify new files and checksums on the configured external drive. Disconnect/reconnect between safe runs, test interrupted-copy recovery and confirm deletions never propagate.',
            )
        ]
    if 'samba' in modules or 'tailscale' in modules:
        out += [
            (
                'remote_permissions',
                'Actual remote access and permissions',
                'Test from the intended PC/phone and outside home where applicable. Confirm guest access is denied and protected archive shares reject writes. Selected applications use the separately verified gateway.',
            )
        ]
    if 'gateway_lan' in modules or 'gateway_tailnet' in modules:
        out += [
            (
                'gateway_clients',
                'Application gateway on real clients',
                'On each intended phone/PC, trust the LAN CA where needed and verify login, permission denial, media streaming, WebSockets and original uploads. For Tailscale also test away from home and verify tailnet policy. Management interfaces must remain inaccessible through the gateway.',
            )
        ]
    if 'server_os' in modules:
        out += [
            (
                'server_idle',
                'Server idle and drive policy',
                'After reboot, leave the server idle longer than desktop idle timers. Verify jobs/network access continue with screen off and observe the intended HDD standby behavior without powering down irreplaceable drives.',
            )
        ]
    if 'tv' in modules:
        out += [
            (
                'tv_session',
                'HDMI, sound, controller and Remote Play',
                'Verify TV picture/audio, controller input, Steam login/Remote Play and browser/media launchers. Confirm idle AC settings keep server jobs running.',
            )
        ]
    if 'heartbeat' in modules:
        out += [
            (
                'outage_delivery',
                'Independent outage alert',
                'Confirm receipt at the outside monitor, stop only nekoserver-heartbeat.timer until period plus grace has elapsed, witness an alert, re-enable/start it and witness recovery. Keep the monitor outside this PC and its power/network failure domain.',
            )
        ]
    if 'notifications' in modules:
        out += [
            (
                'notification_delivery',
                'Phone notification delivery',
                'Subscribe the intended phone to the protected ntfy topic; run the native test command and witness receipt with the dashboard closed. Rehearse problem/recovery delivery and network interruption without touching irreplaceable drives.',
            )
        ]
    if 'recovery' in modules:
        out += [
            (
                'os_recovery',
                'OS replacement/recovery exercise',
                'With independent verified backups, rehearse the documented supported OS recovery and confirm retained data/apps and reboot behaviour.',
            )
        ]
    out += restore_tests(modules, config)
    return [item(key, name, 'needs_setup', detail) for key, name, detail in out]


def log_rotation():
    """Containers whose logs have no size limit, or None if Docker cannot be read."""
    from modules import docker as docker_module

    rc, ids = health.run(['docker', 'ps', '-aq', '--no-trunc'], 20)
    # rc is None when Docker is not installed (or did not answer): nothing was checked.
    if rc != 0:
        return None
    if not ids.split():
        return []
    rc, out = health.run(['docker', 'inspect', *ids.split()], 30)
    try:
        return docker_module.unbounded_logs(json.loads(out)) if rc == 0 else None
    except ValueError:
        return None


def log_rotation_finding(unbounded):
    """(status, detail) for the container log check; None means Docker could not be read."""
    if unbounded == []:
        return 'pass', 'Every container log is size-limited.'
    if unbounded is None:
        return (
            'needs_setup',
            'Docker could not be asked about its containers (is it installed and running?), so their log limits were not checked.',
        )
    names = ': ' + ', '.join(unbounded[:8]) if unbounded else ''
    return 'needs_setup', (
        'Unlimited container logs can fill the SSD' + names + '. Containers made before the limit '
        'keep unlimited logs until they are recreated after Docker restarts (a reboot, then the '
        'monthly update or rerunning setup).'
    )


def host_finding(supported):
    """(status, detail) for the recovery host check, naming every Linux family setup supports."""
    families = ', '.join(f.label for f in host.FAMILIES)
    if supported:
        return 'pass', f'Supported x86_64 Linux detected (one of: {families}).'
    return 'needs_setup', f'A supported x86_64 Linux is required: {families}.'


def unavailable_steps(config):
    """The steps setup itself recorded as not available on this PC ("unavailable" in its
    state on the SSD): a TV step on a PC without a desktop, the USB copy with nothing to
    copy. Empty when the state cannot be read."""
    try:
        # The SSD folder only as the settings checks allow it (absolute, no '..', no
        # links), and the state file read only from inside it.
        root = os.path.realpath(mount_path(config['data_mount']))
        state = os.path.realpath(os.path.join(root, 'installer', 'state', 'state.json'))
        if not state.startswith(root + os.sep):
            return set()
        saved = json.loads(safe_path(state).read_text())
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        return set()
    return (
        {name for name, row in saved.items() if isinstance(row, dict) and row.get('status') == 'unavailable'}
        if isinstance(saved, dict)
        else set()
    )


def accepted_modules(config, modules):
    """The planned steps that have something to accept: not optional extras you turned
    down ("wanted": false), nor what this PC can't have (the same decision the step
    itself makes, and what setup recorded as not available)."""
    skipped = unavailable_steps(config)
    # Nextcloud switched off in the settings ("enabled": false) is not set up: its checks
    # and its client test would only ever fail or wait (the nextcloud step skips itself).
    if not neko_common.nextcloud_enabled(config):
        skipped = skipped | {'nextcloud'}
    # A media group switched off (Photos, Movies and shows, Music): its steps set
    # nothing up, so there is nothing of theirs to accept; with every group off, none
    # of the media steps does.
    skipped = skipped | media_steps_left_out(config)
    return [
        m
        for m in modules
        if not (m in OPTIONAL and config.get(m, {}).get('wanted') is False)
        and not (m == 'tv' and not tv.available())
        and m not in skipped
    ]


BUNDLE_CHECK = 'Preserved current installer'


class Inspection:
    """One run of the automatic checks: the settings, the steps checked, the time and the
    report, with what the drive checks found (`matched`, `drives`) and what the last
    report says (`previous`, and whether it is for the same profile and settings: `prior`)."""

    def __init__(self, config, modules, now, run):
        self.config = config
        self.modules = modules
        self.now = now
        self.run = run
        self.checks = run['automatic']
        self.matched = {}
        self.drives = False
        self.previous = {}
        self.prior = False
        self._states = []

    def add(self, *x):
        self.checks.append(item(*x))

    def states_now(self):
        """The runtime's job states, asked once per inspection."""
        if not self._states:
            self._states.append(job_states(self.config))
        return self._states[0]


def checked_modules(config, profile, run):
    """The steps of `profile`, once the settings are found sound; None (and the failed
    configuration row in `run`) when they are not."""
    checks = run['automatic']
    why = 'Configuration or selected dependency profile is invalid.'
    try:
        if not isinstance(config, dict):
            raise installer.InstallerError('The settings file holds no settings (it must be one JSON object, {...})')
        installer.validate_config(config)
        if profile not in config['profiles']:
            why = f'The settings have no profile "{profile}" (they have: {", ".join(sorted(config["profiles"]))}).'
            raise ValueError()
        modules = accepted_modules(
            config, installer.ordered(installer.load_modules(ROOT / 'modules'), config['profiles'][profile])
        )
        run['config_fingerprint'] = fingerprint(config, profile)
        run['dashboard_scope'] = scope_digest(config)
    except installer.InstallerError as exc:
        checks.append(item('configuration', CONFIGURATION_CHECK, 'fail', str(exc)))
        return None
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        checks.append(item('configuration', CONFIGURATION_CHECK, 'fail', why))
        return None
    return modules


def drive_rows(check):
    """The app SSD and the media drive: each the drive setup chose, and two drives."""
    config = check.config
    for role, name in (('data_ssd', 'Application SSD'), ('media_hdd', 'Media HDD')):
        configured = bool(config['storage'][role].get('uuid'))
        problem = (
            drive_problem(config, (role,)) if configured else 'No drive is chosen for it yet: run  sudo bash install.sh'
        )
        check.matched[role] = problem is None
        check.add(
            'mount:' + role,
            name,
            verdict(check.matched[role], configured),
            'The drive setup chose is mounted and takes new files (the runtime\'s drive check).'
            if check.matched[role]
            # The runtime's words say what is wrong and what to do (another drive, or the
            # right one turned read-only), so they come first.
            else problem.rstrip('.') + '. Nothing on the drive is read until this is fixed.',
        )
    distinct = drive_problem(config, ('data_ssd', 'media_hdd')) if all(check.matched.values()) else 'not checked'
    check.drives = distinct is None
    if all(check.matched.values()) and not check.drives:
        check.add('mount:distinct', 'Separate storage identities', 'fail', distinct)


def reboot_row(check):
    """Whether the same checks passed on a later boot than the saved baseline."""
    config, run, now = check.config, check.run, check.now
    current_boot = boot_id()
    run['boot_id'] = current_boot
    previous = (
        load_json(Path(config['data_mount']) / 'reports/acceptance/latest.json') if check.matched['data_ssd'] else {}
    )
    prior = (
        previous.get('format') == 'neko-acceptance-v1'
        and previous.get('profile') == run['profile']
        and previous.get('config_fingerprint') == run['config_fingerprint']
    )
    check.previous, check.prior = previous, prior
    verified = (
        prior
        and current_boot is not None
        and previous.get('baseline_verified') is True
        and previous.get('reboot_boot_id') == current_boot
        and health.number(previous.get('reboot_verified_at'), 0, now) is not None
    )
    baseline = previous.get('baseline_boot_id') if prior and previous.get('baseline_verified') is True else current_boot
    run['baseline_verified'] = check.drives
    observed = (
        prior
        and previous.get('baseline_verified') is True
        and current_boot is not None
        and isinstance(baseline, str)
        and baseline != current_boot
        and check.drives
    )
    if (observed or verified) and check.drives:
        run.update(
            baseline_boot_id=baseline,
            reboot_boot_id=current_boot,
            reboot_verified_at=now if observed else previous['reboot_verified_at'],
        )
        check.add(
            'reboot',
            'Check after a real reboot',
            'pass',
            'This profile has matching storage checks on a later boot than its saved baseline.',
        )
    else:
        run['baseline_boot_id'] = current_boot
        check.add(
            'reboot',
            'Check after a real reboot',
            'needs_setup',
            'Save a baseline, reboot normally and rerun this profile. Same-boot inspections do not pass this check.',
        )


def bundle_row(check):
    """This release's installer, kept on the SSD for a recovery."""
    if not check.drives:
        check.add('bundle', BUNDLE_CHECK, 'blocked', 'Drive verification is required before inspecting private files.')
        return
    try:
        bundle = Path(check.config['data_mount']) / 'installer/releases' / ('v' + VERSION)
        recovery_tools.verify_bundle(bundle, check.config)
        check.add('bundle', BUNDLE_CHECK, 'pass', 'Current SSD recovery bundle and checksums verified.')
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        check.add(
            'bundle',
            BUNDLE_CHECK,
            'needs_setup',
            'Apply recovery from this release to preserve its verified installer bundle.',
        )


def checkpoint_good(config, valid, version):
    """The Docker recovery checkpoint matches the running engine, the drives and the
    media apps' account."""
    saved = load_json(Path(config['data_mount']) / 'installer/host-checkpoint.json')
    expected = {k: config[k] for k in ('storage', 'data_mount', 'media_mount')}
    return (
        valid
        and saved.get('format') == 'neko-host-checkpoint-v1'
        and saved.get('docker_version') == version
        and saved.get('architecture') == platform.machine()
        and saved.get('driver') == 'overlay2'
        and saved.get('docker_root') == str(Path(config['data_mount']) / 'docker-engine')
        and all(saved.get(k) == v for k, v in expected.items())
        and saved.get('uid') == config['media']['uid']
        and saved.get('gid') == config['media']['gid']
    )


def docker_rows(check):
    """Docker's store, service, guard, logs and checkpoint, then the apps' containers."""
    config, modules = check.config, check.modules
    if not check.drives:
        check.add(
            'docker:dependents',
            'Docker/application inspection',
            'blocked',
            'Drive verification is required before reading Docker or application configuration.',
        )
        return
    valid, version = docker_info(config)
    check.add(
        'docker:store',
        'Docker persistent store',
        'pass' if valid else 'fail',
        'Running classic overlay2 store uses the intended SSD.'
        if valid
        else 'Verify Docker availability, SSD data-root and classic storage backend.',
    )
    check.add(
        'docker:service',
        'Docker boot service',
        'pass' if service('docker.service') else 'fail',
        'Service must be active and enabled for boot.',
    )
    check.add(
        'docker:guard',
        'Effective Docker storage guard',
        'pass' if guard_check(config) else 'fail',
        'Installed and loaded UUID/mount guard must match both data drives.',
    )
    check.add('docker:logs', 'Container log size limits', *log_rotation_finding(log_rotation()))
    check.add(
        'checkpoint',
        'Docker recovery checkpoint',
        'pass' if checkpoint_good(config, valid, version) else 'needs_setup',
        'Checkpoint must match the running engine, drives and configured ownership.',
    )
    if 'media' in modules:
        check.checks.extend(containers(config))
        check.checks.extend(media_checks(config))
    if 'nextcloud' in modules:
        from cloud_checks import check as cloud_check

        check.checks.extend(cloud_check(config))
    if 'photos' in modules and immich_wanted(config):
        check.checks.extend(containers(config, photos=True))


BACKUP_STEPS = ('photo_preservation', 'photo_backup', 'app_backup', 'host_backup', 'music_backup')


def backups_selected(config, modules):
    """The backups this server has, by the dashboard's id."""
    # The photo archive and Immich's database copy only with Immich set up: with it
    # switched off those jobs are off too, and could never pass.
    selected = {'archive', 'database'} if 'photo_preservation' in modules and immich_wanted(config) else set()
    if 'photo_backup' in modules:
        selected.add('external')
    if 'app_backup' in modules:
        selected.add('applications')
    if 'music_backup' in modules and music_backed_up(config):
        selected.add('music')
    if 'host_backup' in modules:
        selected.add('host')
    return selected


def external_row(check):
    """The USB (or NAS) drive of the weekly copy is the one setup chose."""
    config = check.config
    spec = config.get('photos', {})
    external = health.mount_info(
        config['external_backup_mount'],
        spec.get('external_backup_uuid', ''),
        spec.get('external_backup_fstype', 'ext4'),
    )
    state = external['status']
    # Mounted: also the NAS disk setup chose, as the weekly copy checks it (the
    # UUID alone passed a restored or copied LUN).
    problem = drive_problem(config, ('usb',)) if state == 'mounted' else None
    check.add(
        'external:identity',
        'External backup identity',
        verdict(state == 'mounted' and problem is None, state == 'wrong_drive' or problem is not None),
        problem
        or 'Connect the intended external drive for this acceptance check; normal offline storage between runs is expected.',
    )


def backup_rows(check):
    """Each backup this server has: done recently, with its timer on."""
    if not any(step in check.modules for step in BACKUP_STEPS):
        return
    # Their reports live on the drives: only read from the drives setup chose (the
    # review of 0.47.2: a refused SSD still gave its reports, and they passed).
    states, why = check.states_now() if check.drives else (None, 'drive verification is required before reading them')
    rows = states['backups'] if states else []
    if not states:
        check.add('backup:states', 'Backup states', 'blocked', 'The backups could not be judged: ' + why)
    selected = backups_selected(check.config, check.modules)
    for job in rows:
        if job['id'] not in selected:
            continue
        status = verdict(
            job['status'] == 'pass'
            and job['last_success'] is not None
            and job['timer']['installed']
            and job['timer']['active'],
            job['status'] in ('fail', 'stale'),
        )
        check.add(
            'backup:' + job['id'],
            job['name'],
            status,
            'A recent successful updated job and active timer are required; checksums/restoration also need manual verification.',
        )
    if 'photo_backup' in check.modules:
        external_row(check)


def maintenance_rows(check):
    """The monthly overnight job: its timer, and a recent successful run."""
    config, now = check.config, check.now
    report = load_json(Path(config['data_mount']) / 'reports/jobs/maintenance.json') if check.drives else {}
    good = (
        report.get('status') == 'pass'
        and type(report.get('last_success')) in (int, float)
        and 0 <= now - report['last_success'] <= 35 * 86400
    )
    check.add(
        'maintenance:timer',
        'Monthly overnight backup and updates',
        'pass' if service('nekoserver-maintenance.timer') else 'needs_setup',
        'Timer must be enabled; missing external storage blocks updates.',
    )
    check.add(
        'maintenance:rehearsal',
        'Recent restore rehearsal and live checks',
        verdict(good, report.get('status') == 'fail'),
        'A real successful monthly transaction is required; unit tests do not establish server acceptance.',
    )


def timer_rows(check):
    """The media check's timer when it is on, and the timers of jobs switched off gone."""
    config, modules = check.config, check.modules
    if 'media_monitor' in modules and config.get('monitoring', {}).get('enabled', True):
        check.add(
            'media:timer',
            'Scheduled media checks',
            'pass' if service('nekoserver-media-health.timer') else 'needs_setup',
            'Scheduled read-only media timer must be active and enabled.',
        )
    for module, enabled, unit in (
        ('media_monitor', config.get('monitoring', {}).get('enabled', True), 'nekoserver-media-health.timer'),
        ('music_backup', music_backed_up(config), 'nekoserver-music-backup.timer'),
        ('backup_cards', neko_common.cards_enabled(config), neko_common.SHARED['cards']['timer']),
    ):
        if module in modules and not enabled:
            check.add(
                'retired:' + unit,
                'Disabled ' + unit,
                'pass' if retired_timer(unit) else 'needs_setup',
                'Old managed timer must be absent or inactive and disabled.',
            )


def heartbeat_row(check):
    """The outage monitor's last check-in was accepted, and its timer is on."""
    states, _ = check.states_now()
    heartbeat = (states or {}).get('heartbeat') or {'status': 'unknown', 'last_success': None, 'timer': {}}
    good = (
        heartbeat['status'] == 'pass'
        and heartbeat['last_success'] is not None
        and heartbeat['timer'].get('installed')
        and heartbeat['timer'].get('active')
    )
    check.add(
        'heartbeat:delivery',
        'Heartbeat HTTPS acknowledgement',
        verdict(good, heartbeat['status'] in ('fail', 'stale')),
        'A current successful HTTPS request and active timer are required; monitor registration and outside alerts remain witnessed.',
    )


def notification_rows(check):
    """The phone alerts' timer is on, and an alert was accepted."""
    states, _ = check.states_now()
    delivery = (states or {}).get('notifications') or {'status': 'unknown', 'last_success': None, 'timer': {}}
    good = delivery['timer'].get('installed') and delivery['timer'].get('active')
    check.add(
        'notifications:timer',
        'Background notification schedule',
        'pass' if good else 'needs_setup',
        'Native notification timer must be installed and active.',
    )
    check.add(
        'notifications:delivery',
        'Notification provider acceptance',
        verdict(
            delivery['status'] not in ('fail', 'stale') and delivery['last_success'] is not None,
            delivery['status'] in ('fail', 'stale'),
        ),
        'A recorded provider-accepted delivery is required; actual phone receipt remains a witnessed check.',
    )


def tailscale_row(check):
    """Tailscale is signed in and its service on."""
    rc, out = health.run(['tailscale', 'status', '--json'], 15)
    try:
        value = json.loads(out)
        active = rc == 0 and isinstance(value, dict) and value.get('BackendState') == 'Running'
    except ValueError:
        active = False
    check.add(
        'tailscale',
        'Tailscale authentication',
        'pass' if active and service(host.service('tailscale')) else 'needs_setup',
        'Authenticated local backend and enabled service required; actual remote access remains a witnessed test.',
    )


def gateway_rows(check):
    """The application gateways (home network, Tailscale)."""
    for kind, module in (('lan', 'gateway_lan'), ('tailnet', 'gateway_tailnet')):
        if module not in check.modules:
            continue
        if check.drives:
            check.checks.extend(gateway_checks.check(check.config, kind))
        else:
            check.add(
                'gateway:' + kind,
                'Application gateway',
                'blocked',
                'Drive verification is required before reading gateway state.',
            )
        if kind == 'lan':
            check.add(
                'gateway:lan:service',
                'LAN gateway boot service',
                'pass' if service('nekoserver-gateway-lan.service') else 'needs_setup',
                'LAN gateway must be active and enabled.',
            )


def launchers_good(home, hashes):
    """Each TV launcher file is the one the TV step wrote (its checksum in the report)."""
    for name, digest in hashes.items():
        rel = Path(name)
        if (
            rel.is_absolute()
            or '..' in rel.parts
            or not isinstance(digest, str)
            or not re.fullmatch(r'[a-f0-9]{64}', digest)
            or hashlib.sha256(safe_path(home / rel).read_bytes()).hexdigest() != digest
        ):
            return False
    return True


def tv_row(check):
    """The TV desktop's launchers are the ones the TV step installed."""
    config = check.config
    try:
        user, home = tv.desktop_user(config)
        saved = load_json(Path(config['data_mount']) / 'reports/tv-setup.json') if check.matched['data_ssd'] else {}
        hashes = saved.get('launchers_sha256', {})
        good = saved.get('user') == user.pw_name and isinstance(hashes, dict) and len(hashes) >= 2
        good = good and launchers_good(home, hashes)
        check.add(
            'tv:launchers',
            'TV desktop launchers',
            'pass' if good else 'needs_setup',
            'Current desktop launcher files must match their installation report; controller/display/session tests remain manual.',
        )
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        check.add(
            'tv:launchers',
            'TV desktop launchers',
            'needs_setup',
            'TV user or managed launcher evidence unavailable; reapply TV setup.',
        )


def runtime_rows(check):
    """NekoServer's own program, as each of its services runs it."""
    if not check.drives:
        check.add(
            'runtime',
            'Native runtime',
            'blocked',
            'Drive verification is required before inspecting installed native workers.',
        )
        return
    try:
        check.checks.extend(native_runtime.acceptance_checks(check.config, check.modules))
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        check.add(
            'runtime',
            'Native runtime',
            'needs_setup',
            'Verify native build integrity and reapply the affected modules.',
        )


def service_rows(check):
    """The checks of the steps that keep a service or a report: each only when its step is
    in the profile."""
    config, modules = check.config, check.modules
    if check.drives:
        check.checks.extend(permission_checks.check(config, modules))
    if 'server_os' in modules:
        check.checks.extend(os_checks.check(config))
    if 'maintenance' in modules:
        maintenance_rows(check)
    timer_rows(check)
    if 'dashboard' in modules:
        check.checks.extend(dashboard_checks(config, check.now))
    if 'heartbeat' in modules:
        heartbeat_row(check)
    if 'notifications' in modules:
        notification_rows(check)
    if 'tailscale' in modules:
        tailscale_row(check)
    if 'samba' in modules:
        check.add(
            'samba',
            'Samba boot service',
            'pass' if service(host.service('samba')) else 'needs_setup',
            'Samba service must be active and enabled; share permissions require actual client tests.',
        )
    gateway_rows(check)
    if 'tv' in modules:
        tv_row(check)
    runtime_rows(check)


def inspect(config, profile='full', now=None):
    now = time.time() if now is None else now
    run = {
        'format': 'neko-acceptance-v1',
        'version': VERSION,
        'generated_at': now,
        'run_id': str(uuid.uuid4()),
        'profile': profile,
        'automatic': [],
        'manual': [],
        'scope': 'selected deployment profile; manual results are user reported',
    }
    modules = checked_modules(config, profile, run)
    if modules is None:
        return summarize(run)
    check = Inspection(config, modules, now, run)
    check.add('configuration', CONFIGURATION_CHECK, 'pass', 'Selected profile and dependency plan are valid.')
    check.add('host', 'Supported recovery host', *host_finding(recovery_tools.host_supported()))
    drive_rows(check)
    reboot_row(check)
    bundle_row(check)
    if 'docker' in modules:
        docker_rows(check)
    backup_rows(check)
    service_rows(check)
    run['manual'] = carried(manual_tests(modules, config), check.previous, check.prior, now)
    return summarize(run)


def carried(manual, previous, same, now):
    """The witnessed tests, with what you recorded before for the same profile and settings
    (`same`): a later check, the one setup runs after every run included, must not throw
    away a test restore you watched. Only results you recorded (pass or fail, with when)
    are kept; anything else stays pending."""
    earlier = (
        {row.get('id'): row for row in previous.get('manual', []) if isinstance(row, dict)}
        if same and isinstance(previous.get('manual'), list)
        else {}
    )
    for check in manual:
        row = earlier.get(check['id'], {})
        if (
            row.get('status') in ('pass', 'fail')
            and row.get('evidence') == 'user_reported'
            and health.number(row.get('confirmed_at'), 0, now + 60) is not None
        ):
            check.update(status=row['status'], confirmed_at=row['confirmed_at'], evidence='user_reported')
    return manual


def summarize(report):
    report['summary'] = {
        group: {s: sum(x['status'] == s for x in report[group]) for s in STATES} for group in ('automatic', 'manual')
    }
    report['acceptance_complete'] = bool(report['automatic']) and all(
        x['status'] == 'pass' for group in ('automatic', 'manual') for x in report[group]
    )
    if report['acceptance_complete']:
        report['status'] = 'complete'
    elif any(x['status'] == 'fail' for group in ('automatic', 'manual') for x in report[group]):
        report['status'] = 'failed'
    else:
        report['status'] = 'pending'
    return report


def record_manual(report, prompt=input):
    for check in report['manual']:
        print('\n' + check['name'] + '\n' + check['detail'])
        value = prompt('Type PASS ' + check['id'] + ', FAIL ' + check['id'] + ', or Enter to leave pending: ').strip()
        if value in ('PASS ' + check['id'], 'FAIL ' + check['id']):
            check.update(
                status='pass' if value.startswith('PASS ') else 'fail',
                confirmed_at=time.time(),
                evidence='user_reported',
            )
    return summarize(report)


def guided_manual(config, report):
    """Yes/no handoffs for witnessed tests; never replace automatic findings."""
    from types import SimpleNamespace

    ctx = SimpleNamespace(config=config, apply=True)
    links = load_json(Path(config['data_mount']) / 'reports/gateway-tailnet.json').get('links', {})
    for check in report['manual']:
        key = check['id']
        targets = []
        if key.startswith('tv'):
            targets = [('steam', '')]
        elif any(word in key for word in ('media', 'music', 'cloud', 'photo', 'remote', 'gateway')):
            targets = [('url', url) for url in links.values() if guided_setup.valid_url(url)]
            if not targets:
                targets = [('url', f'http://127.0.0.1:{app_port(app)}/') for app in ('jellyfin', 'immich')]
        if any(word in key for word in ('notification', 'alert')):
            targets = [('url', 'https://ntfy.sh/docs/subscribe/phone/')]
        if 'heartbeat' in key or 'outage' in key:
            targets = [('url', 'https://healthchecks.io/docs/')]
        done = guided_setup.checkpoint(
            ctx,
            check['name'],
            [
                check['detail'],
                'Carry out the test on the named device/service. A browser launched here '
                'cannot operate a different phone/PC or prove hardware behavior.',
                'Do not perform destructive storage/restore tests on your only live data. '
                'Use the documented isolated recovery workflow and independent copies.',
                'Answer yes only after witnessing this specific result. No repeats the instructions '
                'and reopens any relevant local pages/apps; Ctrl+D leaves it pending.',
            ],
            targets,
            ask='test_passed',
            values={'name': check['name']},
        )
        if done:
            check.update(status='pass', confirmed_at=time.time(), evidence='user_reported')
    return summarize(report)


def save(config, report):
    recovery_tools.mounts(config)  # Recheck immediately before any SSD report write.
    path = safe_path(Path(config['data_mount']) / 'reports/acceptance/latest.json')
    atomic_json(path, report)
    return path


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--config',
        type=lambda v: Path(checked_settings(v)),
        default=default_settings(ROOT),
        help=neko_common.SETTINGS_HELP,
    )
    parser.add_argument(
        '--profile', default='full', help='The set of setup steps to check (default: full, the whole server)'
    )
    parser.add_argument('--save', action='store_true', help='Save the report on the SSD, for the dashboard')
    parser.add_argument(
        '--manual',
        action='store_true',
        help='Also ask about the tests you watched yourself (a test restore, say) and record your answers; '
        'needs --save and a terminal',
    )
    return parser


def exit_code(report):
    """0 when acceptance is complete, 2 when a check failed, 1 while some still wait."""
    if report['acceptance_complete']:
        return 0
    if report['status'] == 'failed':
        return 2
    return 1


def checked_and_said(a, config):
    """The inspection run (and the witnessed tests asked with --manual), saved with
    --save, and said on the screen. Returns the exit code."""
    report = inspect(config, a.profile)
    if a.manual:
        if config.get('guided_setup', {}).get('enabled', True):
            guided_manual(config, report)
        else:
            record_manual(report)
    if a.save:
        save(config, report)
    print('NekoServer acceptance · ' + report['profile'] + ' · ' + report['status'])
    for group in ('automatic', 'manual'):
        for check in report[group]:
            print(group + ' / ' + check['id'] + ': ' + check['status'] + ' — ' + check['detail'])
    print('Manual passes are user-reported. This report applies only to the selected profile and current inspection.')
    return exit_code(report)


def main(argv=None):
    neko_common.readable_output()
    parser = arguments()
    a = parser.parse_args(argv)
    if a.manual and (not a.save or not sys.stdin.isatty()):
        parser.error('--manual requires --save and an interactive terminal')
    if os.geteuid() != 0:
        print('Run with sudo for complete local inspection.', file=sys.stderr)
        return 2
    # The settings file read with what is wrong with it named: through load_json a broken
    # or missing file was read as no settings, and the check said only "Configuration or
    # selected dependency profile is invalid".
    try:
        config = installer.read_json(a.config)
    except installer.InstallerError as exc:
        print('Acceptance check stopped: ' + str(exc), file=sys.stderr)
        return 2
    try:
        return checked_and_said(a, config)
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, installer.InstallerError, KeyboardInterrupt) as exc:
        print(
            'Acceptance check could not finish: '
            + installer.failure_reason(exc)
            + '. No application state was changed.',
            file=sys.stderr,
        )
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
