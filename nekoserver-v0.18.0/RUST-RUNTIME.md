# v0.18.0 schedule policy

Routine NekoServer media checkups are disabled, music audio has no automatic backup, and managed photo originals/database/external backups run weekly on Sunday night. Monthly update/recovery checks and app-state backups remain. [STORAGE.md](STORAGE.md) defines the current policy and supersedes older schedule/coverage descriptions below.

# NekoServer v0.15.1 — Python installation, Rust workers

The installer and configuration modules remain Python. The dashboard browser UI remains HTML/CSS/JavaScript, with the same cat and appearance. The new native runtime handles:

| Native command | Installed task |
| --- | --- |
| serve | Continuously running localhost dashboard HTTP service |
| media-health | Scheduled GET-only local media API and container audit |
| collect | Scheduled dashboard metrics, disks/SMART, application state and sanitized reports |
| preservation | Scheduled original-photo archive and nightly database job |
| weekly | Scheduled mounted external add-only photo copy |
| hdd-policy | Guarded media SATA standby setup at installation/boot |
| notify | Background ntfy notifications and durable deduplication/retry tracking |
| app-snapshot | Nightly private media app state snapshots on the separate HDD |
| app-resume | Restart original containers after failure/interruption or reboot |
| archive / copy / database | Direct archive, copy and logical dump helpers |

Interactive application API onboarding, recovery/acceptance tools and installer orchestration stay Python. The scheduled media API audit now runs natively with the Rust backend; it is a bounded timer job, not a permanent daemon. Jellyfin, Immich, Navidrome, Caddy, Tailscale and Docker retain their own implementations. PostgreSQL still performs pg_dump inside its database container; the Rust worker coordinates and verifies its output.

## Enable or keep the Python backend

The supplied new config defaults to runtime.backend=rust. Older configured files without runtime retain Python. On an existing server, add this setting to your actual configuration while retaining its confirmed drive UUIDs, users, application versions and credentials:

```json
"runtime": { "backend": "rust" }
```

Then inspect/apply the affected profiles:

```bash
sudo bash install.sh --profile dashboard --dry-run
sudo bash install.sh --profile dashboard --apply
sudo bash install.sh --profile photos --dry-run
sudo bash install.sh --profile photos --apply
sudo bash install.sh --profile media_monitor --apply
sudo bash install.sh --module app_backup --apply
sudo bash install.sh --module notifications --apply
# If upgrading an existing external-copy schedule separately:
sudo bash install.sh --module photo_backup --apply
```

The full profile also installs the selected backend. A dashboard-only application does not update your photo jobs. Existing running/installed timers should be updated while their jobs are inactive; check normal systemd status before changing the backend. The jobs retain the same archive/external lock names across Python and Rust, so simultaneous runs refuse the shared lock. Backend selection is explicit, with no silent fallback if a native build is damaged. Application snapshots require Rust and have no scheduled Python fallback. To return other modules to Python, disable the app snapshot timer explicitly and set backend=python and reapply those same modules. The frontend, app data and photo archive paths are unchanged.

The release includes a stripped static x86_64 Linux executable. No Rust compiler, Cargo or dynamically loaded Rust runtime is installed on the server. Its binary and source checksums, version and target are checked before deployment. Unsupported platforms must use the Python backend. Existing differing versioned files are preserved for review. The native service still requires actual Linux/systemd and its external tools such as findmnt, Docker and optional smartctl.

## Safeguards and operation

The web service binds only 127.0.0.1, refuses root and keeps the existing DynamicUser/systemd restrictions. It serves fixed assets and a bounded, format-checked snapshot through GET/HEAD. Host/Origin/cross-site checks, security headers and forbidden management methods remain enforced. Its fixed four-worker pool, bounded queue, maximum header size and read/write deadlines bound connection resource usage. Development diagnostics are absent from the shipped executable.

The privileged collector only publishes the existing restricted dashboard schema. Matching SSD identity is required before reading private reports/state; the SSD identity is rechecked before writing internal state. Device probing retains the sleeping-disk SMART option, bounded command output and timeouts. Reports continue to omit credentials, raw application responses, SQL, personal files and arbitrary errors. Acceptance scope digests include the backend so a backend change requires new evidence.

Photo workers remain add-only. They verify content checksums and source stability, create partial files exclusively, flush data, and publish atomically without replacing a concurrent destination. Symlink paths, overlapping source/destination, changed originals and collisions fail. Interrupted recognized partial copies can be retried; published copies are preserved after source deletion. Linux no-replace rename is retained for filesystems without hard links. Special source files are rejected.

Scheduled native jobs additionally compare both drive UUIDs/filesystems against private /etc/nekoserver-backups/storage.json before work. Jobs require root, take exclusive locks and record private fixed-schema running/pass/fail outcomes. Failed jobs retain the last successful timestamp without claiming a new success. Database dumps are private and copied with checksum verification to both SSD and HDD. Weekly copies check the configured external drive and only unmount a drive mounted by that run. No drive is formatted, no deletions are synchronized and no ordinary rename substitutes for no-replace publication.

Reapply the affected modules after an OS replacement to restore OS-side services and the private backup guard. The preserved SSD release includes the executable, source, Cargo.lock and build metadata. Add a fresh acceptance baseline after deployment and verify real reboot, client permissions, photo deletion behavior and independently restored files/databases. Rust does not replace those checks.

## Measurements and test limitations

Three local fixture runs gave median dashboard resident memory of 14,528 KiB (Python) versus 1,564 KiB (Rust), about 89% less for this service. A disposable 64 MiB archive used median peak memory of 14,300 KiB versus 1,760 KiB. These are workspace process measurements, not whole-server savings. Copy timings used a warm cache and do not predict real HDD throughput. Evidence and samples are in audit-evidence/runtime-benchmark.json.

The workspace only maps UID/GID zero and cannot start an actual unprivileged service. HTTP tests and dashboard measurements therefore use a separately compiled test-fixtures executable that bypasses only the development server's root check and exposes a fixture metrics route. Production root refusal and absence of that route/marker are independently tested. The fixture executable is not shipped. The native collector, file copy and database job tests run the actual production binary with disposable files; system commands are mocked where hardware/Docker would be required. Real DynamicUser permissions, systemd isolation, SMART devices, external filesystem disconnects and full photo/database restoration remain hardware acceptance tasks.

## Rebuild and development

Build from an ordinary-user extraction with an installed Rust toolchain and the x86_64-unknown-linux-musl target:

```bash
rustup target add x86_64-unknown-linux-musl
python3 runtime/build.py
```

The build uses Cargo.lock and refreshes the binary/source manifest. It refuses to compile as root. Source/build artifacts under runtime/target are not installation payloads. Preserve the production build and do not install a development test-fixtures executable.

For development, build test-fixtures into a separate target directory and set NEKO_TEST_SERVER_BINARY to that executable before running the root-only job fixtures in tests/test_native_v0100.py. tests/runtime_benchmark.py uses the same variable and a temporary GCC-built wait4 helper to measure archive peak memory; no benchmark compiler/helper is needed on the deployed server. The complete tests use disposable files and mocked hardware commands; they are not a substitute for acceptance.py on the real server.

## v0.15.1 scheduled media audit

The native `media-health` command uses fixed localhost services and GET-only API requests with bounded headers/bodies and deadlines. It never follows redirects or exports raw API payloads. Exact container mounts, read-only playback paths, Prowlarr routing keys, Arr roots/download categories and Seerr live request-provider profiles are checked. Configuration or playback tasks remain visible as incomplete. HTTP fixture port overrides are compiled only into the separate development build; they do not exist in the shipped executable.

Reapply `media_monitor` as well as dashboard/photos/photo_backup when changing backend. A configured Rust timer starts no Python process. Native media exit 3 means remaining setup, exit 2 means reported failed checks and exit 1 means runtime/guard failure. Only exit 3 is a successful incomplete timer run; runtime refusal must remain a failed systemd unit. Python fallback uses exit 1 for incomplete configuration and verifies drive identities before reading keys and publishing reports.

Background notifications require Rust, curl/system CA certificates and a configured ntfy destination. There is no scheduled Python notification fallback. Read [NOTIFICATIONS.md](NOTIFICATIONS.md).

Host/user snapshots and Samba recovery use `host-snapshot` and `host-resume`; the Python host-recovery tool runs only on demand. See HOST-BACKUPS.md.

Default Docker and LAN gateway startup guards now use native `storage-guard` and `gateway-guard`. Python startup guard scripts are retained for explicit fallback/ad hoc inspection.

## v0.17.0 storage workers

`music-backup --config /etc/nekoserver-backups/storage.json` runs the native versioned SSD-to-HDD music backup. The nightly timer is installed by `music_backup` (included by the music/full profiles and the media profile). `music-recovery.py` is an ad hoc inventory verification/restore tool. HDD `hdd-policy` now accepts a guarded `server_os` policy object with `idle` plus `hdd_idle_minutes`, alongside `keep_spinning`; see STORAGE.md. Native photo preservation and monthly recovery now read Immich originals from the SSD tree.
