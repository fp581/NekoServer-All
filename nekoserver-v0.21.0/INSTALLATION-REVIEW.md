# Account setup extension - v0.21.0

See ACCOUNT-SETUP.md for Konsole generation/custom prompts, local account provisioning, verified connector reuse, private export, interrupted reruns and existing-account behavior. This extends the previous deployment review. New account APIs are exercised through stateful real local HTTP fixtures with simulated Docker identity; live application containers remain unverified.

# Installation review - v0.21.0

Current deployment instructions are README.md and the supplied illustrated PDF. Historical audits describe earlier releases and must not override STORAGE.md, NEXTCLOUD.md or MONTHLY-MAINTENANCE.md.

Reviewed paths: shell prerequisite bootstrap; configuration validation/dependency ordering; UUID confirmation and mount guards; fresh Docker data-root and verified Rust deployment; media/Immich/Nextcloud Compose; API onboarding and private gateways; host/container permissions; weekly photo protection; monthly complete snapshots, isolated restore rehearsal and matched image/data rollback; schedule cleanup; no-sleep/standby policy; TV setup; acceptance/reporting.

The new shell entrypoint can install missing one-time base prerequisites on supported Arch/Nyarch hosts before Python runs. Read-only modes never mutate packages. Package availability is checked before package installation; failures stop the entrypoint. Module-specific packages retain their selected-module installation paths. A maintained OS, working repositories/network, systemd/x86_64, preformatted disks, existing user and Steam multilib remain host prerequisites.

Integration fixtures execute actual modules with redirected files and simulated host/package/container operations. They exercise generated Compose using the official standalone parser, reruns, custom-manifest preservation and wrong-UUID refusal. Native filesystem/HTTP/backup/recovery tests execute real binaries against isolated fixtures. These do not prove actual hardware, systemd sandbox, live container upgrades, real client accounts or package repository compatibility.

Before everyday use, complete the illustrated acceptance checklist: app/phone accounts and actual transfers/playback; intended LAN/Tailscale/SMB access; certificate trust; independent backup restoration; reboot and missing-drive behavior; SATA standby/wake; capacity and monthly supervised update/rollback. No current live VM or hardware pass is claimed.
