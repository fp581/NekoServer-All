# Private app gateways

User services: Jellyfin (8443), Seerr (8444), Immich (8445), Navidrome (8446), Nextcloud (8447), and the read-only dashboard (8448, Tailscale only; `"remote": false` under `"dashboard"` turns it off). Backends stay on localhost; management UIs (Sonarr, Radarr, Lidarr, Prowlarr, qBittorrent) are not in the private remote Serve routes.

## Home LAN

Apply `lan_access` or the base `full` profile using the confirmed config. Select the physical home interface/address/subnet. The interactive installer opens selected local apps, explains login/signup/sharing checks, asks yes/no and rechecks supported managed accounts before recording permission to enable their routes.

The LAN Caddy gateway uses a private certificate authority. Clients using its LAN URLs must explicitly trust the exported public root certificate; never transfer the private key. Export instructions are in gateway_cert.py --help. The home portal uses port 8442.

**Check each device before relying on the LAN URLs.** Phones and especially TV apps often ignore certificate authorities a user installs: Android apps do so by default, and many smart-TV, Roku and Fire TV apps offer no way to add one. Test the Jellyfin, Immich and Nextcloud apps on each device you plan to use. If an app refuses the LAN certificate, use the Tailscale route below (publicly trusted certificates), or the server's own TV session. A router DHCP reservation should keep the chosen LAN address stable.

## Away from home

Tailscale is part of the main installation (`sudo bash install.sh`). After the apps are set up, the wizard pauses to connect this server to your Tailscale account, turn on secure addresses (MagicDNS and HTTPS), create a private address for each everyday app (Jellyfin, Seerr, Immich, Navidrome, Nextcloud), and walk you through each app on your phone with a QR-code page. These addresses are private to devices on your Tailscale account: Tailscale Serve, never Funnel, and no router port forwarding. Sonarr, Radarr, Lidarr, Prowlarr and qBittorrent stay reachable only on the server. To add or redo just this part later: `sudo bash install.sh --profile remote_access --apply --config my-config.json`. Exact steps: GUIDED-SETUP.md.

Generated URLs are in `/srv/nekoserver/reports/gateway-tailnet.json`, under `links`. Use the same complete https://...ts.net:port/ URLs at home and away with Tailscale connected. The phone needs the official Tailscale app, authorized VPN configuration, membership/access in the same tailnet, and each server app's own credentials. Test with Wi-Fi off and mobile data on.

Tailscale's publicly trusted Serve HTTPS path does not need the LAN Caddy CA on the phone. It publishes only to permitted tailnet users/devices; it is not Funnel. No router port forwarding, public IP, exit node or subnet router is required for these five routes. Do not reset unrelated Serve routes to bypass an ownership conflict.

Tailscale authorizes the device connection; it is not a shared app login, backup destination or photo/music player. Both the home server and phone need internet. The server must remain powered on with its storage available. Reauthentication/expiry and tailnet policy still need administration.

Nextcloud answers on every gateway address at once: each gateway only adds its address to Nextcloud's trusted domains, and links follow whichever address the client used. (Before v0.21.1 the gateway applied last replaced the other's address, so applying remote_access broke Nextcloud on the home network for devices without Tailscale.) The first gateway's address is used for links in background emails. The Docker hop between the gateway and Nextcloud is a trusted proxy, so Nextcloud sees each client's real address for login throttling and logs. Phone tests are recorded as user-confirmed evidence, separate from automatic server route checks and full acceptance.
