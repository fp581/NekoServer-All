# J06 Change backup times through full and partial setup

```
ID: J06
Purpose: New backup times move every timer together and are kept; a partial run never moves them
Preconditions: a finished install (post-install snapshot) at the template's times
Actions: sudo bash install.sh; at the drives step answer 23:00 (daily), Mon and 01:00 (weekly),
  03:30 (monthly); let it finish. Then sudo bash install.sh again and press Enter at the times.
  Then sudo bash install.sh --module host_backup --apply
Expected: see below
Evidence: systemctl cat of each timer after each run; backup_times in my-config.json; the log
  lines quoted below; the dashboard's checklist
Cleanup: restore the post-install snapshot
```

## Expected results

Full run with new answers (`choose_backup_times`, guided_setup.py:434):

- Before saving, setup says which jobs move, in words (guided_setup.py:518, `moves from ... to ...`;
  `test_a_changed_time_on_a_server_set_up_before_says_which_jobs_move`, tests/test_backup_times.py:477).
- `my-config.json` holds `"backup_times": {"daily": "23:00", "weekly_day": "Mon", "weekly": "01:00", "monthly": "03:30"}`.
- The timers (`job_times`, neko_common.py:1398; `test_the_nightly_jobs_keep_their_distance_from_the_chosen_time`,
  tests/test_backup_times.py:76; `test_the_nightly_timers`, tests/test_backup_times.py:146;
  `test_the_weekly_timers`, tests/test_backup_times.py:159;
  `test_the_monthly_timers_and_the_runtimes_copy_of_the_time`, tests/test_backup_times.py:175):

| Unit | OnCalendar |
| --- | --- |
| nekoserver-music-backup.timer (if on) | `*-*-* 22:30:00 <zone>` |
| nekoserver-app-backup.timer | `*-*-* 23:00:00 <zone>` |
| nekoserver-host-backup.timer | `*-*-* 00:00:00 <zone>` |
| nekoserver-immich-db-backup.timer | `Mon *-*-* 00:15:00 <zone>` (before 00:45 it lands on the day before: `day_before_if_wrapped`, neko_common.py:1356) |
| nekoserver-photo-backup.timer | `Mon *-*-* 01:00:00 <zone>` |
| nekoserver-maintenance.timer | `*-*-01 03:30:00 <zone>` |
| nekoserver-updates.timer | `Wed *-*-02..31 03:30:00 <zone>` |

- A monthly time at or after `maintenance.night_end_hour` is refused and the latest time
  before the night's end is offered (guided_setup.py:591;
  `test_a_monthly_time_no_longer_at_night_is_offered_the_latest_time_that_is`, tests/test_backup_times.py:553).
- The witnessed restore results on the dashboard's checklist are marked outdated
  (`test_a_changed_schedule_makes_witnessed_results_outdated`, tests/test_backup_times.py:194).

Rerun with Enter:

- The `Last time, the optional questions were answered this way` list shows 23:00, Mon
  01:00, 03:30, the times the settings hold (`test_the_list_of_last_answers_shows_the_times_the_settings_hold`,
  tests/test_backup_times.py:539); Enter keeps them and the log says `Backup times kept as
  they were` (guided_setup.py:479; `test_enter_keeps_the_times_shown_and_the_answer_is_kept`,
  tests/test_backup_times.py:221). The timers are unchanged.
- When the settings and the remembered answer differ, setup says so and asks again
  (guided_setup.py:461; `test_a_remembered_answer_that_is_not_what_the_settings_hold_is_asked_again`,
  tests/test_backup_times.py:507).

Partial run (`--module host_backup --apply`): setup asks nothing and says `Backup times
are changed only by a full setup run (sudo bash install.sh); this run keeps ...`
(guided_setup.py:453; `test_a_run_that_does_not_write_every_timer_again_keeps_the_times_and_asks_nothing`,
tests/test_backup_times.py:609; `test_the_drives_step_asks_only_when_the_run_writes_a_timer`,
tests/test_backup_times.py:654). `nekoserver-host-backup.timer` still reads 00:00.

## Manual parts

None: the rig's rules answer the times. 0.48.1 gate: partly proved (the rerun showed and
kept the times; the partial-run keep is unit-tested only).
