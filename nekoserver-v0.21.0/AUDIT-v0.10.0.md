# NekoServer v0.10.0 — native runtime audit

October 11, 2026. **516 automated tests, 60 release checks and 16 dashboard DOM checks pass.** Rust formatting and Clippy with warnings denied pass. The earlier live Caddy HTTPS proxy fixture evidence remains included. Read RUST-RUNTIME.md for backend selection and measurement limits.

## Implementation

The new Rust executable implements dashboard serving/collection and scheduled photo preservation, database dumping and external copying. Python retains installation, API setup/audits and recovery/acceptance. Existing Python workers are an explicit fallback; old configs keep that backend. The browser UI/cat assets are unchanged.

New configurations select the verified static x86_64 Linux executable. The Python installer validates its target/version and binary/source manifest, installs the OS dashboard executable with permission for DynamicUser, retains private scheduled-job code on the SSD, writes private storage identities for native backup jobs and generates the same systemd timer names. The preserved recovery bundle includes native code/binary/build metadata/lockfile. Acceptance inspects the selected native executable hash, loaded systemd command and backup guard where applicable. Backend changes invalidate acceptance scope.

The HTTP transport uses bounded headers, fixed workers and a bounded connection queue. Root refusal, loopback binding, exact routes, JSON/size checks, Host/Origin protection, security headers and blocked mutations are retained. The collector retains bounded read-only external probes, sleeping-disk SMART checks, sanitized storage/container/backup/recovery/acceptance data and private-state writes guarded by SSD identity. Scheduled backups retain checksum/source-stability checks, atomic no-replace publication, locks, retries and fixed private outcome reports; native scheduled jobs additionally enforce both filesystem identities before work.

## Execution evidence

43 added tests execute the production native binary or the separately identified HTTP development fixture. They compare all 55 supported photo/video extensions against the Python archive, preserve deletions, reject changed originals/symlinks/overlap/invalid age, recover partial files, retain published file timestamps on rerun, compare external copy behavior, reject wrong external UUIDs, detect mid-copy source mutation and preserve concurrently created destinations. Database fixtures verify private successful dumps, failure cleanup, matching SSD/HDD copies and sanitized status. Full preservation-job fixtures verify UUID gating, success/failure reports, retained last-success evidence and lock exclusion.

Collector checks cover public/private permissions, expected Python output schema, report redaction, wrong-drive private-read/state exclusion, CPU/history/cache continuity, malformed/symlinked reports and acceptance/witness sanitization. Round-trip JSON float parsing preserves Python timestamps. Native deployment tests cover the legacy/default/invalid backend, source/binary integrity, immutable custom files, systemd hardening, native/Python definitions, recovery payloads and loaded-command rejection.

HTTP checks cover actual localhost GET/HEAD assets/status, fixed security headers, Host/Origin/cross-site restrictions, methods, paths, malformed/oversize/symlinked files, duplicate Host/framing rejection, header bounds and the fixed worker count. This workspace only maps root UID/GID; HTTP fixtures bypass root refusal at compile time in an isolated development binary. The production binary refuses root and excludes the fixture route/marker. No development binary is included in the ZIP. Real unprivileged execution and systemd sandbox enforcement are not proved by these fixtures.

Release checks include all eleven profile plans, Python/shell/JavaScript syntax, rendered media Compose templates, generated native dashboard and backup systemd fixtures, CLI help, DOM checks, production Caddy validation, previous 15 live HTTPS proxy checks, native build integrity, production identity and static ELF inspection. systemd-analyze validates definitions without running them. Cargo compilation, formatting and Clippy are separate recorded checks; there are no standalone Rust unit tests claimed.

Three process runs measured dashboard RSS of 14,544 KiB Python versus 1,564 KiB Rust (median; about 89% reduction for the dashboard process). A 64 MiB production archive fixture measured 14,296 KiB versus 1,484 KiB peak RSS. Those are local fixture results; they do not quantify whole-server memory, permanent CPU savings or real HDD performance. The Rust HTTP fixture differs only in development root/metrics hooks; measurements are explicitly labelled accordingly.

## Remaining acceptance

Actual Nyarch deployment and package ownership, DynamicUser access/isolation, reboot behavior, SMART sensors, native phone/TV certificate trust, real media acquisition/playback/uploads, private Tailscale policy and gateway reachability, Samba permissions, live external mount/unmount/disconnect behavior and independently restored photo/database data remain unverified here. The no-hardlink/no-replace rename fallback requires a real external filesystem exercise. Bazzite automatic migration remains unsupported. Do not delete original photos before independent restoration succeeds.

## Evidence

- audit-evidence/unittest-output.txt — 516 passing tests, including 43 native tests.
- audit-evidence/checks.json — 60 release checks.
- audit-evidence/runtime-benchmark.json — method and all process samples.
- audit-evidence/rust-checks.txt — compilation/format/Clippy outcomes.
- runtime/build.json — production target/compiler and byte/source checksums.
- audit-evidence/systemd-verify.txt and systemd-fixtures/ — native syntax fixtures, not installation payloads.
- audit-evidence/dashboard-dom-checks.json — 16 interactions simulated with jsdom.
- audit-evidence/gateway-proxy-checks.json — previously tested live Caddy transport fixture.
- audit-evidence/sha256-manifest.json — complete release checksums, excluding itself.

Prior audits remain historical records; current backend/default statements supersede them.
