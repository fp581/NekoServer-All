# NekoServer v0.27.7

NekoServer turns a PC running **Arch Linux** or **Nyarch** into a home media server: your own Netflix-style video library, photo backup from your phone, music streaming, and a private cloud drive. One command sets up the apps, creates their accounts, connects them together, sets up your phone, makes them reachable from anywhere through **Tailscale** (privately, only for your own devices) and schedules backups.

It does **not** install Linux and it **never formats a drive**. You prepare those first (see [What you need](#what-you-need)).

---

## What you get

| App | What it does | Where your files live |
| --- | --- | --- |
| **Jellyfin** | Watch your movies and TV shows on a TV, phone or browser | HDD |
| **Seerr** | Request a movie or show; it is found and added for you | |
| **Sonarr / Radarr / Lidarr** | Find and organise TV shows / movies / music | |
| **Prowlarr** | Manages the sources Sonarr, Radarr and Lidarr search | |
| **qBittorrent** | Downloads what they find | HDD (video), SSD (music) |
| **Navidrome** | Stream your music to phones and browsers | SSD |
| **Immich** | Back up and browse photos and videos from your phone | SSD |
| **Nextcloud** | Your own private cloud drive (like Google Drive) | HDD |
| **Dashboard** | One page for your apps, backups, drives and live hardware: processor, graphics, temperatures, fans, network. Fan curves and RGB lighting (OpenRGB) too. Works on phone and tablet, installable as an app | |
| **Tailscale** | Use your apps from anywhere, privately, on your own devices | |

Also included: shared folders on your home network (Samba) and, if you want them, watching on a TV connected to the server (Steam Big Picture) and phone alerts when something fails (ntfy).

---

## What you need

**A PC that stays on**, with:

- **Arch Linux or Nyarch**, 64-bit (x86_64), already installed, up to date, and connected to the internet.
- **Your phone**, to set up the apps on it during installation.
- **Four drives**, each already formatted (ext4 is the safe choice):

| Drive | Used for | Notes |
| --- | --- | --- |
| **System disk** | Linux itself | Your existing disk. NekoServer does not use it for data. |
| **SSD** | Apps, photos, music | Fast drive. |
| **HDD** | Movies, shows, Nextcloud files, backups | Large drive. |
| **USB drive** | Weekly copy of your photos and Nextcloud files | Can be exFAT or ext4. Keep it plugged in. |

> ⚠️ **Formatting erases everything on a drive.** NekoServer will never do it for you. Format the SSD, HDD and USB drive yourself (for example with GNOME Disks), and double-check which drive you are formatting.

If you want the TV extras, also turn on the **multilib** repository in `/etc/pacman.conf` (Steam needs it).

---

## Install

Open a **terminal**, logged in as your normal user, and run:

```bash
git clone https://github.com/fp581/NekoServer---Arch-based-media-server.git
cd NekoServer---Arch-based-media-server/nekoserver-v0.27.7
sudo bash install.sh
```

That's it: one command does everything. First it **updates the system** (`pacman -Syu`, so every app installs at its current version) and **installs the packages the apps need** (Docker, Tailscale, Caddy, Samba and a few tools). If the update installed a new Linux kernel, it asks you to restart the PC and run the command again. It needs 5 GB free on the system disk for this; it stops before changing anything if there is less.

Then the setup wizard explains what will happen and asks `Ready to start? [yes/no]`. It:

1. **Connects the server to Tailscale** so you can use your apps from anywhere. It pauses, explains what Tailscale is, and opens the sign-in page. You create a free account (with Google, Microsoft, Apple or GitHub) and approve the server.
2. **Asks you to pick your SSD, HDD and USB drive** from a list, confirms each one, and **checks they have enough space** before installing anything.
3. **Installs the apps**, connects them to each other, and **turns on the graphics chip for video** in Jellyfin and Immich when your PC has an Intel or AMD one ([HARDWARE-VIDEO.md](HARDWARE-VIDEO.md)).
4. **Creates an account for each app.** For each one you choose a generated password (recommended) or your own.
5. **Asks for your sudo password once**, to lock the backup of your app passwords. You are never asked again; night-time backups run on their own.
6. **Gives each app a private Tailscale address and sets up your phone, app by app.** A page with QR codes opens on the PC: scan a code with the phone camera to open each address. For every app it tells you which phone app to install (iPhone and Android), what to type, and which username to use.
7. **Asks which network connection is your home network** (a numbered list such as `1. enp3s0 · 192.168.1.20`), for using the apps at home without Tailscale.
8. **Asks about three optional extras**, once each: a TV connected to the server, phone alerts when something fails, and outage alerts. Answer `no` to skip any of them; they are not needed for anything else.
9. **Shows a finishing screen** with what's done, anything still waiting for you, your app addresses, and where your passwords are.

Whenever it needs you, it shows numbered instructions, opens the right page, and asks `Have you completed this step successfully? [yes/no]`. Answer `no` to see the step again.

**Stopped, or something not finished?** Run `sudo bash install.sh` again. It keeps everything already done and continues with the rest. Steps you already confirmed (Tailscale, the phone, the TV tests) are not asked again.

Your settings are saved in `my-config.json` next to the installer. The wizard creates it the first time, with your timezone and user filled in, so you don't need to edit it. See [Optional settings](#optional-settings).

---

## Using your server

**Passwords.** Every app's username and password is saved on the server. To see them:

```bash
sudo cat /srv/nekoserver/secrets/accounts/APP-CREDENTIALS.txt
```

Keep a copy somewhere safe, such as a password manager.

**On your phone (anywhere, at home or away).** Keep the **Tailscale** app turned on, then use the apps the wizard set up. The addresses look like `https://yourserver.tailXXXX.ts.net:8443`. The wizard shows yours, and the phone setup page lists them with QR codes; open it again any time on the PC at `/usr/local/share/nekoserver-pages/phone-setup.html`.

| App | Phone app | Address ends in |
| --- | --- | --- |
| Jellyfin (movies, TV) | Jellyfin | `:8443` |
| Seerr (requests) | Phone browser (add it to your home screen) | `:8444` |
| Immich (photos) | Immich | `:8445` |
| Navidrome (music) | Amperfy (iPhone), Substreamer or Ultrasonic (Android) | `:8446` |
| Nextcloud (files) | Nextcloud | `:8447` |
| Dashboard (status) | Phone browser (add it to your home screen) | `:8448` |

These addresses are **private**: they work only on devices signed in to your Tailscale account. Nothing is opened to the public internet, and your router needs no changes.

**On other devices at home** you can also use your server's home address (first, in your router, reserve that address for the server, usually called "DHCP reservation" or "static lease"; otherwise it can change after a restart and the home addresses stop working), for example `https://192.168.1.20:8442` for the app menu (ports 8443–8447 for the apps, as above). Browsers warn about the certificate the first time, because your server made its own; [GATEWAY.md](GATEWAY.md) explains how to trust it. The Tailscale addresses have normal certificates and never warn.

**The dashboard** shows your apps, backups, drives, alerts and live hardware (processor, memory, graphics, every temperature sensor and fan it can find, network, processes). On the server it is at `http://127.0.0.1:8787` (or **NekoServer Dashboard** in the app menu); on your phone or laptop (Tailscale on) at `https://yourserver.tailXXXX.ts.net:8448`, also on the phone setup page. Apart from fan curves and RGB lighting (*Fans & lights*; phones and tablets need the PIN chosen during setup), nothing can be changed from it. On a phone or tablet, choose *Add to Home Screen* / *Install app* to use it like an app. To keep it on the server only, set `"remote": false` under `"dashboard"`. Sonarr, Radarr, Lidarr, Prowlarr and qBittorrent are only reachable from the server's own browser, on purpose.

---

## Backups

What is copied, where, and when (times are in your `timezone`):

| What | Where it is backed up | When |
| --- | --- | --- |
| Photos and videos (Immich) | HDD **and** USB drive | Sundays |
| Immich albums, faces, settings (database) | HDD **and** USB drive (newest 4 copies on the USB) | Sundays |
| Nextcloud files | USB drive | Sundays |
| App settings | HDD | Every night |
| Steam saves and settings, Samba settings, any extra folders you list | HDD | Every night |
| All app settings, databases and app versions, before updates | HDD | 1st of each month |
| **Movies, shows and music** | **Not backed up** | |

- **Photos, videos and Nextcloud files are only ever added to the backup.** Nothing on it is overwritten or deleted. If you edit a file, the new version is saved next to the old one with a date in its name.
- **Old snapshots are cleaned up automatically:** the newest 3 monthly, 14 nightly, and 8 weekly Immich database copies are kept. Change `keep_snapshots` and `database_keep` in `my-config.json` to keep more, or set them to `0` to keep everything.
- **On the 1st of each month**, at night, the server takes a snapshot, tests that it can be restored, then updates the apps. If an update fails, it goes back to the snapshot automatically. New major versions are only shown on the dashboard until you approve them (see [Optional settings](#optional-settings)).
- **Weekly copies only add what is new.** Photos already backed up are recognised without being read again, so a weekly run takes minutes, not hours, even for a big library. Any file rewritten or replaced since its copy is compared by content, and every 4 weeks every copy is compared by checksum.
- **An incomplete weekly copy is shown as one.** If some Nextcloud files could not be copied (open while copying, or names an exFAT USB drive cannot store), the dashboard and phone alerts say *Incomplete* and list them, instead of reporting success.

**Getting something back.** Run `sudo bash install.sh --recover`. A menu asks what you want back (photos, Nextcloud files, the password list, app settings, or a whole monthly snapshot), lists the backups by date, and restores into a **new** folder, so nothing you have now is overwritten. It checks there is enough free space first, and waits if the weekly backup is using the USB drive. It ends by telling you how to put things back. More detail: [RECOVERY.md](RECOVERY.md).

---

## Optional settings

Everything works without editing anything. If you want to change something, edit `my-config.json` (next to the installer) and run `sudo bash install.sh` again.

| Setting | What it does | Default |
| --- | --- | --- |
| `"timezone"` | Timezone for the night-time backups and updates, e.g. `"Europe/London"` | `"auto"`: the PC's own timezone |
| `"user"` under `"tv"` | Which account is the TV user (below) | `"auto"`: you |
| `"torrent_port"` under `"media"` | Lets other peers connect to qBittorrent for faster downloads. Set a port such as `51413`, then forward it on your router. | off |
| `"keep_snapshots"` and `"database_keep"` | How many backups to keep (`0` keeps all) | 3 monthly, 14 nightly, 8 database |
| `"immich_major_update"`, `"nextcloud_major_update"` under `"maintenance"` | Approve a new major app version shown on the dashboard; the next monthly update installs it. See [MONTHLY-MAINTENANCE.md](MONTHLY-MAINTENANCE.md#major-version-updates). | empty (not installed) |
| `"wanted"` under `"tv"`, `"notifications"` or `"heartbeat"` | Your answer to the optional extras (TV, phone alerts, outage alerts). Change `false` to `true` to set one up later. | asked once |
| `"enabled"` under `"nextcloud"` | Set to `false` if you don't want Nextcloud; also remove `"nextcloud"` from the `"services"` list under `"gateway"` | on |

### Who is the TV user?

The **TV user** is the account you log into on the server PC itself: the one whose desktop shows on a TV connected to it. NekoServer adds Steam and video shortcuts to that account's desktop and, if you turn it on, starts Steam Big Picture when it logs in.

`"auto"` means "the user who ran `sudo`", which is you. Only change it if a **different** account on the server should be the TV account; then type that account's username, e.g. `"user": "livingroom"`.

---

## Words you might see

| Word | Meaning |
| --- | --- |
| **Tailscale** | A free app that lets your devices reach your server from anywhere, privately. |
| **Module** | One part of the installer (for example `photos` or `dashboard`). |
| **NEEDS SETUP** | A step needs something from you. The message says what. Fix it and run `sudo bash install.sh` again. |
| **BLOCKED** | A step waits for another step that has not succeeded yet. |
| **Gateway** | The secure front door other devices use to reach your apps. |
| **Profile** | (Advanced) a named set of steps. The wizard runs `full`. |

---

## More guides

| Topic | File |
| --- | --- |
| Drive layout and backup details | [STORAGE.md](STORAGE.md) |
| App accounts and passwords | [ACCOUNT-SETUP.md](ACCOUNT-SETUP.md) |
| Each guided step | [GUIDED-SETUP.md](GUIDED-SETUP.md) |
| Home and remote access, certificates | [GATEWAY.md](GATEWAY.md) |
| TV setup (Steam, controllers) | [TV-SETUP.md](TV-SETUP.md) |
| Music | [MUSIC.md](MUSIC.md) |
| Monthly updates | [MONTHLY-MAINTENANCE.md](MONTHLY-MAINTENANCE.md) |
| Hardware video (graphics chip) | [HARDWARE-VIDEO.md](HARDWARE-VIDEO.md) |
| Restoring from backup | [RECOVERY.md](RECOVERY.md) |
| Phone alerts | [NOTIFICATIONS.md](NOTIFICATIONS.md) |
| The dashboard | [DASHBOARD.md](DASHBOARD.md) |
| Nextcloud | [NEXTCLOUD.md](NEXTCLOUD.md) |
| Outage alerts from outside (heartbeat) | [HEARTBEAT.md](HEARTBEAT.md) |
| Sleep, logs and memory settings | [SERVER-OS.md](SERVER-OS.md) |
| Checking the installation | [ACCEPTANCE.md](ACCEPTANCE.md) |

---

## Status

Every change is tested automatically (Python and Rust tests, and checks of every app's configuration), but those tests simulate the hardware. Before trusting it with your only copy of anything, try a full round on your own PC: play a video, upload a photo from your phone, and restore a file from backup.
