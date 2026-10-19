# Changelog

## v0.24.2 — 2026-10-19

### Fixed (from an outside review)
- **`install.sh` exited silently before setup.** It called `df -P --output=avail`, which GNU df rejects; with `set -euo pipefail` and the error hidden, the installer stopped without a word. It now uses `df --output=avail`, and if the space cannot be measured it says so plainly and continues.
- **Weekly backups could miss a replaced file.** A copy was trusted when its size matched and it was written after the source's *modified* time, which tools that preserve timestamps can set back (reproduced: `AAAA` backed up, replaced by `BBBB` with the old timestamp, nothing copied). The check now uses the source's *status-change* time (ctime), which any rewrite or replacement moves forward and which cannot be set back. In addition, every 4 weeks the archive and USB jobs compare every existing copy by checksum.
- **Turning on the dashboard's Tailscale address broke the dashboard service, local access included.** The service passed `--public-host`, which the runtime's option table did not accept. The option is registered.
- **Incomplete USB backups were reported as successful.** When Nextcloud files were skipped (in use) or refused (names exFAT cannot store), the weekly job still recorded success. It now records **partial**, with counts and up to 50 file names; the dashboard shows *Incomplete* with the files, phone alerts warn, and it does not count as a full success.

- **Password files are no longer in plain text in monthly snapshots.** Encrypting `APP-CREDENTIALS.txt` left other copies readable: each app's Compose `.env` (database and Nextcloud admin passwords) and Nextcloud's `config.php` in `data.tar`, and the resolved app definitions (`*-old.json`, `*-new.json`, `*-rehearsal.json`, `result.json`). The `.env` files and `config.php` now go only into the encrypted `secrets.tar.cms` (left out of `data.tar`), and the saved definitions have their password values blanked once the update or its rollback is over (they are needed until then). Snapshots made before this release stay as they were until retention removes them. Without a backup key the files stay in `data.tar` so the snapshot remains restorable.
- **The USB drive can rebuild Immich on its own.** It held the photos but not the database, so albums, faces and users needed the SSD or HDD. The weekly USB job now also copies the newest Immich database copy (the one made that night; the newest 4 are kept) and `IMMICH-VERSION.txt` with the Immich version that made them (never the database password). RECOVERY.md has a "Rebuild Immich from the USB drive" section.
- **The recovery wizard described file versions backwards.** It called the dated `.neko-<date>` copies "older versions"; they are the later edits. It now offers to restore only the newest version of each file under its own name (the default), shows the date of each file that had several versions, and explains the naming correctly when every version is copied.
- **Every restore checks free space first.** Nightly snapshot restores, monthly snapshot unpacking and loading saved app versions now use the same check as photo and Nextcloud restores (refuse if less than 5 GB or 5% would be left free).
- **Nightly app and host backups use the configured timezone**, like every other schedule (they ran in the system's own timezone). A test checks every generated timer.

- **The USB backup must be a separate physical disk.** The picker excluded the SSD's and HDD's own filesystems but not other partitions on the same disks, so a second partition of the media HDD (or of the system disk) could be chosen, and one disk failure would take both copies. Only partitions on a disk that holds neither the system, the SSD nor the media HDD are offered now.
- **Phone alerts cover Nextcloud and monthly maintenance.** Notifications used a list of known alert names that left out `app:nextcloud`, `backup:maintenance`, `backup:music` and the new major-update notices, so those were shown on the dashboard but never sent. A test now checks every monitored app and job has a notification.
- **The Tailscale acceptance check accepts the installer's own routes.** It compared against the apps only, without the dashboard route the installer adds, and failed. Installation and the check now use one route calculation (`tailnet_routes`).
- **Failures say why and where.** The runtime printed one generic message and dropped every command's error output. It now keeps a short, sanitized reason (anything that looks like a password or key removed) and the step it was on ("copying photos and videos to the USB drive"), in the job report, on the dashboard ("Failed while …: …") and in its error output.

- **Old full snapshots on the USB drive are no longer removed automatically.** Releases before v0.21.1 wrote full monthly snapshots (photos and Nextcloud files included) to the USB drive, and the weekly job removed them once current backups existed, without checking that their files survive elsewhere. A file deleted from the server long ago could exist only there. The weekly job now keeps and lists them; the recovery menu (`sudo bash install.sh --recover`, "Old full snapshots on the USB drive") shows their dates and sizes, offers to copy them to the HDD first, and removes them only when you type DELETE. The automatic cleanup code is gone.
- **qBittorrent's music category must point to the music download folder.** Setup accepted an existing `music` category saving to `/data/downloads` (on the HDD, where Lidarr does not look) and reported success. Music must save to `/data/downloads/music` (on the SSD); a wrong but empty music category is corrected, one with torrents stops setup with instructions. TV and movies may still use the shared `/data/downloads`.
- **Turning Nextcloud off stops an installed Nextcloud.** `"enabled": false` only skipped installation, so an existing Nextcloud, its database, Redis and cron kept running and restarting. Its containers are now stopped (files and database kept; they stay stopped after a reboot), and turning it back on starts them again.

- **`--config` reaches the wizard and recovery.** `sudo bash install.sh --recover --config FILE` (and `--wizard --config FILE`) accepted the option but used the settings next to the installer, so recovery could look for backups on the wrong drives. Both now use the chosen file; without `--config` they use `my-config.json` as before.
- **Nextcloud acceptance results no longer turn the dashboard's acceptance panel to "unknown".** Python writes a manual check named `cloud_client` that the Rust dashboard did not know, so it treated every cloud/full report as invalid. It is known now, and a test checks that every manual check Python can write is known to the dashboard.
- **A pending app login can be corrected.** If creating an app's account did not finish and you completed the app's setup yourself with another login, the account tool kept retrying the saved login and never offered to change it. It now offers to enter the login you used; that switches the app to "existing account", so it is never created or reset.

- **The USB space check no longer counts old backup versions as free room.** It added everything already on the USB drive to its free space, as if those files could be reused. The backup only ever adds: earlier versions stay, and a changed file needs its full size again. With 100 GB of new files, 30 GB free and 150 GB of earlier versions, it gave no warning. It now adds up what the next weekly copy still has to write (files not on the drive yet, files whose copy differs, and this week's Immich database copy) and compares that, plus room to grow, with the free space.
- **An interrupted first setup of Immich or Nextcloud now finishes on the next run.** Each writes several settings files; stopping between them left a half-written set that every rerun refused ("Incomplete … deployment"). `compose.yaml` is now always written last, so it marks a finished step. A rerun that finds only the earlier files completes the step: Immich keeps the `.env` it wrote (its version and database password); Nextcloud writes its files again, since nothing has started yet. Half-written sets left by older releases are completed the same way as long as the app has no database yet; once it has one, the files are still kept for inspection.

- **`--config` paths are checked before they are read.** The installer, the setup wizard and recovery now accept only a `.json` file, with no `..` and no symlinks along the path (flagged by SonarCloud as a possible path traversal).

### Added: tests of how the pieces connect
- `tests/test_integration.py` runs the real `install.sh` preflight with the real `df` (only pacman, network tools and system identity stood in for), and checks every runtime command the installer writes into a systemd unit (22 commands) against the runtime's own option table, and against the bundled binary whenever it matches its sources. Both reported blockers fail these tests.

### Fixed
- **Dashboard app buttons work on the phone.** Opened over Tailscale, the dashboard's app buttons still pointed to `http://127.0.0.1:…`, which on a phone means the phone itself. They now open each everyday app's private Tailscale address (Jellyfin, Seerr, Immich, Navidrome, Nextcloud); the admin apps, which are only reachable on the server, say "Opens only on the server itself" instead of failing.

### Changed
- Version 0.24.2; the release folder is now `nekoserver-v0.24.2`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (version change only).

## v0.24.1 — 2026-10-19

### Added
- **Dashboard on your phone.** The read-only dashboard (drives, apps, backups, alerts) now also has a private Tailscale address, `https://<server>.ts.net:8448/`, on your own devices only (Tailscale Serve, never Funnel). It is on the phone setup page and the finishing screen. The dashboard server accepts only its loopback names and this server's own Tailscale name, and has no way to change anything. `"remote": false` under `"dashboard"` keeps it on the server only.
- **Hardware video in Jellyfin and Immich.** A new step, *Video using the graphics chip*, finds the graphics chip, installs its video drivers, checks from inside Jellyfin that the chip really decodes video, then turns on hardware transcoding: Quick Sync on Intel, VA-API on AMD, in both Jellyfin and Immich (Immich's server also gets the graphics device, kept across monthly updates in a `hardware.yaml` next to its Compose file). A setting you already chose in either app is kept; without a usable chip (or with NVIDIA) both apps keep working in software. See HARDWARE-VIDEO.md.
- Shared `gpu.py` for graphics detection (TV and hardware video).

### Fixed
- **Music no longer stays "unfinished" without Firefox.** Its optional desktop shortcut needed Firefox, which only the TV setup installed; with the TV extra turned down, music (and the finishing screen) stayed "needs setup" forever. A missing browser now just skips the shortcut; music works in any browser and on phones.

### Changed: less duplicated code (same behaviour)
- `app-recovery.py` and `host-recovery.py` were two near-identical 140-line copies. Both now share `snapshot_restore.py`; the commands and their output are unchanged, and the restore logic is covered by tests for the first time.
- One yes/no question (`guided_setup.yes_no`) replaces five copies (setup wizard, space check, recovery wizard, optional extras, guided steps).
- One Arch/Nyarch check (`neko_common.arch_host`) replaces three (Tailscale, TV, recovery); it also tolerates a missing or unusual `/etc/os-release`.
- One drive-list command for all drive pickers.
- Rust: one monthly-snapshot listing for maintenance, the weekly USB job and the dashboard, and one Immich database-copy name check.
- Version 0.24.1; the release folder is now `nekoserver-v0.24.1`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).
- Existing servers: add `"hardware_video"` after `"accounts"` in the `full` profile of `my-config.json` (or start from the new template), then run `sudo bash install.sh`. The dashboard's Tailscale address is added on that run too.

## v0.23.3 — 2026-10-19

### Fixed
- **Old full snapshots on the USB drive were removed once an HDD snapshot merely existed.** The weekly job now also checks that snapshot's archives against their checksums before removing them; a missing or altered archive keeps the USB snapshots.
- **The Seerr permission check still assumed user 1000.** Since v0.23.2 Seerr's folder belongs to your user, but the readiness check still checked it as 1000:1000 and reported a false "needs setup" for other users. It now checks the configured user, like every other app.
- **The system disk is checked before the system update.** The update and packages go on the system disk, before the drives are picked; with less than 5 GB free there, `install.sh` now stops before changing anything and says how to free space. (The SSD and HDD are still checked after you pick them, before any app is installed.)
- **Monthly retention could delete the last good snapshot.** Snapshots beyond the newest 3 were deleted without checking that the 3 kept are intact. If any kept one fails its checksum check, nothing older is deleted that month, and the log says why.
- **Running the installer during a night-time backup no longer fails with "Another recovery operation is active".** It waits for the backup (up to 30 minutes, and says so), then explains what to do if it is still running.
- The home-network step and the README now say to reserve the server's address in the router, so the home addresses keep working after a restart.
- Finishing screen: a list join no longer uses `sum()` (SonarCloud).

### Changed
- Version 0.23.3; the release folder is now `nekoserver-v0.23.3`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).

## v0.23.2 — 2026-10-19

### Fixed (from an outside review)
- **A monthly backup's SSD copy could be deleted while the HDD copy was missing or damaged.** Old SSD copies of monthly snapshots were removed when their manifest matched the HDD's, without checking the HDD archives themselves. Now the SSD copy is removed only when the HDD run is complete (restore-tested) and every HDD archive matches its checksum. Otherwise it stays, as possibly the only good copy.
- **A failed drive space check did not stop the installation.** Docker, the apps, Immich, Nextcloud, backups and maintenance still ran after the check failed (or you answered no). Every step that writes to the drives now waits for it; Tailscale, which does not, still runs.
- **Seerr could not use its settings folder with a user other than 1000.** It runs as your user (`media.uid`/`gid`), but its folder was created for 1000:1000 with mode 0750. The folder is now created for your user, and an existing one owned by 1000:1000 is handed to your user (that folder only; links are not followed).
- **A correct nightly app-backup setup failed its readiness check.** Setup writes the retention setting into the backup's private settings, but the check expected them without it and reported "needs setup". Both now use one shared definition.
- **The finishing screen could say "ready" with missing or unreadable state.** It now checks every step the installation is meant to run; a step with no record counts as unfinished.
- **The USB space check used the drive's size, not its free space**, so a large but nearly full drive passed. It now measures free space, plus what an earlier weekly copy already holds there (opening the drive read-only if needed).

### Fixed
- **No stale "newer Nextcloud" warning after an approved upgrade.** In the month an approved Nextcloud major was installed, maintenance first recorded "Nextcloud 32 is available (you have 31)" and the dashboard kept showing it for a month. The check is now skipped in the month a major is being installed.
- **Approving an Immich major survives a fix release.** The approval had to be the exact release (`"v3.0.1"`), so it stopped matching if Immich published v3.0.2 before the update night. You can now approve the major itself (`"immich_major_update": "v3"`); an exact release still works.
- **Acceptance checks leave out extras you turned down** (TV, phone alerts, outage alerts), so they no longer stay "not done" on the dashboard.
- **Answering "no" at the wizard's first question no longer says "Nothing was changed"**: by then the system update and packages are already installed, and it now says so.
- Docs: a big Nextcloud can take longer than the 5-minute health wait to upgrade; MONTHLY-MAINTENANCE.md explains raising `health_wait_seconds`.

### Changed
- Version 0.23.2; the release folder is now `nekoserver-v0.23.2`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).

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
