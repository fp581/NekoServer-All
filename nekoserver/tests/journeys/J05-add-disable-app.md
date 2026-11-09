# J05 Add or disable an app after installation

```
ID: J05
Purpose: Taking an app out of the list stops it and keeps its files; putting it back brings it
  back whole, with its account and its links
Preconditions: a finished install (post-install snapshot); a few files in the app (adddata.py)
Actions: take one app (say Lidarr) out of the app list in my-config.json; sudo bash install.sh;
  put it back; sudo bash install.sh again
Expected: see below
Evidence: docker ps before and after each run; the dashboard's app list; the health report
  reports/jobs/*.json; the credentials file's keys (never its values); the setup log
Cleanup: restore the post-install snapshot
```

## Expected results

After the app is taken out:

- Its container is stopped and its files stay on the SSD
  (`test_an_app_taken_out_of_the_list_is_stopped_and_keeps_its_files`, tests/test_media_unselected.py:20;
  `test_a_rerun_of_the_deployment_stops_the_app_taken_out`, tests/test_media_unselected.py:57).
- The media monitor follows the list (`test_the_installed_monitor_follows_the_app_selection`,
  tests/test_media_unselected.py:108): the health report no longer names the app.
- Prowlarr's and Seerr's links to it are taken away, only for an app setup had set up
  (`test_prowlarrs_link_to_a_removed_app_is_taken_away_and_a_changed_one_kept`,
  tests/test_app_followups.py:202; `test_seerrs_provider_for_a_removed_app_is_taken_away`,
  tests/test_app_followups.py:253; `test_setup_takes_them_away_only_for_an_app_it_had_set_up`,
  tests/test_app_followups.py:270). A preview (`--dry-run`) removes nothing
  (`test_a_preview_removes_nothing`, tests/test_app_followups.py:222).
- The dashboard's app list and the nightly app snapshot no longer include it; the
  finishing screen still says `NekoServer is ready!`.

After the app is put back:

- The container runs again from its kept files; its password step runs with it
  (`test_every_app_step_alone_gets_its_password_step`, tests/test_install_order.py:1832).
- A client that still signs in is left alone; one with the old login gets the new one
  (`test_a_client_that_still_signs_in_is_left_as_it_is`, tests/test_app_followups.py:315;
  `test_a_client_with_the_old_login_gets_the_new_one`, tests/test_app_followups.py:306).
- The health report names it again with `pass`; the dashboard lists it; the download
  clients go with qBittorrent as before (`test_the_arr_download_clients_go_with_qbittorrent`,
  tests/test_app_followups.py:232).
- The files adddata.py put in are still there (checkdata.py prints `OK` for each).

## Manual parts

Editing the settings file is by hand on the VM (the rig has no rule for it yet); the
two reruns are driven. 0.48.1 gate: not tested (unit tests only).
