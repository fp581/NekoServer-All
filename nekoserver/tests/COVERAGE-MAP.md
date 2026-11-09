# Coverage map for release 0.48.1

What the 0.48.1 gate proved, as `TESTING.md` asks: one row per journey, safety rule and
controlled failure, with the evidence. Gate run of 2026-11-09 on the NekoDebian VM at
commit ee30d0e; distro subset on Fedora 44, AlmaLinux 10 and openSUSE Tumbleweed. Unit
test names are the evidence a reader can rerun (`python -m unittest tests.<file>.<Class>.<test>`,
`cargo test <name>` in `runtime/`). Status words: **proved**, **partly proved**, **not tested**.

## Journeys

| ID | Journey | 0.48.1 status | Evidence |
| --- | --- | --- | --- |
| J01 | Fresh install with defaults | proved | Gate stage `fresh install` on the VM: 31 of 32 steps with the phone step held, rerun 32 of 32. Backups once, all `pass` (`applications`, `database`, `external`, `host`) with byte-identical restore checks of music, photos and Nextcloud files. Timers fired by drop-in (rig `fire-timer.sh`) for `nekoserver-app-backup.timer` and `nekoserver-photo-backup.timer`. Phone checks over Tailscale 10 of 10. Distro subset: Fedora 44, AlmaLinux 10, Tumbleweed. Unit: `test_only_switched_on_backups_run_in_order_then_the_checkup` (tests/test_wizards.py:329), `test_the_shipped_settings_keep_every_job_at_the_time_it_always_had` (tests/test_backup_times.py:53). |
| J02 | Optional features off | not tested | Unit only: `test_phone_alerts_off_stop_their_timer` (tests/test_extras_off.py:45), `test_the_outage_monitor_off_stops_its_timer_and_says_to_pause_the_monitor` (tests/test_extras_off.py:50), `test_extras_read_and_write_the_settings` (tests/test_setup_gui.py:54). No gate stage. |
| J03 | Skip, close, resume | not tested | Unit only: `test_ctrl_d_leaves_the_step_pending_and_ctrl_c_stops_setup` (tests/test_installer_flow.py:440), `test_waiting_is_not_a_failure` (tests/test_setup_round.py:360), `test_confirmed_tests_are_remembered` (tests/test_reruns.py:75). The J01 rerun after the held phone step is the nearest gate evidence. |
| J04 | Browser and terminal switch | not tested | Unit only: `test_the_window_or_the_terminal_answers_whichever_is_first` (tests/test_setup_gui.py:192), `test_the_pages_rows_go_when_a_plain_question_follows` (tests/test_setup_flow.py:257), `test_a_weekday_typed_in_lowercase_reaches_the_page_as_the_day_itself` (tests/test_backup_times.py:583). The rig has no window driver. |
| J05 | Add or disable an app | not tested | Unit only: `test_an_app_taken_out_of_the_list_is_stopped_and_keeps_its_files` (tests/test_media_unselected.py:20), `test_prowlarrs_link_to_a_removed_app_is_taken_away_and_a_changed_one_kept` (tests/test_app_followups.py:202), `test_every_app_step_alone_gets_its_password_step` (tests/test_install_order.py:1832). |
| J06 | Change backup times | partly proved | Gate: the rerun showed the chosen times and kept them. Partial run keep: unit only, `test_a_run_that_does_not_write_every_timer_again_keeps_the_times_and_asks_nothing` (tests/test_backup_times.py:609), `test_the_drives_step_asks_only_when_the_run_writes_a_timer` (tests/test_backup_times.py:654); the move message `test_a_changed_time_on_a_server_set_up_before_says_which_jobs_move` (tests/test_backup_times.py:477). |
| J07 | Update, fail, roll back | not tested | Unit: `a_rollback_puts_back_only_what_its_run_saved` (runtime/src/maintenance.rs:6808), `a_rollback_puts_back_earlier_images_docker_no_longer_has` (runtime/src/maintenance.rs:7615), `test_an_unfinished_update_is_tried_again_every_hour_only_while_it_is_there` (tests/test_finish_update.py:30). Rig: the fault suite's cut-off monthly run (`fault.sh`, `fault-any.sh`) in earlier rounds, not a gate stage. |
| J08 | Upgrade from older | not tested | Unit: `test_a_failed_setup_or_check_puts_the_release_you_had_back` (tests/test_upgrade.py:325), `test_a_failed_step_and_a_refusal_by_install_sh_are_failed_setups` (tests/test_upgrade.py:346), `test_a_copy_hidden_by_a_cut_off_way_back_gets_its_name_back_when_the_record_is_closed` (tests/test_upgrade.py:772). Rig: `stuck-way-back.sh`, `fresh-old.sh`, `cut.sh` in earlier rounds, not run for 0.48.1. |
| J09 | Replace a drive, restore elsewhere | partly proved | Gate (VM): photos restored from the USB drive through the recovery menu, 3 of 3 test photos byte-identical. Nextcloud files and app settings: not completed (the menu asked `Restore whose Nextcloud files?`, a per-user choice the rig driver did not expect). Monthly snapshot restore and drive replacement: not tested. Unit: `test_restored_files_are_checked_against_their_recorded_checksums` (tests/test_wizards.py:1003), `test_a_replacement_drive_keeps_nothing_of_the_old_one` (tests/test_setup_round.py:685). |

## Safety rules

Marked by the tests that assert them (the full lists are in `TESTING.md`). A rule is
**proved** when its tests pass in CI and a gate stage exercised it; **partly proved**
when CI tests pass but no gate stage did.

| ID | Rule | 0.48.1 status | Evidence |
| --- | --- | --- | --- |
| S01 | Missing storage never redirects writes onto the OS disk | partly proved | CI: `the_ssd_check_refuses_the_right_drive_with_the_wrong_subvolume` (runtime/src/backup.rs:8216), `guard_config_does_not_allow_root_mount` (runtime/src/startup.rs:437), `test_a_restore_never_writes_onto_the_system_disk_when_the_hdd_is_missing` (tests/test_wizards.py:1544). No gate stage unplugged a drive. |
| S02 | Wrong UUID, subvolume or NAS identity blocks | partly proved | CI: `a_btrfs_subvolume_is_part_of_the_drive_identity` (runtime/src/backup.rs:4905), `test_two_clones_of_one_drive_still_stop_setup` (tests/test_setup_flow.py:593), `test_a_nas_disk_that_shows_no_wwn_is_refused_as_the_runtime_refuses_it` (tests/test_network_storage.py:619). The HDD-replaced refusal was not run for 0.48.1. |
| S03 | Failed inspection never means safe, empty or healthy | partly proved | CI: `test_a_lun_that_cannot_be_inspected_stops_setup_instead_of_passing_as_empty` (tests/test_network_storage.py:263), `test_a_failed_checkup_is_never_ready` (tests/test_wizards.py:376), `a_check_that_could_not_run_since_the_last_report_makes_it_stale` (runtime/src/collect.rs:3066), `test_a_missing_certificate_ends_the_step_as_needs_setup_with_the_reason` (tests/test_tailnet_certificate.py). No gate stage; the 0.48.2 gate found B-0482-01 under this rule. |
| S04 | Interrupted work retains enough information to recover | partly proved | CI: `a_resume_has_work_only_with_a_journal_or_a_cut_off_report` (runtime/src/backup.rs:4747), `test_a_cut_off_upgrade_is_put_right_at_the_next_start` (tests/test_upgrade.py:691), `test_a_move_cut_off_before_its_switch_whose_apps_ran_again_starts_over` (tests/test_migrate.py:575). No cut-off stage in the gate. |
| S05 | Backups verify their contents before being reported successful | proved | Gate: byte-identical restore checks of music, photos and Nextcloud files after the first backups. CI: `a_copy_is_published_only_when_stream_disk_and_source_checksums_agree` (runtime/src/backup.rs:5348), `test_app_snapshot_verifies_restores_and_catches_tampering` (tests/test_snapshot_restore.py:50), `test_a_copy_that_reads_back_differently_is_refused` (tests/test_music_recovery.py:85). |
| S06 | Failed updates preserve a usable recovery path | partly proved | CI: `a_rollback_puts_back_only_what_its_run_saved` (runtime/src/maintenance.rs:6808), `test_putting_the_release_back_is_checked_before_it_is_called_done` (tests/test_upgrade.py:632), `test_the_old_release_guard_lets_the_way_back_run_its_setup` (tests/test_upgrade.py:403). No failed update in the gate. |
| S07 | Passwords never appear in public reports or logs | partly proved | CI: `failure_reasons_keep_the_cause_but_not_secrets` (runtime/src/common.rs:1270), `scrub_blanks_passwords_in_every_saved_model` (runtime/src/maintenance.rs:6128), `test_password_never_on_command_line` (tests/test_backup_secrets.py:256), `test_questions_are_seen_while_they_wait_and_never_their_answers` (tests/test_setup_gui.py:86). The gate's reports were not grepped for secrets. |

## Controlled failures

| ID | Failure | 0.48.1 status | Evidence |
| --- | --- | --- | --- |
| F01 | NAS disappears | not tested | CI: `the_apps_stop_once_on_a_failing_drive_and_start_only_when_all_take_writes` (runtime/src/nas_watch.rs:435). `lab/nas_lab.py` was not run for 0.48.1 (no NAS code changed). |
| F02 | Backup disk fills | not tested | CI: `a_full_drive_removes_the_oldest_snapshots_but_keeps_the_newest` (runtime/src/backup.rs:7092), `unfinished_runs_make_room_on_a_full_hdd_but_complete_ones_stay` (runtime/src/maintenance.rs:6653). Rig `usb-full.sh` ran in round 14, not for 0.48.1. |
| F03 | Process stops mid-update | not tested | CI: `test_a_cut_off_upgrade_is_put_right_at_the_next_start` (tests/test_upgrade.py:691), `a_written_snapshot_says_so_in_the_journal` (runtime/src/host_snapshot.rs:569). Rig `cut.sh` and the fault suite ran in earlier rounds. |
| F04 | API returns 409/500 | partly proved | CI: `test_a_refused_start_gives_the_button_back_with_the_reason` (tests/test_setup_flow.py:174), `test_error_codes_are_explained_everywhere` (tests/test_dashboard.py:67). A 500 from the runtime to the dashboard page has no test. |
| F05 | Settings are malformed | partly proved | CI: `test_a_settings_file_that_cannot_be_read_is_never_written_over` (tests/test_setup_gui.py:448), `test_a_setting_that_could_not_be_read_is_never_overwritten` (tests/test_gateway_cloud_origin.py:144), `test_a_wrong_time_is_refused_in_plain_words` (tests/test_backup_times.py:118). No rig stage. |
| F06 | Health report is stale | partly proved | CI: `only_a_complete_check_turns_stale` (runtime/src/collect.rs:3554), `stale_input_never_recovers_reported_problem` (runtime/src/notify.rs:844), `test_a_changed_schedule_makes_witnessed_results_outdated` (tests/test_backup_times.py:194). No rig stage moved the clock past a report. |

## Bugs fixed in 0.48.1

One ID per changelog entry of 0.48.1 (`CHANGELOG.md`), in order, with the regression test
that fails without the fix and the journey or rule it belongs to.

| ID | Changelog entry (short) | Regression test | Journey / rule |
| --- | --- | --- | --- |
| B-0481-01 | Immich's weekly database copy lands on the right day when its 45 minutes cross midnight | `test_the_weekly_timers` (tests/test_backup_times.py:159): `Sat *-*-* 23:35:00` for Sun 00:20 | J06, J01 |
| B-0481-02 | Enter at the backup times on a server set up before 0.48.0 no longer moves them silently | `test_enter_everywhere_on_a_server_set_up_before_keeps_its_own_times` (tests/test_backup_times.py:458), `test_a_changed_time_on_a_server_set_up_before_says_which_jobs_move` (tests/test_backup_times.py:477) | J06, J08 |
| B-0481-03 | Setup run again shows the backup times the settings really hold | `test_the_list_of_last_answers_shows_the_times_the_settings_hold` (tests/test_backup_times.py:539), `test_a_remembered_answer_that_is_not_what_the_settings_hold_is_asked_again` (tests/test_backup_times.py:507) | J06, J03 |
| B-0481-04 | Enter never keeps a monthly time the maintenance step refuses | `test_a_monthly_time_no_longer_at_night_is_offered_the_latest_time_that_is` (tests/test_backup_times.py:553), `test_a_monthly_time_after_the_night_is_refused_on_the_page` (tests/test_setup_flow.py:244) | J06 |
| B-0481-05 | A weekday typed in lowercase no longer blanks the window's day selector | `test_a_weekday_typed_in_lowercase_reaches_the_page_as_the_day_itself` (tests/test_backup_times.py:583) | J04 |
| B-0481-06 | Only a full setup run changes the backup times | `test_a_run_that_does_not_write_every_timer_again_keeps_the_times_and_asks_nothing` (tests/test_backup_times.py:609), `test_the_drives_step_asks_only_when_the_run_writes_a_timer` (tests/test_backup_times.py:654) | J06 |
| B-0481-07 | A NAS disk that cannot be inspected stops setup instead of passing as empty | `test_a_lun_that_cannot_be_inspected_stops_setup_instead_of_passing_as_empty` (tests/test_network_storage.py:263) | S03 |
| B-0481-08 | Ctrl+C during the first checkup still reaches the finishing screen | `test_ctrl_c_during_the_first_checkup_still_shows_the_finishing_screen` (tests/test_wizards.py:150) | J03, J01 |
| B-0481-09 | The setup window's Start button comes back after a refused start | `test_a_refused_start_gives_the_button_back_with_the_reason` (tests/test_setup_flow.py:174) | F04, J04 |
| B-0481-10 | The setup window's Send works again after the backup-times page | `test_the_pages_rows_go_when_a_plain_question_follows` (tests/test_setup_flow.py:257) | J04 |
| B-0481-11 | The setup window's header wraps in a narrow window | no test (CSS only, `setup-gui/setup.css`); checked by eye in a narrow window | J04 |
| B-0481-12 | Documentation: README links the wiki's Architecture page; GUIDED-SETUP names the backup times | no test (documentation); the root `README.md`, line 86 | none |
| B-0481-13 | A runtime copy hidden by a way back that was cut off gets its name back | `test_a_copy_hidden_by_a_cut_off_way_back_gets_its_name_back_when_the_record_is_closed` (tests/test_upgrade.py:772), `test_a_copy_left_with_nothing_at_its_name_gets_it_back_after_setup_by_hand` (tests/test_upgrade.py:784) | J08, S06 |
| B-0481-14 | A failed step and an install.sh refusal no longer pass for a step that waits | `test_a_failed_step_and_a_refusal_by_install_sh_are_failed_setups` (tests/test_upgrade.py:346), `test_a_new_step_that_failed_puts_the_release_you_had_back` (tests/test_upgrade.py:384) | J08, S06 |
| B-0481-15 | A weekly USB copy that gives way to the monthly run is not recorded as a failure | `a_weekly_copy_that_gives_way_to_the_monthly_run_keeps_the_last_report` (runtime/src/backup.rs:8252) | S03, J01 |
| B-0481-16 | A USB drive the monthly check could not check no longer overwrites the weekly report | `the_monthly_usb_check_leaves_the_weekly_report_alone_unless_it_finds_damage` (runtime/src/backup.rs:8292) | S03, S05 |
| B-0481-17 | The nightly app snapshot says when room was made by cutting the history short | `a_full_drive_removes_the_oldest_snapshots_but_keeps_the_newest` (runtime/src/backup.rs:7092): ends `partial` with `1 older application snapshot was removed` | F02, S05 |
| B-0481-18 | The monthly update's retry after the autumn clock change is set for tomorrow | `the_retry_on_the_night_the_clocks_go_back_is_set_for_tomorrow_not_today` (runtime/src/maintenance.rs:8013) | J07 |
| B-0481-19 | The host snapshot's protected complete snapshot is chosen in the pruning's own order | `the_last_snapshot_with_steam_outlives_the_ones_without` (runtime/src/host_snapshot.rs:679) | S05 |
| B-0482-01 | The Tailscale step and its acceptance row say why the phone addresses do not open when Tailscale has no HTTPS certificate, instead of passing | `test_a_missing_certificate_ends_the_step_as_needs_setup_with_the_reason`, `test_matching_routes_whose_addresses_do_not_answer_are_not_a_pass` (tests/test_tailnet_certificate.py) | S03, J01 |

## The five biggest gaps

1. J07 and J08 (a failed update and an upgrade from an older release) have unit tests and
   earlier-round rig scripts but no stage in the VM gate.
2. J09 is proved for photos only: the Nextcloud-files and app-settings restores stopped at
   the per-user question, and the monthly snapshot restore and the drive replacement were
   not run.
3. No controlled failure (F01 to F06) is a gate stage; F01 and F02 depend on the lab and
   on round-14 scripts that did not run for 0.48.1.
4. J02 to J05 (features off, skip and resume, window and terminal, add or disable an
   app) rest on unit tests alone; the rig has no window driver and no rule for editing
   the settings between runs.
5. Compatibility fixtures are inline in test code; no settings file, manifest or journal
   of an older release is kept as a file with a loader test (`TESTING.md`, Compatibility
   fixtures).
