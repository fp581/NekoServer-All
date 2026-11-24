# J08 Upgrade NekoServer from an older installation

```
ID: J08
Purpose: A server set up by an older release is upgraded whole, keeps its own times and settings,
  and has a checked way back
Preconditions: the previous release installed with data in the apps (the VM's checkpoint of it:
  0.52.3 for 1.0.0); for the older path, a fresh 0.47.9 install (rig fresh-old.sh)
Actions: the new release's sudo bash install.sh, or the dashboard's upgrade (or sudo python3
  upgrade.py <tag>), to the release under test;
  read the upgrade log; then, from the snapshot again, make the check fail (rig stuck-way-back.sh
  upgrade, then stuck-way-back.sh resume) and read what the server runs
Expected: see below
Evidence: the log in /srv/nekoserver/installer/upgrades/; systemctl show of every nekoserver unit;
  the dashboard's version; ls /srv/nekoserver/installer/releases; /etc/nekoserver-backups/upgrade.json
Cleanup: restore the 0.47.9 snapshot
```

## Expected results

The upgrade that works:

- Setup's first page reads `Review this NekoServer update` and `From <old> to <new>`, and the
  saved answers are kept.
- A server from before 1.0.0 moves from the `beta` channel to `stable` once, without a question,
  and setup says: `Updates: this server is offered stable NekoServer releases only from now on
  (stable is the default since 1.0.0). To be offered betas again, set "nekoserver_channel":
  "beta" under "maintenance" in my-config.json and run setup again`
  (`test_a_beta_channel_from_before_1_0_0_moves_to_stable_once`, tests/test_installer_flow.py);
  a server already on `stable` is left as it is (`test_a_stable_channel_is_left_as_it_is`,
  tests/test_installer_flow.py); a server that chose no channel is offered stable releases only
  (`a_server_that_chose_no_channel_is_offered_stable_releases_only`, runtime/src/updates.rs).

- The older release's copy stays at `/srv/nekoserver/installer/releases/v0.47.9`
  (`kept_release`, upgrade.py:169); without it the upgrade is refused with `This release's
  files are not kept on the SSD` (upgrade.py:552).
- Setup runs the profile the server is set up with, without questions and without a system
  update (`test_setup_runs_the_profile_this_server_is_set_up_with`, tests/test_upgrade.py:212;
  `test_setup_runs_without_questions_and_without_a_system_update`, tests/test_upgrade.py:846).
- Every installed service runs the new release and the dashboard answers with its version
  on that release's port (`test_every_installed_service_must_run_the_new_release`,
  tests/test_upgrade.py:660; `test_the_dashboard_is_asked_on_the_port_that_release_uses`,
  tests/test_upgrade.py:652).
- The settings keep each job's own time: a server from before 0.48.0 has no `backup_times`,
  so `job_times` reads `app_backup.time`, `music.backup_time`, `host_backup.time`,
  `photos.*` and `maintenance.time` (`_own_time`, neko_common.py:1390;
  `test_settings_from_before_keep_each_jobs_own_time`, tests/test_backup_times.py:95;
  `test_a_server_set_up_before_is_offered_its_own_times`, tests/test_backup_times.py:380).
- `/etc/nekoserver-backups/upgrade.json` is removed at the end and only the newest logs
  stay (`test_a_finished_upgrade_leaves_only_its_newest_logs`, tests/test_upgrade.py:255).
- checkdata.py prints `OK` for every file and login.

The upgrade whose check fails:

- A failed step ends setup with 5, a step that waits with 1, and a refusal by install.sh
  is read from the log (`setup_problem`, upgrade.py:242;
  `test_a_failed_step_and_a_refusal_by_install_sh_are_failed_setups`, tests/test_upgrade.py:346; B-0481-14).
- The release you had is put back and checked before it is called done
  (`test_a_failed_setup_or_check_puts_the_release_you_had_back`, tests/test_upgrade.py:325;
  `test_putting_the_release_back_is_checked_before_it_is_called_done`, tests/test_upgrade.py:632).
- 0.47.9's own guard lets the way back run (`test_the_old_release_guard_lets_the_way_back_run_its_setup`,
  tests/test_upgrade.py:403; the guard as shipped: tests/old_release_guard_0479.py).
- A runtime copy hidden by a way back that was cut off gets its name back when the record
  is closed (`test_a_copy_hidden_by_a_cut_off_way_back_gets_its_name_back_when_the_record_is_closed`,
  tests/test_upgrade.py:772; B-0481-13); no later setup fails with `Refusing symlink path`.
- The report says each ending as the dashboard reads it
  (`test_each_ending_is_reported_as_the_dashboard_reads_it`, tests/test_upgrade.py:905).

## Manual parts

The rig scripts `fresh-old.sh`, `stuck-way-back.sh` and `cut.sh` (an upgrade cut off,
then `nekoserver-upgrade-resume.service`) drive it; reading the dashboard is by eye.
0.48.1 gate: not tested (unit tests and the earlier-round rig scripts).

1.0.0 round (`tests/COVERAGE-MAP.md`): partly proved. VM, from the installed v0.52.3
checkpoint by the new release's setup: to e09cdbd (33 of 34, the phone step waiting), to 37843d5
(T1-gate-37843d5: the first page, the answers kept, the one-time move to stable said as above,
16 of 16 containers, the dashboard on 1.0.0 with no alert, a restart fine) and to 7efba8a. Not
run: the upgrade from 0.47.9, the dashboard's upgrade button, and the check made to fail. On the
final commit 597c800 the channels drill passed (T1-final-8), and so did the upgrade cut by a
power cut mid-run (T1-final-7: still 0.52.3 and working after the boot, then "Continue
unfinished setup" to 1.0.0); on e2da4a2 the upgrade with a restart after it (T1-proof-8).
Betas being offered could not be shown live: no release newer than 1.0.0 exists.
