# NekoServer installation and acceptance

## Guided setup

With guided_setup.enabled=true (default), --manual --save now uses guided yes/no handoffs instead of the legacy typed PASS/FAIL prompt. No repeats the instructions and opens the relevant pages/apps again. Ctrl+C leaves that check pending. Manual confirmation never overrides automatic failures. Disabling the guide retains the legacy explicit PASS/FAIL mode. See GUIDED-SETUP.md.


## Prepare before installation

Verify actual hardware compatibility and prices before ordering. The installer targets x86_64 Arch/Nyarch and Debian/Ubuntu, Docker classic overlay2, two distinct persistent filesystems and an optional external backup drive. Bazzite automatic recovery remains unsupported. The application SSD and media HDD must already have suitable filesystems: the installer discovers/confirms existing UUIDs and mounts them; it never formats them.

Keep independent copies of important files. Use disposable photos/videos for deletion and interruption tests. The existing 512GB external drive may be too small for a complete snapshot of a larger library; check actual capacity. A matched recovery snapshot on the source HDD is not protection against that HDD failing. Establish independent backup space before deleting phone originals or testing storage loss.

Use the actual config.json produced/confirmed during setup. A release ZIP contains blank UUIDs rather than machine-specific identities. Never replace your configured file with those defaults during an upgrade. The full profile also needs selected media sources/accounts, a pinned Immich deployment, external-backup identity, the intended desktop user and Samba credentials. Optional remote_access additionally requires Tailscale authentication.

## Deploy and preserve the tools

Inspect the plan and then apply the required profile:

```bash
sudo bash install.sh --profile full --dry-run
sudo bash install.sh --profile full --apply
```

Profiles may report needs_setup for accounts/authentication or provider configuration. Complete those steps and rerun affected modules using --module NAME --apply or the appropriate profile; inspect their logs locally. Do not interpret installer completion as a verified restore or completed download.

The recovery module preserves the current scripts/configuration under the configured application SSD. The following examples use the default SSD path; substitute your actual one. On an existing server, apply this release's needed profiles so the preserved installer bundle, dashboard collector and current backup reporting helpers are installed. A current foundation/recovery bundle is required by the acceptance report.

## Collect a baseline, then check after reboot

```bash
cd /srv/nekoserver/installer/releases/v0.28.1
sudo python3 acceptance.py --profile full
# Inspection only: no report write.
sudo python3 acceptance.py --profile full --save
# Now reboot normally through the desktop or your normal system administration flow.
# After reboot, return to the same directory and run:
sudo python3 acceptance.py --profile full --save
```

--config can select another actual configuration file. --profile defaults to full and uses that profile's dependency closure. Standard scopes are foundation, media, photos, network, networking, lan_access, remote_access, media_monitor, dashboard, tv and full. Custom configured profiles can be inspected in the CLI, but the dashboard summary only displays the standard scopes. The dashboard-only profile does not inspect absent media applications, Docker or backup jobs.

The first saved report records a verified storage baseline and boot identity. The same boot never counts as reboot proof. A later boot must match the selected profile/configuration and correct drives. Once recorded, that observed reboot evidence can be retained on repeated checks during the same later boot; changing the configuration/profile resets it. Current service and container checks always rerun. A missing/unverified baseline cannot prove reboot success. No command in acceptance.py reboots the server.

Default inspection only prints fixed results. --save replaces **reports/acceptance/latest.json** on the verified application SSD with a private 0600 report. The storage guard rechecks both UUIDs immediately before saving. A mismatch blocks report writing. The report contains status, fixed descriptions, scoped configuration digest, boot/run identifiers and timestamps; it contains no raw Docker environment, application API response, keys, passwords, SQL or arbitrary error text.

## Automatic checks and what they establish

| Check | Evidence |
| --- | --- |
| Configuration | Selected profile and dependency plan validate. |
| Host | Supported x86_64 Arch/Nyarch or Debian/Ubuntu recovery host. |
| Data drives | Actual mount UUID/filesystem identity and distinct filesystems. Wrong drives block private/dependent inspection. |
| Reboot | Current matching-drive inspection occurs after a saved verified baseline on an earlier boot. |
| Preserved installer | Current release bundle files match its manifest and storage configuration. |
| Docker store | Running local daemon uses the intended SSD/docker-engine and classic overlay2, plus active/enabled boot service. |
| Startup guard | Managed unit definition, guard code/config and currently loaded non-ignored ExecStartPre/mount dependencies agree. |
| Checkpoint | Engine version, architecture, storage and configured UID/GID match the recovery checkpoint. |
| Containers | Exactly one expected project/service, running/non-unhealthy state, correct persistent source and destination, media network and localhost-only published ports. Jellyfin/music libraries retain required read-only mounts. |
| Media configuration | Current GET-only API/library/provider checks, distinct from actual transfer/playback tests. |
| Backups | Recent successful updated archive/database/external-copy report with active timer. No stored success is still pending. |
| External identity | Intended external UUID/filesystem is connected for this check. Being disconnected between ordinary runs is expected; wrong UUID fails. |
| Dashboard | Local sanitized endpoint responds with a snapshot no older than 150 seconds, plus active/enabled web service and collection timer. |
| Networking | Selected Samba/Tailscale boot services and authentication; gateway configuration/ownership and HTTPS transport where selected. Real login, certificate trust, uploads, streaming and denied-client/share permissions need witnessed tests. |
| TV | Managed launcher hashes still match the intended desktop user's installation report. Actual display, sound, controller and power behavior are witnessed. |

A passing automatic check describes that evidence, not every possible behavior of the component. Non-running/unhealthy containers, public Docker bindings, wrong destinations or writable protected playback libraries fail. A successful backup job does not establish independently restored data. Root inspection is required for private reports, Docker and storage guards; --help needs no privileges. External drives must be connected when accepting their identity and copy results.

## Perform and record witnessed tests

The runner prints the tests required by the chosen profile. Perform each applicable test on the server, then use:

```bash
sudo python3 acceptance.py --profile full --save --manual
```

The command reruns automatic checks and, by default, opens relevant pages/apps and asks yes/no for each witnessed test. Yes records that specific test as user-reported; no repeats its instructions and reopens its targets. Ctrl+C leaves that test pending. Confirmation timestamps and user_reported labels are retained. Do not confirm a planned/unperformed test. These answers never override an automatic failure. With guided_setup.enabled=false, the legacy explicit PASS exact_test_id / FAIL exact_test_id / Enter prompt remains available.

| Witnessed test | Required exercise |
| --- | --- |
| missing_drive | With verified independent backups and the server shut down, test a deliberately unavailable persistent drive; verify dependent startup is blocked. Shut down before reconnecting. Never unplug a live irreplaceable drive. |
| media_workflow | Use authorized material to verify the configured request/acquisition/import/playback chain; inspect requested hardware acceleration. |
| music_playback | Verify the configured music import and actual client streaming. |
| photo_preservation | Upload disposable originals, verify their preservation/checksums, and test phone/Immich deletion behavior without risking real originals. |
| independent_restore | Independently copy a matched snapshot, restore it to a fresh isolated workspace and open originals/videos/albums. Verify database and deletion behavior; see RECOVERY.md. |
| external_backup | Verify additive copies/checksums, safe disconnect/reconnect and interrupted-copy recovery; confirm deleted source files remain in backups. |
| remote_permissions | Use actual intended clients, including an away-from-home test as applicable. Verify guest denial and protected-share write denial. Localhost application gateway remains deferred. |
| tv_session | Test HDMI/audio, Xbox controller, Steam login/Remote Play, app shortcuts and AC idle behavior while jobs remain running. |
| os_recovery | With independently verified backups, rehearse supported OS recovery, retained apps/data and subsequent reboot. See RECOVERY.md; automatic Bazzite reattachment is blocked. |

The runner uses only the tests relevant to your configured profile. Manual answers are not inherited from an older run. Recheck and reconfirm when you create a new report; prior confirmation is historical evidence, not proof of unchanged behavior. Test failures remain failures. Same-boot observed reboot evidence may be retained if scope/configuration still match.

## Read the result

Exit 0 means **all checks in that selected profile** passed, including user-reported witnessed tests. Exit 1 means setup/tests remain pending; exit 2 means a check failed or inspection/save could not finish. The command does not assert hardware safety, silently start downloads, change access controls or perform restores for you.

Backups & recovery on the dashboard shows profile, age, automatic/manual counts and acceptance state. It exposes only fixed labels and aggregate outcomes, not boot identifiers or private detailed records. Old-release or changed storage/service-selection reports are outdated. Reports over 24 hours old are stale. Unsupported/malformed/empty reports cannot show complete, and manual passes need witness metadata. The page remains read-only.

The existing restoration warning remains visible even when a checklist is complete: original-photo recovery acceptance is an ongoing responsibility, not a permanent guarantee. The dashboard report is shown on its next collection; reload once the collector has run. No new external notification or remote gateway is introduced by this release.

## Application gateways

See GATEWAY.md for LAN certificate trust, selected app ports and optional private Tailscale access. gateway_clients records real-client evidence separately from automatic transport checks. The full profile uses LAN access; remote_access is optional and should be accepted separately when deployed.

## Native worker selection

See RUST-RUNTIME.md before upgrading workers. The current recovery bundle includes native source, executable and build checksums. Native selection is explicit; reapply affected modules after an OS replacement to restore services and private backup identities. Acceptance checks the selected native binary and loaded commands; changing backend requires new report/reboot evidence. Real file/database restoration remains required.

Media application snapshots have a separate `app_restore` witnessed check. Confirm checksum restoration, actual isolated database/app startup and interrupted-run/boot restart recovery; see [APP-BACKUPS.md](APP-BACKUPS.md).

Background notifications add a separately witnessed `notification_delivery` check for actual phone receipt with the browser closed and problem/recovery/network-failure behavior. A passing HTTP response alone does not complete it.

The `server_idle` witnessed check covers idle/reboot service availability and the intended HDD behavior. Live sleep masks and managed journal/standby configuration are inspected automatically.

Host backup acceptance requires a current host job/timer, native command/guard and witnessed `host_restore`. Review source mapping and missing optional paths, copy independently and test actual game loads/Samba authentication before declaring recovery proven.

Heartbeat acceptance adds a current native timer/check-in check and the witnessed `outage_delivery` check. HTTP acceptance does not substitute for outside missed-ping alert/recovery receipt.
