# J01 Fresh installation with default settings

```
ID: J01
Purpose: A blank PC becomes a working server with the README steps and Enter at every question
Preconditions: NekoDebian VM at snapshot clean-debian13-desktop; blank SSD, HDD and USB drive;
  a phone with Tailscale and the Immich app at hand (or the phone step held for a rerun)
Actions: README steps; sudo bash install.sh; Enter at every question (the setup window open);
  confirm the phone step; let the first checkup run to the end
Expected: see below
Evidence: terminal log; finishing screen; systemctl list-timers 'nekoserver-*';
  /srv/nekoserver/reports/jobs/*.json; reports/acceptance/latest.json; a dashboard screenshot
Cleanup: take the post-install snapshot; later stages restore it
```

## Expected results

Finishing screen (`finishing_screen`, wizard.py:430; `headline`, wizard.py:337):

- `NekoServer is ready!` on the first ruled line, then `Done: 32 of 32 steps.` (the count
  is the installer's plan for the VM with a desktop; 31 of 32 while the phone step waits).
- The `NEXT_DONE` lines (wizard.py:385): `Everything is set up. Backups and monthly
  updates run on their own;` and `the dashboard shows how they are doing.`
- `Dashboard:          http://127.0.0.1:8787  (on this PC)` and `Dashboard PIN:      set`
  (`show_dashboard`, wizard.py:351; the port is `dashboard.port` in config.json).
- `Your apps:` with the home address and the Tailscale links (`show_addresses`, wizard.py:371).

First checkup (first_checkup.py:16 `JOBS`, run in that order): `App health check ...`
`done`, then `App settings and databases`, `System and user settings`, `Photos and the
Immich database, to the HDD`, `Photos and files, to the USB drive`, each `done` (the
music backup is off by default); then `Checkup: N checks passed.` and no `Needs a look`.

Units and times (template `backup_times` 04:15 / Sun 03:00 / 02:30; `job_times`,
neko_common.py:1398; `NIGHTLY_OFFSETS`, neko_common.py:1335; `DATABASE_BEFORE_USB` 45):

| Unit | Written by | OnCalendar |
| --- | --- | --- |
| nekoserver-app-backup.timer | modules/app_backup.py:78 | `*-*-* 04:15:00 <zone>`, Persistent=true |
| nekoserver-host-backup.timer | modules/host_backup.py:250 | `*-*-* 05:15:00 <zone>`, Persistent=true |
| nekoserver-music-backup.timer | modules/music_backup.py:23 | `*-*-* 03:45:00 <zone>` (only with the music backup on) |
| nekoserver-immich-db-backup.timer | modules/photo_preservation.py:58 | `Sun *-*-* 02:15:00 <zone>` (weekly schedule, the default: installer.py:256) |
| nekoserver-photo-backup.timer | modules/photo_backup.py:317 | `Sun *-*-* 03:00:00 <zone>`, Persistent=true |
| nekoserver-maintenance.timer | modules/maintenance.py:126 | `*-*-01 02:30:00 <zone>` |
| nekoserver-updates.timer | modules/maintenance.py:176 | `Wed *-*-02..31 02:30:00 <zone>` |
| nekoserver-update-check.timer, -system-check.timer | modules/maintenance.py:191 | `13:41` and `13:51` daily |

Reports (`execute_keeping`, runtime/src/backup.rs:2726, writes `reports/jobs/<name>.json`):
`applications.json`, `host.json`, `database.json`, `external.json` each `"status": "pass"`;
`reports/acceptance/latest.json` (acceptance.py:1278) with every automatic check `pass`;
`reports/integrity.json` present after the weekly copy (runtime/src/backup.rs:849).

Dashboard: each backup card reads `Passed` (`LABELS`, dashboard/app.js:208); no card
reads `Overdue`, `Incomplete` or `Not checked yet`; the checklist lists the restore tests
still to witness (tests/test_restore_tried.py).

## Manual parts

The phone step (Tailscale sign-in, Immich upload) is confirmed by a person; the gate
runs the phone checks as their own stage. Everything else is driven by the rig's rules.
0.48.1 gate (2026-11-09, VM, commit ee30d0e): passed, 31 of 32 with the phone step
held, rerun 32 of 32.
