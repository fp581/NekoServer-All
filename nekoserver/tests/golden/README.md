# Golden files: what setup writes, frozen

`tests/test_golden_units.py` renders, for a few fixed configurations, every systemd unit,
timer and drop-in and every per-job settings file the setup modules produce, through the
modules' own functions (no root, no `systemctl`), and compares each byte for byte with the
file kept here under `<configuration>/<name>`. A difference fails the test with a unified
diff. These are characterization tests: they say what setup writes today, so that a
cleanup that changes nothing on purpose can be shown to change nothing.

## Regenerating them on purpose

When a change to a unit or a settings file is meant (a new line, a renamed option), or
after a version bump (the paths name the release, `v0.48.1` today, so a bump changes
every unit that runs the runtime), write the expected files again and commit them:

```bash
cd nekoserver
NEKO_UPDATE_GOLDEN=1 python3 -m unittest tests.test_golden_units
```

Stale files are removed, missing ones added. Look at `git diff tests/golden` before
committing: that diff is the review of what the change does to the installed server.

## The configurations

| Folder | What it stands for |
| --- | --- |
| `defaults` | The shipped `config.json` on a fixed machine: everything on the SSD, a USB backup drive, the shipped `backup_times` (daily 04:15, weekly Sun 03:00, monthly 02:30), UTC. |
| `media-drive` | Photos, music and Nextcloud's files on the media drive (`locations`), no USB drive, the drives on a NAS (`network_storage.wanted`), HDD kept spinning, fan and lighting control off, a dashboard that also answers a Tailscale name, snapshots kept without limit. |
| `before-0.48.0` | A settings file from before 0.48.0: no `backup_times`, each job's own time (`app_backup.time`, `host_backup.time`, `music.backup_time`, `photos.backup_time`/`backup_weekday`/`database_backup_time`, `maintenance.time`), the old daily photo schedule (`backup_schedule: legacy`), `hdd_policy: unchanged`. |
| `past-midnight` | `backup_times` that cross midnight: weekly USB copy Sun 00:20 (so the Immich database copy lands on Sat 23:35), daily 23:30 (the host snapshot at 00:30), monthly 05:45 just before `night_end_hour` 6, Asia/Jerusalem, an exFAT USB drive. |
| `no-media` | The shipped `config.json` with "Install the media apps too?" answered no (`media_apps.wanted: false`, 0.50.0): no app backup, music backup, media check or Immich copy units, no `applications.json`; the photo backup copies Nextcloud alone; the dashboard and the monthly maintenance are told of no media app. |

In every configuration but `no-media` the optional jobs are switched on (`monitoring.enabled`,
`music.backup_enabled`, `photos.deploy_immich`, `nextcloud.enabled`) so that each producer
renders; the shipped file leaves them off until setup asks.

## The files

Unit files keep their own names (`nekoserver-app-backup.timer`, ...). Drop-ins are named
by their folder and file, and the settings files by where setup writes them:

| Golden name | Written to | By |
| --- | --- | --- |
| `applications.json` | `/etc/nekoserver-backups/applications.json` | `app_backup.guard_config` |
| `host.json` | `/etc/nekoserver-backups/host.json` | `host_backup.guard` (a fixed account `neko`, Arch's settings files) |
| `maintenance.json` | `/etc/nekoserver-backups/maintenance.json` | `maintenance.guard_settings` |
| `backups-storage.json` | `/etc/nekoserver-backups/storage.json` | `native_runtime.storage_config` (`backup_guard`) |
| `os-drives.json` | `/etc/nekoserver-os/drives.json` | `native_runtime.storage_config` (server_os drive return) |
| `os-storage.json` | `/etc/nekoserver-os/storage.json` | `server_os.hdd_guard` (not with `hdd_policy: unchanged`) |
| `dashboard-config.json` | `/etc/nekoserver-dashboard/config.json` | `dashboard.collector_settings` |
| `media-monitor-config.json` | `<data_mount>/scripts/config.json` | `media_monitor.run` (the whole configuration) |
| `sleep.conf.d-90-nekoserver.conf` | `/etc/systemd/sleep.conf.d/90-nekoserver.conf` | `server_os.files` |
| `logind.conf.d-90-nekoserver.conf` | `/etc/systemd/logind.conf.d/90-nekoserver.conf` | `server_os.files` |
| `journald.conf.d-90-nekoserver.conf` | `/etc/systemd/journald.conf.d/90-nekoserver.conf` | `server_os.files` (no administrator limits of their own) |
| `docker.service.d-10-nekoserver-storage.conf` | `/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf` | `docker.guard_contents` |

Machine facts that are not settings are fixed in the test: the drives' UUIDs, the
desktop account for host snapshots, `systemd-escape`'s answers for the drive devices and
mount units, systemd's socket proxy at `/usr/lib/systemd/systemd-socket-proxyd`, and the
Tailscale name of the `media-drive` dashboard.

Not frozen here, and why: the heartbeat's and the phone alerts' private files (they hold
the URL and token you typed), the gateway's `/etc/nekoserver-gateway/storage.json` and
Docker's `installer/guards/<release>/storage.json` (both composed inline in a function
that also deploys the runtime and calls `systemctl`), and the gateway's Caddy
configuration (`lan.json`) and portal page (not units or job settings).
