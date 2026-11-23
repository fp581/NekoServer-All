# Backups of the apps' settings

Every night NekoServer copies the settings of the media apps to the HDD: Jellyfin (your users, what you watched), Navidrome (playlists, favourites), Seerr, Sonarr, Radarr, Lidarr, Prowlarr and qBittorrent. Each night's copy is a **snapshot**: the settings as they were at that moment, kept with its date. The newest 3 are kept (setup's backups page takes 1 to 7). Your films, shows and music are not in it (they are not backed up), and Immich and Nextcloud have backups of their own ([STORAGE.md](STORAGE.md)).

You do nothing for it. It runs at the daily backup time (04:15 unless you chose another), and stops the media apps for a short while as it copies, so a film playing at that minute pauses. The dashboard's Backups page shows how the last run went.

## Putting one app's settings back

When a change went wrong (a setting you regret, a library that got muddled), bring the settings of that app back from the night before:

1. Run `sudo bash install.sh --recover`, choose **Restore something from a backup**, then **App settings, from a nightly backup**, and the night you want. It restores every app's settings into a **new** folder and replaces nothing; it names the folder, for example `/mnt/nekoserver-media/restored/apps-20261114-093000`.
2. Stop the app, put its present settings aside, copy the restored ones in their place, and start it again. For Jellyfin, with the folder the menu named in place of `FOLDER`:

   ```bash
   sudo docker stop neko-jellyfin-jellyfin-1
   sudo mv /srv/nekoserver/appdata/jellyfin /srv/nekoserver/appdata/jellyfin.before &&
     sudo cp -a FOLDER/appdata/jellyfin /srv/nekoserver/appdata/
   sudo docker start neko-jellyfin-jellyfin-1
   ```

   For another app, write its name in place of `jellyfin` everywhere (`sonarr`, `radarr`, `lidarr`, `prowlarr`, `seerr`, `navidrome`, `qbittorrent`). Seerr is linked to Jellyfin, Sonarr and Radarr: when you put one of those four back on a rebuilt server, put all four back, from the same night, or Seerr stays linked to the old ones (setup then says "Seerr is linked to another Jellyfin server"). `/srv/nekoserver` is your SSD's folder. `cp -a` keeps each file's owner, which the app needs.

   **If `mv` says "Device or resource busy"** (then nothing was copied, and the app started again with its present settings): the app is installed on a drive of its own (a server whose `my-config.json` has `"install"`), and its folder on the SSD only shows that drive's folder. `grep appdata/jellyfin /etc/fstab` prints a line that starts with the app's folder on that drive, for example `/mnt/nekoserver-drives/big/nekoserver/appdata/jellyfin`. With that folder in place of `ONDRIVE`:

   ```bash
   sudo docker stop neko-jellyfin-jellyfin-1
   sudo umount /srv/nekoserver/appdata/jellyfin &&
     sudo mv ONDRIVE ONDRIVE.before &&
     sudo cp -a FOLDER/appdata/jellyfin ONDRIVE
   sudo mount /srv/nekoserver/appdata/jellyfin
   sudo docker start neko-jellyfin-jellyfin-1
   ```

3. Open the app and check it. Its logins come back as they were that night: a password changed in the app since then, yours or a family member's, is the old one again. If setup's saved login for the app no longer works (you told setup a new password since that night, or the settings come from before a new SSD), the next `sudo bash install.sh` says so: run it once more and choose **c** at that app, and type the login it has now (the list of app passwords in a backup has it: recovery menu → **The list of app passwords**). When all is well, delete the settings you put aside (`sudo rm -r /srv/nekoserver/appdata/jellyfin.before`, or `sudo rm -r ONDRIVE.before` for an app on a drive of its own) and the restore folder. If it is not, stop the app again and move the `.before` folder back (on a drive of its own: `umount` first, as above, and `mount` again after).

Only the `appdata` folder is copied back. The `compose` folder in the restore is for reference: never start an app from it.

---

## For administrators

The rest of this page is the technical reference.

The `app_backup` module schedules a private nightly snapshot at the daily backup time setup asked for (**04:15** unless you said otherwise: `"daily"` under `"backup_times"` in your settings). It briefly stops the selected media apps and resumes exactly the containers that were running. This includes qBittorrent; active transfers pause while the snapshot runs. Plan the schedule outside playback/download maintenance windows. Do not deploy, upgrade or change app settings concurrently.

### Install or upgrade

Use your existing confirmed configuration. It runs on NekoServer's own program, which setup builds on this PC.

```bash
python3 installer.py --config /path/to/your/config.json --module app_backup --check
sudo bash install.sh --config /path/to/your/config.json --module app_backup --apply
```

The `media` and `full` profiles include the module, and an update adds it to the profiles of an older `my-config.json` (a step you took out yourself stays out). The installer checks both mounted UUIDs before deploying the timer. It does not format disks or change existing appdata ownership.

```bash
systemctl status nekoserver-app-backup.timer
# An immediate snapshot briefly interrupts selected media apps:
sudo systemctl start nekoserver-app-backup.service
sudo journalctl -u nekoserver-app-backup.service --no-pager
```

Snapshots are created under `/mnt/nekoserver-media/backups/media-applications/snapshot-*` with default mount settings. Every run adds a new directory; existing snapshots are never replaced. A run that fails does not report success and removes its half-made snapshot (a hidden `.partial` directory) as it ends; one cut off by a power cut leaves it, and the next run removes it before it starts (each one is as big as a whole snapshot, so on a full HDD they used to make every later run fail too). The newest `app_backup.keep_snapshots` complete snapshots (3 on a new server; setup takes 1 to 7) are kept and older ones deleted after each successful run; anything else in the folder is never touched. A snapshot is deleted by first renaming it to a hidden `.snapshot-….deleting` name, so a power cut part-way never leaves a half-deleted snapshot that still looks complete; the next run finishes such a delete. A count from before 0.50.0 (more than 7, or `0` for every one) stays as it is until you choose a number on setup's backups page; written by hand now, such a count is refused. A snapshot contains a full copy of the selected app state, so check HDD space against your chosen count.

**The one exception to "deleted after each successful run": a full HDD.** Before copying, each run checks the HDD has room for the app files plus 2 GB. When it does not, the oldest app snapshots are removed first, one at a time, to make room, whatever the count (also when every one is kept): so a run that then fails has removed backups, and its message says how many. The newest complete one is always kept. When removing all the older ones would still not make enough room (the films fill the HDD, say), none is removed: the run fails with a message saying how much is free and how much is needed.

Symlinks, sockets, FIFOs, devices and nested filesystems inside an app's folder are not restorable state; they are skipped and listed under `skipped` in the snapshot's manifest instead of failing the night's backup. Hard-linked files are copied as ordinary files. The SSD music library and downloads (`library/` on the SSD) are media, not app state, and are not included.

### Contents and permissions

| Included | Excluded |
| --- | --- |
| Selected Jellyfin, Navidrome, Sonarr, Radarr, Lidarr, Prowlarr, Seerr and qBittorrent appdata | Movies, music, downloads and other media content |
| Corresponding Compose folders, including settings stored there | Immich upload/PostgreSQL state; use the separate matched Immich snapshot workflow |
| Preserved SSD installer release bundles and their saved configuration | Docker engine storage, image layers, arbitrary volumes, VM disks |
| File checksums, original numeric owners/modes, container identities and image IDs | Steam/user home, Samba password database, Tailscale credentials and arbitrary host state |

Snapshots contain API keys and possibly passwords. Directories are private (`0700`), payload files and manifests are private (`0600`), owned by the root backup worker. Original ownership/modes are recorded rather than exposing the backup copy. Copy the **whole snapshot** to another protected device or off-machine location and verify it there. Image IDs are recorded for diagnosis; container images themselves are not backed up or guaranteed available upstream.

Custom writable mounts and app folders shared with another container are refused: the run fails and says why, so review it locally instead of assuming a partial snapshot covers everything. Symlinks, special files and nested filesystems inside an app's folder are skipped and listed under `skipped` in the manifest (see above), and hard-linked files are copied as ordinary files; nothing is skipped without being listed. Compose folders and installer bundles must be stable while the snapshot runs: a file that changes, appears or disappears during the copy makes the run fail.

### Verify and restore into a separate directory

```bash
sudo python3 app-recovery.py verify --snapshot /mnt/nekoserver-media/backups/media-applications/snapshot-EXAMPLE
# The destination must not already exist; its parent must exist.
sudo python3 app-recovery.py restore \
  --snapshot /mnt/nekoserver-media/backups/media-applications/snapshot-EXAMPLE \
  --destination /srv/nekoserver/restore-rehearsal-EXAMPLE
```

This verifies the full inventory and checksums, then copies files into a **new** directory. It does not start containers, rewrite production manifests or overwrite live appdata. Copied Compose manifests still reference the original production paths: **do not run them directly from the restore folder**. Restore files stay private by default. Files and folders get back the time they were last changed (snapshots from 0.28.5 on record it; older ones restore with today's date). `--preserve-owners` explicitly applies saved numeric ownership and modes inside the new private directory and requires root.

The generated `restore-report.json` records file verification and explicitly keeps application startup unverified. A complete rehearsal still needs a separate Docker host/VM or a deliberately isolated project with rewritten paths, blocked network access and no production media/database mounts. Confirm actual SQLite/database recovery, users, libraries, API settings and request/download routing before treating the snapshot as a proven operational restore. This release does not automate that container rehearsal.

### Interrupted runs

The worker saves a private resume journal before stopping any container. On ordinary failure it attempts every restart and retains the journal if a restart fails. The backup service runs the native resume command through `ExecStopPost`; an enabled boot service retries after Docker starts. Recovery uses saved container IDs and exact Compose labels and never starts containers that were already stopped. When a saved container is gone because the app was set up again since (which gives it a new container), and exactly one container with that app's Compose labels is there now, that one is started if it is stopped and the app counts as recovered; with none or several, nothing is guessed and the log says what to do (start the app again, or remove the extra containers, then run the resume service below; if you removed the app on purpose, delete the journal). Before, such a gone container blocked every later nightly backup. A later snapshot retries pending recovery before stopping apps again. A monthly update that was cut off holds the nightly app backup back only while its journal says it was in the middle of updating or rolling back; before it changed anything, or once the update stands, the nightly backups go on.

```bash
sudo systemctl start nekoserver-app-resume.service
sudo journalctl -u nekoserver-app-resume.service --no-pager
```

If recovery still fails, inspect Docker and the private `/etc/nekoserver-backups/app-resume.json` locally. Do not delete the journal until the intended original containers have been recovered. A successful file snapshot can coexist with failed app restart; the job remains failed. `ExecStopPost` recovery after forced termination and reboot/power-loss recovery still require a real systemd/server rehearsal.

### Acceptance

```bash
# In the NekoServer folder (it reads your my-config.json). Add --manual to record the
# tests you witness yourself; a test recorded before is kept by later checks.
sudo python3 acceptance.py --profile full --save
```

The dashboard shows the additional job and its timer. Automatic acceptance requires a recent successful run and active timer; the separately witnessed application restore remains pending. Current tests exercise real Rust copying/restoration with simulated Docker and mount responses. They do not establish live database/startup or hardware readiness.
