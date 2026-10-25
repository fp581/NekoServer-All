# Monthly overnight backup, restore test and update

## What is scheduled

The full profile installs a Rust worker and a systemd timer for the **first of each month at 02:30** in your configured `timezone` (default Asia/Jerusalem). The timezone is explicit, including daylight saving changes. Persistent catch-up is disabled: a missed night will not trigger updates at lunchtime. The worker checks its night window before starting and again before activating updates. Recovery of an interrupted job can run outside that window to bring services back.

This is an installed server timer, not a ChatGPT reminder. It operates without the dashboard or a chat session. Apply the full profile to the actual PC to enable it. No production schedule has been activated in the development workspace.

## What the monthly snapshot holds

The monthly snapshot is the software side of the server, taken just before updates so a failed update can be rolled back. It is written to the **HDD only**; the USB drive does not need to be connected (when it is, the checksum check below also covers it). Each snapshot contains:

- Selected media app configuration/state and Compose definitions (no movies, shows, music or downloads).
- Immich's and Nextcloud's cleanly stopped PostgreSQL data, application configuration and deployment files.
- The app password list, encrypted (see below).
- The exact old Docker images, in images.tar, so image IDs can be restored without relying on old registry tags still existing.
- SHA-256 archive checksums, storage identities and private resolved Compose models for recovery.

It does **not** contain the Immich photo library or Nextcloud files. Updates change the databases, not your photos or files, so rollback does not need them, and they already have their own add-only weekly copies: photos/videos on the HDD and USB drive, Nextcloud user files on the USB drive (see STORAGE.md). To rebuild Immich or Nextcloud after losing a drive, restore the database/app state from a monthly snapshot and the photos or files from those weekly copies.

Before v0.21.1 each snapshot also held the full photo library and Nextcloud files and was copied to the SSD and the USB drive. Old SSD copies are removed once the same run is on the HDD and its archives pass their checksum check. Old copies on the USB drive are kept: a file deleted from the server since then may exist only there. The weekly USB job lists them, and `sudo bash install.sh --recover` ("Old full snapshots on the USB drive") shows their dates and sizes, offers to copy them to the HDD, and removes them only if you type DELETE.

## Transaction and live tests

The monthly run does three things, always in this order: **check, back up, update**.

0. **Checksum check of your photo and file copies.** Every copy in the HDD photo archive is read and compared with its recorded checksum, and so is every copy on the USB drive when it is plugged in (the weekly copy runs then, reading everything). This finds damage that leaves a file's size and date unchanged. If anything is damaged, the run stops here: nothing is backed up or updated, the job shows as failed on the dashboard, and the file names are in its log (`journalctl -u nekoserver-maintenance`). Repair with `sudo bash install.sh --recover`. A USB drive that is not plugged in is checked in the next monthly run it is plugged in for. The hourly and weekly copies compare size and dates only, so they stay quick.
1. Verify disks, free capacity, the running selected containers, persistent mounts and local application health. The existing native media audit must also show that selected app connections are configured and passing; incomplete onboarding blocks updates.
2. Commit a private resume journal on the OS disk **before stopping writers**. Stop application/cron workers before databases and reject forced/OOM shutdowns. Refuse other containers writing into the covered state.
3. Keep production apps stopped while making consistent uncompressed archives and saving old images. Verify the HDD snapshot. This avoids accepting uploads between a backup and a potentially failed migration.
4. Extract the backup into private staging, compare extracted files against the archives, and start each restored stack under a separate Compose project/internal network with ephemeral localhost ports. Use the saved old images. Clone definitions never attach to production Docker networks or bind writable live application data. HDD movie/show/download libraries are empty read-only substitutes; covered SSD music paths use isolated restored copies. Test real HTTP endpoints. Immich and Nextcloud cannot start fully without their photos/files, which the snapshot leaves out, so for them only the restored database is started and checked by SQL for the app's own tables. Remove only the recorded rehearsal projects/volumes and temporary rehearsal data.
5. Pull candidate images from declared version streams. Nextcloud remains on its installed major; PostgreSQL and Redis retain their major tags. Immich checks the latest stable release, fetches that release's official Compose manifest, localizes its known port mapping and validates the covered mounts/services. A different major (Immich or Nextcloud) is recorded as pending, shown on the dashboard and left unchanged unless you approved it (see "Major version updates"). Unknown upstream structure blocks the update.
6. Journal the migration phase, activate candidates, and check all selected containers and HTTP endpoints, Immich SQL and Nextcloud file operations. Run the existing live media connection audit again. These are real server checks during an installed run; development tests simulate Docker while exercising the native worker, archives, copies and local HTTP.
   **Immich and Nextcloud stay closed until the update is committed.** A rollback restores their databases but not your photos and files (the snapshot leaves those out). So while they are checked, nothing may change those files. They run on a random port on this computer only, so the gateway, Tailscale and phones can't reach them. Nextcloud's cron doesn't run, and Immich answers requests but runs no background jobs. Only after every check has passed is the update committed (journal phase `committed`). Then they are restarted as usual and checked once more. If something fails after the commit, the update stands: setup or the resume service keeps opening them up and never rolls back. A rollback after clients had uploaded or deleted files would leave the database and the files disagreeing. Sonarr, Radarr and Lidarr also run checked first: they keep their usual ports (the media connection audit needs them), but see the library read-only until the commit. No import, rename or delete can change your films, shows or music while a later check could still roll their databases back. A download that finishes in that window may show as not imported in the app's Activity queue; if it doesn't go through by itself afterwards, import it from there. The other media apps already see the library read-only, and qBittorrent keeps downloading into its downloads folder, which the update doesn't roll back.
7. On success, save immutable image pins for installer reruns and reboot reproducibility. Old images are not pruned; old snapshots follow the retention rule below. On failure, stop candidates, verify/extract the matched old backup on the correct destination filesystems, quarantine the failed state, restore old files/databases and images, restart and test the old stack. The failed run remains failed in monitoring even if recovery succeeds.

**App password files.** With a backup key (setup creates one), files that hold passwords in plain text are not stored readable in monthly snapshots: the private account inventory (`secrets/accounts`, including `APP-CREDENTIALS.txt`), each app's Compose `.env` (database passwords, the Nextcloud admin password) and Nextcloud's `config.php` (database password and secret). They are left out of `data.tar` and kept only in the encrypted archive. Each snapshot holds it as `secrets.tar.cms`, encrypted with OpenSSL. The nightly job needs no password: it encrypts to a public certificate. The matching private key is protected by a **backup password** you choose once during setup, typed twice so a typo can't slip in; a copy of the locked key travels with every snapshot, so restoring needs only that password. Keys made before 0.28.5 open with the sudo password you had then (setup used to take that, checked through sudo, which a passwordless sudo rule let through unchecked). **Without a key**, for example when setup ran without a keyboard, the password files are **not** left out: they stay in `data.tar` **unencrypted** (readable by root only, on the HDD), so the snapshot can still be restored. Setup says so, each monthly run says so in its log, and the snapshot's `manifest.json` says `"password_files": "unencrypted_in_data_tar"`. Run `sudo python3 backup_secrets.py --setup` in a terminal to encrypt them from then on. Restore them with:

```bash
sudo python3 monthly-recovery.py --snapshot /ABSOLUTE/BACKUP/RUN --secrets /ABSOLUTE/NEW-DIRECTORY
```

The decrypted folder mirrors the data SSD: `secrets/accounts/`, `compose/<app>/.env` and `appdata/nextcloud/html/config/config.php`. The saved app definitions in a snapshot (`*-old.json`, `*-new.json`, `result.json`) are kept for automatic rollback during the update and have their password values blanked once it is over. Snapshots made before v0.24.2 still hold those files in plain text until monthly retention removes them (the newest 3 are kept). Without a backup key the files stay in `data.tar` unencrypted, so the snapshot remains restorable; run `sudo python3 backup_secrets.py --setup` to create one.

**Automatic rollback still works with encryption on.** While an update runs, a private copy of the left-out files (each app's `.env` and Nextcloud's `config.php`) and the Immich update's new settings are kept on the data SSD in `/srv/nekoserver/backups/maintenance-work/<run>/` (readable by root only; the same SSD already holds the live files). If the update fails, the rollback puts those files back before the old apps start, without asking for the backup password. The folder is removed when the update or its rollback is over. If that copy is missing or damaged, the rollback stops and says so; restore the files with `monthly-recovery.py --secrets` as described below.

**Retention.** `maintenance.keep_snapshots` (default 3) keeps the newest three *complete* monthly snapshots on the HDD and deletes older complete ones, both before a run (so old snapshots cannot block it for lack of space) and after a successful one. A snapshot is complete once its isolated restore test passed. An incomplete run (cut off by a power cut or a restart, or its restore test failed) is nearly a full snapshot in size; it is deleted once `keep_snapshots` complete snapshots newer than it exist, just as a complete one of its age would be, and listed in the run's `retention.json` until then. When the HDD is too full for a run, the message lists the ones still kept, with their size. Directories the worker did not create are never deleted. Set `keep_snapshots` to `0` to keep everything, as before v0.21.1.

Backup and image archive sizes determine downtime. A large library may take hours to copy and rehearse; work starts at night and can extend beyond it. If backup/testing/downloads exhaust the update window, updates are deferred and originals restart. The worker reserves space for backups, isolated extraction and rollback, and checks each drive for what goes on it: the HDD needs room for the snapshot (app data and the apps' saved images, each image counted once) and its unpacked restore test; the SSD for a rollback copy of the app data and the new images the update downloads. Each also keeps `maintenance.minimum_free_gb` (default 20) free. It stops before updating if a drive is too full. Beyond the `keep_snapshots` retention described above, it never deletes old generations. Add capacity or explicitly manage retention when needed.

Downloads/requests/uploads are unavailable while writers are stopped. Unapproved major application upgrades (see "Major version updates"), new upstream deployment structures, Pacman/Yay/OS upgrades, firmware and automatic reboot are outside this transaction. An OS rollback requires a separate OS snapshot strategy. Bazzite migration is a separate future automation. VM/KVM is not required for this project.

## Recovery and visibility

The worker shares installer/recovery.lock with deployment/snapshot work. Jobs never run concurrently against the same state: a scheduled job that finds another one running (for example the Sunday photo backup when the 1st is a Sunday, or the nightly app backup while monthly maintenance is still working) waits for it, up to 3 hours, instead of failing and skipping that night. The service's ExecStopPost and a boot resume service process the durable journal. A failed recovery retains the journal and quarantined data for inspection; it never reports a successful update. If a required drive is unavailable, reconnect the correct drive and restart the resume service. Do not delete its journal to bypass a failed recovery.

```bash
systemctl list-timers nekoserver-maintenance.timer
sudo systemctl status nekoserver-maintenance.service
sudo journalctl -u nekoserver-maintenance.service
sudo systemctl start nekoserver-maintenance-resume.service
```

The dashboard's Monthly backup and updates card and existing background alerts show the job result. Acceptance requires an enabled timer and a recent real successful transaction; unit test counts do not pass this acceptance.

For disaster recovery, choose a verified HDD backups/monthly/RUN directory. The bundled ad hoc Python helper checks every archive and can stage files without replacing live data:

```bash
sudo python3 monthly-recovery.py --snapshot /ABSOLUTE/BACKUP/RUN
sudo python3 monthly-recovery.py --snapshot /ABSOLUTE/BACKUP/RUN --stage /ABSOLUTE/NEW-PRIVATE-STAGING
sudo python3 monthly-recovery.py --snapshot /ABSOLUTE/BACKUP/RUN --load-images
```

The stage must be a fresh directory with adequate space. The helper preserves original ownership/metadata and compares extracted data. --load-images loads the saved images only; it does not start apps. Before activating a disaster restore, stop the affected live stacks, verify the saved storage paths/configuration for the replacement OS, preserve failed state, move staged data onto the correct SSD/HDD paths, put back the `.env` files and Nextcloud's `config.php` from `--secrets` (they are not in the staged data), and start each app from its staged `compose/<app>/compose.yaml` and `.env` with its project name `neko-<app>`. Startup guards still require matching confirmed mounts/UUIDs. Do not start a saved model against the wrong or empty filesystems. A real independent-drive restore on the intended PC remains required before everyday-use acceptance.

Snapshot directories and files on the HDD keep their private, root-only permissions.

Monthly update readiness distinguishes automated checks from the three explicitly unwitnessed human playback/end-to-end rows. Those rows remain pending for acceptance; healthy automated connections can proceed. Missing music credentials or real API/configuration failures still block updates.

## Major version updates

Monthly updates install new versions within the major version you have (for example Immich v2.1 to v2.2, Nextcloud 31.0.5 to 31.0.6). A new **major** version (Immich v3, Nextcloud 32) can need extra steps, so it is never installed on its own. When one is out, the dashboard shows a warning such as "Nextcloud 32 is available (you have 31)".

To install it:

1. Read the app's release notes for anything you need to do first (Immich: github.com/immich-app/immich/releases; Nextcloud: nextcloud.com/changelog).
2. In `my-config.json`, under `"maintenance"`, type the version from the dashboard warning:
   - Immich: `"immich_major_update": "v3"` (the major from the warning, with the `v`; an exact release such as `"v3.0.1"` also works).
   - Nextcloud: `"nextcloud_major_update": "32"`. Nextcloud goes up **one major at a time**: from 30 to 32, approve 31 first, and 32 after that month's update.
3. Run `sudo bash install.sh` again so the monthly job sees the change (finished steps are not repeated).

The next monthly update installs it the same safe way as any other update: snapshot first, restore test, health checks, and an automatic return to the snapshot if anything fails. You can leave the setting as it is afterwards. A big Nextcloud can take more than 5 minutes to upgrade; if the update is rolled back for taking too long, raise `"health_wait_seconds"` under `"maintenance"` (up to 1800) and run `sudo bash install.sh` again.

