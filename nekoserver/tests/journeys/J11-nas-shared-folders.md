# J11 The media drive and the backup drive as folders a NAS shares (NFS)

```
ID: J11
Purpose: A NAS that shares folders over NFS can hold the media drive and the backup drive;
  a folder that would lose owners, permissions or locks is refused with what to change;
  a NAS that is off never keeps the PC from starting
Preconditions: NekoDebian VM at clean-debian13-desktop; the SSD connected; an NFS server on
  the test network with three exports: media (rw, no_root_squash), backup (rw,
  no_root_squash) and squashed (rw, root_squash)
Actions: switch on "My media and backup drives are in a network storage box (NAS)"; give the
  server's address; (a) choose the squashed export for the media drive; (b) run setup again
  and choose the media and backup exports; (c) run the weekly copy; (d) restart the VM with
  the NFS server stopped, then start the server
Expected: see below
Evidence: the setup log; my-config.json ("network_storage", "storage", "photos");
  /etc/fstab; findmnt; systemctl status of the mount and automount units;
  /srv/nekoserver/reports/jobs/external.json; journalctl -u nekoserver-nas-watch -u
  nekoserver-drive-return; the dashboard's Drives page
Cleanup: restore the post-install snapshot; stop the NFS server
```

## Expected results

**Finding and choosing**

- Setup says what answers at the address: iSCSI, NFS or both, and asks only when both do
  (`chosen_kind`, modules/network_storage.py:737; `test_only_nfs_answers`,
  tests/test_nfs_storage.py:336; `test_both_answer_and_the_person_chooses`,
  tests/test_nfs_storage.py:348). A NAS chosen before 0.50.0 stays on iSCSI and is asked
  nothing (`test_a_nas_chosen_before_stays_iscsi_and_nobody_is_asked`,
  tests/test_nfs_storage.py:329).
- The NFS tools are installed only when missing, by this Linux's package name
  (`test_each_linux_names_the_nfs_tools`, tests/test_nfs_storage.py:111;
  `test_the_tools_are_installed_only_when_missing`, tests/test_nfs_storage.py:116); the
  iSCSI tools are not installed.
- The exports are listed with who may use them, and each drive's folder is chosen by its
  number or typed; a NAS that gives no list has the folder typed
  (`test_the_first_run_finds_asks_tries_and_keeps_both_folders`, tests/test_nfs_storage.py:404;
  `test_a_nas_with_no_list_has_the_folder_typed`, tests/test_nfs_storage.py:446). The SSD is
  never asked for and never accepted (`test_the_apps_ssd_is_never_a_shared_folder`,
  tests/test_nfs_storage.py:533). Setup says NFS is slower.

**(a) A folder that cannot be used**

- The squashed export stops the step with "turns this PC's administrator (root) into a
  guest", the names the setting has on a NAS, and "Nothing was changed"; nothing is in
  `/etc/fstab`, nothing is mounted, `storage.media_hdd` is not saved
  (`test_root_squash_is_refused_with_its_names_on_a_nas`, tests/test_nfs_storage.py:197;
  `test_a_folder_that_cannot_be_used_stops_setup_with_the_reason`, tests/test_nfs_storage.py:467).
  The other refusals have tests of their own: read-only access, owners or permissions not
  kept, locks, a size that makes no sense, a NAS that will not mount the folder
  (tests/test_nfs_storage.py:188 to 289).

**(b) Two folders that work**

- The log says each folder was "tried out as root and as the apps' account", with its NFS
  version and free space. `my-config.json` keeps the folders under `network_storage.nfs`,
  the media folder as `storage.media_hdd` (`"network": {"transport": "nfs", "export": ...}`,
  `"uuid": "nfs-..."`) and the backup folder under `photos.external_backup_*`.
- `/etc/fstab` has one line for the media folder, by its address, with `_netdev`, `nofail`,
  `x-systemd.automount` and `hard` (`test_the_line_in_fstab_never_holds_up_the_start`,
  tests/test_nfs_storage.py:79; `test_its_line_is_written_beside_the_ssds`,
  tests/test_nfs_storage.py:578); `findmnt /mnt/nekoserver-media` shows the export.
- Setup finishes as J01 does: the apps run with their library on the folder, files there
  belong to the apps' account, and the drive check passes
  (`a_folder_the_nas_shares_is_checked_by_its_address`, runtime/src/backup.rs:4800).
- The dashboard's Drives page shows the media drive as on the network storage box, with
  its space and "takes writes"; no SMART reading is claimed for it.

**(c) The weekly copy**

- It mounts the backup folder by its address, copies, and unmounts it;
  `reports/jobs/external.json` says `pass` (`test_the_weekly_copy_is_told_the_folders_address`,
  tests/test_nfs_storage.py:667). A restore from the recovery menu mounts it read-only the
  same way (`test_the_recovery_menu_opens_the_backup_folder_by_its_address`,
  tests/test_nfs_storage.py:682).

**(d) The NAS off at start, then on**

- The PC starts and reaches its desktop without waiting for the NAS; Docker and the apps
  are not running; the dashboard shows the media drive as missing.
- Within about two minutes of the NFS server answering again, the NAS check starts drive
  return, the folder is mounted and the apps run again, with no command typed
  (`a_mounted_shared_folder_is_never_released_and_an_automount_is_no_mount`,
  runtime/src/drive_return.rs:547; `test_drive_return_waits_for_no_device_behind_a_shared_folder`,
  tests/test_nfs_storage.py:676).

## Manual parts

- On a real NAS box: finding the squash setting under the name its page gives it.
- Timing a film's start from the folder against a drive on this PC, for the "slower" note.
