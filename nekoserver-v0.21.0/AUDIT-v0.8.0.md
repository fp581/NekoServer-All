# NekoServer v0.8.0 — dashboard audit

October 8, 2026. **381 automated tests pass**, including 60 new dashboard/collector/job-reporting tests. **38 release checks** and **14 DOM interaction checks** pass. Earlier audit documents remain in the package.

## Connected implementation

The dashboard module follows storage and recovery, is independent of media/Docker deployment success, and precedes TV in the full profile. A dedicated eighth deployment profile installs its prerequisites. The TV launcher points to the configured dashboard port. Recovery bundles preserve HTML/CSS/JavaScript/SVG assets and new runtime imports. The root collector and unprivileged web service use OS-resident immutable assets so missing SSD/HDD mounts can still be reported. Their systemd definitions include privilege/filesystem/address-family restrictions; the web process cannot access the application SSD/configuration or Docker socket.

The collector uses bounded read-only commands, verified UUIDs/filesystems, fixed Docker Compose labels and allowlisted report fields. Wrong SSD identity prevents history and private report reads/writes. Actual disk capacity and supported SMART JSON values are collected only for mounted matching data drives. SMART is identity-cached with standby/no-wake requests; missing sensors/tools/output remain unknown. Public JSON drops raw command responses, service credentials, private exception messages and application metadata. CPU boot changes and unsupported physical disk mapping cannot produce false success.

Hourly archiving, nightly database dumps and weekly external copy now publish private attempt/success/failure records around their existing work. Last success survives failure. Interrupted jobs become overdue rather than remaining indefinitely running. Existing servers must reapply the relevant jobs to obtain new reporting. Add-only copy/checksum and full restoration behavior retain the prior audited limitations. A report is not a full restore acceptance certificate.

The HTTP server binds localhost, validates Host/Origin/fetch-site, serves exact fixed paths, rejects mutations, sets no-store and restrictive CSP/security headers, and refuses symlinked/malformed status files. Dynamic status text uses textContent; application URLs come from a fixed known-port map. Failed or stale refreshes retain clearly marked prior readings. Notifications require explicit browser permission/opt-in and stay local while the page is open. The offline preview embeds the actual assets and explicitly labelled sample data; it does not fetch status or open apps.

## Verification and practical limits

The automated suite covers prior media/storage/recovery behavior plus CPU and sensor fixtures, SMART failures/cache identity and malformed readings, UUID mismatches, stopped/ambiguous applications, private report redaction, interrupted jobs, symlink protection, real localhost HTTP requests/headers/origin rejection/mutation refusal, private configuration, installer reruns/custom-file protection and profile/recovery wiring. These are command and filesystem fixtures for system-level operations; they do not install production services or run real Docker/Postgres/SMART hardware.

Release checks compile Python, parse shell, validate all eight profile dry-run/check plans, parse eight rendered media Compose files, verify generated systemd fixture definitions with systemd-analyze, parse desktop launchers, inspect recovery/dashboard CLI help and JavaScript syntax, and run the DOM checks. The 14 DOM checks use jsdom against the actual offline/live page scripts: example labels, metrics, app lists/search, navigation, TV toggle, preview link/notification suppression, text injection resistance, fixed links, stale snapshots and failed refresh. DOM simulation does not validate browser layout, font rendering, native notification permissions or Steam/controller behavior. An actual Chromium rendering attempt was blocked by unavailable browser binaries/downloads in this workspace; no browser screenshot approval is claimed.

Real Nyarch/systemd sandbox execution, sensors/SMART on the chosen parts, drive disconnect/reboot behavior, end-to-end media playback, external drive reconnection and an independently copied full Immich restoration still require server acceptance. Bazzite migration, remote gateway and background/external alert delivery remain deferred. Keep original photos until a verified independent restore passes.

## Evidence

- audit-evidence/unittest-output.txt: full automated suite result.
- audit-evidence/checks.json: 38 release checks.
- audit-evidence/dashboard-dom-checks.json: 14 simulated DOM checks.
- audit-evidence/systemd-verify.txt and systemd-fixtures/: syntax verification inputs/results, using test paths; do not install these fixtures.
- audit-evidence/dashboard-demo-data.json: example readings only.
- audit-evidence/sha256-manifest.json: package-file checksums, excluding itself.

## Primary references

- systemd execution sandbox settings: https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html
- smartmontools source and JSON/no-check behavior: https://github.com/smartmontools/smartmontools

