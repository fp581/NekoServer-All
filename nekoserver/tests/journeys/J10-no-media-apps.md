# J10 Installation without the media apps, then with them

```
ID: J10
Purpose: Photos, Movies and shows and Music switched off on setup's first page give a
  complete server (Nextcloud, the dashboard, backups, Tailscale, shared folders) that asks
  for nothing it does not have; on later installs them as on any server; off on a server
  that has them deletes nothing
Preconditions: NekoDebian VM at clean-debian13-desktop; blank drives
Actions: (a) switch the three groups off on the first page (the window's switches, or each
  line's number in the terminal), Install selected apps, Enter at every other question; (b) run
  setup again, "Change apps or settings", switch them on; (c) put test data in the apps, run
  setup again and switch them off
Expected: see below
Evidence: my-config.json ("media_apps"); docker ps; systemctl list-timers 'nekoserver-*';
  the dashboard's Home and Backups pages; the finishing screen; the phone guide;
  sudo bash install.sh --recover; ls of the SSD's appdata and the HDD's library
Cleanup: restore the post-install snapshot
```

## Expected results

**(a) Off on a fresh install**

- The groups are chosen once, on the first page, and the review lists them under what is
  left out (`test_a_new_server_is_offered_the_apps_on_and_the_extras_off`,
  `test_settling_gives_every_part_an_answer_so_no_step_asks`, tests/test_first_page.py).
  Setup run without that page (`installer.py` itself) asks them at the drives step, before
  the drives are chosen (`asked_media_apps`, guided_setup.py;
  `test_asked_once_explained_and_saved`,
  `test_the_drives_step_asks_it_before_the_drives_and_keeps_it_after`,
  tests/test_media_apps.py). `my-config.json` holds a `false` for each group under `"media_apps"`;
  `media.services` and `photos.deploy_immich` are as shipped
  (`test_the_setting_itself_is_never_emptied`, tests/test_media_apps.py:84).
- The drive questions name the drives by what goes on them ("the HDD (Nextcloud files and
  the backups)") and only Nextcloud's place is asked
  (`test_the_drives_are_described_by_what_goes_on_them`, tests/test_media_apps.py:188;
  `test_only_nextclouds_place_is_asked_without_the_media_apps`, tests/test_media_apps.py:194).
- No container of Jellyfin, Seerr, Sonarr, Radarr, Lidarr, Prowlarr, qBittorrent, Immich or
  Navidrome runs, and none of their compose folders exists; Nextcloud and the dashboard run
  (`test_media_sets_nothing_up_on_a_new_server`, tests/test_media_apps.py:226;
  `test_music_tv_onboarding_and_hardware_video_have_nothing_to_do`, tests/test_media_apps.py:263).
- No `nekoserver-app-backup.timer`, `nekoserver-music-backup.timer`,
  `nekoserver-media-health.timer`, `nekoserver-photo-preservation.timer` or
  `nekoserver-immich-db-backup.timer`; the host snapshot, the weekly copy (Nextcloud's
  files alone), the monthly check and the updates are scheduled
  (`test_the_media_schedules_are_retired`, tests/test_media_apps.py:276; the golden files
  under `tests/golden/no-media`, `test_no_media`, tests/test_golden_units.py).
- The dashboard shows Nextcloud's tile only; its Backups page shows the app and music
  backups as "Switched off: the media apps are left out", never as overdue
  (`jobs_of_a_switched_off_app_are_retired_not_overdue`, runtime/src/collect.rs:2983;
  `the_media_apps_left_out_are_not_among_the_apps`, runtime/src/apps.rs:647).
- The Tailscale and home addresses, the phone guide and the desktop shortcuts name
  Nextcloud and the dashboard only (`test_the_addresses_are_nextclouds_only`,
  tests/test_media_apps.py:292; `test_no_shortcuts_for_the_apps_left_out`,
  tests/test_media_apps.py:297); no login is made for an app left out
  (`test_no_logins_are_made_for_the_apps_left_out`, tests/test_media_apps.py:287).
- The first checkup runs and passes with the checks of the media steps left out, and the
  finishing screen says `NekoServer is ready!`
  (`test_the_first_checkup_and_acceptance_leave_their_checks_out`, tests/test_media_apps.py:323).
- The recovery menu offers no photo or app-settings restore
  (`test_the_recovery_menu_offers_the_media_restores_only_with_a_backup_of_theirs`,
  tests/test_media_apps.py:330).

**(b) On later**

- Setup says what it found on this PC first; with the groups switched on on the first
  page, every media step runs as on a fresh install with the default answers
  (J01's expected results hold) (`test_a_rerun_keeps_the_answer_unless_asked_again`,
  tests/test_media_apps.py:154).

**(c) Off on a server that has them**

- The apps are stopped (`docker compose down`, never with volumes) and setup says so; every
  folder under the SSD's `appdata` and the HDD's library, photos and backups is still there,
  byte for byte (checkdata.py) (`test_media_stops_the_apps_a_server_had_and_keeps_their_data`,
  tests/test_media_apps.py:234; `test_immich_is_switched_off_and_keeps_its_photos`,
  tests/test_media_apps.py:257).
- The shared folder with the films stays shared; the recovery menu still offers the photo
  and app restores, as their backups are there
  (`test_the_shared_folders_follow_what_is_there`, tests/test_media_apps.py:304).
- Yes again brings the apps back with their settings, logins and libraries.

## Manual parts

- Reading the first page's cards for plain words and the right sizes on the gate machine.
- Opening the dashboard and the phone guide to see that nothing names an app left out.
