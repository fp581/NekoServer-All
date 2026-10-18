# NekoServer v0.11.0 audit

This release adds media application backups. Earlier release evidence remains historical.

## Changes

- Rust snapshot worker gracefully stops selected media containers, copies appdata/Compose/installer releases onto the verified media HDD and publishes a private immutable snapshot with file checksums and original ownership/modes.
- A private durable journal records exactly which containers were originally running. Successful and failed operations restart them; systemd ExecStopPost and a boot service recover interrupted operations. Containers originally stopped remain stopped.
- Extra writable state, another container sharing selected appdata, symlinks, hardlinks, special files, nested filesystem entries, wrong UUIDs and nonprivate backup destinations fail closed.
- Python installs schedules and performs explicit verify/new-directory restoration. Scheduled copying runs in Rust, with no runtime Python process. No backup deletion, live restore overwrite or restore container startup occurs automatically.
- Dashboard and acceptance report the additional backup job and require a separately witnessed real application restore.

## Verification

565 Python tests, 7 Rust unit tests and 68 release checks passed. Exact outcomes are in audit-evidence; 18 of the Python tests cover this new snapshot/restore phase. Tests execute the shipped native snapshot worker against real filesystem data with mocked Docker/mount commands. Python restoration fixtures verify copied files, corruption rejection, private permissions, traversal protection and new destination enforcement. Rust clippy denies warnings; cargo tests, language compilation, installer profile previews, generated systemd syntax and native build/source checks are also recorded.

## Limits

No Docker daemon or server hardware is available in this environment. Graceful shutdown/database correctness, actual original UID/GID restoration, power-loss durability, real systemd interruption/reboot recovery, application startup and hardware/storage throughput require server acceptance. Fixture evidence does not prove those checks.

This phase covers media app settings/databases, Compose manifests and installer release bundles. It excludes media content, Immich/PostgreSQL, Docker engine storage, arbitrary volumes, VM disks, Steam/user-home, Samba authentication and host network/service state. Existing Immich matched snapshots remain separate. Backup space increases without automatic rotation; monitor free space and make independent/off-machine copies. Checksum verification detects corruption, not malicious tampering of both payload and manifest. Do not run concurrent deployment/upgrade/onboarding or another host writer during a snapshot.
