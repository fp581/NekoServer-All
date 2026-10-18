# Current plan review - v0.21.0

| Requirement | Current implementation | Remaining real-server work |
| --- | --- | --- |
| Fresh Arch/Nyarch setup | Base prerequisite bootstrap, selected-module packages, private config, dependency/retry state | Existing supported OS/repos/user, formatted disks and Steam multilib |
| Separate SSD/HDD/USB | Exact UUID confirmation, OS-disk exclusion, physical-disk separation, guarded writes | Identify devices, reboot and absent-drive acceptance |
| Immich | SSD uploads/database, weekly HDD preservation, weekly external originals, monthly full recovery | Phone albums/background permissions, matched independent restore |
| Jellyfin/Arr/qBittorrent/Seerr | Compose plus authenticated connectors and shared video paths on HDD | Console-selected/generated admin accounts are provisioned; choose providers and witness request-to-play |
| Navidrome | SSD music/downloads, private listener, client setup | Load library and listen on chosen device; no raw music backup |
| Nextcloud | Four-container deployment, HDD files/SSD app+DB, monthly three-drive full backup | Admin selection/verification automated; witness client transfer/sharing and restore |
| Monthly maintenance | Complete snapshot copies/checks, isolated restore rehearsal, supported-stream container updates, matched old-data/image rollback | Connected external drive, capacity and supervised real-engine failure drills |
| No-sleep/standby/RAM | Sleep masks, bounded logs, guarded 60-minute SATA idle timer | Firmware/drive behavior and actual memory trend; no cache-clearing job |
| Access/permissions | Loopback apps, secured LAN gateway, private CA, Samba and optional Tailscale | Trust certificate, authenticate intended users and test denied access |
| Runtime | Rust scheduled workers/guards/dashboard; Python/shell setup and explicit recovery/acceptance | Actual kernel/systemd/service sandbox checks |
| TV and alerts | Steam/Firefox/controller setup; opt-in ntfy/outside heartbeat | Gaming PC login/streaming/HDMI/controller, real delivery/outage witness |
| Routine media checkup/music backup | Disabled; retained cleanup safely retires managed old jobs | Inspect timers after applying |
| Bazzite/VM | Bazzite separate future work; no VM/KVM required | Outside current automation |

This release is for controlled deployment testing. Executable fixture tests are not a live VM/hardware installation. The supplied PDF distinguishes automated steps from account/drive/trust choices and gives the final witnessed checklist. Exact evidence and scope are in AUDIT.md and audit-evidence/EVIDENCE-NOTES.md.
