# NekoServer visual dashboard

## Open your control room

The installed page is **http://127.0.0.1:8787/** on the NekoServer itself. The TV profile adds a Neko Control Room desktop shortcut. Port 8787 is configurable through dashboard.port; other application and restore-test ports are reserved. It is also on your own devices through Tailscale at **https://yourserver.tailXXXX.ts.net:8448/** (Tailscale Serve, never Funnel: only devices signed in to your Tailscale account). The dashboard is read-only, so nothing can be changed from it; it answers only its loopback names and this server's own Tailscale name. Set `"remote": false` under `"dashboard"` and run `sudo bash install.sh` again to keep it on the server only.

Open dashboard/preview.html directly in a browser to explore the same interface without installing anything. The preview is offline, uses example readings and does not request notification permission or navigate application links. Tabs, application search and the TV readability toggle work. The standalone delivery HTML is the same preview.

## Installation and existing-server updates

Set or confirm your actual storage UUIDs through the normal installer workflow. Never substitute the example UUIDs. The storage module must mount the configured SSD and HDD before dashboard installation.

```bash
sudo bash install.sh --profile dashboard --dry-run
sudo bash install.sh --profile dashboard --apply
```

The dedicated dashboard profile runs system, storage, recovery and dashboard. It does not deploy media, photos, networking, TV or scheduled media checks. The full profile includes the dashboard before TV. Existing configurations do not silently activate the new optional module through --all. Reuse your actual configuration and add dashboard options when needed; defaults are port 8787, collection interval 60 seconds, SMART interval 1800 seconds, and install_smartmontools true.

Existing backup jobs need this release's runtime helper and generated service definitions to begin recording results:

```bash
sudo bash install.sh --profile photos --dry-run
sudo bash install.sh --profile photos --apply
sudo bash install.sh --module photo_backup --dry-run
sudo bash install.sh --module photo_backup --apply
# For current authenticated media checks, if this profile is wanted:
sudo bash install.sh --profile media_monitor --apply
# For the new desktop shortcut, using your configured desktop user:
sudo bash install.sh --profile tv --apply
```

These modules retain their storage checks, existing-data protection and existing application onboarding requirements. An Immich version must be pinned through the existing setup workflow. The weekly external backup still requires its configured drive UUID and a connected external drive; it never propagates deletions. Run the ordinary backup jobs and inspect their results rather than interpreting old backups as newly tested. Previous runs have no historical dashboard status until an updated job runs.

## What the readings mean

| Area | Source and interpretation |
| --- | --- |
| CPU | /proc/stat delta between collections, including idle and iowait. First sample and boot changes show unknown. |
| Memory | /proc/meminfo total minus available memory. |
| Temperature | Highest readable coretemp/k10temp/zenpower CPU sensor. Unsupported sensors stay unknown. |
| Uptime | /proc/uptime. |
| History | Up to 120 samples within the last two hours on the application SSD. No fabricated samples; gaps remain gaps. |
| Storage | findmnt verifies configured UUID and filesystem before SSD/HDD capacity or private report access. Missing/wrong drives show unavailable. |
| SMART | Read-only smartctl JSON health/attributes, cached per disk identity. Standby disks are not deliberately woken. Unsupported mapping, missing tool or sleeping drives show unknown. |
| Applications | Exact Compose project/service labels from local Docker ps; running does not prove account, library or media playback readiness. |
| Backups | Private updated job reports: latest attempt, successful run and schedule state. Failed/overdue jobs alert; an abandoned running job eventually becomes overdue. |
| Media checks | Restricted current/stale scheduled media-monitor report; setup requirements stay visible. |
| Recovery | Checkpoint/rehearsal presence. Original-file, album and deletion-behaviour acceptance remains required even after an isolated test starts. |

The collector runs after boot and approximately every 60 seconds after its previous run finishes. The page refreshes every 15 seconds and visibly marks failed/old readings unavailable after 150 seconds. SMART probes default to every 30 minutes and never launch disk self-tests. Conventional SATA/NVMe devices are supported; RAID, crypt/device-mapper and unusual USB bridges may need manual health inspection. A SMART pass alone does not prove a disk cannot fail.

Warnings cover missing/mismatched mounts, storage usage at 90% (critical at 97%), SMART problems, CPU temperature at 80°C (critical at 95°C), non-running/unhealthy containers, failed/overdue backups and stale/failing media checks. These are monitoring thresholds, not CPU-specific hardware limits. Archive success is overdue after three hours, database success after 48 hours, external copy after ten days. No prior successful run shows unknown. An inactive schedule is shown explicitly even when a recent run passed.

## Appearance and notifications

Overview brings together the main cards. Applications shows all deployed-service tiles plus Steam Big Picture and YouTube desktop/browser links, with search. Storage, Backups & recovery and Alerts have focused views. The top TV button increases label sizes; its preference is stored only in the browser. The layout includes mobile breakpoints, keyboard focus indicators and reduced-motion support.

The notification button asks the browser for permission on the live localhost page. Only after permission and opt-in does it show local system notifications for new warning/critical items while the page is open. A maximum of three newly encountered alerts is shown per refresh. It does not email or message anyone, and is not a background notification service. Reopening the page can announce currently active alerts again. Browser settings control revocation.

## Status boundary and recovery

The root collector uses a private sanitized configuration under /etc/nekoserver-dashboard/config.json. It contains drive identities and selected service identifiers, not the entire installation configuration. Private CPU/SMART/history state and backup-job reports live on the SSD. Public /run/nekoserver-dashboard/status.json contains only the selected health fields and fixed alert text. The collector refuses to read private SSD reports/history when its drive identity is wrong.

The HTTP service binds only 127.0.0.1, runs as a dynamic unprivileged user and is blocked from the Docker socket, application SSD and private dashboard configuration. It serves an exact asset allowlist and one status endpoint. It rejects foreign Host/Origin and cross-site requests, has no write/action endpoints, and sets restrictive content/security headers. There is no remote gateway or administrative authentication flow in this release.

OS assets are immutable per release under /usr/local/lib/nekoserver-dashboard/v0.8.1. Existing differing assets and unmanaged units stop installation rather than being overwritten. Reapplying a managed dashboard refreshes its service definitions, takes a collection and restarts only its web service. The SSD recovery bundle preserves dashboard assets, code, helpers and this guide; recreate OS service files with the dashboard profile after validated OS recovery. Generated live runtime status disappears at reboot and is rebuilt by the collector. Docker and photo recovery retain the restrictions in RECOVERY.md, including blocked automatic Bazzite reattachment.

```bash
systemctl status nekoserver-dashboard.service nekoserver-dashboard-collect.timer
sudo journalctl -u nekoserver-dashboard-collect.service -n 50
```

Review sensitive application logs locally. Do not paste passwords, API keys or database contents into ordinary reports. Complete a real installation/reboot, actual browser display check and independently backed-up full photo restore before treating the system as production-ready.

## Deployment acceptance summary

Run the current acceptance.py from the preserved installer with --profile full --save, or choose the profile actually installed. Backups & recovery shows its restricted report, profile and manual progress. Reports from another release/configuration are outdated; reports over a day old are stale. Only complete automatic and witnessed tests can complete that scoped checklist. See ACCEPTANCE.md for baseline/reboot/recording commands.

## Application access in v0.9.0

The monitoring dashboard stays localhost-only. The separate LAN portal uses the same cat and theme to link selected apps over HTTPS; optional Tailscale access provides individual app URLs. See GATEWAY.md. Acceptance summaries recognize the gateway scopes and witnessed client checks.

## Native dashboard in v0.10.0

The same interface can be served/collected by the Rust runtime. New configurations select Rust; existing configurations retain Python until explicitly changed. Reapply dashboard after selection. The private collector and DynamicUser localhost service remain separate processes. See RUST-RUNTIME.md for measured footprint and unprivileged hardware acceptance requirements.

## Background delivery

The Alerts area also shows sanitized ntfy delivery status. Notifications run independently of an open browser after the notification module is configured; see [NOTIFICATIONS.md](NOTIFICATIONS.md). Provider acceptance and confirmed phone receipt are separate checks.
