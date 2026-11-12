# J13 Films you already have, used where they are (a folder shared over NFS)

```
ID: J13
Purpose: A folder of films on another Linux box, shared over NFS, is used by Jellyfin where
  it is: nothing copied, nothing of the person's changed, read as the apps' account
Preconditions: NekoDebian VM at clean-debian13-desktop with two drives; an NFS server on the
  test network sharing a folder of films, mounted on the VM
Actions: sudo bash install.sh; the default first page; at "Do you have films or shows already
  that this server should use?" yes, and the mounted folder; Enter at every other question;
  open Jellyfin; stop the NFS server and restart the VM
Expected: see below
Evidence: the setup log; the placement table in my-config.json; /etc/fstab; findmnt;
  Jellyfin's library page; a file list of the share before and after
Cleanup: restore the snapshot; stop the NFS server
```

## Expected results

- Only a server that plays films is asked, once
  (`test_only_a_server_that_plays_films_is_asked_and_only_once`, tests/test_existing_page.py).
- The folder is read as the apps' account before it is accepted; a folder that account
  cannot read says who and where to fix it; a share that is not mounted is an empty folder,
  not the films (`test_a_folder_on_a_mounted_share_is_read_as_the_apps_account`,
  `test_a_folder_the_apps_account_cannot_read_says_who_and_where_to_fix_it`,
  `test_a_share_that_is_not_mounted_is_an_empty_folder_not_the_films`,
  tests/test_existing_media.py).
- The choice is said back and saved in the group's row of the placement table; what the
  settings file says is checked again before use
  (`test_a_folder_is_checked_said_back_and_saved_in_the_table`, tests/test_existing_page.py;
  `test_what_the_settings_file_says_is_not_trusted`, tests/test_existing_media.py).
- The folder is bound read-only into the library, now and at every start, waiting for the
  share; nothing of the person's is touched
  (`test_the_line_binds_read_only_and_waits_for_the_share`,
  `test_setup_mounts_it_now_and_at_every_start_and_touches_nothing_of_the_persons`,
  tests/test_existing_media.py).
- Jellyfin gets a library at the folder as it sees it, and shows the films
  (`test_jellyfin_gets_a_library_for_it_with_its_signed_in_client`, tests/test_existing_page.py;
  `test_jellyfin_gets_a_library_at_the_folder_as_it_sees_it`, tests/test_existing_media.py).
- With the share away the step does not fail, nothing is written in its place, and the
  other apps run (`test_a_share_that_is_away_is_said_and_never_fails_the_step`,
  tests/test_existing_page.py;
  `test_a_share_that_is_away_is_not_mounted_and_nothing_is_written_in_its_place`,
  tests/test_existing_media.py).

## Manual parts

- Looking at Jellyfin's library for the films on the gate machine.
- Comparing the share's file list before and after.
