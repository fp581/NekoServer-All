# Step-by-step installation and integration review — v0.15.1

## Verdict

The reviewed release is ready for controlled testing on the intended x86_64 Arch/Nyarch PC. This audit found and fixed installation blockers and configuration handoff bugs; it was not just a repeat of the existing tests. No real disk was mounted or formatted, no real Docker daemon/systemd service was started, and no live application accounts or external monitor recipients were configured in this workspace.

The installer is a setup tool for an already installed OS, not an Arch/Nyarch OS installer. Supported deployment still requires Python 3.11+, normal package repositories, formatted/identified drives, an existing desktop user and the necessary hardware. Steam's multilib packages, /dev/dri, controller/HDMI support, time/CA trust and service namespaces must be verified on the PC. Systemd wall-clock schedules use the host timezone: check it before choosing nightly times.

## Review sequence

| Step | Code reviewed | Verification and result | What still needs the PC |
| --- | --- | --- | --- |
| 1. Entry point and configuration | install.sh, installer.py, config_checks.py | Configuration/dependency plans, pure selected-module option checks before changes, private logs, source binary integrity, interrupted-run retry and concurrent-install rejection | Correct OS/prerequisites and chosen profile/configuration |
| 2. Storage | modules/storage.py, shared path helpers, startup.rs | Explicit UUID confirmation, physical OS-disk exclusion, separate app/media physical disks, safe fstab backup/publication and mount identity checks | Actual block topology, filesystem suitability, mount/reboot behavior and absent-drive drill |
| 3. Persistent engine | modules/docker.py, storage-guard command, recovery checkpoint | Fresh data-root setup and rerun simulation, classic store guard, exact SSD/HDD UUID startup guard, no silent store adoption/migration | Real Docker daemon, overlay2 compatibility and retained-store reattachment |
| 4. Media deployment | modules/media.py, eight Compose templates | All files generated through real installer modules and checked with official Compose; loopback ports/shared network/library mounts preserved; custom manifest edits refused | Image pull/startup, Intel hardware acceleration and app version compatibility |
| 5. Photo deployment | modules/photos.py, current upstream Immich manifest | Current v3.3.1 release manifest was fetched through the production resolver. Quoted/unquoted 2283 bindings safely localized; real Compose parser accepts the generated deployment; private .env publication and repeated drive checks | Immich account/phone setup, upload processing and real database/filesystem behavior |
| 6. App connections | onboard.py, media-connect.py, qbit_setup.py, media_libraries.py, seerr_providers.py, both health implementations | Existing API/failure/real-local-HTTP fixtures cover keys, root folders, Prowlarr synchronization, torrent categories, Jellyfin library identity and Seerr routing | First-run accounts, lawful indexers/credentials and a real request → download → import → playback |
| 7. Local/remote access | gateway_tools.py, network_access.py, Samba/Tailscale modules, permission_checks.py | Fresh gateway public ancestors/private state under restrictive umask, native LAN startup checks, loopback upstreams and truthful pending setup states; bind-root permissions distinguished from host traversal | DHCP reservation/static address, client CA trust, actual Samba login, tailnet policy and ACL/supplementary-group behavior |
| 8. Preservation/backups | Rust backup/app_snapshot/host_snapshot, timers and recovery tools | Native copying/checksums, add-only collision behavior, durable app/Samba resume journals, isolated staged extraction, wrong-drive and interrupted-job fixtures; deployment/photo snapshots share a recovery lock | Independent copies, disk capacity/retention choices, actual SQL/app/Steam/Samba restoration |
| 9. Monitoring/alerts | dashboard, media monitor, notifications, heartbeat, acceptance | Current options and private scope hash handed to collectors; public data stays sanitized; local HTTPS certificate/error/redirect fixtures; actual alert receipt remains explicitly unwitnessed | Sensors, systemd sandbox access, outside monitor registration and missed-ping/recovery receipt |
| 10. OS/TV | server_os.py, os_policy.rs, tv.py | Sleep masks, bounded journals, guarded SATA standby settings and TV setup/failure fixtures | Firmware/controller HDD behavior, idle/reboot, Steam login/streaming, Bluetooth/controller revision, sound/resolution |

## Fixes made during this review

1. **Current Immich installation blocker:** the upstream release uses single-quoted `'2283:2283'`; the earlier two-pattern localizer rejected it. The new localizer handles all three quoting forms and refuses missing/ambiguous/unknown mappings. It never converts an unknown publication into a guessed public listener.
2. **Options checked before setup:** invalid selected timing, identity/boolean and job settings now fail before modules touch the OS or create install state. Corrupt native build identity is also caught before native installation begins.
3. **Truthful inspection state:** running without `--apply` records separate inspection results; it cannot replace deployment failures/successes with an inspection-only green result.
4. **Interruption/concurrency:** a module is durably marked running before execution, interrupted states are retryable, and simultaneous installers are refused. Logs/state are private; path traversal/symlink state targets are refused.
5. **Selected settings persist consistently:** confirmed disk/LAN/USB selections update both the original working configuration and the SSD recovery copy, so reruns do not forget them.
6. **Drive safety:** OS-drive sibling partitions are not deployment candidates; application/media filesystems cannot be two partitions of one physical disk. Incomplete topology fails closed. Deployment rechecks stored UUIDs before writes and before starting projects; fstab backup/temp handling no longer follows a dangling reserved backup link.
7. **Caddy permissions:** fresh code/config/portal/state ancestors have explicit modes independent of installer umask. Existing blocked paths, including a restrictive mount-root ancestor, are reported rather than silently chmodded. Private TLS state remains private.
8. **Container permission checks:** appdata is a bind root inside the container; host ancestors need not be traversable by the container UID. Share/library checks still inspect full host traversal. Actual ACL/container behavior is not inferred from mode bits alone.
9. **External photo setup:** an unassigned external filesystem prompts for selection and exact UUID confirmation, supports common desktop automount locations, preserves its chosen filesystem type and remains pending in noninteractive setup.
10. **Tailscale/Samba states:** missing Tailscale authentication or unchosen LAN sharing settings are pending setup, not successful configured connections. Distribution detection handles quoted os-release IDs.
11. **Monitoring handoffs:** the custom heartbeat and archival intervals reach the installed collector. Archive overdue thresholds adapt to the configured interval. A private full relevant-configuration scope hash invalidates old public acceptance summaries without exposing private choices.
12. **Rust startup path:** the default Docker filesystem guard and LAN gateway filesystem/address guard are native Rust. Python guard scripts remain available only for explicit fallback/ad hoc use.
13. **Deployment/snapshot coordination:** media/photo deployment and native preservation use the existing recovery lock alongside application/host snapshots; a concurrent operation fails safely instead of altering a snapshot's source during copying.
14. **Private and durable publication:** Immich environment/manifests and shared atomic file writes use private temporary publication and parent-directory fsync.

## Installation simulation scope

The simulation runs actual modules, not modules replaced with “return success.” It uses a redirected filesystem and command adapters for mount, package manager, systemd and Docker. It exercises:

- Fresh UUID selection and persistence, fstab setup, Docker preparation/checkpoint, all media/photo manifests, timers and dashboard setup.
- The actual native Docker startup verifier as a subprocess, using simulated mount command output.
- The actual official standalone Compose v5.6.0 parser for each rendered Compose deployment, including the fetched Immich release.
- Identical reruns, refusal/preservation of a customized manifest, and rejection of a wrong UUID by the deployed guard.

Kernel mounting, package installation, ownership changes, container startup and systemd operation are simulated. The fixture result does not establish their behavior on the user's hardware. Exact current results and evidence scope are under audit-evidence; earlier proxy/benchmark measurements remain labelled historical.

## Remaining limits and acceptance

First-run app accounts, provider credentials, source choices, desktop identity, certificate trust and outside alert recipients require the user's actual choices. An HTTP success response alone does not prove monitor registration or phone receipt. Remote SMB/NFS sources must already be mounted. Backup share/save scope and independent copies must be selected explicitly; photo-only external copying does not automatically copy full snapshots or SQL trees.

Media image templates still use upstream latest tags; initial container versions must therefore be recorded/validated during deployment, and unattended application upgrades are not enabled. Immich is pinned to its resolved release on first setup. Retained Docker-store recovery requires compatible recorded engine/UID/storage identity. Staged media/host restoration does not activate applications or recreate inert symlinks. ACLs/xattrs/security labels are not included in host snapshots. Full Immich snapshots are supervised ad hoc operations; a hard process interruption may require manually restarting its stopped writers. No automatic backup deletion/retention is introduced.

Personal cloud, VM/KVM management, Bazzite migration and optional private YouTube uploading remain unfinished and are not part of this release's readiness claim.

Use the confirmed configuration, not a replacement sample:

```bash
python3 installer.py --profile full --check --config /absolute/path/to/confirmed-config.json
sudo bash install.sh --profile full --apply --config /absolute/path/to/confirmed-config.json
sudo python3 acceptance.py --profile full --save --config /absolute/path/to/confirmed-config.json
```

Save a baseline, reboot normally, rerun acceptance and complete the witnessed tests. Verify actual restore, playback/transfer, intended user access and outside alert/recovery receipt before calling the installed stack ready for everyday use.
