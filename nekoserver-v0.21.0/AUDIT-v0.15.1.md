# NekoServer v0.15.1 audit

690 Python tests, 25 Rust unit tests and 93 release checks passed. Exact current results are in audit-evidence.

See INSTALLATION-REVIEW.md for the complete step-by-step audit and fixes. Real installer modules execute in a redirected filesystem with simulated mount/package/systemd/Docker/ownership operations; deployed native guards and official Compose parsing execute as subprocesses. The current upstream Immich manifest is checked. No physical server installation or live app/restore acceptance is claimed. Existing supervised Immich recovery, latest-tag media image compatibility, private endpoint/account choices and independent backup scope remain explicitly documented limits.
