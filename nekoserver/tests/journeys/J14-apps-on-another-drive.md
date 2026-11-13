# J14 A group's apps installed on a drive of their own

```
ID: J14
Purpose: The apps of a group keep their settings and databases on a spare drive, and every
  part that trusts the app drive still holds: the nightly backup, the monthly snapshot, the
  update's rollback, the start with the drive away, the uninstaller
Preconditions: NekoDebian VM at clean-debian13-desktop with the app drive, the backups' drive
  and one more local drive (ext4, 20 GB or more)
Actions: sudo bash install.sh; the default first page; on the storage page, Movies and shows
  "Installed on": the spare drive; Install; fire the nightly app backup; fire the monthly
  update with Jellyfin's new image made to fail its health check; detach the spare drive and
  restart; run setup; attach it again; sudo bash uninstall.sh with the drive mounts chosen
Expected: see below
Evidence: the review; /etc/fstab; findmnt; the app snapshot's manifest; tar -t of the monthly
  snapshot's data.tar; the rollback's log and Jellyfin's settings after it; the dashboard;
  the journal of each job with the drive away
Cleanup: restore the snapshot
```

## Expected results

- The page offers the list only where there is a drive for it, the review names the drive,
  and the choice is written once the app drive is mounted
  (`test_enter_keeps_the_apps_drive_and_a_number_or_the_windows_answer_takes_another`,
  `test_nothing_is_asked_without_a_drive_for_it_or_for_a_group_already_placed`,
  `test_the_choice_is_written_once_the_apps_drive_is_open_and_never_moves_an_app`,
  tests/test_install_drive.py).
- Each app's folder on the drive is mounted at its usual place by a line of setup's own
  block; the folder underneath is closed; Docker starts after the mounts and without them
  (`test_the_line_waits_for_both_drives_and_never_stops_the_start`,
  `test_the_folder_is_made_on_the_drive_closed_underneath_and_mounted`,
  `test_docker_starts_after_those_folders_and_does_not_need_them`, tests/test_install_drive.py).
- The nightly app backup holds those apps' settings; with the drive away it stops and names
  the app, and never publishes an empty folder as its settings
  (`an_app_on_another_drive_is_backed_up_from_that_drive_and_never_as_an_empty_folder`,
  runtime/src/app_snapshot.rs).
- The monthly snapshot holds them (tar is given the mount point by name: MANUAL, not
  provable in a unit test).
- A failed update is rolled back on the app's own drive, and a rollback cut off goes on
  (`a_rollback_puts_an_app_on_another_drive_back_on_that_drive`, runtime/src/maintenance.rs).
- A server with everything on the app drive runs the rollback it always ran, and has the
  units and settings files it always had (the same test; the golden files).
- The drive has a card on the dashboard (`a_drive_something_is_placed_on_has_its_own_card`,
  runtime/src/collect.rs); the space check counts it
  (`test_the_space_check_counts_the_drive_the_apps_are_installed_on`, tests/test_install_drive.py).
- With the drive away: setup stops with what to do; a group given up with the drive gone is
  set up anew on the app drive; one still on its drive is not moved back
  (`test_a_lost_drive_stops_setup_with_what_to_do`,
  `test_with_the_drive_gone_the_closed_folder_is_opened_for_the_app_to_start_anew`,
  `test_an_app_still_on_its_drive_is_not_moved_back_and_setup_says_why`,
  tests/test_install_drive.py).
- The uninstaller takes the block out of /etc/fstab, unmounts what is mounted inside the
  drives first, and unmounts the drive
  (`test_a_whole_block_is_taken_and_nothing_else`,
  `test_the_closed_folder_is_opened_and_goes_and_other_folders_are_left_alone`,
  `test_the_drives_beyond_the_two_are_unmounted_and_their_folders_go`, tests/test_install_drive.py).

## Gate rows

| # | Do | Pass |
|---|---|---|
| E1 | Install with Movies and shows on the spare drive | `findmnt /srv/nekoserver/appdata/jellyfin` shows the spare drive's folder; the apps run |
| E2 | Fire `nekoserver-app-backup` | the manifest lists `appdata/jellyfin/...` with files; `skipped` has no "nested filesystem" for it |
| E3 | Fire the monthly snapshot | `tar -t` of data.tar lists files under `appdata/jellyfin/` |
| E4 | Monthly update with Jellyfin's image failing its health check | rolled back; Jellyfin's settings as before; `appdata/jellyfin.neko-failed-<run>` is on the spare drive; the mount shows the folder put back |
| E5 | Detach the spare drive, restart | the PC starts; Docker runs; Jellyfin has written nothing under `/srv/nekoserver/appdata/jellyfin`; the photo apps run; each backup job stops naming the app |
| E6 | Run setup with the drive detached | it stops with "is not connected" and the steps; nothing changed |
| E7 | Uninstall with the drive mounts chosen | no line of the block in `/etc/fstab`; the spare drive unmounted; its files untouched unless the apps were chosen |

## Manual parts

- E3 and E4 on the gate machine: they are the two that no unit test can show.
- Reading the dashboard's card for the drive.
