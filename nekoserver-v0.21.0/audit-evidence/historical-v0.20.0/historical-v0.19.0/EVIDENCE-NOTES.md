# Evidence scope - v0.19.0

Current: unittest-output.txt (763 passed), native-build.txt (32 Rust tests, formatting, Clippy with warnings denied, production and transport-fixture static builds), checks.json (39 source/shell/profile/native-integrity checks), python-static-check.json and installation-simulation.json. Official Compose and Immich provenance is retained and matches the fetched fixture bytes.

Five shell bootstrap tests use a fully isolated command PATH and fake pacman: missing Python/tools installed before launch, repository failure stops setup, read-only modes never install, contradictory modes stop and arguments reach Python. Three new managed-engine Compose tests cover an existing plugin, missing-plugin installation/reverification and disabled installation. No real package manager is invoked by these tests.

The full suite executes real files, tar/checksums, local HTTP, Rust workers and the official Compose parser. Host mount/package/systemd/Docker/ownership identities are simulated. The install simulation runs actual foundation/media/Immich/photo jobs/dashboard modules with redirected files, reruns and refusal cases; it is not an end-to-end live deployment of every application. Nextcloud and monthly recovery have their own executable fixtures.

No full live VM or intended hardware install, physical disk standby/wake, real application account/client playback, live engine upgrade/rollback or independent hardware disaster acceptance is claimed. Those remain in the illustrated guide checklist. Files under historical-v0.18.0 are retained earlier evidence, not measurements rerun for this release. No current performance benchmark or graphical dashboard browser pass is asserted.
