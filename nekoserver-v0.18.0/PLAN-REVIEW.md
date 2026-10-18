# v0.18.0 schedule policy

Routine NekoServer media checkups are disabled, music audio has no automatic backup, and managed photo originals/database/external backups run weekly on Sunday night. Monthly update/recovery checks and app-state backups remain. [STORAGE.md](STORAGE.md) defines the current policy and supersedes older schedule/coverage descriptions below.

# v0.17.0 storage update

The current layout is defined in [STORAGE.md](STORAGE.md): music and Immich live data on SSD, independent HDD backups, movies/shows only on HDD, and a verified 60-minute HDD idle timer. This supersedes older layout statements below. Existing deployments with old host paths are preserved and require a verified migration.

# NekoServer: full-plan review and remaining work

Reviewed October 14, 2026 against the supplied project summary, subsequent decisions and the available original-plan context. This is a requirements check, not a claim that an uninstalled server is production-ready.

## Requirements and current evidence

| Requirement | Implementation in v0.15.1 | Automation and remaining acceptance |
| --- | --- | --- |
| Nyarch bare-metal, TV PC and 24/7 server | Arch/Nyarch modules; Docker on the persistent SSD; OS-wide sleep masks/drop-ins, bounded journal, guarded SATA standby setup and GNOME AC suspend setup | OS installation itself is outside this installer. Confirm motherboard, Intel GPU, HDMI/audio and idle operation on hardware. |
| Replaceable 256GB OS SSD, 1TB app SSD, 12TB media HDD | UUID selection, separate mounts, Docker startup guard and SSD installer/state | Exact drive selection requires explicit UUID confirmation. No automatic formatting or silent migration. Test restart and deliberately absent disks with independent backups. |
| Modular reruns, dependency ordering and independent failure handling | Named profiles, dependency graph, failure/needs-setup state, retries | Covered by command and failure fixtures. Real package managers/systemd still need deployment checks. |
| Jellyfin and Navidrome | Separate Compose projects, HDD libraries and TV/gateway shortcuts | Jellyfin first administrator and Navidrome first account remain app-wizard steps. Jellyfin library setup is automated after authenticated administrator login. Music and video playback remain witnessed checks. |
| Prowlarr, Sonarr, Radarr and Lidarr | API onboarding, root folders, Prowlarr connections and qBittorrent clients | Select lawful indexers and supply required credentials/profile choices; verify a real download/import. The audit checks saved keys, enabled routing and accessible folders. |
| Seerr requests | Jellyfin identity/library verification, guarded Sonarr/Radarr providers and live provider checks | Complete Seerr's first-run login/server selection. Existing custom/default/4K choices are preserved. Test a real request through the complete pipeline. |
| qBittorrent and existing sources | Cookie-authenticated configuration, categories and verified add-only media copies | Installer asks for login and indexers/sources. Remote SMB/NFS sources must already be mounted; automatic remote mounting is not implemented. Source inventory alone never means a source was imported. |
| Immich on persistent storage | Pinned upstream release manifest, private database settings and HDD upload tree | Create the app account and configure the phone. Upload, deletion behavior and actual restoration remain hardware/client acceptance. |
| Permanent photo archive and hourly preservation | Rust archive worker; add-only verified copies, minimum age, no overwrite, exclusive scheduled locks | Keep originals until independent restoration passes. Archive directory permissions no longer depend on the installer umask. |
| Nightly database and weekly external copies | Rust database coordinator, SSD/HDD SQL copies, external UUID/filesystem checks and checksum copies | USB identification requires configuration. Archive and SQL dumps are not an automatically matched full-restore snapshot. External copy currently includes the photo archive, not the full snapshot or SQL trees. |
| Matched full Immich recovery | Explicit snapshot command pauses app writers, copies upload tree and SQL; separate-project restore rehearsal | Snapshot creation, independent copying and restore rehearsal are explicit operations. They are not automatically scheduled. Verify albums, original files and database restoration on a real Docker installation. |
| Home-network sharing with optional remote access | LAN HTTPS Caddy portal; optional private Tailscale Serve; authenticated Samba | Trust the LAN CA on clients and secure app accounts before exposure. Samba now prompts for a missing password interactively and reports incomplete noninteractive setup. Verify actual client and tailnet policy access. |
| Steam Remote Play, Xbox and TV shortcuts | Native Steam packages/permissions, optional Bluetooth, desktop launchers and optional Big Picture autostart | Steam login, pairing, Remote Play, controller-revision compatibility, HDMI resolution and sound remain user/hardware steps. No inferred login or destructive display changes. |
| Visual monitoring and SMART | Rust dashboard server/collector; sanitized snapshots, storage/SMART, job outcomes and acceptance summaries | Real sensors, sleeping HDD behavior, DynamicUser isolation and permissions need hardware acceptance. Local browser alerts require an open dashboard. |
| Lightweight post-install execution | Rust server, collector, media audit, archive, database coordinator, external-copy worker, app snapshots and notifications | Python is installation/onboarding, ad hoc acceptance/recovery and an explicitly selected fallback. Third-party apps keep their upstream implementation. Timer jobs exit after work. |
| Nyarch reinstall/recovery | SSD release bundle, Docker checkpoint, guarded reattachment and restore tooling | Real OS reattachment still needs rehearsal. Docker compatibility and UUIDs must match. |
| Bazzite fallback | Recognized as unsupported for automatic reattachment | **Missing implementation/validation.** Native/runtime/immutable-OS compatibility must be designed and tested; no silent reuse of Docker storage. |
| VM/KVM management | No VM installer/management module | **Missing code.** Keep VMs out of the readiness claim. |
| Personal cloud storage | No dedicated personal-cloud module | **Missing code.** Samba/Immich do not constitute the separate planned cloud feature. |
| Media application backups | Nightly private Rust snapshots of selected media appdata, Compose manifests and SSD release bundles onto the media HDD; checksum inventory and new-directory restore | Graceful stop, exact originally-running container restart, durable journal and systemd/boot recovery. No deletion/rotation. Native failure and filesystem restore fixtures pass; real app startup/restore remains pending. |
| Host/Steam/Samba backups | Nightly Rust private snapshots of selected OS settings, SSD installer bundles, native Steam data and managed standalone Samba state; explicit extra save/share paths; verified staged restoration | Actual OS/Steam/Samba activation and independent copying remain acceptance work. Extra share contents/native-game saves must be selected explicitly. VM disks and arbitrary host state remain unimplemented. |
| Notifications with the dashboard closed | Rust ntfy delivery worker; grouped problems/recoveries, debounce, repeat limits and backoff; OS-local private state and destination | Configure your own HTTPS topic/token and phone subscription. Local TLS/authentication/failure fixtures pass; real provider, phone, network/reboot receipt require acceptance. Independent Rust HTTPS heartbeat is now implemented separately; outside provider/recipient setup and witnessed missed-ping/recovery receipt remain pending. Direct email delivery remains unimplemented. |
| Optional private YouTube archive | No uploader module | Explicit optional, last-priority future feature; publishing must still require manual approval. |

## Connections checked in the software

- Prowlarr to each selected Arr app: enabled synchronization, Docker URL and matching API key.
- Arr apps to qBittorrent: enabled client, Docker host/port and expected category; initial setup verifies login and preferences.
- Seerr to Jellyfin: saved server identity, authenticated expected libraries and enabled library IDs.
- Seerr to Sonarr/Radarr: saved default routing, key, root, search policy and live profiles/root visibility through Seerr.
- Containers to storage: exact expected host and container paths; playback libraries read-only; managers/downloads writable.
- Gateway to the selected apps: loopback upstreams, separately configured LAN/private-tailnet endpoints and app-account confirmation.
- Timers to workers/reports: selected runtime and correct arguments; incomplete configuration differs from runtime failure; reports remain private and sanitized.

## Permission limits

Fresh shared directories and copied files have explicit modes, independent of umask. Fresh private database directories remain private. Existing directories retain their ownership and modes. Acceptance checks traversal and Unix mode bits for configured media and Samba accounts without recursively changing existing trees. These checks do not emulate ACLs, container supplementary groups, Samba authorization or systemd namespaces; actual client/container tests are still required.

## Readiness statement

The implemented stack is ready for controlled hardware testing. The full original plan is not complete. Installation, mount confirmations, first-run accounts, device pairing, chosen indexers/credentials, certificate trust and independent restore tests cannot be inferred safely. The acceptance checker keeps them pending rather than substituting a passing unit-test count.

The server_os phase adds available-RAM alerts without forcing cache eviction. HDD firmware/controller behavior remains a hardware acceptance check; an accepted legacy standby command does not prove the spindle always stays active.

The v0.15.1 step-by-step audit is in INSTALLATION-REVIEW.md. Fresh installation/reruns are simulated through real modules and validated with official Compose; real hardware/engine/account/restore acceptance remains mandatory.

## Current scope update — v0.16.0

Personal cloud is implemented with Nextcloud. Monthly overnight backup/rehearsal/container update and data/image recovery are implemented. Full snapshots are copied to HDD, SSD and external USB; phone upload, hourly archive and weekly original-file preservation remain distinct. VM/KVM is removed from this automation’s requirements. Bazzite migration is explicitly deferred to a separate automation. Physical installation, live app/version compatibility, client permissions, capacity/downtime, alert receipt and independent-drive disaster restore remain acceptance work. Optional YouTube uploading remains optional future work.

The v0.16.1 music setup is complete in software: Navidrome/Lidarr share the HDD music library, guided private listener configuration and native catalogue checks are connected, and Amperfy/Feishin client setup is documented. Actual phone/desktop installation, credentials, gateway trust and listening/offline playback remain witnessed acceptance. See MUSIC.md.
