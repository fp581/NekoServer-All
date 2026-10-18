# NekoServer v0.8.1 — deployment acceptance audit

October 8, 2026. **423 automated tests pass**, including 42 new acceptance tests. **39 release checks and 16 simulated dashboard DOM checks pass.** The prior dashboard/recovery audits remain included.

## Implementation and scope

acceptance.py inspects the chosen deployment profile's dependency closure. It validates configuration, supported recovery host, matching distinct mounts, saved-baseline/later-boot evidence, current preserved recovery bundle, Docker classic SSD store, currently loaded startup guard and checkpoint, selected container health and exact bind destinations, protected read-only media sources, expected localhost port bindings, current authenticated GET-only media configuration, successful backup jobs/active timers, external drive identity, live dashboard age/services, Tailscale backend/Samba service state and TV launcher hashes.

Missing/wrong drives block private reads and dependent Docker/media inspection. Report saving reruns the UUID guard and writes a private file only to the application SSD. Default invocation does not write. No packages, service restarts, reboot, download/import, API mutation, database restore or production cutover are performed. --manual requires --save and an interactive terminal; only exact PASS/FAIL test identifiers record user-reported evidence. Manual results do not carry across new reports and cannot override automatic failures. Reboot evidence requires a valid earlier verified baseline with matching profile/configuration and later boot; a same-boot run cannot pass.

acceptance_summary.py exports a bounded allowlisted dashboard view. Counts are recomputed; raw responses, credentials, exception text, boot/run IDs and arbitrary private descriptions are excluded. Empty/malformed/unknown manual checks, invalid timestamps and unrecorded witness metadata cannot show complete. Release/storage/service-selection mismatch marks outdated; reports older than 24 hours are stale. A scoped completed checklist is explicitly described as including user-reported manual tests. The existing restore acceptance warning remains visible. The cat/vector design is retained; this adds a small acceptance summary to the existing recovery view.

## Verification

New tests cover first/same/later boot evidence, invalid baseline and changed scope/configuration, observed reboot retention, profile-scoped inspection, invalid configuration/profile, wrong/blank drive identity, blocked private reads, pre-save UUID recheck, 0600 reports, exact manual confirmation/failure/pending semantics, no automatic-failure override, no inherited manual passes, root read-only CLI behavior, interactive-only recording, Docker wrong root/containerd rejection, secret redaction, exact mount destinations and read-only sources, public-port rejection, unhealthy/duplicate containers, loaded guard with non-ignored command, stale endpoint and disabled/inactive services, recovery preservation, private-to-public summary sanitization/age/version/scope/count/witness validation and wrong-SSD dashboard reads.

Release checks retain the eight profile plan validations, compiled Python/shell/JavaScript checks, eight rendered Compose templates, generated systemd fixture syntax verification, TV launcher parsing, recovery/dashboard CLI help and DOM checks, plus acceptance CLI help. The 16 jsdom checks exercise the actual scripts including the new pending-profile counts and outdated-report display. They simulate DOM behavior; actual browser rendering, native permissions and TV hardware are not tested here.

A real read-only dashboard-profile inspection in this workspace correctly reported missing configuration/server services and pending boot/manual tests; no --save or installation was performed. This is negative-path execution evidence, not a successful server acceptance run. Systemd/Docker/SMART/TV/restore operations in the automated suite remain fixtures; no actual server hardware or independently restored photo library was available.

Hardware compatibility, actual Nyarch deployment/reboot, SMART sensors, media transfers/playback, phone deletion safety, full independent restoration, disconnected-drive protection, remote client permissions, controller/HDMI and supported OS recovery still require the checklist exercise. Bazzite migration and remote app gateway remain deferred. Do not delete original photos until independent recovery succeeds.

## Evidence

- audit-evidence/unittest-output.txt — 423 tests.
- audit-evidence/checks.json — 39 release checks.
- audit-evidence/dashboard-dom-checks.json — 16 DOM simulations.
- audit-evidence/acceptance-workspace-check.txt — expected incomplete workspace inspection.
- audit-evidence/systemd-verify.txt and systemd-fixtures/ — test-path syntax evidence; do not install those fixtures.
- audit-evidence/sha256-manifest.json — release contents, excluding the manifest itself.
