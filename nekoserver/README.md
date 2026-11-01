# NekoServer v0.41.2

NekoServer turns a PC running **Arch Linux**, **Nyarch**, **Debian**, **Ubuntu**, **Fedora**, **RHEL** (and Rocky Linux, AlmaLinux) or **openSUSE** into a home media server: your own Netflix-style video library, photo backup from your phone, music streaming, and a private cloud drive. One command sets up the apps, creates their accounts, connects them together, sets up your phone, makes them reachable from anywhere through **Tailscale** (privately, only for your own devices) and schedules backups.

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

Seerr, Sonarr, Radarr, Lidarr, Prowlarr and qBittorrent are always set up, but you never have to use them: Jellyfin, Immich and Navidrome play and show your own files without them.

Also included: shared folders on your home network (Samba) and, if you want them, watching on a TV connected to the server (Steam Big Picture) and phone alerts when something fails (ntfy).

---

## What you need

**A PC that stays on**, with:

- **Linux, 64-bit (x86_64)**, already installed, up to date, and connected to the internet. One of:
  - **Arch Linux** or **Nyarch**
  - **Debian 12** or newer, or **Ubuntu 24.04** or newer (also Linux Mint and other Ubuntu-based systems built on those)
    - On Debian 12, setup builds NekoServer's program with Debian's newer Rust (`rustc-web`), since Debian 12's own `rustc` is too old. If you installed Debian's `rustc` yourself before, setup asks you to run `sudo apt install rustc-web cargo-web` once (it replaces the older one) and then `sudo bash install.sh` again.
  - **Fedora 42** or newer (new: tried only in a test setup so far; see *Fedora* below)
  - **RHEL 10**, **Rocky Linux 10**, **AlmaLinux 10** or **CentOS Stream 10**, or newer (new: tried only on AlmaLinux in a test setup so far; see *RHEL* below). Version 9 is not enough: its Python is too old.
  - **openSUSE Leap 16** or newer, **Tumbleweed** or **Slowroll** (tried on Leap 16 and Tumbleweed in a test setup; see *openSUSE* below). Leap 15 is not enough: its Python is too old.
- **8 GB of memory or more.** The apps use about 2 GB when idle, and more while Immich looks at new photos or Jellyfin converts a video; with less, those can be stopped for lack of memory. Setup shows what it found.
- **Your phone**, to set up the apps on it during installation.
- **Three drives besides the disk Linux is on** (four drives in all), each already formatted (ext4 is the safe choice). NekoServer never puts your files on the disk Linux runs from, so a mini PC with one internal disk needs the three as extra (USB) drives:

| Drive | Used for | Size | Notes |
| --- | --- | --- | --- |
| **System disk** | Linux itself | 5 GB free for the packages | Your existing disk. NekoServer does not use it for data. |
| **SSD** | Apps, photos, music | At least 20 GB free; 100 GB or more recommended | Fast drive. |
| **HDD** | Movies, shows, Nextcloud files, backups | At least 50 GB free; 500 GB or more recommended | Large drive. |
| **USB drive** | Weekly copy of your photos and Nextcloud files | At least 16 GB, and room for those files | Can be exFAT or ext4. Keep it plugged in. |

Setup checks the sizes when you pick the drives, before it mounts or saves anything: a drive that is too small is refused at once and you pick another one.

The SSD and HDD can be ext4, XFS or Btrfs (a Btrfs pool over several drives counts as one), also inside an LVM volume, a software RAID array or an encrypted (LUKS) drive. An encrypted drive must be unlocked by Linux at every start (listed in `/etc/crypttab`), because setup mounts it then.

> ⚠️ **Formatting erases everything on a drive.** NekoServer will never do it for you. Format the SSD, HDD and USB drive yourself (for example with GNOME Disks), and double-check which drive you are formatting.

The TV extras (Steam Big Picture on a TV) come from your Linux's own repositories on Arch/Nyarch, Debian, Ubuntu, Fedora and openSUSE Tumbleweed; RHEL (and Rocky/AlmaLinux) and openSUSE Leap have no Steam package, so the step skips itself there. Steam lives in a part of the repositories that is off by default (Arch's **multilib**, Debian's contrib/non-free with 32-bit packages, Fedora's RPM Fusion Steam repository, openSUSE's non-OSS): setup never switches those on itself; it tells you the exact command and you run setup again. On Debian and Ubuntu, Steam's installer asks you to accept Valve's licence: since you chose the TV step, setup answers "I agree" for you (without an answer the install fails). See [TV-SETUP.md](TV-SETUP.md).

---

## Install

Open a **terminal** (Ctrl+Alt+T on most desktops), logged in as your normal user, and run:

```bash
git clone https://github.com/fp581/NekoServer-All.git
cd NekoServer-All/nekoserver
sudo bash install.sh
```

`git: command not found`? Ubuntu Desktop and some other systems don't come with git. Install it with `sudo apt install git` (Fedora and RHEL: `sudo dnf install git`; openSUSE: `sudo zypper install git`; Arch: `sudo pacman -S git`), then run the three lines again.

That's it: one command does everything. Before its questions start, it works on its own for a while (usually 5 to 15 minutes, longer on a slow connection):

1. It **gets the system ready and installs the packages the apps need** (Docker, Tailscale, Caddy, Samba and a few tools), switched off until their own setup step. It needs 5 GB free on the system disk and stops before changing anything if there is less. How this works on each Linux is under *What setup does on your Linux* below.
2. It **builds NekoServer's own background program** (the Rust runtime that runs the backups, the dashboard and the checks) on your PC: it installs Rust from your Linux's own packages and compiles the program, the first time and after each update. Nothing of it is downloaded ready-made.
3. It **checks that this computer has what setup cannot do without** (such as systemd 247 or newer, or SELinux not enforcing) and, if not, stops and says what is missing.
4. The **setup window** opens (on a PC with a desktop) and the wizard starts.

**If a download fails.** A site that does not answer is tried again, three times, 20 seconds apart. If it still cannot be reached, setup names the site (`download.docker.com`, say) and the likely causes: something on your network blocking that site (a proxy, a router or parental filter, an ad-blocker such as Pi-hole, a VPN), the site being down for a while, or no internet right now. Check that the site opens in a browser on this PC, allow it if your network blocks it, and run `sudo bash install.sh` again. On a PC with a desktop the setup window opens to show this too.

**A firewall?** If one such as ufw is switched on, allow your home network to reach the shared folders (port 445) and the home addresses (ports 8442-8447, or 8450-8499 when another program has one of those), for example `sudo ufw allow from 192.168.1.0/24 to any port 445,8442:8447,8450:8499 proto tcp` with your own network instead of `192.168.1.0/24`; the Tailscale addresses need no rule.

<details>
<summary><b>What setup does on your Linux</b> (Arch, Debian and Ubuntu, Fedora, RHEL, openSUSE): only if you want the details</summary>

- **Debian / Ubuntu:** it only **refreshes the package lists** (`apt-get update`) and installs just what NekoServer needs. Updating the rest of your system stays your own routine. If an earlier update is waiting for a restart, it asks you to restart first. Docker comes from Docker's own repository and Tailscale from Tailscale's. If Debian's or Ubuntu's own Docker (`docker.io`) is installed, it stops and asks you to remove it first, because the two must not be mixed.
- **Arch / Nyarch:** it **updates the system** (`pacman -Syu`, because Arch only supports full updates). If the update installed a new Linux kernel, it asks you to restart the PC and run the command again.
- **Fedora:** like Debian, it only **refreshes the package lists** (`dnf makecache`) and installs just what NekoServer needs; Docker comes from Docker's own repository and Tailscale from Tailscale's. If Fedora's own Docker (`moby-engine`, `podman-docker`) is installed, it stops and asks you to remove it first. **SELinux:** NekoServer is not tested with SELinux yet, so setup stops while it is enforcing (Fedora's default) and explains how to try permissive mode and send what SELinux logged. Fedora's firewall (firewalld) needs the shared-folder and home-address ports opened; setup shows the command.
- **RHEL, Rocky Linux, AlmaLinux, CentOS Stream:** as on Fedora (package lists refreshed only, Docker and Tailscale from their makers' repositories, SELinux and firewalld the same). Caddy (the home gateway) and qrencode come from **EPEL**, Fedora's extra packages for these systems: setup adds it if it isn't there yet (from the system's own repositories, or from EPEL's site on RHEL itself). RHEL's own container tools that clash with Docker's (`runc`, `podman-docker`) must be removed first; setup says so. Podman itself can stay.
- **openSUSE:** everything comes from openSUSE's own repositories (Docker, Compose, Caddy and Tailscale included), and only the package lists are refreshed (`zypper refresh`). One exception: when the freshly installed Python cannot load parts of itself because the system's libraries are older than it (seen on a Leap 16 that had not been updated for a while), setup brings the installed packages up to date (`zypper update`; on Tumbleweed and Slowroll, which are rolling, `zypper dist-upgrade`) and goes on. SELinux is handled as on Fedora (Leap 16 enforces it by default on new installs); firewalld needs the same ports opened.

While it installs, a package source that does not answer is tried again a little later (also when `apt-get update` only warns about it), and when another program is already installing packages (automatic updates, a software centre) setup says so and waits for it, up to 10 minutes. A package manager that runs out of time is asked to stop and waited for, never killed, so it never leaves its lock or a half-made change behind; a pacman lock that no program has open (left by one that was cut off) is removed. A damaged repository key (Docker's or Tailscale's) is downloaded again. If the PC's clock is wrong, downloads and package checks fail ("not yet valid", "expired"); setup then says so and how to fix it: `sudo timedatectl set-ntp true`. The list of what setup found on this computer notes it too when the network does not keep the clock right.

</details>

Then the setup wizard explains what will happen and asks `Ready to start? [yes/no]`. Once you start, it first lists what it found on this computer (processor, systemd, Linux family, Docker, firewall and so on), then it:

1. **Connects the server to Tailscale** so you can use your apps from anywhere. It pauses, explains what Tailscale is, and opens the sign-in page. You create a free account (with Google, Microsoft, Apple or GitHub) and approve the server.
2. **Asks you to pick your SSD, HDD and USB drive** from a list (with each drive's size and the name you gave it), confirms each one by its name, and **checks they have enough space** before installing anything. The question says how big each must be (SSD at least 20 GB, HDD at least 50 GB); a drive that is too small is refused straight away, before anything is mounted or saved, and you pick another.
3. **Asks how to make the apps' logins**: generated passwords for all of them (recommended) or your own, app by app.
4. **Installs the apps, creates an account for each one**, connects them to each other, and **turns on the graphics chip for video** in Jellyfin and Immich when your PC has an Intel or AMD one ([HARDWARE-VIDEO.md](HARDWARE-VIDEO.md)). Connecting the media apps, the music setup and the shared-folder password ask their questions in the terminal only.
5. **Asks you to choose a dashboard PIN** (6 to 8 digits, typed twice). It lets you change fans and lights from your phone, and restart or close apps from the dashboard. Press Enter to skip it.
6. **Asks about a TV connected to the server** (an optional extra; answer `no` to skip it) and **puts shortcuts on the desktop** for the dashboard, the web browser, Steam and each app ([Desktop shortcuts](#desktop-shortcuts)).
7. **Gives each app a private Tailscale address and sets up your phone, app by app.** A page with QR codes opens on the PC: scan a code with the phone camera to open each address. For every app it tells you which phone app to install (iPhone and Android), what to type, and which username to use.
8. **Asks which network connection is your home network** (a numbered list such as `1. enp3s0 · 192.168.1.20`), for using the apps at home without Tailscale.
9. **Asks about two more optional extras**, once each: phone alerts when something fails, and outage alerts. Answer `no` to skip either; nothing else needs them.
10. **Asks you to choose a backup password once** (typed twice), to lock the backup of your app passwords. Write it down: restoring that list needs it. You are never asked again; night-time backups run on their own.
11. **Runs the first checkup and backup**, once every step is done: an app health check, then each backup you set up, straight away instead of the first night, then a check of the whole server. It says which ones finished and names anything that needs a look. This takes a few minutes to an hour, depending on how much is on the drives.
12. **Shows a finishing screen**: what's done, what needs you first and what waits for it, your app addresses, whether a dashboard PIN is set, and how to see your passwords.

Whenever it needs you, it shows numbered instructions, opens the right page, and asks `Have you completed this step successfully? [yes/no]`. Answer `no` to see the step again.

### Two ways to follow the installation: the terminal and the setup window

It is one installation, which you can follow and answer in two places at the same time. Use whichever you like; you can switch between them at any moment.

**1. The terminal** (always there). This is where the installation runs.

1. Open a terminal: on most desktops press **Ctrl+Alt+T**, or open the apps menu and search for **Terminal** (it may be called Konsole, GNOME Terminal or Console).
2. Go to the folder and start setup:

   ```bash
   cd NekoServer-All/nekoserver
   sudo bash install.sh
   ```

3. Type your own login password when `sudo` asks for it (nothing appears while you type; that is normal) and press Enter.
4. Answer the questions by typing and pressing Enter: `yes` or `no`, a number from a list, or a password. Keep this terminal open until the finishing screen: closing it stops the installation (running the command again continues where it stopped).

**2. The setup window** (on a PC with a desktop). Once the packages are installed and NekoServer's program is built (usually 5 to 15 minutes after you start `sudo bash install.sh`), a small window opens in your web browser by itself, just before `Ready to start?`. The terminal says so and shows the window's address, for example `http://127.0.0.1:40123/t/AbC.../`. If setup stops before then (a site it downloads from cannot be reached, say), a window still opens to say why.

- **Before setup starts** it explains the optional extras (a TV, phone alerts, outage alerts, Nextcloud) and has a switch for each. Switch off what you don't want (setup asks about the TV and the alerts you leave alone when it gets to them; Nextcloud is installed unless you switch it off), then press **Start setup** (the same as typing `yes` at "Ready to start?" in the terminal). From then on the choices are fixed; to change one later, edit `my-config.json` and run setup again.
- **While setup runs** it shows the step under way, what that step installs and why, and every question setup asks, with buttons and boxes to answer it. Answer it there or in the terminal: whichever comes first counts.
- **"Answer this step in the terminal"**: a few steps run a program of their own (connecting the media apps, music setup, the shared-folder password) whose questions can only be answered in the terminal. The window says so while they run; switch to the terminal for them.
- **Closed the window, or it did not open?** Copy the address the terminal showed into a browser **on this PC**, from your own account, and it opens again (a window still open elsewhere then says it was opened somewhere else). It works only on this PC (it is not reachable from another computer or a phone) and only while setup runs. Without a desktop, use the terminal alone; nothing is missing.
- **At the end** it shows the finishing screen (what's done, what still needs you, your app addresses), which stays in the window until you close it, also after setup has closed.

More detail: [GUIDED-SETUP.md](GUIDED-SETUP.md#the-setup-window).

**Stopped, or something not finished?** Run `sudo bash install.sh` again. It keeps everything already done and continues with the rest. Steps you already confirmed (Tailscale, the phone, the TV tests) are not asked again.

Your settings are saved in `my-config.json` next to the installer. The wizard creates it the first time, with your user filled in (the timezone is this PC's own, `"auto"`), so you don't need to edit it. See [Optional settings](#optional-settings).

**Updating to a newer release.** In the `NekoServer-All/nekoserver` folder:

```bash
git pull
sudo bash install.sh
```

Your `my-config.json` stays where it is (git does not touch it); setup updates what changed, builds the runtime for the new version and keeps everything else. Steps the new release added are added to your settings' profiles (the log names them); a step you took out yourself stays out. Read the new version's *Upgrade notes* in [CHANGELOG.md](../CHANGELOG.md) first.

**Updating from a clone made before 0.40.0** (a server running 0.39.x or older). The repository's history was cleaned in 0.40.0, so an older clone cannot simply `git pull`: git refuses ("refusing to merge unrelated histories") or wants to merge the whole old history back. Clone it again once, in the folder that holds `NekoServer-All`:

```bash
mv NekoServer-All NekoServer-All.old
git clone https://github.com/fp581/NekoServer-All.git
cp NekoServer-All.old/nekoserver/my-config.json NekoServer-All/nekoserver/
cd NekoServer-All/nekoserver
sudo bash install.sh
```

The third line copies your settings into the new folder. If you forget it, setup finds the copy it keeps on the SSD and asks `Use them for this release (yes when updating)?`: answer `yes`. Nothing installed runs from the old folder, so once setup has finished you can delete `NekoServer-All.old`. After this, updates are `git pull` again.

Coming from a release before 0.39.2, where each release had a folder named for its version (`nekoserver-v0.37.3`, say)? After `git pull` the folder is simply `nekoserver`, and your `my-config.json` is still in the old one. Setup finds it there, or the copy it keeps on the SSD, and asks `Use them for this release (yes when updating)?`. Answer `yes`: it copies your settings in. The old folder can then be deleted.

---

## Using your server

**Passwords.** Every app's username and password is saved on the server. To see an app's login (with a QR code to scan on your phone; the screen is cleared afterwards):

```bash
sudo python3 /srv/nekoserver/installer/releases/v<version>/account-setup.py show jellyfin
```

(`/srv/nekoserver` is your SSD's folder and `<version>` the release you run, the number at the top of this page; the finishing screen shows the exact command.)

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

**On other devices at home** you can also use your server's home address.

> **First, in your router, reserve that address for the server** (the setting is usually called "DHCP reservation" or "static lease"). Otherwise it can change after a restart and the home addresses stop working; if that happens, run `sudo bash install.sh` again and choose the home network again.

The home address is, for example, `https://192.168.1.20:8442` for the app menu (ports 8443–8447 for the apps, as above; when another program on the server already uses one of these ports, setup takes a free one from 8450–8499 instead and says so, and the app menu links each app with its right port). The end of setup shows the app menu address. Browsers warn about the certificate the first time, because your server made its own; [GATEWAY.md](GATEWAY.md) explains how to trust it. The Tailscale addresses have normal certificates and never warn.

**The dashboard** shows your apps, backups, drives, alerts and live hardware (processor, memory, graphics, every temperature sensor and fan it can find, network, processes). On the server it is at `http://127.0.0.1:8787` (or **NekoServer Dashboard** in the app menu); on your phone or laptop (Tailscale on) at `https://yourserver.tailXXXX.ts.net:8448`, also on the phone setup page. You can change fan curves and RGB lighting (*Fans & lights*; phones and tablets need the PIN chosen during setup), and restart an app or close a desktop app (these need the PIN on every device). Nothing else can be changed from it. On a phone or tablet, choose *Add to Home Screen* / *Install app* to use it like an app. To keep it on the server only, set `"remote": false` under `"dashboard"`. Sonarr, Radarr, Lidarr, Prowlarr and qBittorrent are only reachable from the server's own browser, on purpose.

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
- **Old snapshots are cleaned up automatically:** the newest 7 nightly (apps and settings), 4 weekly Immich database copies and 3 monthly snapshots are kept. Change `keep_snapshots` and `database_keep` in `my-config.json` to keep more, or set them to `0` to keep everything.
- **On the 1st of each month**, at night, the server takes a snapshot, tests that it can be restored, then updates the apps. If an update fails, it goes back to the snapshot automatically. New major versions are only shown on the dashboard until you approve them (see [Optional settings](#optional-settings)).
- **Weekly copies only add what is new.** Photos already backed up are recognised without being read again, so a weekly run takes minutes, not hours, even for a big library. Any file rewritten or replaced since its copy is compared by content. Once a month, as the first step of the monthly run (before its backup and updates), every copy is compared by checksum.
- **An incomplete weekly copy is shown as one.** If some Nextcloud files could not be copied (open while copying, or names an exFAT USB drive cannot store, including two names that differ only in upper/lower case, such as `Documents` and `documents`, which exFAT treats as one), the dashboard and phone alerts say *Incomplete* and list them, instead of reporting success.

**Getting something back.** Run `sudo bash install.sh --recover`. A menu asks what you want back (photos, Nextcloud files, the password list, app settings, or a whole monthly snapshot), lists the backups by date, and restores into a **new** folder, so nothing you have now is overwritten. It checks there is enough free space first, and waits if the weekly backup is using the USB drive. It ends by telling you how to put things back. More detail: [RECOVERY.md](RECOVERY.md).

---

## Optional settings

Everything works without editing anything. If you want to change something, edit `my-config.json` (next to the installer) and run `sudo bash install.sh` again.

| Setting | What it does | Default |
| --- | --- | --- |
| `"timezone"` | Timezone for the night-time backups and updates, e.g. `"Europe/London"` | `"auto"`: the PC's own timezone |
| `"user"` under `"tv"` | Which account is the TV user (below) | `"auto"`: you |
| `"torrent_port"` under `"media"` | Lets other peers connect to qBittorrent for faster downloads. Set a port such as `51413`, then forward it on your router. | off |
| `"keep_snapshots"` and `"database_keep"` | How many backups to keep (`0` keeps all) | 7 nightly, 4 weekly database copies, 3 monthly |
| `"immich_major_update"`, `"nextcloud_major_update"` under `"maintenance"` | Approve a new major app version shown on the dashboard; the next monthly update installs it. See [MONTHLY-MAINTENANCE.md](MONTHLY-MAINTENANCE.md#major-version-updates). | empty (not installed) |
| `"wanted"` under `"tv"`, `"notifications"` or `"heartbeat"` | Your answer to the optional extras (TV, phone alerts, outage alerts). Change `false` to `true` to set one up later. | asked once |
| `"data_mount"`, `"media_mount"`, `"external_backup_mount"` | Where the SSD, HDD and USB drive are mounted. Anywhere under `/srv` or `/mnt`; not under `/home`, `/root` or `/run/user`, which NekoServer's services can't see. | `/srv/nekoserver`, `/mnt/nekoserver-media`, `/mnt/nekoserver-photos` |
| `"enabled"` under `"nextcloud"` | Set to `false` if you don't want Nextcloud | on |
| `"lan_ports"` under `"gateway"` | The home addresses' ports, written by setup only when the usual port (8442-8447) was taken by another program. Delete an entry to go back to the usual port once that program is gone. | none |
| `"samba_user"` under `"networking"` | Whose login opens the shared folders from other computers. `"auto"` is the person who ran `sudo bash install.sh` the first time; later runs keep that account, whoever runs them. Put a username here to choose another. | `"auto"` |
| `"services"` under `"gateway"` | Which apps get a private address at home and through Tailscale. Left as shipped, the list follows the apps you set up; a list you edit is used as it is. | all the apps |

### Who is the TV user?

The **TV user** is the account you log into on the server PC itself: the one whose desktop shows on a TV connected to it. NekoServer puts its shortcuts on that account's desktop (see "Desktop shortcuts" below). Nothing starts by itself when it logs in. Left as `"auto"`, it is the person who ran `sudo bash install.sh` the first time, and later runs keep that account; put a username under `"user"` in `"tv"` to choose another.

### Desktop shortcuts

At the end of setup, the server's desktop (and its app menu) gets a shortcut for:

- the **NekoServer Dashboard**, in its own window;
- the **web browser**;
- **Steam**, if it is installed (with the TV extras, also **Steam Big Picture** and **YouTube**);
- **each app** that was set up: Jellyfin, Immich, Seerr, Navidrome, Nextcloud and the others. Each opens in your browser.

A shortcut you changed or made yourself with the same name is never replaced. To leave the desktop alone (the app menu still gets them), set `"desktop_shortcuts": false` under `"tv"`. On GNOME (Ubuntu's desktop), desktop icons need the desktop-icons extension; Ubuntu has it already.

`"auto"` means "the user who ran `sudo`", which is you. Only change it if a **different** account on the server should be the TV account; then type that account's username, e.g. `"user": "livingroom"`.

---

## Uninstalling

It removes NekoServer from this PC: what you choose, after two confirmations, and it never formats a drive.

1. Open a terminal (**Ctrl+Alt+T**, or search the apps menu for **Terminal**) and go to the folder you installed from:

   ```bash
   cd NekoServer-All/nekoserver
   ```

2. **Look first** (optional, changes nothing, no password needed): `bash uninstall.sh --dry-run` shows what each choice would remove, with sizes.
3. **Start it:** `sudo bash uninstall.sh`, then type your login password.
4. **Choose what goes.** A numbered checklist appears; `[x]` means it will be removed. Type a number and press Enter to switch that line on or off (several at once work too: `3 4`), `all` to take everything, or just press Enter when the list is the way you want it. Only NekoServer itself and the app containers are switched on to begin with: your photos, movies, Nextcloud files and backups stay unless you switch them on.
5. **Check the plan.** It lists every folder that would go, with its size, and what stays. Type `yes` to go on (anything else stops, with nothing changed).
6. **Type the phrase** it shows, exactly, for example `remove NekoServer and delete my photos, backups`. Then it removes what you chose and says what it did. Nothing removed can be brought back.

What each choice means:

- **NekoServer itself always goes:** its services and timers, programs, settings, the Samba shares and account it made, the Samba storage guard (and Docker's, when its data goes too: see below), the pages it served on your tailnet, its group, the desktop shortcuts.
- **Everything else is a checklist** you switch on or off: the apps' containers and networks; the apps' data on the SSD (databases, settings, passwords, downloads, NekoServer's own files; Docker's data-root setting is put back); your photos and videos; your movies, shows and music; your Nextcloud files; the backups on the HDD and the USB drive; the drive mounts (the lines NekoServer wrote in `/etc/fstab`, the empty mount folders).
- **Then the plan**, every path with its size, and the first confirmation (`yes`). **Then the phrase** that names what goes, typed exactly, such as `remove NekoServer and delete my photos, backups`. Nothing listed can be brought back afterwards.
- **What stays:** the drives and whatever you kept on them, this folder (delete it yourself), and the packages NekoServer installed (Docker, Samba, Caddy, Tailscale, Rust, OpenRGB, Steam if the TV step added it): remove any you do not want with your package manager (the package repositories NekoServer added for Docker and Tailscale stay with them). Samba's own example `smb.conf`, set aside by setup, is put back. A package installation setup left unfinished is finished first, as setup would (or, when that is not possible, the protection it kept is lifted: the masks on the new services and NekoServer's `/usr/sbin/policy-rc.d`), so no service stays unable to start. This PC stays logged in to Tailscale. Setup's own records on the system disk (`/root/.local/state/nekoserver`) are removed; `/etc/fstab.nekoserver.bak` stays: it is your fstab from before NekoServer; delete it yourself once you no longer need it.
- **Samba is left as it was before NekoServer.** If NekoServer switched Samba on, it is switched off again (so the example `smb.conf` that comes back is not used). If Samba was already on before NekoServer, or your own `smb.conf` uses it, it stays on. The plan says which.

Good to know:

- **It waits** while a monthly update is running or did not finish, and while setup is running: it says what to do and removes nothing.
- **If it stops part-way** (a service or a container that won't stop), nothing has been deleted, and NekoServer's services and timers are switched back on as they were; fix what it names and run it again.
- **Docker's start guard stays** while Docker keeps its data on the SSD (you kept the SSD data and the mounts), so Docker never starts an empty store on the system disk while the SSD is missing. It goes with the SSD data or the mounts.
- **`/etc/fstab`** is replaced in one step, and the file as it was is kept beside it (`fstab.before-nekoserver-uninstall-<date-time>`); so is Docker's `daemon.json`. A drive that is still in use is not unmounted: it says `could not unmount (busy): sudo umount …`.
- **Nothing fails quietly.** A file it could not delete is named, with the reason (`could not delete …: Device or resource busy`). So is a command that did not work, with the command to run yourself. At the end it lists everything it could not remove or switch off, and then it says "Finished, but not everything went" instead of "Done".
- **Which settings it used** is the first line it shows (`Settings: …`). It uses `my-config.json` in this folder. Run from a fresh download (no `my-config.json`), it reads the drives setup recorded in `/etc/nekoserver-os/drives.json` or `/etc/nekoserver-backups/storage.json` (the newer one): the mount folders and the drives' IDs. The USB backup drive is not in those files, so nothing on it is removed then. With neither file, nothing on the drives is removed.

`bash uninstall.sh --dry-run` only shows what each choice would remove.

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
