"""Configure private Rust host/Steam/Samba snapshots and staged recovery."""
import os
from pathlib import Path
import pwd
import re
import subprocess

import native_runtime
from neko_common import VERSION, atomic_json, atomic_write, safe_path, verify_storage

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Nightly host, Steam and standalone Samba snapshots; staged restore only'
HOST_FILES = ('/etc/fstab', '/etc/hostname', '/etc/hosts', '/etc/locale.conf',
              '/etc/vconsole.conf', '/etc/pacman.conf', '/etc/pacman.d/mirrorlist',
              '/etc/passwd', '/etc/group', '/etc/docker/daemon.json')
HOST_DIRS = ('/etc/nekoserver-backups', '/etc/nekoserver-os', '/etc/nekoserver-notifications',
             '/etc/nekoserver-gateway', '/etc/nekoserver-heartbeat', '/etc/systemd/sleep.conf.d',
             '/etc/systemd/logind.conf.d', '/etc/systemd/journald.conf.d')


def settings(config):
    opts = config.get('host_backup', {})
    if not isinstance(opts, dict):
        raise RuntimeError('host_backup must be an object')
    time = opts.get('time', '05:15')
    if not isinstance(time, str) or not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', time):
        raise RuntimeError('host_backup.time must be HH:MM')
    for key in ('steam', 'samba'):
        if type(opts.get(key, True)) is not bool:
            raise RuntimeError('host_backup switches must be booleans')
    free = opts.get('minimum_free_gb', 2)
    if type(free) is not int or not 1 <= free <= 1024:
        raise RuntimeError('Invalid backup free-space reserve')
    keep = opts.get('keep_snapshots', 14)
    if type(keep) is not int or not 0 <= keep <= 3650:
        raise RuntimeError('host_backup.keep_snapshots must be a whole number from 0 to 3650')
    paths = opts.get('extra_paths', [])
    if not isinstance(paths, list) or len(paths) > 32 or any(not isinstance(p, str) for p in paths):
        raise RuntimeError('extra_paths must be a list of at most 32 absolute paths')
    return opts


def guard(config):
    """Resolve the selected desktop identity at install time, never at timer time."""
    opts = settings(config)
    sources = []
    def add(path, optional=True):
        path = Path(path)
        sources.append({'id': 'source'+str(len(sources)), 'path': str(path), 'optional': optional})
    add(Path(config['data_mount'])/'installer/releases')
    add(Path(config['data_mount'])/'gateway')
    for name in ('nekoserver-host-backup.service', 'nekoserver-host-backup.timer', 'nekoserver-host-resume.service'):
        add('/etc/systemd/system/'+name)
    for path in HOST_FILES + HOST_DIRS:
        add(path)
    # Preserve only NekoServer units/drop-ins, not unrelated host services.
    units = Path('/etc/systemd/system')
    for p in sorted(units.glob('nekoserver-*')):
        if not p.is_symlink() and str(p) not in {v['path'] for v in sources}: add(p)
    for path in ('/etc/systemd/system/docker.service.d', '/etc/systemd/system/smb.service.d'):
        add(path)
    user = None
    if opts.get('steam', True) or opts.get('extra_paths'):
        selected = opts.get('user', 'auto')
        if selected == 'auto': selected = os.environ.get('SUDO_USER') or config.get('tv', {}).get('user', 'auto')
        if selected == 'auto': user = pwd.getpwuid(config.get('media', {}).get('uid', 1000))
        elif isinstance(selected, str) and re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', selected): user = pwd.getpwnam(selected)
        else: raise RuntimeError('Choose a valid host backup user')
        home = safe_path(user.pw_dir)
        if user.pw_uid < 1000 or not home.is_absolute() or not home.is_dir() or home.stat().st_uid != user.pw_uid:
            raise RuntimeError('Host backup needs an existing non-root user with an owned home')
        if opts.get('steam', True):
            # Native Arch Steam. Root symlinks are resolved only here, within this home.
            steam = home/'.local/share/Steam'
            if not steam.exists() and (home/'.steam/steam').exists(): steam = home/'.steam/steam'
            if steam.exists():
                steam = steam.resolve(strict=True)
                if home not in steam.parents: raise RuntimeError('Steam root outside selected home; configure explicit extra paths')
            for name in ('config', 'userdata', 'steamapps/compatdata'):
                add(steam/name)
        for value in opts.get('extra_paths', []):
            path = safe_path(value)
            if not path.is_absolute() or not any(root in path.parents for root in (home, Path(config['data_mount']), Path(config['media_mount']))):
                raise RuntimeError('Extra paths must be inside the selected home or verified storage')
            if path == Path(config['media_mount'])/'backups' or Path(config['media_mount'])/'backups' in path.parents:
                raise RuntimeError('Backup recursion refused')
            add(path, False)
    samba = opts.get('samba', True)
    if samba:
        add('/etc/samba')
        add('/var/lib/samba')
    value = native_runtime.storage_config(config)
    value['host_backup'] = {'sources': sources, 'samba': samba, 'steam_uid': user.pw_uid if user and opts.get('steam', True) else None,
                            'minimum_free_bytes': opts.get('minimum_free_gb', 2)*1024**3,
                            'keep_snapshots': opts.get('keep_snapshots', 14)}
    return value


def definitions(config, binary, private):
    time = settings(config).get('time', '05:15')
    resume = f'{binary} host-resume --config {private}'
    mounts = f'RequiresMountsFor={config["data_mount"]} {config["media_mount"]}\n'
    return {
        'nekoserver-host-backup.service': '[Unit]\nDescription=NekoServer host and user snapshot\n'+mounts+
            '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=6h\n'+f'ExecStart={binary} host-snapshot --config {private}\nExecStopPost={resume}\nTimeoutStopSec=5min\n',
        'nekoserver-host-backup.timer': '[Unit]\nDescription=NekoServer nightly host and user snapshot\n'+f'[Timer]\nOnCalendar=*-*-* {time}:00\nPersistent=true\n[Install]\nWantedBy=timers.target\n',
        'nekoserver-host-resume.service': '[Unit]\nDescription=NekoServer recover Samba after interrupted snapshot\n'+mounts+
            '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=5min\n'+f'ExecStart={resume}\n[Install]\nWantedBy=multi-user.target\n',
    }


def run(context):
    settings(context.config)
    if not context.apply:
        context.log('Host snapshot timer available with --apply; Steam must be closed, extra save/share paths selected explicitly')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Host snapshots require root and runtime.backend=rust')
    verify_storage(context.config)
    try: options = guard(context.config)
    except KeyError:
        return {'status': 'needs_setup', 'reason': 'Set host_backup.user to the existing desktop user and reapply'}
    project = Path(__file__).resolve().parents[1]
    binary = native_runtime.deploy(project, context.config, Path(context.config['data_mount'])/'installer/runtime'/('v'+VERSION))
    private = safe_path('/etc/nekoserver-backups/host.json')
    units = definitions(context.config, binary, private)
    for name in units:
        path = safe_path(Path('/etc/systemd/system')/name)
        if path.exists() and 'Description=NekoServer ' not in path.read_text():
            raise RuntimeError('Unmanaged host backup unit preserved')
    atomic_json(private, options)
    for name, value in units.items(): atomic_write(Path('/etc/systemd/system')/name, value, 0o644)
    for command in (['systemctl','daemon-reload'], ['systemctl','enable','--now','nekoserver-host-resume.service'], ['systemctl','enable','--now','nekoserver-host-backup.timer']):
        subprocess.run(command, check=True, capture_output=True, timeout=300)
    context.log('Host/Steam/Samba timer installed; no first snapshot assumed. Extra save/share folders require explicit selection; independent restore remains pending.')
