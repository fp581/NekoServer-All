# NekoServer v0.21.1

NekoServer turns a PC running **Arch Linux** or **Nyarch** into a home media server: your own Netflix-style video library, photo backup from your phone, music streaming, and a private cloud drive. One installer sets up the apps, creates their accounts, connects them together and schedules backups.

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
| **Dashboard** | One page showing drives, apps and backups | |

Optional extras: watching on a TV connected to the server (Steam Big Picture), shared folders on your home network (Samba), access from outside your home (Tailscale), and phone alerts when something fails (ntfy).

---

## What you need

**A PC that stays on**, with:

- **Arch Linux or Nyarch**, 64-bit (x86_64), already installed, up to date, and connected to the internet.
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

Do every step in a **terminal**, logged in as your normal user (not root).

### 1. Download NekoServer

```bash
git clone https://github.com/fp581/NekoServer---Arch-based-media-server.git
cd NekoServer---Arch-based-media-server/nekoserver-v0.21.1
```

### 2. Make your own settings file

```bash
cp config.json my-config.json
chmod 600 my-config.json
```

`my-config.json` is yours: the installer saves your choices in it, and you use the same file every time you run the installer. Open it in a text editor and check these few settings. Everything else can stay as it is.

| Setting | What to put there | How to find it |
| --- | --- | --- |
| `"timezone"` | Your timezone, e.g. `"Europe/London"` | Run `timedatectl` and look at "Time zone". Backups run at night in this timezone. |
| `"uid"` and `"gid"` (under `"media"`) | The numbers for your user | Run `id -u` and `id -g`. Usually both are `1000`. |
| `"user"` (under `"tv"`) | Leave it as `"auto"` | See [Who is the TV user?](#who-is-the-tv-user) |

You do **not** need to type drive IDs: the installer shows your drives and asks you to pick.

### 3. Check before installing (optional, changes nothing)

```bash
bash install.sh --profile full --check --config "$PWD/my-config.json"
```

It should end with `Configuration and dependency graph valid.` If it says Python is missing, skip this step: the install in step 4 installs Python first.

### 4. Install

```bash
sudo bash install.sh --profile full --apply --config "$PWD/my-config.json"
```

This takes a while. The installer will:

1. **Ask you to pick your SSD, HDD and USB drive** from a list and confirm each one.
2. **Install** Docker and the apps, and connect them to each other.
3. **Ask which network connection is your home network** (a numbered list such as `1. enp3s0 · 192.168.1.20`). That address is how other devices at home reach your apps.
4. **Create an account for each app.** For each app you choose a generated password (recommended) or your own.
5. **Ask for your sudo password once** to protect the backup of your app passwords. You are never asked again; night-time backups run on their own.
6. **Walk you through anything it cannot do alone**, such as signing in on a web page or pairing a phone. Each time it opens the right page, tells you what to do, and asks `Have you completed this step successfully? [yes/no]`. Answer `no` to see the step again.

At the end it prints how many steps passed. If some say **NEEDS SETUP** or **FAILED**, read the message, fix what it says, and run:

```bash
sudo bash install.sh --retry-failed --apply --config "$PWD/my-config.json"
```

It is always safe to run the installer again with the same `my-config.json`: it keeps what is done and continues with the rest.

### 5. Optional: use your apps outside your home

This adds secure access through **Tailscale** (free for personal use) without opening your router to the internet:

```bash
NEKO_RELEASE=/srv/nekoserver/installer/releases/v0.21.1
sudo bash "$NEKO_RELEASE/install.sh" --profile remote_access --apply --config "$NEKO_RELEASE/config.json"
```

It guides you through signing in to Tailscale and setting up your phone.

---

## Using your server

**Passwords.** Every app's username and password is saved on the server in:

```
/srv/nekoserver/secrets/accounts/APP-CREDENTIALS.txt
```

Only root can read it: `sudo cat /srv/nekoserver/secrets/accounts/APP-CREDENTIALS.txt`. Keep a copy somewhere safe, such as a password manager.

**Opening the apps from another device at home.** Use the home-network address you picked during install (the installer also prints the app menu's address at the end; `192.168.1.20` below is an example). Open the app menu in a browser:

| Page | Address |
| --- | --- |
| App menu | `https://192.168.1.20:8442` |
| Jellyfin | `https://192.168.1.20:8443` |
| Seerr | `https://192.168.1.20:8444` |
| Immich | `https://192.168.1.20:8445` |
| Navidrome (music) | `https://192.168.1.20:8446` |
| Nextcloud | `https://192.168.1.20:8447` |

Replace `192.168.1.20` with your server's address. The first time, your browser warns that the certificate is not trusted, because your server made its own. To stop the warning, install the server's certificate on each device ([GATEWAY.md](GATEWAY.md) explains how). Some TV and phone apps refuse home-made certificates; if one does, use the Tailscale addresses from step 5 instead.

**On the server itself**, the dashboard is at `http://127.0.0.1:8787`. Sonarr, Radarr, Lidarr, Prowlarr and qBittorrent are also only reachable from the server's own browser, on purpose.

**Phone apps:** Immich (photos), Jellyfin (video), Nextcloud (files), and a Navidrome-compatible music player such as Amperfy on iPhone (see [MUSIC.md](MUSIC.md)). Sign in with the server address and the app's username and password.

---

## Backups

What is copied, where, and when (times are in your `timezone`):

| What | Where it is backed up | When |
| --- | --- | --- |
| Photos and videos (Immich) | HDD **and** USB drive | Sundays |
| Immich albums, faces, settings (database) | HDD | Sundays |
| Nextcloud files | USB drive | Sundays |
| App settings | HDD | Every night |
| Steam saves and settings, Samba settings, any extra folders you list | HDD | Every night |
| All app settings, databases and app versions, before updates | HDD | 1st of each month |
| **Movies, shows and music** | **Not backed up** | |

- **Photos, videos and Nextcloud files are only ever added to the backup.** Nothing on it is overwritten or deleted. If you edit a file, the new version is saved next to the old one with a date in its name.
- **Old snapshots are cleaned up automatically:** the newest 3 monthly, 14 nightly, and 8 weekly Immich database copies are kept. Change `keep_snapshots` and `database_keep` in `my-config.json` to keep more, or set them to `0` to keep everything.
- **On the 1st of each month**, at night, the server takes a snapshot, tests that it can be restored, then updates the apps. If an update fails, it goes back to the snapshot automatically.

To get things back after a problem, see [RECOVERY.md](RECOVERY.md).

---

## Who is the TV user?

The **TV user** is the account you log into on the server PC itself: the one whose desktop shows on a TV connected to it. NekoServer adds Steam and video shortcuts to that account's desktop and, if you turn it on, starts Steam Big Picture when it logs in.

`"auto"` means "the user who ran `sudo`", which is you. Only change it if a **different** account on the server should be the TV account; then type that account's username, e.g. `"user": "livingroom"`.

---

## Words you might see

| Word | Meaning |
| --- | --- |
| **Profile** | A set of parts to install. `full` is everything for a home server; `remote_access` adds access from outside. |
| **Module** | One part of the installer (for example `photos` or `dashboard`). |
| **NEEDS SETUP** | A step needs something from you. The message says what. Fix it and rerun with `--retry-failed`. |
| **BLOCKED** | A step waits for another step that has not succeeded yet. |
| **Gateway** | The secure front door other devices use to reach your apps. |
| **Acceptance** | An optional checklist (`acceptance.py`) that confirms everything works after installation. |

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
| Restoring from backup | [RECOVERY.md](RECOVERY.md) |
| Phone alerts | [NOTIFICATIONS.md](NOTIFICATIONS.md) |
| Checking the installation | [ACCEPTANCE.md](ACCEPTANCE.md) |

**Advanced settings** in `my-config.json`, all optional:

- **`media.torrent_port`:** let other peers connect to qBittorrent for faster downloads. Set a port such as `51413`, then forward it on your router.
- **`keep_snapshots` and `database_keep`:** how many backups to keep.
- **No Nextcloud?** Set `nextcloud.enabled` to `false` **and** remove `"nextcloud"` from the `gateway.services` list.

---

## Status

Every change is tested automatically (Python and Rust tests, and checks of every app's configuration), but those tests simulate the hardware. Before trusting it with your only copy of anything, try a full round on your own PC: play a video, upload a photo from your phone, and restore a file from backup.
