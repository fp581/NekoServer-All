# NekoServer v0.15.0 audit

661 Python tests, 22 Rust unit tests and 81 release checks passed. Exact current results are in audit-evidence.

Adds a Rust OS-local HTTPS heartbeat worker and bounded timer, private interactive endpoint setup, sanitized dashboard status and automatic/witnessed acceptance. Secret URLs stay out of public snapshots and process arguments. Curl uses validated HTTPS, system CA trust, no redirects/proxy or user curl configuration, bounded timeouts and no application payload. Failures do not advance last success. Host snapshots preserve the private endpoint configuration.

Local native fixtures check status/privacy and HTTP error handling. Real local HTTPS tests verify certificate validation and redirect refusal. No real external provider account was configured or contacted, and no outside phone/email outage alert was witnessed. Users must set provider period/grace/recipient and complete the missing-ping/recovery exercise. The worker proves its own reachability, not health of every app or drive. VM/cloud/Bazzite support remain unfinished.
