"""Characterization ("golden-file") tests of everything setup writes for systemd and for
the runtime: for a few fixed configurations, every unit, timer, drop-in and per-job
settings file is rendered through the modules' own functions (no root, no systemctl)
and compared byte for byte with the file kept under tests/golden/<configuration>/.

A difference is shown as a unified diff. When a change is meant (a unit gains a line, a
release bump changes the paths), the expected files are written again on purpose:

    NEKO_UPDATE_GOLDEN=1 python3 -m unittest tests.test_golden_units

tests/golden/README.md says what each file stands for and which path it is written to.
"""

import difflib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gateway_tools  # noqa: E402
import host  # noqa: E402
import native_runtime  # noqa: E402
from modules import (  # noqa: E402
    app_backup,
    dashboard,
    docker,
    heartbeat,
    host_backup,
    maintenance,
    media_monitor,
    music_backup,
    notifications,
    photo_backup,
    photo_preservation,
    server_os,
)
from neko_common import VERSION, atomic_json, media_apps_wanted, media_services  # noqa: E402

GOLDEN = ROOT / 'tests' / 'golden'
UPDATE = os.environ.get('NEKO_UPDATE_GOLDEN') == '1'

# The drives of the fixed machine; the runtime's settings files and the drive-return
# unit name them, so they are part of what is frozen.
SSD_UUID = '1a2b3c4d-0001-4000-8000-00000000aaaa'
HDD_UUID = '1a2b3c4d-0002-4000-8000-00000000bbbb'
USB_UUID = '1a2b3c4d-0003-4000-8000-00000000cccc'
# Where each step keeps its copy of the runtime (modules/*.py, gateway_tools.py).
RELEASE = 'v' + VERSION


def shipped():
    return json.loads((ROOT / 'config.json').read_text())


def base():
    """The shipped settings on a fixed machine, with every scheduled job switched on so
    that each producer renders (the shipped file leaves the optional ones off)."""
    cfg = shipped()
    cfg['timezone'] = 'UTC'
    cfg['storage'] = {
        'data_ssd': {'uuid': SSD_UUID, 'fstype': 'ext4'},
        'media_hdd': {'uuid': HDD_UUID, 'fstype': 'ext4'},
    }
    cfg['photos'].update(deploy_immich=True, external_backup_uuid=USB_UUID, external_backup_fstype='ext4')
    cfg['monitoring']['enabled'] = True
    cfg['music']['backup_enabled'] = True
    cfg['gateway'].update(lan_ip='192.168.1.20', lan_cidr='192.168.1.0/24')
    return cfg


def defaults():
    """(a) Everything on the SSD, a USB drive, the shipped backup_times."""
    return base()


def media_drive():
    """(b) Photos, music and Nextcloud's files on the media drive, no USB drive, the SSD
    and HDD on a NAS, lighting and fan control off, a second administrator's choices."""
    cfg = base()
    cfg['locations'] = {'photos': 'media', 'music': 'media', 'cloud': 'media'}
    cfg['photos'].update(external_backup_uuid='')
    cfg['server_os'].update(hdd_policy='keep_spinning', hdd_idle_minutes=120, journal_disk_mb=1024)
    cfg['dashboard'].update(fan_control=False, rgb=False, port=8790, interval_seconds=120, smart_interval_seconds=3600)
    cfg['host_backup'].update(steam=False, extra_paths=['/srv/nekoserver/notes'], keep_snapshots=14, minimum_free_gb=4)
    cfg['app_backup']['keep_snapshots'] = 0
    cfg['maintenance'].update(keep_snapshots=0, update_day='Sat', nekoserver_channel='stable', settle_days=3)
    cfg['network_storage'] = {
        'wanted': True,
        'portal': '192.168.1.50:3260',
        'targets': ['iqn.2000-01.com.synology:nas.Target-1.4a6f9c'],
    }
    cfg['heartbeat']['interval_seconds'] = 600
    cfg['photos']['database_keep'] = 8
    return cfg


def before_0480():
    """(c) A settings file from before 0.48.0: no backup_times, each job's own time, the
    old daily photo schedule, the HDD left alone."""
    cfg = base()
    del cfg['backup_times']
    cfg['app_backup']['time'] = '01:00'
    cfg['host_backup']['time'] = '02:00'
    cfg['music']['backup_time'] = '00:30'
    cfg['photos'].update(
        backup_weekday='Mon',
        backup_time='05:00',
        database_backup_time='04:30',
        backup_schedule='legacy',
        archive_interval_minutes=360,
    )
    cfg['maintenance']['time'] = '03:00'
    cfg['server_os']['hdd_policy'] = 'unchanged'
    return cfg


def past_midnight():
    """(d) backup_times that cross midnight: the weekly USB copy Sun 00:20 (so the Immich
    database copy lands on Sat 23:35), the nightly jobs around 23:30 (the host snapshot
    at 00:30), the monthly check at 05:45 just before night_end_hour 6, an exFAT drive."""
    cfg = base()
    cfg['timezone'] = 'Asia/Jerusalem'
    cfg['backup_times'] = {'daily': '23:30', 'weekly_day': 'Sun', 'weekly': '00:20', 'monthly': '05:45'}
    cfg['photos']['external_backup_fstype'] = 'exfat'
    return cfg


def no_media():
    """(e) "Install the media apps too?" answered no (0.50.0): no Jellyfin, Seerr, the apps
    that find and download, Immich, Navidrome, hardware video, media monitor or TV mode;
    Nextcloud, the dashboard, the backups and the USB copy of Nextcloud's files stay."""
    cfg = base()
    cfg['media_apps'] = {'wanted': False}
    return cfg


# Each configuration, with the facts about the machine that are not settings (the
# Tailscale name the dashboard answers, learned from the installed unit).
CONFIGURATIONS = {
    'defaults': (defaults, {}),
    'media-drive': (media_drive, {'public_host': 'neko.tail1234.ts.net'}),
    'before-0.48.0': (before_0480, {}),
    'past-midnight': (past_midnight, {}),
    'no-media': (no_media, {}),
}


def systemd_escape(path, suffix):
    """systemd-escape --path --suffix=<suffix>, for the fixed paths used here."""
    body = '-'.join(
        ''.join(c if c.isalnum() or c in '_.' else f'\\x{ord(c):02x}' for c in part)
        for part in path.strip('/').split('/')
    )
    return body + '.' + suffix


def escaping_run(args, **_):
    """A stand-in for subprocess.run that answers systemd-escape only."""
    assert args[:2] == ['systemd-escape', '--path'], args
    return mock.Mock(stdout=systemd_escape(args[3], args[2].split('=', 1)[1]) + '\n', returncode=0)


def escaping_call(args, timeout=120):
    return escaping_run(args).stdout.strip()


class Ctx:
    def __init__(self, config):
        self.config, self.apply, self.logs = config, True, []

    def log(self, message):
        self.logs.append(message)


def as_json(tmp, payload):
    """A settings file's bytes exactly as setup writes them (atomic_json)."""
    target = Path(tmp) / 'settings.json'
    atomic_json(target, payload)
    return target.read_text()


def dropin_name(path):
    """A drop-in's golden name: its folder and file (three are all 90-nekoserver.conf)."""
    path = Path(path)
    return path.parent.name + '-' + path.name


def host_guard(cfg):
    """host_backup.guard on a fixed machine: one desktop account, Arch's settings files,
    no stray units of an earlier install."""
    user = mock.Mock(pw_name='neko', pw_uid=1000, pw_dir='/home/neko')
    steam = [Path('/home/neko/.local/share/Steam') / n for n in ('config', 'userdata', 'steamapps/compatdata')]
    with (
        mock.patch.object(host_backup, 'choose_user', return_value=user),
        mock.patch.object(host_backup, 'owned_home', return_value=Path('/home/neko')),
        mock.patch.object(host_backup, 'steam_paths', return_value=steam),
        mock.patch.object(host, 'family_or_default', return_value=host.Arch),
        mock.patch.object(Path, 'glob', return_value=iter([])),
    ):
        return host_backup.guard(cfg)


def photo_jobs(cfg, runtime, tmp):
    """What photo_preservation.run writes: the weekly (or old daily) jobs and Immich's
    resume service, as files in a stand-in systemd folder."""
    units = Path(tmp) / 'photo-units'
    units.mkdir()
    with (
        mock.patch.object(photo_preservation.os, 'geteuid', return_value=0),
        mock.patch.object(photo_preservation, 'require_mounts'),
        mock.patch.object(photo_preservation, 'forget_last_run'),
        mock.patch.object(photo_preservation, 'SYSTEMD_UNITS', units),
        mock.patch.object(photo_preservation.subprocess, 'run'),
        mock.patch('modules._schedules.retire'),
        mock.patch.object(native_runtime, 'deploy', return_value=runtime),
        mock.patch.object(native_runtime, 'backup_guard', return_value=Path('/etc/nekoserver-backups/storage.json')),
    ):
        photo_preservation.run(Ctx(cfg))
    return {p.name: p.read_text() for p in sorted(units.iterdir())}


def usb_copy(cfg, runtime, guard, tmp):
    """What photo_backup.install_units writes for the weekly USB copy (nothing without a
    USB drive, or with nothing to copy), as files in a stand-in systemd folder."""
    photos, cloud = photo_backup.copies(cfg)
    uuid = cfg['photos'].get('external_backup_uuid', '')
    if not uuid or not (photos or cloud):
        return {}
    source = photo_backup.archive_source(cfg) if photos else None
    cmd = photo_backup.command_line(
        cfg, runtime, guard, uuid, cfg['photos']['external_backup_fstype'], source, photo_backup.destination(cfg)
    )
    day, when = photo_backup.schedule(cfg)
    units = Path(tmp) / 'usb-units'
    units.mkdir()
    with (
        mock.patch.object(photo_backup, 'forget_last_run'),
        mock.patch.object(photo_backup.subprocess, 'run'),
    ):
        photo_backup.install_units(
            cfg, cmd, photos, day, when, units / photo_backup.SERVICE_UNIT.name, units / photo_backup.TIMER_UNIT.name
        )
    return {p.name: p.read_text() for p in sorted(units.iterdir())}


def media_audit(cfg, runtime, tmp):
    """What media_monitor.run writes: its two units and the settings copy the audit reads."""
    written = {}
    with (
        mock.patch.object(media_monitor.os, 'geteuid', return_value=0),
        mock.patch.object(media_monitor.os.path, 'ismount', return_value=True),
        mock.patch.object(Path, 'is_dir', return_value=True),
        mock.patch.object(media_monitor.shutil, 'which', return_value='/usr/bin/systemctl'),
        mock.patch.object(native_runtime, 'payload', return_value=b''),
        mock.patch.object(native_runtime, 'deploy', return_value=runtime),
        mock.patch.object(media_monitor, 'check_units'),
        mock.patch.object(media_monitor, 'atomic_json', lambda path, payload: written.update(settings=payload)),
        mock.patch.object(media_monitor, 'write_units', written.update),
        mock.patch.object(media_monitor.subprocess, 'run'),
    ):
        media_monitor.run(Ctx(cfg))
    settings = written.pop('settings')
    written['media-monitor-config.json'] = as_json(tmp, settings)
    return written


def rendered(cfg, facts, tmp):
    """Every file setup produces for this configuration: {golden name: text}."""
    data = Path(cfg['data_mount'])
    backups = Path('/etc/nekoserver-backups')
    storage_guard = backups / 'storage.json'
    runtime = data / 'installer/runtime' / RELEASE / native_runtime.NAME
    out = {}

    def lib(step):
        return Path('/usr/local/lib/nekoserver-' + step) / RELEASE / native_runtime.NAME

    # The nightly snapshots and their settings files (the media apps' and the music's only
    # with the media apps: left out, those steps retire their schedules).
    if media_services(cfg):
        out.update(app_backup.definitions(cfg, runtime, app_backup.GUARD))
        out['applications.json'] = as_json(tmp, app_backup.guard_config(cfg))
    out.update(host_backup.definitions(cfg, runtime, host_backup.PRIVATE))
    out['host.json'] = as_json(tmp, host_guard(cfg))
    if media_apps_wanted(cfg):
        out.update(music_backup.definitions(cfg, runtime, storage_guard))
    out['backups-storage.json'] = as_json(tmp, native_runtime.storage_config(cfg))
    # The monthly check, the weekly updates, Update now, the upgrade and their recovery.
    out.update(maintenance.definitions(cfg, runtime, maintenance.GUARD))
    out['maintenance.json'] = as_json(tmp, maintenance.guard_settings(cfg))
    # The photo jobs and the weekly USB copy.
    out.update(photo_jobs(cfg, runtime, tmp))
    out.update(usb_copy(cfg, runtime, storage_guard, tmp))
    # The media audit (retired with the media apps left out).
    if media_apps_wanted(cfg):
        out.update(media_audit(cfg, runtime, tmp))
    # The heartbeat and the phone alerts (their private files hold what you typed: not here).
    out.update(heartbeat.definitions(cfg, lib('heartbeat'), Path(heartbeat.PRIVATE)))
    out.update(notifications.definitions(lib('notifications'), Path(notifications.PRIVATE_CONFIG)))
    # The server OS: the sleep, login and journal drop-ins (no administrator limits of
    # their own on this machine), drive return, the NAS check, the HDD policy.
    policy, _ = server_os.settings(cfg)
    for path, text in server_os.files(cfg, journald=Path(tmp) / 'no-journald.conf').items():
        out[dropin_name(path)] = text
    out[server_os.DRIVE_UNIT] = server_os.drive_unit(lib('os'), server_os.drive_devices(cfg, run=escaping_run))
    out['os-drives.json'] = as_json(tmp, native_runtime.storage_config(cfg))
    if server_os.on_a_nas(cfg):
        out.update(server_os.nas_watch_units(lib('os')))
    if policy != 'unchanged':
        out[server_os.HDD_UNIT] = server_os.unit(lib('os'), Path('/etc/nekoserver-os/storage.json'), cfg)
        out['os-storage.json'] = as_json(tmp, server_os.hdd_guard(cfg))
    # The dashboard: the web service and its socket, the collector, the control service,
    # lighting (when on) and the collector's settings.
    port, interval, smart = dashboard.options(cfg)
    code = Path('/usr/local/lib/nekoserver-dashboard') / RELEASE
    control, lighting = dashboard.control_options(cfg)
    out.update(
        dashboard.units(data, code, dashboard.COLLECTOR_SETTINGS, port, interval, facts.get('public_host'), True)
    )
    out.update(dashboard.control_units(code, lighting, control, data, proxyd=dashboard.PROXYD_PLACES[0]))
    out['dashboard-config.json'] = as_json(tmp, dashboard.collector_settings(cfg, smart, interval))
    # The LAN gateway's service.
    out['nekoserver-gateway-lan.service'] = gateway_tools.unit(
        cfg, Path('/usr/local/lib/nekoserver-gateway') / RELEASE, data / 'gateway/caddy-state'
    )
    # Docker's storage guard drop-in.
    with mock.patch.object(docker, 'call', escaping_call):
        out['docker.service.d-10-nekoserver-storage.conf'] = docker.guard_contents(
            cfg, data / 'installer' / 'guards' / RELEASE
        )
    return out


def diff(name, file, expected, produced):
    return ''.join(
        difflib.unified_diff(
            expected.splitlines(keepends=True),
            produced.splitlines(keepends=True),
            fromfile=f'tests/golden/{name}/{file} (expected)',
            tofile=f'{file} (produced now)',
        )
    )


class GoldenFileTests(unittest.TestCase):
    """One test per configuration: every produced file equals its expected file, no
    expected file is left without a producer, and no produced file lacks an expected one."""

    maxDiff = None

    def check(self, name):
        make, facts = CONFIGURATIONS[name]
        with tempfile.TemporaryDirectory() as tmp:
            produced = rendered(make(), facts, tmp)
        folder = GOLDEN / name
        if UPDATE:
            folder.mkdir(parents=True, exist_ok=True)
            for stale in folder.iterdir():
                if stale.name not in produced:
                    stale.unlink()
            for file, text in produced.items():
                (folder / file).write_bytes(text.encode())
        expected = {p.name: p.read_bytes().decode() for p in folder.iterdir() if p.is_file()} if folder.is_dir() else {}
        problems = []
        for file in sorted(set(expected) | set(produced)):
            if file not in produced:
                problems.append(f'{name}/{file}: an expected file is kept, but setup no longer produces it')
            elif file not in expected:
                problems.append(f'{name}/{file}: produced, but there is no expected file for it yet')
            elif expected[file] != produced[file]:
                problems.append(diff(name, file, expected[file], produced[file]))
        if problems:
            self.fail(
                f'{len(problems)} golden file(s) of the {name} configuration differ (the expected files name '
                f'release {RELEASE}: a version bump changes every path in them). When the change is meant, run  '
                'NEKO_UPDATE_GOLDEN=1 python3 -m unittest tests.test_golden_units  and commit tests/golden/.\n\n'
                + '\n'.join(problems)
            )
        return produced

    def test_defaults(self):
        produced = self.check('defaults')
        # Every producer is represented: the units that run the runtime name this release's copy.
        self.assertIn('nekoserver-photo-backup.timer', produced)
        self.assertIn(dashboard.OPENRGB, produced)  # lighting on
        self.assertNotIn(server_os.NAS_WATCH, produced)  # no NAS
        for file, text in produced.items():
            if 'ExecStart=' in text and 'nekoserver-runtime' in text:
                self.assertIn(RELEASE, text, file)

    def test_media_drive(self):
        produced = self.check('media-drive')
        self.assertNotIn('nekoserver-photo-backup.timer', produced)  # no USB drive
        self.assertNotIn(dashboard.OPENRGB, produced)  # lighting off
        self.assertIn(server_os.NAS_WATCH, produced)
        self.assertIn('--public-host neko.tail1234.ts.net', produced[dashboard.SERVICE])

    def test_before_0480(self):
        produced = self.check('before-0.48.0')
        self.assertIn('nekoserver-photo-preservation.timer', produced)  # the old daily archive pass
        self.assertNotIn(server_os.HDD_UNIT, produced)  # hdd_policy unchanged
        self.assertIn('OnCalendar=*-*-* 01:00:00 UTC', produced['nekoserver-app-backup.timer'])

    def test_no_media(self):
        produced = self.check('no-media')
        for left_out in (
            'nekoserver-app-backup.timer',
            'nekoserver-music-backup.timer',
            'nekoserver-media-health.timer',
            'nekoserver-photo-preservation.timer',
            'nekoserver-immich-db-backup.timer',
        ):
            self.assertNotIn(left_out, produced)
        # Nextcloud's files still get their weekly USB copy, without a photo source.
        self.assertNotIn('--source', produced['nekoserver-photo-backup.service'])
        self.assertIn('nekoserver-host-backup.timer', produced)
        collector = json.loads(produced['dashboard-config.json'])
        self.assertEqual(collector['media'], {'services': []})
        self.assertEqual(collector['media_apps'], {'wanted': False})
        self.assertIs(collector['photos']['deploy_immich'], False)
        maintenance_settings = json.loads(produced['maintenance.json'])
        self.assertEqual(maintenance_settings['media_apps'], {'wanted': False})

    def test_past_midnight(self):
        produced = self.check('past-midnight')
        self.assertIn('OnCalendar=Sat *-*-* 23:35:00 Asia/Jerusalem', produced['nekoserver-immich-db-backup.timer'])
        self.assertIn('OnCalendar=Sun *-*-* 00:20:00 Asia/Jerusalem', produced['nekoserver-photo-backup.timer'])
        self.assertIn('OnCalendar=*-*-* 00:30:00 Asia/Jerusalem', produced['nekoserver-host-backup.timer'])
        self.assertIn('OnCalendar=*-*-01 05:45:00 Asia/Jerusalem', produced['nekoserver-maintenance.timer'])

    def test_every_golden_folder_belongs_to_a_configuration(self):
        if UPDATE:  # a generating run makes every folder first (this test may run before the others)
            for name in CONFIGURATIONS:
                self.check(name)
        folders = sorted(p.name for p in GOLDEN.iterdir() if p.is_dir())
        self.assertEqual(folders, sorted(CONFIGURATIONS))

    def test_the_expected_files_name_the_current_release(self):
        """A version bump changes the paths in them: the README says to regenerate."""
        stale = sorted(
            f'{p.parent.name}/{p.name}'
            for p in GOLDEN.glob('*/*')
            if p.is_file() and 'nekoserver-runtime' in p.read_text() and RELEASE not in p.read_text()
        )
        self.assertEqual(stale, [], 'regenerate the golden files for this release (see tests/golden/README.md)')


if __name__ == '__main__':
    unittest.main()
