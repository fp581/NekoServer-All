"""Deploy isolated media Compose projects with guarded SSD/HDD mounts."""
from neko_common import compose_image_override
import os
import re
import shutil
import subprocess
from pathlib import Path
from neko_common import safe_path, ensure_directory, verify_storage, atomic_write, host_timezone

from importlib.util import spec_from_file_location, module_from_spec

DEPENDENCIES = ['docker']
DESCRIPTION = 'Guarded deployment of eight independent media Compose projects (requires --apply)'
SERVICES = ('jellyfin', 'navidrome', 'sonarr', 'radarr', 'lidarr', 'prowlarr', 'seerr', 'qbittorrent')


def check_mount(path):
    if not shutil.which('findmnt'):
        raise RuntimeError('findmnt required')
    p = Path(path)
    if not p.is_absolute() or not p.is_dir() or p.is_symlink():
        raise RuntimeError(f'Expected existing non-symlink absolute mount directory: {p}')
    cmd = subprocess.run(['findmnt', '-n', '-o', 'TARGET', '--mountpoint', str(p)],
                         capture_output=True, text=True, timeout=10)
    if cmd.returncode or cmd.stdout.strip() != str(p):
        raise RuntimeError(f'Dedicated filesystem must be mounted at {p}; refusing to deploy')
    src = subprocess.run(['findmnt', '-n', '-o', 'SOURCE', '--mountpoint', str(p)],
                         capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    return src


def run_cmd(cmd, timeout=120):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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
        desired = render(template, data, media, uid, gid, tz, bind)
        if dest.exists() and dest.read_text(encoding='utf-8') != desired:
            # Replace only an unedited copy of a template we shipped earlier.
            current = dest.read_text(encoding='utf-8')
            for old_template, backup_name in previous_templates(name, template):
                previous = render(old_template, data, media, uid, gid, tz, bind)
                if current == previous:
                    break
            else:
                raise RuntimeError(f'{dest} has local edits or different settings; refusing to overwrite it')
            backup = safe_path(dest.parent/backup_name)
            if backup.exists() and backup.read_text() != previous:
                raise RuntimeError(f'Template backup {backup} differs; preserved for review')
            migrations[name] = (backup, previous)
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
            os.chown(target, 1000 if name == 'seerr' else uid, 1000 if name == 'seerr' else gid)
        safe_path(target)
        if target.is_symlink():
            raise RuntimeError(f'Refusing symlink appdata: {target}')
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


def network_exists():
    return subprocess.run(['docker', 'network', 'inspect', 'nekoserver-media'], capture_output=True, timeout=15).returncode == 0


def run(context):
    if not context.apply:return _run(context)
    from recovery_tools import mounts, lock
    mounts(context.config)
    with lock(context.config):return _run(context)
