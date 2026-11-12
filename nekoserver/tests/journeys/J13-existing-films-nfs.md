# J13 Films, shows and music you already have (a folder shared over NFS)

```
ID: J13
Purpose: Folders of films and of music on another Linux box, shared over NFS, are used
  where they are (Jellyfin, Navidrome), take the new ones too when asked, or are copied
  into the library: nothing of the person's changed, read as the apps' account
Preconditions: NekoDebian VM at clean-debian13-desktop with two drives; an NFS server on the
  test network sharing a folder of films, mounted on the VM
Actions: sudo bash install.sh; the default first page; at "Do you already have films, shows
  or music that this server should use?" yes; films: the mounted folder, used where it is,
  "Save new ones here too?" yes; shows: a folder to copy; music: the mounted folder, used
  where it is; Enter at every other question; open Jellyfin and Navidrome; request a film
  in Seerr; stop the NFS server and restart the VM
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

- New ones saved in the folder: checked for writing as the apps' account, the note about
  room and time said, the folder the one place for new films; a read-only folder says why
  and another place is asked for
  (`test_yes_saves_the_folder_as_the_place_for_new_ones`,
  `test_a_read_only_folder_is_still_used_and_another_place_is_asked_for`,
  `test_it_is_not_asked_on_a_server_that_downloads_nothing`, tests/test_existing_page.py).
- A request made in Seerr is saved in that folder, and the readiness check takes it
  (`test_a_new_provider_saves_requests_in_the_persons_folder`,
  `test_the_readiness_check_takes_either_folder_and_no_other`, tests/test_seerr_folders.py;
  `a_request_may_be_saved_in_the_librarys_folder_or_the_persons_own`, runtime/src/media.rs).
- Music used where it is gets a library in Navidrome
  (`test_navidrome_gets_its_library_where_the_libraries_are_made`, tests/test_existing_page.py;
  `test_navidrome_gets_a_second_music_folder`, tests/test_existing_media.py).
- A folder to copy: the choice is kept through the approval, the copy says what it will do,
  the folder is left as it was, and a copy that cannot be made is tried again next run
  (`test_the_choice_is_kept_until_the_copy_and_nothing_is_mounted_for_it`,
  `test_the_copy_says_what_it_will_do_then_copies_and_forgets_the_folder`,
  `test_a_copy_that_cannot_be_made_says_why_and_is_tried_again_next_time`,
  tests/test_existing_page.py).

## Manual parts

- Playing a track from the person's folder in Navidrome, and following a Seerr request to
  the file on the share.

- Looking at Jellyfin's library for the films on the gate machine.
- Comparing the share's file list before and after.
