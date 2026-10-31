# Getting things back

**Easiest way:** in a terminal on the server, run this in the NekoServer folder:

```bash
cd NekoServer-All/nekoserver
sudo bash install.sh --recover
```

A menu asks what you want back, lists the backups by date, and copies them into a **new** folder. Nothing you have now is overwritten or deleted.

The technical tools behind it (photo snapshots, test restores, replacing Linux, database copies) are at the end, under [For administrators](#for-administrators).

## Which situation are you in?

| What happened | The simplest way back |
| --- | --- |
| **I deleted photos or videos by mistake** | Recovery menu → **1. Photos and videos (Immich)** → the HDD (or the USB drive) → the newest backup → at "Which files?" choose **As of the last backup**. Then upload the photos you need again with the Immich app or website. |
| **I lost files in Nextcloud** | Plug in the USB drive (Nextcloud files are copied there each week). Recovery menu → **2. Nextcloud files** → whose files → at "Which files?" choose **As of the last backup**. Then upload the files you need again with the Nextcloud app or website. |
| **I need one of the app passwords** | The server still works: show it on the screen with `sudo python3 /srv/nekoserver/installer/releases/v0.40.3/account-setup.py show` ([GUIDED-SETUP.md](GUIDED-SETUP.md#seeing-an-apps-password)). After rebuilding the server: Recovery menu → **3. App password list**; it asks for the **backup password** you chose during setup. |
| **An app's settings went wrong** (after a change you regret, say) | Recovery menu → **4. App settings (nightly backup)** → the night before the change. It restores every app's settings into a new folder; the end of the menu, and [APP-BACKUPS.md](APP-BACKUPS.md), say how to copy back the one you need. |
| **I want older Steam saves or shared-folder settings** | Recovery menu → **5. Steam and Samba settings (nightly backup)**. |
| **The checks found a damaged backup copy** (the dashboard says so) | Recovery menu → **8. Repair damaged backup copies**. |
| **I am replacing Linux** (new system disk; the SSD and HDD are fine) | Follow [Replace the OS and reattach the existing store](#replace-the-os-and-reattach-the-existing-store), step by step. Your apps and files on the drives are kept. |
| **The SSD or the HDD broke** | Put in a new drive (formatted) and run `sudo bash install.sh`. It says to take the old drive's `"uuid"` out of `my-config.json` first, and then asks for the new drive. Then get your things back with the recovery menu: photos from the other drive or the USB drive, app settings, the password list. |
| **Both the SSD and the HDD are lost** | Your photos and videos are on the USB drive: follow [Rebuild Immich from the USB drive](#rebuild-immich-from-the-usb-drive). |

Not sure? Start the recovery menu anyway. It only reads your backups until you choose something, and it never overwrites anything.

## The recovery menu, step by step

1. **Start it:** `sudo bash install.sh --recover` in the NekoServer folder. Type your login password when `sudo` asks.
2. **Choose what you want back** by its number. `0` goes back (or leaves the menu).
3. **Choose where from**, when the menu asks: the HDD, or the USB drive (plug it in first). If it says no backup was found, check that the drive is plugged in.
4. **Choose a backup by its date.** The newest is first.
5. **For photos and Nextcloud files, choose "Which files?":**
   - **As of the last backup** (recommended): the files you had at that backup, each as it was then. Files you had deleted on purpose stay out.
   - **As of an earlier backup**: the same, for a date you pick.
   - **Everything ever kept**: every file the backup has, also ones you deleted or renamed. Use this when you can't find something.
6. **Wait.** The menu checks that there is enough free space first, and waits if the weekly backup is using the USB drive. Every restored file is checked against the backup.
7. **Find your files.** They go into a new folder on the HDD, normally `/mnt/nekoserver-media/restored/`, with the date and time in its name. The menu shows the exact folder and says how to put the files back.

Good to know:

- **A file that could not be restored** (damaged or missing on that backup) is named at the end and in `NOT-RESTORED.txt` in the restore folder. Try the other backup (HDD or USB) for it.
- **Steam and Samba settings** come back in folders called `source1`, `source2` and so on. `restore-report.json` in the restore folder says which original folder each one is. To read it: `sudo cat /mnt/nekoserver-media/restored/FOLDER/restore-report.json`, with the folder name the menu showed in place of `FOLDER`.
- **The HDD is missing or broken?** Then the menu asks you to type a folder on another drive for the restore, so your system disk never fills up.

---

## For administrators

The rest of this page is the technical reference: how the backup lists work, and the command-line recovery tools for snapshots, test restores and replacing Linux.

### Which files: how the lists work

**Restore only the files you had.** The weekly photo and Nextcloud copies only ever add files. A file you deleted on purpose is still on them, and a renamed or moved file is there under both its old and its new name. So since 0.39.6 each weekly copy also writes down which files you had at that moment, and which saved copy holds each one's content then. These lists are kept in a hidden `.neko-inventory` folder in the backup (the newest 26). A run writes a new list only when something changed since the last one (the monthly check right after a weekly copy, or a week without changes, adds none), so with the weekly schedule they reach back at least half a year. With the older `"backup_schedule": "legacy"` for photos (a copy every few hours), the photo lists can cover only days. When you restore photos or Nextcloud files, the recovery menu asks **Which files?**:

- **As of the last backup** (recommended): only the files you had at the newest weekly copy, each as it was then. Deleted files stay out, and a renamed file comes back only under its new name.
- **As of an earlier backup**: the same for a date you pick from the list.
- **Everything ever kept**: every file the backup has, also the ones you deleted or renamed (the newest version of each, or every version).

Each run notes, before it copies anything, that it has started (`.neko-inventory/last-run.json`, "pending"), and marks itself complete only once its files, checksum list and list of files are all written. If the newest backup did not finish (it was cut off part-way, or its list could not be written, a full drive say), it is shown as incomplete on the dashboard (when its list failed), and the menu does not offer the older list as "the last backup": it says the newest backup did not finish, that the newest complete list is from an earlier date and leaves out the files backed up after it, and recommends **Everything ever kept**. The older list is still offered, named by its date.

**Where a restore goes.** Restores go into a new folder in `restored/` on the HDD, but only while the HDD is mounted and is the drive in your settings. When it has failed or is unplugged, its folder is an empty one on the system disk; the menu then says so and asks you to type a folder on another drive (one under the HDD's folder is refused), so a restore never fills the system disk. Each restore gets a folder of its own, named after the date and time to the second (with `-2`, `-3` added if that name is taken), so restoring again at once never runs into "That folder already exists".

**Who can open restored photos and Nextcloud files.** They are made yours: at the end of a photo or Nextcloud restore the new folder and every file in it are given to the account that ran `sudo` (or, without sudo, to the media account in your settings, `media.uid`), still private to it. So you can open them from your desktop and pick them in the browser's Upload window. The HDD's `restored` folder lets that account's group in; the other restores kept there (app settings, monthly snapshots) stay private to root. If the folder you chose is somewhere that account can't reach, the menu says so and prints the `sudo mv` line that moves it into your home folder.

Each list also keeps the checksum of every file's content, so a date restores exactly what you had then: also when a file later became a folder (and the backup moved its saved copy into `neko-replaced`), or a new file took its name. A file whose content no saved copy holds any more is named as not restored, never replaced by another file's content. Lists written by 0.39.6 have no checksums yet: a file that became a folder (or the other way round) after such a list is left out of that date's restore, and the menu says so; "Everything ever kept" still has every saved copy of it. A backup made before 0.39.6 has no list yet. Then the menu says so and restores everything ever kept, as before: look through the restore folder and delete what you had removed before uploading the rest. A list that can't be read is never used; the menu then offers everything too. A photo still being written during a weekly copy is not in that copy yet, so it is not in its list either.

### What each operation does

| Command | Default behavior | With --apply |
| --- | --- | --- |
| check | Check mounted UUIDs and preserved installer checksums | No apply mode |
| checkpoint | Inspect running Docker/store identity | Save private host checkpoint on SSD |
| reattach | Verify existing-store reattachment prerequisites | Configure the fresh OS and start the same store after SSD UUID confirmation |
| snapshot | Verify prerequisites and show a pause/copy/dump preview | Create a new matched Immich bundle; temporarily pause and then resume running services |
| restore-test | Check all snapshot files and preview a separate instance | Restore into fresh isolated files/database after RESTORE-ISOLATED confirmation |

These operations are separate from ordinary installation. Nothing formats drives, overwrites an existing snapshot/workspace, replaces the live Postgres directory or converts Docker to Podman. A successful check is not proof of restored photos. A snapshot stored alongside the source HDD does not protect against that HDD failing.

### Prepare while the current server works

Apply this release's foundation profile once to preserve the recovery tools and record the checkpoint. This recognizes an already correctly configured Docker foundation and does not restart it. The checkpoint supports rootful classic overlay2 Docker; other stores need a separately tested recovery plan.

```bash
sudo bash install.sh --profile foundation --apply
sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py check
sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py checkpoint --apply
```

Save the current config.json, host-checkpoint.json, installer bundle and independent backups somewhere accessible outside the OS disk before replacing the OS. Keep the configured media UID/GID and note the Docker version (the reinstalled Linux needs that version or newer). Configuration contains drive identities; application secrets belong in the protected Compose/appdata files and snapshot bundles. Do not put those secrets into ordinary logs.

### Create a matched photo snapshot

The snapshot reads the existing pinned Immich deployment and the containers it actually uses. It checks the standard upload/database mounts and matches database credentials/routing. External libraries and custom services/hardware/user settings require a manual recovery plan; the tool refuses to silently omit them.

Choose a new name directly beneath recovery-snapshots on the application SSD or media HDD. Free space must cover the full upload copy, a database size allowance and reserve. Snapshot creation can take a long time for large libraries; it keeps Immich's running non-database services paused during copy/dump. Do not perform other Immich maintenance at the same time. Before stopping them it records which ones it stopped and installs a one-time boot hook (`nekoserver-immich-snapshot-resume.service`). So if the PC loses power or the command is killed part-way, Immich is started again at the next boot. You can also run `sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py resume` to start it yourself. The record and the hook are removed only after the services are confirmed running.

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py snapshot --destination /mnt/nekoserver-media/recovery-snapshots/first-test
sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py snapshot --destination /mnt/nekoserver-media/recovery-snapshots/first-test --apply
```

The completed directory contains upload/, database.sql, compose-resolved.json and snapshot.json. All files are hashed; images are pinned by digest. An .incomplete marker means a failed snapshot and blocks restoration. Use a new name when retrying; published files are never overwritten. An interrupted snapshot records the original services to resume in its private .incomplete marker; if the process was forcibly killed, inspect and resume those services manually. If service resumption fails, the command reports failure even if the snapshot was already completed; inspect and resume the original services locally.

Copy the WHOLE completed directory to independent storage using a tool that preserves its paths and protected permissions, then run the restore preview against that copied directory to verify every byte. Keep bundles private: resolved Compose configuration includes database credentials and the SQL contains personal metadata. On a filesystem without POSIX permissions, protect access through the storage/mount settings; merely copying 0600 files is insufficient. Check that the drive you copy it to has room for the whole folder. The automatic weekly add-only photo archive copy continues separately; it does not automatically export these full snapshots.

Old standalone SQL dumps and the add-only archive cannot be passed as this matched snapshot format. Those files still preserve useful recovery material, but require a version-specific manual recovery procedure and regenerated content as appropriate.

### Rehearse the restore without replacing production

Use a fresh workspace directly under recovery-tests on either SSD/HDD, with enough capacity for both the copied upload tree and a fresh database. The database requires POSIX ownership/permissions. The default test port is 2284, while live Immich uses 2283. No existing workspace is reused, including one left by a failed test.

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py restore-test --snapshot /mnt/nekoserver-media/recovery-snapshots/first-test --workspace /srv/nekoserver/recovery-tests/first-test
sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py restore-test --snapshot /mnt/nekoserver-media/recovery-snapshots/first-test --workspace /srv/nekoserver/recovery-tests/first-test --apply --confirm RESTORE-ISOLATED
```

The tool verifies the snapshot, creates separate upload/Postgres/cache directories, generates protected Compose JSON and chooses a unique neko-restore-* project. Container/volume/network names and storage paths are independent of production. The test network has no way out (no IP masquerading), so outbound integrations are limited: its fresh ML cache may lack downloadable models, and OAuth/external services may not work during this isolated rehearsal. The published test address on this PC works. Local password login and media recovery are the acceptance focus.

Only Postgres starts before SQL import. The import is transactional and stops on errors. After table checks, the app services start and the tool checks the localhost web endpoint. Missing images can be pulled by their exact digest. No newer release is substituted. Successful software checks still return exit 1/needs_setup for manual acceptance; failures return exit 2.

Open http://127.0.0.1:2284/ on the server. Read the private restore-report.json for the project name and results. Check:

1. Existing local accounts can sign in, and representative albums/photos/videos have their expected metadata.
2. Download original files and compare their SHA-256 values with the independently preserved originals; test HEIC/RAW/Live Photo companions and large videos where present.
3. Confirm the photo/video count and review representative older/newer dates, not just thumbnails. Public table count and HTTP success do not prove complete restoration.
4. Delete only a sacrificial copy from the TEST instance and confirm the production file, permanent archive and independent backup remain unchanged.
5. Inspect the test container logs locally for migration, storage or media errors. Restart only the test project and repeat playback/file checks.

The rehearsal leaves its containers/data available for review. To stop it, use the exact project name from its report and that workspace's compose.json:

```bash
sudo docker --host unix:///var/run/docker.sock compose -p PROJECT_FROM_REPORT -f /srv/nekoserver/recovery-tests/first-test/compose.json stop
```

Do not substitute the production project name. Automatic destructive cleanup and production cutover are deliberately absent. Preserve failed workspaces for diagnosis; never delete the live Postgres directory to retry this tool.

### Replace the OS and reattach the existing store

This path supports x86_64 Arch/Nyarch, Debian/Ubuntu, Fedora, RHEL / Rocky / AlmaLinux and openSUSE (tried on AlmaLinux 10 and openSUSE Tumbleweed; not yet on Fedora) with the classic overlay2 Docker store, and a Docker client/daemon of the version that last ran the apps or newer (an older one is refused: it may not read the store). App data moves between them; host settings (the host backup) belong to the Linux they came from, so restore those only onto the same family. Converting the store to Podman is not supported. A UID/GID change, drive replacement or storage-backend migration needs a separate validated plan.

1. While the old OS works, finish checkpoint/backup preparation above and copy your settings file, `my-config.json` in the installer folder, off the OS drive. Setup also keeps a copy on the application SSD, `/srv/nekoserver/installer/releases/v0.40.3/config.json` (and, when the settings change, the earlier ones beside it as `config.previous-<date-time>.json`: the oldest always, and the newest four after it; when a rebuilt copy of the same release replaces the kept one, as after reinstalling Linux, its settings and their earlier copies move into the new one, so they are still there), next to the host checkpoint (`installer/host-checkpoint.json`) that setup records by itself; each nightly app backup keeps the Docker version in it current, as the OS's own updates move Docker on. Keep independent backups of important appdata/Compose/VM/media state; the photo snapshot covers Immich rather than every application.
2. Install the new Linux onto the system disk only (the disk Linux was on before). Keep the data SSD and HDD as they are. Recreate the desktop user with the expected UID/GID.
3. Download the installer (the same release), put your saved settings file next to `install.sh` as `my-config.json`, and run a normal setup:

```bash
sudo bash install.sh
```

It mounts the drives itself (the storage step never formats) and builds NekoServer's program for this Linux, which also replaces the copy kept on the SSD: the one the old Linux built may not start on this one (one built on Arch, Ubuntu 24.04 or Fedora does not start on Debian 12), and the reattach in step 5 needs it. Until step 5 is done, that run stops at the Docker step and points here: that is expected, it never takes over the existing app store by itself. A Btrfs pool over several drives is mounted and recognised like a single drive. Leave any copy of the SSD or HDD (made with `dd` or Clonezilla) unplugged: the drive checks refuse while two drives look the same. An encrypted drive must be listed in `/etc/crypttab` on the new Linux too; setup says which one.

If you did not save the settings file, take the copy from the SSD first. Find the SSD with `lsblk -f` (by its size or label; `FSTYPE` shows ext4, xfs or btrfs), then, in the downloaded installer's folder, mount it read-only. The extra option keeps even a drive that was not shut down cleanly from being written to (a plain `-o ro` still finishes its journal): `noload` for ext4, `norecovery` for XFS, `nologreplay` for Btrfs. A Btrfs SSD used with a subvolume (`"subvolume"` under `data_ssd` in the settings, `/@data` say) is mounted with that subvolume:

```bash
sudo mkdir -p /mnt/neko-ssd-read
sudo mount -o ro,noload /dev/sdX /mnt/neko-ssd-read             # ext4
# sudo mount -o ro,norecovery /dev/sdX /mnt/neko-ssd-read       # XFS
# sudo mount -o ro,nologreplay,subvol=/@data /dev/sdX /mnt/neko-ssd-read   # Btrfs with a subvolume (yours may have another name)
sudo cp /mnt/neko-ssd-read/installer/releases/v0.40.3/config.json my-config.json
# If setup already ran on the new Linux with new settings, that file holds those: take
# the config.previous-<date-time>.json beside it whose "storage" UUIDs are your drives'
# (the oldest one there is never removed, so the settings from before are kept).
sudo chown "$USER": my-config.json
sudo umount /mnt/neko-ssd-read
```

(Not sure which Btrfs subvolume? Mount the whole filesystem with `sudo mount -o ro,nologreplay,subvolid=5 /dev/sdX /mnt/neko-ssd-read`: each subvolume is a folder there, and the settings are in the one holding `installer/`, at `/mnt/neko-ssd-read/@data/installer/releases/v0.40.3/config.json` for `/@data`.) Then run `sudo bash install.sh` as above. Mounting only the drives, without the rest of setup, is `sudo bash install.sh --config /path/to/my-config.json --module storage --apply`; it does not replace the program kept on the SSD, so run the normal setup before step 5 anyway. Settings that lived on the old system disk are asked for again: the dashboard PIN, the Samba password, the Tailscale sign-in and the backup password (a new backup key; monthly snapshots made before still open with the password they were made with, see "App password files" below). The gateway's certificate authority is kept on the SSD, so phones that trusted it keep doing so; setup hands its files to the new Linux's caddy account.

4. Install Docker the way setup does, and keep it stopped (a Docker that starts on its own makes a fresh, empty store of its own):

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py install-docker --apply
```

It takes Docker's own packages on Debian, Ubuntu, Fedora and RHEL (from Docker's repository, which setup adds) and the system's on Arch and openSUSE, with the Compose plugin, and leaves `docker.service` and `docker.socket` stopped. It must be the version the host checkpoint names (`docker_version`) or newer. Debian's own `docker.io` is usually older (20.10 on Debian 12) and is refused: if you installed it, remove it first (`sudo apt remove docker.io`). If you installed Docker yourself, stop it before step 5: `sudo systemctl stop docker.service docker.socket`. If /var/lib/docker already contains data, recovery refuses it for review.
5. From the verified persistent release, preview and then explicitly confirm reattachment:

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py check
sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py reattach
sudo python3 /srv/nekoserver/installer/releases/v0.40.3/recover.py reattach --apply --confirm YOUR_APPLICATION_SSD_UUID
```

`check` also says when NekoServer's program kept on the SSD does not start on this Linux; then run `sudo bash install.sh` (step 3) first. The UUID must exactly match storage.data_ssd.uuid. The tool installs the mount/UUID guard before daemon configuration/startup. The existing Docker store's restart policies may start its existing containers. Failed post-start verification stops Docker and its socket. The tool preserves foreign configuration and refuses an automatic ownership or backend change, or an older Docker; after a successful takeover the host checkpoint names the Docker version now running.

6. Rerun the needed deployment profiles from the same release to recreate OS-side timers/networking/TV setup. Complete Tailscale authentication, Samba credentials and Steam login as required. Run media health checks and test application data, uploads/playback, backup jobs and reboot/missing-disk behavior.

Do not treat this store reattachment as a backup restore. Actual hardware/OS replacement, matched-snapshot restoration and independent photo retention must still pass before deleting your only originals.

### Gateway recovery

Preserve gateway configuration, ownership journals and protected Caddy CA state on the application SSD. Restore the intended account ownership explicitly; do not copy private keys to clients. Reapply the current LAN/remote profile only after drive identity, accounts and network identity are verified. See GATEWAY.md.

### Native worker selection

See RUST-RUNTIME.md before upgrading workers. The current recovery bundle includes native source, executable and build checksums. Native selection is explicit; reapply affected modules after an OS replacement to restore services and private backup identities. Acceptance checks the selected native binary and loaded commands; changing backend requires new report/reboot evidence. Real file/database restoration remains required.

### Media application state

Nightly media app snapshots and explicit checksum/new-directory restoration are documented in [APP-BACKUPS.md](APP-BACKUPS.md). They exclude Immich/PostgreSQL, media content, VM disks and host/user state. Do not run restored Compose manifests with their original production paths.

### App password files

Monthly snapshots store the account inventory encrypted (see MONTHLY-MAINTENANCE.md). To get it back, you need the backup password chosen during setup (for a key made before 0.28.5: the sudo password you had then). Restore it with:

```bash
sudo python3 backup_secrets.py --restore /ABSOLUTE/BACKUP/RUN --to /ABSOLUTE/NEW-DIRECTORY
```

A snapshot made without a backup key holds the list unencrypted in `data.tar` instead (marked *not encrypted* in the list). The easiest way to get it back from either kind is `sudo bash install.sh --recover`, then **Restore the password list**: it copies `secrets/accounts/` into a new folder and asks for the backup password only when the snapshot is encrypted.

### Rebuild Immich from the USB drive

Use this when both the SSD and the HDD are lost. The USB drive holds the photos and videos (`upload/` and `library/`), the newest weekly Immich database copies (`immich-database/immich-<date>.sql`, the newest 4 are kept), each with a `.sql.json` note of the Immich version that made it, when, and its checksum, and `immich-database/IMMICH-VERSION.txt`, which names the version of the newest copy. If the newest copy is missing, more than 8 days old or has no note, the weekly USB backup is reported as incomplete on the dashboard. Together they bring back albums, faces, people, users and settings as well as the pictures.

1. Set up NekoServer on the new drives as usual, but before the Immich step set `"immich_version"` under `"photos"` in `my-config.json` to the version in the `.sql.json` note of the copy you will restore (`IMMICH-VERSION.txt` names it for the newest copy). A database copy must be restored into the same Immich version.
2. Get the photos back with the recovery menu, not by copying the USB drive's folders as they are: the drive keeps every version of a changed file (the plain name is the first one) and NekoServer's own notes beside them. Run `sudo bash install.sh --recover`, choose **Photos and videos (Immich)**, then **The USB drive (weekly copy)**. At "Which files?" choose **As of the last backup**; on a backup made before 0.39.6 (no such question) answer **yes** to "Restore only the NEWEST version of each file?". It restores the current version of each file, checked against the backup's checksums, into a new folder on the HDD (`/mnt/nekoserver-media/restored/photos-<date-time>/` with the default folders; the menu names it). Then put its two folders into the Immich library folder, keeping their names:

```bash
sudo cp -a /mnt/nekoserver-media/restored/photos-DATE-TIME/upload /mnt/nekoserver-media/restored/photos-DATE-TIME/library /srv/nekoserver/photos/immich-library/
```

   Use the folder the menu named. When everything works (step 4), you can delete the restore folder.
3. Follow Immich's own "Backup and Restore" guide (immich.app/docs/administration/backup-and-restore) with the newest `immich-<date>.sql` from the USB drive. With NekoServer the Compose files are in `/srv/nekoserver/compose/immich/`, the project name is `neko-immich`, and the dump restores into the `immich` database (it was made with `pg_dump -d immich`).
4. Start Immich and check that albums, faces and a few photos open before using it again.

### Nextcloud's database

Since 0.39.6 the weekly USB backup first makes a copy of Nextcloud's database (users, shares, calendars, contacts and settings) with `pg_dump`. It is kept on the HDD in `/mnt/nekoserver-media/backups/nextcloud-database/` and on the USB drive in `nextcloud-database/` (the newest 4 in each place). Each copy `nextcloud-<date>.sql` has a `.sql.json` note with the Nextcloud version that made it and its checksum; `NEXTCLOUD-VERSION.txt` names the version of the newest one. The copy is made only while Nextcloud is switched on and the USB backup is set up, and never while a monthly update is unfinished. If it can't be made, or the newest is more than 8 days old, the weekly USB backup is reported as incomplete.

To put a database copy back (after losing the SSD, say), on a server where Nextcloud is set up again with the same major version (the first number in the note's version):

1. Check the copy first: `sha256sum nextcloud-DATE.sql` must print the `sha256` in its `.sql.json` note.
2. Stop Nextcloud but keep its database running:

```bash
cd /srv/nekoserver/compose/nextcloud
sudo docker compose -p neko-nextcloud -f compose.yaml --env-file .env stop nextcloud cron
```

3. Empty the database and load the copy into it:

```bash
sudo docker compose -p neko-nextcloud -f compose.yaml --env-file .env exec -T database sh -c 'dropdb -U nextcloud nextcloud && createdb -U nextcloud nextcloud'
sudo docker compose -p neko-nextcloud -f compose.yaml --env-file .env exec -T database psql -U nextcloud -d nextcloud < /path/to/nextcloud-DATE.sql
```

4. Start Nextcloud again with `sudo bash install.sh`, then put your files back (above, **Nextcloud files**) and let Nextcloud find them: `sudo docker exec -u www-data neko-nextcloud-nextcloud-1 php occ files:scan --all`.

Nextcloud's own guide (docs.nextcloud.com, "Restoring backup") has the details. Some settings Nextcloud keeps encrypted with its own secret (passwords for external storage, for example) need the old `config.php`. It is in each monthly snapshot (**App password list** in the recovery menu). This restore was not tried on a real server by NekoServer's tests: try it once before you rely on it.

Changed photo sidecars appear twice on the backup (`name.xmp` and `name.neko-<date>.xmp`); the dated one is a later edit, saved on that date. A hidden note (`.name.xmp.neko-latest`) records which copy matches the file as the backup last saw it, also after an edit was undone. The recovery wizard (`sudo bash install.sh --recover`) restores that current version of each file unless you ask for all of them. It checks every restored file against the backup's checksum ledger. A file damaged on this backup, or missing from it, is not restored: the rest comes back, and those files are named at the end and in `NOT-RESTORED.txt` in the restore folder, so you can take them from the other backup (photos are on both the HDD and the USB drive; Nextcloud files only on the USB drive). A damaged copy is still kept as it is in the restore's `damaged-as-is` folder (a photo or PDF with a few bad bytes often still opens in part; nothing checked it). Damage a restore finds is also added to the list the menu's **Repair damaged backup copies** works from (and the dashboard shows), so that option then offers to fix it, as it would after a monthly check. A saved copy you accepted as lost in the repair menu is named as such (with where its damaged copy is kept, in `neko-damaged`) and is not asked about again at each restore. A file it cannot read at all still stops the restore. See STORAGE.md, "How backups are verified".
