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
  then reformat the HDD (rig hdd-replaced.sh) and run setup again
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

Drive replacement: setup refuses the reformatted HDD while the settings name the old
UUID, with the runtime's own reason (`test_a_refusal_says_the_runtimes_own_reason`,
tests/test_storage_identity.py:170); once the old UUID is taken out, the replacement keeps
nothing of the old drive and the size is checked before mounting or saving
(`test_a_replacement_drive_keeps_nothing_of_the_old_one`, tests/test_setup_round.py:685;
`test_the_size_is_checked_before_mounting_writing_fstab_or_saving`, tests/test_setup_round.py:743).
The OS reinstall path follows RECOVERY.md:144.

## Manual parts

The drive swap is by hand. The rig driver answers `Restore whose Nextcloud files?` with the
first account since the 0.48.1 gate (it had no rule for it during that gate).
0.48.1 gate (VM): partly proved. Photos from the USB drive through the recovery menu:
3 of 3 test photos byte-identical. Nextcloud files and app settings: not completed (the
menu asked `Restore whose Nextcloud files?`, a per-user choice the rig driver did not
expect). Monthly snapshot restore and drive replacement: not tested.
