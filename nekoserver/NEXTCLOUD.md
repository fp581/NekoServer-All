# Personal cloud

Nextcloud is included in the full profile and has its own cloud profile. No VM is required. It runs as a four-container Compose project: Apache/Nextcloud, PostgreSQL 17, Redis 7 and the upstream cron worker. It uses the upstream Nextcloud Docker image, rather than the All-in-One controller, to keep ownership of storage and the existing NekoServer gateway clear.

- SSD: appdata/nextcloud/html (application/configuration/custom apps), appdata/nextcloud/postgres and compose/nextcloud.
- HDD: cloud/files, mounted as Nextcloud's data directory. Do not modify this directory through Samba or edit its files directly.
- Local setup: http://127.0.0.1:8088. Only the app publishes a host port, bound to loopback.
- First deployment resolves a stable Nextcloud release and saves its major Apache tag, e.g. nextcloud:35-apache. Monthly updates follow that major stream; they do not skip major releases.
- **Your login.** Setup creates the Nextcloud administrator for you (the name you chose, or one it made up) and writes the name and password into your credentials file. To see it on the screen (with a QR code for your phone): `sudo python3 /srv/nekoserver/installer/releases/v0.41.3/account-setup.py show nextcloud` (see ACCOUNT-SETUP.md). The installer never prints the password or puts it in the dashboard. Set account recovery/email and MFA in Nextcloud if you like.
- **Two-factor sign-in (MFA) is fine.** Setup checks the administrator with Nextcloud's own user list (`occ user:info`: the account exists, is switched on and is an administrator), then tries the saved password. Once two-factor sign-in is on for that account (or required for everyone), Nextcloud refuses any plain password from a program like NekoServer, so the password can't be tried any more: setup then says *two-factor sign-in is on* and counts the account as checked, and the steps after it (hardware video, say) are not held up. If you want the password tried again, create an *app password* in Nextcloud (Settings, Security, *Create new app password*) and save it with `sudo python3 account-setup.py --apply --config my-config.json`, choice **c**. Your own password stays your sign-in for the website and the apps.
- **Changing that password** in Nextcloud is fine, but tell setup too, or every later `sudo bash install.sh` stops at the Nextcloud account check. In the NekoServer folder (where `install.sh` is) run `sudo python3 account-setup.py --apply --config my-config.json`: it finds that the saved login no longer works. Run it once more: it now asks about Nextcloud; press **c** and type the new password. (With account setup switched off, `accounts.enabled: false`, the administrator is `nextcloud.admin_user`, default `admin`, and its first password is in the root-only `compose/nextcloud/.env` on the SSD.)
- The same application and data mounts are shared by Nextcloud and cron. PostgreSQL/Redis have no public ports. Nextcloud owns new HDD files as www-data UID/GID 33. Existing populated directories are refused instead of silently adopted or recursively chmodded.

Apply with the already confirmed configuration:

```bash
sudo bash install.sh --profile cloud --apply --config /absolute/path/to/confirmed-config.json
```

The LAN gateway adds Nextcloud at https://YOUR-LAN-IP:8447; optional Tailscale Serve uses its private node name on the same port. Complete initial login/security setup before confirming secured gateway services. The gateway checks all four containers, expected storage mounts, localhost publication and the live installed/migration status before exposure. It sets an explicit trusted host and canonical HTTPS origin via upstream occ. The most recently applied gateway becomes the canonical origin: choose LAN or tailnet as the primary client URL, especially if using both. Reapply the chosen gateway when its address changes. Client certificate trust and real phone/desktop/share permissions still need witnessed acceptance.

**Switching Nextcloud off** (`"enabled": false` under `"nextcloud"`, then `sudo bash install.sh`) stops its four containers; your files and its database stay, and switching it on again starts it as it was. Its home and Tailscale addresses are taken away by the same run. Phones and computers with the Nextcloud app keep trying to sync until you remove the account in that app.

Nextcloud trusts the gateways' hop through Docker for the whole of Docker's own range (`172.16.0.0/12`), not one address, so it keeps making https links (and signing in keeps working) after its Docker network is made again with another number. Docker's other range, `192.168.0.0/16`, which most home networks use, is not trusted as a whole. Nextcloud only answers on this PC itself (`127.0.0.1:8088`), so no other device can talk to it directly.

The dashboard lists Nextcloud, and acceptance includes automatic container/status checks plus a manual client upload/download/sharing test.

**What is backed up, and how it is tested:**

- **Monthly snapshot (on the HDD):** Nextcloud's application folder, its database and its Compose settings (the passwords in `.env` and `config.php` encrypted with the backup key; see MONTHLY-MAINTENANCE.md). **Not your files**: `cloud/files` on the HDD is not in the monthly snapshot.
- **Weekly USB copy:** your files (each user's own `files` folder), add-only, with checksums (STORAGE.md). Previews, trash, versions and group folders are not copied. Since 0.39.6 it first makes a copy of Nextcloud's database, kept on the HDD and on the USB drive (RECOVERY.md, "Nextcloud's database").
- **The monthly restore test** starts the restored database on its own and checks that Nextcloud's file table can be read from it. It does not start the whole restored Nextcloud with your files; the health check of the running Nextcloud (a disposable file written, read and deleted) happens only after an update, on the live server. So try a real restore (`sudo bash install.sh --recover` → Nextcloud files) before you rely on it.

**Server-side encryption** (Nextcloud's *Encryption* app) is not covered: the weekly USB copy takes the encrypted files, but not the keys Nextcloud needs to read them (its `files_encryption` folders), so they could not be read after losing the server. When it is on, the weekly backup says so and shows as **incomplete**, and the recovery wizard warns instead of suggesting an upload. Leave it off (the default), or keep your own backup of the whole Nextcloud data folder and its keys.

References: https://github.com/nextcloud/docker ; https://docs.nextcloud.com/server/stable/admin_manual/maintenance/backup.html ; https://docs.nextcloud.com/server/stable/admin_manual/maintenance/upgrade.html
