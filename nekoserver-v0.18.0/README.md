# v0.18.0 schedule policy

Routine NekoServer media checkups are disabled, music audio has no automatic backup, and managed photo originals/database/external backups run weekly on Sunday night. Monthly update/recovery checks and app-state backups remain. [STORAGE.md](STORAGE.md) defines the current policy and supersedes older schedule/coverage descriptions below.

# v0.17.0 storage update

The current layout is defined in [STORAGE.md](STORAGE.md): music and Immich live data on SSD, independent HDD backups, movies/shows only on HDD, and a verified 60-minute HDD idle timer. This supersedes older layout statements below. Existing deployments with old host paths are preserved and require a verified migration.

# NekoServer v0.15.1 🐱

This audit release fixes fresh-install, configuration handoff, permission and rerun issues. Read [INSTALLATION-REVIEW.md](INSTALLATION-REVIEW.md) for the step-by-step review, evidence and deployment limits.

A modular Nyarch/Arch TV PC and home-server installer. Application state lives on a separate SSD; media and photo archives live on the HDD. The OS can be replaced through the guarded recovery workflow. No drive is formatted automatically.

This release adds Rust independent-outage monitoring. See [HEARTBEAT.md](HEARTBEAT.md) for outside monitor setup and alert acceptance. See [HOST-BACKUPS.md](HOST-BACKUPS.md) for scope, extra save/share folders and recovery. Read **[PLAN-REVIEW.md](PLAN-REVIEW.md)** for every implemented, manual, unverified and missing requirement. The full original plan is not finished.

## Start with a preview

```bash
python3 installer.py --profile full --check
sudo bash install.sh --profile full --dry-run
# On the supported server, after inspecting the plan:
sudo bash install.sh --profile full --apply
```

Use the existing confirmed configuration when upgrading. Do not replace your UUIDs, account IDs, source choices or application settings with the sample config. The installer asks for unassigned drives and confirms their exact UUIDs. Independent modules continue after unrelated failures; failed prerequisites block their dependants. Incomplete onboarding is recorded separately and can be retried.

```bash
sudo bash install.sh --retry-failed --apply
```

Individual profiles include server_os, foundation, media, photos, media_monitor, app_backup, notifications, tv, dashboard, lan_access and remote_access. The full profile includes LAN access; Tailscale remains optional. Existing custom manifests and incompatible runtime files are preserved for review.

## Runtime split

| Python | Rust with `runtime.backend=rust` |
| --- | --- |
| Installer and package/configuration setup | Local dashboard HTTP service |
| Interactive API/account/library/source onboarding | Scheduled dashboard/SMART collector |
| Explicit recovery and acceptance commands | Scheduled GET-only media API/container audit |
| Explicitly selected fallback workers | Photo archival, database-backup coordination, external-copy jobs, application snapshots and background notifications |

A verified static x86_64 Linux executable is supplied. The server does not need a Rust toolchain. Legacy configurations keep their explicitly compatible Python behavior until `runtime.backend=rust` is selected and affected modules are reapplied. There is no silent fallback for a damaged native build. See [RUST-RUNTIME.md](RUST-RUNTIME.md) for upgrading all relevant schedules.

## Validate the deployed machine

```bash
cd /srv/nekoserver/installer/releases/v0.15.1
sudo python3 acceptance.py --profile full --save
# Reboot normally, then repeat the same command.
# After performing the real tests listed in the guide:
sudo python3 acceptance.py --profile full --save --manual
```

The dashboard is available locally at http://127.0.0.1:8787/ with default settings. It displays current status rather than running privileged work on refresh. `dashboard/preview.html` contains labelled example readings; it is not server evidence.

## Guides

- [SERVER-OS.md](SERVER-OS.md): no-sleep policy, HDD setup, log bounds and RAM behavior.
- [NOTIFICATIONS.md](NOTIFICATIONS.md): private phone delivery, retries and recovery notices.
- [APP-BACKUPS.md](APP-BACKUPS.md): nightly media app snapshots and private restore verification.
- [AUDIT.md](AUDIT.md): release fixes, verification and limitations.
- [PLAN-REVIEW.md](PLAN-REVIEW.md): full project requirements and missing work.
- [ACCEPTANCE.md](ACCEPTANCE.md): installation, reboot and real-server checks.
- [RECOVERY.md](RECOVERY.md): snapshots, independent copies and restore rehearsal.
- [GATEWAY.md](GATEWAY.md): home HTTPS, certificate trust and optional Tailscale.
- [TV-SETUP.md](TV-SETUP.md): Steam, controller, HDMI and desktop steps.
- [DASHBOARD.md](DASHBOARD.md): dashboard operation and monitoring.
- [RUST-RUNTIME.md](RUST-RUNTIME.md): runtime selection and measurement limits.

Keep original photos until an independently copied full photo/database snapshot restores successfully. The add-only archive and nightly SQL files are useful layers, but are not themselves a matched full Immich restore bundle. Bazzite migration, VM/cloud management, VM/Steam/Samba state backups remain unimplemented. Background ntfy delivery is available after destination setup; email and independent remote heartbeat monitoring are not implemented.

Earlier `AUDIT-v*.md` files are historical records, not current deployment instructions.

## v0.16.0 personal cloud and monthly maintenance

See [NEXTCLOUD.md](NEXTCLOUD.md) and [MONTHLY-MAINTENANCE.md](MONTHLY-MAINTENANCE.md). The full profile now includes Nextcloud and monthly three-drive full-state backups, isolated restore tests, container updates and matching data/image rollback. Python installs the services; scheduled orchestration runs in Rust and upstream Docker/Nextcloud tools keep their native implementations. VM/KVM is unnecessary; Bazzite migration and OS upgrades are separate.

## v0.16.1 music setup

[MUSIC.md](MUSIC.md) covers Navidrome, the shared Lidarr library, Amperfy/Feishin clients, the private non-admin listener and native authenticated checks. The full profile includes music; the dedicated music profile respects your configured service selection. Monthly maintenance no longer blocks solely on unwitnessed human playback checks.
