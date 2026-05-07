# Manual TrueNAS Steps

These steps are intentionally not automated by the Phase 1 installer because they are TrueNAS UI actions, hardware-destructive choices, or first-time app configuration.

## Before Running `install.sh`

- Install TrueNAS Community Edition on the boot SSD.
- Create the `tank` pool on the HDD mirror.
- Create the `apps` pool on the app SSD or app SSD mirror.
- Create these datasets:
  - `apps/appdata`
  - `apps/scripts`
  - `apps/backups`
  - `apps/transcode`
  - `apps/downloads-incomplete`
  - `tank/data`
  - `tank/photos`
  - `tank/realdebrid`
  - `tank/backups`
- Set download dataset security:
  - `apps/downloads-incomplete`: exec off, setuid off, devices off.
  - `tank/data`: exec off, setuid off, devices off.

## After Running `install.sh`

Edit `/mnt/apps/scripts/config.env` and fill in:

- `TZ`
- `RENDER_GID`
- `TS_AUTHKEY`
- `QBIT_PASSWORD`
- `POSTGRES_PASSWORD`
- `DB_PASSWORD`
- `USB_UUID`, if using USB photo backup
- `WEBHOOK_URL`, if using alerts

Deploy `/mnt/apps/scripts/docker-compose.yml` through TrueNAS Apps:

1. Apps > Discover Apps.
2. Three-dot menu > Install via YAML.
3. Name: `media-stack`.
4. Paste the generated compose file.
5. Save and wait for containers to start.

## Snapshot Tasks

Create these in Data Protection > Periodic Snapshot Tasks:

| Dataset | Recursive | Schedule | Retention |
| --- | --- | --- | --- |
| `apps/appdata` | Yes | Every 4 hours | 7 days |
| `apps/scripts` | Yes | Daily at 00:30 | 14 days |
| `tank/photos` | Yes | Daily at 01:00 | 30 days |
| `tank/data` | Yes | Daily at 01:30 | 14 days |
| `tank/backups` | Yes | Daily at 02:00 | 30 days |

## Cron Jobs

Create these in System Settings > Advanced Settings > Cron Jobs:

| Description | Command | Schedule |
| --- | --- | --- |
| Health check | `bash /mnt/apps/scripts/health-check.sh` | `*/10 * * * *` |
| Backup app configs | `bash /mnt/apps/scripts/backup-app-config.sh` | `0 3 * * *` |
| Cleanup downloads | `bash /mnt/apps/scripts/cleanup-downloads.sh` | `0 4 * * *` |
| Photo USB backup | `bash /mnt/apps/scripts/photo-backup-usb.sh` | `0 6 * * *` |

Only add this job if Phase 1 was installed with `--with clamav`:

| Description | Command | Schedule |
| --- | --- | --- |
| Virus scan | `bash /mnt/apps/scripts/scan-downloads.sh` | `30 4 * * *` |

## First-Time App Setup

Use `guide.md` for the full click-through setup for:

- qBittorrent paths and categories.
- Prowlarr indexers.
- Sonarr, Radarr, and Lidarr download client and root folder setup.
- Bazarr providers and languages.
- Jellyfin libraries and VAAPI transcoding.
- Navidrome users.
- Immich users and phone backup.
- Seerr Jellyfin, Radarr, and Sonarr connections.
