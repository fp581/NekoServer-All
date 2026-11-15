# Your app addresses, at home and away

Every app has two addresses, and both stay private:

- **With Tailscale** (anywhere, at home too): `https://yourserver.tailXXXX.ts.net:8443` and so on, on every device signed in to your Tailscale account. These have ordinary certificates, so no browser or app warns. This is the way to use on phones.
- **At home without Tailscale**: `https://192.168.1.20:8443` and so on, with your server's home address. The app menu at port 8442 lists them all. These work only on your home network.

Nothing is opened to the internet, and your router needs no changes.

## The warning at the home address

The first time you open a home address, the browser warns that the connection "is not private". That is expected: for the home addresses your server makes its own certificate (the proof a site gives that it is what it says), and your browser has never heard of the one who signed it, which is your server itself.

- **Simplest:** in the browser, choose *Advanced* and then *Proceed* (or *Accept the risk and continue*). The connection is encrypted all the same. You do it once per device and app.
- **To make the warning go away**, give the device your server's public certificate:
  1. On the server, in the NekoServer folder, write it to a file: `sudo python3 gateway_cert.py --export ~/nekoserver-root.crt` (it refuses to replace a file that is already there). It also shows the certificate's fingerprint, a long code to compare if you want to be sure the file is the right one.
  2. Copy that file to the device (a USB stick, or the shared folders).
  3. Install it there as a trusted certificate authority. Windows: double-click the file, *Install Certificate*, *Local Machine*, *Trusted Root Certification Authorities*. macOS: double-click it, then in Keychain Access open it and set *Trust* to *Always Trust*. iPhone and iPad: open the file, install the profile in Settings, then switch it on under *General*, *About*, *Certificate Trust Settings*. Android: Settings, *Security*, *Install a certificate*, *CA certificate*. Firefox keeps its own list: Settings, *Privacy & Security*, *View Certificates*, *Import*.

  The file holds no secret: it only lets a device recognise your server. Its private half never leaves the server.
- **Phone and TV apps often refuse** a certificate you installed yourself (Android apps do by default, and many TV apps have no way to add one). When an app won't connect at the home address, use its Tailscale address instead.

## On a TV

TV apps can't be given your server's certificate, so a TV uses the Tailscale addresses, and Tailscale runs only on some TVs:

- **Android TV and Google TV** (Sony, Philips, TCL and others, and the Google TV Streamer or Chromecast with Google TV) and **Apple TV** (tvOS 17 or later): install Tailscale from the TV's app store and sign in to your Tailscale account, then install Jellyfin (on Apple TV: Swiftfin or Infuse) and enter the Jellyfin address ending in `:8443`.
- **Fire TV:** Tailscale is not in Amazon's app store; it is installed from Tailscale's own site (https://tailscale.com/docs/install/amazon-fire), which takes some fiddling, and the newest sticks (Vega OS, such as the Fire TV Stick 4K Select) cannot run it.
- **LG (webOS), Samsung (Tizen) and Roku TVs** have no Tailscale app, so their Jellyfin app cannot reach your server. Plug in a box or stick that has one (Google TV Streamer, Apple TV), or connect the server to the TV with an HDMI cable ([TV-SETUP.md](TV-SETUP.md)).
- **Casting from the phone** (the cast button in the Jellyfin app) does not work: the Chromecast or TV then fetches the film itself and cannot reach the private address. Use the Jellyfin app on the TV or stick instead.

---

## For administrators

The rest of this page is the technical reference.

User services: Jellyfin (8443), Seerr (8444), Immich (8445), Navidrome (8446), Nextcloud (8447), and the dashboard (8448, Tailscale only: it shows everything, and changes fans, lights and apps only with your PIN; `"remote": false` under `"dashboard"` turns it off). Backends stay on localhost; management UIs (Sonarr, Radarr, Lidarr, Prowlarr, qBittorrent) are not in the private remote Serve routes. On the home network, a port that another program on the server already uses is replaced by a free one from 8450-8499; the choice is kept in `"lan_ports"` under `"gateway"` in your settings and the app menu page links each app with its port.

### Home LAN

Apply `lan_access` or the base `full` profile using the confirmed config. Setup finds the home network connection by itself (a cable before Wi-Fi) and says which it uses; `"lan_ip"` and `"lan_cidr"` under `"gateway"` name another. The interactive installer opens selected local apps, explains login/signup/sharing checks, asks yes/no and rechecks supported managed accounts before recording permission to enable their routes.

The LAN Caddy gateway uses a private certificate authority. Clients using its LAN URLs must explicitly trust the exported public root certificate; never transfer the private key. `sudo python3 gateway_cert.py --export FILE` writes that certificate ("The warning at the home address", above). The home portal uses port 8442.

**Check each device before relying on the LAN URLs.** Phones and especially TV apps often ignore certificate authorities a user installs: Android apps do so by default, and many smart-TV, Roku and Fire TV apps offer no way to add one. Test the Jellyfin, Immich and Nextcloud apps on each device you plan to use. If an app refuses the LAN certificate, use the Tailscale route below (publicly trusted certificates), or the server's own TV session. Tailscale has clients for Android TV/Google TV, Apple TV and (sideloaded, not Vega OS) Fire TV only; LG webOS, Samsung Tizen and Roku TVs have none and need a streaming stick or the TV session ("On a TV", above). Casting to a Chromecast fails on both routes: the receiver fetches the stream itself and neither trusts the private CA nor joins the tailnet. A router DHCP reservation should keep the chosen LAN address stable.

### Away from home

Tailscale is part of the main installation (`sudo bash install.sh`). After the apps are set up, the wizard pauses to connect this server to your Tailscale account, turn on secure addresses (MagicDNS and HTTPS), create a private address for each everyday app (Jellyfin, Seerr, Immich, Navidrome, Nextcloud) and for the dashboard, and walk you through each app on your phone with a QR-code page. Each app's phone step shows the command that puts its password on the PC's screen, with a QR code (`account-setup.py show APP`; see GUIDED-SETUP.md). These addresses are private to devices on your Tailscale account: Tailscale Serve, never Funnel, and no router port forwarding. Sonarr, Radarr, Lidarr, Prowlarr and qBittorrent stay reachable only on the server. To add or redo just this part later: `sudo bash install.sh --profile remote_access --apply --config my-config.json`. Exact steps: GUIDED-SETUP.md.

Generated URLs are in `/srv/nekoserver/reports/gateway-tailnet.json`, under `links`. Use the same complete https://...ts.net:port/ URLs at home and away with Tailscale connected. The phone needs the official Tailscale app, authorized VPN configuration, membership/access in the same tailnet, and each server app's own credentials. Test with Wi-Fi off and mobile data on.

Tailscale's publicly trusted Serve HTTPS path does not need the LAN Caddy CA on the phone. It publishes only to permitted tailnet users/devices; it is not Funnel. No router port forwarding, public IP, exit node or subnet router is required for these six routes (five apps and the dashboard). Do not reset unrelated Serve routes to bypass an ownership conflict.

Tailscale authorizes the device connection; it is not a shared app login, backup destination or photo/music player. Both the home server and phone need internet. The server must remain powered on with its storage available. **Keep the server signed in:** Tailscale signs a device out after 180 days unless key expiry is switched off for it. In the Tailscale admin console (Machines), open this server's menu and choose **Disable key expiry** (setup says so too). The dashboard warns two weeks before a sign-in runs out, and says so at once when the server is signed out (run `sudo tailscale up` to sign in again). Tailnet policy (who may reach the server) is yours to set.

Nextcloud answers on every gateway address at once: each gateway only adds its address to Nextcloud's trusted domains, and links follow whichever address the client used. (Before v0.21.1 the gateway applied last replaced the other's address, so applying remote_access broke Nextcloud on the home network for devices without Tailscale.) The first gateway's address is used for links in background emails. The Docker hop between the gateway and Nextcloud is a trusted proxy, so Nextcloud sees each client's real address for login throttling and logs. Phone tests are recorded as user-confirmed evidence, separate from automatic server route checks and full acceptance.
