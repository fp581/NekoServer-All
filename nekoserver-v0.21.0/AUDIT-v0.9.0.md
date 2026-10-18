# NekoServer v0.9.0 — application gateway audit

October 11, 2026. **472 automated tests, 49 release checks and 16 dashboard DOM checks pass.** The release checks include **15 live HTTPS proxy fixture checks**. See GATEWAY.md for deployment and client acceptance. This is software verification, not production hardware acceptance.

## Changes

Independent LAN HTTPS uses a dedicated unprivileged Caddy service, a fixed cat-themed app portal and separate root URLs for Jellyfin, Seerr, Immich and Navidrome. Requests must match the configured RFC1918 IPv4 address and actual LAN client subnet. Its admin API, wildcard bindings, HTTP redirects and public entry points are not enabled. Persistent private CA state is separated from OS code and export is limited to the public root certificate. Startup checks both storage identities and the active LAN address.

Optional private Tailscale Serve uses an explicit owned-route journal and rejects existing unowned routes, malformed/unknown configuration, Funnel and node changes. Partial failures retain pending ownership evidence. Cleanup only happens when live configuration is recognizable; unavailable inspection/removal requires manual review. No global Serve reset is performed.

New Samba configurations support password-protected LAN access with protected read-only shares. Legacy configs remain tailnet-only unless explicitly changed; custom definitions are preserved. The full installer runs media onboarding before the LAN gateway and leaves Tailscale optional. Gateway installation requires application-account confirmation, GET-only Jellyfin/Seerr wizard checks and container/mount/network/binding verification. Immich/Navidrome permissions are operator-attested, not automatically proved.

Acceptance scopes include LAN and remote gateway checks and a real-client witnessed test. Configuration changes invalidate the boot baseline. Public dashboard summaries remain bounded; the dashboard itself stays localhost-only. Existing cat/style assets are unchanged.

## Verification and limits

The new tests cover private IPv4/subnet selection, unsupported/public addresses, application allowlisting, account confirmation, isolated startup imports, full-profile ordering, Caddy configuration restrictions, public-certificate export and no-overwrite behavior, source/Host restrictions, ownership journals, interrupted/repeated Serve configuration, removed routes, unowned route preservation, node changes, custom LAN files, port collisions, state ownership and restart failure. Privileged installer operations and Tailscale are fixtures; chown behavior is mocked because this workspace filesystem cannot model the production ownership changes.

Official Caddy v2.11.7 validates the generated production JSON. A separate real Caddy process was exercised over trusted HTTPS with disposable HTTP/WebSocket upstreams. Checks cover URL/query preservation, HTTPS forwarding headers, 401 responses and bearer credentials, secure cookies, range responses, original and chunked uploads, foreign Host denial, actual-source denial despite spoofed forwarding headers, portal path restrictions and WebSocket frames. Only listener/matcher addresses and ports were adapted to isolated loopback fixtures. Its temporary CA keys were discarded. No Caddy executable or private certificate state is bundled.

Release checks cover all eleven configured profile plans in inspection/dry-run modes, Python/shell/JavaScript syntax, Compose rendering, generated systemd fixture syntax (including the LAN unit), desktop entries, CLI help, dashboard DOM behavior, Caddy validation and proxy evidence. systemd-analyze checks syntax; actual service hardening, package-account ownership, reboot and LAN binding behavior require a real Nyarch installation. Docker/app databases, SMART, Samba clients, Tailscale policy/remote connectivity, native client trust, large uploads and video playback against real apps remain unverified here. The 16 DOM checks simulate interaction, not browser layout rendering.

Photo deletion safety, independent restoration, external backup interruption/reconnect and OS replacement remain acceptance requirements. Bazzite automatic migration remains unsupported. Keep original photos until independently restored copies have been verified.

## Evidence

- audit-evidence/unittest-output.txt — complete automated suite output.
- audit-evidence/checks.json — 49 release checks.
- audit-evidence/gateway-proxy-checks.json — 15 actual HTTPS proxy fixture checks.
- audit-evidence/gateway-caddy-template.json — validated production model with example LAN IP.
- audit-evidence/dashboard-dom-checks.json — 16 simulated interactions.
- audit-evidence/systemd-verify.txt and systemd-fixtures/ — syntax fixtures; do not install them.
- audit-evidence/sha256-manifest.json — packaged file checksums, excluding the manifest itself.

Prior release audit documents remain included as historical records. Their deferred gateway/default networking statements are superseded by this release.

The HTTPS fixture is reproducible with an installed Caddy binary: `NEKO_TEST_CADDY=/path/to/caddy python3 tests/https_proxy_fixture.py`. It uses disposable loopback ports and CA state and rewrites its evidence JSON. It does not start production applications.
