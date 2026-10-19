# Storage and scheduled work

| Content | Working location | Backup policy |
|---|---|---|
| Music audio and downloads | SSD `library/music` and `library/downloads/music` | No automatic file backup, including monthly snapshots |
| Music application settings/accounts/playlists | SSD appdata | Existing application-state backup policy |
| Immich photos/videos | SSD `photos/immich-library` | Weekly add-only originals copy to the HDD and to the USB drive; weekly Immich database dump on the SSD and HDD (newest `photos.database_keep` dumps kept, default 8) |
| Movies/shows and their downloads | HDD library | No media-file backup; application state remains protected |
| Nextcloud files | HDD `cloud/files` | Weekly add-only copy of each user's files to the USB drive |
| App settings and databases, container images | SSD appdata, Docker | Nightly app-state snapshot and monthly pre-update snapshot, both on the HDD |
| OS and user settings (Steam, Samba) | OS disk, home | Nightly host-state snapshot on the HDD |

**What goes where.** The USB drive holds the weekly add-only copies of Immich photos/videos and Nextcloud user files, plus the newest 4 weekly Immich database copies and the Immich version that made them, so it can rebuild Immich on its own (RECOVERY.md, "Rebuild Immich from the USB drive"). The HDD holds the software side (app settings, databases, container images, OS/user settings) and the same add-only Immich photo copy plus the Immich database. "Add-only" means new files are added and nothing on the backup is ever overwritten or deleted: a file that changed (an edited document, an updated photo sidecar) is kept next to the old one as `name.neko-<date>.ext`. Nextcloud previews, trash and caches are not copied; group folders are not covered.

Navidrome, Lidarr and the qBittorrent music category use the SSD. Local imports route music to SSD and videos to HDD. Incomplete downloads may use the configured shared temporary folder. No watched-video migration between SSD/HDD is performed.

## Weekly photo protection

The combined originals/database worker runs **Sunday at 02:15** in your configured `timezone`. It copies mature originals additively from SSD to HDD, verifies bytes, creates the logical database dump, and verifies database copies on SSD/HDD. Both operations use one worker and recovery lock. A failed archival step prevents a misleading successful database job. The dashboard's archive/database cards use this combined result and weekly overdue thresholds.

The USB copy runs **Sunday at 03:00**. It can overlap a still-running local photo job; that is safe because it skips files still being written (`.neko-part`), and anything missed is copied the next week. The correct external disk must be connected; otherwise the run fails visibly without writing to the OS disk. All managed photo timers are weekly, with daytime catch-up disabled. A missed scheduled week waits for the next scheduled run unless started manually. Files modified in the last ten minutes are deferred for stability. New uploads can therefore wait roughly a week for an independent server copy. Monthly cold full-state snapshots and isolated recovery tests remain enabled.

The previous frequent photo archival timer is disabled/stopped on apply. The formerly daily database timer is rewritten as weekly. Immich's own internal SSD-local database backup feature is separate from these managed server-to-HDD/external copies.

## Removed recurring work

`monitoring.enabled: false` disables and stops the old NekoServer media-health timer/worker. There is no replacement every-six-hours media audit. Monthly pre/post-update connection checks and ad hoc diagnostics remain available. The dashboard continues lightweight host/container-status collection and reports routine media checks as disabled; it does not show stale old API audit failures.

`music.backup_enabled: false` disables/stops the music backup timer/worker. Monthly recovery excludes raw music and music downloads too. Application accounts/playlists/settings still receive the existing state backups. Previous backup files are preserved; no user data is deleted. The opt-in backup CLI/restore reader remain available for explicit manual use/old inventories.

The cleanup modules remain in dependency plans so applying the current full/media/music installer can retire old timers. Managed unit files are recognized before service changes; custom administrator files are preserved and reported for review. A disabled timer is checked as absent/inactive during acceptance, rather than required to be active.

## HDD and unchanged jobs

The guarded native SATA standby timer remains **60 minutes** (`hdparm -S 242`), with no PC sleep. SMART queries avoid waking a standby disk. Playing HDD videos, Nextcloud activity, torrent seeding, app-owned scans, nightly application/host-state backups, weekly photo copies and monthly maintenance can still wake it. Removing NekoServer's periodic media audit cannot prevent ordinary application activity. Native power behaviour and wake latency need testing on the target PC.

Nightly app-state/host-state backups and monthly overnight recovery/update work are unchanged. Scheduled orchestration remains Rust; Python handles installation and ad hoc recovery. No OS upgrades or automatic formatting are added. Snapshot retention is described in MONTHLY-MAINTENANCE.md, APP-BACKUPS.md and HOST-BACKUPS.md; add-only photo and Nextcloud copies are never pruned.

## Existing installations and recovery

Applying this release updates schedules and retires the recognized old timers. Existing old HDD music/Immich working layouts still require the verified, stopped-writer migration described in the prior release; different manifests or environment paths are preserved/refused. Do not switch a mount to an empty SSD folder and assume files moved.

Monthly recovery covers selected app state and databases (including Immich's and Nextcloud's) and exact old images. Since v0.21.1 it no longer contains the Immich photo library or Nextcloud files; those are restored from the weekly add-only copies. Raw audio/movie/show files are outside rollback coverage. Restored test stacks use isolated empty read-only substitutes for excluded media folders. Old-version durable recovery journals must be handled by their matching release; a changed version refuses them without replacing live state.

## Private account inventory

The secrets/accounts directory is included in monthly snapshots on the HDD, encrypted, and opens with your sudo password (see MONTHLY-MAINTENANCE.md). It is never written to the USB drive. See ACCOUNT-SETUP.md.
