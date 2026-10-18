# Changelog

## v0.21.1 — 2026-10-19

### Changed: what is backed up where
- **USB drive: only add-only copies of Immich photos/videos and Nextcloud user files.** The weekly USB job now also copies each Nextcloud user's own files (not previews, trash or caches). Monthly maintenance no longer writes to the USB drive or needs it connected; the full monthly snapshots older releases put there are removed by the weekly job once its own copies succeed.
- **HDD: software, OS and photos.** Nightly app-state and host-state snapshots, the weekly add-only photo copy and Immich database dump, and the monthly pre-update snapshot of app settings, databases and container images. The monthly snapshot no longer contains the Immich photo library or Nextcloud files; its restore test checks the Immich and Nextcloud databases by SQL instead of starting those two apps.
- **Add-only copies never fail on a changed file.** A file that changed since it was copied (an edited document, an updated `.xmp` photo sidecar) used to fail the whole job ("Collision or changed original"). It is now kept beside the old copy as `name.neko-<date>.ext`; nothing is overwritten or deleted.

### Fixed
- **Lidarr imports no longer copy music.** Lidarr now mounts the SSD library once at `/data`, so `/data/downloads/music` and `/data/music` share one mount. Previously they were separate bind mounts, so imports failed to hardlink/move (EXDEV) and fell back to copying, doubling SSD use while seeding. Container paths and app settings are unchanged; mount checks in `acceptance.py`, `media-doctor.py`, `media-health.py` and the Rust runtime were updated to match.
- **Seerr honours `media.uid` / `media.gid`** instead of a hardcoded `1000:1000`.
- **Nextcloud works on both gateways.** Applying `remote_access` after the LAN gateway replaced Nextcloud's `overwritehost` with the Tailscale name, breaking Nextcloud on the home network for devices without Tailscale. Gateways now only add their address to `trusted_domains`; a gateway-set `overwritehost` from v0.21.0 is removed. The Docker hop is added to `trusted_proxies`, so Nextcloud sees real client addresses for login throttling and logs.
- **Monthly maintenance no longer fills the SSD and blocks itself.** Each month it copied the full snapshot (all photos, Nextcloud files, images) onto the SSD as well, and never deleted any, so the free-space check soon failed and container updates stopped. Snapshots now go to the HDD only, old SSD copies are reclaimed once the same run is on the HDD, and the newest `maintenance.keep_snapshots` complete snapshots (default 3) are kept.
- **Nightly app backups failed every night with the default apps.** qBittorrent's SSD music-downloads mount (and now Lidarr's SSD library) counted as "unmanaged writable state". The SSD library is media, not app state, and is now allowed.
- **Nightly app and host backups no longer abort on one odd file.** Symlinks, sockets, FIFOs, devices and nested filesystems are skipped and listed in the manifest; hard-linked files are copied normally.
- **Monthly updates no longer require a configured USB drive.** Maintenance still listed the USB photo backup as a required module, so without a USB drive set up the installer blocked it and monthly updates never ran.
- **Overlapping night jobs no longer fail each other.** The weekly photo backup, monthly maintenance and nightly app/host backups share one lock, which failed immediately when busy: the Sunday app backup failed whenever the photo job ran long, the nightly backups failed on maintenance nights, and maintenance was skipped for the month when the 1st was a Sunday. Scheduled jobs now wait up to 3 hours (app/host backup service limits raised from 2 h to 6 h to allow for it).
- **Weekly Immich database dumps no longer pile up forever.** Each week added another full `.sql` dump to the SSD and HDD (often hundreds of MB to GBs). The newest `photos.database_keep` dumps (default 8, about two months) are kept on each drive; `0` keeps all.
- **Jellyfin starts on machines without a GPU.** `/dev/dri` is only passed through when it exists.
- **Weekly Nextcloud copy to an exFAT USB drive** skips (and lists) files whose names exFAT cannot store, such as `Meeting 10:30.docx`, instead of failing the job.
- **Fresh Nextcloud installs avoid a brand-new major** (x.0.0/x.0.1) and install the previous major, since the Docker image can lag the release.
- **Password restore** accepts only absolute snapshot/destination paths without `..` or symlinks, and checks the snapshot is a real monthly snapshot (SonarCloud findings).
- CI: actions pinned by commit, Rust pinned to 1.99.1, PyYAML installed by hash from wheels only.
- **Timezone is configurable.** A new top-level `timezone` config key drives container `TZ`, Immich's `.env` and every systemd timer. It falls back to the old `media.timezone` / `maintenance.timezone` keys, then the host's `/etc/localtime`, then UTC. The default stays `Asia/Jerusalem`.

### Removed
- **Python fallback backend.** Every scheduled job and background service runs the bundled Rust program. Configs that say `runtime.backend: python`, or have no `runtime` setting, are treated as Rust; rerun the installer once to rewrite their units. Deleted: `photo-preserve.py`, `photo-preservation-job.py`, `photo-backup.py`, `photo-weekly.py`, `immich-db-backup.py`, `job_status.py`, `modules/_runtime.py`, `dashboard_server.py`, `gateway_guard.py`. `dashboard_collect.py`, `storage-guard.py` and `media-health.py` stay: they are libraries or manual tools.
- **Unused tools:** `media-doctor.py`, `migrate-docker.py` (referenced by nothing).
- **Historical docs and old test output:** `AUDIT.md`, `INSTALLATION-REVIEW.md`, `PLAN-REVIEW.md`, `RELEASE-CONTENTS.md`, `audit-evidence/`, and old version notes at the top of several docs. Tests now run in CI instead.
- Unused imports and one unused variable.

### Added
- **App password list encrypted in monthly HDD snapshots** (it is never written to the USB drive), opened with your sudo password (`backup_secrets.py`). The installer asks for the sudo password once and checks it; nightly runs need no password. Restore with `monthly-recovery.py --secrets` or `backup_secrets.py --restore`.
- **Retention** for monthly (`maintenance.keep_snapshots`, default 3) and nightly (`app_backup.keep_snapshots`, `host_backup.keep_snapshots`, default 14) snapshots. `0` keeps everything. Incomplete runs and anything the worker did not create are never deleted.
- **Optional qBittorrent peer port** (`media.torrent_port`, default closed). It is the only port allowed beyond localhost, and the installer, monthly worker and acceptance checks refuse any other.
- Docs: device certificate checks for phones/TVs on the LAN gateway.
- Upgrade path: unedited v0.21.0 Lidarr and Seerr manifests are recognised and migrated (`previous_templates()` in `modules/media.py`), alongside the existing Navidrome migration.
- Regression tests: `tests/test_review_fixes.py`, `tests/test_gateway_cloud_origin.py`, `tests/test_backup_secrets.py`, and Rust tests for retention, encryption, skipped files, the peer-port rule, versioned add-only copies, the Nextcloud copy and USB cleanup.
- GitHub Actions CI: Python compile/lint/tests, `docker compose config`, `cargo fmt`/`clippy`/`test`, a check that the bundled runtime matches its sources, and a static musl runtime build that can commit the rebuilt binary.
- `.gitignore`.

### Upgrade notes
- The bundled `runtime/bin/nekoserver-runtime` must be rebuilt for this release (Rust sources and version changed). Run the CI workflow with **commit_runtime**, or `python3 runtime/build.py` as a normal user.
- Rerunning the installer upgrades unedited v0.21.0 Lidarr and Seerr manifests in place, keeps the old one as `compose.before-v0.21.1.yaml`, and recreates those containers. A manifest you edited by hand is left alone and reported, as before.
- Rerun the installer interactively once so it can ask for your sudo password and create the backup key. Until then, monthly backups leave the password files out (they are never stored in plain text).
- **Old backups are deleted.** On the first successful runs after upgrading: HDD monthly snapshots beyond the newest 3, nightly snapshots beyond the newest 14, Immich database dumps beyond the newest 8, v0.21.0's SSD copies of monthly snapshots, and (after the weekly USB copy succeeds) every old full monthly snapshot on the USB drive. v0.21.0 monthly snapshots contain the password list in plain text; the HDD ones go once they fall outside the retention count. Set the `keep_snapshots` values before upgrading if you want to keep more.
- Rerun the `photo_backup` module (or the full profile) so the weekly USB job picks up the Nextcloud copy. The first run copies every Nextcloud file and can take a long time.
- `maintenance.require_external` is no longer used.
- Reapply the gateways (`full`, then `remote_access`) to apply the Nextcloud fix.
- The installation guide PDF still shows v0.21.0 paths; substitute `v0.21.1`.
