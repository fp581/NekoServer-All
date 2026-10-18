# NekoServer v0.7.1 — recovery audit

October 8, 2026. **321 automated tests pass**, including 63 new recovery tests. Earlier audit reports remain in the package.

## Recovery handoffs

A new recovery_checkpoint module follows Docker in the foundation, media, photos, full and media_monitor profiles. It records only a verified classic overlay2 Docker root on the configured SSD, storage UUIDs/filesystems, engine version, architecture and configured media UID/GID. Unsupported or unavailable checkpoints remain needs_setup. Existing user configurations do not silently enable this new optional module through --all. The recovery bundle preserves all new scripts, imports and this guide on the SSD.

recover.py check verifies both mounted UUIDs/filesystems and every checksum in the preserved installer release manifest. Reattachment requires that bundle, a compatible checkpoint, the same nonempty Docker store, same media UID/GID, matching native Docker client AND daemon versions, an x86_64 Nyarch/Arch host, inactive Docker service/socket, an empty default store and compatible daemon/guard configuration. An explicit application SSD UUID confirmation is required before changes. The tool installs the verified UUID mount guard before writing daemon configuration and starting Docker. Failed startup/post-start verification stops Docker service/socket. It does not migrate, format, recursively chown or restore application data over existing files. Reattachment resumes the same existing store; it is not a disk-failure restore.

Matched Immich snapshots cover the four standard services, the full upload tree (including profiles, generated files, marker files and empty directories), a logical pg_dump and the resolved Compose configuration. They pin the immutable digests of images actually used by the existing containers. Required source storage bindings and database credentials/routing must match; external libraries, custom mounts/services, explicit custom users/hardware/runtime settings, non-root images, missing digests or unpinned configured release versions require manual recovery. Private bundle files include application secrets; directory permissions protect them on POSIX storage.

Snapshots preflight storage identities and free space, create a new private directory, stop only previously running non-database services, rescan/copy/hash the upload tree and dump the database while those writers are stopped. The same services resume in finally, including after partial stop/copy/dump failures. Failed snapshots retain an incomplete marker and cannot be restored. No snapshot name is reused or overwritten. Do not run concurrent administrative operations against Immich during snapshot creation.

Restore rehearsal validates the inventory and every file checksum, requires a new workspace under SSD/HDD recovery-tests and a different free localhost port, and requires RESTORE-ISOLATED confirmation. All bind paths point into that workspace. Production container names, network/volume names, host ports and storage environment paths are removed; database/cache/ML routes point to the test services. A new project and internal network keep application integrations from normal outbound access. Fresh Postgres starts alone, becomes ready and receives SQL using single-transaction, ON_ERROR_STOP and explicit -f -. The documented search_path substitution is applied only to a copied SQL stream. Public table count is checked before starting the test applications. The HTTP check does not follow redirects. Failures stop only that project, retain workspace/report/incomplete state and preserve the source backup. Images may be pulled by digest when absent. The successful report still says needs_setup until manual photo/account/deletion acceptance.

## Validation and limits

All 321 tests pass. Recovery cases cover actual temporary filesystem snapshots/checksums, profiles/markers/empty folders, service pause/resume sequencing, partial failures, corrupt/incomplete/extra/changed files, symlinks, free-space refusal, existing-destination preservation, custom/external runtime refusal, isolated paths/resources/routing, SQL import ordering/transaction flags, failed imports/HTTP startup, client/daemon mismatch, UUID and UID/GID mismatch, unsupported hosts, existing foreign stores/guards, locking, bundle checksum validation and preserved helper imports. A real local HTTP fixture verifies success and redirect rejection.

Python compilation, shell syntax, seven profile plans in check/dry-run mode, eight rendered media Compose templates, generated systemd fixture syntax, desktop entry parsing and recovery CLI help checks pass (32 release checks). ZIP integrity and all packaged checksums are verified before delivery.

Docker/PostgreSQL executables are unavailable in this workspace. Commands and service/database responses are simulated; no actual image pulls, database import, Docker store adoption or OS reinstall occurred. Compose JSON is generated from validated fixtures and checked through simulated Compose commands; actual Compose rendering/startup must pass on the server. Same-version/backend checks reduce unsupported combinations but do not prove kernel/storage compatibility or a complete recovery.

Real Immich restoration, original file/video/album verification, deletion safety, reboot/missing-disk behavior and server deployment remain outstanding. The rehearsal deliberately has no production cutover or automatic cleanup command. Complete backups of media application databases/configuration, VM disks, Steam home state and external libraries are not introduced by this photo snapshot feature. Production image pinning outside the snapshots, Bazzite runtime migration, monitoring dashboard and remote app gateway remain future work. Keep original photos until an independently copied full restore has passed.

## References

- Immich backup/restore and file locations: https://docs.immich.app/administration/backup-and-restore/
- Standard Compose topology (use the installed release, not main, for deployment): https://raw.githubusercontent.com/immich-app/immich/main/docker/docker-compose.yml
- Docker canonical JSON rendering: https://docs.docker.com/reference/cli/docker/compose/config/
- PostgreSQL transaction/error flags: https://www.postgresql.org/docs/15/app-psql.html
- Bazzite container model: https://docs.bazzite.gg/Installing_and_Managing_Software/Containers/
