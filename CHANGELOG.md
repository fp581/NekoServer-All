# Changelog

## v0.23.1 — 2026-10-19

### Changed: install order
- **The system is updated first.** `sudo bash install.sh` now runs a full `pacman -Syu` (with the Arch keyring first, as Arch recommends) before installing anything, so the apps and their dependencies install at their current versions. If the update installed a new Linux kernel, it stops and asks you to restart, then run the same command again: Docker networking and Tailscale need the running kernel's modules. The recovery wizard (`--recover`) skips the update.
- **Every system package the apps need is installed up front**, before the wizard starts: `docker`, `docker-compose`, `tailscale`, `caddy`, `samba`, `hdparm`, `smartmontools`, `qrencode`, `xdg-utils`. A missing package now stops the setup at the start instead of halfway. (Installing Docker does not start it; the Docker step still sets it up on the SSD.)
- **Tailscale comes right after the system check**, before the drives and apps. The Tailscale addresses for the apps are set up before the home-network addresses.
- **The USB backup drive is chosen with the SSD and HDD**, so the early space check covers it. Before, it was only chosen at the weekly-backup step near the end, and the USB check never ran on a first install.

### Fixed
- **Weekly photo and Nextcloud copies no longer re-read the whole library.** They only ever copied missing files, but checked every existing copy by reading the photo and the copy in full (SSD and HDD, then HDD and USB) each week. A copy with the same size, written after the photo last changed, is now skipped without reading it; new and changed files are still copied and checksum-verified.
- **New major versions of Immich and Nextcloud are no longer silent.** Monthly maintenance recorded a new Immich major only inside the snapshot folder, and never checked Nextcloud at all, which stays on its installed major. Both now show on the dashboard, and you can approve one in `my-config.json` (`immich_major_update`, `nextcloud_major_update`); the next monthly update installs it with the usual snapshot, restore test and automatic rollback. Nextcloud is moved one major at a time.
- **Drives the desktop already opened (`/run/media/...`) can be chosen as the SSD or HDD.** Before, a freshly plugged-in drive was missing from the list.
- **Drive prompts no longer mention "1TB" and "12TB"**, and you confirm a drive by typing its short name (such as `sdb1`) instead of its long UUID.
- **Missed weekly backups run at the next start-up** (the PC was off on Sunday night) instead of being skipped for that week.
- Samba explains the password it asks for.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).
- Existing settings keep their old step order (it still works); the new order applies to new settings files.

## v0.23.0 — 2026-10-19

### Fixed
- **Running the installer again no longer repeats finished steps.** The Tailscale HTTPS step is skipped when MagicDNS and HTTPS are already on, the phone walkthrough is skipped when it was already confirmed for the same addresses (the QR page is still refreshed), and TV hardware tests already confirmed are not asked again.
- **Optional extras can be turned down.** TV setup, phone alerts (ntfy) and the outage monitor were part of the main installation with no way to say no, so servers without them stayed "almost ready" for good and were asked again on every run. Each is now asked once; `no` is saved as `"wanted": false` and counts as done. Change it to `true` to set one up later.
- **The drive space check no longer blocks a re-run.** Its minimums are for a new install; once Docker is on the SSD, low space is reported but nothing is stopped.
- **The phone steps and the QR page show the real password-list path** on your data SSD instead of always `/srv/nekoserver/...`.
- **The recovery wizard never shares the USB drive with the weekly backup** (it takes the same lock, and says to try later if the backup is running), so the backup cannot unmount the drive in the middle of a restore. It also checks free space first and refuses a restore that would leave less than 5 GB (or 5%) free.
- **TV setup installs the right Vulkan driver** for Intel or AMD graphics (it always installed the Intel one). With NVIDIA only, it points to the Arch wiki instead of guessing a driver.

### Changed
- Version 0.23.0; the release folder is now `nekoserver-v0.23.0`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (the version changed; the Rust sources did not).
- The first run after upgrading asks the three optional questions once (unless the extra is already set up).

## v0.22.0 — 2026-10-19

### Added
- **Setup wizard: `sudo bash install.sh`.** One command, no options. It explains what will happen, creates `my-config.json` by itself (your timezone and user filled in), runs the whole installation with every guided pause, and continues where it stopped when run again.
- **Early drive space check**, right after the drives are chosen and before anything is installed. It stops with a plain explanation if the SSD or HDD is too small (under 20 GB / 50 GB free) and warns, with a yes/no to continue, below the recommended sizes or when the USB drive is too small for the photos and Nextcloud files.
- **Tailscale in the main installation.** The wizard pauses to explain Tailscale, opens the sign-in page, and turns on secure addresses. Jellyfin, Seerr, Immich, Navidrome and Nextcloud get private addresses that work anywhere on your own devices: Tailscale Serve, never Funnel, no router changes. The admin apps stay on the server.
- **Phone setup, app by app, with a QR-code page.** For each app: which phone app to install (iPhone and Android), what to type, and which username to use. Passwords are never shown; the steps say where they are. The page is at `/usr/local/share/nekoserver-pages/phone-setup.html`.
- **Finishing screen**: what's done, what still needs you (in plain words), your home and Tailscale addresses, where the passwords and phone page are, and what to do next.
- **Recovery wizard: `sudo bash install.sh --recover`.** A menu for photos, Nextcloud files, the password list, app settings, Steam/Samba settings or a monthly snapshot. It lists backups by date, restores into a new folder only, and explains how to put things back.
- `"timezone": "auto"` (the new default) uses the PC's own timezone.

### Fixed
- **Old monthly snapshots on the USB drive are removed only when it is safe.** Before, they were removed after any weekly run, even one where Nextcloud files were skipped (in use, or names exFAT cannot store). Now the weekly job keeps them unless this week's copies were complete and a restore-tested monthly snapshot (plus, if they covered Immich, an Immich database copy) is on the HDD. It logs why it kept them.

### Changed
- Version 0.22.0; the release folder is now `nekoserver-v0.22.0`.
- The Tailscale and phone steps are rewritten in plain language.

### Upgrade notes
- **Existing servers:** add `"capacity"` after `"storage"`, and `"tailscale"`, `"gateway_tailnet"` after `"gateway_lan"`, in the `full` profile of your config. Or start from the new template; the wizard creates one if `my-config.json` does not exist.
- The bundled runtime must be rebuilt for this release (Rust sources and version changed).
- A monthly update interrupted *during* the upgrade from 0.21.1 cannot be resumed by 0.22.0 (it refuses a journal from another version); finish or resolve it with 0.21.1 first.

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
