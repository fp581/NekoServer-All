# Media application backups and restoration

The `app_backup` module schedules a private nightly snapshot at **04:15**, configurable with `app_backup.time`. It briefly stops the selected media apps and resumes exactly the containers that were running. This includes qBittorrent; active transfers pause while the snapshot runs. Plan the schedule outside playback/download maintenance windows. Do not deploy, upgrade or change app settings concurrently.

## Install or upgrade

Use your existing confirmed configuration. It runs on the bundled Rust program; no Rust toolchain is needed on the server.

```bash
python3 installer.py --config /path/to/your/config.json --module app_backup --check
sudo bash install.sh --config /path/to/your/config.json --module app_backup --apply
```

New sample `media` and `full` profiles include the module. Existing profiles in an older configuration are preserved: use `--module app_backup` to install it explicitly. The installer checks both mounted UUIDs before deploying the timer. It does not format disks or change existing appdata ownership.

```bash
systemctl status nekoserver-app-backup.timer
# An immediate snapshot briefly interrupts selected media apps:
sudo systemctl start nekoserver-app-backup.service
sudo journalctl -u nekoserver-app-backup.service --no-pager
```

Snapshots are created under `/mnt/nekoserver-media/backups/media-applications/snapshot-*` with default mount settings. Every run adds a new directory; existing snapshots are never replaced. Incomplete runs keep a hidden `.partial` directory for inspection and do not report success. The newest `app_backup.keep_snapshots` complete snapshots (default 14) are kept and older ones deleted after each successful run; `.partial` directories and anything else in the folder are never touched. Set it to `0` to keep everything, as before v0.21.1. A snapshot contains a full copy of the selected app state, so check HDD space against your chosen count.

Symlinks, sockets, FIFOs, devices and nested filesystems inside an app's folder are not restorable state; they are skipped and listed under `skipped` in the snapshot's manifest instead of failing the night's backup. Hard-linked files are copied as ordinary files. The SSD music library and downloads (`library/` on the SSD) are media, not app state, and are not included.

## Contents and permissions

| Included | Excluded |
| --- | --- |
| Selected Jellyfin, Navidrome, Sonarr, Radarr, Lidarr, Prowlarr, Seerr and qBittorrent appdata | Movies, music, downloads and other media content |
| Corresponding Compose folders, including settings stored there | Immich upload/PostgreSQL state; use the separate matched Immich snapshot workflow |
| Preserved SSD installer release bundles and their saved configuration | Docker engine storage, image layers, arbitrary volumes, VM disks |
| File checksums, original numeric owners/modes, container identities and image IDs | Steam/user home, Samba password database, Tailscale credentials and arbitrary host state |

Snapshots contain API keys and possibly passwords. Directories are private (`0700`), payload files and manifests are private (`0600`), owned by the root backup worker. Original ownership/modes are recorded rather than exposing the backup copy. Copy the **whole snapshot** to another protected device or off-machine location and verify it there. Image IDs are recorded for diagnosis; container images themselves are not backed up or guaranteed available upstream.

Custom writable mounts, shared appdata writers, symlinks/hardlinks, special files and nested filesystem mounts are refused. Review such a failed backup locally instead of assuming a partial snapshot covers everything. Unsupported files are never silently skipped. Compose folders and installer bundles must be stable while the snapshot runs.

## Verify and restore into a separate directory

```bash
sudo python3 app-recovery.py verify --snapshot /mnt/nekoserver-media/backups/media-applications/snapshot-EXAMPLE
# The destination must not already exist; its parent must exist.
sudo python3 app-recovery.py restore \
  --snapshot /mnt/nekoserver-media/backups/media-applications/snapshot-EXAMPLE \
  --destination /srv/nekoserver/restore-rehearsal-EXAMPLE
```

This verifies the full inventory and checksums, then copies files into a **new** directory. It does not start containers, rewrite production manifests or overwrite live appdata. Copied Compose manifests still reference the original production paths: **do not run them directly from the restore folder**. Restore files stay private by default. `--preserve-owners` explicitly applies saved numeric ownership and modes inside the new private directory and requires root.

The generated `restore-report.json` records file verification and explicitly keeps application startup unverified. A complete rehearsal still needs a separate Docker host/VM or a deliberately isolated project with rewritten paths, blocked network access and no production media/database mounts. Confirm actual SQLite/database recovery, users, libraries, API settings and request/download routing before treating the snapshot as a proven operational restore. This release does not automate that container rehearsal.

## Interrupted runs

The worker saves a private resume journal before stopping any container. On ordinary failure it attempts every restart and retains the journal if a restart fails. The backup service runs the native resume command through `ExecStopPost`; an enabled boot service retries after Docker starts. Recovery uses saved container IDs and exact Compose labels, never starts containers that were already stopped, and refuses replaced/missing identities. A later snapshot retries pending recovery before stopping apps again.

```bash
sudo systemctl start nekoserver-app-resume.service
sudo journalctl -u nekoserver-app-resume.service --no-pager
```

If recovery still fails, inspect Docker and the private `/etc/nekoserver-backups/app-resume.json` locally. Do not delete the journal until the intended original containers have been recovered. A successful file snapshot can coexist with failed app restart; the job remains failed. `ExecStopPost` recovery after forced termination and reboot/power-loss recovery still require a real systemd/server rehearsal.

## Acceptance

```bash
sudo python3 acceptance.py --profile full --save
```

The dashboard shows the additional job and its timer. Automatic acceptance requires a recent successful run and active timer; the separately witnessed application restore remains pending. Current tests exercise real Rust copying/restoration with simulated Docker and mount responses. They do not establish live database/startup or hardware readiness.
