# NekoServer v0.10.1 — complete-plan and integration audit

October 11, 2026. This release reviews the existing installer and its scheduled workers against the project plan. It does not certify an uninstalled server or claim that the entire original plan has been built. Read [PLAN-REVIEW.md](PLAN-REVIEW.md) for the requirement-by-requirement status.

## Fixes

1. **Shared filesystem permissions:** fresh library/appdata parents, imported media trees and archive/external-copy directories have explicit modes, independent of a restrictive umask. Imported media is owned by the configured UID/GID, with shared group access. Existing directories retain their modes/owners. Private native database directories remain private. Ownership behavior is verified through command fixtures because this workspace cannot map non-root UIDs; real ownership changes still need server acceptance.
2. **Read-only permission findings:** the acceptance report checks parent traversal and requested Unix access for configured media/Samba accounts. Failures are reported without recursive chmod/chown. This is mode-bit evidence, not an ACL/container/Samba authorization simulation.
3. **Native scheduled media checks:** the selected Rust runtime now runs the hourly media audit as well as the dashboard, collector, archive, database coordinator and external-copy jobs. Python remains interactive installation/onboarding, explicit recovery/acceptance and an explicit fallback.
4. **Stricter app connections:** native checks inspect exact host/container mounts, read-only playback paths, Prowlarr routing URLs and matching manager keys, enabled qBittorrent clients/categories, accessible Arr roots, Jellyfin identity/libraries, Seerr default routing and live profiles/root visibility. Existing custom settings are never changed by monitoring.
5. **Drive guards before private reads/writes:** the native media audit and Python media fallback verify configured UUID/filesystem identities before reading keys and publishing reports. Python scheduled backups now receive the same saved storage configuration and validate their argument paths. Jobs recheck storage before reporting success; native job status also rechecks SSD identity rather than merely accepting any mount.
6. **Failure reporting:** native media exit 3 means incomplete setup, exit 2 means failed checks and exit 1 means runtime/guard failure. Rust systemd definitions accept only exit 3 as non-failing incomplete work. This prevents guard refusal from being mistaken for a successful timer run. Failed/replaced-drive jobs do not create a new success timestamp.
7. **Samba handoff:** managed config changes restart the service so bindings actually take effect. A missing Samba account/password is set interactively using Samba's own prompt, without NekoServer storing it. Noninteractive setup returns needs_setup; existing passwords are preserved.
8. **Current instructions:** README is now one current workflow instead of accumulated historical deployment commands. Operational guide paths point to the current preserved release. A full plan matrix explicitly includes missing VM/cloud, Bazzite, broader-backup and background-notification work.

## Verification

- 547 Python-discovered tests pass, including 31 new permissions/integration regressions and localhost HTTP tests of the native media worker. The shipped production executable also runs the complete fixture API chain on the normal localhost ports, without development port overrides.
- 7 Rust unit tests pass for bounded chunked-response decoding and API record validation.
- 62 release checks pass: all 11 profile check/dry-run plans, Python/shell/JavaScript syntax and undefined-global symbol scanning, rendered media YAML, generated systemd syntax, CLI help, build/source integrity, static executable inspection and current Caddy configuration validation.
- 16 dashboard interaction checks pass in jsdom. These are DOM/behavior checks, not visual browser or TV acceptance.
- Rust formatting and Clippy on all targets with warnings denied pass. The locked musl production and isolated development fixture builds succeed.
- Earlier evidence for 15 live Caddy HTTPS fixture checks is retained. This audit validates the current generated Caddy configuration and confirms the unchanged gateway implementation; it does not claim those earlier transport tests were rerun against real applications.
- Fresh local fixture measurements: dashboard median RSS 14,528 KiB Python versus 1,564 KiB Rust (89.2% reduction for this process). A 64 MiB archive fixture had median peak RSS 14,300 KiB versus 1,760 KiB. Warm-cache transfer times are not HDD performance predictions. No whole-server or media-audit speed claim is inferred from them.

The initial plain root test invocation failed eight HTTP server tests because the production server correctly refuses root. Running the documented isolated HTTP fixture configuration resolves that environment issue; the production executable's root refusal remains independently covered. Two newly added fixture tests were corrected for this runner's unmapped non-root ownership and temporary systemd path mapping. No root-bypass fixture executable or port overrides are shipped.

## Remaining work and acceptance

The software is ready for controlled testing of the implemented features. It is not a completed implementation of the full plan. Missing code includes VM/KVM management, dedicated personal cloud, Bazzite migration, coordinated broader application/VM/Steam state backups and notification delivery with the dashboard closed. The optional YouTube uploader remains optional future work.

Automatic remote SMB/NFS mounting is not implemented. First-run Jellyfin/Navidrome/Seerr accounts, required credentials/indexer choices, Steam login, controller pairing, phone settings and certificate trust require user interaction. The add-only archive and nightly SQL copies are not a matched full Immich snapshot. Full snapshots/rehearsals and independent copying are explicit operations, not automated schedules. The weekly external job copies the archive only.

Actual Nyarch deployment, package installation, DynamicUser isolation, real user/group/ACL ownership, GPU/HDMI/audio, reboot and missing-drive behavior, SMART, real request/download/import/playback, phone uploads/certificate trust, Tailscale policy, Samba clients, external-drive disconnect/reconnect and independent full Immich restoration remain server acceptance tasks. This workspace has no Docker Engine and only root UID/GID mapping. No production containers, live storage mounts or real database restoration were exercised.

Keep original photos until independent full restoration succeeds. Persistent state on the application SSD improves OS recovery but is not itself an independent backup.

## Evidence

`audit-evidence/unittest-output.txt`, `rust-tests.txt`, `rust-checks.txt`, `checks.json`, `systemd-verify.txt`, `systemd-fixtures/`, `dashboard-dom-checks.json`, `runtime-benchmark.json`, inherited `gateway-proxy-checks.json` and `sha256-manifest.json`. `runtime/build.json` binds the production executable to its source/lockfile/target identity. Historical AUDIT-v*.md files retain earlier implementation history.
