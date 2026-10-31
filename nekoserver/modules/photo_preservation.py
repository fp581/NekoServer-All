"""Install combined weekly photo preservation, retiring old frequent archive timers."""

import native_runtime
import os, subprocess
from neko_common import RESUME_TIMEOUT, VERSION, SYSTEMD_UNITS
from pathlib import Path
from neko_common import safe_path, relative_path, atomic_write, host_timezone, forget_last_run

DEPENDENCIES = ['photos']
DESCRIPTION = 'Weekly additive archival of SSD Immich originals and database backup'


def command_line(binary, guard, data, media, age, archive, keep):
    """The runtime command the timer runs (tests check it against the real program)."""
    return f'{binary} preservation --config {guard} --data {data} --media {media} --min-age {age} --archive-dir {archive} --db-keep {keep}'


# The weekly run pauses Immich for its last photos and the database copy (backup.rs
# paused_backup). Its resume starts Immich again: when the run ends in any way
# (ExecStopPost), and at boot after a power cut.
RESUME = 'nekoserver-immich-resume'


def resume_command(binary, guard):
    return f'{binary} immich-resume --config {guard}'


def resume_unit(binary, guard, data):
    """Immich lives on the SSD only, so its resume needs only that drive (not the HDD):
    a HDD that dropped out mid-run must not keep Immich stopped. The drive-return
    service runs it again once a missing drive is back."""
    return (
        f'[Unit]\nDescription={RESUME}\nRequiresMountsFor={data}\nAfter=docker.service\nRequires=docker.service\n'
        f'[Service]\nType=oneshot\nUMask=0077\nTimeoutStartSec={RESUME_TIMEOUT}\nExecStart={resume_command(binary, guard)}\n'
        '[Install]\nWantedBy=multi-user.target\n'
    )


# The job that makes the Immich database copy (and pauses Immich for it).
DB_BACKUP = ' --db-backup'


def service_text(name, cmd, data, media, binary, guard):
    """A job's unit. The one that makes the database copy pauses Immich: whenever it ends
    (finished, failed, stopped or cut off), its ExecStopPost starts Immich again."""
    text = f'[Unit]\nDescription={name}\nRequiresMountsFor={data} {media}\n[Service]\nType=oneshot\nExecStart={cmd}\n'
    if cmd.endswith(DB_BACKUP):
        text += f'ExecStopPost={resume_command(binary, guard)}\nTimeoutStopSec={RESUME_TIMEOUT}\n'
    return text


def install_resume(binary, guard, data):
    resume = SYSTEMD_UNITS / (RESUME + SERVICE)
    text = resume_unit(binary, guard, data)
    if resume.exists() and f'Description={RESUME}' not in resume.read_text():
        raise RuntimeError(f'Unmanaged existing unit: {resume}')
    safe_path(resume)
    atomic_write(resume, text, 0o644)


def require_mounts(*paths):
    for p in paths:
        if (
            p.is_symlink()
            or subprocess.run(
                ['findmnt', '-n', '--mountpoint', str(p)], stdout=subprocess.DEVNULL, check=False, timeout=30
            ).returncode
        ):
            raise RuntimeError(f'Missing mount: {p}')


# The schedules this step installs (legacy and weekly); the USB copy has its own.
TIMER = '.timer'
SERVICE = '.service'
UNITS = [
    name + suffix
    for name in ('nekoserver-photo-preservation', 'nekoserver-immich-db-backup')
    for suffix in (TIMER, SERVICE)
] + [RESUME + SERVICE]


def switch_off(ctx, units=None, run=subprocess.run):
    """No Immich, no photos to keep: no schedule. Ones an earlier setup installed are
    stopped and removed, so they don't keep running against a stopped Immich (the USB
    copy of Nextcloud files has a schedule of its own and stays)."""
    from modules._schedules import remove

    removed = remove([Path(units or SYSTEMD_UNITS) / name for name in UNITS], run, ctx.log)
    ctx.log(
        'Photo backup to the HDD: Immich is switched off, so there is nothing to keep'
        + ('; its schedules were removed' if removed else '')
    )


def run(ctx):
    if not ctx.apply:
        ctx.log('Photo preservation: apply installs the configured weekly originals/database timer')
        return
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    cfg = ctx.config
    data = Path(cfg['data_mount'])
    media = Path(cfg['media_mount'])
    opts = cfg['photos']
    if not opts.get('deploy_immich', False):
        return switch_off(ctx)
    require_mounts(data, media)
    runtime = data / 'installer' / 'runtime' / ('v' + VERSION)
    interval = opts.get('archive_interval_minutes', 360)
    age = opts.get('archive_min_age_seconds', 600)
    if not isinstance(interval, int) or interval < 15 or not isinstance(age, int) or age < 60:
        raise RuntimeError('Invalid archive interval/age')
    tm = opts.get('database_backup_time', '02:15')
    import re

    if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', tm):
        raise RuntimeError('Invalid database backup time')
    archive = relative_path(opts.get('archive_dir', 'photos/archive'))
    binary = native_runtime.deploy(Path(__file__).resolve().parents[1], cfg, runtime)
    keep = opts.get('database_keep', 4)
    if type(keep) is not int or not 0 <= keep <= 520:
        raise RuntimeError('photos.database_keep must be a whole number from 0 to 520')
    guard = native_runtime.backup_guard(cfg)
    base = command_line(binary, guard, data, media, age, archive, keep)
    schedule = opts.get('backup_schedule', 'legacy')
    if schedule not in ('legacy', 'weekly'):
        raise RuntimeError('Invalid photo backup schedule')
    day = opts.get('backup_weekday', 'Sun')
    if day not in ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'):
        raise RuntimeError('Invalid photo backup weekday')
    if schedule == 'weekly':
        from modules._schedules import retire

        retire(
            [SYSTEMD_UNITS / ('nekoserver-photo-preservation.' + suffix) for suffix in ('timer', 'service')],
            subprocess.run,
        )
    services = {
        'nekoserver-photo-preservation': (base, f'OnBootSec=10min\nOnUnitInactiveSec={interval}min\n'),
        'nekoserver-immich-db-backup': (
            base + DB_BACKUP,
            f'OnCalendar=*-*-* {tm}:00 {host_timezone(cfg)}\nPersistent=true\n',
        ),
    }
    if schedule == 'weekly':
        services = {
            'nekoserver-immich-db-backup': (
                base + DB_BACKUP,
                f'OnCalendar={day} *-*-* {tm}:00 {host_timezone(cfg)}\nPersistent=true\n',
            )
        }
    # The DB job also archives to keep an identical fail-closed precondition.
    for name, (cmd, calendar) in services.items():
        service = SYSTEMD_UNITS / (name + SERVICE)
        timer = SYSTEMD_UNITS / (name + TIMER)
        timer_text = f'[Unit]\nDescription={name} timer\n[Timer]\n{calendar}\n[Install]\nWantedBy=timers.target\n'
        for target, text in ((service, service_text(name, cmd, data, media, binary, guard)), (timer, timer_text)):
            content = text.replace('\\n', '\n')
            if target.exists() and target.read_text() != content:
                previous = target.read_text()
                # Require this unit's exact managed description before updating it.
                if f'Description={name}' not in previous:
                    raise RuntimeError(f'Unmanaged existing unit: {target}')
            safe_path(target)
            forget_last_run(target)  # a timer created again must not run every missed night at once
            atomic_write(target, content, 0o644)
    install_resume(binary, guard, data)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=120)
    subprocess.run(['systemctl', 'enable', RESUME + SERVICE], check=True, capture_output=True, text=True, timeout=120)
    # Output captured: systemd's "Created symlink" notes stay off the screen; a failure's own
    # words still reach the log (installer.failure_reason).
    for name in services:
        subprocess.run(
            ['systemctl', 'enable', '--now', name + TIMER], check=True, capture_output=True, text=True, timeout=120
        )
    ctx.log(
        'Enabled '
        + (
            'weekly combined photo originals/database backup'
            if schedule == 'weekly'
            else 'periodic archive and nightly SQL'
        )
        + '; no deletions'
    )
