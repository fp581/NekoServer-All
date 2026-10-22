"""Deploy isolated media Compose projects with guarded SSD/HDD mounts."""
from neko_common import compose_image_override
import hashlib
import os
import re
import time
import shutil
import subprocess
from pathlib import Path
from neko_common import APP_PORTS, DASHBOARD_DEFAULT_PORT, safe_path, ensure_directory, verify_storage, atomic_write, host_timezone


DEPENDENCIES = ['docker', 'account_credentials']  # its logins are chosen before it first starts
DESCRIPTION = 'Guarded deployment of eight independent media Compose projects (requires --apply)'
SERVICES = ('jellyfin', 'navidrome', 'sonarr', 'radarr', 'lidarr', 'prowlarr', 'seerr', 'qbittorrent')


def check_mount(path):
    if not shutil.which('findmnt'):
        raise RuntimeError('findmnt required')
    p = Path(path)
    if not p.is_absolute() or not p.is_dir() or p.is_symlink():
        raise RuntimeError(f'Expected existing non-symlink absolute mount directory: {p}')
    cmd = subprocess.run(['findmnt', '-n', '-o', 'TARGET', '--mountpoint', str(p)],
                         capture_output=True, text=True, timeout=10, check=False)
    if cmd.returncode or cmd.stdout.strip() != str(p):
        raise RuntimeError(f'Dedicated filesystem must be mounted at {p}; refusing to deploy')
    src = subprocess.run(['findmnt', '-n', '-o', 'SOURCE', '--mountpoint', str(p)],
                         capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    return src


def run_cmd(cmd, timeout=120):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f'{" ".join(cmd[:4])} failed: {result.stderr[-500:]}')
    return result.stdout.strip()


# Lines changed in v0.21.1. Earlier unedited manifests are migrated in place.
LIDARR_V0210_MOUNTS = (
    '      - "${NEKO_MEDIA}/library:/data"\n'
    '      - "${NEKO_DATA}/library/downloads/music:/data/downloads/music"\n'
    '      - "${NEKO_DATA}/library/music:/data/music"\n'
)
LIDARR_V0211_MOUNTS = (
    '      # Single SSD mount: /data/downloads/music and /data/music share one\n'
    '      # filesystem mount, so imports hardlink or move instead of copying.\n'
    '      - "${NEKO_DATA}/library:/data"\n'
)
NAVIDROME_V0170_SCANNER = (
    'ND_MUSICFOLDER: "/music"\n      ND_DATAFOLDER: "/data"\n      ND_SCANNER_SCHEDULE: "0 * * * *"\n'
    '      ND_SCANNER_WATCHERWAIT: "10s"\n      ND_ENABLESHARING: "false"'
)


def previous_templates(name, template):
    """Earlier shipped templates for ``name`` and the backup file to keep for each."""
    candidates = {
        # v0.16.0 and earlier shipped this exact scanner setting.
        'navidrome': [(NAVIDROME_V0170_SCANNER, 'ND_SCANSCHEDULE: "1h"', 'compose.before-music-setup.yaml')],
        # v0.21.0 split Lidarr's music folders across separate bind mounts.
        'lidarr': [(LIDARR_V0211_MOUNTS, LIDARR_V0210_MOUNTS, 'compose.before-v0.21.1.yaml')],
        # v0.21.0 hardcoded Seerr's account IDs.
        'seerr': [('user: "${NEKO_UID}:${NEKO_GID}"', 'user: "1000:1000"', 'compose.before-v0.21.1.yaml')],
    }
    out = []
    for new, old, backup in candidates.get(name, []):
        if new in template:
            out.append((template.replace(new, old), backup))
    return out


GPU_DEVICE_BLOCK = '    devices:\n      - /dev/dri:/dev/dri\n'
GPU_DEVICE = Path('/dev/dri')


QBIT_WEBUI_LINE = '      - "127.0.0.1:8080:8080"\n'
QBIT_ENV_LINE = '      WEBUI_PORT: "8080"\n'
RESERVED_PORTS = {*APP_PORTS, DASHBOARD_DEFAULT_PORT}


def torrent_port(settings):
    """Optional qBittorrent peer port (``media.torrent_port``); None keeps it closed."""
    port = settings.get('torrent_port')
    if port is None:
        return None
    if type(port) is not int or not 1024 <= port <= 65535 or port in RESERVED_PORTS:
        raise RuntimeError('media.torrent_port must be an unused port from 1024 to 65535, or null')
    return port


def adapt_to_host(name, text, gpu_present=None, peer_port=None):
    """Fit a rendered manifest to this host and the chosen options.

    Jellyfin: drop GPU passthrough on hosts without /dev/dri. Docker refuses to
    create a container whose device does not exist, so on a PC without integrated
    graphics (or a VM) Jellyfin would never start; it transcodes in software instead.
    qBittorrent: publish the optional peer port so other peers can connect in.
    """
    if gpu_present is None:
        gpu_present = GPU_DEVICE.is_dir()
    if name == 'jellyfin' and not gpu_present:
        return text.replace(GPU_DEVICE_BLOCK, '')
    if name == 'qbittorrent' and peer_port is not None:
        if QBIT_WEBUI_LINE not in text or QBIT_ENV_LINE not in text:
            raise RuntimeError('qBittorrent template changed; cannot add the peer port')
        text = text.replace(QBIT_ENV_LINE, QBIT_ENV_LINE + f'      TORRENTING_PORT: "{peer_port}"\n')
        text = text.replace(QBIT_WEBUI_LINE, QBIT_WEBUI_LINE + f'      - "{peer_port}:{peer_port}/tcp"\n      - "{peer_port}:{peer_port}/udp"\n')
    return text


def without_host_adaptations(text):
    """A manifest with every adapt_to_host change undone, for recognising our own output."""
    text = text.replace(GPU_DEVICE_BLOCK, '')
    text = re.sub(r' {6}TORRENTING_PORT: "\d+"\n', '', text)
    return re.sub(r' {6}- "(\d+):\1/(?:tcp|udp)"\n', '', text)


TIMEZONE_LINE = re.compile(r'^( +TZ: )"[^"\n]*"$', re.MULTILINE)
# The checksum of the manifest this step last wrote, next to it: a later run then
# knows its own unedited file whatever setting changed since (media.uid, a drive, the
# PC's timezone), and replaces it after keeping a copy.
WRITTEN = '.neko-written'


def checksum(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def written_by_us(dest, current):
    record = dest.parent / WRITTEN
    if record.is_symlink() or not record.is_file():
        return False
    return record.read_text(encoding='utf-8').strip() == checksum(current)


def remember(dest, content):
    atomic_write(safe_path(dest.parent / WRITTEN), checksum(content) + '\n', 0o640)


def app_user(text):
    """The uid:gid a rendered manifest runs its app as (PUID/PGID or user:)."""
    found = re.search(r'PUID: "(\d+)"\n +PGID: "(\d+)"', text) or re.search(r'user: "(\d+):(\d+)"', text)
    return ':'.join(found.groups()) if found else None


def same_user(current, rendered):
    """Navidrome and Seerr run as media.uid:gid and can't read settings and music that
    belong to someone else, so another user is never switched to on a rerun."""
    before, after = app_user(current), app_user(rendered)
    if before != after:
        raise RuntimeError(f'media.uid/gid changed from {before} to {after}, but the apps\' settings and library '
                           f'files belong to {before}. Put media.uid and media.gid back in my-config.json')


def same_but_host(current, rendered):
    """Our own output for this template before a host, option or timezone change (the
    GPU appeared or vanished, the peer port opened or closed, the PC's timezone was
    set). Servers set up before the checksum record are recognised this way."""
    def plain(text):
        return TIMEZONE_LINE.sub(r'\1""', without_host_adaptations(text))
    return plain(current) == plain(rendered)


def replacement(name, dest, template, rendered, values):
    """(backup file, current text) when the manifest at ``dest`` is an unedited one of
    ours that may be replaced; raises when it was edited by someone."""
    current = dest.read_text(encoding='utf-8')
    candidates = [(render(old, *values), backup) for old, backup in previous_templates(name, template)]
    if written_by_us(dest, current):
        same_user(current, rendered)
        candidates.append((current, f'compose.before-{int(time.time())}.yaml'))
    elif same_but_host(current, rendered):
        candidates.append((current, f'compose.before-{int(time.time())}.yaml'))
    previous = next((backup for text, backup in candidates if text == current), None)
    if previous is None:
        raise RuntimeError(f'{dest} was changed by hand, so setup leaves it alone. To let setup write a fresh one '
                           f'(with your current settings), rename it, for example to compose.mine.yaml, and run '
                           f'setup again; your copy stays next to it')
    backup = safe_path(dest.parent / previous)
    if backup.exists() and backup.read_text() != current:
        raise RuntimeError(f'Template backup {backup} differs; preserved for review')
    return backup, current


def render(template, data, media, uid, gid, tz, bind):
    substitutions = dict(DATA=str(data), MEDIA=str(media), UID=str(uid), GID=str(gid), TZ=tz, BIND=bind)
    output = template
    for key, value in substitutions.items():
        output = output.replace('${NEKO_' + key + '}', value)
    if re.search(r'\$\{NEKO_', output):
        raise RuntimeError('Unresolved template variable')
    return output


def _run(context):
    settings = context.config.get('media', {})
    if not context.apply:
        context.log('Media is available but inactive: use --module media --apply to deploy.')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Media deployment requires root (sudo)')
    data = Path(context.config['data_mount'])
    media = Path(context.config['media_mount'])
    if data == media or str(data) == '/' or str(media) == '/':
        raise RuntimeError('Data and media must be distinct dedicated mounts')
    src_data, src_media = check_mount(data), check_mount(media)
    if src_data == src_media:
        raise RuntimeError('Data and media appear to share the same filesystem')
    verify_storage(context.config)
    context.log(f'Dedicated mounts verified: SSD={src_data}, HDD={src_media}')
    if not shutil.which('docker'):
        raise RuntimeError('Docker missing')
    run_cmd(['docker', 'info', '--format', '{{.ServerVersion}}'], timeout=20)
    selected = settings.get('services', list(SERVICES))
    if not isinstance(selected, list) or not selected or any(x not in SERVICES for x in selected):
        raise RuntimeError('media.services must be a nonempty list of known services')
    uid, gid = int(settings.get('uid', 1000)), int(settings.get('gid', 1000))
    tz, bind = host_timezone(context.config), settings.get('bind_ip', '127.0.0.1')
    if uid <= 0 or gid <= 0 or not re.fullmatch(r'[A-Za-z0-9_+./-]+', tz):
        raise RuntimeError('Invalid uid, gid, or timezone')
    if bind != '127.0.0.1':
        raise RuntimeError('Media UIs must stay localhost-only behind the configured gateway')
    project_root = Path(__file__).resolve().parents[1]
    compose_dir = data / 'compose'
    configs = data / 'appdata'
    # Check ALL destinations first; never clobber previously customized manifests.
    manifests = {}
    migrations = {}
    for name in selected:
        dest = compose_dir / name / 'compose.yaml'
        safe_path(dest)
        if dest.is_symlink():
            raise RuntimeError(f'Refusing symlink manifest: {dest}')
        template = (project_root / 'compose' / f'{name}.yaml').read_text(encoding='utf-8')
        values = (data, media, uid, gid, tz, bind)
        rendered = render(template, *values)
        desired = adapt_to_host(name, rendered, peer_port=torrent_port(settings))
        if dest.exists() and dest.read_text(encoding='utf-8') != desired:
            # Replace only an unedited copy of ours (an earlier template included).
            migrations[name] = replacement(name, dest, template, rendered, values)
        manifests[name] = (dest, desired)
    for name in selected:
        for path in (configs / name, compose_dir / name):
            safe_path(path)
            if path.is_symlink():
                raise RuntimeError(f'Refusing symlink path: {path}')
    context.log('Prepared all media Compose manifests without touching existing projects')
    ensure_directory(media / 'library', 0o755)
    ensure_directory(data / 'library', 0o755)
    for folder in ('music', 'downloads/music'):
        target = safe_path(data / 'library' / folder)
        if not target.exists():
            ensure_directory(target, 0o2775, uid, gid)
    for folder in ('movies', 'tv', 'downloads'):
        target = media / 'library' / folder
        safe_path(target)
        if target.is_symlink():
            raise RuntimeError(f'Refusing symlink media directory: {target}')
        if not target.exists():
            ensure_directory(target, 0o2775, uid, gid)
    # No recursive chown: preserve existing users and files. Create fresh appdata owned by configured user.
    for name in selected:
        target = configs / name
        if not target.exists():
            ensure_directory(configs, 0o755)
            ensure_directory(target, 0o750)
            os.chown(target, uid, gid)
        safe_path(target)
        if target.is_symlink():
            raise RuntimeError(f'Refusing symlink appdata: {target}')
        if name == 'seerr':
            adopt_seerr_folder(target, uid, gid, context.log)
    run_cmd(['docker', 'network', 'inspect', 'nekoserver-media'], timeout=15) if network_exists() else run_cmd(['docker', 'network', 'create', 'nekoserver-media'], timeout=20)
    failed = []
    for name, (dest, content) in manifests.items():
        ensure_directory(dest.parent, 0o750)
        # Rerun is permitted only for identical manifests. Never overwrite local edits.
        if name in migrations:
            backup,previous=migrations[name]
            if not backup.exists():atomic_write(backup,previous,0o640)
            atomic_write(dest,content,0o640)
        elif not dest.exists():
            atomic_write(dest, content, 0o640)
        remember(dest, content)
        try:
            run_cmd(['docker', 'compose', '-f', str(dest), '-p', f'neko-{name}', *compose_image_override(dest), 'config', '-q'], timeout=30)
            verify_storage(context.config)
            run_cmd(['docker', 'compose', '-f', str(dest), '-p', f'neko-{name}', *compose_image_override(dest), 'up', '-d'], timeout=900)
            context.log(f'Deployed {name} (localhost only); project neko-{name}')
        except Exception as exc:
            context.log(f'{name} failed: {exc}. Manifest preserved: {dest}; others will continue.')
            failed.append(name)
    if failed:
        raise RuntimeError(f'Media projects failed: {failed}; retry with the same manifest/config once fixed')
    context.log('Media stack deployed. Run the onboarding module to connect accounts, libraries and API keys; verify actual playback.')


def adopt_seerr_folder(folder, uid, gid, log=print):
    """Seerr runs as media.uid:gid. Releases before v0.23.2 created its folder for
    1000:1000 (and before v0.21.1 Seerr itself ran as 1000:1000), so with another user it
    could not read or write its own settings. Hand that folder, and only it, to the
    configured user. Symlinks are never followed."""
    stat = folder.lstat()
    if (stat.st_uid, stat.st_gid) == (uid, gid) or (stat.st_uid, stat.st_gid) != (1000, 1000):
        return False
    for root, dirs, files in os.walk(folder, followlinks=False):
        for name in dirs + files:
            path = Path(root) / name
            if path.lstat().st_uid == 1000:
                os.chown(path, uid, gid, follow_symlinks=False)
    os.chown(folder, uid, gid, follow_symlinks=False)
    log(f'Seerr settings folder handed to {uid}:{gid}, the user Seerr runs as')
    return True


def network_exists():
    return subprocess.run(['docker', 'network', 'inspect', 'nekoserver-media'], capture_output=True, timeout=15, check=False).returncode == 0


def run(context):
    if not context.apply:return _run(context)
    from recovery_tools import mounts, lock
    mounts(context.config)
    with lock(context.config):return _run(context)
