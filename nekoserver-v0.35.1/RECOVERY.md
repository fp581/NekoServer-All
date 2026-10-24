# NekoServer recovery and restore rehearsal

**Easiest way:** `sudo bash install.sh --recover` opens a guided menu: photos and videos, Nextcloud files, the app password list, app settings, Steam/Samba settings, or a whole monthly snapshot. It lists the backups by date and restores into a new folder, never over what you have now. The rest of this page explains the underlying tools for advanced use.

## What each operation does

| Command | Default behavior | With --apply |
| --- | --- | --- |
| check | Check mounted UUIDs and preserved installer checksums | No apply mode |
| checkpoint | Inspect running Docker/store identity | Save private host checkpoint on SSD |
| reattach | Verify existing-store reattachment prerequisites | Configure the fresh OS and start the same store after SSD UUID confirmation |
| snapshot | Verify prerequisites and show a pause/copy/dump preview | Create a new matched Immich bundle; temporarily pause and then resume running services |
| restore-test | Check all snapshot files and preview a separate instance | Restore into fresh isolated files/database after RESTORE-ISOLATED confirmation |

These operations are separate from ordinary installation. Nothing formats drives, overwrites an existing snapshot/workspace, replaces the live Postgres directory or converts Docker to Podman. A successful check is not proof of restored photos. A snapshot stored alongside the source HDD does not protect against that HDD failing.

## Prepare while the current server works

Apply this release's foundation profile once to preserve the recovery tools and record the checkpoint. This recognizes an already correctly configured Docker foundation and does not restart it. The checkpoint supports rootful classic overlay2 Docker; other stores need a separately tested recovery plan.

```bash
sudo bash install.sh --profile foundation --apply
sudo python3 /srv/nekoserver/installer/releases/v0.35.1/recover.py check
sudo python3 /srv/nekoserver/installer/releases/v0.35.1/recover.py checkpoint --apply
```

Save the current config.json, host-checkpoint.json, installer bundle and independent backups somewhere accessible outside the OS disk before replacing the OS. Keep the configured media UID/GID and note the Docker version (the reinstalled Linux needs that version or newer). Configuration contains drive identities; application secrets belong in the protected Compose/appdata files and snapshot bundles. Do not put those secrets into ordinary logs.

## Create a matched photo snapshot

The snapshot reads the existing pinned Immich deployment and the containers it actually uses. It checks the standard upload/database mounts and matches database credentials/routing. External libraries and custom services/hardware/user settings require a manual recovery plan; the tool refuses to silently omit them.

Choose a new name directly beneath recovery-snapshots on the application SSD or media HDD. Free space must cover the full upload copy, a database size allowance and reserve. Snapshot creation can take a long time for large libraries; it keeps Immich's running non-database services paused during copy/dump. Do not perform other Immich maintenance at the same time. Before stopping them it records which ones it stopped and installs a one-time boot hook (`nekoserver-immich-snapshot-resume.service`). So if the PC loses power or the command is killed part-way, Immich is started again at the next boot. You can also run `sudo python3 /srv/nekoserver/installer/releases/v0.35.1/recover.py resume` to start it yourself. The record and the hook are removed only after the services are confirmed running.

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.35.1/recover.py snapshot --destination /mnt/nekoserver-media/recovery-snapshots/first-test
sudo python3 /srv/nekoserver/installer/releases/v0.35.1/recover.py snapshot --destination /mnt/nekoserver-media/recovery-snapshots/first-test --apply
```

The completed directory contains upload/, database.sql, compose-resolved.json and snapshot.json. All files are hashed; images are pinned by digest. An .incomplete marker means a failed snapshot and blocks restoration. Use a new name when retrying; published files are never overwritten. An interrupted snapshot records the original services to resume in its private .incomplete marker; if the process was forcibly killed, inspect and resume those services manually. If service resumption fails, the command reports failure even if the snapshot was already completed; inspect and resume the original services locally.

Copy the WHOLE completed directory to independent storage using a tool that preserves its paths and protected permissions, then run the restore preview against that copied directory to verify every byte. Keep bundles private: resolved Compose configuration includes database credentials and the SQL contains personal metadata. On a filesystem without POSIX permissions, protect access through the storage/mount settings; merely copying 0600 files is insufficient. Check free capacity before using the existing 512GB drive. The automatic weekly add-only photo archive copy continues separately; it does not automatically export these full snapshots.

Old standalone SQL dumps and the add-only archive cannot be passed as this matched snapshot format. Those files still preserve useful recovery material, but require a version-specific manual recovery procedure and regenerated content as appropriate.

## Rehearse the restore without replacing production

Use a fresh workspace directly under recovery-tests on either SSD/HDD, with enough capacity for both the copied upload tree and a fresh database. The database requires POSIX ownership/permissions. The default test port is 2284, while live Immich uses 2283. No existing workspace is reused, including one left by a failed test.

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.35.1/recover.py restore-test --snapshot /mnt/nekoserver-media/recovery-snapshots/first-test --workspace /srv/nekoserver/recovery-tests/first-test
sudo python3 /srv/nekoserver/installer/releases/v0.35.1/recover.py restore-test --snapshot /mnt/nekoserver-media/recovery-snapshots/first-test --workspace /srv/nekoserver/recovery-tests/first-test --apply --confirm RESTORE-ISOLATED
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

## Replace the OS and reattach the existing store

This path supports x86_64 Arch/Nyarch, Debian/Ubuntu, Fedora and RHEL / Rocky / AlmaLinux (tried on AlmaLinux 10; not yet on Fedora) with the classic overlay2 Docker store, and a Docker client/daemon of the version that last ran the apps or newer (an older one is refused: it may not read the store). App data moves between them; host settings (the host backup) belong to the Linux they came from, so restore those only onto the same family. Bazzite/Podman conversion is blocked. A UID/GID change, drive replacement or storage-backend migration needs a separate validated plan.

1. While the old OS works, finish checkpoint/backup preparation above and copy your settings file, `my-config.json` in the installer folder, off the OS drive. Setup also keeps a copy on the application SSD, `/srv/nekoserver/installer/releases/v0.35.1/config.json`, next to the host checkpoint (`installer/host-checkpoint.json`) that setup records by itself; each nightly app backup keeps the Docker version in it current, as the OS's own updates move Docker on. Keep independent backups of important appdata/Compose/VM/media state; the photo snapshot covers Immich rather than every application.
2. Install the new OS onto the 256GB OS SSD only. Keep data SSD/HDD contents intact. Recreate the desktop user with the expected UID/GID.
3. From the downloaded installer (the same release) and the saved settings file, mount the existing identified filesystems with the storage module. This never formats:

```bash
sudo bash install.sh --config /path/to/my-config.json --module storage --apply
```

If you did not save the settings file, take the copy from the SSD. Mounting it read-only writes nothing to it. Find the SSD with `lsblk -f` (by its size or label), then, in the downloaded installer's folder:

```bash
sudo mkdir -p /mnt/neko-ssd-read
sudo mount -o ro /dev/sdX /mnt/neko-ssd-read
sudo cp /mnt/neko-ssd-read/installer/releases/v0.35.1/config.json my-config.json
sudo chown "$USER": my-config.json
sudo umount /mnt/neko-ssd-read
```

With `my-config.json` next to `install.sh`, a normal `sudo bash install.sh` mounts the drives itself (the storage step never formats). Until step 5 is done, that run stops at the Docker step and points here: it never takes over the existing app store by itself. Settings that lived on the old system disk are asked for again: the dashboard PIN, the Samba password, the Tailscale sign-in and the backup password (a new backup key; monthly snapshots made before still open with the password they were made with, see "App password files" below). The gateway's certificate authority is kept on the SSD, so phones that trusted it keep doing so; setup hands its files to the new Linux's caddy account.

4. Install the native Docker client/daemon and Compose plugin through trusted packages: the version the host checkpoint names (`docker_version`) or newer, which is what a freshly installed Linux usually has. Keep Docker service AND socket stopped. Do not start a fresh default store. If /var/lib/docker already contains data, recovery refuses it for review.
5. From the verified persistent release, preview and then explicitly confirm reattachment:

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.35.1/recover.py check
sudo python3 /srv/nekoserver/installer/releases/v0.35.1/recover.py reattach
sudo python3 /srv/nekoserver/installer/releases/v0.35.1/recover.py reattach --apply --confirm YOUR_APPLICATION_SSD_UUID
```

The UUID must exactly match storage.data_ssd.uuid. The tool installs the mount/UUID guard before daemon configuration/startup. The existing Docker store's restart policies may start its existing containers. Failed post-start verification stops Docker and its socket. The tool preserves foreign configuration and refuses an automatic ownership or backend change, or an older Docker; after a successful takeover the host checkpoint names the Docker version now running.

6. Rerun the needed deployment profiles from the same release to recreate OS-side timers/networking/TV setup. Complete Tailscale authentication, Samba credentials and Steam login as required. Run media health checks and test application data, uploads/playback, backup jobs and reboot/missing-disk behavior.

Do not treat this store reattachment as a backup restore or a proven Bazzite migration. Actual hardware/OS replacement, matched-snapshot restoration and independent photo retention must still pass before deleting your only originals.

## Gateway recovery

Preserve gateway configuration, ownership journals and protected Caddy CA state on the application SSD. Restore the intended account ownership explicitly; do not copy private keys to clients. Reapply the current LAN/remote profile only after drive identity, accounts and network identity are verified. See GATEWAY.md. Automatic Bazzite recovery remains unsupported.

## Native worker selection

See RUST-RUNTIME.md before upgrading workers. The current recovery bundle includes native source, executable and build checksums. Native selection is explicit; reapply affected modules after an OS replacement to restore services and private backup identities. Acceptance checks the selected native binary and loaded commands; changing backend requires new report/reboot evidence. Real file/database restoration remains required.

## Media application state

Nightly media app snapshots and explicit checksum/new-directory restoration are documented in [APP-BACKUPS.md](APP-BACKUPS.md). They exclude Immich/PostgreSQL, media content, VM disks and host/user state. Do not run restored Compose manifests with their original production paths.

## App password files

Monthly snapshots store the account inventory encrypted (see MONTHLY-MAINTENANCE.md). To get it back, you need the backup password chosen during setup (for a key made before 0.28.5: the sudo password you had then). Restore it with:

```bash
sudo python3 backup_secrets.py --restore /ABSOLUTE/BACKUP/RUN --to /ABSOLUTE/NEW-DIRECTORY
```

A snapshot made without a backup key holds the list unencrypted in `data.tar` instead (marked *not encrypted* in the list). The easiest way to get it back from either kind is `sudo bash install.sh --recover`, then **Restore the password list**: it copies `secrets/accounts/` into a new folder and asks for the backup password only when the snapshot is encrypted.

## Rebuild Immich from the USB drive

Use this when both the SSD and the HDD are lost. The USB drive holds the photos and videos (`upload/` and `library/`), the newest weekly Immich database copies (`immich-database/immich-<date>.sql`, the newest 4 are kept), each with a `.sql.json` note of the Immich version that made it, when, and its checksum, and `immich-database/IMMICH-VERSION.txt`, which names the version of the newest copy. If the newest copy is missing, more than 8 days old or has no note, the weekly USB backup is reported as incomplete on the dashboard. Together they bring back albums, faces, people, users and settings as well as the pictures.

1. Set up NekoServer on the new drives as usual, but before the Immich step set `"immich_version"` under `"photos"` in `my-config.json` to the version in the `.sql.json` note of the copy you will restore (`IMMICH-VERSION.txt` names it for the newest copy). A database copy must be restored into the same Immich version.
2. Copy `upload/` and `library/` from the USB drive into the new Immich library folder (`/srv/nekoserver/photos/immich-library/`), keeping those two folder names.
3. Follow Immich's own "Backup and Restore" guide (immich.app/docs/administration/backup-and-restore) with the newest `immich-<date>.sql` from the USB drive. With NekoServer the Compose files are in `/srv/nekoserver/compose/immich/`, the project name is `neko-immich`, and the dump restores into the `immich` database (it was made with `pg_dump -d immich`).
4. Start Immich and check that albums, faces and a few photos open before using it again.

Changed photo sidecars appear twice on the backup (`name.xmp` and `name.neko-<date>.xmp`); the dated one is a later edit, saved on that date. A hidden note (`.name.xmp.neko-latest`) records which copy matches the file as the backup last saw it, also after an edit was undone. The recovery wizard (`sudo bash install.sh --recover`) restores that current version of each file unless you ask for all of them. It checks every restored file against the backup's checksum ledger and stops at the first damaged or unreadable file; if that happens, restore from the other backup (HDD or USB). See STORAGE.md, "How backups are verified".
