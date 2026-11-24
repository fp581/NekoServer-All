# J07 Update one app, fail its update, and roll it back

```
ID: J07
Purpose: A failed app update leaves the app at its old version with its data, and says so
Preconditions: a finished install (post-install snapshot) with data in the apps (adddata.py);
  one app's new image made to fail (a local registry tag whose container exits at start)
Actions: start nekoserver-updates.service (or move nekoserver-maintenance.timer with a drop-in,
  rig fire-timer.sh); once more, stop the service in the middle of the image pull
  (systemctl stop nekoserver-maintenance.service), then reboot
Expected: see below
Evidence: the maintenance report in reports/jobs/; /etc/nekoserver-backups/maintenance-resume.json
  before and after; docker image ls; the app's version page; the dashboard's updates card
Cleanup: restore the post-install snapshot; remove the local registry tag
```

## Expected results

The failed update:

- A snapshot with `manifest.json` under `backups/monthly/` on the HDD exists before any
  image changes (the monthly run's order: snapshot, restore test, update; README.md:222).
- The rollback puts back exactly the images and config files that run saved
  (`a_rollback_puts_back_only_what_its_run_saved`, runtime/src/maintenance.rs:6808;
  `a_rollback_puts_back_earlier_images_docker_no_longer_has`, runtime/src/maintenance.rs:7615;
  `rollback_with_encryption_restores_the_left_out_config_files`, runtime/src/maintenance.rs:6026).
- The app answers at its old version and checkdata.py prints `OK` for every file.
- The report says `fail`, with a reason that names the app and keeps no password
  (`sanitize`, runtime/src/common.rs:883), and that it tries again tomorrow night; on the
  night the clocks go back, tomorrow is the calendar day after today
  (`the_retry_on_the_night_the_clocks_go_back_is_set_for_tomorrow_not_today`,
  runtime/src/maintenance.rs:8013; B-0481-18).
- The dashboard's updates card shows the failure and the app at its old version; a new
  major version is only shown until approved (README.md:222).

The weekly update night, or **Update now** (since 1.0.0 the gate's own drill: one app pinned
to its previous build, its **Update now** started, and the new image's container killed while
it is checked):

- The earlier image is back and the app's container runs on it; the app's page answers with
  its settings as before.
- The report says `fail` with `rolled_back` true and the reason in plain words:
  `<app> <version>: <what failed>; undone, <app> runs its previous version and the update is
  tried again next week`; the update stays pending.
- The dashboard shows a warning, `Weekly app updates: the update was undone, your apps work`,
  with `The update was undone: your apps are back on their previous version and work as
  before. It is tried again next week. Why: ...`, never `Urgent`
  (`a_rolled_back_weekly_update_says_the_apps_are_back_and_working`, runtime/src/collect.rs).
  An app left broken after another app's update was undone that night makes the night a
  failure (`an_app_left_broken_after_an_undone_one_makes_the_night_a_failure`,
  runtime/src/maintenance.rs).
- **Update now** again, with nothing killed, installs it; pending no longer lists it and the
  alert is gone after the next collect.

The stopped update:

- The journal `/etc/nekoserver-backups/maintenance-resume.json` exists while the run is
  cut off, with format `neko-maintenance-resume-v1` and the drive identities
  (runtime/src/maintenance.rs:1822).
- At boot `nekoserver-maintenance-resume.service` reads it and finishes or rolls back; it
  waits for a backup holding the lock (`test_the_boot_resume_waits_for_a_backup_holding_the_lock`,
  tests/test_install_order.py:1135) and never runs on other drives
  (`nothing_is_locked_or_resumed_on_the_wrong_drives`, runtime/src/backup.rs:5241).
- A run cut off in the middle is called `running` while it runs and never `cut off`
  (`test_a_monthly_update_running_right_now_is_called_running_not_cut_off`, tests/test_install_order.py:1327).
- The journal is gone when the resume ends; an unfinished update is tried again every
  hour while it is there (`nekoserver-maintenance-retry.timer`, modules/maintenance.py:142;
  `test_an_unfinished_update_is_tried_again_every_hour_only_while_it_is_there`, tests/test_finish_update.py:30).

## Manual parts

Making an image fail needs the rig's local registry; the cut-off run is the fault
suite's `monthly cut off while updating` scenario (rig `fault.sh`, `fault-any.sh`), run
in earlier rounds on the WSL distros, not yet a stage of the VM gate.

0.48.1 gate: not tested (unit tests and earlier-round rig scripts).

1.0.0 round (`tests/COVERAGE-MAP.md`): partly proved. The weekly drill above on the VM:
37843d5 rolled back right, but the dashboard said `Urgent` (T1-R11); on 7efba8a the alert is
the warning above and Update now then installed it. The monthly run's roll-back on a real
install was not reached: on NekoArch v0.52.3 (T2-monthly) every run stopped, correctly, at the
check before the update, for the rig's own settings. The stopped update was not driven. The
roll-back drill on the final commit 597c800: pending.
