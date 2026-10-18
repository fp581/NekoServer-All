# NekoServer v0.13.0 audit

598 Python tests, 18 Rust unit tests and 76 release checks passed. Exact outcomes are in audit-evidence. Retained earlier measurements are labelled historical.

## Changes

The new Python server_os module blocks standard systemd sleep/hibernation paths, installs managed sleep/logind policy and bounded journald disk/runtime-file retention. Custom target units and owned-path conflicts are preserved. Display blanking and normal desktop sessions remain available; logind is not restarted during installation.

A small native Rust boot job verifies both UUID/filesystem identities, resolves the media HDD parent device, refuses unsupported transports/SSDs/mapped devices and rechecks the source before issuing only hdparm -S 0 to that disk. Setup installs hdparm through pacman where available, reports unsupported/failed hardware setup separately and reexecutes the job on reapply. No drive formatting, blanket settings, periodic dummy I/O or firmware/parking commands are added.

Collectors and background notifications now report genuinely low available RAM using the existing MemAvailable-based calculation. No cache eviction, swap rewriting, arbitrary RAM caps or leak-hiding reboot/restart schedule is installed. Acceptance inspects live masks, owned drop-ins and native HDD service, with separate idle/reboot/HDD hardware witnessing.

## Verification and limits

Current Python/Rust results, source/binary hashes and generated service/profile checks are bundled. Native HDD process tests mock mount/device commands, verify the exact selected parent disk and reject wrong UUID, USB/SSD/mapped devices, a changed source and command failure. OS module fixtures check sleep masks, bounded journal, custom-file preservation, private guard and repeat application. Memory tests verify cache-aware accounting and pressure alerts.

No production OS settings or real HDD commands were applied in this workspace. Real systemd/desktop idle behavior, firmware/controller compliance, SATA model compatibility, physical spindle behavior and long-running app memory usage remain server acceptance tasks. Standard sleep blocking cannot prevent physical power failure or privileged direct kernel power operations. OS journal limits do not bound arbitrary app/Docker logs. Disabling an earlier HDD service does not restore a previously changed hardware timer. Full original-plan readiness remains incomplete.
