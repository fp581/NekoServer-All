# Storage and scheduled work — v0.18.0

| Content | Working location | Backup policy |
|---|---|---|
| Music audio and downloads | SSD `library/music` and `library/downloads/music` | No automatic file backup, including monthly snapshots |
| Music application settings/accounts/playlists | SSD appdata | Existing application-state backup policy |
| Immich photos/videos | SSD `photos/immich-library` | Weekly HDD originals archive and logical database backup; weekly external originals copy; retained monthly complete recovery snapshot |
| Movies/shows and their downloads | HDD library | No media-file backup; application state remains protected |
| Nextcloud files | HDD `cloud/files` | Existing monthly full backup policy |

Navidrome, Lidarr and the qBittorrent music category use the SSD. Local imports route music to SSD and videos to HDD. Incomplete downloads may use the configured shared temporary folder. No watched-video migration between SSD/HDD is performed.

## Weekly photo protection

The combined originals/database worker runs **Sunday at 02:15 Asia/Jerusalem**. It copies mature originals additively from SSD to HDD, verifies bytes, creates the logical database dump, and verifies database copies on SSD/HDD. Both operations use one worker and recovery lock. A failed archival step prevents a misleading successful database job. The dashboard's archive/database cards use this combined result and weekly overdue thresholds.

The external originals copy runs **Sunday at 03:00 Asia/Jerusalem**, ordered after a still-running combined local backup. The correct external disk must be connected; otherwise the run fails visibly without writing to the OS disk. All managed photo timers are weekly, with daytime catch-up disabled. A missed scheduled week waits for the next scheduled run unless started manually. Files modified in the last ten minutes are deferred for stability. New uploads can therefore wait roughly a week for an independent server copy. Monthly cold full-state snapshots and isolated recovery tests remain enabled.

The previous frequent photo archival timer is disabled/stopped on apply. The formerly daily database timer is rewritten as weekly. Immich's own internal SSD-local database backup feature is separate from these managed server-to-HDD/external copies.

## Removed recurring work

`monitoring.enabled: false` disables and stops the old NekoServer media-health timer/worker. There is no replacement every-six-hours media audit. Monthly pre/post-update connection checks and ad hoc diagnostics remain available. The dashboard continues lightweight host/container-status collection and reports routine media checks as disabled; it does not show stale old API audit failures.

`music.backup_enabled: false` disables/stops the music backup timer/worker. Monthly recovery excludes raw music and music downloads too. Application accounts/playlists/settings still receive the existing state backups. Previous backup files are preserved; no user data is deleted. The opt-in backup CLI/restore reader remain available for explicit manual use/old inventories.

The cleanup modules remain in dependency plans so applying the current full/media/music installer can retire old timers. Managed unit files are recognized before service changes; custom administrator files are preserved and reported for review. A disabled timer is checked as absent/inactive during acceptance, rather than required to be active.

## HDD and unchanged jobs

The guarded native SATA standby timer remains **60 minutes** (`hdparm -S 242`), with no PC sleep. SMART queries avoid waking a standby disk. Playing HDD videos, Nextcloud activity, torrent seeding, app-owned scans, nightly application/host-state backups, weekly photo copies and monthly maintenance can still wake it. Removing NekoServer's periodic media audit cannot prevent ordinary application activity. Native power behaviour and wake latency need testing on the target PC.

Nightly app-state/host-state backups and monthly overnight recovery/update work are unchanged. Scheduled orchestration remains Rust; Python handles installation and ad hoc recovery. No OS upgrades, automatic formatting or automatic retention deletion are added.

## Existing installations and recovery

Applying this release updates schedules and retires the recognized old timers. Existing old HDD music/Immich working layouts still require the verified, stopped-writer migration described in the prior release; different manifests or environment paths are preserved/refused. Do not switch a mount to an empty SSD folder and assume files moved.

Monthly recovery continues to cover SSD Immich originals/generated assets and its database/settings, Nextcloud data, selected app state and exact old images. Raw audio/movie/show files are outside rollback coverage. Restored test stacks use isolated empty read-only substitutes for excluded media folders. Old-version durable recovery journals must be handled by their matching release; a changed version refuses them without replacing live state.
