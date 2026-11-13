"""Verify the native runtime built for this release (by install.sh on this PC, or the
static release build) and deploy it without compiling as root."""

import hashlib, json, platform, subprocess, sys
from pathlib import Path
from neko_common import RECOVERY, SYSTEMD_UNITS, VERSION, safe_path, atomic_write, atomic_json

TARGET = 'x86_64-unknown-linux-musl'  # the static release build
TARGETS = (TARGET, 'x86_64-unknown-linux-gnu')  # ... or this PC's own, as install.sh builds it
NAME = 'nekoserver-runtime'
SOURCES = ('Cargo.toml', 'Cargo.lock', 'build.py', 'src/shared.json')


def expected_sources(root):
    # Every Rust source, those in folders of their own too (src/cardset/, 0.51.0).
    return {*SOURCES, *[str(p.relative_to(root)) for p in (root / 'src').rglob('*.rs')]}


def runs_here(binary, run=subprocess.run):
    """The runtime starts on this system and names this release. A copy built on another
    Linux (kept on the SSD through a reinstall) may not: it is built again then."""
    try:
        result = run([str(binary), '--version'], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip().endswith(' ' + VERSION)


def build_needed(project, run=subprocess.run):
    """True when the release folder has no runtime yet, one built from other sources than
    these, one its record does not describe (a build cut off before its record was
    written), or one that does not run here (install.sh then builds it)."""
    root = Path(project) / 'runtime'
    try:
        manifest = json.loads((root / 'build.json').read_text())
        hashes = manifest.get('source_sha256', {})
        if not (root / 'bin' / NAME).is_file() or set(hashes) != expected_sources(root):
            return True
        if hashlib.sha256((root / 'bin' / NAME).read_bytes()).hexdigest() != manifest.get('binary_sha256'):
            return True
        if any(hashlib.sha256((root / rel).read_bytes()).hexdigest() != hashes[rel] for rel in hashes):
            return True
        return not runs_here(root / 'bin' / NAME, run)
    except (OSError, ValueError, AttributeError):
        return True


def backend(config):
    """Scheduled jobs always use the bundled Rust runtime since v0.21.1.

    The Python fallback was removed. Older configs that say runtime.backend=python,
    or have no runtime setting, are treated as Rust; rerunning the installer
    rewrites their units. install.sh already requires x86_64, which the bundled
    runtime supports.
    """
    options = config.get('runtime', {})
    if not isinstance(options, dict):
        raise RuntimeError('runtime must be an object')
    value = options.get('backend', 'rust')
    if value not in ('rust', 'python'):
        raise RuntimeError('runtime.backend must be rust')
    return 'rust'


def payload(project, config):
    backend(config)
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('The bundled Rust runtime requires x86_64 Linux')
    root = safe_path(Path(project) / 'runtime')
    manifest = json.loads(safe_path(root / 'build.json').read_text())
    if manifest.get('version') != VERSION or manifest.get('target') not in TARGETS:
        raise RuntimeError('Native build identity does not match this release')
    binary = safe_path(root / 'bin' / NAME).read_bytes()
    if hashlib.sha256(binary).hexdigest() != manifest.get('binary_sha256') or binary[:6] != b'\x7fELF\x02\x01':
        raise RuntimeError('Native executable checksum or format mismatch')
    expected = expected_sources(root)
    hashes = manifest.get('source_sha256', {})
    if set(hashes) != expected:
        raise RuntimeError('Native build source manifest is incomplete')
    for rel in expected:
        if hashlib.sha256(safe_path(root / rel).read_bytes()).hexdigest() != hashes[rel]:
            raise RuntimeError(
                'The runtime was built from other sources than these; run  sudo bash install.sh  again (it builds the runtime), or python3 runtime/build.py'
            )
    return binary


def ask_runtime(project, config, args, timeout=60, run=subprocess.run):
    """Run one of the runtime's checks from this release's own verified build (payload),
    for a rule that lives once, in the runtime: (exit code, what it said)."""
    payload(project, config)
    binary = safe_path(Path(project) / 'runtime' / 'bin' / NAME)
    done = run([str(binary), *args], capture_output=True, text=True, timeout=timeout, check=False)
    return done.returncode, ((done.stdout or '') + (done.stderr or '')).strip()


def reason_of(said, fallback):
    """Why one of the runtime's checks refused, from what it said: its last line, without
    the "NekoServer runtime failed:" around it."""
    line = (said or '').strip().splitlines()[-1].strip() if (said or '').strip() else ''
    line = line.removeprefix('NekoServer runtime failed: ').removesuffix(' No success recorded.')
    return line or fallback


RECORD_SUFFIX = '.build.json'  # next to each copy NekoServer deploys: the build it came from


def recorded_copy(target):
    """True when the copy at target is exactly the build its record beside it describes:
    NekoServer put it there and nothing changed it since. No record, one that cannot be
    read, or one naming another build (someone's own copy, a changed one, or one from a
    release before 0.38.0) means the copy is left alone."""
    target = Path(target)
    try:
        value = json.loads(Path(str(target) + RECORD_SUFFIX).read_text())
    except (OSError, ValueError):
        return False
    return (
        isinstance(value, dict)
        and target.is_file()
        and value.get('binary_sha256') == hashlib.sha256(target.read_bytes()).hexdigest()
    )


def deploy(project, config, destination):
    """Put this release's runtime at destination. A copy NekoServer put there itself (its
    build record beside it names exactly that build) is replaced when the runtime was built
    again from other sources, as after `git pull` into the same folder; a copy with no
    record, or one its record does not describe (someone's own, a changed one, or one from
    a release before 0.38.0), is never overwritten."""
    blob = payload(project, config)
    target = safe_path(Path(destination) / NAME)
    record = Path(str(target) + RECORD_SUFFIX)
    manifest = (Path(project) / 'runtime/build.json').read_text()
    if target.exists() and target.read_bytes() != blob:
        if not recorded_copy(target):
            raise RuntimeError(
                f'The runtime copy at {target} differs from the one in this release (same version {VERSION}) '
                'and NekoServer did not put it there (or it was changed since): if this release is the newer '
                f'download (its runtime was rebuilt), move the old copy away (sudo mv {target} {target}.old) and '
                'run setup again; a file you put there yourself is kept'
            )
        atomic_write(target, blob, 0o750)
    elif not target.exists():
        atomic_write(target, blob, 0o750)
    elif target.stat().st_mode & 0o111 == 0:
        raise RuntimeError('Existing native executable has no execute permission; review locally')
    if not record.is_file() or record.read_text() != manifest:
        atomic_write(record, manifest, 0o640)
    return target


# Where each release keeps its own copy of the runtime (modules/*.py, gateway_tools.py).
SYSTEM_HOMES = (
    'nekoserver-dashboard',
    'nekoserver-os',
    'nekoserver-heartbeat',
    'nekoserver-notifications',
    'nekoserver-gateway',
)
KEEP_BUNDLES = 2  # release bundles (installer/releases/vX) kept on the SSD: this one and the one before


def release_number(name):
    """(0, 28, 10) from "v0.28.11"; None for any other folder name."""
    parts = name[1:].split('.') if name.startswith('v') else []
    return tuple(int(p) for p in parts) if parts and all(p.isdigit() for p in parts) else None


def in_use(folder, units=SYSTEMD_UNITS):
    """Some installed service definition, or a drop-in for one, still names this folder:
    Docker's start guard is named only in docker.service.d, and Docker does not start
    without it."""
    text = str(folder)
    for unit in [*Path(units).glob('*'), *Path(units).glob('*.d/*')]:
        try:
            if unit.is_file() and text in unit.read_text(errors='replace'):
                return True
        except OSError:
            return True
    return False


def release_folders(home):
    """This home's per-release folders (v0.28.9, ...), oldest first."""
    if not home.is_dir():
        return []
    found = [p for p in home.glob('v*') if p.is_dir() and not p.is_symlink() and release_number(p.name)]
    return sorted(found, key=lambda p: release_number(p.name))


def settings_files(folder):
    """The settings files a kept release folder holds (config.json and its earlier copies)."""
    return [f for f in Path(folder).glob('config*.json') if f.is_file() and not f.is_symlink()]


UPGRADE_JOURNAL = RECOVERY['upgrade'][0]


def upgrade_needs(journal=UPGRADE_JOURNAL):
    """The release folder names an upgrade at work (or cut off) still needs: the release
    it would put back (its way back). An unreadable record keeps every release, as it
    can't say which one is needed."""
    try:
        saved = json.loads(Path(journal).read_text())
    except FileNotFoundError:
        return set()
    except (OSError, ValueError):
        return None
    if not isinstance(saved, dict):
        return None
    # The record's 'project' is the folder the new release's setup runs from (its
    # download's nekoserver/ folder), never a release folder here: nothing to protect.
    names = {Path(str(saved.get('previous') or '')).name}
    if saved.get('from'):
        names.add('v' + str(saved['from']).lstrip('v'))
    return {n for n in names if n}


def proven_release(current, journal=UPGRADE_JOURNAL):
    """The release this server last ran and passed its check with: the one an upgrade at
    work came from (the release it is setting up has not passed yet), otherwise this one."""
    try:
        came = json.loads(Path(journal).read_text()).get('from')
    except (OSError, ValueError, AttributeError):
        return current
    return 'v' + str(came).lstrip('v') if came else current


def retire_old(data, log=print, lib=Path('/usr/local/lib'), units=SYSTEMD_UNITS, journal=UPGRADE_JOURNAL):
    """After a setup run finished: remove the runtime copies of earlier releases that no
    installed service uses any more (each release deploys its own copies, which otherwise
    stay for good), and release bundles older than the KEEP_BUNDLES newest. The current
    release, and any folder a service still points at, are never touched."""
    import shutil

    data = Path(data)
    current = 'v' + VERSION
    # An upgrade at work runs this release's setup, which ends here, before it checks
    # the result: the release it would put back must still be there then.
    needed = upgrade_needs(journal)
    if needed is None:
        log('Earlier releases kept: the NekoServer upgrade record can not be read')
        return []
    old = []
    # Docker's start guard (modules/docker.py) has its own per-release folder on the SSD.
    for home in [Path(lib) / name for name in SYSTEM_HOMES] + [data / 'installer/runtime', data / 'installer/guards']:
        old += [p for p in release_folders(home) if p.name != current and not in_use(p, units)]
    old += retired_bundles(data / 'installer/releases', current, units, proven_release(current, journal))
    old = [p for p in old if p.name.split('.replaced-')[0] not in needed]
    for folder in old:
        shutil.rmtree(folder, ignore_errors=True)
    if old:
        log('Removed the files of earlier releases no longer in use: ' + ', '.join(str(p) for p in old))
    return old


def retired_bundles(releases, current, units=SYSTEMD_UNITS, proven=None):
    """The release bundles kept on the SSD that retire_old removes."""
    folders = release_folders(releases)
    # The two kept are the last release that passed its check and the one before it. A
    # newer one is an upgrade still at work (kept: it is this release) or one that was
    # put back: counted among the two, it pushed out a release that worked.
    last = release_number(proven or current)
    ahead = [p for p in folders if release_number(p.name) > last and p.name != current]
    earlier = [p for p in folders if release_number(p.name) <= last][:-KEEP_BUNDLES]
    # A bundle a service still runs from (the Immich snapshot's boot hook runs recover.py
    # from it while a restart is owed) stays, as the runtime copies above do.
    bundles = [p for p in earlier if p.name != current and not in_use(p, units)]
    # A kept copy set aside by another build of its version (modules/recovery.set_aside) goes with that version,
    # unless it holds settings no remaining folder has (an earlier release set copies aside with them inside).
    aside = [
        p
        for p in (releases.glob('v*.replaced-*') if releases.is_dir() else [])
        if p.is_dir() and p.name.split('.replaced-')[0] != current
    ]
    # The release that was put back goes the same way: it stays if only it has some settings.
    spare = [p for p in ahead if not in_use(p, units)] + aside
    kept = [p for p in (releases.iterdir() if releases.is_dir() else []) if p.is_dir() and p not in bundles + spare]
    held = {f.read_bytes() for folder in kept for f in settings_files(folder)}
    return bundles + [p for p in spare if not any(f.read_bytes() not in held for f in settings_files(p))]


def saved_paths(config):
    """The folders setup decided for the apps' files, written down for the runtime: it reads
    them and never works them out again (common::saved_path), so the two halves can't
    disagree. Every settings file the runtime reads carries them."""
    from neko_common import (
        LOCATIONS,
        cloud_files,
        location,
        movies_library,
        music_downloads,
        music_library,
        photo_library,
    )

    paths = {
        'photos': str(photo_library(config)),
        'music': str(music_library(config)),
        'downloads': str(music_downloads(config)),
        'cloud': str(cloud_files(config)),
    }
    if location(config, 'movies') != LOCATIONS['movies'][0]:
        # The films and shows placed off the media drive (0.50.0): said only then, so a
        # server that has them where they always were keeps the settings files it has.
        paths['library'] = str(movies_library(config))
    return paths


# The apps that see each kind of the person's folders (their Compose files bind
# library/existing of that kind's drive): the ones stopped while its share is away.
SHARE_APPS = {
    'movies': ('jellyfin', 'radarr', 'sonarr', 'qbittorrent'),
    'tv': ('jellyfin', 'radarr', 'sonarr', 'qbittorrent'),
    'music': ('navidrome', 'lidarr'),
}


def shared_folders(config):
    """The person's own folders on a network share (NFS, SMB) used where they are
    (existing_media.py), as the runtime checks them: what it holds, the folder and the
    share it is on, where it is mounted into the library, and the apps that see it."""
    import existing_media
    from neko_common import media_services

    found = []
    for entry in existing_media.extras(config):
        if entry['fstype'] not in existing_media.NETWORK:
            continue
        found.append(
            {
                'holds': entry['holds'],
                'path': entry['path'],
                'mount': entry['mount'],
                'share': entry['share'],
                'point': str(existing_media.mount_point(config, entry['holds'], entry['use'])),
                'apps': [app for app in SHARE_APPS[entry['holds']] if app in media_services(config)],
            }
        )
    return found


def storage_config(config):
    """The drives as the runtime's jobs read them, with where each app keeps its files."""
    found = {k: config[k] for k in ('data_mount', 'media_mount', 'storage')}
    if config.get('locations'):
        found['locations'] = config['locations']
    found['paths'] = saved_paths(config)
    import placement

    elsewhere = placement.apps_elsewhere(config)
    if elsewhere:
        # The apps whose settings are on another drive than the apps' own (0.50.0): the
        # app backup and the monthly update take each one's folder on that drive
        # (app_snapshot.rs). Said only then: every other server keeps its settings files.
        found['installed'] = elsewhere
    backup = config.get('photos', {}).get('external_backup_network') or {}
    if backup.get('wwn'):
        # The backup drive's NAS disk: the weekly copy checks it is that one (backup.rs).
        found['backup_drive'] = {'wwn': backup['wwn']}
    elif backup.get('transport') == 'nfs':
        # The backup drive is a folder the NAS shares: the weekly copy mounts it by this
        # address (backup.rs UsbRun).
        found['backup_drive'] = {'export': backup.get('export')}
    import existing_media

    extra = existing_media.backed_up(config)
    if extra:
        # The person's music folders the music backup copies too (G7; music_backup.rs).
        found['music_extra'] = extra
    shares = shared_folders(config)
    if shares:
        # The person's own folders on a network share, used where they are (0.50.0): the
        # apps that see one stay stopped while it does not answer (shared_folders.rs).
        found['shared_folders'] = shares
    nas = config.get('network_storage') or {}
    if nas.get('wanted') is True:
        # The NAS check logs in again once the NAS answers (runtime nas_watch.rs).
        found['network_storage'] = {k: nas[k] for k in ('wanted', 'portal', 'targets') if k in nas}
    return found


def backup_guard(config):
    path = safe_path('/etc/nekoserver-backups/storage.json')
    atomic_json(path, storage_config(config))
    return path


# How a unit names its settings file on its command line.
CONFIG_FLAG = '--config '


def acceptance_checks(config, modules):
    """Inspect native selection, executable bytes, private guard and loaded commands."""
    import status_checks as health

    root = Path(__file__).resolve().parent
    expected = hashlib.sha256(payload(root, config)).hexdigest()
    data = Path(config['data_mount'])
    rows = []
    jobs = []
    if 'dashboard' in modules:
        code = Path('/usr/local/lib/nekoserver-dashboard') / ('v' + VERSION) / NAME
        jobs += [
            ('nekoserver-dashboard.service', code, 'serve', False),
            ('nekoserver-dashboard-collect.service', code, 'collect', False),
        ]
        if config.get('dashboard', {}).get('fan_control', True):
            jobs += [('nekoserver-control.service', code, 'control', False)]
    if 'server_os' in modules and config.get('server_os', {}).get('hdd_policy', 'keep_spinning') in (
        'keep_spinning',
        'idle',
    ):
        jobs += [
            (
                'nekoserver-hdd-policy.service',
                Path('/usr/local/lib/nekoserver-os') / ('v' + VERSION) / NAME,
                'hdd-policy',
                False,
            )
        ]
    if 'heartbeat' in modules:
        jobs += [
            (
                'nekoserver-heartbeat.service',
                Path('/usr/local/lib/nekoserver-heartbeat') / ('v' + VERSION) / NAME,
                'heartbeat',
                False,
            )
        ]
    if 'notifications' in modules:
        jobs += [
            (
                'nekoserver-notifications.service',
                Path('/usr/local/lib/nekoserver-notifications') / ('v' + VERSION) / NAME,
                'notify',
                False,
            )
        ]
    code = data / 'installer/runtime' / ('v' + VERSION) / NAME
    if 'media_monitor' in modules and config.get('monitoring', {}).get('enabled', True):
        jobs += [('nekoserver-media-health.service', code, 'media-health', False)]
    if 'photo_preservation' in modules:
        jobs += [('nekoserver-immich-db-backup.service', code, 'preservation', True)]
        if config.get('photos', {}).get('backup_schedule') != 'weekly':
            jobs += [('nekoserver-photo-preservation.service', code, 'preservation', True)]
    if 'host_backup' in modules:
        jobs += [('nekoserver-host-backup.service', code, 'host-snapshot', False)]
    if 'maintenance' in modules:
        jobs += [('nekoserver-maintenance.service', code, 'maintenance', False)]
    if 'app_backup' in modules:
        jobs += [('nekoserver-app-backup.service', code, 'app-snapshot', False)]
    from neko_common import music_backed_up

    if 'music_backup' in modules and music_backed_up(config):
        jobs += [('nekoserver-music-backup.service', code, 'music-backup', True)]
    if 'photo_backup' in modules:
        jobs += [('nekoserver-photo-backup.service', code, 'weekly', True)]
    for unit, binary, command, guard in jobs:
        good = False
        try:
            good = hashlib.sha256(safe_path(binary).read_bytes()).hexdigest() == expected
            rc, loaded = health.run(['systemctl', 'show', unit, '--property=ExecStart,FragmentPath'])
            good = (
                good
                and rc == 0
                and ('argv[]=' + str(binary) + ' ' + command + ' ') in loaded
                and ('FragmentPath=' + str(SYSTEMD_UNITS / unit)) in loaded
            )
            if guard:
                good = (
                    good
                    and health.read_json('/etc/nekoserver-backups/storage.json') == storage_config(config)
                    and '--config /etc/nekoserver-backups/storage.json' in loaded
                )
            if command == 'maintenance':
                private = safe_path('/etc/nekoserver-backups/maintenance.json')
                good = (
                    good
                    and health.read_json(private) == config
                    and private.stat().st_uid == 0
                    and private.stat().st_mode & 0o077 == 0
                    and CONFIG_FLAG + str(private) in loaded
                )
                rc, recovery = health.run(['systemctl', 'show', unit, '--property=ExecStopPost'])
                good = good and rc == 0 and str(binary) + ' maintenance-resume --config ' + str(private) in recovery
            if command == 'app-snapshot':
                from modules import app_backup

                expected_guard = app_backup.guard_config(config)
                good = (
                    good
                    and health.read_json('/etc/nekoserver-backups/applications.json') == expected_guard
                    and '--config /etc/nekoserver-backups/applications.json' in loaded
                )
                rc, recovery = health.run(['systemctl', 'show', unit, '--property=ExecStopPost'])
                good = (
                    good
                    and rc == 0
                    and str(binary) + ' app-resume --config /etc/nekoserver-backups/applications.json' in recovery
                )
            if command == 'host-snapshot':
                from modules import host_backup

                good = (
                    good
                    and health.read_json('/etc/nekoserver-backups/host.json') == host_backup.expected(config)
                    and '--config /etc/nekoserver-backups/host.json' in loaded
                )
                rc, recovery = health.run(['systemctl', 'show', unit, '--property=ExecStopPost'])
                good = (
                    good
                    and rc == 0
                    and str(binary) + ' host-resume --config /etc/nekoserver-backups/host.json' in recovery
                )
            if command == 'hdd-policy':
                from modules import server_os

                good = (
                    good
                    and health.read_json('/etc/nekoserver-os/storage.json') == server_os.hdd_guard(config)
                    and '--config /etc/nekoserver-os/storage.json' in loaded
                )
            if command == 'heartbeat':
                private = safe_path('/etc/nekoserver-heartbeat/config.json')
                from modules import heartbeat

                good = (
                    good
                    and CONFIG_FLAG + str(private) in loaded
                    and private.stat().st_uid == 0
                    and private.stat().st_mode & 0o077 == 0
                    and heartbeat.endpoint(health.read_json(private).get('endpoint'))
                )
            if command == 'notify':
                private = safe_path('/etc/nekoserver-notifications/config.json')
                good = (
                    good
                    and CONFIG_FLAG + str(private) in loaded
                    and private.stat().st_uid == 0
                    and private.stat().st_mode & 0o077 == 0
                )
            if command == 'serve':
                good = good and binary.stat().st_mode & 0o005 == 0o005
        except (OSError, RuntimeError):
            pass
        rows.append(
            {
                'id': 'runtime:' + unit,
                'name': 'Native ' + unit.removesuffix('.service'),
                'status': 'pass' if good else 'needs_setup',
                'detail': 'Selected native executable checksum, loaded command and applicable storage guard must match this release.',
            }
        )
    return rows


if __name__ == '__main__':
    # install.sh asks: exit 0 when the runtime must be built (none yet, or other sources).
    if sys.argv[1:] != ['needs-build']:
        print('usage: python3 native_runtime.py needs-build', file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if build_needed(Path(__file__).resolve().parent) else 1)
