# v0.18.0 schedule policy

Routine NekoServer media checkups are disabled, music audio has no automatic backup, and managed photo originals/database/external backups run weekly on Sunday night. Monthly update/recovery checks and app-state backups remain. [STORAGE.md](STORAGE.md) defines the current policy and supersedes older schedule/coverage descriptions below.

# v0.17.0 storage update

The current layout is defined in [STORAGE.md](STORAGE.md): music and Immich live data on SSD, independent HDD backups, movies/shows only on HDD, and a verified 60-minute HDD idle timer. This supersedes older layout statements below. Existing deployments with old host paths are preserved and require a verified migration.

# Monthly overnight backup, restore test and update

## What is scheduled

The full profile installs a Rust worker and a systemd timer for the **first of each month at 02:30 Asia/Jerusalem**. This timezone is explicit, including daylight saving changes. Persistent catch-up is disabled: a missed night will not trigger updates at lunchtime. The worker checks its night window before starting and again before activating updates. Recovery of an interrupted job can run outside that window to bring services back.

This is an installed server timer, not a ChatGPT reminder. It operates without the dashboard or a chat session. Apply the full profile to the actual PC to enable it. No production schedule has been activated in the development workspace.

## Three physical drives

The SSD holds the live Immich library and music; the HDD holds Nextcloud files and movie/show libraries. Every monthly transaction additionally creates a full, consistent snapshot on the HDD and checksum-verifies copies of that snapshot on the SSD and external USB drive. Each copy contains:

- Selected media app configuration/state and Compose definitions and SSD music files (HDD movie/show files are excluded).
- Immich's original asset tree, its cleanly stopped PostgreSQL data, deployment configuration and secrets.
- Nextcloud's cleanly stopped PostgreSQL data, application/configuration/custom apps and HDD file data.
- The exact old Docker images, in images.tar, so image IDs can be restored after an SSD failure without relying on old registry tags still existing.
- SHA-256 archive checksums, storage identities and private resolved Compose models for recovery.

The live photo tree is on SSD. Independent full recovery snapshots are **monthly**, not instantaneous. HDD add-only original preservation runs every six hours; daily SQL copying and weekly external photo-archive copying are retained. Those photo-only copies are useful independent original files; they do not by themselves recreate Immich accounts/albums/database state. HDD working data plus an HDD snapshot still share one physical disk failure domain: there are three physical drives, not four independent drives. Copies are never described as a guarantee against every failure.

External UUID and filesystem come from the confirmed photo-backup selection. The worker mounts an unmounted attached target with restricted options, validates the exact UUID, and confirms all three UUIDs belong to different physical disks. Already-mounted wrong targets are refused. exFAT backup storage must be mounted root-only (uid=0,gid=0,umask=077), because full archives contain private credentials; a permissive existing mount is not silently remounted. Mount/UUID checks are repeated before critical writes. No disk is formatted, no old backups are overwritten/deleted and no existing data is automatically moved.

## Transaction and live tests

1. Verify disks, free capacity, the running selected containers, persistent mounts and local application health. The existing native media audit must also show that selected app connections are configured and passing; incomplete onboarding blocks updates.
2. Commit a private resume journal on the OS disk **before stopping writers**. Stop application/cron workers before databases and reject forced/OOM shutdowns. Refuse other containers writing into the covered state.
3. Keep production apps stopped while making consistent uncompressed archives and saving old images. Verify the HDD snapshot and both independent copies. This avoids accepting uploads between a backup and a potentially failed migration.
4. Extract the backup into private staging, compare extracted files against the archives, and start each restored stack under a separate Compose project/internal network with ephemeral localhost ports. Use the saved old images. Clone definitions never attach to production Docker networks or bind writable live application data. HDD movie/show/download libraries are empty read-only substitutes; covered SSD music paths use isolated restored copies. Test real HTTP endpoints; execute SQL in restored Immich; perform a database-backed file round trip in restored Nextcloud. Remove only the recorded rehearsal projects/volumes and temporary rehearsal data.
5. Pull candidate images from declared version streams. Nextcloud remains on its installed major; PostgreSQL and Redis retain their major tags. Immich checks the latest stable release, fetches that release's official Compose manifest, localizes its known port mapping and validates the covered mounts/services. A different major is recorded as pending and left unchanged. Unknown upstream structure blocks the update.
6. Journal the migration phase, activate candidates, and check all selected containers and HTTP endpoints, Immich SQL and Nextcloud file operations. Run the existing live media connection audit again. These are real server checks during an installed run; development tests simulate Docker while exercising the native worker, archives, copies and local HTTP.
7. On success, save immutable image pins for installer reruns and reboot reproducibility. Do not prune the old images or backups. On failure, stop candidates, verify/extract the matched old backup on the correct destination filesystems, quarantine the failed state, restore old files/databases and images, restart and test the old stack. The failed run remains failed in monitoring even if recovery succeeds.

Backup and image archive sizes determine downtime. A large library may take hours to copy and rehearse; work starts at night and can extend beyond it. If backup/testing/downloads exhaust the update window, updates are deferred and originals restart. The worker reserves space for backups, isolated extraction and rollback; it stops before updating if the drive is too full. It never automatically deletes old generations. Add capacity or explicitly manage retention when needed.

Downloads/requests/uploads are unavailable while writers are stopped. Major application upgrades, new upstream deployment structures, Pacman/Yay/OS upgrades, firmware and automatic reboot are outside this transaction. An OS rollback requires a separate OS snapshot strategy. Bazzite migration is a separate future automation. VM/KVM is not required for this project.

## Recovery and visibility

The worker shares installer/recovery.lock with deployment/snapshot work. Conflicting jobs fail safely; they never run concurrently against the same state. The service's ExecStopPost and a boot resume service process the durable journal. A failed recovery retains the journal and quarantined data for inspection; it never reports a successful update. If a required drive is unavailable, reconnect the correct drive and restart the resume service. Do not delete its journal to bypass a failed recovery.

```bash
systemctl list-timers nekoserver-maintenance.timer
sudo systemctl status nekoserver-maintenance.service
sudo journalctl -u nekoserver-maintenance.service
sudo systemctl start nekoserver-maintenance-resume.service
```

The dashboard's Monthly backup and updates card and existing background alerts show the job result. Acceptance requires an enabled timer and a recent real successful transaction; unit test counts do not pass this acceptance.

For independent disaster recovery, choose any verified drive's backups/monthly/RUN directory. The bundled ad hoc Python helper checks every archive and can stage files without replacing live data:

```bash
sudo python3 monthly-recovery.py --snapshot /ABSOLUTE/BACKUP/RUN
sudo python3 monthly-recovery.py --snapshot /ABSOLUTE/BACKUP/RUN --stage /ABSOLUTE/NEW-PRIVATE-STAGING
sudo python3 monthly-recovery.py --snapshot /ABSOLUTE/BACKUP/RUN --load-images
```

The stage must be a fresh directory with adequate space. The helper preserves original ownership/metadata and compares extracted data. --load-images loads the saved images only; it does not start apps. Before activating a disaster restore, stop the affected live stacks, verify the saved storage paths/configuration for the replacement OS, preserve failed state, move staged data onto the correct SSD/HDD paths and use the saved *-old.json Compose models with their recorded project names. Startup guards still require matching confirmed mounts/UUIDs. Do not start a saved model against the wrong or empty filesystems. A real independent-drive restore on the intended PC remains required before everyday-use acceptance.

For exFAT, unmount GUI/alternate mount aliases before the private monthly mount. exFAT permissions are mount options, so a second permissive view could otherwise expose archive contents; the worker refuses that situation. With Unix filesystems, snapshot directories and files retain their private permissions.

In v0.16.1, monthly update readiness distinguishes automated checks from the three explicitly unwitnessed human playback/end-to-end rows. Those rows remain pending for acceptance; healthy automated connections can proceed. Missing music credentials or real API/configuration failures still block updates.
