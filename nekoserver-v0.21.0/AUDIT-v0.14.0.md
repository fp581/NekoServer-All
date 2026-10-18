# NekoServer v0.14.0 audit

648 Python tests, 21 Rust unit tests and 77 release checks passed. Exact current results are in audit-evidence.

New Rust host/user snapshot worker preserves selected host settings, installer bundles, Steam data and managed standalone Samba state. Explicit extra folders cover custom saves and share contents. Snapshots are private and immutable, with file checksums, bounded inventories, space preflight, repeat drive guards and change checks. Symlinks remain inert text. Durable resume journal, normal failure cleanup, systemd ExecStopPost and boot recovery restore only previously active Samba. Busy/unknown process probes, unsupported/unmanaged writers and failed restarts cannot record success.

Python host recovery verifies inventories/checksums and extracts only into a new directory. No live OS/user/Samba files or services are automatically replaced/activated. Optional absent sources are reported and cannot be taken as fully protected Steam/Samba data.

Fixtures cover actual native file copying/staged restore with mocked mount/process/service commands, failure paths and retained journals. No production services or hardware were exercised. Real OS recovery, game-save loads, Samba login, process/ACL/metadata behavior and independent restore remain deployment acceptance. Shared-content extras are stable-file snapshots, not transactional application backups. VM support, personal cloud, Bazzite migration and independent outage detection remain outside this phase. No automatic deletion or independent snapshot copy is introduced.
