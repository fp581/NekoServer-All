# Console account setup

## Guided setup

If a supported API cannot finish an account, guided setup now opens its UI, repeats instructions on no, and retries authenticated checks on yes. Normal local email-shaped logins do not imply external email verification. See GUIDED-SETUP.md.


The full installer now collects and applies local app credentials. It asks in the terminal before deploying applications:

```text
Admin accounts:
  1 - generate random usernames and passwords for all
  2 - choose per app
```

With option 2, each app asks whether to generate its login or enter it yourself. Custom passwords are typed with hidden input and confirmed a second time. New passwords must be 12-128 characters with no control characters. Usernames use letters, digits, underscores or hyphens and begin with a letter. Immich and Seerr require an email-shaped login instead of a conventional username.

Automatic usernames use Python secrets.token_hex; automatic passwords contain 32 characters generated with secrets.choice (cryptographically secure operating-system randomness). Every app receives its own generated credentials. Generated email logins end in @server.invalid; they are usable local login identifiers but cannot receive password-recovery emails. Choose your actual email manually if you want recovery email later and configure mail in the app.

## What is configured

| App | Account setup | Local URL |
| --- | --- | --- |
| Jellyfin | First administrator and startup completion; automatic router mapping stays off | http://127.0.0.1:8096 |
| Navidrome | First administrator plus a separate non-admin listener with music library access; listener monitor token saved | http://127.0.0.1:4533 |
| Sonarr / Radarr | Forms administrator and authentication required for all addresses; actual password login verified without the API key | :8989 / :7878 |
| Lidarr / Prowlarr | Forms administrator and authentication required for all addresses; actual password login verified | :8686 / :9696 |
| qBittorrent | Managed first-run temporary login replaced by chosen credentials; local/subnet authentication bypass disabled | http://127.0.0.1:8080 |
| Seerr | Initial owner linked to Jellyfin, separate selected email/password for local login, initialization completed | http://127.0.0.1:5055 |
| Immich | First administrator and authenticated role/identity check | http://127.0.0.1:2283 |
| Nextcloud | Selected administrator passed to initial deployment, then password access and administrator role checked | http://127.0.0.1:8088 |

The installer verifies the exact managed Docker project, expected SSD bind and loopback port before sending account credentials. A first-admin operation is allowed only for a recorded fresh app. Once verified, creation is permanently disabled for that saved record. Existing data causes the console to ask for the current login and only authenticate it; no password reset is attempted.

The full profile runs account setup before the media connectors. The existing Jellyfin library connector and qBittorrent/Arr connector reuse verified saved credentials rather than asking you to type them again. Provider/indexer choices and credentials are separate prompts. Navidrome clients use the listener account; keep its administrator for management.

## Your credentials file

The actual installer creates these on your server, not inside this downloadable release:

```text
/srv/nekoserver/secrets/accounts/APP-CREDENTIALS.txt
/srv/nekoserver/secrets/accounts/credentials.json
```

The text file has a heading for each app/account, its local URL, setup status, username and password. JSON is the private resumable source of truth. Both files are root-owned mode 0600 inside a mode-0700 directory. Passwords are never echoed in the installer log or public dashboard. View the inventory locally with:

```bash
sudo less /srv/nekoserver/secrets/accounts/APP-CREDENTIALS.txt
```

This is an unencrypted plaintext inventory requested for setup. Keep any offline copy private. PLANNED, STARTED and NEEDS_SETUP are not successful logins; only VERIFIED entries have passed their checks. Reruns preserve passwords and regenerate a missing text export from the saved JSON. They do not rotate passwords. The inventory records choices known to this installer; it does not watch password changes made later in app interfaces.

The private inventory is included, encrypted, in monthly HDD snapshots when it exists (it opens with the backup password chosen during setup; see MONTHLY-MAINTENANCE.md). It is never written to the USB drive. Keep an independent secure copy before relying on the server's restoration. The release ZIP contains no credentials generated on your future server.

## Install and resume

Account setup is part of the setup wizard. Run it, or run it again to continue:

```bash
sudo bash install.sh
```

The media, photos, cloud and music profiles include their corresponding account steps. Advanced --module deployments remain component operations; use the full/named profiles or the standalone account helper for onboarding. Read-only check/dry-run modes never prompt for, create or print passwords. A noninteractive fresh run remains needs_setup until you choose credentials in a terminal.

For already deployed applications, use the current release helper with your confirmed config:

```bash
sudo python3 account-setup.py --apply --config /absolute/path/to/confirmed-config.json
```

It selects deployed app manifests. If an imported/existing login fails, rerunning interactively offers ENTER to retry saved credentials or c to enter the current login. Correcting that private record does not reset the account's password in the app. Fresh in-progress records are retained so interrupted first-run setup can authenticate and resume without losing its original generated password.

Older saved configurations do not silently gain this feature or new profile entries. For a new server, use this release's sample `config.json` as the starting point. For existing deployments, use the standalone helper with their confirmed config; do not replace disk/network settings with a fresh sample. This release is still not a general migration or password-rotation tool.

## What still asks for you

Actual device/account choices remain: filesystem UUID confirmation, content sources and provider credentials, LAN/Tailscale policy and certificate trust, phone permissions/albums, Steam/Tailscale external sign-ins and witnessed transfer/playback/restore checks. Caddy/dashboard and internal databases do not each get a new human administrator login from this prompt; their existing service-secret/access policies remain. Existing Samba/system users keep their separate host setup flow.

Application APIs can change. An unsupported response, failed login or wrong role stays pending, and saved credentials are retained. Protocol/command fixtures are not a live Docker/hardware acceptance pass.

Primary API/source references: https://github.com/jellyfin/jellyfin/blob/master/Jellyfin.Api/Controllers/StartupController.cs ; https://github.com/immich-app/immich/blob/main/server/src/controllers/auth.controller.ts ; https://github.com/navidrome/navidrome/blob/master/server/auth.go ; https://github.com/seerr-team/seerr/blob/develop/seerr-api.yml ; https://github.com/Sonarr/Sonarr/blob/develop/src/Sonarr.Api.V3/Config/HostConfigController.cs ; https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-%28qBittorrent-5.0%29
