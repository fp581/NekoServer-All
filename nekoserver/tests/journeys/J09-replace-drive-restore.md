# J09 Replace a drive and restore onto a separate system

```
ID: J09
Purpose: Everything comes back from the backups on a different PC, byte for byte, and a replaced
  drive is never mistaken for the old one
Preconditions: a finished install with data (adddata.py, its checksums in /root/r18-data.json);
  a copy of the USB drive's contents and one monthly snapshot folder saved aside;
  a second VM (or the same VM from clean-debian13-desktop) with blank drives
Actions: fresh install on the clean VM (J01); put the old USB contents on the new USB drive and
  the snapshot under backups/monthly (rig restore-r14.sh); sudo bash install.sh --recover:
  menu 1 photos, 2 Nextcloud files (one user), 4 app settings, 6 monthly snapshot;
  then reformat the HDD (rig hdd-replaced.sh) and run setup again.
  Since 1.0.0 the gate runs three drills from an installed checkpoint with test data (a few
  photos and an album, a Nextcloud file, a short film, a changed app setting) after its
  nightly backup, monthly snapshot and weekly USB copy: both data drives lost, the SSD lost,
  the HDD lost (below), each ending with the restored data opened in the apps
Expected: see below
Evidence: the recovery menu's output; the checksum comparison against r18-data.json; the
  refusal line at the drives step; the new settings' storage section
Cleanup: restore the post-install snapshot; remove the restore folders
```

## Expected results

Recovery menu (`MENU`, recovery_wizard.py:68; RECOVERY.md:33):

- Photos: every test photo restored from the USB drive matches its recorded checksum;
  restores copy into a new folder only and never inside a backup
  (`test_photos_restore_copies_into_a_new_folder_only`, tests/test_wizards.py:753;
  `test_a_restore_never_goes_inside_a_backup`, tests/test_wizards.py:789); files are
  checked against `.neko-ledger.json` and a damaged one is left out and named
  (`test_restored_files_are_checked_against_their_recorded_checksums`, tests/test_wizards.py:1003;
  `test_a_damaged_backup_file_is_left_out_and_named_and_the_rest_restored`, tests/test_wizards.py:1012).
- Nextcloud files: the menu asks `Restore whose Nextcloud files?` with the users read from
  the USB drive (`test_nextcloud_restore_lists_users_from_the_usb`, tests/test_wizards.py:812);
  the newest version of each file comes back under its own name
  (`test_newest_version_is_restored_under_its_own_name`, tests/test_wizards.py:859); "as of"
  a date restores the content it had then (`test_each_date_restores_the_content_it_had_then`,
  tests/test_restore_as_of.py:281).
- App settings: the nightly snapshot is verified before and after, tampering is caught,
  and no existing folder is reused (`test_app_snapshot_verifies_restores_and_catches_tampering`,
  tests/test_snapshot_restore.py:50; `test_restore_never_reuses_an_existing_folder`, tests/test_snapshot_restore.py:180).
- Monthly snapshot: unpacked, checked and marked; an archive changed after its check is
  never unpacked (`test_a_snapshot_is_unpacked_checked_and_marked`, tests/test_monthly_recovery.py:68;
  `test_an_archive_changed_after_its_check_is_never_unpacked_or_marked`, tests/test_monthly_recovery.py:106);
  the menu says where the password files are (`test_the_monthly_restore_says_where_the_password_files_are`,
  tests/test_wizards.py:1433).
- A verified file that is missing stops the restore and is named
  (`test_a_restore_stops_when_a_verified_file_is_missing`, tests/test_wizards.py:1450); with
  the HDD missing nothing is written to the system disk
  (`test_a_restore_never_writes_onto_the_system_disk_when_the_hdd_is_missing`, tests/test_wizards.py:1544).

Drive replacement: setup never takes another drive in the old one's place by itself. With
a blank new drive it says the old drive is not connected and asks for the new one on the
storage page (RECOVERY.md "The SSD or the HDD broke"); a drive the settings name that is
there but is not the one they name is refused with the runtime's own reason (`test_a_refusal_says_the_runtimes_own_reason`,
tests/test_storage_identity.py:170); once the old UUID is taken out, the replacement keeps
nothing of the old drive and the size is checked before mounting or saving
(`test_a_replacement_drive_keeps_nothing_of_the_old_one`, tests/test_setup_round.py:685;
`test_the_size_is_checked_before_mounting_writing_fstab_or_saving`, tests/test_setup_round.py:743).
The OS reinstall path follows RECOVERY.md:144.

Both the SSD and the HDD lost (a new system disk, new blank SSD and HDD, a copy of the old USB
drive), RECOVERY.md "Both the SSD and the HDD are lost":

- The photos and the Immich database copy come back from the USB drive (the copy loaded as
  root, `sudo -i` first: its folders are root's), and Immich shows the photos and the album
  with the old login.
- The password list comes back with the old server's backup password
  (`RebuiltServerPasswordWordsTests`, tests/test_rebuilt_server_password_words.py).
- The app settings come back from the USB drive's copy (since 1.0.0 the weekly USB copy
  carries the newest app settings, sealed for the backup key:
  `the_newest_app_backup_goes_onto_the_usb_drive_sealed_for_the_backup_key`,
  runtime/src/usb_app_settings.rs; `test_the_menu_reads_the_copy_the_weekly_job_writes`,
  tests/test_usb_app_settings.py), and APP-BACKUPS.md's copy-back puts each app's settings
  back as they were; Seerr goes back with Jellyfin, Sonarr and Radarr
  (`SeerrLinkedToAnotherJellyfinTests`, tests/test_jellyfin_requests.py).
- Nextcloud's database copy loads as the account Nextcloud uses
  (`test_the_copy_is_loaded_as_the_account_nextcloud_uses`, tests/test_docs_follow_setup.py);
  setup then makes the folder of each restored account the new drive lacks, so uploads work
  (`test_accounts_the_restored_database_lists_get_their_missing_folders`, tests/test_review_fixes.py);
  the files come back from the USB drive and a new upload is taken.
- The next nightly backup, host snapshot, Immich database copy and USB copy all pass.

The SSD lost, with Nextcloud on:

- Setup on the new SSD finds this server's own Nextcloud backup on the HDD, the newest of
  the nightly backup (Nextcloud is in it from the first night since 1.0.0), the weekly
  database copy and the monthly snapshot, and asks `Bring Nextcloud back from the backup of
  <date>? [yes/no]`, with no default (`YesRestoresTests`, `NightlyBackupTests`,
  tests/test_nextcloud_restore.py).
- Yes brings back the settings, the program and the database, asks the backup password when
  the backup is sealed, and leaves the files folder as it was; Nextcloud answers with the
  old login and takes an upload. Image pins this PC does not have are taken out first
  (`PinnedImagesTests`, tests/test_nextcloud_restore.py). A step that fails puts the SSD back
  as it was (`FailedStepTests`).
- No keeps the refusal and changes nothing (`NoKeepsTheRefusalTests`); another server's
  backup is never offered (`ForeignBackupTests`); before the first night there is nothing to
  offer and setup says so (`YoungServerTests`).
- Setup names the backup password in force when it keeps the backup key
  (`test_a_kept_key_says_which_password_is_in_force_also_on_new_drives`,
  tests/test_backup_secrets_words.py).

The HDD lost, with the photos on it:

- Setup on the new HDD, then the photos restored from the USB drive through the menu and
  copied with the menu's own command, then setup again: it makes Immich's other folders and
  their markers, and Immich starts with the photos and the album
  (`ImmichFoldersAfterANewDriveTests`, tests/test_immich_compose.py). With the HDD unplugged,
  setup stops and nothing is made on the system disk.

## Manual parts

The drive swap is by hand. The rig driver answers `Restore whose Nextcloud files?` with the
first account since the 0.48.1 gate (it had no rule for it during that gate).
0.48.1 gate (VM): partly proved. Photos from the USB drive through the recovery menu:
3 of 3 test photos byte-identical. Nextcloud files and app settings: not completed (the
menu asked `Restore whose Nextcloud files?`, a per-user choice the rig driver did not
expect). Monthly snapshot restore and drive replacement: not tested.

1.0.0 round (`tests/COVERAGE-MAP.md`): proved on the VM, on earlier commits of the release
branch: both drives lost on e09cdbd (after T1-R1 to T1-R4 were found on 35ea7bc), the SSD lost
on 047ee3d (T1-R5, T1-R9; from the monthly snapshot and from the nightly backup), the HDD lost
on 047ee3d (T1-R6). The three drills on the final commit 597c800 passed (T1-final-2 to T1-final-4), with
T1-R13 (downloads after a whole-server restore, fixed and proved on e2da4a2) and T1-R14 (a
link share lost with a file uploaded again; the steps now say so).
