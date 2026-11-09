# J03 Skip a step, close setup, then resume

```
ID: J03
Purpose: A step left for later and a closed setup lose nothing; the next run continues
Preconditions: a fresh install under way (J01 up to the phone step)
Actions: press Skip for now in the window (or Ctrl+D in the terminal) at the phone step;
  at the TV playback test press Ctrl+C; read the finishing screen; run sudo bash install.sh
  again and answer the two steps
Expected: see below
Evidence: both finishing screens; the exit codes; the state file; the window's finished event
  (setup_progress); systemctl list-timers before and after
Cleanup: restore the post-install snapshot
```

## Expected results

First run:

- Skip for now and Ctrl+D leave the step pending, Ctrl+C stops setup (GUIDED-SETUP.md:225;
  `test_ctrl_d_leaves_the_step_pending_and_ctrl_c_stops_setup`, tests/test_installer_flow.py:440;
  `test_ctrl_c_records_the_step_and_ends_without_a_traceback`, tests/test_installer_flow.py:99).
- `install.sh` exits 1, the waiting code, never 5 (installer.py:40 `WAITING, STEP_FAILED`;
  `test_waiting_is_not_a_failure`, tests/test_setup_round.py:360).
- The finishing screen says `NekoServer is almost ready` and `Done: 31 of 32 steps.`
  (`headline`, wizard.py:337: fewer than half done would say `Setup stopped: n things need you`,
  `test_fewer_than_half_done_is_not_almost_ready`, tests/test_setup_round.py:454), lists the
  phone step under `What needs you (fix these first; ...)` with its own line, and ends
  with `NEXT_PENDING` (wizard.py:389): `1. Fix the first item ...` `2. Then run  sudo bash
  install.sh  again: it continues where it stopped and keeps everything done.`
- The setup window shows the same title and count (`test_the_window_gets_the_same_title_and_count`,
  tests/test_setup_round.py:469) and keeps the screen once setup has closed
  (`test_the_finishing_screen_stays_once_setup_has_closed`, tests/test_setup_round.py:370).
- The timers of the finished steps are already installed and enabled.

Second run (`sudo bash install.sh`):

- Nothing confirmed before is asked again (README.md:142; `test_confirmed_tests_are_remembered`,
  tests/test_reruns.py:75; `test_earlier_phone_confirmation_is_read`, tests/test_reruns.py:190).
- The remembered optional answers are listed, with the backup times the settings hold
  (`test_the_list_of_last_answers_shows_the_times_the_settings_hold`, tests/test_backup_times.py:539).
- The phone step and the playback test are asked; after them the first checkup runs and
  the screen says `NekoServer is ready!`, `Done: 32 of 32 steps.`; exit code 0.
- A switch left alone in the window is asked later, not lost
  (`test_a_switch_left_alone_is_asked_later`, tests/test_setup_round.py:299).

## Manual parts

Pressing Skip for now and Ctrl+C at the right moments is manual, or done by the rig's
`ask.sh` through the inbox. 0.48.1 gate: not tested (unit tests only).
