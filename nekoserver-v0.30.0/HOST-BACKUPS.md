# Host, Steam and Samba backups

This phase adds a Rust nightly `host_backup` worker, separate from media application and photo jobs. Python configures the timer and performs ad hoc verification/staged restoration. New full installations select the module. Upgrades preserve customized profiles: select it explicitly.

```bash
python3 installer.py --module host_backup --check
sudo bash install.sh --module host_backup --apply
sudo systemctl start nekoserver-host-backup.service
systemctl status nekoserver-host-backup.timer
```

## Scope and setup

Default schedule: 05:15, with missed runs caught up after boot. `host_backup.user` identifies an existing non-root desktop account (`auto` uses the sudo/TV account, then the configured media UID, the first time; after that the same account stays chosen, even when another administrator runs setup. Name the account in `host_backup.user` to change it). Paths/identity are resolved during setup and retained in the private guard. Reapply after moving Steam or adding an external Steam library. **Steam left open** (for Remote Play, say): Steam's files are only copied while Steam is closed, including running games, Wine and Proton, because they change while it runs. On a night Steam is open, everything else (system settings, Samba and the rest) is still backed up, and Steam's files are left out: the run shows as *partial* on the dashboard with "Steam was open", and the snapshot's manifest lists them under `left_out`. They are backed up on the next night Steam is closed. Until then, the newest snapshot that does have them is never deleted by `keep_snapshots` (it is kept on top of that number), so a run of nights with Steam open can't remove the last copy of your saves. The worker never closes Steam, a game or a session, and never logs into Steam. If Steam is started in the middle of a run that is copying its files, that run fails and the next one tries again. A failed process check also fails the run.

Included, when present:

- Selected OS identity/mount/locale/package-manager settings (pacman's on Arch/Nyarch; apt's sources, keys and preferences plus `/etc/default/locale` and `/etc/default/keyboard` on Debian/Ubuntu) and user/group ID references (not shadow passwords).
- Docker daemon settings, SSD gateway state/CA and NekoServer power/notification/gateway/backup settings and installed NekoServer service definitions/drop-ins.
- Dashboard settings, fan curves, lighting choices and the fan-and-light PIN hash (`/etc/nekoserver-dashboard`, `/etc/nekoserver-control`), OpenRGB's settings (`/var/lib/nekoserver-openrgb`) and the i2c driver setting for memory and motherboard lighting. Every setup run brings an existing list of places up to date, so upgrading adds these too.
- Preserved SSD installer release bundles, so a standalone host backup also retains the recovery code/configuration.
- Native Steam's config, userdata and the default library's compatdata. Flatpak Steam, additional Steam libraries, native-game save folders and mods/game files outside these folders require explicit extra paths. Downloadable game binaries are not copied by default.
- `/etc/samba` and `/var/lib/samba`, for the managed standalone Samba installation. The worker refuses unmanaged Samba configuration or detected AD/DC, winbind and NetBIOS writers. At runtime, testparm must confirm state/private database directories inside these trees and the tdbsam backend; other locations/backends are refused.

Optional absent paths are recorded in the private manifest by source ID. A successful copy does not mean an absent Steam/Samba installation or every game save was backed up. Review `sources` and `missing_optional` during acceptance. Extra required paths must exist; missing required paths fail the run.

Set `host_backup.extra_paths` to explicit absolute save/share directories inside the selected user's home or verified SSD/HDD. Paths under the backup tree and source roots containing the destination are refused. This is how custom game saves and Samba share **contents** are included; Samba account/config backup does not include every exposed file automatically. For example:

```json
"host_backup": {
  "time": "05:15",
  "user": "lidor",
  "steam": true,
  "samba": true,
  "extra_paths": ["/home/lidor/.local/share/MyGame", "/mnt/nekoserver-media/documents"],
  "minimum_free_gb": 2
}
```

Use actual existing paths; these are examples. Files changed during enumeration/copy, special files, hardlinks, privileged file modes and nested filesystems are refused. Other actively written applications need their own coordinated backup method; arbitrary extras are file snapshots, not transactional application/database backups. A partial failed copy is retained as a hidden `.partial` directory, never published as a completed snapshot.

## Storage, links and availability

Completed snapshots are private, timestamped directories under `<media_mount>/backups/host-user`. Original UID/GID/modes are inventory metadata; ACLs, extended attributes and security labels are not preserved, so ACL-dependent share extras require a separate metadata-aware backup. snapshot directories/files are 0700/0600. Snapshots can contain account databases, API keys and authentication tokens. Do not expose this tree through Samba or the web gateway. Existing public backup roots are refused rather than silently changing permissions.

Files have SHA-256 checksums; both configured drive identities are checked before work and again before publication. Space is checked against payload size plus the configured reserve. Concurrent application/recovery jobs share the existing recovery lock. A Samba resume journal is committed before stopping smb.service. Completion and failure attempt to restart it only if it was originally running; failed restarts retain the journal. ExecStopPost and a boot recovery unit retry after interrupted work. Unsupported/live writer probes fail closed. SMB clients can be interrupted during the copy.

Proton and user data may contain symlinks. The worker records their targets as **inert text files**, never reads through them. Restoration keeps them inert; the report retains their mapping. Review/recreate appropriate links manually when activating a restored prefix. This avoids following a game/user link into unrelated live OS data. The staged prefix is not immediately runnable.

The newest `host_backup.keep_snapshots` complete snapshots (default 7) are kept and older ones deleted after each successful run; set it to `0` to keep everything, as before v0.21.1. Sockets, FIFOs, devices and nested filesystems are skipped and listed under `skipped` in the manifest instead of failing the backup. A snapshot on the media HDD protects SSD/OS data against their loss, but cannot protect the HDD against its own failure. Copies of HDD share contents on that same HDD are historical copies, not an independent backup. Weekly external photo copying still covers photos only; copy these snapshots to a suitably sized independent device/location separately. This release does not silently fill the 512GB photo disk with Steam/share snapshots.

## Verify and rehearse restoration

```bash
sudo python3 host-recovery.py verify --snapshot /mnt/nekoserver-media/backups/host-user/snapshot-EXAMPLE
sudo python3 host-recovery.py restore --snapshot /mnt/nekoserver-media/backups/host-user/snapshot-EXAMPLE --destination /srv/host-restore-rehearsal
```

The destination must be a new absolute directory with an existing parent. The tool verifies every inventoried file and rejects unlisted data/path traversal/links/tampering. Sources are restored under `sourceN` IDs; `restore-report.json` maps them to original paths, records missing optional paths and states that links are inert. Default restored permissions are private. Every file and folder gets back the time it was last changed (recorded in the snapshot), so synchronisation tools and age comparisons don't see restored files as new. `--preserve-owners` is optional, requires root and applies recorded UID/GID/modes to staged data only; retained link text stays 0600.

No live `/etc`, home directory, Samba database or Docker data is overwritten, and no service/container is started. Inspect staged configuration before adopting it. Do not replace `/etc/passwd` or `/etc/group` wholesale on a new OS: reconcile usernames and numeric IDs. Host settings belong to the Linux family they came from (Arch/Nyarch or Debian/Ubuntu); don't adopt one family's package-manager settings on the other. Samba restore requires the matching standalone Samba version/configuration, stopped services, reconciled users/IDs, restored database permissions and a real client login test. Steam restore requires the selected user, reviewed links, game/save compatibility and an actual game-load test. Rehearse those on an independent installation before relying on the backup.

Run `sudo python3 acceptance.py --profile full --save` after a successful job/reboot and complete `host_restore`. Fixture results establish checksum/staging and restart behavior, not a live restored OS, Steam game or Samba login.

References: [Steam Cloud/data locations](https://help.steampowered.com/en/faqs/view/68D2-35AB-09A9-7678), [Samba database handling](https://www.samba.org/samba/docs/old/Samba3-HOWTO/tdb.html).
