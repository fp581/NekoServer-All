<html><body>
<!--StartFragment--><html><head></head><body><h1>TrueNAS SCALE Home Media Server — Complete Guide</h1>
<p><strong>Version: Beta</strong> · Real-Debrid · ZFS · Tailscale · Self-Healing · Remote Access</p>
<p><em>Jellyfin · Navidrome · Immich · Sonarr · Radarr · Lidarr · Bazarr · Seerr</em>
<em>Intel Core Ultra 5 225 · Netanya, Israel</em></p>
<hr>
<h2>What You Will Have When You Finish</h2>

  |  
-- | --
🎬 Movies & TV | Phase 1: Seerr → qBittorrent → Jellyfin via local download. Phase 2: Jellyfin also browses your existing Real-Debrid library through Zurg/rclone.
🎵 Music | Add an artist once in Lidarr, every album downloads and appears in Symfonium or Substreamer on your phone.
📷 Photos | Phone photos back up automatically on WiFi. AI face and object search. Your private Google Photos.
🌍 Remote access | All apps reachable from anywhere via Tailscale. No port forwarding, no domain name needed.
🛡️ Self-healing | ZFS snapshots, nightly config backups, virus scanning, health checks. Resilient to drive failure.
📱 Plays on | Phone, Apple TV, Samsung, Android TV, Fire TV, Roku, Chromecast, any web browser.

</body></html><!--EndFragment-->
</body>
</html># TrueNAS SCALE Home Media Server — Complete Guide

**Version: Final Edition** · Real-Debrid · ZFS · Tailscale · Self-Healing · Remote Access

*Jellyfin · Navidrome · Immich · Sonarr · Radarr · Lidarr · Bazarr · Seerr*
*Intel Core Ultra 5 225 · Netanya, Israel*

---

## What You Will Have When You Finish

| | |
|---|---|
| 🎬 **Movies & TV** | Phase 1: Seerr → qBittorrent → Jellyfin via local download. Phase 2: Jellyfin also browses your existing Real-Debrid library through Zurg/rclone. |
| 🎵 **Music** | Add an artist once in Lidarr, every album downloads and appears in Symfonium or Substreamer on your phone. |
| 📷 **Photos** | Phone photos back up automatically on WiFi. AI face and object search. Your private Google Photos. |
| 🌍 **Remote access** | All apps reachable from anywhere via Tailscale. No port forwarding, no domain name needed. |
| 🛡️ **Self-healing** | ZFS snapshots, nightly config backups, virus scanning, health checks. Resilient to drive failure. |
| 📱 **Plays on** | Phone, Apple TV, Samsung, Android TV, Fire TV, Roku, Chromecast, any web browser. |

---

## How to Use This Guide

1. Read one part at a time. Do only the steps in that part.
2. Do not skip ahead unless the guide tells you to.
3. **"In the TrueNAS Shell"** → click the `>_` icon in the top-right corner of the TrueNAS web page.
4. **"In the TrueNAS web page"** → clicking buttons in your browser.
5. Every code block is safe to copy and paste in full. Do not retype — paste.
6. If you feel unsure, stop and re-read the step before pressing anything. Slow is safe.

---

# Part 0 — Understand What You Are Building

Before touching any hardware, understand the complete picture. This is a server that sits silently at home giving you Netflix + Spotify + Google Photos on your own hardware, under your own control.

## The Three Storage Layers

| Layer | Device | Pool / Path | Purpose |
|---|---|---|---|
| Operating system | SSD 1 | TrueNAS boot device | TrueNAS SCALE only — nothing else ever stored here |
| App data | SSD 2 | `apps` pool (`/mnt/apps/`) | App databases, configs, transcode temp, incomplete downloads — fast random I/O |
| Main storage | 2× 8 TB IronWolf | `tank` mirror (`/mnt/tank/`) | Media, photos, completed downloads, backups — large and redundant |

> [!TIP]
> **Why two SSDs?** The apps SSD handles all small random writes: Immich database, Jellyfin metadata, active torrent pieces. This keeps the HDD mirror doing large sequential reads and writes — what spinning drives do best. The result is snappier apps without wearing out the HDDs.

## Every App and What It Does

| App | What it does | Think of it as |
|---|---|---|
| TrueNAS SCALE | The operating system managing storage and Docker | The foundation — free |
| Tailscale | Encrypted private network for remote access | Your secure tunnel home |
| Jellyfin | Streams movies and TV to any device | Your private Netflix |
| Navidrome | Music streaming server | Your private Spotify |
| Immich | Photo backup with AI face and object search | Your private Google Photos |
| Sonarr | Tracks TV shows, finds and downloads episodes automatically | The TV robot |
| Radarr | Tracks movies, finds and downloads them automatically | The movie robot |
| Lidarr | Follows artists, downloads albums automatically | The music robot |
| Prowlarr | Search engine hub connecting all robots to indexers | The shared search engine |
| Bazarr | Auto-downloads subtitles for everything | The subtitle robot |
| Seerr | Request movies and shows from your phone | Your personal request app |
| qBittorrent | Downloads torrent files | The downloader |
| Zurg | Connects to Real-Debrid, creates virtual media folder | The Real-Debrid bridge |
| rclone | Mounts the Zurg virtual folder so Jellyfin can see it | The folder translator |
| FlareSolverr | Bypasses Cloudflare bot-protection for public indexers | The Cloudflare bypass |
| ClamAV | Scans downloaded files for viruses | Your download security guard |

## What Is Real-Debrid?

Real-Debrid costs about €4/month. Sign up at [[real-debrid.com](https://real-debrid.com/)](https://real-debrid.com).

- Real-Debrid has cached millions of movies and TV shows on fast servers
- When a movie is already in your Real-Debrid library, Zurg/rclone can expose it to Jellyfin almost instantly
- **This guide does not automatically turn every Seerr request into a Real-Debrid stream.** Automatic Seerr-to-Debrid grabbing needs an extra bridge (Decypharr/RDT-Client) which is outside this beginner build
- Local media always wins: anything imported by Sonarr/Radarr onto `/mnt/tank/data/media` plays from your drives and uses no Real-Debrid quota

> [!IMPORTANT]
> Real-Debrid is **Phase 2** in this guide. Do the entire Phase 1 first — prove that local media works, Jellyfin plays, and downloads import correctly. Then add Real-Debrid on top in Part 13.

## How Jellyfin Picks What to Stream

| Priority | Source | What happens |
|---|---|---|
| **1st — Always wins** | Your local drive (`/mnt/tank/data/media/`) | File is on your drives. Fastest. Zero Real-Debrid quota used. |
| **2nd** | Real-Debrid virtual folder (`/mnt/tank/realdebrid/`) | Streams from your mounted Real-Debrid library when the item already exists there. **Uses 0% of your local HDD space** — the content streams directly from Real-Debrid's servers. |
| **3rd** | Torrent download in progress | File appears in Jellyfin once qBittorrent finishes. |

> [!TIP]
> **Real-Debrid content uses zero local disk space.** Items in your Real-Debrid library stream directly from their cloud servers. Only content downloaded via qBittorrent (Phase 1) is stored on your IronWolf drives. You can browse thousands of Real-Debrid titles in Jellyfin without any of them touching your HDDs.

## The Network Model

| Traffic type | Needed? | How this guide handles it |
|---|---|---|
| Outbound internet | Yes | Always allowed — indexers, subtitles, metadata, updates |
| Public inbound internet | **No** | Blocked — do not open router port forwards |
| Private inbound (you remotely) | Yes | Tailscale only — encrypted private tunnel |
| Torrent peer inbound | Optional | Not required — Real-Debrid works outbound only |

## Music and Video Are Completely Separate

| | VIDEO — Jellyfin | MUSIC — Navidrome |
|---|---|---|
| Content | Movies, TV shows | Albums, singles, artists |
| Source | Real-Debrid OR local drive | Local drive only |
| Storage | Never auto-deleted | Never deleted — grows forever |
| Phone app | Jellyfin (free) | Symfonium (Android) or Substreamer (iOS) |
| Port | 8096 | 4533 |

---

# Part 1 — Hardware

| Component | Role in this build |
|---|---|
| Intel Core Ultra 5 225 | CPU + built-in Arc iGPU (hardware transcoding) + NPU |
| 32 GB DDR5 RAM | TrueNAS, ZFS ARC cache, and all running containers |
| MAXSUN iCraft B860M CROSS PRO | Motherboard |
| SSD 1 (e.g. Corsair T500 1 TB) | TrueNAS OS boot drive — nothing else ever stored here |
| SSD 2 (e.g. Corsair T500 1 TB) | Apps pool — databases, transcode, incomplete downloads |
| 2× Seagate IronWolf 8 TB | `tank` mirror — media, photos, completed downloads, backups |
| Lian Li SP750 V2 Gold 750 W | Power supply |
| Wired Ethernet cable | **Required** — never use Wi-Fi for a NAS |

## Cable Connections

- SSD 1 → M.2 slot M2_1 (OS drive)
- SSD 2 → M.2 slot M2_2 (apps drive)
- IronWolf Drive 1 → SATA port 1
- IronWolf Drive 2 → SATA port 2
- Ethernet cable → motherboard ethernet port → your router or switch
- Keyboard and monitor → connect temporarily for first install only

> [!WARNING]
> Always use a wired Ethernet cable. Wi-Fi causes mysterious transfer failures and timeouts that are very hard to diagnose.

---

# Part 2 — Install TrueNAS SCALE

This part installs the operating system onto SSD 1. You need a keyboard, monitor, and USB drive connected to the NAS for this part only.

## Step 2.1 — Create the USB Installer

1. Download **balenaEtcher** for free from [[balena.io/etcher](https://balena.io/etcher)](https://balena.io/etcher)
2. Download the latest **TrueNAS SCALE ISO** from [[truenas.com/truenas-scale](https://truenas.com/truenas-scale)](https://truenas.com/truenas-scale) (about 1.5 GB). Use TrueNAS SCALE 24.10 or newer for best Intel Arc iGPU support.
3. Open balenaEtcher → Flash from file → select ISO → Select target → your USB drive → Flash. Wait ~5 minutes then safely eject.

> [!WARNING]
> Do not select your SSD or HDD as the flash target — that would erase your drive.

## Step 2.2 — BIOS Setup

On the **MAXSUN iCraft B860M CROSS PRO**, the BIOS key is **Delete**. Press it immediately and repeatedly as soon as the screen lights up after powering on.

1. Plug the USB into the NAS and power it on.
2. Press **Delete** repeatedly. A colourful settings screen appears — this is the BIOS.
3. **Boot tab → Boot Option #1** → change to your USB drive (appears by brand name).
4. **Advanced tab → CPU Configuration → Intel Virtualization Technology** → Enabled.
5. Same area: **IOMMU / Intel VT-d** → Enabled. (Lets Docker containers use the Intel graphics chip.)
6. Power savings: find **ASPM** (PCIe Active State Power Management) → Enabled. Find **CPU C-states / Package C-state** → Auto or Enabled.
7. **Critical for headless use:** find **Primary Display**, **Primary Graphics**, or **iGPU Multi-Monitor** (in Advanced or Chipset/Graphics sub-menu). Set to **IGFX**, **iGPU**, or **Internal Graphics**. This forces the Intel Arc iGPU to stay active with no monitor connected. Without this, `/dev/dri` will be empty and Jellyfin hardware transcoding silently fails.
8. Press **F10** to save and exit. The NAS restarts from USB.

## Step 2.3 — Install TrueNAS

The installer is a blue text menu. Use arrow keys to move, Enter to select.

1. Select **Install/Upgrade**.
2. Select only **SSD 1** as the install destination — it will be the smallest drive (~1 TB), NOT the 8 TB IronWolfs. If you see two similar-sized SSDs, check serial numbers against the stickers on the drives.
3. Confirm the disk will be erased. This only erases SSD 1.
4. Create an admin account and password. **Write this down.**
5. Installation takes ~10 minutes. Select Reboot. While rebooting, **remove the USB drive** so TrueNAS boots from SSD 1.

## Step 2.4 — First Login

On your regular PC (not the NAS), open a web browser.

1. Type `http://truenas.local` and press Enter. If that does not work, the NAS shows its IP on the monitor during boot — type `http://192.168.1.50` (use your actual IP).
2. Log in with the username and password from installation.
3. A short setup wizard appears. Click **Next** or **Skip** through everything — do not configure storage here.

> [!TIP]
> TrueNAS SCALE is completely free. No license, no trial, no expiry.

---

# Part 3 — Create Storage Pools

A **pool** is a logical storage container spanning one or more physical drives. You will create two pools.

Before you start: write down the serial numbers from the stickers on the back of each IronWolf drive — TrueNAS shows drives by serial number.

## Pool 1: `tank` — HDD Mirror

| Setting | Value |
|---|---|
| Pool name | `tank` |
| Disks | Both 8 TB IronWolf HDDs |
| Layout | **Mirror** — both drives store identical data. One can fail without losing anything. |
| Purpose | Media, photos, completed downloads, backups |

1. In TrueNAS, click **Storage** in the left sidebar → **Create Pool**.
2. Name field: `tank`
3. Under Available Disks, tick both 8 TB IronWolf drives.
4. Click **Add Vdev → Mirror**. Both drives move into the mirror vdev area.
5. Confirm the layout shows **Mirror** with both drives inside.
6. Click **Create Pool**. Type exactly what TrueNAS asks to confirm. Pool creation takes ~1 minute.

> [!WARNING]
> Creating a pool **erases the selected drives**. Make sure both IronWolf drives are empty and you have NOT selected the SSDs.

## Pool 2: `apps` — SSD

| Setting | Value |
|---|---|
| Pool name | `apps` |
| Disk | SSD 2 only |
| Layout | Single disk — no mirror, but backed up nightly to `tank` |
| Purpose | App databases, transcode temp, incomplete downloads, scripts |

1. Still in Storage, click **Create Pool** again.
2. Name: `apps`
3. Select **SSD 2 only**. Do not select SSD 1 (TrueNAS is there) or the HDDs.
4. Click **Add Vdev → Stripe**.
5. Click **Create Pool** and confirm.

> [!NOTE]
> **Why not use the SSD as ZFS cache?** A ZFS L2ARC cache only helps when the same blocks are read repeatedly and RAM is exhausted. Putting the Immich database, Jellyfin metadata, active torrent writes, and transcode temp on SSD gives you far more benefit AND keeps app data separate from media.

> [!NOTE]
> **Enable SSD TRIM:** After creating the apps pool, go to **Storage → Disks**. Find your app SSD and make sure TRIM is enabled. Without TRIM, the apps SSD can slow down noticeably after months of use.

---

# Part 4 — Create Datasets and Folders

A **dataset** is like a special folder with its own snapshots, permissions, and security settings.

> [!NOTE]
> **The path rule:** Pool named `apps` + dataset named `appdata` = path `/mnt/apps/appdata`. Pool named `tank` + dataset named `photos` = path `/mnt/tank/photos`. The pool name is always part of the path.

**How to create a dataset:** In TrueNAS → Storage → click the ⋮ menu next to the pool name → Add Dataset → type the name → Save.

## Datasets on the `apps` pool (SSD)

| Dataset path | Purpose |
|---|---|
| `apps/appdata` | All app configs and databases |
| `apps/scripts` | Maintenance scripts and `config.env` |
| `apps/transcode` | Jellyfin transcoding temp files — heavy I/O on SSD |
| `apps/downloads-incomplete` | Active qBittorrent incomplete downloads |

## Datasets on the `tank` pool (HDD mirror)

| Dataset path | Purpose |
|---|---|
| `tank/data` | Media AND completed downloads — **one dataset** for instant hardlinks |
| `tank/photos` | Immich photo library |
| `tank/realdebrid` | rclone/Zurg virtual mount target (Phase 2 — Part 13) |
| `tank/backups` | Nightly backups of app configs from the SSD |

> [!IMPORTANT]
> **`tank/data` must be ONE dataset.** Do not create `tank/data/media` or `tank/data/downloads` as separate datasets.
>
> Sonarr and Radarr use **hardlinks** to import files. A hardlink creates a second directory entry pointing to the same physical data — no bytes copied, no space used twice, instantaneous regardless of file size. Hardlinks only work within a single ZFS dataset. If downloads and media are in different datasets, every import falls back to a slow file copy.
>
> **Triple-check:** In TrueNAS Storage → tank pool, you should see `data` as a dataset. You should NOT see `media` or `downloads` as separate datasets under it.

## Dataset Security Settings — Download Folders Only

**How to set:** click ⋮ next to dataset → Edit → Advanced Options → find ZFS Exec, ZFS Setuid, ZFS Devices.

| Dataset | ZFS Exec | ZFS Setuid | ZFS Devices | Why |
|---|---|---|---|---|
| `apps/downloads-incomplete` | Disabled | Disabled | Disabled | Active downloads are untrusted |
| `tank/data` | Disabled | Disabled | Disabled | Downloaded files untrusted until imported. After import, a hardlink exists in both `downloads/` and `media/` — exec=off ensures neither can execute. **exec=off blocks execution only — it does not affect deletion, reading, or writing. All host-side maintenance scripts (cleanup, ClamAV, backup) work normally on this dataset.** |
| All other datasets | Enabled (default) | Disabled | Enabled (default) | App databases and media need normal access |

## Create the Folders

Open **TrueNAS Shell** (click the `>_` icon in the top-right corner). Paste each block and press Enter:

```bash
# App SSD subfolders
mkdir -p /mnt/apps/appdata/{bazarr,clamav,flaresolverr,immich-db,immich-ml,jellyfin,lidarr,navidrome,prowlarr,qbittorrent,radarr,rclone,seerr,sonarr,tailscale,zurg}
mkdir -p /mnt/apps/{scripts,transcode/jellyfin,downloads-incomplete}

# Tank HDD subfolders — all under tank/data (one dataset, instant hardlinks)
mkdir -p /mnt/tank/data/media/{movies,tv,music}
mkdir -p /mnt/tank/data/downloads/complete/{movies,tv,music}
mkdir -p /mnt/tank/data/downloads/quarantine

# Other tank folders
mkdir -p /mnt/tank/photos/library
mkdir -p /mnt/tank/realdebrid/{movies,tv}
mkdir -p /mnt/tank/backups/configs
```

## Set Permissions

```bash
chown -R 568:568 /mnt/apps/appdata /mnt/apps/transcode /mnt/apps/downloads-incomplete
chown -R 568:568 /mnt/tank/data /mnt/tank/photos /mnt/tank/realdebrid
chmod -R 775 /mnt/apps/appdata /mnt/apps/transcode /mnt/apps/downloads-incomplete
chmod -R 775 /mnt/tank/data /mnt/tank/photos /mnt/tank/realdebrid

# Scripts folder — owned by the current logged-in user
SCRIPT_OWNER="$(id -un)"
chown -R "$SCRIPT_OWNER":568 /mnt/apps/scripts
chmod -R 775 /mnt/apps/scripts
```

> [!NOTE]
> **ZFS recordsize optimisation for app databases:**
>
> ZFS default recordsize is 128k. SQLite (Jellyfin, Sonarr, Radarr) and Postgres (Immich) write in 4k–16k page sizes. This mismatch causes write amplification: each small database update triggers a 128k block write, which wears out the SSD faster and fragments database files over time.
>
> Set the recordsize on the appdata dataset to match database page sizes:
> ```bash
> zfs set recordsize=16k apps/appdata
> # Verify:
> zfs get recordsize apps/appdata
> # Output: apps/appdata  recordsize  16K  local
> ```
> This must be done before the dataset has data in it. If appdata already has content, recreate the dataset or accept that existing files keep the old recordsize until rewritten.

> [!WARNING]
> **Immich database — run this LAST, after all other `chown` commands:**
> ```bash
> chown -R 999:999 /mnt/apps/appdata/immich-db
> ```
> The Immich database container runs internally as user 999, not 568. **Always run the general `chown 568` commands first, then this command last.** If you run the 568 chown after the 999 chown, it will overwrite the database permissions and Immich will fail to start.
>
> **Run this again any time you restore from backup, recreate the folder, or use the TrueNAS "Reset Permissions" button on the `apps` dataset** — all of these can revert `immich-db` back to 568:568.

> [!NOTE]
> **Find the GPU render group ID** — run this in TrueNAS Shell and write down the number:
> ```bash
> getent group render | cut -d: -f3
> # Prints a single number, e.g. 107 or 109
> ```
> This number can drift after TrueNAS updates. After every system update, re-run this and compare to `RENDER_GID` in `config.env`. If it changed, update `config.env` and redeploy the stack.

> [!NOTE]
> **Verify `/dev/dri` exists before continuing:**
> ```bash
> ls /dev/dri
> # Should show: card0  renderD128
> # If empty, fix BIOS iGPU settings first (Part 2), then try the Arrow Lake fallback below.
> ```
>
> **Arrow Lake force_probe — optional fallback if `/dev/dri` is still empty:**
>
> The Intel Core Ultra 5 225 uses Arrow Lake. In TrueNAS SCALE 24.10, the i915 driver may not automatically recognise this chip. `ix_diagnostics_force_probe` is a TrueNAS-specific sysctl that maps to the kernel's `i915.force_probe` parameter. For the Core Ultra 5 225, the GPU device ID is **0x7D67**.
>
> Only use this if `/dev/dri` is missing **after** correct BIOS iGPU settings:
>
> 1. TrueNAS: **System Settings → Advanced → Sysctl → Add**
>    - Variable: `ix_diagnostics_force_probe`
>    - Value: `7d67`
> 2. Reboot the NAS.
> 3. Verify: `ls /dev/dri` — should show `card0  renderD128`
> 4. Confirm: `dmesg | grep -i "i915\|force_probe\|render"`
>
> If `7d67` does not work, try the wildcard value `*` (probes all unrecognised Intel GPU IDs — safe on a dedicated NAS).
>
> **This setting survives TrueNAS updates.** Sysctls added via System Settings → Advanced → Sysctl are stored in the TrueNAS configuration database and persist across major OS upgrades. No manual `midclt` commands needed.

---

# Part 5 — Remote Access: Tailscale First

Install Tailscale before anything else. It gives you a secure private tunnel from your phone and laptop to the NAS without opening any router ports.

> [!IMPORTANT]
> **Do not open any router port forwards while building this system.** Not for TrueNAS, not for Jellyfin, not for any app. Tailscale provides all remote access. This is the single most important security decision in the guide.

## Step 5.1 — Create a Tailscale Account and Auth Key

1. Go to [[tailscale.com](https://tailscale.com/)](https://tailscale.com) and create a free account.
2. Go to [[tailscale.com/settings/keys](https://tailscale.com/settings/keys)](https://tailscale.com/settings/keys) → **Generate auth key**.
3. Set: **Reusable** and expiry **No expiry**.

> [!WARNING]
> A one-time-use key works for the first boot but the NAS silently loses its Tailscale identity the next time the container is recreated (after a stack update or TrueNAS upgrade). **Reusable + non-expiring** means the NAS reconnects automatically every time.

4. Copy the key — it looks like `tskey-auth-kXXXXXXXXXXX`. Save it somewhere safe.

> [!NOTE]
> **How Tailscale identity is preserved across restarts:**
>
> The Tailscale container stores its authenticated state in `/var/lib/tailscale` inside the container, which maps to `/mnt/apps/appdata/tailscale` on your SSD. As long as that folder exists, the NAS reconnects after any restart without needing a new auth key.
>
> The nightly config backup (Part 8) backs up this folder to the mirrored HDD, so even an apps SSD failure does not permanently lose your Tailscale identity.

> [!WARNING]
> **Before deleting `/mnt/apps/appdata/tailscale` or doing a "fresh start" migration:**
>
> First go to [[tailscale.com/admin/machines](https://tailscale.com/admin/machines)](https://tailscale.com/admin/machines) and **delete the old `truenas-nas` entry** from the admin console. If you do not do this first, the new container will try to register the same hostname and either fail to authenticate or create a duplicate "truenas-nas (1)" machine. The non-expiring key will still work for re-authentication after deletion.

## Step 5.2 — Connect Your Phone

1. Install the **Tailscale app** on your phone — free on App Store and Google Play.
2. Sign in with the same Tailscale account.
3. Tap the toggle to connect. Leave Tailscale running in the background permanently.

## Step 5.3 — Enable MagicDNS (Recommended)

1. Go to [[tailscale.com/admin/dns](https://tailscale.com/admin/dns)](https://tailscale.com/admin/dns).
2. Click **Enable MagicDNS**. Your NAS will be reachable as `truenas-nas` instead of a number like `100.64.12.34`.

## Step 5.4 — Enable Subnet Routing (Optional)

Subnet routing lets you reach **any device on your home network** (router admin page, printers, smart home hubs) from anywhere via the same Tailscale tunnel.

1. Go to [[tailscale.com/admin/machines](https://tailscale.com/admin/machines)](https://tailscale.com/admin/machines) → find `truenas-nas` → ⋮ menu → **Edit Route Settings**.
2. In TrueNAS Shell, tell the Tailscale container to advertise your home LAN:
```bash
docker exec tailscale tailscale up --advertise-routes=192.168.1.0/24
# Replace 192.168.1.0/24 with your actual home subnet.
# Find it with: ip route | grep -v tailscale | grep src
```
3. Back in the Tailscale admin panel, approve the advertised route in the Edit Route Settings dialog.

> [!TIP]
> Your Tailscale-connected phone can now reach your router admin page, home automation hubs, and any device at home — from anywhere in the world over the same encrypted tunnel.

## Access Model

| App | At home (local) | Away from home | Reasoning |
|---|---|---|---|
| Jellyfin, Immich, Navidrome, Seerr | NAS IP | Tailscale IP | Family watches locally, you watch remotely |
| qBittorrent, Sonarr, Radarr, Prowlarr, etc. | NAS IP | Tailscale IP (admin only) | Management tools — no need to be public |
| TrueNAS web page | NAS IP | Tailscale IP only | Never expose the storage OS to the internet |

---

# Part 6 — The Docker Stack

The entire app stack is defined in two files: `config.env` (your personal settings) and `docker-compose.yml` (the app blueprint).

## Step 6.1 — Create config.env

`config.env` holds all your settings in one place. Every script and container reads from it.

1. Open TrueNAS Shell.
2. Create the scripts folder and open the config file:

```bash
mkdir -p /mnt/apps/scripts
nano /mnt/apps/scripts/config.env
```

3. Paste this template:

```env
TZ="Asia/Jerusalem"
PUID="568"
PGID="568"
RENDER_GID=""

TS_AUTHKEY=""

QBIT_PASSWORD="ChangeMe_qBit123"

IMMICH_DB_PASS="ChangeMe_DB456"
POSTGRES_USER="immich"
POSTGRES_DB="immich"
POSTGRES_PASSWORD="ChangeMe_DB456"
DB_HOSTNAME="immich-db"
DB_USERNAME="immich"
DB_DATABASE_NAME="immich"
DB_PASSWORD="ChangeMe_DB456"
REDIS_HOSTNAME="immich-redis"

IMMICH_MACHINE_LEARNING_GPU_ACCELERATION=openvino
# Pin Immich to a specific version so server and ML image are always in sync.
# Use "release" for the latest stable, or a version tag like "v1.135.3".
# Both images must be the same version — see the version note in Part 6.3.
IMMICH_VERSION=release

WAIT_FOR_RD="0"

ENABLE_USB_BACKUP="1"
USB_UUID=""

WEBHOOK_URL=""
INCOMPLETE_DAYS="14"

ND_TRANSCODINGCACHESIZE="2GB"
```

4. Fill in **your** values:

| Variable | What to put here | Where to find it |
|---|---|---|
| `TZ` | Your timezone, e.g. `Asia/Jerusalem` | [[List of tz database time zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) |
| `RENDER_GID` | **The number from Step 4 — do not leave blank.** Re-check after every TrueNAS update. | Run: `getent group render \| cut -d: -f3` |
| `TS_AUTHKEY` | Your Tailscale auth key | From tailscale.com/settings/keys — starts with `tskey-auth-k...` |
| `QBIT_PASSWORD` | A strong password for qBittorrent | You choose — 12+ characters |
| `IMMICH_DB_PASS` + `POSTGRES_PASSWORD` + `DB_PASSWORD` | **One strong password for all three** | You choose — all three must be identical or Immich breaks |
| `IMMICH_MACHINE_LEARNING_GPU_ACCELERATION` | Leave as `openvino` | Tells the Immich ML container to use Intel Arc for face/clip encoding |
| `IMMICH_VERSION` | Leave as `release` for latest, or pin to e.g. `v1.135.3` | Must match the version used for all Immich images. See Part 6.3 warning. |
| `USB_UUID` | Leave blank for now | Fill in Part 9 after plugging in a USB drive |
| `WEBHOOK_URL` | Leave blank for now | Optional — fill in Part 12 for alerts |

5. Press `Ctrl+X → Y → Enter` to save.

> [!WARNING]
> `IMMICH_DB_PASS`, `POSTGRES_PASSWORD`, and `DB_PASSWORD` must all be the **same value**. `IMMICH_DB_PASS` is used by scripts, `POSTGRES_PASSWORD` is read by the database container, and `DB_PASSWORD` is used by the Immich server to connect to the database. If any one differs, Immich will fail to connect to its own database.

## Step 6.2 — Create docker-compose.yml

This file tells Docker which apps to run, which folders they access, which ports they use, and which containers can communicate.

> [!IMPORTANT]
> **Hardlink rule:** qBittorrent, Sonarr, Radarr, and Lidarr all mount `/mnt/tank/data` as `/data` inside the container. This is deliberate — completed downloads and final media are in the same ZFS dataset, so imports are instant hardlinks instead of slow copies.

> [!NOTE]
> **Docker subnet note:** The compose uses explicit `172.31.x.x` subnets so Docker does not randomly pick a range that overlaps your home LAN or Tailscale.
>
> **Never use `172.17.0.0/16`** — that is Docker's default bridge range and is almost always already in use. If you need to change the `172.31.x.x` ranges, pick something in `172.20.x.x` through `172.30.x.x`. Tailscale uses `100.64.x.x` (CGNAT space) and does not conflict.

1. In TrueNAS Shell:

```bash
nano /mnt/apps/scripts/docker-compose.yml
```

2. Paste the complete compose file:

```yaml
# version key removed — deprecated in Docker Compose V2+

x-logging: &default-logging
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"

networks:
  download-net:
    driver: bridge
    ipam:
      config:
        - subnet: 172.31.10.0/24
  media-net:
    driver: bridge
    ipam:
      config:
        - subnet: 172.31.20.0/24
  request-net:
    driver: bridge
    ipam:
      config:
        - subnet: 172.31.30.0/24
  subtitle-net:
    driver: bridge
    ipam:
      config:
        - subnet: 172.31.40.0/24

services:

  # ── TAILSCALE ──────────────────────────────────────────────────────────
  tailscale:
    image: tailscale/tailscale:latest
    container_name: tailscale
    hostname: truenas-nas
    env_file: [/mnt/apps/scripts/config.env]
    environment: [TS_STATE_DIR=/var/lib/tailscale, TS_USERSPACE=false, TS_AUTH_ONCE=true]
    volumes:
      - /mnt/apps/appdata/tailscale:/var/lib/tailscale
      - /dev/net/tun:/dev/net/tun
    cap_add: [NET_ADMIN, NET_RAW]
    network_mode: host
    logging: *default-logging
    restart: unless-stopped

  # ── REAL-DEBRID (Phase 2 — already commented out; uncomment in Part 13) ─
  # zurg:
  #   image: ghcr.io/debridmediamanager/zurg-testing:latest
  #   container_name: zurg
  #   restart: unless-stopped
  #   # Zurg only needs its config folder and a port to serve WebDAV.
  #   # Do NOT mount /mnt/tank/realdebrid here — rclone-mount.sh manages
  #   # that host path. Mounting it from both Zurg and rclone causes a
  #   # FUSE "transport endpoint is not connected" error.
  #   volumes:
  #     - /mnt/apps/appdata/zurg:/config
  #       # /config is Zurg's internal database and cache — NOT your media folder.
  #       # Your Real-Debrid media is served as a virtual WebDAV endpoint, not stored here.
  #   ports: ["9999:9999"]
  #   networks: [download-net]
  #   logging: *default-logging

  # ── DOWNLOADERS ────────────────────────────────────────────────────────
  qbittorrent:
    image: lscr.io/linuxserver/qbittorrent:latest
    container_name: qbittorrent
    env_file: [/mnt/apps/scripts/config.env]
    environment: [WEBUI_PORT=8090]
    volumes:
      - /mnt/apps/appdata/qbittorrent:/config
      - /mnt/tank/data:/data
      - /mnt/apps/downloads-incomplete:/downloads/incomplete
    ports: ["8090:8090"]
    networks: [download-net]
    logging: *default-logging
    restart: unless-stopped

  clamav:
    image: clamav/clamav:latest
    container_name: clamav
    environment: [CLAMAV_NO_MILTERD=true]
    volumes:
      - /mnt/apps/appdata/clamav:/var/lib/clamav
      - /mnt/tank/data/downloads/complete:/scandir
      - /mnt/tank/data/downloads/quarantine:/quarantine
    networks: [download-net]
    logging: *default-logging
    restart: unless-stopped

  # ── CLOUDFLARE BYPASS ──────────────────────────────────────────────────
  flaresolverr:
    image: ghcr.io/flaresolverr/flaresolverr:latest
    container_name: flaresolverr
    environment: [LOG_LEVEL=info]
    ports: ["8191:8191"]
    networks: [download-net]
    logging: *default-logging
    restart: unless-stopped

  # ── INDEXERS / AUTOMATION ──────────────────────────────────────────────
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:latest
    container_name: prowlarr
    env_file: [/mnt/apps/scripts/config.env]
    volumes: ["/mnt/apps/appdata/prowlarr:/config"]
    ports: ["9696:9696"]
    networks: [download-net, request-net]
    logging: *default-logging
    restart: unless-stopped

  sonarr:
    image: lscr.io/linuxserver/sonarr:latest
    container_name: sonarr
    env_file: [/mnt/apps/scripts/config.env]
    volumes:
      - /mnt/apps/appdata/sonarr:/config
      - /mnt/tank/data:/data
    ports: ["8989:8989"]
    networks: [download-net, media-net, subtitle-net, request-net]
    logging: *default-logging
    restart: unless-stopped

  radarr:
    image: lscr.io/linuxserver/radarr:latest
    container_name: radarr
    env_file: [/mnt/apps/scripts/config.env]
    volumes:
      - /mnt/apps/appdata/radarr:/config
      - /mnt/tank/data:/data
    ports: ["7878:7878"]
    networks: [download-net, media-net, subtitle-net, request-net]
    logging: *default-logging
    restart: unless-stopped

  lidarr:
    image: lscr.io/linuxserver/lidarr:latest
    container_name: lidarr
    env_file: [/mnt/apps/scripts/config.env]
    volumes:
      - /mnt/apps/appdata/lidarr:/config
      - /mnt/tank/data:/data
    ports: ["8686:8686"]
    networks: [download-net]
    logging: *default-logging
    restart: unless-stopped

  bazarr:
    image: lscr.io/linuxserver/bazarr:latest
    container_name: bazarr
    env_file: [/mnt/apps/scripts/config.env]
    volumes:
      - /mnt/apps/appdata/bazarr:/config
      - /mnt/tank/data:/data
      # Bazarr must use the same /data mount as Sonarr and Radarr.
      # Sonarr/Radarr report file paths starting with /data/media/...
      # If Bazarr cannot see /data, it cannot find those files to subtitle.
    ports: ["6767:6767"]
    networks: [subtitle-net, download-net]
    # download-net is required for Bazarr to reach external APIs
    # (OpenSubtitles, etc.). subtitle-net alone has no guaranteed internet egress.
    logging: *default-logging
    restart: unless-stopped

  # ── MEDIA SERVERS ──────────────────────────────────────────────────────
  jellyfin:
    image: lscr.io/linuxserver/jellyfin:latest
    container_name: jellyfin
    env_file: [/mnt/apps/scripts/config.env]
    devices: [/dev/dri:/dev/dri]
    group_add: ["${RENDER_GID:-107}"]
    volumes:
      - /mnt/apps/appdata/jellyfin:/config
      - /mnt/apps/transcode/jellyfin:/transcode
      - /mnt/tank/data/media/movies:/media/movies:ro
      - /mnt/tank/data/media/tv:/media/tv:ro
      - /mnt/tank/realdebrid:/media/realdebrid:ro,shared
        # :shared enables bind propagation so rclone mounts made AFTER the
        # container starts are visible inside the container. Without this,
        # Jellyfin sees an empty folder even when rclone is working correctly.
      - /mnt/apps/scripts:/mnt/apps/scripts
        # Mounts the scripts folder so the wait script can see the .rd-mounted
        # marker written by rclone-mount.sh, and so the wait script itself is
        # accessible. Does NOT expose credentials outside the container.
      - /mnt/apps/scripts/jellyfin-wait-for-rd.sh:/custom-cont-init.d/10-wait-for-rd.sh
        # No :ro here — LinuxServer's fix-attrs process chowns everything in
        # custom-cont-init.d at startup. Read-only causes a permission error
        # and crashes the container before Jellyfin ever starts.
    ports: ["8096:8096"]
    networks: [media-net, request-net]
    logging: *default-logging
    restart: unless-stopped

  navidrome:
    image: deluan/navidrome:latest
    container_name: navidrome
    user: "568:568"
    env_file: [/mnt/apps/scripts/config.env]
    volumes:
      - /mnt/apps/appdata/navidrome:/data
      - /mnt/tank/data/media/music:/music:ro
    ports: ["4533:4533"]
    networks: [media-net]
    logging: *default-logging
    restart: unless-stopped

  # ── PHOTOS ─────────────────────────────────────────────────────────────
  immich-server:
    image: ghcr.io/immich-app/immich-server:${IMMICH_VERSION:-release}
    container_name: immich-server
    env_file: [/mnt/apps/scripts/config.env]
    devices: [/dev/dri:/dev/dri]
    group_add: ["${RENDER_GID:-107}"]
    environment:
      - IMMICH_MACHINE_LEARNING_URL=http://immich-machine-learning:3003
    volumes:
      - /mnt/tank/photos/library:/data
    ports: ["2283:2283"]
    depends_on:
      immich-db:
        condition: service_healthy   # waits for postgres to be READY, not just started
      immich-redis:
        condition: service_started
    networks: [media-net, request-net]
    logging: *default-logging
    restart: unless-stopped

  immich-machine-learning:
    image: ghcr.io/immich-app/immich-machine-learning:${IMMICH_VERSION:-release}-openvino
    # The -openvino variant bundles Intel OpenVINO runtime for hardware face/clip encoding.
    # It must be the same version as immich-server. Both use IMMICH_VERSION from config.env.
    # Example: if IMMICH_VERSION=v1.135.3, images become:
    #   immich-server:v1.135.3
    #   immich-machine-learning:v1.135.3-openvino
    container_name: immich-machine-learning
    env_file: [/mnt/apps/scripts/config.env]
    devices: [/dev/dri:/dev/dri]
    group_add: ["${RENDER_GID:-107}"]
    volumes: ["/mnt/apps/appdata/immich-ml:/cache"]
    networks: [media-net]
    logging: *default-logging
    restart: unless-stopped

  immich-redis:
    image: docker.io/valkey/valkey:9
    container_name: immich-redis
    networks: [media-net]
    logging: *default-logging
    restart: unless-stopped

  immich-db:
    image: ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0
    container_name: immich-db
    env_file: [/mnt/apps/scripts/config.env]
    environment: [POSTGRES_INITDB_ARGS=--data-checksums]
    volumes: ["/mnt/apps/appdata/immich-db:/var/lib/postgresql/data"]
    networks: [media-net]
    shm_size: 128mb
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -d $${POSTGRES_DB} -U $${POSTGRES_USER}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s
    logging: *default-logging
    restart: unless-stopped

  # ── REQUESTS ───────────────────────────────────────────────────────────
  seerr:
    image: ghcr.io/seerr-team/seerr:latest
    container_name: seerr
    init: true
    user: "568:568"
    # If Seerr fails to start, remove the user: line above — some image versions
    # require root to initialise the /app/config directory on first run.
    env_file: [/mnt/apps/scripts/config.env]
    environment: [LOG_LEVEL=info, PORT=5055]
    volumes: ["/mnt/apps/appdata/seerr:/app/config"]
    healthcheck:
      test: curl -sf http://localhost:5055/api/v1/settings/public > /dev/null || exit 1
      start_period: 20s
      timeout: 3s
      interval: 15s
      retries: 3
    ports: ["5055:5055"]
    networks: [request-net, media-net]
    logging: *default-logging
    restart: unless-stopped
```

3. Press `Ctrl+X → Y → Enter` to save.

## Step 6.3 — Deploy via TrueNAS Apps UI

> [!WARNING]
> **Create the wait script placeholder before deploying.** The compose mounts `/mnt/apps/scripts/jellyfin-wait-for-rd.sh` into Jellyfin. If this file does not exist when Docker starts, Docker creates a **directory** at that path inside the container instead of a file — and Jellyfin crashes before it starts.
>
> Create the placeholder now (it simply exits immediately — the real logic is added in Part 13):
> ```bash
> cat > /mnt/apps/scripts/jellyfin-wait-for-rd.sh << 'PLACEHOLDER'
> #!/usr/bin/with-contenv bash
> # Placeholder — replaced with Real-Debrid wait logic in Part 13.
> # When WAIT_FOR_RD=0 (the default), this script exits immediately.
> exit 0
> PLACEHOLDER
> chmod 755 /mnt/apps/scripts/jellyfin-wait-for-rd.sh
> chown root:root /mnt/apps/scripts/jellyfin-wait-for-rd.sh
> ```

> [!NOTE]
> Do not use `docker compose up -d` as the normal way to run the stack. TrueNAS should own app deployment so it can manage updates and restarts.

1. In TrueNAS, click **Apps** in the left sidebar.
2. Click **Discover Apps**.
3. Click the ⋮ menu in the top-right area.
4. Click **"Install via YAML"**.
5. Name: `media-stack`
6. In the YAML box, paste the contents of the compose file. Or use:

```yaml
include:
  - /mnt/apps/scripts/docker-compose.yml
```

7. Click **Save**. Wait for TrueNAS to deploy all containers.
8. Go to **Apps → Installed**. Confirm `media-stack` shows all containers as Running.

> [!WARNING]
> **Validate Immich versions before deployment.** Immich updates frequently. Before running the stack:
>
> 1. Check the current official Immich compose at [[immich.app/docs/install/docker-compose](https://immich.app/docs/install/docker-compose)](https://immich.app/docs/install/docker-compose)
> 2. Verify the `postgres` image tag matches what Immich currently recommends (`ghcr.io/immich-app/postgres:14-vectorchord...`)
> 3. Check that `DB_HOSTNAME`, `DB_USERNAME`, `DB_DATABASE_NAME`, `DB_PASSWORD`, and `REDIS_HOSTNAME` are still the correct environment variable names
> 4. Verify the OpenVINO ML image tag format (`vX.Y.Z-openvino`) is still valid for your version

> [!NOTE]
> **`immich-server` and `immich-machine-learning` must be the same version.** They are separate images built from the same Immich source. If they diverge, API calls between them fail.
>
> This guide uses `IMMICH_VERSION` in `config.env` (default: `release`) so both always share the same tag:
> ```
> immich-server:${IMMICH_VERSION:-release}
> immich-machine-learning:${IMMICH_VERSION:-release}-openvino
> ```
>
> **For production stability, pin a specific version** instead of `release`:
> 1. Find the latest version at [[github.com/immich-app/immich/releases](https://github.com/immich-app/immich/releases)](https://github.com/immich-app/immich/releases)
> 2. In `config.env`, set `IMMICH_VERSION=v1.135.3` (use the actual version number)
> 3. To update: change the version, redeploy `media-stack` from TrueNAS Apps
>
> **To check current running versions:**
> ```bash
> docker inspect immich-server --format '{{.Config.Image}}'
> docker inspect immich-machine-learning --format '{{.Config.Image}}'
> # Both should show the same version number
> ```

### Verify the Stack Started

Wait 2-3 minutes after deployment, then check **Apps → Installed → media-stack**. Most containers should show Running.

```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
# Most should show "Up (X minutes)"
# Zurg will show Restarting until Part 13 — expected

# If any app fails, check logs:
docker logs jellyfin | tail -30
```
---

# Part 7 — ZFS Snapshots

Snapshots are save points. TrueNAS photographs your data automatically so you can roll back if an update breaks something.

> [!IMPORTANT]
> **Snapshot retention warning:** ZFS snapshots hold old disk blocks even after files are deleted. If you delete a 50 GB file while a snapshot still references it, that 50 GB stays on disk until the snapshot expires. Keep `tank/data` retention at 14 days or less at first.

> [!WARNING]
> Do **NOT** install `sanoid` via `apt-get` or any package manager on TrueNAS SCALE. It is unsupported and a system update can break or remove it. Use the built-in UI snapshots below.

## Create Snapshot Tasks in TrueNAS UI

**Data Protection → Periodic Snapshot Tasks → Add**

> [!IMPORTANT]
> When creating each task, always tick the **Recursive** checkbox. Without it, any sub-datasets you create later (e.g. `tank/data/4k`) will be silently excluded from snapshots.

Form fields: Dataset (type or select), **Recursive (tick Yes — see above)**, Snapshot Lifetime (how long to keep each), Schedule (how often).

| Dataset | Recursive | Schedule | Retention | Why |
|---|---|---|---|---|
| `apps/appdata` | Yes | Every 4 hours | 7 days | App configs change often — roll back quickly if an update breaks an app |
| `tank/photos` | Yes | Daily at 01:00 | 30 days | Photos are precious — long retention |
| `tank/data` | Yes | Daily at 01:30 | 14 days | Media and downloads — keep retention short (see warning above) |
| `tank/backups` | Yes | Daily at 02:00 | 30 days | Config tarballs — long retention |

## How to Roll Back a Snapshot

1. **Storage → find the dataset → Snapshots**
2. Find the snapshot from before the problem. Click it.
3. Select **Rollback**. TrueNAS asks you to confirm by typing exactly what it shows in the dialog.
4. The dataset rolls back. Restart affected apps from **Apps → Installed**.

> [!WARNING]
> Rolling back destroys all changes made after that snapshot. Only roll back the specific dataset with the problem — never the entire `tank` pool unless you truly mean to revert all your media.

---

# Part 8 — Maintenance Scripts

Five scripts automate predictable, low-risk maintenance tasks. None of them delete your media library. All use `set -Eeuo pipefail` — they fail loudly if something goes wrong instead of silently continuing.

> [!NOTE]
> **How to create these scripts:** Open TrueNAS Shell, run the `nano` command shown, paste the script content, then press `Ctrl+X → Y → Enter` to save.
> ```bash
> chmod +x /mnt/apps/scripts/script-name.sh  # make it runnable
> bash /mnt/apps/scripts/script-name.sh       # test it immediately
> ```

## Script 1: `backup-app-config.sh` — Nightly SSD Backup

The most important script. The apps SSD has no redundancy. This backs it up to the mirrored HDD pool every night.

```bash
nano /mnt/apps/scripts/backup-app-config.sh
```

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

SRC="/mnt/apps/appdata"
SCRIPT_SRC="/mnt/apps/scripts"
DEST="/mnt/tank/backups/configs"
DATE="$(date +%F_%H-%M-%S)"
LOG="/mnt/apps/scripts/backup-app-config.log"

mkdir -p "$DEST"
echo "[backup] Starting $DATE" >> "$LOG"
tar -czf "$DEST/app-config-$DATE.tar.gz" "$SRC" "$SCRIPT_SRC" >> "$LOG" 2>&1
find "$DEST" -name "app-config-*.tar.gz" -type f -mtime +30 -print -delete >> "$LOG" 2>&1
echo "[backup] Finished $DATE" >> "$LOG"
```

```bash
chmod +x /mnt/apps/scripts/backup-app-config.sh
```

## Script 2: `photo-backup-usb.sh` — USB Drive Photo Backup

Creates a physical copy of your photos on a USB drive. The USB is unmounted when not in use so it cannot be affected by ransomware.

First, plug in your USB drive and find its UUID:

```bash
blkid
# Find the line matching your USB drive (small size, type ext4 or exfat)
# Copy the UUID value — looks like: a1b2c3d4-e5f6-7890-abcd-123456789012
# Add it to config.env: USB_UUID="your-uuid-here"
```

```bash
nano /mnt/apps/scripts/photo-backup-usb.sh
```

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

source /mnt/apps/scripts/config.env

[ "${ENABLE_USB_BACKUP:-0}" != "1" ] && exit 0
[ -z "${USB_UUID:-}" ] && exit 0

MOUNT=/mnt/usb-photo-backup
mkdir -p "$MOUNT"
mount UUID="$USB_UUID" "$MOUNT"

if ! mountpoint -q "$MOUNT"; then
  echo "[photo-backup] USB drive did not mount. Stopping."
  exit 1
fi

trap 'umount "$MOUNT" 2>/dev/null || true' EXIT

# Ensure the destination directory exists on the USB drive.
# rsync -a does NOT create the destination directory itself.
mkdir -p "$MOUNT/photos"
rsync -a --ignore-existing --no-perms /mnt/tank/photos/library/ "$MOUNT/photos/"
sync
```

```bash
chmod +x /mnt/apps/scripts/photo-backup-usb.sh
```

## Script 3: `cleanup-downloads.sh` — Daily Junk Removal

Removes torrent junk files and stale incomplete downloads. Does NOT touch your media library.

```bash
nano /mnt/apps/scripts/cleanup-downloads.sh
```

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

source /mnt/apps/scripts/config.env

COMPLETE="/mnt/tank/data/downloads/complete"
INCOMPLETE="/mnt/apps/downloads-incomplete"
LOG="/mnt/apps/scripts/cleanup-downloads.log"
INCOMPLETE_DAYS="${INCOMPLETE_DAYS:-14}"

echo "[cleanup] Starting $(date)" >> "$LOG"

# Delete torrent junk (NFO, SFV, screenshots, sample clips).
# The \( ... \) parentheses group the -iname alternatives so that -o (OR)
# binds only within the group, not across the whole expression.
# Without them, only the first -iname would be affected by -not -path and -type.
# -not -path '*/.*' skips hidden folders including .zfs snapshot directories.
find "$COMPLETE" -not -path '*/.*' -type f \( \
  -iname "*.nfo" -o -iname "*.sfv" -o -iname "*.url" -o \
  -iname "*.txt" -o -iname "*sample*" -o -iname "*featurette*" \
\) -print -delete >> "$LOG" 2>&1

# Delete stale incomplete downloads
find "$INCOMPLETE" -not -path '*/.*' -type f -mtime +"$INCOMPLETE_DAYS" -print -delete >> "$LOG" 2>&1

# Remove empty folders
find "$COMPLETE" "$INCOMPLETE" -not -path '*/.*' -type d -empty -print -delete >> "$LOG" 2>&1

echo "[cleanup] Finished $(date)" >> "$LOG"
```

```bash
chmod +x /mnt/apps/scripts/cleanup-downloads.sh
```

## Script 4: `scan-downloads.sh` — Scheduled Virus Scan

Runs ClamAV inside its container to scan completed downloads.

```bash
nano /mnt/apps/scripts/scan-downloads.sh
```

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

LOG="/mnt/apps/scripts/clamav-scan.log"

echo "[clamav] Starting $(date)" >> "$LOG"

# /scandir and /quarantine are paths INSIDE the ClamAV container.
# They are mapped from the host via the compose volumes:
#   /mnt/tank/data/downloads/complete  → /scandir     (read-write: source files)
#   /mnt/tank/data/downloads/quarantine → /quarantine  (read-write: infected files moved here)
# Do NOT create /scandir or /quarantine on the host manually —
# they already exist from Part 4 and are mounted by the compose.
#
# ClamAV exits 1 when it finds threats (by design) — || true prevents
# set -Eeuo pipefail from treating a detection as a script error.
# The log will contain "Moved to QUARANTINE: ..." for any infected file.
# --exclude-dir skips .zfs snapshot directories inside the scan path.
docker exec clamav clamscan --recursive   --exclude-dir='(^|/)\.zfs'   --move="/quarantine" --quiet "/scandir" >> "$LOG" 2>&1 || true

echo "[clamav] Finished $(date)" >> "$LOG"
```

```bash
chmod +x /mnt/apps/scripts/scan-downloads.sh
```

## Script 5: `health-check.sh` — Container Status Log

Logs which containers are running. Read-only — it does NOT auto-restart anything. Auto-restart hides failures; logs let you see that something keeps crashing.

```bash
nano /mnt/apps/scripts/health-check.sh
```

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

source /mnt/apps/scripts/config.env

LOG="/mnt/apps/scripts/health-check.log"
WEBHOOK_URL="${WEBHOOK_URL:-}"
APPS="jellyfin navidrome immich-server immich-db immich-redis immich-machine-learning qbittorrent prowlarr sonarr radarr lidarr bazarr seerr clamav flaresolverr tailscale"

# Rotate log: keep only the last 500 lines to prevent unbounded growth.
# The log runs every 10 minutes — 500 lines ≈ 3 days of history.
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 500 ]; then
  tail -400 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

echo "[health] Check $(date)" >> "$LOG"

for app in $APPS; do
  # Use docker inspect for real state — docker ps also lists restarting containers
  # as if they were running, masking crash loops.
  STATE=$(docker inspect --format '{{.State.Status}}' "$app" 2>/dev/null || echo "missing")
  if [ "$STATE" = "running" ]; then
    echo "[health] OK: $app" >> "$LOG"
  else
    echo "[health] DOWN: $app (state: $STATE)" >> "$LOG"
    if [ -n "$WEBHOOK_URL" ]; then
      MSG=$(printf '{"text": "NAS: %s is down (state: %s)"}' "$app" "$STATE")
      curl -sf -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" -d "$MSG" || true
    fi
  fi
done
```

```bash
chmod +x /mnt/apps/scripts/health-check.sh
```

## Schedule All Scripts

**System Settings → Advanced Settings → Cron Jobs → Add**

For each job: fill in Description, paste the Command exactly, set Run as User to `root`, use the Schedule string.

| Description | Command | Schedule | Time |
|---|---|---|---|
| Health check | `bash /mnt/apps/scripts/health-check.sh` | `*/10 * * * *` | Every 10 min |
| Backup app configs | `bash /mnt/apps/scripts/backup-app-config.sh` | `0 3 * * *` | 3:00 AM daily |
| Cleanup downloads | `bash /mnt/apps/scripts/cleanup-downloads.sh` | `0 4 * * *` | 4:00 AM daily |
| Virus scan | `bash /mnt/apps/scripts/scan-downloads.sh` | `30 4 * * *` | 4:30 AM daily |
| Photo USB backup | `bash /mnt/apps/scripts/photo-backup-usb.sh` | `0 6 * * *` | 6:00 AM daily |

> [!NOTE]
> **Maintenance job schedule rule** — do not let jobs overlap:
> - `03:00` — Config backup (before anything else)
> - `04:00` — Cleanup downloads (low I/O)
> - `04:30` — Virus scan (heavy I/O on downloads folder)
> - `06:00` — USB photo backup
> - `05:15` — Jellyfin internal tasks (Database Optimization, Library Refresh, Metadata)
> - `05:45` — Immich background jobs (Machine Learning, Thumbnail Generation, Smart Search)
>
> Set the Jellyfin and Immich internal scheduled tasks in their respective dashboards, not here.

---

# Part 9 — First-Time App Setup

Set up apps in this order. Each step builds on the previous one.

> [!NOTE]
> When the guide says "open qBittorrent", go to `http://[NAS-IP]:8090` in your browser. Replace `[NAS-IP]` with your actual server IP address, e.g. `192.168.1.50`. See the [[Quick Reference table in Part 18](https://github.com/fp581/TrueNAS-SCALE-Home-Media-Server/wiki/TrueNAS-SCALE-Home-Media-Server/_edit#part-18--quick-reference)](#part-18--quick-reference) for all app URLs.

## 9.1 — qBittorrent

Downloads torrent files to `/mnt/apps/downloads-incomplete/` (SSD), then moves completed files to `/mnt/tank/data/downloads/complete/` (HDD).

> [!IMPORTANT]
> `linuxserver/qbittorrent` generates a **random password** on first start. Find it before you can log in:
> ```bash
> docker logs qbittorrent 2>&1 | grep -i password
> # Output: "A temporary password is provided for this session: AbCdEf1234"
> ```

1. Open qBittorrent at `http://[NAS-IP]:8090`. Log in with username `admin` and the temporary password.
2. **Settings (gear icon) → Web UI → Authentication** → change password to your `QBIT_PASSWORD` from `config.env`. Save.
3. **Settings → Downloads** → Default Save Path: `/data/downloads/complete`
4. Enable "Keep incomplete torrents in:" → `/downloads/incomplete`
5. **Tools → Torrent Categories → Add** — create these three:

| Category | Save path |
|---|---|
| `movies` | `/data/downloads/complete/movies` |
| `tv` | `/data/downloads/complete/tv` |
| `music` | `/data/downloads/complete/music` |

## 9.2 — Prowlarr

Central search engine that manages indexers and shares them with Sonarr, Radarr, and Lidarr.

1. Open Prowlarr at `http://[NAS-IP]:9696`. Complete the setup wizard and create an admin account.
2. **Settings → General → Authentication** → Forms-based, enter username and password, restart Prowlarr.

### Step A — Register FlareSolverr as a Proxy

Many public torrent sites (1337x, EZTV, YTS) sit behind Cloudflare bot protection. Without FlareSolverr, Prowlarr gets a 403 Forbidden error. Register the proxy first.

1. **Settings → Indexers → Proxies tab → Add Proxy (+)**
2. Choose FlareSolverr.
3. Name: `FlareSolverr` — Host: `http://flaresolverr:8191` — click Test (green) → Save.

### Step B — Add Indexers

| Indexer | Priority | How to add | Notes |
|---|---|---|---|
| Real-Debrid via Prowlarr | N/A | **Do not add in beginner build** — use Zurg/rclone for Jellyfin library mounting instead | Prowlarr does not become Debrid-aware just because Zurg exists |
| Torrentio / Debrid bridge | N/A | **Optional advanced only** — skip in beginner build | A Stremio manifest URL is not a normal Prowlarr Torznab URL |
| YTS | 25 | Search "YTS" → Test → Save | Good quality for movies |
| 1337x | 25 | Search "1337x" → Test → Save → assign FlareSolverr proxy | General fallback |
| EZTV | 25 | Search "EZTV" → Test → Save → assign FlareSolverr proxy | TV shows fallback |
| Redacted (optional) | 10 | Requires account at [[redacted.ch](https://redacted.ch/)](https://redacted.ch) → enter credentials → Priority: 10 | Lossless FLAC music |
| Orpheus (optional) | 10 | Requires account at [[orpheus.network](https://orpheus.network/)](https://orpheus.network) → Priority: 10 | Lossless FLAC music |

> [!IMPORTANT]
> **Assign FlareSolverr to ALL public torrent indexers immediately** — do not wait for a 403 error. Cloudflare protection changes dynamically.
>
> After saving any public indexer (YTS, 1337x, EZTV): click the pencil (edit) icon → scroll to the Proxy dropdown → select FlareSolverr → Save.
>
> Private trackers (Redacted, Orpheus) usually do not need it.

> [!NOTE]
> **Priority summary for this beginner build:** music indexers around 10, public torrent indexers around 25. Lower number = tried first. Add Debrid-aware automation only later as a separate advanced phase.

## 9.3 — Sonarr

Monitors TV shows, automatically downloads new episodes as they air.

1. Open Sonarr at `http://[NAS-IP]:8989`. **Settings → General → Security → Authentication Required** → Forms-based → username + password. Restart Sonarr.
2. Connect to qBittorrent: **Settings → Download Clients → + → qBittorrent:**

| Field | Value |
|---|---|
| Host | `qbittorrent` (container name — never use the NAS IP for container-to-container) |
| Port | `8090` |
| Username | `admin` |
| Password | your `QBIT_PASSWORD` |
| Category | `tv` |

Click **Test** (green) → **Save**.

3. Connect to Prowlarr: In Sonarr, **Settings → General** → copy the **API Key**. Open Prowlarr → **Settings → Apps → + → Sonarr** → paste the API key → Test → Save. Prowlarr now pushes all indexers to Sonarr automatically.
4. Root folder: **Settings → Media Management → Root Folders → +** → type `/data/media/tv`
5. Naming format: **Settings → Media Management → Rename Episodes: ON** → Episode Format field:

```
{Series Title} - S{season:00}E{episode:00} - {Episode Title}
```

This creates files like: `Breaking Bad - S01E01 - Pilot.mkv` — essential for subtitle matching.

## 9.4 — Radarr

Same as Sonarr but for movies.

1. Open Radarr at `http://[NAS-IP]:7878`. Same authentication setup.
2. Same qBittorrent connection — change Category to: `movies`
3. Same Prowlarr sync — copy Radarr API key from **Settings → General**, add in Prowlarr → **Settings → Apps → + → Radarr**.
4. Root folder: `/data/media/movies`
5. Naming format: **Settings → Media Management → Rename Movies: ON** → Movie Format:

```
{Movie Title} ({Release Year})
```

## 9.5 — Lidarr

Follows artists and automatically downloads new albums and discographies.

1. Open Lidarr at `http://[NAS-IP]:8686`. Same authentication setup.
2. Same qBittorrent connection — Category: `music`
3. Connect to Prowlarr: In Lidarr, **Settings → General** → copy the **API Key**. Open Prowlarr → **Settings → Apps → + → Lidarr** → paste the Lidarr API key → Test → Save. Prowlarr now pushes all indexers to Lidarr automatically.
4. Root folder: `/data/media/music`
5. Naming: **Settings → Media Management:**
   - Artist folder: `{Artist Name}`
   - Album folder: `{Album Title} ({Release Year})`
   - Track format: `{track:00} - {Track Title}`
6. Quality profile: **Settings → Profiles → Quality Profiles → +** → Name: `Lossless` → FLAC at top, then MP3 320 kbps → cutoff: MP3 192 kbps (rejects anything below) → Save.

> [!TIP]
> **How to add music to your library:**
> 1. Open Lidarr → **Artists → Add New**
> 2. Search for any artist name, e.g. `Radiohead`
> 3. Set Quality Profile: `Lossless`, Root Folder: `/data/media/music`
> 4. Click **Add Artist**
> 5. Lidarr finds all albums, qBittorrent downloads them, Navidrome picks them up within minutes
> 6. New albums by that artist download automatically on release day — you never need to do anything again

## 9.6 — Bazarr

Automatically downloads subtitles for everything Sonarr and Radarr manage.

1. Open Bazarr at `http://[NAS-IP]:6767`.
2. **Settings → Sonarr → Enable** → Hostname: `sonarr`, Port: `8989` → paste API key (find it in Sonarr at **Settings → General**, copy the API Key field) → Test → Save.
3. **Settings → Radarr → Enable** → Hostname: `radarr`, Port: `7878` → paste API key (find it in Radarr at **Settings → General**) → Test → Save.
4. **Settings → Languages → +** → add your language → **+** → add `English` → Save.
5. **Settings → Providers → + → OpenSubtitles.com** → register free at [[opensubtitles.com](https://opensubtitles.com/)](https://opensubtitles.com) → enter credentials → Save.

## 9.7 — Jellyfin

Streams your local media and Real-Debrid content to any device.

> [!NOTE]
> The paths below are **container paths** — Docker translates them to your actual hard drive folders automatically. `/media/movies` inside the container maps to `/mnt/tank/data/media/movies` on your drives.

1. Open Jellyfin at `http://[NAS-IP]:8096`. The first-time setup wizard appears. Create an admin account.
2. When asked for media libraries, add two for now:
   - Type: Movies → Folder: `/media/movies` → Name: `Movies`
   - Type: Shows → Folder: `/media/tv` → Name: `TV Shows`

   You will add the Real-Debrid libraries (`/media/realdebrid/movies` and `/media/realdebrid/tv`) in Part 13 after it is working.

3. Finish the wizard.

4. Enable hardware video transcoding: **Administration Dashboard** (person icon top-right → Dashboard) → **Playback → Hardware Acceleration** → select **VAAPI** → VA-API Device: `/dev/dri/renderD128` → tick all codec checkboxes (H264, HEVC, VP8, VP9, AV1, MPEG2) → tick **Enable Tone Mapping** → Transcoding temp path: `/transcode` → **Save**.

> [!NOTE]
> **VA-API vs QuickSync (QSV) for Intel Core Ultra / Arrow Lake:**
>
> Arrow Lake uses the `xe` kernel driver, which is newer than the `i915` driver QuickSync (QSV) was built around. VA-API works through the standard Linux GPU abstraction layer and is fully supported on `xe`. QSV on Arrow Lake can be unstable or produce errors like "Failed to create a MFX session".
>
> **Always choose VA-API for this hardware.** The `/dev/dri/renderD128` device path is correct for both — only the acceleration method in the dropdown changes.

5. **Validate GPU access** — run this in TrueNAS Shell to confirm Jellyfin can actually reach the render device:
```bash
docker exec jellyfin ls -l /dev/dri
# You should see renderD128 owned by a group ID matching your RENDER_GID in config.env.
# Example:
#   crw-rw---- 1 root render 226, 128 ... renderD128
# If the group shown does not match your RENDER_GID, hardware transcoding will silently
# fail. Update RENDER_GID in config.env and redeploy the stack.
```
6. Install the Playback Reporting plugin: **Administration Dashboard → Plugins → Catalog** → search "Playback Reporting" → Install → restart Jellyfin.
7. Schedule internal tasks for quiet hours: **Administration Dashboard → Scheduled Tasks** → find Database Optimization, Library Refresh, and Metadata tasks → set them around 5:15 AM.

## 9.8 — Navidrome

Serves your music library. First account you create becomes admin.

1. Open Navidrome at `http://[NAS-IP]:4533`. Create your admin account.
2. Wait a few minutes for the initial music scan to complete. If your music does not appear, trigger a manual scan: click the **gear icon** in the top-right → **Full Scan**. This is a common first-run issue.
3. Create separate accounts for family members: **Settings → Users → Add User**. Never share the admin account.

## 9.9 — Immich

Backs up your phone photos and videos with AI-powered face and object search.

1. Open Immich at `http://[NAS-IP]:2283`. Click **Get Started** and create your admin account.
2. **Administration → Settings → Storage Template** if you want to organise photos by date.

3. **Enable AI Hardware Acceleration:** Go to **Administration → Settings → Machine Learning → Hardware Acceleration** → select **OpenVINO** → Save. This uses your Intel Arc iGPU/NPU to scan faces and objects instantly instead of burdening the CPU.

4. Schedule background jobs for quiet hours: **Administration → Jobs** → set Machine Learning, Thumbnail Generation, and Smart Search to run around 5:45 AM.

### Set up the Immich phone app

1. Install the **Immich app** on your phone — free on App Store and Google Play.
2. Server URL: `http://[TAILSCALE-IP]:2283` (use Tailscale IP so it works both at home and away).
3. Log in with your admin username and password.
4. Tap your **profile photo** → **Background Backup** → turn **ON** → set to **WiFi only**.
5. Create additional Immich accounts for family: **Administration → Users → + New User**.

## 9.10 — Seerr

A friendly request interface. Family members open Seerr, search for any movie or show, tap Request, and it appears in Jellyfin after the download finishes.

1. Open Seerr at `http://[NAS-IP]:5055`. The setup wizard appears.
2. Click **"Sign In with Jellyfin"** → enter Jellyfin URL (`http://[NAS-IP]:8096`) and your Jellyfin admin credentials → Connect. Seerr inherits all Jellyfin users.
3. **Settings → Services → Radarr → Add** → Server: `radarr` (container name), Port: `7878`, API Key from **Radarr → Settings → General** → Test → Save.
4. **Settings → Services → Sonarr → Add** → Server: `sonarr`, Port: `8989`, API Key from **Sonarr → Settings → General** → Test → Save.
5. **Phone bookmark:** open Seerr in your phone browser at `http://[TAILSCALE-IP]:5055` → iPhone: tap Share → Add to Home Screen. Android: tap ⋮ → Add to Home Screen.

## 9.11 — Set Passwords on All Apps

| App | Where to set the password |
|---|---|
| qBittorrent | Settings → Web UI → Authentication (done in 9.1) |
| Prowlarr | Settings → General → Authentication → Forms → Username + Password → Restart |
| Sonarr | Settings → General → Security → Authentication Required → Forms → Restart |
| Radarr | Settings → General → Security → Authentication Required → Forms → Restart |
| Lidarr | Settings → General → Security → Authentication Required → Forms → Restart |
| Bazarr | Settings → General → Security → Username + Password |
| Jellyfin | Created in wizard. Enable 2FA: profile icon → Settings → Security → Two-Step Verification |
| Navidrome | Created on first launch. Manage: Settings → Users |
| Immich | Created in setup. Enable 2FA: Account Settings → Security |
| Seerr | Uses Jellyfin credentials |
| TrueNAS | Credentials → Local Users → your user → Edit → Password |

> [!TIP]
> Use a different strong password for each app. [[Bitwarden](https://bitwarden.com/)](https://bitwarden.com) (free) is an excellent password manager. A passphrase like `correct-horse-battery-staple` is both strong and memorable — 4+ words, 16+ characters.

---

# Part 10 — Phone and TV App Setup

All apps use your Tailscale IP when outside home. Keep Tailscale running in the background on your phone at all times — almost no battery, connects automatically.

## Movies and TV — Jellyfin

| Setting | Value |
|---|---|
| App | Jellyfin — free on App Store and Google Play |
| Add server (home WiFi) | `http://[NAS-IP]:8096` |
| Add server (away / Tailscale) | `http://[TAILSCALE-IP]:8096` |
| Tip | Add **both** addresses. The app uses whichever responds first. |
| Notifications | Profile icon → Notifications → New Episodes ON |
| Quality | Settings → Max Bitrate → Original for best quality on WiFi |

## Jellyfin on TV — The Big Screen

| TV Platform | App name | How to set up |
|---|---|---|
| Apple TV | Jellyfin (official, App Store) | Search Jellyfin in tvOS App Store → Add Server → `http://[NAS-IP]:8096` |
| Samsung / LG Smart TV | Jellyfin (official) | Search in your TV app store |
| Android TV / Google TV | Jellyfin (official, Google Play) | Search Jellyfin |
| Amazon Fire TV | Jellyfin (official, Amazon Appstore) | Search Jellyfin |
| Roku | Jellyfin (Roku Channel Store) | Search Jellyfin |
| Chromecast | Cast from Jellyfin phone app | Tap the cast icon in the Jellyfin phone app |
| Any computer | `http://[NAS-IP]:8096` in browser | No app needed |

## Music — Symfonium (Android) and Substreamer (iOS)

Navidrome is your music server. Symfonium and Substreamer are the phone apps that connect to it.

### Android — Symfonium (~€5 one-time)

| Step | What to do |
|---|---|
| 1. Install | Search Symfonium in Google Play Store |
| 2. Add server | Open app → tap + → Media Provider → select Navidrome |
| 3. Server URL | `http://[NAS-IP]:4533` at home — or — `http://[TAILSCALE-IP]:4533` everywhere |
| 4. Login | Your Navidrome username and password |
| Offline music | Tap download icon on any album |
| Playlists | Create in Symfonium — they sync back to Navidrome |

### iOS — Substreamer (free)

| Step | What to do |
|---|---|
| 1. Install | Search Substreamer in App Store |
| 2. Add server | Settings → Add Server |
| 3. Server type | Select **Subsonic** — Navidrome is Subsonic-compatible |
| 4. Server URL | `http://[TAILSCALE-IP]:4533` — works at home and away |
| 5. Login | Your Navidrome username and password |
| Offline | Long-press any album → Download |

## Photos — Immich App

| Step | What to do |
|---|---|
| 1. Install | Search Immich on App Store or Google Play — free |
| 2. Server URL | `http://[TAILSCALE-IP]:2283` — works at home and away |
| 3. Login | Your Immich admin username and password |
| 4. Enable backup | Profile photo → Background Backup → ON → WiFi only |
| Family | Create additional accounts in Immich web UI |

## Requesting Content — Seerr

| Step | What to do |
|---|---|
| Open | `http://[TAILSCALE-IP]:5055` in your phone browser |
| Log in | Your Jellyfin username and password |
| Add to phone | iPhone: Share → Add to Home Screen. Android: ⋮ → Add to Home Screen |
| Request a movie | Tap Search → type name → tap Request |
| It appears | After the torrent downloads and Radarr/Sonarr imports it |

---

# Part 11 — How Everything Works Together

## The Movie/TV Pipeline — From Request to Playing

```
You open Seerr → search "Dune Part Two" → tap Request
  ↓
Seerr sends the request to Radarr
  ↓
Radarr asks Prowlarr to find the file
  ↓
Prowlarr checks the torrent indexers you enabled (YTS, 1337x, EZTV)
  │  If an acceptable release is found → Radarr sends it to qBittorrent
  │  qBittorrent downloads active pieces to the apps SSD incomplete folder
  │  Finished files land in /mnt/tank/data/downloads/complete/
  │
  ├─ NORMAL LOCAL DOWNLOAD PIPELINE:
  │    Radarr hardlinks the file into /mnt/tank/data/media/ (same dataset!)
  │    Jellyfin scans the local library and adds it
  │    Bazarr downloads subtitles automatically
  │
  ├─ REAL-DEBRID PHASE 2 (Part 13):
  │    Zurg/rclone exposes items already in your Real-Debrid account
  │    at /mnt/tank/realdebrid/ — Jellyfin can play that separate library
  │    This guide does NOT automatically add Seerr requests to Real-Debrid
  │
  └─ AFTER qBittorrent FINISHES:
       Radarr hardlinks the completed file into /tank/data/media/ immediately
       Jellyfin detects the local file
       Bazarr adds subtitles
```

## The TV Show Pipeline

```
You add "Severance" in Sonarr (or request via Seerr)
  ↓
Sonarr downloads all existing episodes using the movie pipeline above
  ↓
Sonarr MONITORS the show forever via RSS feed
  New episode released? Sonarr detects it within minutes
  → downloads → appears in Jellyfin automatically — you do nothing
  ↓
Bazarr finds subtitles for each new episode automatically
  ↓
Repeat forever for every show you follow
```

## The Music Pipeline

```
You add "Radiohead" in Lidarr
  ↓
Lidarr searches Prowlarr (music indexers Redacted/Orpheus at priority 10)
  ↓
qBittorrent downloads to /mnt/apps/downloads-incomplete/ (SSD — fast writes)
  ↓
Download finishes → file moves to /mnt/tank/data/downloads/complete/music/
  ↓
Lidarr hardlinks it instantly to /mnt/tank/data/media/music/Radiohead/...
  (hardlink works because downloads and media are in the same tank/data dataset)
  ↓
Navidrome detects new albums within minutes
  ↓
Albums appear in Symfonium or Substreamer on your phone
  ↓
New album released? Lidarr detects and downloads automatically
  ↓
Your music library grows forever — never automatically deleted
```

## What Runs Automatically Every Night

| Time | What runs | What it does |
|---|---|---|
| Every 10 min | `health-check.sh` | Logs container status. Sends webhook alert if something is down. |
| Every 4 hours | TrueNAS snapshot: `apps/appdata` | Roll back if an update breaks an app |
| 01:00 daily | TrueNAS snapshot: `tank/photos` | 30-day retention |
| 01:30 daily | TrueNAS snapshot: `tank/data` | 14-day retention |
| 03:00 daily | `backup-app-config.sh` | Tarballs all app configs from SSD to mirrored HDD |
| 04:00 daily | `cleanup-downloads.sh` | Removes torrent junk and stale incomplete downloads |
| 04:30 daily | `scan-downloads.sh` | ClamAV scans completed downloads |
| 05:15 daily | Jellyfin scheduled tasks | Database optimization, metadata refresh |
| 05:45 daily | Immich background jobs | Machine learning, thumbnail generation |
| 06:00 daily | `photo-backup-usb.sh` | Copies photos to USB drive |
| Automatic | ClamAV freshclam | Updates virus database internally |

---

# Part 12 — Optional Webhook Alerts

If you set `WEBHOOK_URL` in `config.env`, `health-check.sh` sends a message whenever a container goes down. Works with Discord, Telegram, Slack, ntfy, and any JSON webhook.

| Service | How to get your webhook URL |
|---|---|
| ntfy.sh (recommended — free) | URL: `https://ntfy.sh/YOUR_UNIQUE_TOPIC` — install ntfy app on phone and subscribe |
| Discord | Server Settings → Integrations → Webhooks → New Webhook → Copy URL |
| Telegram | Create a bot via @BotFather, get the token: `https://api.telegram.org/bot{TOKEN}/sendMessage` |
| Slack | App directory → Incoming Webhooks → Add → choose channel → Copy URL |

```bash
# Edit config.env and add your webhook URL:
nano /mnt/apps/scripts/config.env
# Set: WEBHOOK_URL="https://ntfy.sh/my-nas-alerts-abc123"

# Test it:
source /mnt/apps/scripts/config.env
curl -sf "$WEBHOOK_URL" -d "Test alert from NAS"
# You should receive a notification on your phone immediately
```

---

# Part 13 — Real-Debrid + Zurg + rclone (Phase 2)

> [!IMPORTANT]
> Only do this after Phase 1 is working. Prove that Jellyfin streams local media, Sonarr/Radarr import a download correctly, and the nightly config backup succeeds. Then add Real-Debrid on top.

## What Is Real-Debrid (Quick Recap)

Real-Debrid costs about €4/month. Sign up at [[real-debrid.com](https://real-debrid.com/)](https://real-debrid.com). It is a cloud service that has cached many movies and TV shows. In this beginner guide, Zurg/rclone exposes items already in your Real-Debrid account as a separate Jellyfin library. Automatic Seerr-to-Debrid grabbing is an advanced add-on phase, not part of this stack.

- Your NAS only needs outbound internet access to Real-Debrid — no public inbound ports needed
- Get your API key at: **real-debrid.com/apitoken** (log in, go to that page, copy the long string)

## Step 13.1 — Create the Zurg Config

Zurg is the bridge between your NAS and Real-Debrid. It creates a virtual folder containing all your Real-Debrid content.

```bash
mkdir -p /mnt/apps/appdata/zurg
nano /mnt/apps/appdata/zurg/config.yml
```

Paste this content. Replace `YOUR_REAL_DEBRID_API_KEY` with your actual key from **real-debrid.com/apitoken**:

```yaml
# zurg: v1
token: YOUR_REAL_DEBRID_API_KEY
port: 9999
concurrent_workers: 20
check_for_changes_every_secs: 10
retain_folder_name_extension: true

directories:
  movies:
    group_order: 10
    group: media
    only_show_the_biggest_file: true
    filters:
      - regex: ".*"
  tv:
    group_order: 20
    group: media
    filters:
      - has_episodes: true
  # Key name 'tv' becomes the folder name in the rclone mount:
  # /mnt/tank/realdebrid/tv/ — matching all other guide path references.
```

## Step 13.2 — Create the rclone Config

rclone mounts the Zurg virtual folder so Jellyfin can see it as a normal folder on disk.

```bash
mkdir -p /mnt/apps/appdata/rclone
nano /mnt/apps/appdata/rclone/rclone.conf
```

```ini
[zurg]
type = webdav
url = http://127.0.0.1:9999/dav
vendor = other
```

> [!IMPORTANT]
> Use `http://127.0.0.1:9999/dav` — **not** `http://localhost:9999/dav` and not `http://zurg:9999/dav`.
>
> rclone runs as a script on the TrueNAS host, not inside Docker. `localhost` works in most cases but can be intercepted by Tailscale when MagicDNS or exit node features are active. `127.0.0.1` always resolves directly to the loopback interface and is unaffected by Tailscale. Zurg publishes port 9999 to the host via `ports: ["9999:9999"]` in the compose — both `localhost` and `127.0.0.1` resolve to that port, but `127.0.0.1` is more reliable.
>
> `http://zurg:9999/dav` fails because the host cannot resolve Docker container names.
>
> **If `127.0.0.1:9999` still times out:** run `curl -I http://127.0.0.1:9999/dav` in TrueNAS Shell. If it times out even after Zurg is running, find the actual Docker bridge IP:
> ```bash
> docker inspect zurg --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}'
> # Example output: 172.31.10.2
> ```
> Use that IP in `rclone.conf` instead of `127.0.0.1`. This is a fallback for unusual network configurations — `127.0.0.1` works for the vast majority of setups.

## Step 13.3 — Enable Zurg in the Compose and Create the Mount

Zurg runs as a container. The rclone mount runs as a Post Init script so the folder is ready before Docker starts.

### Uncomment Zurg in the Compose

```bash
nano /mnt/apps/scripts/docker-compose.yml
# Find the # zurg: section and remove the leading "# " from each service line.
# Be careful: lines that have TWO # (like "#   # comment") should keep the inner
# # intact — they become "    # comment" (valid YAML inline comment).
# Press Ctrl+X → Y → Enter to save
```

> [!TIP]
> To uncomment exactly the Zurg block without manual editing mistakes, you can use this sed command instead of nano:
> ```bash
> # This removes the "# " prefix ONLY from lines inside the zurg service block.
> # It correctly turns "#   # comment" lines into "    # comment" (stays a YAML comment).
> sed -i '/^  # zurg:/,/^  #   logging: \*default-logging/{/^  # /s/^  # /  /}' \
>   /mnt/apps/scripts/docker-compose.yml
> # Verify the result looks correct before saving:
> grep -A 20 '  zurg:' /mnt/apps/scripts/docker-compose.yml
> ```

### Create the FUSE pre-init script

```bash
nano /mnt/apps/scripts/enable-fuse-allow-other.sh
```

```bash
#!/bin/bash
# Use absolute path for sed — Post Init scripts may have incomplete PATH.
# || true prevents failure if fuse.conf is missing or already configured.
/usr/bin/sed -i 's/#user_allow_other/user_allow_other/' /etc/fuse.conf || true
```

```bash
chmod +x /mnt/apps/scripts/enable-fuse-allow-other.sh
```

### Create the rclone mount script

```bash
nano /mnt/apps/scripts/rclone-mount.sh
```

```bash
#!/bin/bash
# Explicit PATH required for Post Init scripts on TrueNAS SCALE 24.10+.
# /usr/local/bin is where rclone installs — absent from early-boot PATH.
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

set -Eeuo pipefail

# Verify rclone is installed before attempting anything else.
# On TrueNAS SCALE, rclone is NOT shipped with the OS and must be installed
# manually. It may also need reinstalling after major TrueNAS updates.
if ! command -v rclone >/dev/null 2>&1; then
  echo "ERROR: rclone is not installed on the TrueNAS host."
  echo "Install it: curl https://rclone.org/install.sh | bash"
  echo "This is an unsupported host modification — reinstall after TrueNAS system updates."
  exit 1
fi

sleep 30  # Give TrueNAS networking, FUSE, and the Zurg container time to settle.
          # TrueNAS starts Post-Init scripts and Docker containers concurrently.
          # 30 seconds gives Zurg enough time to start, pull its WebDAV library
          # from Real-Debrid, and begin serving before rclone connects.
          # Use 30 not 10/20 — Zurg's initial Real-Debrid handshake takes longer
          # on first boot or after a long gap.

mkdir -p /mnt/tank/realdebrid

source /mnt/apps/scripts/config.env

rclone mount zurg: /mnt/tank/realdebrid \
  --config /mnt/apps/appdata/rclone/rclone.conf \
  --allow-other \
  --uid "${PUID:-568}" \
  --gid "${PGID:-568}" \
  --attr-timeout 10s \
  --dir-cache-time 24h \
  --daemon

# Wait for the mount to become ready (up to 60 seconds).
# The marker file is written to /mnt/apps/scripts/.rd-mounted on the HOST —
# NOT inside the FUSE mount. Zurg serves read-only WebDAV; writing into
# the rclone mount would fail silently.
MARKER="/mnt/apps/scripts/.rd-mounted"
rm -f "$MARKER"

for i in $(seq 1 30); do
  if mountpoint -q /mnt/tank/realdebrid; then
    touch "$MARKER"
    echo "Real-Debrid mounted successfully — marker written to $MARKER"
    exit 0
  fi
  sleep 2
done

echo "Real-Debrid mount did not become ready in time"
exit 1
```

```bash
chmod +x /mnt/apps/scripts/rclone-mount.sh
```

> [!NOTE]
> **Verify port 9999 is free:** Before starting Phase 2, confirm nothing else is using port 9999 on your NAS:
> ```bash
> ss -tlnp | grep 9999
> # Should return nothing. If another service is listed, change Zurg's port
> # in both config.yml (port: 9999) and the compose (ports: ["9999:9999"]).
> ```

> [!NOTE]
> **rclone on TrueNAS host:** TrueNAS SCALE does not ship with rclone. Installing rclone on the TrueNAS host is an **advanced appliance modification**. It may need to be repeated after TrueNAS updates.
>
> ```bash
> which rclone          # check if already available
>
> # If not found, install it:
> curl https://rclone.org/install.sh | bash
>
> rclone version        # verify
> ```

> [!NOTE]
> **Post Init scripts and PATH in TrueNAS SCALE 24.10+:**
>
> In Electric Eel and later, the system PATH is not fully initialised when Post Init scripts run. Both scripts above protect against this:
>
> 1. `#!/bin/bash` (not `#!/usr/bin/env bash`) — the `env` command finds bash using PATH, which may not be set during early boot. `/bin/bash` is an absolute path that always works.
> 2. `rclone-mount.sh` sets `PATH` explicitly at the top — rclone installs to `/usr/local/bin` which is absent from the early-boot PATH.
>
> If a Post Init script works fine manually but fails on cold boot, check `/var/log/syslog` for "command not found" entries from those scripts.

### Register Both Scripts as Post Init

1. **System Settings → Advanced Settings → Init/Shutdown Scripts → Add**
2. First script: Type = Script, path = `/mnt/apps/scripts/enable-fuse-allow-other.sh`, When = **Post Init**, enable it
3. Second script: Type = Script, path = `/mnt/apps/scripts/rclone-mount.sh`, When = **Post Init**, enable it

### Redeploy and Verify

Redeploy **media-stack** from TrueNAS Apps so Zurg starts.

```bash
# Check Zurg connectivity using 127.0.0.1 (avoids Tailscale interception):
curl -I http://127.0.0.1:9999/dav
# Should return HTTP/1.1 401 Unauthorized (or 200 OK) — both mean Zurg is alive.
# A timeout or 'Connection refused' means Zurg is not running or port 9999 is blocked.

# Verify the FUSE mount is active (not just the local empty pre-created folders):
mountpoint -q /mnt/tank/realdebrid && echo 'Mounted OK' || echo 'NOT mounted'

# Check the marker file was written:
ls /mnt/apps/scripts/.rd-mounted

# Browse the mount contents (shows Real-Debrid content via Zurg):
ls /mnt/tank/realdebrid
# Should show movies/ and tv/ directories with content from your Real-Debrid account.
# Note: these directories exist even when NOT mounted (created in Part 4).
# Use mountpoint -q above to confirm the FUSE mount is actually active.
```

> [!NOTE]
> **About the mount marker file:**
>
> `rclone-mount.sh` creates `/mnt/apps/scripts/.rd-mounted` on the HOST when the mount succeeds. The Jellyfin wait script and safety rule checks look for this file. You never create it by hand.
>
> After TrueNAS boots and the Post Init script runs, verify the marker was created:
> ```bash
> ls /mnt/apps/scripts/.rd-mounted
> # If the file exists, the mount completed and Jellyfin will start
>
> # If the file is missing, check:
> cat /var/log/syslog | grep rclone
> # Or run the script manually:
> bash /mnt/apps/scripts/rclone-mount.sh
> ```

## Step 13.4 — Set WAIT_FOR_RD=1 and Add Jellyfin Library

1. In `config.env`, change `WAIT_FOR_RD` to `1`:

```bash
nano /mnt/apps/scripts/config.env
# Change: WAIT_FOR_RD="0" → WAIT_FOR_RD="1"
```

2. Create the Jellyfin wait script. This script runs every time Jellyfin starts and prevents it from scanning an empty folder before rclone is ready:

```bash
nano /mnt/apps/scripts/jellyfin-wait-for-rd.sh
```

```bash
#!/usr/bin/with-contenv bash
if [ "${WAIT_FOR_RD:-0}" != "1" ]; then exit 0; fi

# Wait for the host-side marker file written by rclone-mount.sh.
#
# IMPORTANT — Do NOT check "ls -A /media/realdebrid" here.
# Part 4 pre-creates empty movies/ and tv/ subfolders at that path.
# ls -A always returns non-empty because those folders exist on disk,
# even when rclone has not mounted yet. The check would always pass,
# Jellyfin would start, and find empty Real-Debrid libraries.
#
# The marker file /mnt/apps/scripts/.rd-mounted is written by
# rclone-mount.sh ONLY after a confirmed successful mountpoint check.
# It is the only reliable signal that the FUSE mount is actually live.

until [ -f /mnt/apps/scripts/.rd-mounted ]; do
  sleep 5
done

echo "Real-Debrid mount confirmed — starting Jellyfin."
```

```bash
chmod 755 /mnt/apps/scripts/jellyfin-wait-for-rd.sh
chown root:root /mnt/apps/scripts/jellyfin-wait-for-rd.sh
```

> [!WARNING]
> **Deliberate exception: this mount is intentionally NOT read-only.**
>
> The LinuxServer documentation generally recommends mounting custom init scripts as `:ro` where possible. This guide does **not** do that for one specific reason:
>
> The LinuxServer Jellyfin image runs a `fix-attrs` process at startup that attempts to `chown` every file in `/custom-cont-init.d/`. If the mount is `:ro`, the `chown` fails with "Permission Denied" and the **entire container crashes before Jellyfin starts** — not just the script, the whole container.
>
> The tradeoff: the Jellyfin container can technically write to this path inside its own view of `/custom-cont-init.d/`. In practice, linuxserver containers do not modify their own init scripts. If this ever becomes a security concern in your environment, the alternative is to place the script in `/mnt/apps/appdata/jellyfin/custom-cont-init.d/` (the appdata path mapped to `/config`) instead of using a compose volume mount — that path is always writable and avoids the `:ro` limitation entirely.
>
> `chmod 755` and `chown root:root` on the host file are required regardless.

3. The wait script is already wired into the Jellyfin container via the `docker-compose.yml` (see Part 6.2). Confirm these two volume lines are present in the Jellyfin service:

```yaml
    - /mnt/apps/scripts:/mnt/apps/scripts          # marker + wait script visible
    - /mnt/apps/scripts/jellyfin-wait-for-rd.sh:/custom-cont-init.d/10-wait-for-rd.sh  # no :ro
```

4. Redeploy `media-stack` from TrueNAS Apps.

5. Add the Real-Debrid libraries to Jellyfin: **Administration Dashboard → Libraries → +**
   - Type: Movies → Folder: `/media/realdebrid/movies` → Name: `Movies (RD)`
   - Type: Shows → Folder: `/media/realdebrid/tv` → Name: `TV (RD)`

6. Do **not** add Real-Debrid to Prowlarr in this beginner guide. Zurg/rclone only makes your existing Real-Debrid library visible to Jellyfin — it is not a normal Prowlarr indexer. If you later want automatic Debrid grabbing from Seerr/Radarr/Sonarr, add a maintained bridge such as Decypharr/RDT-Client as a separate advanced phase.

> [!TIP]
> Use Prowlarr with normal torrent indexers for the Sonarr/Radarr download pipeline. Add Debrid-aware automation only later as a separate advanced phase.

## Real-Debrid Mount Safety Rule

Any future script that touches `/mnt/tank/realdebrid` must first verify the mount is alive:

```bash
# Check mountpoint, host-side marker, AND that the mount has content
if ! mountpoint -q /mnt/tank/realdebrid || ! [ -f /mnt/apps/scripts/.rd-mounted ] || ! ls /mnt/tank/realdebrid >/dev/null 2>&1; then
  echo "Real-Debrid is not mounted. Stopping."
  exit 1
fi
```
---

# Part 14 — Monthly Update Process

Do not auto-update the whole stack. Manual approval keeps failures visible and recoverable.

> [!NOTE]
> **Immich updates (v2.x.x line):** Immich reached stable v2.0.0 in September 2025 and now follows semantic versioning. Within the v2.x.x line, server and mobile-app versions are compatible across patch and minor releases — monthly updates are routine. Watch for a future **v3.0.0** major version bump — that would signal database/API changes. Read release notes whenever a major number changes.

## Monthly Checklist

1. Glance at release notes for Immich, Jellyfin, and any `*arr` apps. Look for "Breaking" / "Database Migration" headings.

2. Run the config backup immediately before updating:
```bash
bash /mnt/apps/scripts/backup-app-config.sh
ls -lh /mnt/tank/backups/configs/   # confirm backup file was created
```

3. Take a manual snapshot of `apps/appdata`: **Storage → apps/appdata → Snapshots → Add** — name it something like `pre-update-2025-06-01`. The automatic 4-hour snapshot already gives you a recent point-in-time; the named one is a clearly-labelled marker.

4. In TrueNAS: **Apps → Installed → media-stack** → click **Update** or **Redeploy** to pull new images and restart.

5. Check logs for any app by clicking on it in the TrueNAS Apps screen.

6. Open Jellyfin, Immich, Sonarr, and Radarr in a browser and confirm they work normally.

7. Keep the snapshot for at least one week before considering the update stable.

> [!WARNING]
> Do not update during a scrub job, virus scan, or large import. Pick a quiet hour. Run the backup first, then update.

---

# Part 15 — Recovery Scenarios

## If an HDD Fails

The mirror keeps running on one drive — you do not lose data. Stay calm.

1. Check **TrueNAS Alerts** — it will show a DEGRADED warning with the failed drive's serial number.
2. Power off, replace the failed HDD with a new one of equal or greater size.
3. **TrueNAS → Storage → click the tank pool → Manage Devices → find the failed disk → Replace.**
4. Select the new drive. TrueNAS starts resilvering (rebuilding the mirror). Takes several hours for 8 TB.
5. Wait for resilver to complete. Pool returns to ONLINE status.

## If the Apps SSD Fails

Media and photos on the HDD mirror are completely unaffected.

1. Replace SSD 2 with a new one of equal or greater size.
2. **TrueNAS → Storage → Create Pool** → name it `apps` → select new SSD → Stripe.
3. Recreate datasets: `apps/appdata`, `apps/transcode`, `apps/downloads-incomplete`, `apps/scripts`.
4. Run the `mkdir` and permission commands from Part 4.
5. Restore the latest config tarball:

```bash
# Find and restore the most recent backup automatically:
LATEST=$(ls -t /mnt/tank/backups/configs/app-config-*.tar.gz | head -1)
echo "Restoring from: $LATEST"
cd / && tar -xzf "$LATEST"
```

> [!WARNING]
> **#1 cause of Immich boot-loops after a restore:** The tarball restores folder ownership from the archive. Run this command immediately after restoring, before starting the stack:
> ```bash
> chown -R 999:999 /mnt/apps/appdata/immich-db
> ```
> If you skip this, Postgres will refuse to start because it cannot write to its own data directory.

6. Redeploy `media-stack` from **Apps → Install via YAML** (use the same compose file from `/mnt/apps/scripts/`).
7. Test apps one by one.

> [!WARNING]
> **Tailscale identity is included in the backup tarball.** Restoring an older tarball rolls back the Tailscale state (auth tokens and machine identity) to the snapshot point. If the restored state uses an expired token, or if you had already deleted the old machine from the Tailscale admin console, the NAS will lose its Tailscale connection.
>
> **If the NAS disappears from your Tailscale device list after a restore:** delete `/mnt/apps/appdata/tailscale/` and redeploy. The container will re-authenticate using the `TS_AUTHKEY` in `config.env` (this is why the key must be Reusable and Non-Expiring).

## If the Boot SSD Fails

The boot SSD only holds the TrueNAS OS. Your `tank` and `apps` pools and all data are completely unaffected.

1. Replace the boot SSD with a new one (any 250 GB+ M.2 NVMe).
2. Reinstall TrueNAS SCALE from a USB stick — same procedure as Part 2.
3. After first login: **Storage → Import Pool**. Both `tank` and `apps` should appear as importable. Import each one — TrueNAS re-attaches them with all datasets, snapshots, and contents intact.
4. Re-do **Part 8 → Schedule All Scripts** to register the cron jobs.
5. Re-do **Part 7 → Create Snapshot Tasks**.
6. Re-do **Part 6 → Step 6.3 (Install via YAML)** to redeploy `media-stack`. The compose file is already on the imported `apps` pool at `/mnt/apps/scripts/`.
7. For Tailscale: the state under `/mnt/apps/appdata/tailscale` may carry over — try connecting first. If it does not work, generate a new auth key from [[tailscale.com/admin](https://tailscale.com/admin)](https://tailscale.com/admin).

## If an App Update Breaks Something

1. Stop the affected app from **TrueNAS Apps → media-stack**.
2. Check logs by clicking on the container in the TrueNAS Apps screen.
3. If the app config database is corrupted: **stop every container that shares `apps/appdata`** first (stop the entire `media-stack`). A snapshot rollback while a container is writing corrupts open files — Postgres especially.
4. Go to **Storage → apps/appdata → Snapshots**. Find the snapshot from before the update. Click **Rollback**.
5. Start the stack from TrueNAS Apps. Verify the affected app first.
6. Keep the snapshot until confident the app is working.

> [!WARNING]
> Rolling back `apps/appdata` affects **all** app configs, not just one. Every Sonarr/Radarr/Lidarr import recorded since the snapshot, every Jellyfin watch-progress update, every Immich photo metadata change — all reverted.
>
> If only one app is broken, try restoring just its subfolder from the config tarball first:
> ```bash
> tar -xzf /mnt/tank/backups/configs/app-config-YYYY-MM-DD.tar.gz \
>   -C / mnt/apps/appdata/sonarr   # restore only Sonarr
> ```

---

# Part 16 — Troubleshooting

Always check **TrueNAS Apps → Installed → media-stack** first. Use the container log buttons before reaching for Shell commands.

| Problem | First check |
|---|---|
| Jellyfin cannot see media | Container path mapping — `/media/movies` is inside the container, not on your host. Check the compose volumes section. |
| Immich database error on start | `chown -R 999:999 /mnt/apps/appdata/immich-db` — then restart `immich-db`, `immich-redis`, `immich-server`, `immich-machine-learning` |
| qBittorrent cannot log in | `docker logs qbittorrent 2>&1 \| grep -i password` — find the random startup password |
| qBittorrent paths are wrong | Settings → Downloads: Default Save Path = `/data/downloads/complete`, Incomplete = `/downloads/incomplete`, categories as set in 9.1 |
| Sonarr/Radarr cannot reach qBittorrent | Check hostname is `qbittorrent` (container name), not the NAS IP |
| Seerr cannot reach Sonarr/Radarr | Both Sonarr and Radarr are on `request-net` (same as Seerr) and Prowlarr is on `download-net` + `request-net`. If still failing, verify the `networks:` section of each service in `docker-compose.yml`. |
| rclone mount is empty after Part 13 | `curl -I http://127.0.0.1:9999/dav` — should return 401 or 200. If it times out, Zurg is not running. If Zurg is fine but rclone is empty, confirm `rclone.conf` uses `url = http://127.0.0.1:9999/dav` (not `http://localhost:9999/dav` — Tailscale can intercept localhost; not `http://zurg:9999/dav` — container names do not resolve from host scripts). |
| Jellyfin sees empty Real-Debrid folder | Check the marker: `ls /mnt/apps/scripts/.rd-mounted` — if missing, `rclone-mount.sh` did not complete. Also check that `:ro,shared` is on the `/media/realdebrid` volume in the compose — without `shared`, rclone mounts made after the container starts are invisible to Jellyfin. |
| Zurg works but arr apps cannot reach it | If you moved Zurg to a separate Compose project, attach it to the same `download-net` network. The host-side rclone uses `127.0.0.1:9999` and is unaffected. |
| Permission error starting any app | Re-run permissions from Part 4. Remember `immich-db` needs `chown 999:999`. |
| Disk space filling up | `du -sh /mnt/tank/data/media/*` — run `bash /mnt/apps/scripts/cleanup-downloads.sh` — clear Jellyfin transcode: `rm -rf /mnt/apps/transcode/jellyfin/*` |
| Tailscale remote access fails | Check auth key in `config.env`. `docker logs tailscale`. Check `tailscale.com/admin` shows the NAS as connected. |
| 1337x or EZTV returns 403 Forbidden | FlareSolverr proxy is not assigned. **Prowlarr → Indexers → pencil icon → Proxy dropdown → select FlareSolverr → Save**. Check FlareSolverr is running: `docker logs flaresolverr | tail -20`. |
| Optional Torrentio/Debrid bridge issues | The beginner guide does not include a working Torrentio/Prowlarr bridge. If you installed one as an advanced step, follow that project's own troubleshooting docs. |

```bash
# Useful read-only Shell checks:
docker ps --format "table {{.Names}}\t{{.Status}}"
docker logs jellyfin | tail -50
docker logs immich-server | tail -50
docker logs qbittorrent | tail -50

# Storage checks:
zfs list -t snapshot | grep tank
du -sh /mnt/tank/data/media/*
du -sh /mnt/apps/appdata/*
ls -lh /mnt/tank/backups/configs/
```

---

# Part 17 — Changing Settings

All settings live in `config.env`. Edit the file, then redeploy the stack from TrueNAS Apps.

```bash
nano /mnt/apps/scripts/config.env
# Make your changes, then Ctrl+X → Y → Enter to save

# Redeploy from TrueNAS Apps → media-stack → Update/Redeploy
# OR restart a specific container only:
docker restart jellyfin
```

| What to change | Variable in config.env |
|---|---|
| Timezone | `TZ="Asia/Jerusalem"` — use a valid tz database string |
| qBittorrent password | `QBIT_PASSWORD="..."` |
| Immich database password | `IMMICH_DB_PASS` and `POSTGRES_PASSWORD` and `DB_PASSWORD` — change all three to the same value |
| USB backup on/off | `ENABLE_USB_BACKUP="1"` or `"0"` |
| USB drive UUID | `USB_UUID="..."` — find with: `blkid \| grep -i usb` |
| Webhook alerts URL | `WEBHOOK_URL="https://..."` |
| Tailscale auth key | `TS_AUTHKEY="tskey-auth-k..."` |
| Cleanup grace period | `INCOMPLETE_DAYS="14"` — days before stale incomplete downloads are deleted |
| Enable Real-Debrid wait | `WAIT_FOR_RD="1"` — set this after Part 13 is complete |
| Navidrome transcode cache | `ND_TRANSCODINGCACHESIZE="2GB"` — increase if many concurrent mobile users |
| Immich version (pinned) | `IMMICH_VERSION=release` — change to e.g. `v1.135.3` to pin both server and ML to the same stable version |
| Immich ML acceleration | `IMMICH_MACHINE_LEARNING_GPU_ACCELERATION=openvino` — leave as openvino for Intel Arc |

---

# Part 18 — Quick Reference

## All App Addresses

| App | Local URL | Tailscale URL |
|---|---|---|
| TrueNAS | `http://truenas.local` or `http://[NAS-IP]` | `http://[TAILSCALE-IP]` |
| Jellyfin | `http://[NAS-IP]:8096` | `http://[TAILSCALE-IP]:8096` |
| Navidrome | `http://[NAS-IP]:4533` | `http://[TAILSCALE-IP]:4533` |
| Immich | `http://[NAS-IP]:2283` | `http://[TAILSCALE-IP]:2283` |
| Seerr | `http://[NAS-IP]:5055` | `http://[TAILSCALE-IP]:5055` |
| qBittorrent | `http://[NAS-IP]:8090` | Admin only |
| Prowlarr | `http://[NAS-IP]:9696` | Admin only |
| Sonarr | `http://[NAS-IP]:8989` | Admin only |
| Radarr | `http://[NAS-IP]:7878` | Admin only |
| Lidarr | `http://[NAS-IP]:8686` | Admin only |
| Bazarr | `http://[NAS-IP]:6767` | Admin only |
| FlareSolverr | `http://[NAS-IP]:8191` | Admin only |
| Zurg status | `http://[NAS-IP]:9999` | Admin only |

## Key File Paths

| What | Path |
|---|---|
| `docker-compose.yml` | `/mnt/apps/scripts/docker-compose.yml` |
| `config.env` | `/mnt/apps/scripts/config.env` |
| All app configs | `/mnt/apps/appdata/[appname]/` |
| Jellyfin transcode temp | `/mnt/apps/transcode/jellyfin/` |
| Incomplete downloads | `/mnt/apps/downloads-incomplete/` |
| Movies | `/mnt/tank/data/media/movies/` |
| TV Shows | `/mnt/tank/data/media/tv/` |
| Music | `/mnt/tank/data/media/music/` |
| Photos | `/mnt/tank/photos/library/` |
| Completed downloads | `/mnt/tank/data/downloads/complete/` |
| Quarantine (virus) | `/mnt/tank/data/downloads/quarantine/` |
| App config backups | `/mnt/tank/backups/configs/` |
| Real-Debrid virtual folder | `/mnt/tank/realdebrid/` |
| RD mount marker | `/mnt/apps/scripts/.rd-mounted` |
| Script logs | `/mnt/apps/scripts/*.log` |

## Final Build Checklist

### Phase 1 — Local Stack

- [ ] TrueNAS SCALE 24.10+ installed on boot SSD only
- [ ] BIOS: Intel VT, IOMMU, ASPM, C-states, iGPU Multi-Monitor all configured
- [ ] `tank` HDD mirror created and confirmed
- [ ] `apps` SSD pool created
- [ ] SSD TRIM enabled
- [ ] All datasets created: `apps/appdata`, `apps/scripts`, `apps/transcode`, `apps/downloads-incomplete`, `tank/data`, `tank/photos`, `tank/realdebrid`, `tank/backups`
- [ ] Download dataset security: exec=off, setuid=off, devices=off
- [ ] Folders created with `mkdir` commands
- [ ] Permissions set: `568:568` for app folders, `999:999` for `immich-db`
- [ ] `RENDER_GID` found with `getent group render | cut -d: -f3`
- [ ] `/dev/dri` confirmed present: `ls /dev/dri` shows `card0  renderD128`
- [ ] `config.env` created and all values filled in (RENDER_GID not blank)
- [ ] `docker-compose.yml` created
- [ ] Placeholder `jellyfin-wait-for-rd.sh` created before deploy (see warning in Step 6.3)
- [ ] Stack deployed via TrueNAS Apps → Install via YAML
- [ ] All containers show Running in TrueNAS Apps
- [ ] Tailscale authenticated, IP written down, MagicDNS enabled
- [ ] Phone connected to Tailscale
- [ ] ZFS snapshot tasks created (4 tasks)
- [ ] All 5 maintenance scripts created and scheduled
- [ ] qBittorrent: random password found in logs, changed, download paths set, categories created
- [ ] Prowlarr: FlareSolverr proxy registered (`http://flaresolverr:8191`)
- [ ] Prowlarr: FlareSolverr proxy assigned to YTS, 1337x, and EZTV
- [ ] Prowlarr: all indexers added with correct priorities
- [ ] Sonarr: qBittorrent connection, Prowlarr sync, root folder, naming format
- [ ] Radarr: same as Sonarr but for movies
- [ ] Lidarr: same pattern, Lossless quality profile created
- [ ] Bazarr: Sonarr/Radarr connections, language + English, OpenSubtitles
- [ ] Jellyfin: setup wizard, VAAPI enabled, Playback Reporting plugin installed
- [ ] Navidrome: admin account created
- [ ] Immich: admin account, **OpenVINO hardware acceleration enabled**, phone app installed, background backup enabled
- [ ] Seerr: Jellyfin login, Radarr/Sonarr connected
- [ ] Passwords set on all apps
- [ ] Jellyfin app on phone: both local and Tailscale addresses added
- [ ] Symfonium or Substreamer installed and connected to Navidrome
- [ ] Seerr added to phone home screen
- [ ] **TEST:** download something via torrent, confirm it imports to Jellyfin
- [ ] **TEST:** stream a movie from your phone using Tailscale away from home

### Phase 2 — Real-Debrid

- [ ] Zurg `config.yml` created with Real-Debrid API key
- [ ] `rclone.conf` created (`type = webdav`, `url = http://127.0.0.1:9999/dav`)
- [ ] rclone installed on host (`curl https://rclone.org/install.sh | bash`)
- [ ] Init scripts registered in TrueNAS (enable-fuse + rclone-mount)
- [ ] Zurg service uncommented in compose, stack redeployed
- [ ] `ls /mnt/apps/scripts/.rd-mounted` — marker file exists
- [ ] `ls /mnt/tank/realdebrid/` — shows Real-Debrid content
- [ ] `jellyfin-wait-for-rd.sh` created (marker-only check — no `ls -A`)
- [ ] `WAIT_FOR_RD="1"` set in `config.env`
- [ ] Real-Debrid libraries added to Jellyfin (`/media/realdebrid/movies` and `/media/realdebrid/tv`)
- [ ] **TEST:** open Jellyfin Real-Debrid library and play an item that already exists in your Real-Debrid account
- [ ] No port forwards open on router (verify in router admin page)

---

## Guide Version

| | |
|---|---|
| **Version** | Final Edition |
| **Base architecture** | v13 (hardlink-optimised `tank/data`, TrueNAS UI snapshots, rclone Post Init, RENDER_GID, Docker log limits, explicit subnets, Install via YAML) |
| **Hardware** | Intel Core Ultra 5 225 · Netanya, Israel |
| **Key fixes** | FUSE `:ro,shared` · Marker-only wait script · `docker inspect` health · `rclone` existence check · `127.0.0.1` in rclone.conf · ZFS recordsize=16k · ND_TRANSCODINGCACHESIZE · RENDER_GID drift validation · Tailscale identity reset warning · exec=off clarified · sleep 30 for Zurg startup · placeholder wait script at first deploy · Zurg tv: key matches all path refs · force_probe persists via TrueNAS UI |
