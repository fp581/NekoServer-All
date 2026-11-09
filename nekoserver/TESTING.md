# How NekoServer is tested

This is the standard every release follows, from 0.48.2 on. It names the fixed user
journeys, the safety rules, the controlled failures, the fixtures, the record format and
the release gate report. The filled-in journey records are in `tests/journeys/`, the
coverage map for the last release in `tests/COVERAGE-MAP.md`. Code references are given
as `file:line` as of the commit this file ships with; test names are stable, line numbers
drift.

## The tiers

**CI tier (under 5 minutes, every push).** `.github/workflows/ci.yml` runs the Python
suite (`python -m unittest discover -s tests`), `cargo test --locked` in `runtime/`, ruff,
rustfmt, clippy, Prettier, shfmt, and `docker compose config` on every file in `compose/`.
Everything here simulates the hardware. It holds the unit suites, the golden-file tests
(the setup window and dashboard page harnesses `tests/setup_window_harness.mjs` and
`tests/dashboard_harness.mjs`, the fixtures under `tests/fixtures/`) and the safety-rule
tests listed below. Nothing in this tier may need root, a drive, a VM or the network.

**Release gate (the rig, about 3 hours, once per release candidate).** It runs outside the
repository, on the NekoDebian VirtualBox VM and the five WSL distros (one per Linux
family). Stages, in order: a fresh install from the clean snapshot `clean-debian13-desktop`
(J01), a post-install snapshot, the drills (backups once in first_checkup's order, timers
fired by drop-in, a weekly copy on a full USB drive, retention, restores), the phone checks
over Tailscale, and the distro-specific subset on the WSL distros. A failed stage is fixed,
covered by a unit test, and rerun from the post-install snapshot; the whole gate runs
again only on the final commit. The rig scripts live in their own private repository;
their names are given here so the coverage map can point at them.

**Lab (before a release, when the code it covers changed).** `lab/nas_lab.py`: the iSCSI
interruption tests, which need a kernel with iSCSI and a VM; run by hand as root.

## The nine user journeys

The same nine journeys are run for every release. Each has a record in
`tests/journeys/<ID>-<name>.md` in the standard format below. A journey passes only when
every expected result below is seen, not when a command exits 0.

**J01 Fresh installation with default settings.** A blank PC (the VM from its clean
snapshot, three blank drives), the README steps, `sudo bash install.sh`, Enter at every
question, the phone confirmed. Expected: the finishing screen says `NekoServer is ready!`
and `Done: 32 of 32 steps.` (the count is the installer's plan, `wizard.finishing_screen`),
then `Everything is set up. Backups and monthly updates run on their own;`, the line
`Dashboard:          http://127.0.0.1:8787  (on this PC)` and `Dashboard PIN:      set`.
The timers hold the template's times (`config.json` `backup_times`: 04:15, Sun 03:00,
02:30, placed by `neko_common.job_times`): `nekoserver-music-backup.timer` 03:45 (only
when the music backup is on), `nekoserver-app-backup.timer` 04:15,
`nekoserver-host-backup.timer` 05:15, `nekoserver-immich-db-backup.timer` Sun 02:15,
`nekoserver-photo-backup.timer` Sun 03:00, `nekoserver-maintenance.timer` the 1st at
02:30, `nekoserver-updates.timer` Wed 02:30. The first checkup prints `done` under each
backup and `Checkup: N checks passed.` with no `Needs a look` line; the reports
`/srv/nekoserver/reports/jobs/{applications,host,database,external}.json` say
`"status": "pass"` and `reports/acceptance/latest.json` has every automatic check at
`pass`. The dashboard's backup cards read `Passed`. Evidence: the terminal log, the
finishing screen, `systemctl list-timers 'nekoserver-*'`, the report files, a dashboard
screenshot. Cleanup: the VM goes back to the post-install snapshot.

**J02 Installation with optional features disabled.** The TV, phone alerts, the outage
monitor and Nextcloud switched off before Start (the window's switches, or `no` in the
terminal). Expected: `"wanted": false` under each in `my-config.json`; no
`nekoserver-notifications.timer`, `nekoserver-heartbeat.timer` or Nextcloud container;
those steps count as done, so the finishing screen still says `NekoServer is ready!`;
the first checkup runs only the backups whose timers are enabled; the dashboard has no
Nextcloud card and its checklist asks for no Nextcloud restore test. Evidence: the
settings file, `systemctl list-timers`, `docker ps`, the checklist. Cleanup: snapshot.

**J03 Skip a step, close setup, then resume.** Skip for now (or Ctrl+D) at the phone step,
Ctrl+C at a later waiting step. Expected: `install.sh` exits 1 (waiting, not 5 = a failed
step, `installer.py` `WAITING, STEP_FAILED`); the finishing screen says `NekoServer is
almost ready`, `Done: 31 of 32 steps.` and lists the phone step under `What needs you`;
the setup window shows the same title and count. `sudo bash install.sh` again asks no
confirmed question again and ends `NekoServer is ready!`, `Done: 32 of 32 steps.`
Evidence: both finishing screens, the state file, the window's `finished` event.

**J04 Switch between browser and terminal answers.** One question answered in the window,
the next in the terminal, then the backup-times page in the window and a plain question
after it. Expected: whichever answer comes first counts, once; the window shows the
terminal's answer; after the page, Send still works for the plain question; a weekday
typed `sat` reaches the page as `Sat`; nothing is asked twice. Evidence: the terminal
log, the window's events (`setup_progress`), the saved answers.

**J05 Add or disable an app after installation.** Take one app out of `media.apps`, rerun
setup, put it back, rerun. Expected: the removed app's container is stopped and its files
kept; the media monitor and the dashboard follow the list; Prowlarr's and Seerr's links to
it are taken away; the app comes back with its password step and appears again in the
health report. Evidence: `docker ps`, the dashboard's app list, the credentials file's
keys (never its values).

**J06 Change backup times through full and partial setup.** A full run with new answers
(say 23:00, Mon 01:00, 03:30), then `sudo bash install.sh --module host_backup --apply`.
Expected: setup says which jobs move before saving; the timers read music 22:30, app
23:00, host 00:00, Immich database Mon 00:15, USB copy Mon 01:00, monthly the 1st at
03:30, updates Wed 03:30; a rerun lists those times and Enter logs `Backup times kept as
they were`; the partial run asks nothing and says `Backup times are changed only by a
full setup run (sudo bash install.sh); this run keeps ...`; the witnessed restore
results on the dashboard are marked outdated after a change. Evidence: `systemctl cat`
of each timer, `backup_times` in the settings, the log lines.

**J07 Update one app, fail its update, and roll it back.** A monthly (or weekly update
night) run with one app's new image made to fail its health check, and once more with
the service stopped in the middle. Expected: a snapshot with `manifest.json` under
`backups/monthly/` exists before any image changes; the rollback puts back the images
and config files that run saved; the app answers at its old version; the report says
`fail` with a sanitized reason and that it tries again tomorrow night; after the stop,
`nekoserver-maintenance-resume.service` reads `/etc/nekoserver-backups/maintenance-resume.json`
and finishes or rolls back; the journal is gone when it ends. Evidence: the report, the
journal before and after, `docker image ls`, the app's version page.

**J08 Upgrade NekoServer from an older installation.** A fresh 0.47.9 install, then the
dashboard's upgrade to the release under test. Expected: the older release's kept copy stays
in its own folder under `/srv/nekoserver/installer/releases/`; the upgrade runs the profile the server
was set up with; every installed service runs the new release; the dashboard answers
with the new version; the settings keep each job's own time (a server from before 0.48.0
has no `backup_times`); `/etc/nekoserver-backups/upgrade.json` is removed at the end.
With the check made to fail: the release you had is put back and checked. Evidence: the
upgrade log in `installer/upgrades/`, `systemctl show` of the services, the dashboard.

**J09 Replace a drive and restore onto a separate system.** A fresh install on a clean VM
with blank drives, the old USB drive's contents put on the new one, then the recovery
menu (`sudo bash install.sh --recover`): photos, Nextcloud files, app settings, a
monthly snapshot. Expected: every restored file matches the checksum recorded when it
was put in (adddata.py's `r18-data.json`); restores go into a new folder only and are
checked against `.neko-ledger.json`; a verified file that is missing stops the restore
and is named; a replaced HDD is refused while the settings name the old UUID, and a
replacement drive keeps nothing of the old one. Evidence: the checksum comparison, the
menu's output, the refusal line.

## The seven safety rules

Each rule holds across many tests. The invariant is stated for NekoServer, with the
code that enforces it and the tests that already assert it. A new test that touches a
rule names its ID in a comment. Rust tests are in `#[cfg(test)]` blocks of the file named.

**S01 Missing storage never redirects writes onto the OS disk.** Every backup job runs
with the guard file setup writes, `/etc/nekoserver-backups/storage.json`
(`backup_guard`, `native_runtime.py:311`). The runtime's `guard_data`
(`runtime/src/backup.rs:2842`) refuses a job whose `data_mount` is not the mounted drive
with the identity the guard names, and `execute_keeping` (`runtime/src/backup.rs:2726`)
stops with `Application SSD missing` when nothing is mounted there. Tests:
`the_ssd_check_refuses_the_right_drive_with_the_wrong_subvolume` (runtime/src/backup.rs:8216),
`nothing_is_locked_or_resumed_on_the_wrong_drives` (runtime/src/backup.rs:5241),
`guard_config_does_not_allow_root_mount` (runtime/src/startup.rs:437),
`test_the_ssd_data_root_is_never_set_without_the_drive_guard` (tests/test_setup_flow.py:372),
`test_a_restore_never_writes_onto_the_system_disk_when_the_hdd_is_missing` (tests/test_wizards.py:1544),
`test_repair_never_writes_onto_another_drive_at_the_hdd_folder` (tests/test_wizards.py:1213),
`test_nothing_is_written_while_the_ssd_is_not_mounted` (tests/test_upgrade.py:940),
`test_the_answer_is_never_written_where_the_ssd_is_missing` (tests/test_system_updates.py:164).

**S02 Wrong UUID, subvolume or NAS identity blocks the operation.** Drives are mounted by
UUID only (`modules/storage.py:35`), two devices with one UUID are clones and stop setup
(`CLONED`, `modules/storage.py:77`; `pool_members`, `modules/storage.py:192`); the runtime
checks UUID, filesystem type and Btrfs subvolume together (`guard_data`, above); a NAS
disk is known by its WWN and refused without one (`modules/network_storage.py:652`).
Tests: `a_btrfs_subvolume_is_part_of_the_drive_identity` (runtime/src/backup.rs:4905),
`the_subvolume_is_checked_the_same_with_or_without_findmnt` (runtime/src/backup.rs:4574),
`test_a_cloned_btrfs_drive_is_not_taken_for_a_pool` (tests/test_setup_flow.py:547),
`test_two_clones_of_one_drive_still_stop_setup` (tests/test_setup_flow.py:593),
`test_a_folder_of_an_ext4_drive_mounted_there_is_refused_not_saved_as_a_subvolume` (tests/test_storage_identity.py:82),
`test_a_nas_disk_that_shows_no_wwn_is_refused_as_the_runtime_refuses_it` (tests/test_network_storage.py:619),
`test_a_drive_on_another_nas_disk_than_the_settings_name_is_refused` (tests/test_migrate.py:463),
`test_a_subvolume_the_settings_name_is_never_swapped_for_the_mounted_one` (tests/test_migrate.py:362),
`test_the_wwn_check_looks_at_the_mounted_device_not_the_uuids_link` (tests/test_wizards.py:1799).

**S03 Failed inspection never means "safe", "empty" or "healthy".** `lsblk_row`
(`modules/network_storage.py:396`) raises when lsblk fails, answers unreadably or lists no
disk, and `listed_disks` (`modules/network_storage.py:652`) then stops setup with `LUN n
could not be inspected`. The finishing screen counts a step with no record as `missing`,
never as done (`wizard.py:430`); a backup that left no report is `fail` (`first_checkup.py:39`);
a dashboard check that could not run turns `stale`, never stays green
(`runtime/src/collect.rs:1348`). Tests:
`test_a_lun_that_cannot_be_inspected_stops_setup_instead_of_passing_as_empty` (tests/test_network_storage.py:263),
`test_a_failed_checkup_is_never_ready` (tests/test_wizards.py:376),
`test_the_finishing_screen_judges_against_the_templates_steps` (tests/test_setup_flow.py:1080),
`test_a_pool_whose_layout_cannot_be_read_counts_only_its_smallest_drive` (tests/test_setup_flow.py:444),
`test_a_restart_check_that_cannot_answer_is_not_known_never_not_needed` (tests/test_system_updates.py:200),
`a_check_that_could_not_run_since_the_last_report_makes_it_stale` (runtime/src/collect.rs:3066),
`an_unreadable_journal_is_reported_not_taken_for_absent` (runtime/src/control.rs:2571),
`a_refused_stop_is_never_taken_for_a_stop` (runtime/src/nas_watch.rs:480),
`heat_or_a_lost_sensor_means_full_speed` (runtime/src/fans.rs:868).

**S04 Interrupted work retains enough information to recover.** Every long job keeps a
journal under `/etc/nekoserver-backups/` with a boot service that reads it
(`runtime/src/shared.json` `recovery`: `app-resume.json`, `host-resume.json`,
`maintenance-resume.json`, `immich-resume.json`, `nextcloud-resume.json`, `upgrade.json`,
`immich-snapshot-resume.json`; `migrate.json` for a drive move). `needs_resume`
(`runtime/src/backup.rs:2879`) treats a report still saying `running` as work to finish;
the maintenance journal carries the drive identities and is used only when they match
(`runtime/src/maintenance.rs:1822`); the upgrade's record is written at each phase
(`upgrade.py:83`). Tests:
`a_resume_has_work_only_with_a_journal_or_a_cut_off_report` (runtime/src/backup.rs:4747),
`services_are_resumed_only_after_the_drives_and_the_lock_are_checked` (runtime/src/backup.rs:5290),
`a_written_snapshot_says_so_in_the_journal` (runtime/src/host_snapshot.rs:569),
`review_corrupt_journal_is_reported_and_retained_for_diagnosis` (runtime/src/control.rs:2661),
`test_a_cut_off_upgrade_is_put_right_at_the_next_start` (tests/test_upgrade.py:691),
`test_a_download_or_check_cut_off_part_way_never_leaves_the_record_behind` (tests/test_upgrade.py:560),
`test_a_move_cut_off_before_its_switch_whose_apps_ran_again_starts_over` (tests/test_migrate.py:575),
`test_after_a_cut_off_snapshot_the_hook_restarts_immich_and_clears_itself` (tests/test_install_order.py:1191),
`test_a_run_cut_off_after_copying_files_makes_the_menu_warn` (tests/test_restore_as_of.py:134),
`test_an_interrupted_recovery_finishes_at_the_next_setup_run` (tests/test_review_fixes.py:2692).

**S05 Backups verify their contents before being reported successful.** Every copied
file's SHA-256 goes into the ledger (`Ledger::record`, `runtime/src/ledger.rs:159`,
`.neko-ledger.json`) and a copy is published only when stream, disk and source agree;
the check results go to `reports/integrity.json` (`runtime/src/backup.rs:849`); a job that
left files out ends `partial`, never `pass` (`execute_with`, `runtime/src/backup.rs:2716`).
Tests: `a_copy_is_published_only_when_stream_disk_and_source_checksums_agree` (runtime/src/backup.rs:5348),
`hdd_usb_and_the_ledger_decide_which_copy_is_good` (runtime/src/backup.rs:5395),
`files_without_a_checksum_stay_unverified_until_two_copies_agree` (runtime/src/backup.rs:5446),
`a_stale_or_unrecorded_database_copy_makes_the_usb_backup_incomplete` (runtime/src/backup.rs:5209),
`a_recorded_checksum_is_never_replaced` (runtime/src/ledger.rs:384),
`test_app_snapshot_verifies_restores_and_catches_tampering` (tests/test_snapshot_restore.py:50),
`test_a_copy_that_reads_back_differently_is_refused` (tests/test_music_recovery.py:85),
`test_an_archive_changed_after_its_check_is_never_unpacked_or_marked` (tests/test_monthly_recovery.py:106),
`test_restored_files_are_checked_against_their_recorded_checksums` (tests/test_wizards.py:1003),
`test_a_restore_stops_when_a_verified_file_is_missing` (tests/test_wizards.py:1450),
`test_python_reads_the_ledger_the_runtime_writes` (tests/test_integration.py:1064).

**S06 Failed updates preserve a usable recovery path.** The monthly update snapshots
first and rolls back to that snapshot; a NekoServer upgrade keeps the previous release's
copy on the SSD (`kept_release`, `upgrade.py:169`) and is refused without it
(`upgrade_held`, `upgrade.py:537`); the way back (`go_back`, `upgrade.py:613`) is checked
before it is called done, and runtime copies it hid get their names back
(`show_hidden`, `upgrade.py:689`). Tests:
`a_rollback_puts_back_only_what_its_run_saved` (runtime/src/maintenance.rs:6808),
`a_rollback_puts_back_earlier_images_docker_no_longer_has` (runtime/src/maintenance.rs:7615),
`rollback_with_encryption_restores_the_left_out_config_files` (runtime/src/maintenance.rs:6026),
`test_a_failed_setup_or_check_puts_the_release_you_had_back` (tests/test_upgrade.py:325),
`test_a_new_step_that_failed_puts_the_release_you_had_back` (tests/test_upgrade.py:384),
`test_putting_the_release_back_is_checked_before_it_is_called_done` (tests/test_upgrade.py:632),
`test_the_old_release_guard_lets_the_way_back_run_its_setup` (tests/test_upgrade.py:403),
`test_a_copy_hidden_by_a_cut_off_way_back_gets_its_name_back_when_the_record_is_closed` (tests/test_upgrade.py:772),
`test_this_release_finishes_an_earlier_releases_update` (tests/test_finish_update.py:87).

**S07 Passwords never appear in public reports or logs.** Every reason written to
`reports/` or shown on the dashboard goes through `sanitize` (`runtime/src/common.rs:883`),
which removes anything that looks like a password or key; saved app definitions are
scrubbed after an update; the password list is sealed before it is copied
(`seal_passwords`, `modules/photo_backup.py:413`); passwords travel on stdin or in a
root-only file, never on a command line; the setup window's event log keeps questions,
never their answers. Tests:
`failure_reasons_keep_the_cause_but_not_secrets` (runtime/src/common.rs:1270),
`a_run_that_failed_before_its_journal_keeps_no_passwords` (runtime/src/maintenance.rs:6106),
`scrub_blanks_passwords_in_every_saved_model` (runtime/src/maintenance.rs:6128),
`plain_data_archive_leaves_out_password_files` (runtime/src/maintenance.rs:6166),
`password_files_are_encrypted_and_open_only_with_the_password` (runtime/src/maintenance.rs:6244),
`setup_seals_a_password_list_before_the_first_monthly_snapshot` (runtime/src/backup.rs:7705),
`test_password_never_on_command_line` (tests/test_backup_secrets.py:256),
`test_a_chap_password_is_written_in_the_targets_settings_never_on_a_command_line` (tests/test_network_storage.py:193),
`test_instructions_show_address_and_username_never_password` (tests/test_phone_guide.py:52),
`test_the_qr_code_gets_the_password_on_its_input_not_its_command_line` (tests/test_show_login.py:97),
`test_questions_are_seen_while_they_wait_and_never_their_answers` (tests/test_setup_gui.py:86),
`test_the_pin_is_asked_twice_and_only_passed_to_the_runtime` (tests/test_dashboard.py:505).

## The six controlled failures

Failures are introduced at named checkpoints, in a disposable VM or on test storage,
never against anyone's only data.

| ID | Failure | What we verify | Covered today by |
| --- | --- | --- | --- |
| F01 | NAS disappears | Writes stop safely; recovery is possible | `runtime/src/nas_watch.rs` tests (`the_apps_stop_once_on_a_failing_drive_and_start_only_when_all_take_writes`, runtime/src/nas_watch.rs:435), the recovery menu's read-only check, `lab/nas_lab.py` (lab, not the gate) |
| F02 | Backup disk fills | No false success; existing backups survive | rig `usb-full.sh` (VM); `a_full_drive_removes_the_oldest_snapshots_but_keeps_the_newest` (runtime/src/backup.rs:7092), `unfinished_runs_make_room_on_a_full_hdd_but_complete_ones_stay` (runtime/src/maintenance.rs:6653), `test_a_drive_that_fills_up_mid_restore_says_what_is_there` (tests/test_wizards.py:1827) |
| F03 | Process stops mid-update | Resume or rollback uses its journal correctly | rig `cut.sh` (an upgrade cut off, then `nekoserver-upgrade-resume`) and the fault suite's cut-off monthly run; `test_a_cut_off_upgrade_is_put_right_at_the_next_start` (tests/test_upgrade.py:691); not yet a gate stage |
| F04 | API returns 409/500 | UI explains the problem and permits recovery | `test_a_refused_start_gives_the_button_back_with_the_reason` (tests/test_setup_flow.py:174), `test_error_codes_are_explained_everywhere` (tests/test_dashboard.py:67), `test_a_body_too_large_or_broken_is_refused` (tests/test_setup_gui.py:620); a 500 from the runtime to the dashboard page: not yet covered |
| F05 | Settings are malformed | Existing settings remain untouched | `test_a_settings_file_that_cannot_be_read_is_never_written_over` (tests/test_setup_gui.py:448), `test_a_setting_that_could_not_be_read_is_never_overwritten` (tests/test_gateway_cloud_origin.py:144), `test_changes_are_never_saved_into_the_template` (tests/test_setup_flow.py:42), `test_a_wrong_time_is_refused_in_plain_words` (tests/test_backup_times.py:118); no rig stage |
| F06 | Health report is stale | Dashboard cannot present it as current proof | `only_a_complete_check_turns_stale` (runtime/src/collect.rs:3554), `stale_input_never_recovers_reported_problem` (runtime/src/notify.rs:844), `test_a_changed_schedule_makes_witnessed_results_outdated` (tests/test_backup_times.py:194), `test_a_new_usb_drive_archive_or_schedule_makes_it_outdated` (tests/test_acceptance_scope.py:40); the page shows `Overdue` (`dashboard/app.js:215`); no rig stage moves the clock past a report |

## Compatibility fixtures

Every release must show that it still reads its supported historical formats. What
exists under `tests/` today:

- `tests/fixtures/jellyfin_key_cases.json`: Jellyfin API key cases (used by `tests/test_review_fixes.py`).
- `tests/old_release_guard_0479.py`: the old-release guard exactly as 0.47.9 shipped it, for the way-back test.
- Older settings shapes are built inline in test code, not kept as files: settings from
  before 0.48.0 without `backup_times` (`tests/test_backup_times.py:95`, `:380`, `:458`),
  0.46.3's upgrade record without a units list (`upgrade.py:475`, `tests/test_upgrade.py`),
  pre-0.18 settings without switches (`installer.py:251`), NAS settings in
  `tests/test_network_storage.py`, drive moves in `tests/test_migrate.py`.
- Manifests and journals are also inline: monthly `manifest.json` and resume journals in
  `runtime/src/backup.rs`, `runtime/src/maintenance.rs`, `tests/test_wizards.py`,
  `tests/test_snapshot_restore.py`, `tests/test_backup_secrets.py`; inventory lists with
  and without checksums in `tests/test_restore_as_of.py:309`.

What is missing, to add under `tests/fixtures/` with one loader test each:
`settings/v0.37.x.json`, `v0.46.3.json`, `v0.47.9.json`, `v0.48.0.json` (a `my-config.json`
as setup left it on the rig); `manifests/` (one monthly `manifest.json` per format, an
app snapshot's `.neko-names.json`, a `.neko-ledger.json`); `journals/` (an interrupted
`maintenance-resume.json` of format `neko-maintenance-resume-v1`, an `app-resume.json`,
an `upgrade.json` from 0.46.3 and from 0.48.0); `reports/` (one `reports/jobs/*.json`
of each status from 0.47.10). A fixture is never edited; a new format gets a new file.

## The standard test record

Every case, in `tests/journeys/` or in a test's docstring, has these fields:

```
ID: UI-SETUP-007
Purpose: Hidden backup fields cannot block the next question
Preconditions: Backup-time page is open
Actions: Clear a time; finish in terminal; answer next browser question
Expected: Answer submits successfully
Evidence: Browser result and received API request
Cleanup: Close fixture and remove temporary state
```

IDs: `J01`..`J09` for journeys, `S01`..`S07` for safety rules, `F01`..`F06` for
controlled failures, `UI-SETUP-nnn`, `UI-DASH-nnn`, `REC-nnn` for single cases, and
`B-<release>-<nn>` for bugs (below). The record says what is manual.

## The release gate report

The report for a release candidate names every journey, rule, failure and gate stage with
one of four words: **passed**, **failed**, **skipped** (a stage chosen not to run, with
the reason), **not tested** (no hardware, no script, or the integration was unavailable).
A green CI run never stands in for a stage. Hardware that was not there is `not tested`,
never `passed`. A stage rerun from the post-install snapshot says so. The changelog's
"How it was tested" section carries the summary and `tests/COVERAGE-MAP.md` the table.
A known bug's reproduction becomes a test that asserts the correct behavior; a test that
passes because the bug still occurs is a bug of its own.

## Seeded randomness

Generated input (file names, timestamps, unusual combinations of answers) comes from a
seeded generator: the rig's `adddata.py` and the answer rules (`mkrules.py`) take a seed,
print it on the first line of the stage log, and save the failing input beside the stage
report. A failure is rerun with the same seed first. Exploratory testing stays, with a
checklist and recorded observations in the journey record's Evidence field.

## Bug IDs

A bug gets the ID `B-<release without dots>-<nn>` in the order of its changelog entry
(`B-0481-07` is the seventh entry of 0.48.1). The coverage map lists each ID with the
changelog line, the regression test that fails without the fix (file and test name), and
the journey or rule it belongs to. A fix without such a test is listed as `no test` and
is the first thing the next release adds. Tests name the ID in their docstring or a
comment, so `grep B-0481-07` finds the bug, its test and its journey.
