# J02 Installation with optional features disabled

```
ID: J02
Purpose: A server without the TV, phone alerts, the outage monitor and Nextcloud is complete
  and asks for nothing it does not have
Preconditions: NekoDebian VM at clean-debian13-desktop; blank drives
Actions: in the setup window switch off TV, phone alerts, outage monitor and Nextcloud before
  Start (or answer no in the terminal when each is asked); Enter at every other question
Expected: see below
Evidence: my-config.json (the wanted flags); systemctl list-timers 'nekoserver-*'; docker ps;
  the dashboard's checklist; the finishing screen
Cleanup: restore the post-install snapshot
```

## Expected results

- `my-config.json` holds `"wanted": false` under `tv`, `notifications`, `heartbeat` and
  `nextcloud` (the window's extras: `EXTRAS`, explanations.py:72; the terminal's yes/no:
  GUIDED-SETUP.md:236; `test_extras_read_and_write_the_settings`, tests/test_setup_gui.py:54). The step counts as done, so the finishing screen still
  says `NekoServer is ready!` with every step of the plan done (wizard.py:401 `finished`).
- No `nekoserver-notifications.timer` and no `nekoserver-heartbeat.timer` are installed
  (`test_phone_alerts_off_stop_their_timer`, tests/test_extras_off.py:45;
  `test_the_outage_monitor_off_stops_its_timer_and_says_to_pause_the_monitor`,
  tests/test_extras_off.py:50). No Nextcloud container runs and no `nextcloud` compose
  folder exists under the SSD.
- The first checkup runs only the backups whose timers are enabled
  (`scheduled`, first_checkup.py:26; `test_only_switched_on_backups_run_in_order_then_the_checkup`,
  tests/test_wizards.py:329): applications, host, database, external, each `done`.
- The USB copy's report `/srv/nekoserver/reports/jobs/external.json` says `pass` and
  names no Nextcloud files; the dashboard's checklist asks for the photo restore test
  only (`test_nextcloud_only_asks_for_a_nextcloud_restore_not_photos`,
  tests/test_restore_tried.py:48; `test_a_switched_off_backup_is_not_an_issue`,
  tests/test_restore_tried.py:108).
- The dashboard has no Nextcloud card, no TV card, and no alert about phone alerts or the
  outage monitor.
- A later `sudo bash install.sh` does not ask those four again; setting a flag to `true`
  by hand and rerunning sets the feature up (GUIDED-SETUP.md:236).

## Manual parts

Checking the dashboard cards is by eye (a screenshot is the evidence). The rest is
driven by the rig's rules with the four switches off.

0.48.1 gate: not tested (unit tests only).
