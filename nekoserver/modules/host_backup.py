"""Configure private Rust host/Steam/Samba snapshots and staged recovery."""
import json
import os
from pathlib import Path
import pwd
import re
import subprocess

import native_runtime
import host
from neko_common import RESUME_TIMEOUT, SYSTEMD_UNITS, VERSION, atomic_json, check_units, host_timezone, refresh_settings, safe_path, verify_storage, write_units

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Nightly host, Steam and standalone Samba snapshots; staged restore only'
HOST_FILES = ('/etc/fstab', '/etc/hostname', '/etc/hosts', '/etc/locale.conf',
              '/etc/vconsole.conf',
              '/etc/passwd', '/etc/group', '/etc/docker/daemon.json',
              # The i2c driver OpenRGB needs for memory and motherboard lighting.
              '/etc/modules-load.d/nekoserver-i2c.conf',
              # OpenRGB's own settings, not its whole folder: the server writes a log there
              # all the time (logs/), which made the settings backup fail now and then.
              '/var/lib/nekoserver-openrgb/OpenRGB/OpenRGB.json', '/var/lib/nekoserver-openrgb/OpenRGB/sizes.ors')
HOST_DIRS = ('/etc/nekoserver-backups', '/etc/nekoserver-os', '/etc/nekoserver-notifications',
             '/etc/nekoserver-gateway', '/etc/nekoserver-heartbeat', '/etc/systemd/sleep.conf.d',
             '/etc/systemd/logind.conf.d', '/etc/systemd/journald.conf.d',
             # Fan curves, lighting choices and the PIN hash; the dashboard's settings.
             '/etc/nekoserver-control', '/etc/nekoserver-dashboard')
PRIVATE = Path('/etc/nekoserver-backups/host.json')
BACKUP_UNIT, BACKUP_TIMER = 'nekoserver-host-backup.service', 'nekoserver-host-backup.timer'
RESUME_UNIT = 'nekoserver-host-resume.service'


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
    keep = opts.get('keep_snapshots', 7)
    if type(keep) is not int or not 0 <= keep <= 3650:
        raise RuntimeError('host_backup.keep_snapshots must be a whole number from 0 to 3650')
    paths = opts.get('extra_paths', [])
    if not isinstance(paths, list) or len(paths) > 32 or any(not isinstance(p, str) for p in paths):
        raise RuntimeError('extra_paths must be a list of at most 32 absolute paths')
    return opts


def previous_user(target=None):
    """The account host snapshots already back up (None when not installed yet)."""
    try:
        saved = json.loads((target or PRIVATE).read_text()).get('host_backup', {})
    except (OSError, ValueError, AttributeError):
        return None
    try:
        if isinstance(saved.get('user'), str):
            return pwd.getpwnam(saved['user'])
        if type(saved.get('steam_uid')) is int:
            return pwd.getpwuid(saved['steam_uid'])
    except KeyError:
        return None
    return None


def choose_user(config, opts, previous):
    selected = opts.get('user', 'auto')
    if selected == 'auto':
        # Keep backing up the same person: setup run later by another administrator
        # (or with sudo from another account) must not switch whose files are copied.
        if previous is not None:
            return previous
        selected = os.environ.get('SUDO_USER') or config.get('tv', {}).get('user', 'auto')
    if selected == 'auto':
        return pwd.getpwuid(config.get('media', {}).get('uid', 1000))
    if isinstance(selected, str) and re.fullmatch(r'[a-z_][a-z0-9_-]*[$]?', selected):
        return pwd.getpwnam(selected)
    raise RuntimeError('Choose a valid host backup user')


def system_paths():
    """Host settings and NekoServer's own units, always backed up when present."""
    family = host.family_or_default()
    paths = [str(SYSTEMD_UNITS/name) for name in (BACKUP_UNIT, BACKUP_TIMER, RESUME_UNIT)] + list(HOST_FILES) + list(family.settings) + list(HOST_DIRS)
    # Preserve only NekoServer units/drop-ins, not unrelated host services.
    paths += [str(p) for p in sorted(SYSTEMD_UNITS.glob('nekoserver-*'))
              if not p.is_symlink() and str(p) not in paths]
    paths += [str(SYSTEMD_UNITS/name) for name in (family.service('docker') + '.d', family.service('samba') + '.d')]
    return paths


def owned_home(user):
    home = safe_path(user.pw_dir)
    if user.pw_uid < 1000 or not home.is_absolute() or not home.is_dir() or home.stat().st_uid != user.pw_uid:
        raise RuntimeError('Host backup needs an existing non-root user with an owned home')
    return home


def steam_paths(home):
    """Native Arch Steam. Root symlinks are resolved only here, within this home."""
    steam = home/'.local/share/Steam'
    if not steam.exists() and (home/'.steam/steam').exists():
        steam = home/'.steam/steam'
    if steam.exists():
        steam = steam.resolve(strict=True)
        if home not in steam.parents:
            raise RuntimeError('Steam root outside selected home; configure explicit extra paths')
    return [steam/name for name in ('config', 'userdata', 'steamapps/compatdata')]


def extra_path(config, home, value):
    path = safe_path(value)
    media = Path(config['media_mount'])
    if not path.is_absolute() or not any(root in path.parents for root in (home, Path(config['data_mount']), media)):
        raise RuntimeError('Extra paths must be inside the selected home or verified storage')
    if path == media/'backups' or media/'backups' in path.parents:
        raise RuntimeError('Backup recursion refused')
    return path


def guard(config, previous=None):
    """Resolve the selected desktop identity at install time, never at timer time.
    With user "auto", the account chosen the first time stays chosen."""
    opts = settings(config)
    sources = []
    def add(path, optional=True, steam=False):
        row = {'id': 'source'+str(len(sources)), 'path': str(Path(path)), 'optional': optional}
        if steam:
            # Left out (and reported) on a night Steam is open; the rest is still saved.
            row['steam'] = True
        sources.append(row)
    add(Path(config['data_mount'])/'installer/releases')
    add(Path(config['data_mount'])/'gateway')
    for path in system_paths():
        add(path)
    user = None
    if opts.get('steam', True) or opts.get('extra_paths'):
        user = choose_user(config, opts, previous)
        home = owned_home(user)
        for path in steam_paths(home) if opts.get('steam', True) else []:
            add(path, steam=True)
        for value in opts.get('extra_paths', []):
            add(extra_path(config, home, value), False)
    samba = opts.get('samba', True)
    if samba:
        add('/etc/samba')
        add('/var/lib/samba')
    value = native_runtime.storage_config(config)
    value['host_backup'] = {'sources': sources, 'samba': samba, 'steam_uid': user.pw_uid if user and opts.get('steam', True) else None,
                            'user': user.pw_name if user else None,
                            'minimum_free_bytes': opts.get('minimum_free_gb', 2)*1024**3,
                            'keep_snapshots': opts.get('keep_snapshots', 7)}
    return value


def expected(config, target=None):
    """What the installed list should hold: the same answer for setup, refresh and the
    acceptance check (the account already backed up stays chosen)."""
    return guard(config, previous_user(target))


def refresh(config, log, target=None):
    """After every setup step: an existing host snapshot list is brought up to date
    (new releases add places to back up), even when only another part of the server
    was set up. Nothing happens when host snapshots are not installed."""
    target = target or PRIVATE
    if not target.is_file():
        return False
    try:
        wanted = expected(config, target)
    except (KeyError, RuntimeError) as exc:
        log(f'Host snapshots: list not refreshed ({exc})')
        return False
    return refresh_settings(target, wanted, log, 'Host snapshots')


def definitions(config, binary, private):
    time = settings(config).get('time', '05:15')
    resume = f'{binary} host-resume --config {private}'
    mounts = f'RequiresMountsFor={config["data_mount"]} {config["media_mount"]}\n'
    # A night missed while the PC was off is caught up at boot, when the resume service
    # runs too and holds the same job lock: the backup waits for it instead of failing.
    return {
        BACKUP_UNIT: '[Unit]\nDescription=NekoServer host and user snapshot\n'+mounts+f'After={RESUME_UNIT} nekoserver-maintenance-resume.service\n'+
            '[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec=6h\n'+f'ExecStart={binary} host-snapshot --config {private}\nExecStopPost={resume}\nTimeoutStopSec={RESUME_TIMEOUT}\n',
        BACKUP_TIMER: '[Unit]\nDescription=NekoServer nightly host and user snapshot\n'+f'[Timer]\nOnCalendar=*-*-* {time}:00 {host_timezone(config)}\nPersistent=true\n[Install]\nWantedBy=timers.target\n',
        RESUME_UNIT: '[Unit]\nDescription=NekoServer recover Samba after interrupted snapshot\n'+mounts+
            f'[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec={RESUME_TIMEOUT}\n'+f'ExecStart={resume}\n[Install]\nWantedBy=multi-user.target\n',
    }


def run(context):
    settings(context.config)
    if not context.apply:
        context.log('Host snapshot timer available with --apply; Steam must be closed, extra save/share paths selected explicitly')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Host snapshots require root and runtime.backend=rust')
    verify_storage(context.config)
    try: options = expected(context.config)
    except KeyError:
        return {'status': 'needs_setup', 'reason': 'Set host_backup.user to the existing desktop user and reapply'}
    project = Path(__file__).resolve().parents[1]
    binary = native_runtime.deploy(project, context.config, Path(context.config['data_mount'])/'installer/runtime'/('v'+VERSION))
    private = safe_path(PRIVATE)
    units = definitions(context.config, binary, private)
    check_units(units, 'host backup')
    atomic_json(private, options)
    write_units(units)
    for command in (['systemctl','daemon-reload'], ['systemctl','enable','--now',RESUME_UNIT], ['systemctl','enable','--now',BACKUP_TIMER]):
        subprocess.run(command, check=True, capture_output=True, timeout=300)
    context.log('Host/Steam/Samba timer installed; no first snapshot assumed. Extra save/share folders require explicit selection; independent restore remains pending.')
