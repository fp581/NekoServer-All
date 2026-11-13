# J12 A PC with one drive

```
ID: J12
Purpose: A PC whose only drive is the disk Linux runs from becomes a working server, and
  the uninstaller leaves that disk's own mounts alone
Preconditions: NekoDebian VM at clean-debian13 (no desktop), no drive besides the system disk
Actions: sudo bash install.sh in the terminal; Photos alone on the first page; yes; Enter at
  the storage page (the system disk for both parts, no offline backup); Enter at every other
  question; restart; upload a photo; fire the nightly backup; sudo bash uninstall.sh
Expected: see below
Evidence: the terminal log; /etc/fstab before setup, after setup and after the uninstaller;
  findmnt; the drive records in my-config.json; systemctl list-timers 'nekoserver-*'
Cleanup: restore the snapshot
```

## Expected results

- The storage page offers the system disk for the apps and for the media and backups, and
  says what that means before it is taken; it is refused, with the sizes, when it has no
  room for what goes there
  (`test_the_list_refuses_the_system_disk_when_it_has_no_room_for_what_goes_there`,
  `test_the_room_of_one_filesystem_is_counted_once_for_both_roles`, tests/test_system_disk.py).
- The two parts are the folders `/var/lib/nekoserver/apps` and `/var/lib/nekoserver/media`;
  each record holds the system filesystem's UUID and its folder
  (`test_the_two_folders_are_fixed_and_neither_is_inside_the_other`,
  `test_a_role_placed_there_is_recorded_with_the_system_filesystem_and_its_folder`,
  `test_only_the_roles_own_folder_is_accepted_from_the_settings`, tests/test_system_disk.py).
- Each folder gets a bind line in `/etc/fstab`; the system's own line is left alone, and a
  line someone else wrote for the place stops setup
  (`test_the_folder_gets_a_bind_line_and_the_systems_own_line_is_left_alone`,
  `test_a_line_someone_else_wrote_for_the_place_stops_setup`, tests/test_system_disk.py).
- After a restart the folders are mounted where the apps expect them and a later check
  finds the same folder (`test_a_later_check_must_find_the_same_folder`,
  `test_the_folder_findmnt_shows_is_what_the_record_keeps`, tests/test_system_disk.py).
- Linux reinstalled, or a settings file from another PC, stops setup with the reason
  (`test_linux_reinstalled_or_settings_from_another_pc_stop_setup`, tests/test_system_disk.py).
- No offline backup: **None for now** is saved, no weekly USB job exists, and nothing asks
  about it again.
- The uninstaller removes NekoServer's bind lines and folders and never a line of the
  system's own.

## Manual parts

- The restart, the photo upload and the nightly backup on the gate machine.
- Reading `/etc/fstab` before and after the uninstaller.
