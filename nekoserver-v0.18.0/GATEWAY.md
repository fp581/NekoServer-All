# NekoServer application access — v0.9.0

Home access and away-from-home access are independent. The LAN gateway uses Caddy HTTPS on a selected private IPv4 address; it needs no Tailscale account. Optional remote_access uses private Tailscale Serve HTTPS. Neither module enables Funnel or configures router port forwarding. Individual application accounts remain the authentication boundary; this release does not add shared sign-on.

| App | LAN / Tailscale HTTPS port | Existing loopback backend |
| --- | --- | --- |
| Local app portal | 8442 (LAN only) | Fixed portal assets |
| Jellyfin | 8443 | 8096 |
| Seerr | 8444 | 5055 |
| Immich | 8445 | 2283 |
| Navidrome | 8446 | 4533 |

Use the complete HTTPS URL, including its port. Each app uses its own root URL. The four app names are the only allowed gateway selections. Sonarr, Radarr, Lidarr, Prowlarr, qBittorrent and the monitoring dashboard stay outside these gateways. Docker web bindings remain localhost-only. The local portal keeps the existing cat and purple theme; it contains links rather than private monitoring data. Tailscale provides individual app URLs, not a remote portal.

## Deploy LAN access

Deploy the media/photo services first and finish their initial account setup on the server. Keep your actual configured UUIDs and paths when upgrading; do not replace them with the ZIP defaults.

```bash
sudo bash install.sh --profile lan_access --dry-run
sudo bash install.sh --profile lan_access --apply
```

The access profile installs storage/recovery prerequisites, not the application containers. The full profile deploys apps and runs onboarding before gateway_lan. It includes LAN Samba and does not require Tailscale. network and networking are aliases for LAN gateway plus Samba in the new defaults. Existing user profiles are not rewritten automatically.

The interactive installer lists detected private IPv4 interfaces and asks which home subnet to use. Reserve that IP in your router so it remains stable. Alternatively set gateway.lan_ip and gateway.lan_cidr explicitly in the actual config. The supported range is RFC1918 IPv4, with a /16 through /30 subnet; loopback, public, wildcard, IPv6 and Tailscale addresses are rejected. Interface detection excludes common VPN/container interface names, but you must select the intended network. Neither firewall rules nor router settings are changed. If the host firewall blocks these ports, review its rules for your selected home subnet.

Choose gateway.services from jellyfin, seerr, immich and navidrome. Create administrator accounts, disable unwanted signup and check user permissions. The interactive prompt requires SECURED; unattended deployments require confirmed_services to exactly match services. Jellyfin and Seerr wizard-completion endpoints are checked automatically. Immich/Navidrome account and permission setup rely on your explicit confirmation. Container health, persistent mounts, media networks and loopback bindings are checked before exposure. Changing selected apps requires confirmation again.

Caddy is installed through the Arch package manager if absent. NekoServer uses its own service/configuration, leaving an existing default Caddy service alone. Conflicting ports or unmanaged/edited NekoServer gateway files stop setup for review. Its listeners bind only the selected IP and check the actual client subnet and Host header; forwarded address headers cannot grant LAN access. HTTP redirects and the Caddy admin API are disabled. The portal serves only its index page.

## Trust the LAN certificate

The private Caddy CA lives on the application SSD, under gateway/caddy-state. The dedicated caddy account owns its private state. No automatic system/client trust import is performed.

```bash
# Run from the current preserved release with the actual config.
sudo python3 gateway_cert.py --export /tmp/neko-lan-root.crt
```

This exports only the public root certificate and prints its SHA-256 DER fingerprint. It refuses to overwrite a destination or export private-key material. Transfer the certificate through a trusted channel, verify the fingerprint, and install/trust it on each intended device using that device's certificate settings. Test native app trust as well as the browser; client support can differ. Do not disable certificate verification to make a client work.

Open the reported portal URL, such as https://192.168.1.20:8442/. Configure phone/TV applications with the appropriate full app URL. Review application external/base URL settings if those clients need them; the installer does not mutate every application's URL or permissions. HTTPS alone does not prove authentication or playback correctness.

Persist and independently protect the CA state if you want to retain client trust after OS replacement. It includes private keys. An unexpected existing UID/GID stops setup rather than silently adopting/chowning old state. Restoring that ownership needs an explicit administrator review. Missing/wrong data drives or a missing selected LAN address block service startup through a storage/network guard. Reapply after an intentional address change.

## Optional remote access

```bash
sudo bash install.sh --profile remote_access --dry-run
sudo bash install.sh --profile remote_access --apply
```

Authenticate the server and intended clients with Tailscale. Enable the required MagicDNS/HTTPS capability for your tailnet and restrict its access policy to intended users/devices. Follow any initial Tailscale account/HTTPS authorization locally. NekoServer uses private Serve routes with --bg so their configuration persists; it never enables public Funnel or resets the entire Serve configuration. HTTPS certificate naming uses the node's .ts.net DNS name; review Tailscale's certificate/privacy documentation before enabling HTTPS.

Existing unowned Serve routes, Funnel, unknown/malformed configuration or node-identity changes stop setup without adopting them. The SSD ownership journal records pending changes before route mutations. Reruns handle only recognizable NekoServer-owned routes. On a failure, recognizable owned routes are disabled when possible. If route inspection or removal fails, the pending journal remains and the live outcome requires review; unconditional rollback is not promised. Do not make concurrent manual Serve changes during an installer run.

Private reports/gateway-tailnet.json contains the individual app URLs. Test from outside home, including a client/user that policy should deny. Each app still needs its own login. Native photos/uploads, streamed media and WebSocket behavior must be verified on real clients.

## Samba and acceptance

New configs use networking.share_mode=lan. Samba binds the selected LAN IP and loopback, restricts hosts to the selected subnet, denies guests and keeps protected shares read-only by default. LAN access needs Samba credentials. It is independent of the HTTPS app gateway.

Older configs lacking share_mode retain tailnet behavior. Set share_mode=tailnet to keep Tailscale-only file sharing and run/authenticate tailscale first. An existing managed Samba definition is not silently widened. A recognized generated tailnet configuration can be deliberately switched using networking.allow_tailnet_to_lan=true; custom or edited configurations are preserved for review.

```bash
sudo python3 acceptance.py --profile lan_access --save
sudo python3 acceptance.py --profile remote_access --save
# After performing the displayed client tests:
sudo python3 acceptance.py --profile lan_access --save --manual
```

Run the full acceptance scope too when accepting the whole installation. Automatic gateway checks inspect managed configuration/ownership and trusted transport; they do not prove native login, access permissions, original uploads, range playback or a working remote network. The witnessed gateway_clients checklist covers those real clients. Reboot evidence still requires a saved baseline and later boot. Keep original photos until the independent full restore succeeds.

To disable LAN exposure: systemctl disable --now nekoserver-gateway-lan.service. For remote exposure, inspect tailscale serve status --json and the ownership journal, then disable only your owned HTTPS ports with tailscale serve --bg --https=PORT off. Do not use a global Serve reset to remove NekoServer routes. Backend containers remain unchanged.

References: [Caddy internal TLS](https://caddyserver.com/docs/caddyfile/directives/tls), [Caddy reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy), [Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve), [Tailscale HTTPS](https://tailscale.com/kb/1153/enabling-https).
