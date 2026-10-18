# NekoServer v0.12.0 audit

This release adds background health notifications. 583 Python tests, 15 Rust unit tests and 72 release checks passed. Exact outcomes are in audit-evidence; 18 Python process/setup tests and 8 Rust unit tests cover the new notification phase; the Python tests include real curl against local HTTPS. retained earlier measurements are labelled historical.

## Changes

The scheduled worker is Rust. Python handles installation and private destination setup. The worker publishes fixed sanitized ntfy messages through curl with TLS verification, HTTPS-only transport, disabled redirects/default curl config/proxy environment, bounded execution and root-only temporary credentials. No live external notification was sent during implementation.

Durable OS-local state tracks debounce, delivered severity, grouped recovery, repeat suppression and delivery retry backoff. Wrong/missing SSD alerts do not depend on that SSD for the executable, credentials or state. Stale monitoring and unknown observations cannot falsely resolve previously delivered problems. A successful explicit test records provider acceptance without clearing active problems. Destination changes reset delivery deduplication.

The installer preserves an existing private destination/token, reports needs_setup when missing, installs the native timer and keeps secrets out of systemd arguments and installer config. Dashboard collectors publish restricted delivery status. Acceptance retains a separate phone receipt witness.

## Verification and limits

Current Python and Rust results and release checks are bundled. Native notification process tests cover real files, private permissions, grouping, repeat suppression, retry failure/recovery, stale/unknown observations, destination reset, invalid URLs/header tokens and explicit test outcomes. A real curl test uses a local HTTPS endpoint to verify bearer authentication, POST content, redirect rejection and failure on an untrusted certificate. Hardware/mount/app command fixtures remain simulations.

There is no real ntfy account, phone or Docker/systemd server here. Actual provider access, phone permissions/receipt/latency, boot behavior, missing-drive service operation and original server restore acceptance remain required. A powered-off PC cannot notify about its own outage. Email and independent remote heartbeat monitoring are not implemented. A crash after provider acceptance may duplicate a message. OS replacement requires recovery of private notification credentials/state separately. This release does not add VM/cloud/Steam/Samba backup features or declare the entire original plan complete.
