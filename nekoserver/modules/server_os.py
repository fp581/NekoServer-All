"""Install OS-wide no-sleep policy, bounded journal and guarded HDD boot policy."""

import os
import re
from pathlib import Path
import shutil
import subprocess

import native_runtime
import host
from neko_common import (
    SYSTEMD_UNITS,
    VERSION,
    atomic_json,
    atomic_write,
    check_units,
    safe_path,
    verify_storage,
    write_units,
)

HDD_UNIT = 'nekoserver-hdd-policy.service'
# Brings the apps back when the SSD or HDD returns after a drop-out (see runtime drive_return.rs).
DRIVE_UNIT = 'nekoserver-drive-return.service'
DRIVE_SETTINGS = Path('/etc/nekoserver-os/drives.json')

DEPENDENCIES = ['storage', 'recovery']
DESCRIPTION = 'Disable OS sleep, bound system logs and configure verified media HDD standby'
# Sandboxing for these root services (systemd.exec(5)) that needs no mount namespace of
# their own: drive return releases a dead mount itself (umount), which in a namespace of
# its own would be released for that namespace only, and the drive never mounted again.
# Left out, as each would break a service here: ProtectSystem (the jobs write to /etc),
# ProtectClock and PrivateDevices (both close /dev: hdparm, and blkid reading a drive),
# and a smaller capability set (umount, hdparm and reading the drives need root's).
SANDBOX = (
    'NoNewPrivileges=yes\nRestrictRealtime=yes\nRestrictSUIDSGID=yes\nLockPersonality=yes\n'
    'SystemCallArchitectures=native\nUMask=0077\n'
)
# The HDD power service only reads the mounts and drives (findmnt, lsblk, blkid) and sets
# the drive's timer (hdparm): it also gets a /tmp of its own, no home folders, and the
# kernel's settings and control groups read-only.
SANDBOX_NAMESPACED = SANDBOX + 'PrivateTmp=yes\nProtectHome=yes\nProtectKernelTunables=yes\nProtectControlGroups=yes\n'
TARGETS = ('sleep.target', 'suspend.target', 'hibernate.target', 'hybrid-sleep.target', 'suspend-then-hibernate.target')
MARKER = '# Managed by NekoServer server_os\n'


def settings(config):
    opts = config.get('server_os', {})
    if not isinstance(opts, dict):
        raise RuntimeError('server_os must be an object')
    policy = opts.get('hdd_policy', 'keep_spinning')
    if policy not in ('keep_spinning', 'idle', 'unchanged'):
        raise RuntimeError('hdd_policy must be keep_spinning, idle or unchanged')
    minutes = opts.get('hdd_idle_minutes', 60)
    if type(minutes) is not int or minutes not in range(30, 331, 30):
        raise RuntimeError('hdd_idle_minutes must be 30..330 in 30-minute steps')
    values = {}
    for key, default, low, high in (
        ('journal_disk_mb', 512, 64, 4096),
        ('journal_ram_mb', 64, 16, 256),
        ('journal_retention_days', 14, 1, 90),
    ):
        value = opts.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise RuntimeError('Invalid journal bound')
        values[key] = value
    return policy, values


def hdd_guard(config):
    policy, _ = settings(config)
    return {
        **native_runtime.storage_config(config),
        'server_os': {
            'hdd_policy': policy,
            'hdd_idle_minutes': config.get('server_os', {}).get('hdd_idle_minutes', 60),
        },
    }


# The sleep targets that were masked before NekoServer masked them: the uninstaller
# unmasks only the others, so a PC that was kept awake on purpose stays that way.
MASKS_BEFORE = Path('/etc/nekoserver-os/sleep-masks-before.json')


def masked(name, units=SYSTEMD_UNITS):
    path = Path(units) / name
    return path.is_symlink() and os.readlink(path) == '/dev/null'


def remember_masks(definitions, record=MASKS_BEFORE, units=SYSTEMD_UNITS):
    """Written once, the first time: which targets were already masked. An install from
    before this record (its own drop-ins are there already) masked them itself: none."""
    if record.exists():
        return
    ours = any(Path(name).is_file() and Path(name).read_text().startswith(MARKER) for name in definitions)
    before = [] if ours else [name for name in TARGETS if masked(name, units)]
    atomic_json(record, {'format': 'neko-sleep-masks-v1', 'masked_before': before})


JOURNALD = Path('/etc/systemd/journald.conf')
JOURNAL_DROPIN = '/etc/systemd/journald.conf.d/90-nekoserver.conf'


def files(config, journald=JOURNALD):
    _, values = settings(config)
    wanted, _ = journal_limits(values, journald)
    return {
        '/etc/systemd/sleep.conf.d/90-nekoserver.conf': MARKER
        + '[Sleep]\nAllowSuspend=no\nAllowHibernation=no\nAllowHybridSleep=no\nAllowSuspendThenHibernate=no\n',
        '/etc/systemd/logind.conf.d/90-nekoserver.conf': MARKER + '[Login]\nIdleAction=ignore\n',
        JOURNAL_DROPIN: MARKER + '[Journal]\n' + ''.join(key + '=' + value + '\n' for key, value in wanted.items()),
    }


# The journal limits NekoServer sets, and which way is stricter: a smaller disk or RAM
# limit or a shorter keeping time, but more disk kept free.
SIZE_UNITS = {'': 1, 'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4, 'P': 1024**5, 'E': 1024**6}
TIME_UNITS = {
    'us': 1e-6,
    'usec': 1e-6,
    'ms': 1e-3,
    'msec': 1e-3,
    's': 1,
    'sec': 1,
    'second': 1,
    'seconds': 1,
    'm': 60,
    'min': 60,
    'minute': 60,
    'minutes': 60,
    'h': 3600,
    'hr': 3600,
    'hour': 3600,
    'hours': 3600,
    'd': 86400,
    'day': 86400,
    'days': 86400,
    'w': 604800,
    'week': 604800,
    'weeks': 604800,
    'M': 2629800,
    'month': 2629800,
    'months': 2629800,
    'y': 31557600,
    'year': 31557600,
    'years': 31557600,
}


def size_bytes(text):
    """journald's size ("512M", "1G", "1048576") in bytes, or None when it is not one."""
    found = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([KMGTPE]?)i?B?', text.strip(), re.IGNORECASE)
    return float(found.group(1)) * SIZE_UNITS[found.group(2).upper()] if found else None


def time_seconds(text):
    """journald's time span ("14day", "2 weeks", "1h 30min", "3600") in seconds, or None."""
    parts = re.findall(r'(\d+(?:\.\d+)?)\s*([A-Za-z]*)', text)
    if not parts or re.sub(r'(\d+(?:\.\d+)?)\s*([A-Za-z]*)', '', text).strip():
        return None
    total = 0.0
    for number, unit in parts:
        if (unit or 's') not in TIME_UNITS:
            return None
        total += float(number) * TIME_UNITS[unit or 's']
    return total


def stricter(key, theirs, ours):
    """True when the administrator's value limits the journal at least as much as ours.
    0 (or a value that cannot be read) is never stricter: for keeping time it means no limit."""
    measure = time_seconds if key == 'MaxRetentionSec' else size_bytes
    mine, other = measure(ours), measure(theirs)
    if not other or mine is None:
        return False
    return other >= mine if key == 'SystemKeepFree' else other <= mine


def journald_files(journald=JOURNALD):
    """journald.conf and the administrator's drop-ins beside it (never NekoServer's own),
    in the order journald reads them: the main file first, then the drop-ins by name."""
    folder = Path(str(journald) + '.d')
    try:
        dropins = sorted(
            (p for p in folder.glob('*.conf') if p.name != Path(JOURNAL_DROPIN).name), key=lambda p: p.name
        )
    except OSError:
        dropins = []
    return [Path(journald)] + dropins


def journal_settings(path):
    """{key: value} of the [Journal] section of one journald file ({} when unreadable)."""
    try:
        lines = Path(path).read_text(errors='replace').splitlines()
    except OSError:
        return {}
    found, section = {}, ''
    for line in (line.strip() for line in lines):
        if line.startswith('['):
            section = line
        elif section == '[Journal]' and '=' in line and not line.startswith(('#', ';')):
            key, _, value = line.partition('=')
            found[key.strip()] = value.strip()
    return found


# The my-config.json setting (under "server_os") for each limit you can choose.
JOURNAL_OPTIONS = {
    'SystemMaxUse': 'journal_disk_mb',
    'RuntimeMaxUse': 'journal_ram_mb',
    'MaxRetentionSec': 'journal_retention_days',
}


def admin_journal(ours, journald=JOURNALD):
    """The administrator's own journal limits: ({key: (value, file)} that NekoServer's
    drop-in would override, {key: (value, file)} read after it, which win anyway). The
    last file journald reads a key from is the one that counts."""
    before, after = {}, {}
    for path in journald_files(journald):
        later = path.parent.name.endswith('.d') and path.name > Path(JOURNAL_DROPIN).name
        for key, theirs in journal_settings(path).items():
            if key in ours:
                (after if later else before)[key] = (theirs, path)
    return before, after


def journal_limits(values, journald=JOURNALD):
    """(the limits NekoServer's drop-in sets, what to say about the administrator's own).
    A drop-in wins over journald.conf, so a limit set there would be overridden silently:
    a stricter one is kept (NekoServer leaves that key out), a looser one is replaced and
    said. A drop-in of yours named after 90-nekoserver.conf wins anyway, and is said too."""
    ours = {
        'SystemMaxUse': f'{values["journal_disk_mb"]}M',
        'RuntimeMaxUse': f'{values["journal_ram_mb"]}M',
        'MaxRetentionSec': f'{values["journal_retention_days"]}day',
        'SystemKeepFree': '1G',
    }
    wanted, notes = dict(ours), []
    before, after = admin_journal(ours, journald)
    for key, (theirs, path) in before.items():
        setting = 'your ' + key + '=' + theirs + ' (' + str(path) + ')'
        if theirs == ours[key]:
            continue
        if stricter(key, theirs, ours[key]):
            del wanted[key]
            notes.append('Journal: ' + setting + " is stricter than NekoServer's " + ours[key] + ', so yours is kept')
        else:
            hint = (
                ('; to keep yours, set "' + JOURNAL_OPTIONS[key] + '" under "server_os" in my-config.json')
                if key in JOURNAL_OPTIONS
                else ''
            )
            notes.append(
                'Journal: '
                + setting
                + " is replaced by NekoServer's "
                + ours[key]
                + ' (its drop-in wins over journald.conf)'
                + hint
            )
    for key, (theirs, path) in after.items():
        if theirs != ours[key]:
            notes.append(
                'Journal: your '
                + key
                + '='
                + theirs
                + ' ('
                + str(path)
                + ") is read after NekoServer's drop-in, so it wins over "
                + ours[key]
            )
    return wanted, notes


def log_journal_limits(config, log, journald=JOURNALD):
    for note in journal_limits(settings(config)[1], journald)[1]:
        log(note)


def drive_devices(config, run=subprocess.run):
    """systemd's names for the SSD's and HDD's devices, by their IDs."""
    names = []
    for key in ('data_ssd', 'media_hdd'):
        uuid = config['storage'][key]['uuid']
        shown = run(
            ['systemd-escape', '--path', '--suffix=device', '/dev/disk/by-uuid/' + uuid],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
        names.append(shown)
    return names


def drive_unit(binary, devices):
    """Run whenever either drive appears (at boot too, when there is nothing to do), after
    the drives have been mounted at boot: the drives mounted again, Docker and the shares
    started, then each job a missing drive cut off finished (drive_return.rs)."""
    return (
        '[Unit]\nDescription=NekoServer bring the apps back when a drive returns\n'
        'After=local-fs.target\n'
        '[Service]\nType=oneshot\n'
        f'ExecStart={binary} drive-return --config {DRIVE_SETTINGS}\n'
        # It waits up to 10 minutes for a drive, then finishes any job the drive cut off
        # (a monthly update's rollback included), which must never be cut off itself.
        'TimeoutStartSec=infinity\n' + SANDBOX + '[Install]\nWantedBy=' + ' '.join(devices) + '\n'
    )


def install_drive_return(config, project, run=subprocess.run):
    binary = native_runtime.deploy(project, config, Path('/usr/local/lib/nekoserver-os') / ('v' + VERSION))
    check_units([DRIVE_UNIT], 'Drive return')
    atomic_json(safe_path(DRIVE_SETTINGS), native_runtime.storage_config(config))
    write_units({DRIVE_UNIT: drive_unit(binary, drive_devices(config, run))})
    run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    # Re-enabled each run: the drives' IDs (and so the devices it waits for) may change.
    run(['systemctl', 'reenable', DRIVE_UNIT], check=True, capture_output=True, timeout=30)


def unit(binary, guard, config):
    return (
        '[Unit]\nDescription=NekoServer verified media HDD standby policy\n'
        f'RequiresMountsFor={config["data_mount"]} {config["media_mount"]}\n'
        '[Service]\nType=oneshot\nRemainAfterExit=yes\n'
        f'ExecStart={binary} hdd-policy --config {guard}\nTimeoutStartSec=180\n'
        + SANDBOX_NAMESPACED
        + '[Install]\nWantedBy=multi-user.target\n'
    )


def run(context):
    policy, _ = settings(context.config)
    definitions = files(context.config)
    if not context.apply:
        context.log(
            'Server OS: disable suspend/hibernate, cap journal; HDD policy='
            + policy
            + '. Linux memory cache stays managed by the kernel.'
        )
        return
    if os.geteuid() != 0 or not shutil.which('systemctl'):
        raise RuntimeError('Server OS setup requires root and systemd')
    for name in definitions:
        path = safe_path(name)
        if path.exists() and not path.read_text().startswith(MARKER):
            raise RuntimeError('Unmanaged OS drop-in preserved')
    # Masks are deliberate symlinks to /dev/null; inspect their parents separately.
    for name in TARGETS:
        path = SYSTEMD_UNITS / name
        safe_path(path.parent)
        if path.is_symlink():
            if os.readlink(path) != '/dev/null':
                raise RuntimeError('Custom sleep target link preserved')
        elif path.exists():
            raise RuntimeError('Custom sleep target unit preserved')
    remember_masks(definitions)
    log_journal_limits(context.config, context.log)
    for name, content in definitions.items():
        atomic_write(Path(name), content, 0o644)
    subprocess.run(['systemctl', 'mask', *TARGETS], check=True, capture_output=True, timeout=30)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    subprocess.run(['systemctl', 'restart', 'systemd-journald.service'], check=True, capture_output=True, timeout=30)
    # Existing logind sessions are retained. Masks/sleep policy apply immediately;
    # IdleAction is also loaded on the next normal reboot.
    context.log('OS sleep/hibernation blocked; journal bounds installed. Display blanking may remain enabled.')
    install_drive_return(context.config, Path(__file__).resolve().parents[1])
    context.log(
        'Drive return: when the SSD or HDD comes back after it disappeared, its mount is renewed and the apps start again by themselves'
    )
    if policy == 'unchanged':
        previous = safe_path(SYSTEMD_UNITS / HDD_UNIT)
        if previous.exists():
            if 'Description=NekoServer verified media HDD standby policy\n' not in previous.read_text():
                raise RuntimeError('Unmanaged HDD unit preserved')
            subprocess.run(['systemctl', 'disable', '--now', HDD_UNIT], check=True, capture_output=True, timeout=30)
        context.log(
            'No HDD command issued; earlier managed boot policy disabled. Restore previously changed hardware settings explicitly if needed.'
        )
        return
    verify_storage(context.config)
    if not shutil.which('hdparm'):
        family = host.current()
        if family.available('hdparm'):
            family.install(['hdparm'])
    if not shutil.which('hdparm'):
        return {
            'status': 'needs_setup',
            'reason': 'No-sleep/log limits installed; install hdparm then reapply server_os for the SATA HDD policy',
        }
    binary = native_runtime.deploy(
        Path(__file__).resolve().parents[1], context.config, Path('/usr/local/lib/nekoserver-os') / ('v' + VERSION)
    )
    guard = safe_path('/etc/nekoserver-os/storage.json')
    check_units([HDD_UNIT], 'HDD policy')
    atomic_json(guard, hdd_guard(context.config))
    write_units({HDD_UNIT: unit(binary, guard, context.config)})
    subprocess.run(['systemctl', 'daemon-reload'], check=True, capture_output=True, timeout=30)
    result = subprocess.run(['systemctl', 'enable', HDD_UNIT], capture_output=True, timeout=190, check=False)
    if result.returncode:
        return {
            'status': 'needs_setup',
            'reason': 'No-sleep/log limits installed; HDD command failed or hardware is unsupported. Inspect the policy service; no blanket drive setting was used',
        }
    repeated = subprocess.run(['systemctl', 'restart', HDD_UNIT], capture_output=True, timeout=190, check=False)
    if repeated.returncode:
        return {
            'status': 'needs_setup',
            'reason': 'The HDD power-saving service did not start: '
            + (hdd_service_said('runtime failed') or 'no reason recorded')
            + '. Setup goes on without it (the drive keeps its own power settings); the log is  sudo journalctl -u '
            + HDD_UNIT,
        }
    # What the service did: the timer it set, or why the drive keeps its own settings
    # (in a USB case, an SSD, encrypted): nothing for anyone to do then.
    said = hdd_service_said('Drive power settings:')
    context.log('HDD power settings: ' + said if said else 'Verified SATA media HDD policy applied at boot: ' + policy)


def hdd_service_said(marker, run=subprocess.run):
    """The HDD policy service's own last line with ``marker`` in its log, after the marker
    ('' if there is none): why it failed, or what it set."""
    try:
        shown = run(
            ['journalctl', '-u', HDD_UNIT, '-n', '20', '-o', 'cat', '--no-pager'],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        shown = ''
    lines = [line.strip() for line in shown.splitlines() if marker in line]
    return lines[-1].split(marker, 1)[-1].lstrip(' :').rstrip('.') if lines else ''
