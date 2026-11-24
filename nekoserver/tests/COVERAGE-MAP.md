# Coverage map for release 1.0.0

What the 1.0 release-candidate round proved, as `TESTING.md` asks: one row per journey,
safety rule and controlled failure, with the evidence. The round tested main at 35ea7bc
(0.52.3) and then `release-1.0.0` as each fix landed. The final gate runs on 597c800, and its
rows say **pending** until that run has reported. A run on an earlier commit of the branch
is written with that commit, and is never counted as a run of the final one.

Where it ran:

- **VM**: the Hyper-V NekoDebian VM (Debian 13 with a desktop), with virtual SSD, HDD and USB
  disks, started from its clean or installed checkpoints. It is the only machine with real
  drives, restarts and the Debian/Ubuntu family.
- **WSL** (one distro per family, shared virtual drives): NekoArch (Arch), FedoraLinux-44
  (Fedora), NekoAlma2 (AlmaLinux, the RHEL family) and openSUSE-Tumbleweed (openSUSE). WSL
  has no drive of its own to pull and no restart, so neither was tested there. Its known rig
  artifacts are not NekoServer's: `/` is not a shared mount, Docker's iptables chains are
  shared between distros, and Tailscale cannot sign in. Each is set right on the rig before
  a run.
- **CI**: the steps of `.github/workflows` (ruff, the Python suite, rustfmt, clippy,
  `cargo test`), run on NekoUbuntu as CI runs them, and on GitHub.

The data was fixture data only: three test photos and an album in Immich, one test file in
Nextcloud, one short test film, and a changed Sonarr or Prowlarr setting. Unit test names
are the evidence a reader can rerun (`python -m unittest tests.<file>.<Class>.<test>`, and
`cargo test <name>` in `runtime/`). VM and WSL evidence is the row ID in the round's
findings, with what was seen there. Status words: **proved**, **partly proved**,
**not tested**, and **pending** for the final gate.

## Journeys

| ID | Journey | 1.0.0 status | Evidence |
| --- | --- | --- | --- |
| J01 | Fresh install with defaults | partly proved | VM, 37843d5 (T1-gate-37843d5, T1-R12): the fresh install from the clean checkpoint failed at random. Nextcloud never installed (28 of 34 steps) when its made-up admin password started with "-", which happens in 1 install of 64. Fixed in 9df689b (each value joined to its option) and 7946397 (a made-up password starts with a letter or digit). Both first stopped at "No restart pending from an earlier update is required" after Debian's own update, which is right; after a restart setup went on. WSL, fresh installs with the recommended answers: NekoArch 35ea7bc through the setup window (T4-arch), FedoraLinux-44 35ea7bc (T4-fedora), NekoAlma2 369390b (T4-alma), openSUSE-Tumbleweed 37843d5 (T4-suse, 28 of 32, left with Tailscale and phone alerts only). Unit: `test_a_password_starting_with_a_dash_reaches_occ_as_the_options_own_value` (tests/test_review_fixes.py), `GeneratedPasswordStartTests` (tests/test_generated_passwords.py). Final fresh install on 597c800: pending. |
| J02 | Optional features off | not tested | No stage this round. Unit only: `test_phone_alerts_off_stop_their_timer`, `test_the_outage_monitor_off_stops_its_timer_and_says_to_pause_the_monitor` (tests/test_extras_off.py), `test_extras_read_and_write_the_settings` (tests/test_setup_gui.py). |
| J03 | Skip, close, resume | partly proved | Setup run again after a stop finished what was left. FedoraLinux-44 35ea7bc and NekoAlma2 369390b: second and third runs, after the WSL rig fix (T4-fedora, T4-alma). VM 37843d5: setup again after the restart that Debian's update needed (T1-gate-37843d5). The words for a step left for later were fixed (T3-3). Unit: `test_a_step_left_for_later_says_how_to_finish_it_in_plain_words` (tests/test_installer_flow.py), `test_ctrl_d_leaves_the_step_pending_and_ctrl_c_stops_setup` (tests/test_installer_flow.py). |
| J04 | Browser and terminal switch | partly proved | Setup window from the first page to done, and a second run ("Change apps or settings"), on NekoArch 35ea7bc (T3-1, T3-2, T3-3; all three fixed). The VM runs were made in the terminal. Answering one question in each was not tried. Unit: `test_prowlarrs_reason_is_shown` (tests/test_indexers.py), `test_a_second_run_says_nothing_changes_not_nothing_is_installed` (tests/test_setup_window_look.py), `test_the_window_or_the_terminal_answers_whichever_is_first` (tests/test_setup_gui.py). |
| J05 | Add or disable an app | not tested | Unit only: `test_an_app_taken_out_of_the_list_is_stopped_and_keeps_its_files` (tests/test_media_unselected.py), `test_every_app_step_alone_gets_its_password_step` (tests/test_install_order.py). |
| J06 | Change backup times | not tested | No stage this round. Unit: `test_a_run_that_does_not_write_every_timer_again_keeps_the_times_and_asks_nothing` (tests/test_backup_times.py). |
| J07 | Update, fail, roll back | partly proved | VM 37843d5 (T1-R11): a weekly Update now of one app whose new container was killed during its checks was undone. The old image came back, one container ran, the app's page answered, the report said `rolled_back` and the update stayed pending. The dashboard still called it urgent; fixed in a9fc341 and 7364e64. VM 7efba8a, the same drill: the alert is a warning, "Weekly app updates: the update was undone, your apps work", and Update now with nothing killed then installed it. The monthly run's roll-back on a real install was not reached: NekoArch v0.52.3 (T2-monthly) stopped, correctly, at the check before the update because of rig settings. Unit: `a_rolled_back_weekly_update_says_the_apps_are_back_and_working` (runtime/src/collect.rs), `an_app_left_broken_after_an_undone_one_makes_the_night_a_failure` (runtime/src/maintenance.rs). Final roll-back on 597c800: pending. |
| J08 | Upgrade from older | partly proved | VM, upgrades from the installed v0.52.3 checkpoint: to e09cdbd (33 of 34, T1 proof of T1-R2 to T1-R4), to 37843d5 (T1-gate-37843d5: the first page reads "From 0.52.3 to 1.0.0", the saved answers were kept, the channel moved to stable once and said so, 16 of 16 containers, the dashboard showed 1.0.0 with no alert, a restart was fine), and to 7efba8a (T1-R11). An upgrade from a release older than 0.52.3 was not run this round. Unit: `test_a_beta_channel_from_before_1_0_0_moves_to_stable_once`, `test_a_stable_channel_is_left_as_it_is` (tests/test_installer_flow.py), `a_server_that_chose_no_channel_is_offered_stable_releases_only` (runtime/src/updates.rs). Final channels drill on 597c800: pending. |
| J09 | Replace a drive, restore elsewhere | proved | All on the VM. **Both SSD and HDD lost**, with a new system disk and the old USB drive. 35ea7bc (T1 scenario 1): T1-R1 to T1-R4 found. e09cdbd (T1 proof): photos, album, password list, app settings from the USB copy (Sonarr's and Radarr's settings identical to before), the Nextcloud database by RECOVERY.md's corrected steps, the Nextcloud file (upload works), the Jellyfin login and its library; the next backups rc 0. **SSD only**, with Nextcloud: 047ee3d (T1-R5, T1-R9): setup's offer brought Nextcloud back on yes, from the monthly snapshot and from the nightly backup alike, with the old login; files unchanged, upload works; "no" keeps the refusal. **HDD only**, photos on it: 047ee3d (T1-R6): photo restore, the menu's copy and setup again bring Immich up with the 3 photos and the album. WSL: settings restores from a nightly backup on all four families (T4-arch, T4-fedora, T4-alma, T4-suse), and the T1-R9 bring-back on NekoAlma2. Unit: `YesRestoresTests`, `NightlyBackupTests`, `PinnedImagesTests`, `OlderPythonTests` (tests/test_nextcloud_restore.py), `ImmichFoldersAfterANewDriveTests` (tests/test_immich_compose.py), `NextcloudDatabaseRestoreTests` (tests/test_docs_follow_setup.py), `test_accounts_the_restored_database_lists_get_their_missing_folders` (tests/test_review_fixes.py), `the_newest_app_backup_goes_onto_the_usb_drive_sealed_for_the_backup_key` (runtime/src/usb_app_settings.rs). Final whole-server restore, lose SSD and lose HDD on 597c800: pending. |
| J10 | Without the media apps, then with them | not tested | No stage this round. |
| J11 | Media and backup drives as NFS shares | not tested | No NAS on the rig. |
| J12 | A PC with one drive | partly proved | VM 37843d5 (T1-layouts): the 1-drive layout on a RAID1 mirror of two blank disks (md0, no SSD, no USB). Setup took md0 for everything and said plainly what that protects against. The dashboard showed the array healthy. A nightly backup and a settings restore passed, and so did a restart. The 2-drive layout (SSD and HDD) also passed backup, restore and restart. Not run: the system disk as the only drive, as J12 describes, and a mirror losing a drive and rebuilding. |
| J13 | Existing films and music on an NFS share | not tested | No NAS on the rig. |
| J14 | A group's apps on a drive of their own | not tested | No stage this round. |

## Other checks of this round

| ID | Check | 1.0.0 status | Evidence |
| --- | --- | --- | --- |
| X01 | Restart: everything comes up by itself | proved | VM 37843d5 (T1-layouts): the 1-drive RAID1, 2-drive and upgraded 3-drive installs. Drives mounted, 16 of 16 containers, no failed unit, every app answers. The only alert was a correct one: the home address changed (Hyper-V gives a new address at each start). |
| X02 | Backup cards, end to end | proved | 35ea7bc, on loop-device cards (T2-ok). A set of 2 cards (exFAT, ext4). A damaged byte was found and copied again. Photos lost: restored 15 of 15 byte-identical from the cards. The card tool restored with card 2 missing and then complete. A lost catalogue was taken back. A card of another set was refused. The summary counts after a loss and the take-back words were fixed (T2-01, T2-02). Unit: `files_gone_from_the_server_are_still_counted_on_the_cards` (runtime/src/cardset/tests_buttons.rs), `damaged_copies_found_by_a_take_back_are_said_in_plain_words` (runtime/src/cards/tasks.rs). Physical SD cards in a reader: not tested. |
| X03 | Uninstall with a drive missing | partly proved | FedoraLinux-44, 35ea7bc, HDD not attached (T4-u1, T4-u2). The uninstaller asked everything, then stopped on a job it could not resume, and after that on Docker's socket. Fixed in 4fd5e56. Unit: `DockerDownTests` (tests/test_uninstaller.py). Final uninstall with a drive missing on 597c800: pending. |
| X04 | Nextcloud in the nightly backup (C-01) | proved | NekoArch, t2-100-nc-nightly: each night holds the database, the settings and the program. The secrets are sealed, every file is 0600 and every folder 0700, and Nextcloud is paused about 11 s. The media snapshot is byte for byte unchanged (1127 files). The bring-back from the nightly backup alone passed (T4, NekoArch). NekoAlma2 369390b and openSUSE-Tumbleweed 37843d5: made by the nightly job (T4-alma, T4-suse). Unit: `nextcloud_alone_is_backed_up_each_night` (runtime/src/app_snapshot.rs), `a_night_saves_the_database_settings_and_html_but_not_the_files`, `with_a_backup_key_its_passwords_are_sealed_and_left_out_of_data_tar` (runtime/src/nextcloud_nightly.rs). |
| X05 | The backup password after new drives | proved | NekoAlma2 369390b (T4-key): the key kept on the system disk sealed new backups with an older password without a word. Fixed in 8e200a9: setup says the day the password was chosen and how to choose a new one. Unit: `test_a_kept_key_says_which_password_is_in_force_also_on_new_drives` (tests/test_backup_secrets_words.py). |
| X06 | A first-time reader follows the README and guides | partly proved | Read word for word against what the code and the screen do on main 35ea7bc (T5-01 to T5-07). All of those rows are fixed. Unit: `test_the_readme_says_what_to_do_before_the_first_sudo`, `test_the_no_sudo_passage_covers_arch_too` (tests/test_docs_follow_setup.py). The guides' commands were typed for real in T1's VM runs. Two RECOVERY.md rows are open, not fixed on 597c800: T1-R1, steps in root-only folders written for a normal user, and T1-R10, setup's "may still be starting" for an Immich that waits for its photos. |
| X07 | The tests pass on a PC that already has NekoServer | proved | NekoUbuntu with a real install (INT-later-2): 9 failures and errors before, none after 6ce08cd, b3c96c1, ced2cdc and 8440380. |

## Safety rules

A rule is **proved** when its tests pass in CI and a VM or WSL stage of this round exercised
it; **partly proved** when CI tests pass but no stage did, or a stage covered part of it.

| ID | Rule | 1.0.0 status | Evidence |
| --- | --- | --- | --- |
| S01 | Missing storage never redirects writes onto the OS disk | proved | VM 047ee3d (T1-R6 guard): with the HDD unplugged, setup stopped ("No drive here can be the media HDD"). `/mnt/nekoserver-media` stayed unmounted and empty, and nothing was made. VM 35ea7bc (T1-R5, T1-R6): the dashboard said "Application SSD unavailable" and "Media HDD unavailable" for the pulled drives. CI: `guard_config_does_not_allow_root_mount` (runtime/src/startup.rs), `test_a_restore_never_writes_onto_the_system_disk_when_the_hdd_is_missing` (tests/test_wizards.py), `test_a_media_drive_that_is_not_mounted_stays_an_error_and_nothing_is_made` (tests/test_immich_compose.py). |
| S02 | Wrong UUID, subvolume or NAS identity blocks | partly proved | VM 35ea7bc and 047ee3d (T1-R5, T1-R6): with a blank replacement disk, setup said the old drive was not connected and asked for the new one. The bring-back is never offered for another server's backup: unit only, `ForeignBackupTests` (tests/test_nextcloud_restore.py), `test_a_newer_snapshot_of_another_server_leaves_this_servers_nightly_backup` (tests/test_nextcloud_restore_choices.py). A card of another set was refused (T2-ok). NAS identity: not tested. CI: `a_btrfs_subvolume_is_part_of_the_drive_identity` (runtime/src/backup.rs), `test_two_clones_of_one_drive_still_stop_setup` (tests/test_setup_flow.py). |
| S03 | Failed inspection never means safe, empty or healthy | partly proved | Broken on 35ea7bc and fixed. VM (T1-R3): setup said PASS while every upload of a restored account was refused. Fixed in 8b6cfb2 and 2ff3f45, and proved on e09cdbd: setup made the missing folders and said so. Held on 37843d5 (T1-R12): Nextcloud's admin was "not verified", never passed. CI: `test_a_failed_checkup_is_never_ready` (tests/test_wizards.py), `a_check_that_could_not_run_since_the_last_report_makes_it_stale` (runtime/src/collect.rs). |
| S04 | Interrupted work retains enough information to recover | partly proved | No stage before the final gate. The bring-back puts the SSD back as it was when a step fails: VM d8e1a8e (T1-R9 found it so); unit `FailedStepTests` (tests/test_nextcloud_restore.py). CI: `a_resume_has_work_only_with_a_journal_or_a_cut_off_report` (runtime/src/backup.rs), `test_a_cut_off_upgrade_is_put_right_at_the_next_start` (tests/test_upgrade.py). Final interrupted backup, restore and upgrade on 597c800: pending. |
| S05 | Backups verify their contents before being reported successful | proved | Every restore of this round was checked against the backup's checksums and matched. VM: photos, Nextcloud files and app settings (T1 scenario 1, T1 proof, T1-R6). WSL: settings restores on four families (T4-arch, T4-fedora, T4-alma, T4-suse). Cards: 15 of 15 (T2-ok). CI: `a_copy_is_published_only_when_stream_disk_and_source_checksums_agree` (runtime/src/backup.rs), `test_app_snapshot_verifies_restores_and_catches_tampering` (tests/test_snapshot_restore.py), `test_restored_files_are_checked_against_their_recorded_checksums` (tests/test_wizards.py). |
| S06 | Failed updates preserve a usable recovery path | proved | VM 37843d5 and 7efba8a (T1-R11): the failed update put the earlier image back, and the update stayed pending. VM d8e1a8e (T1-R9): the failed bring-back put the SSD back as it was. CI: `a_rollback_puts_back_only_what_its_run_saved` (runtime/src/maintenance.rs), `test_putting_the_release_back_is_checked_before_it_is_called_done` (tests/test_upgrade.py). |
| S07 | Passwords never appear in public reports or logs | partly proved | T1-R12 on the VM was diagnosed without printing the password. The API key of a refused request is cut from the reason: unit `test_the_api_key_of_the_request_is_never_shown` (tests/test_indexers.py). The reports of this round were not searched for secrets. CI: `failure_reasons_keep_the_cause_but_not_secrets` (runtime/src/common.rs), `test_password_never_on_command_line` (tests/test_backup_secrets.py). |
| S08 | NekoServer works the same without the AI assistant | partly proved | Every VM and WSL run of this round had Ask Neko off, the default. CI: `test_nobody_at_the_terminal_installs_nothing`, `test_an_upgrade_never_goes_back_because_of_the_assistant` (tests/test_assistant.py). |

## Controlled failures

| ID | Failure | 1.0.0 status | Evidence |
| --- | --- | --- | --- |
| F01 | NAS disappears | not tested | No NAS on the rig. CI: `the_apps_stop_once_on_a_failing_drive_and_start_only_when_all_take_writes` (runtime/src/nas_watch.rs). |
| F02 | Backup disk fills | not tested | No stage this round. CI: `a_full_drive_removes_the_oldest_snapshots_but_keeps_the_newest` (runtime/src/backup.rs). |
| F03 | Process stops mid-update | pending | The final gate's interrupted upgrade on 597c800. CI: `test_a_cut_off_upgrade_is_put_right_at_the_next_start` (tests/test_upgrade.py). |
| F04 | API returns 409/500 | partly proved | A question over 4 KB is refused with E42 rather than a cut connection: 200 of 200 under load on t2-100-oversized (INT-flake-1b). Unit: `an_oversized_body_sent_whole_still_gets_its_refusal`, `refused_heads_whose_body_never_comes_keep_no_worker` (runtime/src/serve.rs), `test_a_refused_start_gives_the_button_back_with_the_reason` (tests/test_setup_flow.py). A 500 from the runtime to the dashboard page has no test. |
| F05 | Settings are malformed | partly proved | WSL v0.52.3 on NekoArch (T2-monthly): with settings that disagreed with the drives, setup refused ("Nothing was changed"), and the update's own check refused. CI: `test_a_settings_file_that_cannot_be_read_is_never_written_over` (tests/test_setup_gui.py), `test_a_job_time_is_hours_and_minutes_of_a_day` (tests/test_mistyped_settings.py). |
| F06 | Health report is stale | not tested | No stage moved the clock past a report. CI: `only_a_complete_check_turns_stale` (runtime/src/collect.rs). |
| F07 | The assistant's model is missing | not tested | Ask Neko was off in every run. CI: `a_missing_model_says_it_is_not_there_yet` (runtime/src/assist.rs). |
| F08 | Ollama is stopped | not tested | Ask Neko was off in every run. CI: `ollama_stopped_is_not_available_right_now` (runtime/src/assist.rs). |
| F09 | The model is killed for its memory | not tested | Ask Neko was off in every run. CI: `an_out_of_memory_model_is_said_as_such` (runtime/src/assist.rs). |
| F10 | The model is too slow | not tested | CI: `a_model_too_slow_to_start_is_stopped` (runtime/src/assist.rs). |
| F11 | The model answers garbage | not tested | CI: `a_garbage_answer_is_only_text` (runtime/src/assist.rs). |
| F12 | An NFS share is off when the PC starts | not tested | No NAS on the rig. CI: `test_the_line_in_fstab_never_holds_up_the_start` (tests/test_nfs_storage.py). |
| F13 | A setup step fails | partly proved | NekoArch 35ea7bc through the window (T3-1): a recommended download source was refused by Prowlarr, and Retry and Not now worked. The page now shows Prowlarr's own reason. VM 37843d5 (T1-R12): the failed Nextcloud step showed only the tail of a usage text. Its cause is fixed, but the text shown on an occ failure is unchanged. Unit: `test_prowlarrs_reason_is_shown` (tests/test_indexers.py), `test_a_retry_that_works_lets_the_run_go_on_as_if_nothing_failed` (tests/test_known_fixes.py). |

## Final gate on 597c800

T1 runs these on the VM. Each row stays **pending** until T1 reports it.

| Row | Drill | Status | Evidence |
| --- | --- | --- | --- |
| T1-final | Fresh install from the clean checkpoint (J01) | pending | |
| T1-final | Whole-server restore, both data drives lost (J09) | pending | |
| T1-final | Lose the SSD (J09) | pending | |
| T1-final | Lose the HDD (J09) | pending | |
| T1-final | Interrupted backup, restore and upgrade (S04, F03) | pending | |
| T1-final | Roll-back of a failed update (J07, S06) | pending | |
| T1-final | Release channels (J08) | pending | |
| T1-final | Uninstall with a drive missing (X03) | pending | |

## Bugs fixed in 1.0.0

One ID per changelog entry of v1.0.0 (`CHANGELOG.md`), in order, with the regression test
that fails without the fix and the journey or rule it belongs to.

| ID | Changelog entry (short) | Regression test | Journey / rule |
| --- | --- | --- | --- |
| B-100-01 | The release is stable, not a beta | `test_the_code_says_the_release_the_changelog_is_at` (tests/test_release_number.py) | J08 |
| B-100-02 | A new server is offered stable releases only | `a_server_that_chose_no_channel_is_offered_stable_releases_only` (runtime/src/updates.rs), `test_the_nekoserver_tab_shows_the_channel_and_stable_when_none_is_read` (tests/test_dashboard_page.py) | J08 |
| B-100-03 | A server from before 1.0.0 moves to the stable channel once | `test_a_beta_channel_from_before_1_0_0_moves_to_stable_once`, `test_a_stable_channel_is_left_as_it_is` (tests/test_installer_flow.py) | J08 |
| B-100-04 | The USB drive brings back the app settings too (T1-R4) | `the_newest_app_backup_goes_onto_the_usb_drive_sealed_for_the_backup_key` (runtime/src/usb_app_settings.rs), `test_the_menu_reads_the_copy_the_weekly_job_writes` (tests/test_usb_app_settings.py) | J09, S05 |
| B-100-05 | Nextcloud is backed up every night (C-01) | `nextcloud_alone_is_backed_up_each_night` (runtime/src/app_snapshot.rs), `a_night_saves_the_database_settings_and_html_but_not_the_files` (runtime/src/nextcloud_nightly.rs) | X04, J09 |
| B-100-06 | The README says what to do when sudo is missing or refuses (T5-01) | `test_the_readme_says_what_to_do_before_the_first_sudo` (tests/test_docs_follow_setup.py) | X06 |
| B-100-07 | An Ask Neko question too long gets its own message (INT-flake-1b) | `an_oversized_body_sent_whole_still_gets_its_refusal`, `refused_heads_whose_body_never_comes_keep_no_worker` (runtime/src/serve.rs) | F04 |
| B-100-08 | The README says what setup really does (T5-02 to T5-05) | `test_the_no_sudo_passage_covers_arch_too`, `test_ask_nekos_model_is_among_what_stops_setup_after_the_approval`, `test_the_shared_folders_are_said_to_need_something_to_share`, `test_the_pc_is_said_to_be_intel_or_amd` (tests/test_docs_follow_setup.py) | X06 |
| B-100-09 | The drive check says what to do when a drive's UUID is lost (T5-07) | `test_a_drive_with_no_identity_in_the_settings_says_what_to_do` (tests/test_network_storage.py) | S02 |
| B-100-10 | A download source Prowlarr refuses says Prowlarr's own reason (T3-1) | `test_prowlarrs_reason_is_shown`, `test_the_api_key_of_the_request_is_never_shown` (tests/test_indexers.py) | F13, S07 |
| B-100-11 | A step left for later says how to finish it (T3-3) | `test_a_step_left_for_later_says_how_to_finish_it_in_plain_words` (tests/test_installer_flow.py) | J03 |
| B-100-12 | After a new SSD, setup brings Nextcloud back (T1-R5) | `YesRestoresTests`, `NoKeepsTheRefusalTests`, `UnattendedNeverRestoresTests`, `OlderPythonTests` (tests/test_nextcloud_restore.py) | J09, S04 |
| B-100-13 | The bring-back uses the nightly backup too (C-01) | `NightlyBackupTests`, `YoungServerTests` (tests/test_nextcloud_restore.py) | J09 |
| B-100-14 | The bring-back works after a monthly update (T1-R9) | `PinnedImagesTests` (tests/test_nextcloud_restore.py) | J09 |
| B-100-15 | Nextcloud installs whatever its admin password starts with (T1-R12) | `test_a_password_starting_with_a_dash_reaches_occ_as_the_options_own_value` (tests/test_review_fixes.py) | J01 |
| B-100-16 | The uninstaller with a drive missing says what to do (T4-u1, T4-u2) | `DockerDownTests` (tests/test_uninstaller.py) | X03 |
| B-100-17 | After a new HDD with the photos on it, Immich starts again (T1-R6) | `ImmichFoldersAfterANewDriveTests` (tests/test_immich_compose.py) | J09, S01 |
| B-100-18 | The window's second run is not told "Nothing is installed yet" (T3-2) | `test_a_second_run_says_nothing_changes_not_nothing_is_installed` (tests/test_setup_window_look.py), `test_an_installed_server_is_not_told_nothing_is_installed` (tests/test_update_words.py) | J04 |
| B-100-19 | Backup cards: files gone from the server still count (T2-01) | `files_gone_from_the_server_are_still_counted_on_the_cards` (runtime/src/cardset/tests_buttons.rs) | X02 |
| B-100-20 | Backup cards: a take-back's damaged copies in plain words (T2-02) | `damaged_copies_found_by_a_take_back_are_said_in_plain_words` (runtime/src/cards/tasks.rs) | X02 |
| B-100-21 | RECOVERY.md's Nextcloud database restore brings Nextcloud back (T1-R2) | `test_the_copy_is_loaded_as_the_account_nextcloud_uses` (tests/test_docs_follow_setup.py) | J09 |
| B-100-22 | After a whole-server restore, Nextcloud takes uploads again (T1-R3) | `test_accounts_the_restored_database_lists_get_their_missing_folders` (tests/test_review_fixes.py) | J09, S03 |
| B-100-23 | A Seerr restored beside another Jellyfin is told what goes with it (T1-R7) | `SeerrLinkedToAnotherJellyfinTests`, `test_a_seerr_not_linked_to_any_jellyfin_yet_is_told_to_finish_its_first_run` (tests/test_jellyfin_requests.py) | J09 |
| B-100-24 | Every recovery text names whose backup password it asks for (T1-R8, T3-R8b) | `RebuiltServerPasswordWordsTests` (tests/test_rebuilt_server_password_words.py) | J09 |
| B-100-25 | A weekly app update that was undone says so (T1-R11) | `a_rolled_back_weekly_update_says_the_apps_are_back_and_working` (runtime/src/collect.rs), `an_app_left_broken_after_an_undone_one_makes_the_night_a_failure` (runtime/src/maintenance.rs) | J07, S06 |
| B-100-26 | A made-up app password starts with a letter or digit (after T1-R12) | `GeneratedPasswordStartTests` (tests/test_generated_passwords.py) | J01 |
| B-100-27 | Tests of the setup driver's parts, folder-access rows, mistyped settings (T6-rc1) | tests only: tests/test_permission_checks.py, tests/test_mistyped_settings.py, tests/test_neko_restore.py | S03, F05 |
| B-100-28 | Tests of the home gateway, the snapshot refusals, the backup password tool, recover.py (T6-rc2 to T6-rc4) | tests only: tests/test_gateway_lan_apply.py, tests/test_snapshot_refusals.py, tests/test_backup_secrets_words.py, tests/test_recover_command.py | S05, S07 |
| B-100-29 | The tests no longer depend on the PC they run on (INT-later-2) | tests only: the host-state tests of X07 | X07 |
| B-100-30 | Tests of the recovery tools, the guided restore and the Nextcloud bring-back (T6-rc5 to T6-rc7) | tests only: tests/test_recovery_command_failures.py, tests/test_wizard_checked_copies.py, tests/test_restore_dates_and_accepted.py, tests/test_nextcloud_restore_choices.py | J09, S05 |
| B-100-31 | Setup says which backup password is in force when it keeps the key (T4-key; its changelog line sits under v0.52.3) | `test_a_kept_key_says_which_password_is_in_force_also_on_new_drives` (tests/test_backup_secrets_words.py) | X05 |

## What is not tested

1. Real hardware: a PC's own drives, fans and sensors, NVIDIA graphics, and pulling a drive
   that is in use. The VM's disks are virtual.
2. Phone alerts end to end (they need a person's phone and account), Tailscale sign-in on
   WSL, and a real router's ports and addresses.
3. Real USB sticks and SD cards in a reader: the USB drive was a virtual disk, and the backup
   cards were loop devices.
4. The Debian/Ubuntu family only on the VM. The other families only under WSL: no drive of
   their own, no restart, and no drive loss there.
5. NFS shares and a NAS (J11, J13, F01, F12), the assistant's failures (F07 to F11), a full
   backup disk (F02), J02, J05, J06, J10, J14, an upgrade from before 0.52.3, and a RAID
   mirror losing a drive.
