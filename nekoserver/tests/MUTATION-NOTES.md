# Mutation-testing pass (first, targeted) — 2026-10-25

A mutation tool changes the program in one small way at a time (a `<` into `<=`, a
`||` into `&&`, a function body into `return true`) and runs the tests: a mutant the
tests still pass is "survived" and points at a check nothing exercises. This pass
covered the safety-critical runtime files and the Python areas listed below, decided
every survivor (test gap, or equivalent/irrelevant), and added tests for the gaps.
No product code was changed: no mutant turned out to be a bug.

## Tools and commands

Run on the openSUSE Tumbleweed test distro (Rust 1.99, Python 3.13.16), on a copy of the
tree under `~/mut` (never in place).

- **Rust: cargo-mutants 27.1.0** (`cargo install cargo-mutants`), default (debug) profile,
  a fixed 240 s limit per mutant, four mutants built and tested at a time:

  ```bash
  cargo mutants -f src/maintenance.rs -f src/backup.rs -f src/control.rs -f src/serve.rs \
      --timeout 240 -j 4 --no-shuffle --output ~/mut/rust-out
  ```

  The runtime's tests include two files from outside `runtime/`
  (`tests/fixtures/jellyfin_key_cases.json` in media.rs, `recovery_tools.py` in apps.rs);
  cargo-mutants copies only the package into `/tmp/cargo-mutants-runtime-*.tmp`, so those
  were copied to `/tmp/tests/fixtures` and `/tmp/recovery_tools.py` first.

- **Python: mutmut 2.5.1** (a venv; mutmut 3 drops the unittest runner), full suite as runner:

  ```bash
  mutmut run --paths-to-mutate <file> --runner "python3 -m unittest discover -s tests" \
      --tests-dir tests/ --no-progress --simple-output --CI
  ```

  mutmut mutates whole files, so a `mutmut_config.py` `pre_mutation` hook skipped every
  mutant outside the requested line ranges (and sharded `native_runtime.py` over five
  parallel copies by line number). The hook and scripts are not part of the tree.

## Results

### Rust (1459 mutants)

| file | mutants | caught | survived | timeout | unviable |
|---|---|---|---|---|---|
| src/maintenance.rs | 494 | 239 | 251 | 0 | 4 |
| src/backup.rs | 520 | 313 | 189 | 2 | 16 |
| src/control.rs | 246 | 170 | 58 | 0 | 18 |
| src/serve.rs | 199 | 138 | 61 | 0 | 0 |

"Unviable" mutants do not compile (e.g. a `Default` that does not exist); a timeout counts
as caught (an infinite loop the tests would notice).

### Python (868 mutants in the requested areas, all killed)

| file (lines) | mutants tested | killed | survived |
|---|---|---|---|
| native_runtime.py (whole file) | 517 | 517 | 0 |
| modules/recovery.py (whole file) | 121 | 121 | 0 |
| modules/dashboard.py 277–336 (replaceable, check_payloads, write_files) | 65 | 65 | 0 |
| wizard.py 90–126 (release_version, earlier_candidates, earlier_settings) | 26 | 26 | 0 |
| host.py 187–190, 562–578, 955–967, 1176–1211 (names, package maps, Suse incl. available) | 107 | 107 | 0 |
| neko_common.py 147–181 (maintenance_running, pending_maintenance) | 32 | 32 | 0 |

Four kills were checked by hand (`mutmut apply`, then the suite): each failed exactly the
test that owns the behaviour (e.g. the `TOOLCHAIN_PACKAGES` name is pinned by
`test_build.ToolchainTests`, the zypper lock wait by `test_host.SuseTest`). No Python
test was added: nothing survived.

## Decisions on the Rust survivors

### Tests added (25), and what they catch

backup.rs (11):
- `a_full_check_is_recorded_and_due_again_after_four_weeks` — `FULL_CHECK_EVERY` is
  28 days not one (49), `full_check_due` / `record_full_check` read and write the per-job
  record (52, 69). (Removed in 0.39.3 with the code it tested: the full checksum check
  is now the monthly run's first step, not a 4-weekly part of the weekly jobs.)
- `a_backup_never_copies_into_itself_or_a_parent` — `separate`: destination inside the
  source, source inside the destination, a file as destination, a missing source (86).
- `a_dry_run_says_which_copies_it_would_make` — `verified_copy`'s dry-run answer and the
  collision refusals of `copy_verified_with` (104–106, 135).
- `private_copies_get_a_private_folder` — a 0600 copy gets a 0700 folder (146).
- `a_partial_file_that_is_not_a_file_is_refused` — a `.neko-part` that is a folder (156).
- `dry_runs_write_no_names_and_versions_accumulate` — `remember_name` / `remember_version`
  write nothing on a dry run and keep earlier versions (307, 320, 323, 326).
- `changes_within_one_second_get_numbered_versions` — the `-1`, `-2` suffixes of
  `kept_copy` (456; the `*=` mutant loops for ever, the `-=` one names `-0`).
- `the_problem_text_lists_ten_files_and_counts_the_rest` — `Integrity::problem` (541).
- `backup_bookkeeping_is_not_part_of_the_tree` — `tree_files` skips the quarantine
  folder, the ledger's files, latest markers and partial files (655–659).
- `an_audit_counts_every_file_without_a_checksum` — `audit`'s `unverified` count (689).
- `a_name_kept_only_on_the_usb_drive_still_counts_on_exfat` — **real gap**: the existing
  `a_name_kept_from_an_earlier_week_still_counts_on_exfat` also re-creates the folder in
  the cloud tree in the same week, so the same-week scan refused the file and
  `remember_kept` / `kept_names` (1028, 1044) were never what decided. The new test
  leaves the old spelling only on the USB drive.

control.rs (4):
- `a_second_sign_in_keeps_the_first_device_signed_in` — `Gate::unlock` keeps the other
  devices' tokens (160).
- `a_curve_needs_a_temperature_the_board_has` — `Fans::change` refuses an unknown sensor
  (501, 506).
- `an_unreadable_journal_is_reported_not_taken_for_absent` — `read_journal` keeps and
  `report_unreadable` names a journal that cannot be read (933, 950).
- `saved_fan_settings_are_loaded_at_start` — `load_settings` (866).

maintenance.rs (6):
- `only_known_apps_are_selected_and_each_once` — `names` (75–92).
- `an_image_id_is_a_full_hex_digest` — `image_id` (266).
- `the_update_refuses_models_that_leave_their_sandbox` — **the largest gap**: `valid_model`
  (54 survivors, 277–331) and `expected_service` (252–261), the checks that keep the
  monthly update from running a Compose model with `privileged`, `network_mode`, `pid`,
  `ipc`, `build`, a foreign service name, an extra service, a bind mount outside the app's
  own folders, a named volume on the app, or a port off loopback. The one existing test
  checked a single bind mount.
- `every_app_is_probed_on_its_own_port` — `app_port` (783–796).
- `the_secrets_key_is_ready_only_with_its_certificate` — `secrets_key_ready` (627).
- `three_monthly_snapshots_are_kept_unless_the_settings_say_otherwise` — `keep_snapshots` (1766).

serve.rs (4):
- `the_snapshot_is_checked_to_be_the_collectors` — `Server::snapshot`: E10 for no
  snapshot, E11 for one that is not the collector's (216, 220).
- `containers_come_from_the_snapshot_with_their_services` — `containers` (248).
- `nvidia_figures_expire_at_two_intervals_plus_thirty_seconds` — `merge_nvidia`'s
  freshness window and bus matching (272, 277).
- `tailscale_names_and_error_sentences_have_limits` — `valid_public` 253-character limit
  (133), `error_message` (63).

A second cargo-mutants pass restricted to those functions (`-F` with their names) confirms
the kills; see "Verification" below.

### Equivalent or not worth a test (noted, no change)

- Boundary swaps that no input distinguishes: `at > 0.0` → `>=` in `due_since` (64; a
  record at exactly 0 is due either way), `Instant::now() < until` → `<=` (control 127,
  170, 333), `age_days < -1.0` → `<=` and `> DUMP_STALE_DAYS` → `>=` (backup 1290, 1294),
  `bus.len() < 12` → `<=` (serve 277), `ext.len() + VERSION_TAG + 24 > NAME_MAX`
  arithmetic in `version_parts` (285–289; the long-name tests cover the behaviour, not
  the exact threshold).
- Redundant defence in depth: `s == d` in `separate` (86:25; `starts_with` already
  catches equal paths), the second "source changed" check and the three-way checksum
  `||` in `copy_verified_with` (188, 197; a replaced source is caught by the following
  check, and a disk that returns other bytes than were written cannot be staged),
  `O_NOFOLLOW | O_CLOEXEC` flag arithmetic (170).
- Constants and time: `TOKEN_LIFETIME = 12 * 3600` (control 44) and the lockout
  doubling in `detect_wait` (759) would need hours of waiting; `night()` reads the clock
  (maintenance 191); `random_hex` reads `/dev/urandom` (control 67).
- Output only: `Integrity::print` (backup 578–587), the HTTP reason phrases in `reply`
  (serve 99–106; clients read the status code), the "one major at a time" message
  branch of `nextcloud_major_step` (1691; both branches return `None`),
  `state_owner` (test-only shim).
- Counters in reports that the existing tests check as "at least one": `+=` in
  `record_kept`, `archive_checked`, `mirror`, `copy_cloud`, `start_detect`, `health`.

### Needs the real system (the main follow-up, not done in this pass)

These survivors sit in code that drives Docker, systemd, mounts, sockets or the network,
and the unit tests only reach it through the rehearsal fixtures:

- maintenance.rs: `docker`/`compose`/`curl_as`/`release_fetch` wrappers; `containers`,
  `stop`, `no_other_writers`, `health`, `restored_model`, `database_probe`,
  `rehearsal`/`rehearse`, `immich_candidate`, `nextcloud_major_notice`, `integrations`,
  `mounts`, `model`, `image_bytes`/`free`/`size`, `committed` (fixed journal path) and
  above all **`resume` (27 survivors, 1145–1333)**: the rollback/finish logic of a cut-off
  update is exercised end to end by the fault suite on the rig, not by unit tests. A
  follow-up should give `resume` an injectable journal/docker seam and test its phase
  decisions directly.
- backup.rs: `disks_of`/`own_disk`/`require_private_mount`/`validate_external` (lsblk and
  mounts), `guard_data`, `execute*`, `finish_journal`, `publish_snapshot`, `coordinate`,
  `database`, `preservation`, `weekly` (the weekly USB job's orchestration, 1940–2093),
  `kept_copy`'s version-compare loop details (435–442), `mark_latest` (492),
  `save_integrity` (600), `case_clash` prefix (1006).
- control.rs: `run`, `serve_socket`, `retry_lights`, `set_pin` (stdin), `on_stop`,
  `restore_journals` (913–914: a main journal that cannot be rewritten while outputs are
  still not back), detection cancel/progress (671, 699–712, 837–856), lighting
  (`apply_light`, `shows` 193–225, `Lighting::all_showing` 393).
- serve.rs: `handle` (HTTP limits and timing, 310–430), `control` op names (485–495),
  `run`, `hardware`.

### Timeouts (2, counted as caught)

`copy_verified_with` 177 (`==` → `!=` on the read size: the copy loop never ends) and
`fitted` 265 (`-=` → `/=`).

## Verification

Three cargo-mutants re-runs restricted (`-F`) to the functions the new tests target, each on
a fresh copy with the tests in place:

| pass | mutants | caught | timeout | unviable | still survived |
|---|---|---|---|---|---|
| 1: backup/control/maintenance functions of the first batch | 258 | 212 | 2 | 6 | 38 |
| 2: serve functions, `app_port`, `secrets_key_ready`, `keep_snapshots` | 105 | 80 | 0 | 0 | 25 |
| 3: the cases added after passes 1 and 2 (`kept_copy`, `valid_model`, `expected_service`, `Server::snapshot`) | 101 | 91 | 1 | 1 | 8 |

What still survived in passes 1 and 2, and why it stays:
- Noted above as equivalent: `due_since` 64, `separate` 86:25, `copy_verified_with` 170/188/197,
  `Gate::unlock` 160 `<=` and `Gate::signed_in` 170 `<=`, `merge_nvidia` 277 `<=`, the
  NAMES format check `||` in `remember_name`/`remember_version` (307, 320: format and
  names-is-an-object co-vary), `valid_model` 295 (`/etc/localtime` read-write is refused
  either way, by `safe()` or by the bind rule), `kept_copy` 408 (pre-0.28.5 long-name
  stems), 435 (`Check` compared only when a trusted-by-date version differs in content),
  441–442 (which path a report names for a trusted version).
- Needs the real system: `containers` in maintenance.rs (21; the regex `containers` also
  matched it; it drives `docker ps`/`docker inspect`), `run` 529 in serve.rs.
- Still there after pass 3, all noted above: `kept_copy` 408, 435, 441–442; `valid_model` 295
  and 312 (a read-write `/etc/localtime` mount: on the distro `/etc/localtime` is a symlink,
  so `safe()` refuses it before the bind rule can; on a system where it is a plain file the
  bind rule alone decides, and only that line's `&&` distinguishes the two).
- Fixed by the cases added for pass 3: `kept_copy` 359 (a dry run of a new file answers
  "new"), `expected_service` 260 and `valid_model` 302–313 (Nextcloud, Immich and the
  other apps' own bind mounts, accepted for them and refused for the others),
  `Server::snapshot` 216 (a snapshot that is there but unreadable is E11, not E10).

Full check on a fresh copy of the working tree (version 0.39.2):

| check | result |
|---|---|
| `ruff check --select E9,F63,F7,F82 .` (the CI's selection) | all checks passed |
| `python3 -m unittest discover -s tests` | 616 tests OK (3 skipped) |
| `cargo fmt --check` | clean |
| `cargo clippy --all-targets -- -D warnings` | clean |
| `cargo test --release` | 244 passed, 0 failed |

## 1.0 round (2026-11-23 and 24): the restore and recovery paths

mutmut 2.5 (with the line hook above) on the Python files the 1.0 round's recovery drills run
for real, on main 35ea7bc and, for the Nextcloud bring-back, on `release-1.0.0` 1f7806d. Tests
only: no mutant turned out to be a bug, and no product code was changed. Each row is a
findings row of the round (`T6-rc1` to `T6-rc7`). The kill counts were checked against
mutmut's own mutants; the lists of survivors are in the rig's logs.

| Row | Mutated | Survivors before | Tests added | Left, and why |
| --- | --- | --- | --- | --- |
| T6-rc1 | `permission_checks.py`, `config_checks.py` (`unknown_settings`, the job time), `neko_restore.py` (`CardRoot`): the lines split out in 0.52.2 | 34, 10 and 1 | `tests/test_permission_checks.py` (12), `tests/test_mistyped_settings.py` (the whole warning lines, `JobTimeTests`), `tests/test_neko_restore.py` (the whole sentence) | equivalent: permission_checks 11 and 12 (`safe_path` refuses the link before), config_checks 269 (only the closest match is used) |
| T6-rc2 | `gateway_tools.py`: `_apply_lan`, `start_lan_gateway`, `owned_listeners`, `set_tailnet_routes` | 58 | `tests/test_gateway_lan_apply.py` (10) | none: every hand-made mutant of those functions is killed |
| T6-rc3 | `snapshot_restore.py`, the whole file | 124 | `tests/test_snapshot_refusals.py` (33): 95 killed | equivalent: Kind defaults (both kinds set them), `.` checks (`PurePosixPath` already drops them), the 128 MB edge, inventory values, `followlinks` (a link is refused by `safe_path` either way), open flags (the folder is new), buffer sizes, owner and time order (as root and after every file exists the order changes nothing); `--help` wording |
| T6-rc4 | `backup_secrets.py` and `recover.py`, the whole files | 100 and 84 | `tests/test_backup_secrets_words.py` (32), `tests/test_recover_command.py` (11) | equivalent: stdin and timeouts given to openssl and tar, the password's line end (both ends the same), `mkdir` parents under `/etc`, the staging folder's name, `rmtree` `ignore_errors`, `-recip`; recover.py's `--help` wording (15) |
| T6-rc5 | `recovery_tools.py` outside the 0.52.2 ranges | 21 (and 6 timeouts: loops that hang, caught) | `tests/test_recovery_command_failures.py` (12): all 21 killed | none |
| T6-rc6 | `recovery_wizard.py`, the restore path (lines 255-2434), against all 26 test modules that import it | 563 | `tests/test_wizard_checked_copies.py` (44), `tests/test_restore_dates_and_accepted.py` (9): 113 killed | 320 that only wrap a sentence in `XX` (the tests check wording with `assertIn`, which the wrapped text still passes); 12 logic survivors that are equivalent; and, cut from scope (below), the logic survivors where a wrong result would not lose data, restore the wrong thing or report success on a failure |
| T6-rc7 | `nextcloud_restore.py` (setup's Nextcloud bring-back after a new SSD), the whole file | 194 (and 7 timeouts, caught) | `tests/test_nextcloud_restore_choices.py` (25): 67 of the 98 logic survivors killed | 96 that only wrap a sentence; equivalent: buffer sizes, ties at equal times, `""` against `None` for no dump, `~mode` masked to 0755 by the kernel; minor, left untested: `own_ids` following a symlinked `appdata_` folder, the `exist_ok`/`parents` flags of `mkdir`, `rmtree` `ignore_errors` |

Cut from the scope of this round, and why: in `recovery_wizard.py` the USB and NAS mount
wrappers and the `lsblk`/`findmnt`/`mount`/`umount` calls (their timeouts and capture flags),
the ownership hand-over of a restored folder, and the words of the `restore_*` steps. None of
them decides what is restored or whether a restore is reported as passed; the mount wrappers
need a real drive to say anything and are covered by the recovery drills on the VM instead.
Seen on the way: as a normal user the `chown` of the restore folder's parent fails first, so
`restore_music`'s hand-over success branch is never run by CI.

## 1.0.1 round: storage_jobs.py and mirror_add.py expected survivors (2026-11-24)

storage_jobs.py (04024a1; the refusals, the erase and its re-check, the move's compare, switch and
commit point, the journal's resume and finish, old copies, use and release): 568 mutants, the ones left
after test_storage_erase_steps and test_storage_journal_steps are wording (error texts, "delete it."),
defaults that never change a result (`or 0` against `or 1` on a size or minimum, a staging path
defaulting to "" or "XXXX", the measure of old_copy, `tell` returning 0), the phase names of commits that
no code reads back ('mount', 'release'), `settings_files[+1]` with two files, `parents=False` one
level deep, and `job.reapply(... and saved)` (add_settings writes the settings itself).
mirror_add.py (c2c491d): 131 mutants, 43 left: wording and `[:301]` advice slices, the journal's
unused undo keys ('disk', 'array'), the btrfs dict's unused 'dev', `find`'s `found[2]`, `offers`' code
of a refusal nobody reads, the sleep and the status time limit by one, and three that are a finding,
not a missing test: 363/364/366, "Never started" counted as finished (plans/1.0.1-findings.md T6-101-1).
After T2's fix (e739628, changed lines only): 23 mutants of the new helpers left, all wording, the
labels of `wait_replace`'s answers that no caller compares, `>= 2` against `> 2` or `>= 3` on a piece of a
status line that always has four words, `percent_done` walking past the start of a line that never ends in
a digit, and `NEVER_LOOKS` by one. The "never counted as finished" mutants are killed now.

## 1.0.1 round: the Drives page's Rust lines (cargo-mutants --in-diff v1.0.0..c17ab91, 2026-11-24)
216 mutants on the lines 1.0.1 added to control.rs, serve.rs, storage.rs, drives.rs, media_folders.rs
and collect.rs: 190 caught, 3 unviable, 23 missed. 15 of them were missing tests, now added (drives.rs:
the by-id rank before the alphabet, each drive's words, `rotational` however lsblk says it, the mounted
filesystem shown; storage.rs: release and back pass their fields, a long kernel name or a bare "ata-" is
no stable name, an old copy's id is lower-case hex only; media_folders.rs: a backslash or a control
character in a name is left out on its own). The 8 left are expected: `<` against `<=` on a boundary
(the PIN token's lifetime, the shared tries' `left < 1.0` moved from unlock unchanged, a request's 60 s
`ON_ITS_WAY`), `(rank, name) <` against `<=` in drives::by_id (two entries of one folder never have one
name), control::run's service loop, and drives::disks (it runs the real lsblk; from_lsblk is tested).
