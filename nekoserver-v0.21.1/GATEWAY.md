# Private app gateways

User services: Jellyfin (8443), Seerr (8444), Immich (8445), Navidrome (8446), Nextcloud (8447). Backends stay on localhost; management UIs and the dashboard are not in the private remote Serve routes.

## Home LAN

Apply `lan_access` or the base `full` profile using the confirmed config. Select the physical home interface/address/subnet. The interactive installer opens selected local apps, explains login/signup/sharing checks, asks yes/no and rechecks supported managed accounts before recording permission to enable their routes.

The LAN Caddy gateway uses a private certificate authority. Clients using its LAN URLs must explicitly trust the exported public root certificate; never transfer the private key. Export instructions are in gateway_cert.py --help. The home portal uses port 8442. A router DHCP reservation should keep the chosen LAN address stable.

## Away from home

After the base apps work, apply the `remote_access` profile. It now opens and guides Tailscale authorization, DNS/HTTPS settings and phone setup in one interactive run. No reopens the step and instructions; yes also triggers available checks. Exact commands and fallback behavior: GUIDED-SETUP.md.

Generated URLs are in `/srv/nekoserver/reports/gateway-tailnet.json`, under `links`. Use the same complete https://...ts.net:port/ URLs at home and away with Tailscale connected. The phone needs the official Tailscale app, authorized VPN configuration, membership/access in the same tailnet, and each server app's own credentials. Test with Wi-Fi off and mobile data on.

Tailscale's publicly trusted Serve HTTPS path does not need the LAN Caddy CA on the phone. It publishes only to permitted tailnet users/devices; it is not Funnel. No router port forwarding, public IP, exit node or subnet router is required for these five routes. Do not reset unrelated Serve routes to bypass an ownership conflict.

Tailscale authorizes the device connection; it is not a shared app login, backup destination or photo/music player. Both the home server and phone need internet. The server must remain powered on with its storage available. Reauthentication/expiry and tailnet policy still need administration.

Nextcloud uses the gateway applied last as its primary origin. After rerunning the full profile, reapply remote_access if the .ts.net URL should be primary. Phone tests are recorded as user-confirmed evidence, separate from automatic server route checks and full acceptance.
