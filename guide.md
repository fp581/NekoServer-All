# TrueNAS SCALE Home Media Server

_Final Edition — Complete Beginner-Friendly Build_

ZFS · Real-Debrid · Tailscale · Self-Healing · Remote Access

*Jellyfin · Navidrome · Immich · Sonarr · Radarr · Lidarr · Bazarr · Seerr*

> [!NOTE]
> **🎯 What you will have when you finish this guide**
> 
> 🎬 Movies and TV — open Seerr on your phone, tap Request. The movie plays in Jellyfin within 2 minutes via Real-Debrid, and downloads locally in the background.
> 🎵 Music — your own private Spotify. Add an artist once in Lidarr, get every album downloaded and available in Symfonium or Substreamer on your phone.
> 📷 Photos — your phone photos back up automatically when on WiFi. Search by face, object, or date. Works like Google Photos but on your own drives.
> 🌍 Remote access — all apps reachable from anywhere via Tailscale. No port forwarding. No domain name. No monthly cost beyond the NAS itself.
> 🛡️ Self-healing — ZFS snapshots, nightly config backups, virus scanning, health checks. Resilient to drive failure and accidental mistakes.
> 📱 Plays on phone, Apple TV, Samsung, Android TV, Fire TV, Roku, Chromecast, and any web browser.

> [!NOTE]
> **How to use this guide**
> 
> 1\. Read one part at a time. Do only the steps in that part.
> 2\. Do not skip ahead unless the guide tells you to.
> 3\. If a step says "in the TrueNAS Shell" — click the >\_ icon in the top-right corner of the TrueNAS web page. A black command window opens.
> 4\. If a step says "in the TrueNAS web page" — that means clicking buttons in your browser.
> 5\. Every code block is safe to copy and paste in full. Do not retype — paste.
> 6\. If you feel unsure, stop and re-read the step before pressing anything. Slow is safe.

## Contents

- [Part 0 — Understand What You Are Building](#part-0--understand-what-you-are-building)
- [Part 1 — Hardware](#part-1--hardware)
- [Part 2 — Install TrueNAS SCALE](#part-2--install-truenas-scale)
- [Part 3 — Create Storage Pools](#part-3--create-storage-pools)
- [Part 4 — Create Datasets and Folders](#part-4--create-datasets-and-folders)
- [Part 5 — Remote Access: Tailscale First](#part-5--remote-access-tailscale-first)
- [Part 6 — The Docker Stack](#part-6--the-docker-stack)
- [Part 7 — ZFS Snapshots](#part-7--zfs-snapshots)
- [Part 8 — Maintenance Scripts](#part-8--maintenance-scripts)
- [Part 9 — First-Time App Setup](#part-9--first-time-app-setup)
- [Part 10 — Phone and TV App Setup](#part-10--phone-and-tv-app-setup)
- [Part 11 — How Everything Works Together](#part-11--how-everything-works-together)
- [Part 12 — Optional Webhook Alerts](#part-12--optional-webhook-alerts)
- [Part 13 — Real-Debrid + Zurg + rclone (Phase 2)](#part-13--real-debrid--zurg--rclone-phase-2)
- [Part 14 — Monthly Update Process](#part-14--monthly-update-process)
- [Part 15 — Recovery Scenarios](#part-15--recovery-scenarios)
- [Part 16 — Troubleshooting](#part-16--troubleshooting)
- [Part 17 — Changing Settings](#part-17--changing-settings)
- [Part 18 — Quick Reference](#part-18--quick-reference)

## Part 0 — Understand What You Are Building

Before touching any hardware, understand the complete picture. This is a
server that sits silently at home, giving you the experience of
Netflix + Spotify + Google Photos — but on your own hardware, under your
own control.

### The Three Storage Layers

| **Layer** | **Device** | **Pool / Path** | **Purpose** |
|:---|:---|:---|:---|
| Operating system | SSD 1 | TrueNAS boot device | TrueNAS SCALE only — nothing else |
| App data | SSD 2 | apps pool (/mnt/apps/) | App databases, configs, transcode temp, incomplete downloads — fast random I/O |
| Main storage | 2x 8 TB IronWolf | tank mirror (/mnt/tank/) | Media, photos, completed downloads, backups — large and redundant |

> [!TIP]
> Why two SSDs? The apps SSD handles all the small random writes: Immich database, Jellyfin metadata, active torrent pieces. This keeps the HDD mirror doing large sequential reads and writes — what spinning drives do best. The result is snappier apps without wearing out the HDDs.

> [!IMPORTANT]
> **The apps SSD has no redundancy.** A single drive backed up nightly at 03:00 (see Part 8) means a failure between backups can lose up to 24 hours of state — Immich uploads not yet backed up, Sonarr/Radarr import history, qBittorrent state, Jellyfin watch progress.
>
> If that window matters to you, two options:
>
> - **Run the backup more often** — change the cron in Part 8 from `0 3 * * *` (3 AM daily) to e.g. `0 */6 * * *` (every 6 hours). Trade-off: more snapshot churn on tank.
> - **Use a mirrored apps pool** — add a second SSD and create the apps pool as a mirror in Part 3 instead of a single-disk stripe. Trade-off: cost of a second SSD.

### Every App and What It Does

| **App** | **What it does** | **Think of it as** |
|:---|:---|:---|
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
| ClamAV | Scans downloaded files for viruses | Your download security guard |

> [!NOTE]
> **ClamAV is the wrong tool for this job and will be replaced in a later revision.**
> The download datasets already have `noexec`, `nosetuid`, and `nodev` set (Part 4), so a downloaded file cannot run itself even if it tried. Pirated media is `.mkv` / `.mp4`, not executable, and ClamAV signatures lag months behind real malware. The daily 04:30 scan re-reads every completed download for very little real protection.
>
> A better approach is to filter at the **grab** stage rather than the **post-download** stage: stop suspicious releases from ever entering the download queue. The plan is to add **Profilarr** to the stack — it syncs custom-format definitions and quality profiles into Sonarr/Radarr so releases with malicious file extensions, scam encoders, or known-bad release groups are rejected before qBittorrent ever sees them. ClamAV (and `scan-downloads.sh` in Part 8) will be removed in that revision.

### What Is Real-Debrid?

Real-Debrid costs about €4 per month. Sign up at real-debrid.com. Think
of it as a giant media warehouse in the cloud:

- Real-Debrid has already cached millions of movies and TV shows on its
  fast servers

- When you request a movie, Real-Debrid streams it to Jellyfin instantly
  — no waiting for a download to finish

- At the same time, qBittorrent downloads a local copy in the background

- Next time you watch it, Jellyfin streams from your local drive —
  faster, uses no Real-Debrid quota

> [!IMPORTANT]
> Real-Debrid is Phase 2 in this guide. Do the entire Phase 1 first: prove that local media works, Jellyfin plays, and downloads import correctly. Then add Real-Debrid on top in Part 13.

### How Jellyfin Picks What to Stream — Automatic Priority

| **Priority** | **Source** | **What happens** |
|:---|:---|:---|
| 1st — Always wins | Your local drive (/mnt/tank/data/media/) | File is on your drives. Fastest. Zero Real-Debrid quota used. |
| 2nd | Real-Debrid virtual folder (/mnt/tank/realdebrid/) | Streams instantly from Real-Debrid. qBittorrent downloads local copy simultaneously. |
| 3rd | Torrent download in progress | File appears in Jellyfin once qBittorrent finishes. |

> [!TIP]
> You never choose manually. Local always wins. Real-Debrid fills in what you do not have locally yet.

### The Network Model — What Needs Internet and What Does Not

| **Traffic type** | **Needed?** | **Example** | **How this guide handles it** |
|:---|:---|:---|:---|
| Outbound internet | Yes | Real-Debrid, indexers, subtitles, metadata, updates | Always allowed — your NAS needs to reach the internet |
| Public inbound internet | No | Random people connecting directly from the web | Blocked — do not open router port forwards |
| Private inbound (remote you) | Yes | Your phone or laptop reaching the NAS when away from home | Tailscale only — encrypted private tunnel |
| Torrent peer inbound | Optional | Better torrent speeds | Not required — Real-Debrid works outbound only |

### Music and Video Are Completely Separate

|  | **VIDEO — Jellyfin** | **MUSIC — Navidrome** |
|:---|:---|:---|
| Content | Movies, TV shows | Albums, singles, artists |
| Source | Real-Debrid OR local drive | Local drive only — no Real-Debrid |
| Storage | Never deleted automatically — your choice | Never deleted — grows forever |
| Phone app | Jellyfin app (free) | Symfonium (Android) or Substreamer (iOS) |
| Port | 8096 | 4533 |

> [!IMPORTANT]
> **Lidarr's automation depends on the indexers you have access to.**
>
> - **Private trackers** (Redacted, Orpheus, etc.) — invitation-only. Excellent music coverage, complete discographies, lossless rips, accurate metadata. This is what Lidarr was designed for and what the Music Pipeline in Part 11 assumes.
> - **Public trackers** (1337x, RuTracker, etc.) — open to anyone. Coverage is spotty, especially for niche artists, deep catalogue, and lossless. Lidarr will still work but will miss a lot and pull worse-quality rips. You will hand-import more than you'd like.
>
> If you do not have access to a private music tracker, Lidarr is still useful as a library manager (file naming, metadata, tagging, monitoring for new releases) — just expect more manual work on the acquisition side. Setting expectations now will save frustration in Part 9.

## Part 1 — Hardware

| **Component** | **Role in this build** |
|:---|:---|
| Intel Core i5-13500 (LGA1700) | CPU with UHD 770 iGPU for hardware video transcoding (Quick Sync). 14 cores (6P+8E), 65 W TDP. The i5-12500 is a fine cheaper alternative on the same socket. |
| Intel stock cooler **or** tower cooler (be quiet! Pure Rock 2, Arctic Freezer 36) | Stock is adequate at idle and light transcoding loads. A tower cooler is quieter and keeps temps in check during sustained transcodes or in warm cupboards. |
| 32 GB DDR4-3200 (or DDR5 if your board supports it) | TrueNAS, ZFS ARC cache, and all running containers. ECC RAM is nice but not required for a home NAS. |
| ASUS PRIME B760M-A WIFI (or any LGA1700 micro-ATX board with 2x M.2, iGPU output, and gigabit Ethernet) | Motherboard. B760 micro-ATX boards from ASUS / MSI / Gigabyte / ASRock are all reasonable. |
| Boot SSD: any 250–500 GB M.2 NVMe (e.g. WD Blue SN570 500 GB) | TrueNAS OS only — uses about 50 GB. Anything larger is just spare. |
| Apps SSD: 1 TB M.2 NVMe (e.g. Corsair T500, WD Black SN770) | Apps pool — app databases, transcode temp, incomplete downloads. 1 TB gives headroom for Immich's database, transcode buffers, and active downloads. |
| 2x Seagate IronWolf 8 TB | tank mirror — media, photos, completed downloads, backups. Size to your media library; 8–12 TB per drive is the typical sweet spot for cost-per-TB. |
| be quiet! Pure Power 12 M 550 W (80+ Gold, modular) | Power supply. This build idles around 30–40 W and peaks under 150 W — 550 W is plenty. Anything above 650 W is wasted money and runs less efficiently at low load. |
| Case with at least one intake fan with airflow over the HDD cage | Two 24/7 HDDs in a closed cupboard need active airflow. Fractal Design Pop Mini Air or Cooler Master NR200 are both good micro-ATX options. |
| Wired Ethernet cable | Required — never use Wi-Fi for a NAS |

### Cable Connections

- Boot SSD → M.2 slot 1 on the motherboard (M.2 slot numbering is board-specific — check your manual)

- Apps SSD → M.2 slot 2

- IronWolf Drive 1 → SATA port 1

- IronWolf Drive 2 → SATA port 2

- Ethernet cable → motherboard ethernet port → your router or switch

- Keyboard and monitor → connect temporarily for first install only

> [!WARNING]
> Always use a wired Ethernet cable. Wi-Fi causes mysterious transfer failures and timeouts that are very hard to diagnose.

## Part 2 — Install TrueNAS Community Edition
> [!NOTE]
> **You don't have to copy this build exactly.**
> The parts above are a tested, attainable starting point. What actually matters:
>
> - **Intel CPU with iGPU** (Quick Sync) for hardware video transcoding. AMD works too if you handle GPU passthrough yourself.
> - **Two SSDs** — one boot (small, anything ≥ 250 GB), one apps (1 TB recommended for headroom).
> - **Two HDDs of equal capacity** for the tank mirror. Match capacity exactly; different sizes will waste space.
> - **Active airflow over the HDD cage** if the NAS lives in a cabinet or cupboard. HDDs over 40 °C have meaningfully shorter lifespan.
>
> The catch: BIOS instructions in Part 2 reference a specific motherboard's option layout. If you use a different board, the option names (Intel VT, IOMMU, ASPM, Primary Display) are usually similar but may live in different menus.

> [!NOTE]
> **Author's original build — for reference**
> The first version of this guide was built around an Intel Core Ultra 5 225 (Arrow Lake) on a MAXSUN iCraft B860M CROSS PRO (LGA1851) motherboard. That build still works; the catch is that the i915 graphics driver in TrueNAS 24.10's kernel does not yet recognise the Arrow Lake iGPU device ID, so it requires a kernel parameter workaround (the `ix_diagnostics_force_probe` step in Part 4). The LGA1700 build above does not need any of that.

## Part 2 — Install TrueNAS SCALE

This part installs the operating system onto the boot SSD. You need a
keyboard, monitor, and a USB drive connected to the NAS for this part
only.

> [!NOTE]
> **Naming change:** TrueNAS SCALE was renamed to **TrueNAS Community Edition** (CE) with the 25.04 Fangtooth release. The free, self-hosted product you want is TrueNAS Community Edition. The Enterprise edition is the paid tier. Older guides and forum posts still calling it "SCALE" are referring to the same product.

### Step 2.1 — Create the USB Installer

1. Download a USB writer on your regular computer:
   - **Windows:** [Rufus](https://rufus.ie) — small, signed, no telemetry, supports the TrueNAS ISO out of the box.
   - **Mac / Linux:** the built-in `dd` command works (`sudo dd if=truenas.iso of=/dev/diskN bs=4M status=progress` — confirm the device path with `lsblk` on Linux or `diskutil list` on Mac first).

2. Download the latest **TrueNAS Community Edition** ISO from
   [truenas.com/download-truenas-community-edition](https://www.truenas.com/download-truenas-community-edition/)
   (about 1.5 GB). The current release at time of writing is **25.10.3**
   — install this version or newer.

3. In Rufus: under **Device**, select your USB drive. Click **SELECT**
   and choose the TrueNAS ISO. Leave the other options at their
   defaults (Partition scheme: GPT, Target system: UEFI). Click
   **START**. If prompted about ISO mode, accept the default
   "Write in ISO Image mode". Wait about 5 minutes, then safely eject.

> [!WARNING]
> Do not select your SSD or HDD as the flash target. That would erase your drive. The USB drive is usually the small one (8–32 GB) — double-check the size and label before clicking START.

### Step 2.2 — BIOS Setup

On the MAXSUN iCraft B860M CROSS PRO motherboard, the key to enter BIOS
is Delete. Press it immediately and repeatedly as soon as the screen
lights up after powering on — about once per second. If you miss it,
just power off and try again.

4. Plug the USB into the NAS and power it on.

5. Press the Delete key repeatedly as soon as the screen lights up. A
   colourful settings screen appears. This is the BIOS.

6. Use arrow keys to navigate. Find the Boot tab. Find "Boot Option
   \#1" and change it to your USB drive. The drive appears by its brand
   name.

7. **Disable Secure Boot.** Most B760 / B860 motherboards ship with
   Secure Boot **enabled** by default — TrueNAS will refuse to boot
   with a "Secure Boot violation" error mid-install. Find Secure Boot
   under the Boot tab or a Security tab and set it to **Disabled**
   (some boards label this "Other OS" mode). If you cannot find the
   option, look for a "Setup Mode" toggle or clear the Secure Boot
   keys — both achieve the same result.

8. Find the Advanced tab. Find CPU Configuration. Find "Intel
   Virtualization Technology" and set it to Enabled.

9. In the same area, find "IOMMU" or "Intel VT-d" and set it to
   Enabled. This lets Docker containers use the Intel graphics chip for
   video transcoding.

10. For best idle power consumption: look for ASPM (PCIe Active State
    Power Management) and enable it. Look for CPU C-states or Package
    C-state and set to Auto or Enabled. These settings let the CPU and
    PCIe devices sleep properly when idle.

11. Critical for headless use: your NAS will sit in a cupboard with no
    monitor. By default, some motherboards disable the iGPU when no
    screen is connected. Find the setting labelled "Primary Display",
    "Primary Graphics", or "iGPU Multi-Monitor" — it may be in the
    Advanced tab or a Chipset/Graphics sub-menu. Set it to IGFX, iGPU,
    or Internal Graphics. This forces the Intel iGPU to stay active
    even with no monitor plugged in. Without this, /dev/dri will be
    empty and Jellyfin hardware transcoding will silently fail.

12. Press F10 to save and exit. The NAS restarts from the USB.

### Step 2.3 — Install TrueNAS

The installer is a blue text menu. Use arrow keys to move and Enter to
select.

13. Select "Install/Upgrade" from the menu.

14. The next screen asks which disk to install on. You will see a list.
    Your boot SSD will be the smallest drive — typically 250–500 GB,
    NOT the 8 TB IronWolf drives. Select it. If you see two
    similarly-sized SSDs, double-check the serial numbers against the
    stickers on the drives.

15. TrueNAS warns that the disk will be erased. Confirm. This only
    erases the boot SSD.

16. It asks you to create an admin account with a username and password.
    Write this down. **Note:** TrueNAS pre-fills the username as
    `truenas_admin`. You can accept that default or override it with a
    name of your choosing — the password is yours to set either way.

17. Installation takes about 10 minutes. When it finishes, select
    Reboot. While rebooting, remove the USB drive so TrueNAS boots from
    the SSD.

### Step 2.4 — First Login

On your regular PC (not the NAS), open a web browser.

18. In the address bar, type `http://truenas.local` and press Enter.
    If that does not work, use the IP address shown on the NAS monitor
    during boot — for example `http://192.168.1.50`. (The `.local`
    name relies on mDNS, which is blocked or unrouted on some
    networks — corporate Wi-Fi, some prosumer routers with mDNS
    forwarding off. The IP fallback always works.)

19. A login page appears. Enter the username and password you created
    during installation.

20. A short setup wizard appears. It asks about storage and networking.
    Click Next or Skip through everything — do not configure storage
    here. You will do that in Parts 3 and 4.

21. You land on the TrueNAS Dashboard. The NAS is running.

> [!IMPORTANT]
> **Reserve the NAS's IP address on your router.**
> The NAS got its current IP from your router's DHCP server. After a router reboot or a DHCP lease expiry, the IP can change — and every bookmark, every Jellyfin app on a TV, every script that hard-codes the IP will break.
>
> Two ways to lock it in:
>
> - **DHCP reservation** (recommended): in your router's admin page, find DHCP / LAN settings, find the NAS by its MAC address, and reserve its current IP. Easy to undo, leaves the IP visible from the router.
> - **Static IP on the NAS**: in TrueNAS, Network → Interfaces → edit the interface, set a static address outside your DHCP pool, set the gateway and DNS. More fiddly; if you get it wrong you have to plug a monitor back in to fix.
>
> You only need this for local access. Tailscale (Part 5) handles remote access independently.

> [!TIP]
> TrueNAS Community Edition is completely free. No license, no trial, no expiry.

## Part 3 — Create Storage Pools

A pool is a logical storage container that spans one or more physical
drives. You will create two pools: one on the HDDs for media and data,
one on the SSD for app databases and working files.

Before you start: write down the serial number from the sticker on the
back of each IronWolf drive. TrueNAS shows drives by model and serial
number so you can tell them apart.

### Pool 1: tank — HDD Mirror

| **Setting** | **Value** |
|:---|:---|
| Pool name | tank |
| Disks | Both 8 TB IronWolf HDDs |
| Layout | Mirror — both drives store identical data. One can fail without losing anything. |
| Purpose | Media, photos, completed downloads, backups |

1. In TrueNAS, click Storage in the left sidebar.

2. Click Create Pool in the top right corner.

3. In the Name field, type: tank

4. Under Available Disks, find both 8 TB IronWolf drives. Tick the
   checkbox next to each one.

5. Click Add Vdev, then select Mirror. Both drives move into the mirror
   vdev area. This is correct — a mirror means both drives store the
   same data.

6. Confirm the layout shows Mirror with both drives inside it.

7. Click Create Pool. TrueNAS asks you to confirm by typing a word.
   Type exactly what it asks and confirm. Pool creation takes about 1
   minute.

> [!WARNING]
> Creating a pool erases the selected drives. Make sure both IronWolf drives are empty and you have NOT selected the SSDs.

### Pool 2: apps — SSD

| **Setting** | **Value**                                                    |
|:------------|:-------------------------------------------------------------|
| Pool name   | apps                                                         |
| Disk        | SSD 2 only (single disk) **or** two SSDs in a mirror         |
| Layout      | Single disk — no redundancy, restored from the nightly tank backup if it fails |
| Purpose     | App databases, transcode temp, incomplete downloads, scripts |

> [!TIP]
> **Single SSD or mirrored pair?**
> The single-disk layout assumes you can tolerate losing up to 24 hours of state if the apps SSD fails (the gap between the most recent nightly backup and the failure). For Immich uploads, Sonarr/Radarr import history, qBittorrent state, and Jellyfin watch progress, that window may or may not matter to you. For better durability, use **two SSDs in a mirror** — the procedure is the same as the tank pool above, just substitute SSDs. This trades the cost of a second SSD for zero data-loss window. See Part 0's apps SSD callout for the full trade-off.

8. Still in Storage, click Create Pool again.

9. Name it: apps

10. Select your apps SSD(s). Do not select the boot SSD (TrueNAS is
    installed there — it should not appear in the list at all) and do
    not select the HDDs.

11. For a single-SSD pool: click Add Vdev, then Stripe. A single-drive
    pool has no redundancy — restoration relies on the nightly backup
    in Part 8. For a two-SSD mirror: click Add Vdev, then Mirror, same
    as the tank pool.

12. Click Create Pool and confirm.

> [!NOTE]
> **Why not use the SSD as ZFS cache?**
> 
> A ZFS L2ARC (read cache) only helps when the same blocks are read repeatedly *and* RAM is already exhausted. With 32 GB of RAM, ARC handles the cacheable read workload of this stack on its own.
>
> SLOG (separate intent log) is a different SSD-on-ZFS use case — and also wrong here. SLOG only accelerates **sync writes** (NFS, iSCSI, databases doing `fsync` over the network). The bulk of this stack does buffered I/O over local volume mounts, so SLOG would sit idle.
>
> The real win is putting the Immich database, Jellyfin metadata, active torrent writes, and transcode temp files on SSD as a **separate fast pool** — random, constant, small writes that would otherwise hammer the HDDs. That's what the apps pool above is doing.

> [!NOTE]
> **TRIM / autotrim is on by default for SSD pools**
> 
> Recent TrueNAS releases (24.10+) enable `autotrim` automatically when you create a pool on an SSD. Nothing to configure manually for the typical case.
>
> If you want to verify: **Storage** → click your apps pool → **Edit** → look for the **Auto TRIM** checkbox. It should already be checked. If you ever need to run TRIM by hand (e.g. after a large delete), the same screen has a **Trim** button.

## Part 4 — Create Datasets and Folders

A dataset is like a special folder with extra powers. It can have its
own snapshots, permissions, and security settings. You use datasets
instead of plain folders because TrueNAS can protect and snapshot them
independently.

> [!NOTE]
> **The path rule**
> 
> If the pool is named apps and you create a dataset called appdata, the full path is /mnt/apps/appdata. If the pool is named tank and you create a dataset called photos, the full path is /mnt/tank/photos. The pool name is always part of the path.

How to create one dataset: In TrueNAS, click Storage, click the
three-dot menu (⋮) next to the pool name, click Add Dataset, type the
dataset name, and click Save. Repeat for each dataset below.

### Datasets on the apps pool (SSD)

| **Dataset path** | **Purpose** |
|:---|:---|
| apps/appdata | All app configs and databases — Jellyfin, Sonarr, Radarr, Immich, and all others |
| apps/scripts | Your maintenance scripts and config.env |
| apps/backups | Temporary local backup workspace |
| apps/transcode | Jellyfin transcoding temp files — heavy I/O belongs on SSD |
| apps/downloads-incomplete | Active qBittorrent incomplete downloads — constant writes belong on SSD |

### Datasets on the tank pool (HDD mirror)

| **Dataset path** | **Purpose** |
|:---|:---|
| tank/data | Media AND completed downloads in one dataset — critical for hardlinks (see note below) |
| tank/photos | Immich photo library — important personal data belongs on mirrored storage |
| tank/realdebrid | rclone/Zurg virtual mount target (Phase 2 — Part 13) |
| tank/backups | Nightly backups of app configs from the SSD — protects against SSD failure |

> [!IMPORTANT]
> tank/data must be ONE dataset. Do not create tank/data/media or tank/data/downloads as separate datasets.
> Sonarr and Radarr can only hardlink files inside the same ZFS dataset. A hardlink is an instant file move — no copying, no waiting for a 50 GB file to be duplicated. If downloads are in one dataset and media is in another, every import becomes a slow copy-then-delete.
> By keeping /mnt/tank/data/downloads/ and /mnt/tank/data/media/ inside the single tank/data dataset, imports are instant.
> Triple-check in TrueNAS: Storage → tank pool. You should see "data" as a dataset. You should NOT see "media" or "downloads" as separate datasets under it. If they exist as datasets, delete them before adding any files.

### Dataset Security Settings — Download Folders Only

For the two download datasets, disable the ability for files to execute
as programs. This means even if a downloaded file is secretly malware,
it cannot run itself.

How to set: click the three-dot menu next to the dataset → Edit →
Advanced Options. Look for ZFS Exec, ZFS Setuid, ZFS Devices.

| **Dataset** | **ZFS Exec** | **ZFS Setuid** | **ZFS Devices** | **Why** |
|:---|:---|:---|:---|:---|
| apps/downloads-incomplete | Disabled | Disabled | Disabled | Active downloads are untrusted files |
| tank/data | Disabled | Disabled | Disabled | Downloads and completed media stay here — untrusted until imported |
| All other datasets | Enabled (default) | Disabled | Enabled (default) | App databases and media need normal access |

### Create the Folders

After datasets exist, create the subfolders. Open TrueNAS Shell (click
the \>\_ icon in the top-right corner of the TrueNAS web page). A black
command window opens.

Paste each block and press Enter:

```bash
# App SSD subfolders
mkdir -p /mnt/apps/appdata/{bazarr,immich-db,immich-ml,jellyfin,lidarr,navidrome,prowlarr,qbittorrent,radarr,rclone,seerr,sonarr,tailscale,zurg}
mkdir -p /mnt/apps/{backups,scripts,transcode/jellyfin,downloads-incomplete}
# Tank HDD subfolders — all under tank/data (one dataset, instant hardlinks)
mkdir -p /mnt/tank/data/media/{movies,tv,music}
mkdir -p /mnt/tank/data/downloads/complete/{movies,tv,music}
mkdir -p /mnt/tank/data/downloads/quarantine
# Other tank folders
mkdir -p /mnt/tank/photos/library
mkdir -p /mnt/tank/backups/configs
# Note: /mnt/tank/realdebrid is created in Part 13 by the rclone mount
# script — do not pre-create subfolders here, the FUSE mount overlays them.
```

### Set Permissions

Apps run as user 568 (the TrueNAS apps user). Give that user ownership
of the folders so apps can read and write their data. Without this step,
apps fail with permission errors.

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
> **Scripts folder ownership is captured at install time**
>
> The `chown -R "$SCRIPT_OWNER":568 /mnt/apps/scripts` command snapshots whoever is logged into the shell right now (typically `truenas_admin`). The group is set to `568` (the apps group) so containers can still read the scripts when needed.
>
> If you ever change your TrueNAS admin user, log in as a different admin to edit scripts, or delete and recreate the admin account, you may hit "Permission denied" when editing files in `/mnt/apps/scripts`. The fix is one command, run as the new admin in the TrueNAS Shell:
>
> ```bash
> sudo chown -R "$(id -un)":568 /mnt/apps/scripts
> ```
>
> Cron runs as root regardless of file ownership, so scheduled jobs from Part 8 are unaffected — this only matters for hand-editing.

> [!NOTE]
> **Immich database exception**
> 
> The Immich database container runs internally as user 999, not 568. You must give that user ownership of just the Immich database folder:
> chown -R 999:999 /mnt/apps/appdata/immich-db
> 
> Important: run this command again any time you restore from backup, recreate the folder, or do a broad permission repair. A backup restore can accidentally put the database folder back under user 568, causing Immich to fail with permission errors on the next start.

> [!NOTE]
> **Find the GPU render group ID**
> 
> Jellyfin and Immich need to join the GPU render group. Find its number by running this in TrueNAS Shell:
> getent group render
> \# Output looks like: render:x:107:
> \# The number (107 in this example) is your RENDER_GID.
> \# Write it down — you will put it in config.env in the next part.

> [!NOTE]
> **Verify /dev/dri exists before continuing**
>
> After TrueNAS is installed and running (no USB, no monitor), confirm the Intel iGPU is actually visible to the OS. In TrueNAS Shell:
>
> ```bash
> ls /dev/dri
> # You should see: card0 renderD128
> ```
>
> If the output is empty, the kernel did not bind a graphics driver to your iGPU. First check BIOS (Step 11 in Part 2 — Primary Display = IGFX/iGPU). If BIOS looks right and `/dev/dri` is still empty, you may need the kernel-args workaround in the next callout.
>
> An empty `/dev/dri` is the most common reason Jellyfin hardware transcoding appears to work but does nothing — the container starts, VAAPI is configured, and software transcoding silently takes over.

> [!NOTE]
> **i915 / xe `force_probe` — only if `/dev/dri` is empty after BIOS is correct**
>
> If you used the **i5-13500 build** from Part 1 (or any 12th–14th gen Intel), this section does not apply — the i915 driver in the TrueNAS 25.10 kernel recognises those iGPUs out of the box.
>
> If you used the **Arrow Lake reference build** (Intel Core Ultra on the MAXSUN B860 board) and `/dev/dri` is empty after a clean install, you may need to force the graphics driver to bind to your iGPU's PCI device ID.
>
> **First, find your iGPU's device ID** in the TrueNAS Shell:
>
> ```bash
> lspci -nn | grep -i 'vga\|display'
> # Example output: 00:02.0 VGA compatible controller [0300]: Intel Corporation Device [8086:7d51]
> # The 4-character hex value after "8086:" is your device ID (e.g. 7d51).
> ```
>
> **Then add a kernel argument** (NOT a sysctl — driver probing happens at boot, before sysctls take effect): in the TrueNAS UI, go to **System Settings → Advanced Settings → Kernel Args → Add**, and set the value to one of:
>
> - `i915.force_probe=<your-id>` — for older Intel iGPUs not yet in the i915 driver's PCI ID list (most cases).
> - `xe.force_probe=<your-id>` — for newer Arrow Lake / Lunar Lake / Battlemage iGPUs that are handled by the new `xe` driver.
> - `i915.force_probe=*` (wildcard) — last resort if you don't know which driver claims your iGPU. Forces the i915 driver to probe every unknown Intel GPU. Safe on a dedicated NAS.
>
> Reboot the NAS, then verify:
>
> ```bash
> ls /dev/dri
> # Should now show: card0 renderD128
> ```
>
> If neither driver binds, your kernel is older than your CPU's iGPU support. Update TrueNAS to the latest 25.10.x or newer.

## Part 5 — Remote Access: Tailscale First

Install Tailscale before anything else. It gives you a secure private
tunnel from your phone and laptop to the NAS — from anywhere in the
world — without opening any router ports.

> [!IMPORTANT]
> Do not open any router port forwards while building this system. Not for TrueNAS, not for Jellyfin, not for any app. Tailscale provides all remote access. This is the single most important security decision in the guide.

### Step 5.1 — Create a Tailscale Account and Auth Key

1. Go to tailscale.com and create a free account.

2. Go to tailscale.com/settings/keys and click "Generate auth key".

3. Set it to Reusable and set expiry to No expiry. A home server should
   not go offline because a key expired while you were away. A
   one-time-use key will work for the first boot but the NAS will
   silently lose its Tailscale identity the next time the container is
   recreated (after a stack update or a TrueNAS upgrade). Reusable +
   non-expiring means the NAS reconnects automatically every time.

4. Copy the key. It looks like: tskey-auth-kXXXXXXXXXXX. Save it
   somewhere safe — you will put it in config.env in Part 6.

> [!NOTE]
> **How Tailscale identity is preserved across restarts**
> 
> The Tailscale container stores its authenticated state (its identity on your private network) in the volume mounted at /var/lib/tailscale — which maps to /mnt/apps/appdata/tailscale on your SSD.
> As long as that folder exists and has the correct files, the NAS reconnects automatically after any restart, update, or container recreation without needing a new auth key.
> 
> If you ever delete /mnt/apps/appdata/tailscale or recreate the apps pool, the NAS loses its Tailscale identity and needs the auth key again. That is why the key must be Reusable — so you can re-authenticate without generating a new one.
> 
> The nightly config backup (Part 8) backs up this folder to the mirrored HDD, so even an apps SSD failure does not permanently lose your Tailscale identity.

### Step 5.2 — Connect Your Phone

5. Install the Tailscale app on your phone — free on App Store and
   Google Play.

6. Sign in with the same Tailscale account.

7. Tap the toggle to connect. Leave Tailscale running in the background
   permanently — it uses almost no battery and connects automatically
   when you leave home.

### Step 5.3 — Enable MagicDNS (Recommended)

MagicDNS gives your NAS a readable name instead of a number like
100.64.12.34.

8. Go to tailscale.com/admin/dns in your browser.

9. Click Enable MagicDNS. Your NAS becomes reachable by name instead of
   IP. There are two name forms — both resolve to the same NAS:
   - **Short name:** `truenas-nas` (the hostname you set in the
     Tailscale container). Works in browsers and most apps when the
     device is using your tailnet's MagicDNS resolver.
   - **Full FQDN:** `truenas-nas.<your-tailnet>.ts.net`. Works
     everywhere, including iOS browsers and apps that bypass system
     DNS. Use this form if a short name fails to resolve. Find your
     tailnet name at the top of `tailscale.com/admin/machines`.

> [!TIP]
> After setup is complete, your NAS has two addresses: a local IP (e.g. 192.168.1.50) for when you are at home, and a Tailscale IP (e.g. 100.64.12.34 or the MagicDNS name) for when you are away. The apps work the same way at both addresses.

### Access Model

| **App** | **At home (local)** | **Away from home** | **Reasoning** |
|:---|:---|:---|:---|
| Jellyfin, Immich, Navidrome, Seerr | NAS IP | Tailscale IP | Family watches locally, you watch remotely |
| qBittorrent, Sonarr, Radarr, Prowlarr, etc. | NAS IP | Tailscale IP (admin only) | Management tools — no need to be public |
| TrueNAS web page | NAS IP | Tailscale IP only | Never expose the storage OS to the internet |

## Part 6 — The Docker Stack

The entire app stack is defined in two files: config.env (your personal
settings) and docker-compose.yml (the app blueprint). You create both
files, then deploy through the TrueNAS Apps UI.

### Step 6.1 — Create config.env

config.env is a plain text file that holds all your settings in one
place. Every script and every container reads from it so you never type
the same value twice.

1. Open TrueNAS Shell (click the \>\_ icon in the top-right corner).

2. Paste this to create the scripts folder and open the config file in
   the nano editor:

```bash
mkdir -p /mnt/apps/scripts
nano /mnt/apps/scripts/config.env
```

3. The nano editor opens. Paste the template below:

```bash
TZ="Asia/Jerusalem"
PUID="568"
PGID="568"
RENDER_GID="107"
TS_AUTHKEY=""
QBIT_PASSWORD="ChangeMe_qBit123"
POSTGRES_USER="immich"
POSTGRES_DB="immich"
POSTGRES_PASSWORD="ChangeMe_DB456"
DB_HOSTNAME="immich-db"
DB_USERNAME="immich"
DB_DATABASE_NAME="immich"
DB_PASSWORD="ChangeMe_DB456"
REDIS_HOSTNAME="immich-redis"
WAIT_FOR_RD="0"
ENABLE_USB_BACKUP="1"
USB_UUID=""
WEBHOOK_URL=""
INCOMPLETE_DAYS="14"
```

4. Now fill in YOUR values by moving the cursor to each line and
   changing the value:

| **Variable** | **What to put here** | **Where to find it** |
|:---|:---|:---|
| TZ | Your timezone, e.g. Asia/Jerusalem | Full list at: en.wikipedia.org/wiki/List_of_tz_database_time_zones |
| RENDER_GID | The GPU render group number | The default `107` is correct on most TrueNAS installs. Verify with: `getent group render` — use the middle number if it differs. |
| TS_AUTHKEY | Your Tailscale auth key | From tailscale.com/settings/keys — starts with tskey-auth-k... |
| QBIT_PASSWORD | A strong password for qBittorrent | You choose — 12+ characters |
| POSTGRES_PASSWORD + DB_PASSWORD | One strong password used in two places | You choose — both must be the **same value**. POSTGRES_PASSWORD configures the Immich Postgres container; DB_PASSWORD is what the Immich app uses to connect. If they differ, Immich cannot reach its own database. |
| USB_UUID | Leave blank for now | Fill in Part 9 after plugging in a USB drive |
| WEBHOOK_URL | Leave blank for now | Optional — fill in Part 12 if you want alerts |

5. Press Ctrl+X, then Y, then Enter to save.

### Step 6.2 — Create docker-compose.yml

This file is the blueprint that tells Docker which apps to run, which
folders they can access, which ports they use, and which containers can
talk to each other.

> [!IMPORTANT]
> **Hardlink rule (and one full-copy step you should know about).**
> qBittorrent, Sonarr, Radarr, and Lidarr all mount `/mnt/tank/data` as `/data` inside the container. This is deliberate: when Sonarr/Radarr/Lidarr move a completed download from `/data/downloads/complete/` to `/data/media/`, both paths live in the same `tank/data` ZFS dataset, so the move is an instant hardlink — no copying.
>
> **One exception worth knowing:** qBittorrent first writes incomplete downloads to `/mnt/apps/downloads-incomplete/` (apps SSD pool) for fast random writes during torrenting. When the torrent finishes, qBittorrent moves the file to `/mnt/tank/data/downloads/complete/` (tank HDD pool). That move crosses ZFS pools, so it is a **full file copy**, not a hardlink. A 50 GB movie copies 50 GB once at completion. The trade-off is intentional: SSD absorbs the random-write churn while torrenting, and the one-time HDD copy at the end is sequential and fast. After that, the Sonarr/Radarr import to `/data/media/` is the instant hardlink described above.

6. In TrueNAS Shell, open the compose file in nano:

```bash
nano /mnt/apps/scripts/docker-compose.yml
```
7. Paste the full compose file below. When done, press Ctrl+X → Y →
   Enter to save.

```yaml
x-logging: &default-logging
  driver: "json-file"
  options:
    max-size: "10m"
    max-file: "3"

services:
  # ── TAILSCALE ──────────────────────────────────────────────────
  tailscale:
    image: tailscale/tailscale:latest
    container_name: tailscale
    hostname: truenas-nas
    env_file: [/mnt/apps/scripts/config.env]
    environment: [TS_STATE_DIR=/var/lib/tailscale]
    volumes:
      - /mnt/apps/appdata/tailscale:/var/lib/tailscale
      - /dev/net/tun:/dev/net/tun
    cap_add: [NET_ADMIN, NET_RAW]
    network_mode: host
    logging: *default-logging
    restart: unless-stopped

  # ── REAL-DEBRID (Phase 2 — comment out all lines of both services
  # until Part 13. Add # at the start of each line below.) ─────
  zurg:
    image: ghcr.io/debridmediamanager/zurg-testing:latest
    container_name: zurg
    restart: unless-stopped
    # Zurg only needs its config folder and a port to serve WebDAV.
    # Do NOT mount /mnt/tank/realdebrid here — rclone-mount.sh manages
    # that host path. Mounting it from both Zurg and rclone causes a
    # FUSE "transport endpoint is not connected" error.
    volumes:
      - /mnt/apps/appdata/zurg:/config
    ports: ["9999:9999"]
    logging: *default-logging

  # ── DOWNLOADERS ─────────────────────────────────────────────────
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
    logging: *default-logging
    restart: unless-stopped

  # ── INDEXERS / AUTOMATION ───────────────────────────────────────
  prowlarr:
    image: lscr.io/linuxserver/prowlarr:latest
    container_name: prowlarr
    env_file: [/mnt/apps/scripts/config.env]
    volumes: ["/mnt/apps/appdata/prowlarr:/config"]
    ports: ["9696:9696"]
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
    logging: *default-logging
    restart: unless-stopped

  bazarr:
    image: lscr.io/linuxserver/bazarr:latest
    container_name: bazarr
    env_file: [/mnt/apps/scripts/config.env]
    volumes:
      - /mnt/apps/appdata/bazarr:/config
      - /mnt/tank/data/media/movies:/movies
      - /mnt/tank/data/media/tv:/tv
    ports: ["6767:6767"]
    logging: *default-logging
    restart: unless-stopped

  # ── MEDIA SERVERS ────────────────────────────────────────────────
  jellyfin:
    image: lscr.io/linuxserver/jellyfin:latest
    container_name: jellyfin
    env_file: [/mnt/apps/scripts/config.env]
    devices: [/dev/dri:/dev/dri]
    group_add: ["${RENDER_GID}"]
    volumes:
      - /mnt/apps/appdata/jellyfin:/config
      - /mnt/apps/transcode/jellyfin:/transcode
      - /mnt/tank/data/media/movies:/media/movies:ro
      - /mnt/tank/data/media/tv:/media/tv:ro
      - /mnt/tank/realdebrid:/media/realdebrid:ro
    ports: ["8096:8096"]
    logging: *default-logging
    restart: unless-stopped

  navidrome:
    image: deluan/navidrome:latest
    container_name: navidrome
    env_file: [/mnt/apps/scripts/config.env]
    volumes:
      - /mnt/apps/appdata/navidrome:/data
      - /mnt/tank/data/media/music:/music:ro
    ports: ["4533:4533"]
    logging: *default-logging
    restart: unless-stopped

  # ── PHOTOS ──────────────────────────────────────────────────────
  immich-server:
    image: ghcr.io/immich-app/immich-server:release
    container_name: immich-server
    env_file: [/mnt/apps/scripts/config.env]
    devices: [/dev/dri:/dev/dri]
    group_add: ["${RENDER_GID}"]
    volumes:
      - /mnt/tank/photos/library:/usr/src/app/upload
    ports: ["2283:2283"]
    depends_on: [immich-db, immich-redis]
    logging: *default-logging
    restart: unless-stopped

  immich-machine-learning:
    image: ghcr.io/immich-app/immich-machine-learning:release
    container_name: immich-machine-learning
    env_file: [/mnt/apps/scripts/config.env]
    volumes: ["/mnt/apps/appdata/immich-ml:/cache"]
    logging: *default-logging
    restart: unless-stopped

  immich-redis:
    # Immich switched from Redis to Valkey 8 (drop-in fork) in late 2025.
    # The service name "immich-redis" is preserved so the REDIS_HOSTNAME
    # in config.env still resolves.
    image: docker.io/valkey/valkey:8-bookworm
    container_name: immich-redis
    logging: *default-logging
    restart: unless-stopped

  immich-db:
    image: ghcr.io/immich-app/postgres:16-vectorchord0.4.3-pgvectors0.2.0
    container_name: immich-db
    env_file: [/mnt/apps/scripts/config.env]
    volumes: ["/mnt/apps/appdata/immich-db:/var/lib/postgresql/data"]
    logging: *default-logging
    restart: unless-stopped

  # ── REQUESTS ────────────────────────────────────────────────────
  seerr:
    image: ghcr.io/seerr-team/seerr:latest
    container_name: seerr
    env_file: [/mnt/apps/scripts/config.env]
    volumes: ["/mnt/apps/appdata/seerr:/app/config"]
    ports: ["5055:5055"]
    logging: *default-logging
    restart: unless-stopped
```

### Step 6.3 — Deploy via TrueNAS Apps UI

Do not use "docker compose up -d" in the Shell as the normal way to run
the stack. TrueNAS is an appliance and its Apps system should own app
deployment so it can manage updates and restarts properly.

8. In TrueNAS, click Apps in the left sidebar.

9. Click Discover Apps.

10. Click the three-dot menu (⋮) in the top-right area of the screen.

11. Click "Install via YAML".

12. Give it the name: media-stack

13. In the YAML box, paste the full contents of the docker-compose.yml
    file you just saved. Or if your TrueNAS version supports it, use
    this simpler YAML instead:

```yaml
include:
  - /mnt/apps/scripts/docker-compose.yml
```

14. Click Save. Wait for TrueNAS to deploy all containers.

15. Go to Apps \> Installed. Confirm media-stack shows all containers as
    Running.

> [!NOTE]
> **If TrueNAS complains about RENDER_GID**
> 
> Some TrueNAS YAML screens do not apply config.env substitutions at compose-time. If you see an error about \${RENDER_GID}, replace those two instances in the YAML with the actual number (e.g. 107):
> group_add: ["107"]
> 
> Only replace the two group_add entries — do not change any other variables.

> [!NOTE]
> **If TrueNAS blocks a host path (SMB + Apps conflict)**
> 
> TrueNAS (since 24.10 Electric Eel, still present in 25.10 Goldeye) has a safety feature that blocks an app from using a host path that is also shared via SMB. This is common if you share /mnt/tank/data over SMB so you can drag files from your PC. You will see an error like "Host path is already in use" or "Host Path Safety Check" when deploying the stack.
> Fix: Apps > Settings > Advanced Settings > uncheck "Enable Host Path Safety Checks" > Save > redeploy media-stack.
> 
> This is a conscious choice, not a random click. The folder permissions set in Part 4 keep the data safe. The safety check exists to prevent accidents — disabling it is fine as long as you understand that both SMB and Docker containers will be touching the same folders.
> 
> Why you might have SMB enabled: it is useful for copying large files (movies, music) from your PC directly onto the NAS before asking Sonarr or Radarr to manage them. SMB and Docker containers can share the same data folder safely — TrueNAS is just being cautious by default.

> [!NOTE]
> **Zurg at first launch**
> 
> Zurg will fail to start until you create its config file in Part 13. That is expected. Comment out the zurg service (add # to the start of every line of that section) until you reach Part 13. Everything else should start correctly.

### Verify the stack started correctly

Wait 2-3 minutes after deployment, then check in TrueNAS Apps \>
Installed \> media-stack. Most containers should show Running. If any
show an error, check the container logs by clicking on it in the TrueNAS
UI.

For quick Shell checks (read-only troubleshooting only):

```bash
docker ps --format "table {{.Names}}\t{{.Status}}"
# Most should show "Up (X minutes)"
# Zurg will show Restarting until Part 13 — that is expected
# If Jellyfin or other apps fail, check logs:
docker logs jellyfin | tail -30
```

## Part 7 — ZFS Snapshots

Snapshots are save points. TrueNAS takes a photograph of your data
automatically so you can roll back to any previous state if an update
breaks something, a file gets accidentally deleted, or an import goes
wrong.

> [!IMPORTANT]
> Snapshot retention warning: ZFS snapshots hold on to old disk blocks even after files are deleted or moved.
> If you move a 50 GB movie from downloads to media while a snapshot still remembers the old location, that 50 GB stays on disk until the snapshot expires.
> Keep tank/data retention at 14 days or less at first. Long retention can make tank look full even after you have deleted files.
> This is normal ZFS behaviour — just keep retention times reasonable.

> [!WARNING]
> Do NOT install sanoid via apt-get or any package manager on TrueNAS SCALE. It is unsupported on the TrueNAS base OS and a system update can break or remove it. Use the built-in UI snapshots below instead.

### Create Snapshot Tasks in TrueNAS UI

Go to Data Protection in the left sidebar \> Periodic Snapshot Tasks \>
Add. Create one task for each row below:

The Add form has these fields: Dataset (type or select the path),
Recursive (Yes = also snapshot sub-datasets), Snapshot Lifetime (how
long to keep each snapshot), Schedule (how often to take a new one).

| **Dataset** | **Recursive** | **Schedule** | **Retention** | **Why** |
|:---|:---|:---|:---|:---|
| apps/appdata | Yes | Every 4 hours | 7 days | App configs change often — roll back quickly if an update breaks an app |
| apps/scripts | Yes | Daily at 00:30 | 14 days | `config.env` and all maintenance scripts. Small dataset, easy to lose to a bad edit, cheap to snapshot. |
| tank/photos | Yes | Daily at 01:00 | 30 days | Photos are precious — long retention |
| tank/data | Yes | Daily at 01:30 | 14 days | Media and downloads together — keep retention short (see warning above) |
| tank/backups | Yes | Daily at 02:00 | 30 days | Config tarballs — long retention |

### How to Roll Back a Snapshot

If something goes wrong — bad import, broken update, accidental
deletion:

1. **Stop the affected app first** in Apps → Installed → media-stack →
   click the container → Stop. Rolling back a dataset while a
   container is running can corrupt databases that have open file
   handles (Postgres especially). For Immich, stop both `immich-server`
   and `immich-db` before rolling back `apps/appdata`.

2. Go to Storage → find the dataset → click Snapshots.

3. Find the snapshot from before the problem. Click it.

4. Select Rollback. TrueNAS shows a warning and asks you to confirm by
   typing exactly what it shows in the dialog.

5. The dataset rolls back to that point in time. Start the app(s) you
   stopped in step 1 from Apps → Installed.

> [!WARNING]
> Rolling back destroys all changes made after that snapshot. Only roll back the specific dataset that has the problem (e.g. apps/appdata), never the entire tank pool unless you truly mean to revert all your media.

## Part 8 — Maintenance Scripts

Five scripts automate predictable, low-risk maintenance tasks. None of
them delete your media library. All use "set -Eeuo pipefail" which means
they fail loudly if something goes wrong instead of silently continuing.

> [!NOTE]
> **How to create these scripts**
> 
> Each script is created with nano. Open TrueNAS Shell, run the nano command shown, paste the script content, then press Ctrl+X → Y → Enter to save. Then run chmod +x on the file to make it runnable.
> \# How to make a script runnable:
> chmod +x /mnt/apps/scripts/script-name.sh
> 
> \# How to test a script immediately:
> bash /mnt/apps/scripts/script-name.sh

### Script 1: backup-app-config.sh — Nightly SSD Backup

This is the most important script. The apps SSD has no redundancy. This
backs it up to the mirrored HDD pool every night. If the SSD dies, you
restore from here.

> [!IMPORTANT]
> **Backup from a ZFS snapshot, not from live data.**
>
> The naive way to back up a running stack is `tar` against `/mnt/apps/appdata` directly. The problem: while `tar` is reading, Postgres (Immich's database) is still writing. The result is a "torn" backup of the database — files captured mid-transaction, possibly unrestorable.
>
> The script below uses the most recent **periodic snapshot** of `apps/appdata` (taken every 4 hours per Part 7). A ZFS snapshot is atomic across the whole dataset, so all Postgres files inside it are guaranteed to be a coherent moment in time. The backup runs against the snapshot's read-only view; live writes to the dataset don't affect what `tar` sees.

```bash
nano /mnt/apps/scripts/backup-app-config.sh
```

Paste this content:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

DATASET="apps/appdata"
SCRIPT_SRC="/mnt/apps/scripts"
DEST="/mnt/tank/backups/configs"
DATE="$(date +%F_%H-%M-%S)"
LOG="/mnt/apps/scripts/backup-app-config.log"

mkdir -p "$DEST"
echo "[backup] Starting $DATE" >> "$LOG"

# Find the most recent snapshot of apps/appdata (sorted by creation time).
LATEST_SNAP=$(zfs list -H -t snapshot -o name -s creation -d 1 "$DATASET" 2>/dev/null | tail -1 || true)

if [ -n "$LATEST_SNAP" ]; then
    SNAP_NAME="${LATEST_SNAP##*@}"
    APPDATA_SRC="/mnt/apps/appdata/.zfs/snapshot/$SNAP_NAME"
    echo "[backup] Using snapshot: $SNAP_NAME" >> "$LOG"
else
    APPDATA_SRC="/mnt/apps/appdata"
    echo "[backup] WARNING: no snapshot found, falling back to live data (Postgres may be inconsistent)." >> "$LOG"
fi

# Build the tarball. --transform rewrites the snapshot path back to the
# live path so restoration goes to /mnt/apps/appdata/, not into a .zfs dir.
ARCHIVE="$DEST/app-config-$DATE.tar.gz"
tar -czf "$ARCHIVE" \
    --transform "s|^${APPDATA_SRC#/}|mnt/apps/appdata|" \
    "$APPDATA_SRC" "$SCRIPT_SRC" >> "$LOG" 2>&1

# Verify the tarball is readable end-to-end. A truncated archive will
# pass `set -e` checks but fail to extract at restore time.
if ! tar -tzf "$ARCHIVE" >/dev/null 2>>"$LOG"; then
    echo "[backup] FAILED: archive is corrupt, removing." >> "$LOG"
    rm -f "$ARCHIVE"
    exit 1
fi

# Rotate: keep 30 days.
find "$DEST" -name "app-config-*.tar.gz" -type f -mtime +30 -print -delete >> "$LOG" 2>&1

echo "[backup] Finished $DATE OK ($(du -h "$ARCHIVE" | cut -f1))" >> "$LOG"
```

```bash
chmod +x /mnt/apps/scripts/backup-app-config.sh
```

### Script 2: photo-backup-usb.sh — USB Drive Photo Backup

Creates a physical copy of your photos on a USB drive. The USB is
unmounted when not in use so it cannot be affected by ransomware or NAS
problems.

> [!NOTE]
> **The script is best-effort and tolerates the USB being unplugged.**
> The whole offline-protection design relies on you keeping the USB drive *unplugged* between runs. The script exits cleanly (exit 0) if the drive isn't mounted, so the cron job stays quiet on the days the USB isn't connected. Plug it in once a week (or on whatever cadence works), let the next 06:00 run pick it up, and unplug it again. Failures are logged to `photo-backup-usb.log` for review.

First, plug in your USB drive and find its UUID:

```bash
blkid
# Find the line matching your USB drive (small size, type ext4 or exfat)
# Copy the UUID value — it looks like: a1b2c3d4-e5f6-7890-abcd-123456789012
# Add it to config.env: USB_UUID="your-uuid-here"
```

```bash
nano /mnt/apps/scripts/photo-backup-usb.sh
```

Paste this content:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
source /mnt/apps/scripts/config.env

LOG="/mnt/apps/scripts/photo-backup-usb.log"
echo "[photo-backup] Starting $(date)" >> "$LOG"

[ "${ENABLE_USB_BACKUP:-0}" != "1" ] && { echo "[photo-backup] Disabled in config.env, exiting." >> "$LOG"; exit 0; }
[ -z "${USB_UUID:-}" ]              && { echo "[photo-backup] No USB_UUID set, exiting." >> "$LOG"; exit 0; }

MOUNT=/mnt/usb-photo-backup
mkdir -p "$MOUNT"

if ! mount UUID="$USB_UUID" "$MOUNT" 2>>"$LOG"; then
    echo "[photo-backup] USB drive not present (UUID $USB_UUID). Exiting cleanly." >> "$LOG"
    exit 0
fi

if ! mountpoint -q "$MOUNT"; then
    echo "[photo-backup] USB drive did not mount. Stopping." >> "$LOG"
    exit 1
fi

trap 'umount "$MOUNT" 2>/dev/null || true' EXIT

rsync -a --ignore-existing --no-perms /mnt/tank/photos/library/ "$MOUNT/photos/" >> "$LOG" 2>&1
sync

echo "[photo-backup] Finished $(date) OK" >> "$LOG"
```

```bash
chmod +x /mnt/apps/scripts/photo-backup-usb.sh
```

### Script 3: cleanup-downloads.sh — Daily Junk Removal

Removes torrent junk files from completed downloads and cleans up stale
incomplete downloads. Does NOT touch your media library.

> [!WARNING]
> **Skip this script if you seed torrents.**
> Deleting `.nfo` / `.sfv` / `.url` files from a torrent's directory makes qBittorrent's "this torrent is complete with these files" state diverge from reality. On the next re-check, qBittorrent flips the torrent to "missing files" / "incomplete" and stops seeding. For users on private trackers with ratio requirements, that silently destroys ratio.
>
> If you seed: don't schedule this in cron. Either rely on Sonarr/Radarr's own "import then leave the torrent untouched" handling, or run cleanup manually after a torrent is removed from qBittorrent.
>
> If you don't seed (Real-Debrid only, public-only with no ratio): the script below is safe.

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

# Delete torrent junk: NFO (release info), SFV (CRC checksums), URL (link
# shortcuts). These three are reliably junk. Older versions of this script
# also matched *.txt, *sample*, *featurette* — those have been removed
# because each can hit legitimate content (liner-notes .txt files, movies
# with "sample" in the title, actual featurette extras worth keeping).
# -not -path '*/.*' skips hidden folders including .zfs snapshot directories.
find "$COMPLETE" -not -path '*/.*' -type f \( \
    -iname "*.nfo" -o -iname "*.sfv" -o -iname "*.url" \
\) -print -delete >> "$LOG" 2>&1

# Delete stale incomplete downloads (configurable via INCOMPLETE_DAYS in
# config.env; default 14).
find "$INCOMPLETE" -not -path '*/.*' -type f -mtime +"$INCOMPLETE_DAYS" -print -delete >> "$LOG" 2>&1

# Remove empty folders left behind.
find "$COMPLETE" "$INCOMPLETE" -not -path '*/.*' -type d -empty -print -delete >> "$LOG" 2>&1

echo "[cleanup] Finished $(date)" >> "$LOG"
```

```bash
chmod +x /mnt/apps/scripts/cleanup-downloads.sh
```

### Script 4: scan-downloads.sh — Scheduled Virus Scan

Runs ClamAV inside its container to scan completed downloads. Scheduled
daily instead of triggered by qBittorrent — simpler and more reliable.

```bash
nano /mnt/apps/scripts/scan-downloads.sh
```

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
LOG="/mnt/apps/scripts/clamav-scan.log"
echo "[clamav] Starting $(date)" >> "$LOG"
# --exclude-dir skips .zfs snapshot directories
# || true prevents the script from failing when infected files are found
# (ClamAV returns exit code 1 when it finds threats — that is normal)
docker exec clamav clamscan --recursive \
--exclude-dir='(^|/)\.zfs' \
--move=/quarantine --quiet /scandir >> "$LOG" 2>&1 || true
echo "[clamav] Finished $(date)" >> "$LOG"
```

```bash
chmod +x /mnt/apps/scripts/scan-downloads.sh
```

### Script 5: health-check.sh — Container Status Log

Logs which containers are running. This is a read-only helper — it does
NOT auto-restart anything. The reason: auto-restart hides failures. Logs
let you see that something keeps crashing so you can investigate.

```bash
nano /mnt/apps/scripts/health-check.sh
```

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
LOG="/mnt/apps/scripts/health-check.log"
WEBHOOK_URL="${WEBHOOK_URL:-}"
APPS="jellyfin navidrome immich-server immich-db immich-redis immich-machine-learning qbittorrent prowlarr sonarr radarr lidarr bazarr seerr tailscale"
echo "[health] Check $(date)" >> "$LOG"
for app in $APPS; do
if docker ps --format '{{.Names}}' | grep -qx "$app"; then
echo "[health] OK: $app" >> "$LOG"
else
echo "[health] DOWN: $app" >> "$LOG"
if [ -n "$WEBHOOK_URL" ]; then
MSG=$(printf '{"text": "NAS: %s is down"}' "$app")
curl -sf -X POST "$WEBHOOK_URL" -H "Content-Type: application/json" -d "$MSG" || true
fi
fi
done
```

```bash
chmod +x /mnt/apps/scripts/health-check.sh
```

> [!NOTE]
> **Webhook payload format is Discord/Slack-shaped.**
> The `{"text": "..."}` JSON body works for Discord, Slack, and most generic webhook receivers. **It does not work cleanly for ntfy** — the recommended free option in Part 12. ntfy expects a plain-text body and would deliver `{"text": "NAS: jellyfin is down"}` as the literal notification message instead of just "NAS: jellyfin is down". If you use ntfy, either replace the curl line with a plain-text variant (`curl -sf -d "NAS: $app is down" "$WEBHOOK_URL"`) or expect the JSON envelope to show up in your phone notifications. Telegram needs a different shape entirely (the `{"text": ...}` field maps to nothing). Discord, Slack, and Pushover are the receivers that work as-written.

### Schedule All Scripts

Go to System Settings \> Advanced Settings \> Cron Jobs \> Add. Create
one job for each row:

For each job: fill in the Description, paste the Command exactly as
shown, set Run as User to root, and use the Schedule string. Cron
syntax: \*/10 means every 10 minutes, 0 3 \* \* \* means daily at 3:00
AM.

| **Description** | **Command** | **Schedule** | **Time** |
|:---|:---|:---|:---|
| Health check | bash /mnt/apps/scripts/health-check.sh | \*/10 \* \* \* \* | Every 10 min |
| Backup app configs | bash /mnt/apps/scripts/backup-app-config.sh | 0 3 \* \* \* | 3:00 AM daily |
| Cleanup downloads | bash /mnt/apps/scripts/cleanup-downloads.sh | 0 4 \* \* \* | 4:00 AM daily |
| Virus scan | bash /mnt/apps/scripts/scan-downloads.sh | 30 4 \* \* \* | 4:30 AM daily |
| Photo USB backup | bash /mnt/apps/scripts/photo-backup-usb.sh | 0 6 \* \* \* | 6:00 AM daily |

> [!NOTE]
> **Maintenance job schedule rule**
> 
> Do not let these jobs overlap. The schedule above keeps them separated:
> 03:00 — Config backup (before anything else)
> 04:00 — Cleanup downloads (low I/O)
> 04:30 — Virus scan (heavy I/O on downloads folder)
> 06:00 — USB photo backup (avoids overlapping Immich database jobs)
> Schedule Jellyfin and Immich internal scheduled tasks around 04:00 too — put them in the Jellyfin/Immich settings so their database maintenance does not fight the virus scan.

## Part 9 — First-Time App Setup

Set up apps in this order. Each step builds on the previous one. If
something breaks, you know exactly which app caused it.

> [!NOTE]
> **Use the quick reference table (Part 18) for all app URLs**
> 
> When the guide says "open qBittorrent", go to http://[NAS-IP]:8090 in your browser. Replace [NAS-IP] with your actual server IP address, for example 192.168.1.50.

### 9.1 — qBittorrent

What it does: downloads torrent files to /mnt/apps/downloads-incomplete/
(SSD), then moves completed files to /mnt/tank/data/downloads/complete/
(HDD).

> [!IMPORTANT]
> linuxserver/qbittorrent generates a RANDOM password on first start. You must find it before you can log in. Run this in TrueNAS Shell:
> docker logs qbittorrent 2>&1 | grep -i password
> \# Output: "A temporary password is provided for this session: AbCdEf1234"
> \# Copy that password. You will use it to log in now, then change it.

1. Open qBittorrent at http://\[NAS-IP\]:8090. Log in with username
   admin and the temporary password from the step above.

2. Go to Settings (gear icon) \> Web UI \> Authentication. Change the
   password to your QBIT_PASSWORD from config.env. Save.

3. Go to Settings \> Downloads. Set Default Save Path to:
   /data/downloads/complete

4. Enable "Keep incomplete torrents in:" and set to:
   /downloads/incomplete

5. Save. Then go to Tools \> Torrent Categories \> Add and create these
   three categories:

| **Category** | **Save path**                   |
|:-------------|:--------------------------------|
| movies       | /data/downloads/complete/movies |
| tv           | /data/downloads/complete/tv     |
| music        | /data/downloads/complete/music  |

### 9.2 — Prowlarr

What it does: a central search engine that manages all your indexers
(sources for finding movies, TV, and music) and shares them with Sonarr,
Radarr, and Lidarr.

6. Open Prowlarr at http://\[NAS-IP\]:9696. Complete the setup wizard
   and create an admin account.

7. Go to Settings \> General \> Authentication. Set Forms-based
   authentication, enter a username and password, and restart Prowlarr.

8. Add torrent indexers: click Indexers \> Add Indexer. Add these in
   order:

| **Indexer** | **Priority** | **How to add** | **Notes** |
|:---|:---|:---|:---|
| 1337x | 25 | Search "1337x" \> click it \> no credentials needed \> Test (green) \> Save | General fallback for movies and TV. Sits behind Cloudflare — see FlareSolverr note below. |
| YTS | 25 | Search "YTS" \> click \> Test \> Save | Best quality for movies specifically |
| EZTV | 25 | Search "EZTV" \> click \> Test \> Save | Best for TV shows |
| Redacted (optional, **invitation only**) | 10 | Not openly available. If you have an account, search "Redacted" \> enter credentials \> Priority: 10 \> Test \> Save. | Lossless FLAC music |
| Orpheus (optional, **invitation only**) | 10 | Same as Redacted — invite-only, not open signup. If you have access, configure as above. | Lossless FLAC music |
| Real-Debrid | 1 | Add in Part 13 after Real-Debrid is set up | Always tried first — instant streaming |

> [!TIP]
> The priority number controls which source is tried first for every download request. Lower number = tried first. Priority 1 (Real-Debrid) is always tried before priority 25 (torrent sites). You add Real-Debrid in Part 13 — for now, the torrent fallbacks above work fine.

> [!IMPORTANT]
> **Cloudflare-protected indexers need FlareSolverr.**
> 1337x and many other public trackers in 2026 sit behind Cloudflare's bot challenge. Without FlareSolverr — a small companion container that solves the challenge and proxies the request — Prowlarr's `Test` button returns 403 / "Cloudflare challenge" and the indexer never works.
>
> Add FlareSolverr to your `/mnt/apps/scripts/docker-compose.yml`:
>
> ```yaml
>   flaresolverr:
>     image: ghcr.io/flaresolverr/flaresolverr:latest
>     container_name: flaresolverr
>     environment:
>       - LOG_LEVEL=info
>     ports: ["8191:8191"]
>     logging: *default-logging
>     restart: unless-stopped
> ```
>
> Then in Prowlarr: **Settings → Indexers → Add Indexer Proxy → FlareSolverr → Host: `http://flaresolverr:8191`**. Tag the proxy with a name like `cloudflare`, then on each affected indexer (1337x, etc.) add that same tag — Prowlarr routes those indexer requests through FlareSolverr automatically.
>
> If `Test` still fails after this, the indexer is genuinely down (which happens to public trackers regularly).

### 9.3 — Sonarr

What it does: monitors TV shows, automatically downloads new episodes as
they air, and moves them to the media library.

9. Open Sonarr at http://\[NAS-IP\]:8989. Go to Settings \> General \>
   Security \> Authentication Required. Set Forms-based, enter username
   and password. Restart Sonarr.

10. Connect to qBittorrent: Settings \> Download Clients \> + \>
    qBittorrent:

| **Field** | **Value** |
|:---|:---|
| Host | qbittorrent (container name — never use the NAS IP for container-to-container connections) |
| Port | 8090 |
| Username | admin |
| Password | your QBIT_PASSWORD |
| Category | tv |

11. Click Test (should turn green), then Save.

12. Connect to Prowlarr: in Sonarr, go to Settings \> General and copy
    the API Key shown there. Now open Prowlarr \> Settings \> Apps \> +
    \> Sonarr. Paste the Sonarr API key. Click Test, then Save. Prowlarr
    will now push all your indexers to Sonarr automatically.

13. Set root folder: Settings \> Media Management \> Root Folders \> +
    \> type /data/media/tv

14. Set naming format: Settings \> Media Management \> Rename Episodes:
    ON. Find the "Standard Episode Format" field and type:

```bash
{Series Title} - S{season:00}E{episode:00} - {Episode Title}
```

This creates files like: Breaking Bad - S01E01 - Pilot.mkv — essential
for correct subtitle matching.

> [!NOTE]
> **Anime and Daily formats are separate fields.**
> The same Media Management screen has two more naming fields below "Standard Episode Format": "Daily Episode Format" (for shows like talk shows or news that air daily — uses dates instead of season/episode) and "Anime Episode Format" (for anime, which uses absolute episode numbering with `{absolute:000}`). If you watch any anime or daily-air content, fill those in too — otherwise Sonarr falls back to a less-useful default. The Anime format is typically `{Series Title} - S{season:00}E{episode:00} - {absolute:000} - {Episode Title}`.

### 9.4 — Radarr

What it does: same as Sonarr but for movies.

15. Open Radarr at http://\[NAS-IP\]:7878. Same authentication setup as
    Sonarr.

16. Same qBittorrent connection — only change the Category to: movies

17. Same Prowlarr sync — copy Radarr API key from Settings \> General,
    paste into Prowlarr \> Settings \> Apps \> + \> Radarr.

18. Root folder: /data/media/movies

19. Naming format: Settings \> Media Management \> Rename Movies: ON.
    Movie Format field:

```bash
{Movie Title} ({Release Year}) [imdbid-{ImdbId}]
```

The `[imdbid-...]` suffix lets Jellyfin match the file to the correct
TMDB / IMDB entry automatically — important for ambiguous titles
(remakes, multiple movies sharing a name) so you don't end up with
wrong posters or descriptions. Folder Format should match the file
name: `{Movie Title} ({Release Year}) [imdbid-{ImdbId}]`.

### 9.5 — Lidarr

What it does: follows artists and automatically downloads new albums and
discographies.

20. Open Lidarr at http://\[NAS-IP\]:8686. Same authentication setup.

21. Same qBittorrent connection — Category: music

22. Same Prowlarr sync — copy Lidarr API key, add Lidarr in Prowlarr \>
    Settings \> Apps.

23. Root folder: /data/media/music

24. Naming: Settings \> Media Management \> Artist folder: {Artist Name}
    — Album folder: {Album Title} ({Release Year}) — Track format:

```bash
{track:00} - {Track Title}
```
25. Create a quality profile: Settings \> Profiles \> Quality Profiles
    \> +. Name it `Lossless`. Lidarr profiles work in two parts —
    which formats are *enabled* (ticked checkbox) and what *order*
    they sit in (drag to rearrange; higher = more preferred):
    - **Enable**: FLAC, MP3 320, MP3 192. Disable everything else
      (worse-than-MP3-192 rips and lossless-but-uncommon formats
      like ALAC / WMA Lossless that public indexers rarely have).
    - **Order from top to bottom**: FLAC → MP3 320 → MP3 192. Lidarr
      grabs the highest available; if nothing FLAC exists, falls back
      to MP3 320, then MP3 192.
    - **Set the cutoff** (the slider or marker in the same screen)
      to MP3 192 — once a track meets that quality, Lidarr stops
      looking for upgrades. Anything below MP3 192 gets rejected
      because it's not in the enabled list.
    Save. Use this profile whenever you add an artist.

> [!NOTE]
> **🎵 How to add music to your library**
> 
> 1\. Open Lidarr at http://[NAS-IP]:8686
> 2\. Click Artists in the left sidebar
> 3\. Click Add New
> 4\. Search for any artist name, e.g. Radiohead
> 5\. Click the artist from the results
> 6\. Set Quality Profile to Lossless
> 7\. Set Root Folder to /data/media/music
> 8\. Click Add Artist
> 9\. Lidarr finds all albums, qBittorrent downloads them, Navidrome picks them up within minutes
> 10\. New albums by that artist download automatically on release day — you never need to do anything again

> [!TIP]
> **Optional: add Profilarr for quality control across Sonarr/Radarr/Lidarr.**
> The default Sonarr/Radarr/Lidarr quality profiles are minimal — they cover \"FLAC vs MP3\" and \"1080p vs 4K\" but don't filter out malicious file extensions, scam encoders, low-effort upscales, fake HDR, or known-bad release groups. **Profilarr** ([github.com/Dictionarry-Hub/profilarr](https://github.com/Dictionarry-Hub/profilarr)) is a sidecar that syncs community-maintained custom-format definitions and quality profiles directly into your *arr apps — so suspicious or low-quality releases are rejected at the indexer-search stage, before qBittorrent ever touches them. This is also the planned replacement for ClamAV's daily virus scan in Part 8 (filtering at grab time is more effective than scanning after download).
>
> Add to `/mnt/apps/scripts/docker-compose.yml`:
>
> ```yaml
>   profilarr:
>     image: santiagosayshey/profilarr:latest
>     container_name: profilarr
>     env_file: [/mnt/apps/scripts/config.env]
>     volumes: ["/mnt/apps/appdata/profilarr:/config"]
>     ports: ["6868:6868"]
>     logging: *default-logging
>     restart: unless-stopped
> ```
>
> Then `mkdir -p /mnt/apps/appdata/profilarr && chown -R 568:568 /mnt/apps/appdata/profilarr`, redeploy media-stack, and open Profilarr at `http://[NAS-IP]:6868`. The Profilarr UI walks you through importing the community profile bundles and pushing them into Sonarr/Radarr/Lidarr (it needs the API keys from each).

### 9.6 — Bazarr

What it does: automatically downloads subtitles for everything Sonarr
and Radarr manage, in the languages you choose.

26. Open Bazarr at http://\[NAS-IP\]:6767.

27. Connect to Sonarr: Settings \> Sonarr \> Enable. Set Hostname to
    sonarr (container name), Port to 8989. Copy the API key from Sonarr
    \> Settings \> General and paste it here. Click Test — should turn
    green. Save.

28. Connect to Radarr: Settings \> Radarr \> Enable. Hostname: radarr,
    Port: 7878. Paste Radarr API key. Test and Save.

29. Add languages: Settings \> Languages \> + \> add **your primary
    language**. + again \> add **English** (almost everything has
    English subtitles, even when your primary language doesn't —
    a useful fallback). Save. The author originally used Hebrew +
    English; substitute whatever your household needs.

30. Add subtitle provider: Settings \> Providers \> + \>
    OpenSubtitles.com. Register for a free account at opensubtitles.com,
    then enter your username and password here. Save.

> [!NOTE]
> **OpenSubtitles.com free-tier limit and adding more providers.**
> Free OpenSubtitles.com accounts are capped at **20 downloads per day**. For a household with active automation (newly-grabbed episodes + retries for failed matches), that runs out fast. Two ways to soften it:
>
> - **Add more providers.** Bazarr supports a dozen others — Subdivx (Spanish), Subscene (legacy mirror), Addic7ed (English TV), Yavkata (Bulgarian), Napiprojekt (Polish), and more. Multiple providers = better hit rate AND distributed quotas. Add them in the same Settings \> Providers screen.
> - **Pay for VIP** ($3/mo) if OpenSubtitles is your primary provider — lifts the cap to 1000/day.
>
> Bazarr already de-duplicates: if one provider supplies a match, it doesn't ask the others.

### 9.7 — Jellyfin

What it does: streams your local media and Real-Debrid content to any
device. The paths below are container paths — Docker translates them to
your actual hard drive folders automatically.

31. Open Jellyfin at http://\[NAS-IP\]:8096. The first-time setup wizard
    appears. Create an admin account.

32. When the wizard asks for media libraries, add two libraries for now:

    - Type: Movies \> Folder: /media/movies \> Name: Movies

    - Type: Shows \> Folder: /media/tv \> Name: TV Shows

Note: /media/movies and /media/tv are paths INSIDE the container — they
map to /mnt/tank/data/media/movies and /mnt/tank/data/media/tv on your
drives. You will add the Real-Debrid library (/media/realdebrid) in Part
13 after it is working.

33. Finish the wizard.

34. Enable hardware video transcoding — this uses your Intel iGPU
    (Quick Sync) to convert video roughly 10× faster than the CPU
    while using a fraction of the power: Administration Dashboard
    (person icon top-right \> Dashboard) \> Playback \> Hardware
    Acceleration \> select **VAAPI** from the dropdown \> VA-API
    Device: `/dev/dri/renderD128` \> tick all codec checkboxes:
    H264, HEVC, VP8, VP9, AV1, MPEG2 \> tick Enable Tone Mapping \>
    Transcoding temp path: `/transcode` \> Save.

> [!NOTE]
> **Why VAAPI over QSV (QuickSync).**
> Both options end up driving the same Intel iGPU. VAAPI goes through the standard Linux GPU abstraction layer (`libva`) and is the more portable / better-tested path on Linux container deployments. QSV is Intel's direct API and is slightly more efficient on paper, but it has more known failure modes — \"Failed to create a MFX session\" errors, codec-specific quirks, and brittleness on the newer `xe` driver used by Intel Core Ultra (Arrow Lake) chips.
>
> Recommendation: start with VAAPI. It just works on every Intel iGPU from Sandy Bridge through current generations. Only switch to QSV if you have a specific reason (benchmarking, an edge case VAAPI can't handle) and are willing to debug.
>
> If you previously selected QSV and transcoding is failing or producing corrupted output, go back to Administration Dashboard \> Playback \> Hardware Acceleration, switch to VAAPI, and save. The `/dev/dri/renderD128` device path is correct for both; only the dropdown changes.

35. Install the Playback Reporting plugin: Administration Dashboard \>
    Plugins \> Catalog \> search "Playback Reporting" \> Install \>
    restart Jellyfin.

36. Schedule Jellyfin internal tasks for quiet hours: Administration
    Dashboard \> Scheduled Tasks. Find Database Optimization, Library
    Refresh, and Metadata tasks. Set them to run around 4:00 AM.

### 9.8 — Navidrome

What it does: serves your music library. First account you create
becomes the admin.

37. Open Navidrome at http://\[NAS-IP\]:4533. Create your admin account
    (first account = admin automatically).

38. Wait a few minutes for the initial music scan to complete. New
    albums added by Lidarr appear within minutes of being imported.

39. Create separate accounts for family members — Settings \> Users \>
    Add User. Never share the admin account.

### 9.9 — Immich

What it does: backs up your phone photos and videos, with AI-powered
face and object search.

40. Open Immich at http://\[NAS-IP\]:2283. Click Get Started and create
    your admin account.

41. Go to Administration \> Settings \> Storage Template if you want to
    organise photos by date.

42. Schedule Immich background jobs for quiet hours: Administration \>
    Jobs \> Machine Learning, Thumbnail Generation, and Smart Search.
    Set them around 4:30 AM.

### Set up the Immich phone app

43. Install the Immich app on your phone — free on App Store and Google
    Play.

44. Open the app. Server URL: http://\[TAILSCALE-IP\]:2283 (use
    Tailscale IP so it works both at home and away).

45. Log in with your admin username and password.

46. Tap your profile photo in the top-right corner \> Background Backup
    \> turn it ON \> set to WiFi only. Now every new photo backs up
    automatically when you are on WiFi.

47. Create additional Immich accounts for family members from the
    Immich web UI: Administration \> Users \> + New User. They install
    the app and log in with their own accounts.

### 9.10 — Seerr

What it does: a friendly request interface. Family members open Seerr on
their phone, search for any movie or show, tap Request, and it appears
in Jellyfin within minutes.

48. Open Seerr at http://\[NAS-IP\]:5055. The setup wizard appears.

49. Click "Sign In with Jellyfin". Enter your Jellyfin server URL
    (http://\[NAS-IP\]:8096) and your Jellyfin admin credentials. Click
    Connect. Seerr inherits all Jellyfin users.

50. Settings \> Services \> Radarr \> Add. Server: radarr (container
    name), Port: 7878, API Key: copy from Radarr \> Settings \>
    General. Test, then Save.

51. Settings \> Services \> Sonarr \> Add. Server: sonarr, Port: 8989,
    API Key: from Sonarr \> Settings \> General. Test, then Save.

52. Phone bookmark: open Seerr in your phone browser at
    http://\[TAILSCALE-IP\]:5055. iPhone: tap Share \> Add to Home
    Screen. Android: tap the three-dot menu \> Add to Home Screen. It
    works like an app from your home screen.

> [!TIP]
> **Optional: Seerr can notify users when their request is available.**
> Settings \> Notifications has built-in support for Discord, Telegram, Pushover, ntfy, Gotify, Slack, email, and webhooks. The most useful flow for a household: when a user requests a movie and it's later imported into Jellyfin, Seerr fires a "Now available" notification to them on the channel of their choice. Per-user notification preferences are configured under Users \> click the user \> Notifications. Setup mirrors the Part 12 webhook config — same URL formats apply.

### 9.11 — Set Passwords on All Apps

| **App** | **Where to set the password** |
|:---|:---|
| qBittorrent | Settings \> Web UI \> Authentication (done in 9.1) |
| Prowlarr | Settings \> General \> Authentication \> Forms \> Username + Password \> Restart |
| Sonarr | Settings \> General \> Security \> Authentication Required \> Forms \> Restart |
| Radarr | Settings \> General \> Security \> Authentication Required \> Forms \> Restart |
| Lidarr | Settings \> General \> Security \> Authentication Required \> Forms \> Restart |
| Bazarr | Settings \> General \> Security \> Username + Password |
| Jellyfin | Created in wizard. Enable 2FA: profile icon \> Settings \> Security \> Two-Step Verification |
| Navidrome | Created on first launch. Manage: Settings \> Users |
| Immich | Created in setup. Enable 2FA: Account Settings \> Security |
| Seerr | Uses Jellyfin credentials (set in 9.10) |
| TrueNAS | Credentials \> Local Users \> your user \> Edit \> Password |

> [!TIP]
> Use a different strong password for each app. Bitwarden (free at bitwarden.com) is an excellent password manager. A passphrase like "correct-horse-battery-staple" is both strong and memorable — 4+ words, 16+ characters.

## Part 10 — Phone and TV App Setup

All apps use your Tailscale IP when outside home. At home, use the local
NAS IP. Keep Tailscale running in the background on your phone at all
times — almost no battery, connects automatically.

### Movies and TV — Jellyfin

| **Setting** | **Value** |
|:---|:---|
| App | Jellyfin — free on App Store and Google Play |
| Add server (home WiFi) | http://\[NAS-IP\]:8096 — fast local streaming |
| Add server (away / Tailscale) | http://\[TAILSCALE-IP\]:8096 — works everywhere |
| Tip | Add BOTH addresses. The app uses whichever responds first — at home it uses the fast local connection, away it uses Tailscale. |
| Notifications | Tap profile icon \> Notifications \> New Episodes ON |
| Quality | Settings \> Max Bitrate \> Original for best quality on WiFi |

### Jellyfin on TV — The Big Screen

| **TV Platform** | **App name** | **How to set up** |
|:---|:---|:---|
| Apple TV | Jellyfin (official, App Store) | Search Jellyfin in tvOS App Store. Open app \> Add Server \> http://\[NAS-IP\]:8096 or Tailscale IP. |
| Samsung / LG Smart TV | Jellyfin (official) | Search in your TV app store. Must be on home WiFi to use NAS IP. |
| Android TV / Google TV | Jellyfin (official, Google Play) | Search Jellyfin. Same setup as above. Works with Tailscale IP too. |
| Amazon Fire TV | Jellyfin (official, Amazon Appstore) | Search Jellyfin in Amazon Appstore. NAS IP or Tailscale IP. |
| Roku | Jellyfin (Roku Channel Store) | Search Jellyfin in Roku store. Enter NAS IP during setup. |
| Chromecast | Cast from Jellyfin phone app | No TV app needed. Tap the cast icon in the Jellyfin phone app. |
| Any computer | http://\[NAS-IP\]:8096 in browser | No app needed — Jellyfin works in any web browser. |

### Music — Symfonium (Android) and Substreamer (iOS)

Navidrome is your music server. Symfonium and Substreamer are the phone
apps that connect to it — they look and feel like Spotify with album
art, playlists, and offline downloads.

### Android — Symfonium (~€5 one-time)

| **Step** | **What to do** |
|:---|:---|
| 1\. Install | Search Symfonium in Google Play Store and install. |
| 2\. Add server | Open app \> tap + \> Media Provider \> select Navidrome |
| 3\. Server URL | http://\[NAS-IP\]:4533 at home — or — http://\[TAILSCALE-IP\]:4533 everywhere |
| 4\. Login | Your Navidrome username and password |
| Offline music | Tap the download icon on any album to save it for offline listening |
| Playlists | Create playlists in Symfonium — they sync back to Navidrome |

### iOS — Substreamer (free)

| **Step**        | **What to do**                                         |
|:----------------|:-------------------------------------------------------|
| 1\. Install     | Search Substreamer in App Store and install.           |
| 2\. Add server  | Settings \> Add Server                                 |
| 3\. Server type | Select Subsonic — Navidrome is Subsonic-compatible     |
| 4\. Server URL  | http://\[TAILSCALE-IP\]:4533 — works at home and away  |
| 5\. Login       | Your Navidrome username and password. Save.            |
| Offline         | Long-press any album \> Download for offline listening |

### Photos — Immich App

| **Step** | **What to do** |
|:---|:---|
| 1\. Install | Search Immich on App Store or Google Play. Free. |
| 2\. Server URL | http://\[TAILSCALE-IP\]:2283 — works at home and away |
| 3\. Login | Your Immich admin username and password |
| 4\. Enable backup | Tap profile photo \> Background Backup \> ON \> WiFi only |
| Done | Every new photo backs up automatically when on home WiFi |
| Family | Create additional accounts in Immich web UI. Others install the app and log in. |

### Requesting Content — Seerr

| **Step** | **What to do** |
|:---|:---|
| Open | Go to http://\[TAILSCALE-IP\]:5055 in your phone browser |
| Log in | Use your Jellyfin username and password |
| Add to phone | iPhone: tap Share \> Add to Home Screen. Android: tap menu \> Add to Home Screen. Works like an app. |
| Request a movie | Tap Search \> type the name \> tap Request \> done |
| It appears | Usually within 1-2 minutes if Real-Debrid has it (after Part 13). Otherwise after the torrent downloads. |

## Part 11 — How Everything Works Together

### The Movie/TV Pipeline — From Request to Playing

```mermaid
flowchart TD
    A[Open Seerr, request &quot;Dune Part Two&quot;] --> B[Seerr → Radarr]
    B --> C[Radarr → Prowlarr]
    C --> D{Real-Debrid has it?}

    D -- Yes --> E[File appears in /mnt/tank/realdebrid/ instantly<br/>virtual folder via Zurg + rclone]
    E --> F[Jellyfin scans library → streamable RIGHT NOW]
    E --> G[qBittorrent downloads local copy in parallel]
    G --> H[On finish → Radarr moves to /data/media/movies/]
    H --> I[Jellyfin switches to local — uses no RD quota]
    F --> J[Bazarr fetches subtitles automatically]

    D -- No --> K[Prowlarr tries 1337x / YTS at priority 25]
    K --> L[qBittorrent → /apps/downloads-incomplete/ on SSD]
    L --> M[On finish → /tank/data/downloads/complete/movies/]
    M --> N[Radarr hardlinks to /tank/data/media/movies/<br/>same dataset = instant]
    N --> O[Jellyfin detects new file]
    O --> J
```

### The TV Show Pipeline

```mermaid
flowchart TD
    A[Add &quot;Severance&quot; in Sonarr<br/>or request via Seerr] --> B[Sonarr downloads existing episodes<br/>via the movie pipeline]
    B --> C[Sonarr monitors the show forever via RSS]
    C --> D{New episode released?}
    D -- Yes --> E[Sonarr detects within minutes → downloads]
    E --> F[Episode appears in Jellyfin automatically]
    F --> G[Bazarr fetches subtitles for the new episode]
    G --> C
    D -- No --> C
```

### The Music Pipeline

```mermaid
flowchart TD
    A[Add &quot;Radiohead&quot; in Lidarr] --> B[Lidarr searches Prowlarr<br/>Redacted / Orpheus at priority 10]
    B --> C[qBittorrent → /mnt/apps/downloads-incomplete/<br/>SSD, fast random writes]
    C --> D[On finish → /mnt/tank/data/downloads/complete/music/]
    D --> E[Lidarr hardlinks to /mnt/tank/data/media/music/Radiohead/…<br/>same tank/data dataset = instant]
    E --> F[Navidrome detects new albums]
    F --> G[Albums appear in Symfonium / Substreamer on phone]
    G --> H{New album released?}
    H -- Yes --> B
    H -- No --> I[Library grows forever — never auto-deleted]
```

### What Runs Automatically Every Night

| **Time** | **What runs** | **What it does** |
|:---|:---|:---|
| Every 10 min | health-check.sh | Logs container status. Sends webhook alert if something is down. |
| Every 4 hours | TrueNAS snapshot: apps/appdata | ZFS snapshot of all app configs — roll back if an update breaks an app |
| 01:00 daily | TrueNAS snapshot: tank/photos | 30-day retention — photos are precious |
| 01:30 daily | TrueNAS snapshot: tank/data | 14-day retention — media library and downloads |
| 03:00 daily | backup-app-config.sh | Tarballs all app configs from SSD to mirrored HDD. 30-day retention. Protects against SSD failure. |
| 04:00 daily | cleanup-downloads.sh | Removes torrent junk (.nfo, .sfv, sample files). Clears stale incomplete downloads. |
| 04:30 daily | scan-downloads.sh | ClamAV scans completed downloads. Infected files move to quarantine automatically. |
| 06:00 daily | photo-backup-usb.sh | Copies photos to USB drive. Unmounts USB immediately after. Physical copy stays offline. |
| Automatic | ClamAV freshclam | ClamAV updates its own virus database internally — no cron needed |

## Part 12 — Optional Webhook Alerts

If you set WEBHOOK_URL in config.env, the health-check.sh script sends
you a message whenever a container goes down. Works with Discord,
Telegram, Slack, ntfy, and any JSON webhook.

| **Service** | **How to get your webhook URL** |
|:---|:---|
| ntfy.sh (recommended — free) | URL: https://ntfy.sh/YOUR_UNIQUE_TOPIC — install ntfy app on phone and subscribe to the same topic name |
| Discord | Server Settings \> Integrations \> Webhooks \> New Webhook \> Copy URL |
| Telegram | Create a bot via @BotFather, get the token, URL format: https://api.telegram.org/bot{TOKEN}/sendMessage |
| Slack | App directory \> Incoming Webhooks \> Add \> choose channel \> Copy URL |

```bash
# Edit config.env and add your webhook URL:
nano /mnt/apps/scripts/config.env
# Set: WEBHOOK_URL="https://ntfy.sh/my-nas-alerts-abc123"
# Test it (run in TrueNAS Shell):
source /mnt/apps/scripts/config.env
curl -sf "$WEBHOOK_URL" -d "Test alert from NAS"
# You should receive a notification on your phone immediately
```

## Part 13 — Real-Debrid + Zurg + rclone (Phase 2)

> [!IMPORTANT]
> Only do this after Phase 1 is working. Prove that: Jellyfin streams local media, Sonarr/Radarr import a download correctly, and the nightly config backup succeeds. Then add Real-Debrid on top.

### What Is Real-Debrid (Quick Recap)

Real-Debrid costs about €4 per month. Sign up at real-debrid.com. It is
a cloud service that has already downloaded and cached millions of
movies and TV shows on its fast servers. When you request something,
your NAS fetches it from Real-Debrid instantly instead of waiting for a
torrent.

- Your NAS only needs outbound internet access to Real-Debrid — no
  public inbound ports needed

- Get your API key at: real-debrid.com/apitoken (log in, then go to that
  page, copy the long string)

### Step 13.1 — Create the Zurg Config

Zurg is the bridge between your NAS and Real-Debrid. It creates a
virtual folder containing all your Real-Debrid content.

```bash
mkdir -p /mnt/apps/appdata/zurg
nano /mnt/apps/appdata/zurg/config.yml
```

Paste this content. Replace YOUR_REAL_DEBRID_API_KEY with your actual
key from real-debrid.com/apitoken:

```yaml
token: YOUR_REAL_DEBRID_API_KEY
port: 9999
concurrent_workers: 20
check_for_changes_every_secs: 10
retain_folder_name_extension: true

directories:
  movies:
    group_order: 10
    group: media
    filters:
      - regex: ".*"
  shows:
    group_order: 20
    group: media
    filters:
      - regex: ".*"
```

### Step 13.2 — Create the rclone Config

rclone mounts the Zurg virtual folder so Jellyfin can see it as a normal
folder on disk.

```bash
mkdir -p /mnt/apps/appdata/rclone
nano /mnt/apps/appdata/rclone/rclone.conf
```

```ini
[zurg]
type = webdav
url = http://localhost:9999/dav
vendor = other
```

> [!IMPORTANT]
> The URL must be http://localhost:9999/dav — not http://zurg:9999/dav.
> rclone runs as a script on the TrueNAS host, not inside Docker. Host processes cannot resolve Docker container names like "zurg". They can only reach Docker containers through published ports on localhost.
> 
> Zurg publishes port 9999 to the host (ports: ["9999:9999"] in the compose). That is how the host-side rclone finds it: localhost:9999.
> 
> Using http://zurg:9999/dav in rclone.conf causes a "connection refused" or DNS error that is very confusing because the Zurg container IS running correctly — you just cannot reach it by name from outside Docker.

### Step 13.3 — Enable Zurg in the Compose and Create the Mount

Zurg runs as a container (in docker-compose.yml). The rclone mount runs
as a Post Init script so the folder is ready before Docker starts.

1. Open the compose file and remove the \# characters from all lines
   of the zurg service section (uncomment it):

```bash
nano /mnt/apps/scripts/docker-compose.yml
# Find the zurg section and remove all # characters from the start of each line
# Press Ctrl+X > Y > Enter to save
```

2. Create the FUSE config pre-init script (enables the mount to be
   readable by Jellyfin):

```bash
nano /mnt/apps/scripts/enable-fuse-allow-other.sh
```

Paste:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
sed -i 's/#user_allow_other/user_allow_other/' /etc/fuse.conf
```

```bash
chmod +x /mnt/apps/scripts/enable-fuse-allow-other.sh
```
3. Create the rclone mount script:

```bash
nano /mnt/apps/scripts/rclone-mount.sh
```

Paste:

```bash
#!/usr/bin/env bash
set -Eeuo pipefail
sleep 10 # Give TrueNAS networking and FUSE time to settle after boot
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
# Wait for the mount to become ready (up to 60 seconds)
for i in $(seq 1 30); do
if mountpoint -q /mnt/tank/realdebrid; then
touch /mnt/tank/realdebrid/.mount-test
echo "Real-Debrid mounted successfully"
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
> **rclone on TrueNAS host**
> 
> TrueNAS SCALE does not ship with rclone. You need to install it. In TrueNAS Shell:
> \# Check if rclone is available:
> which rclone
> 
> \# If not found, install it:
> curl https://rclone.org/install.sh | bash
> 
> \# Verify:
> rclone version

4. Register both scripts as Post Init in TrueNAS:

- Go to System Settings \> Advanced Settings \> Init/Shutdown Scripts \>
  Add

- First script: Type = Script, path =
  /mnt/apps/scripts/enable-fuse-allow-other.sh, When = Post Init, enable
  it

- Second script: Type = Script, path =
  /mnt/apps/scripts/rclone-mount.sh, When = Post Init, enable it

5. Redeploy media-stack from TrueNAS Apps \> media-stack \>
   Update/Redeploy so Zurg starts.

6. Verify everything is working:

```bash
# Check Zurg is serving your Real-Debrid library:
curl http://localhost:9999
# Should show an HTML page listing your Real-Debrid content
# Check rclone mounted the folder:
ls /mnt/tank/realdebrid
# Should show movie and TV folder names from your Real-Debrid library
```

> [!NOTE]
> **About the .mount-test marker**
> 
> The rclone-mount.sh script creates the file /mnt/tank/realdebrid/.mount-test automatically when it mounts successfully. The Jellyfin wait script and the safety rule checks look for this file.
> The file is created by rclone-mount.sh — not by you manually. It does not exist until the script runs successfully for the first time.
> 
> After TrueNAS boots and the Post Init script runs, verify the marker was created:
> ls /mnt/tank/realdebrid/.mount-test
> \# If the file exists, the mount is healthy and Jellyfin will start.
> 
> \# If the file is missing, the mount failed. Check:
> cat /var/log/syslog | grep rclone
> \# Or run the script manually to see the error:
> bash /mnt/apps/scripts/rclone-mount.sh
> 
> You never need to create .mount-test by hand. Its absence always means the mount script did not finish successfully — investigate that first.

### Step 13.4 — Set WAIT_FOR_RD=1 and Add Jellyfin Library

7. In config.env, change WAIT_FOR_RD to 1:

```bash
nano /mnt/apps/scripts/config.env
# Change: WAIT_FOR_RD="0" → WAIT_FOR_RD="1"
```

8. Create the Jellyfin wait script. The linuxserver Jellyfin image
   automatically runs any script placed in /config/custom-cont-init.d/
   inside the container. Since /config maps to
   /mnt/apps/appdata/jellyfin on your drive, save the script there
   directly — no compose volume needed:

```bash
# Create the folder that linuxserver Jellyfin watches for startup scripts:
mkdir -p /mnt/apps/appdata/jellyfin/custom-cont-init.d
nano /mnt/apps/appdata/jellyfin/custom-cont-init.d/wait-for-rd.sh
```

Paste:

```bash
#!/usr/bin/with-contenv bash
if [ "${WAIT_FOR_RD:-0}" != "1" ]; then exit 0; fi
for i in $(seq 1 60); do
if cd /media/realdebrid && ls .mount-test >/dev/null 2>&1; then
exit 0
fi
sleep 5
done
echo "Real-Debrid mount marker not found. Jellyfin will not start."
exit 1
```

```bash
chmod +x /mnt/apps/appdata/jellyfin/custom-cont-init.d/wait-for-rd.sh
```

> [!NOTE]
> **Why this location works**
> 
> The linuxserver Jellyfin container runs every executable script it finds in /config/custom-cont-init.d/ before Jellyfin starts. Since the compose file maps /mnt/apps/appdata/jellyfin as /config inside the container, placing the script in /mnt/apps/appdata/jellyfin/custom-cont-init.d/ means it runs automatically on every container start — no compose changes needed.
> The old approach (mounting a script via a compose volume) works too, but requires a compose redeploy to update the script. The appdata path approach lets you edit or replace the script at any time without touching the compose file.

9. Redeploy media-stack from TrueNAS Apps (the script will be picked
   up automatically on next start — no compose edit needed).

10. Add the Real-Debrid library to Jellyfin: Administration Dashboard
    \> Libraries \> + \> Type: Movies \> Folder:
    /media/realdebrid/movies. Add another: Type: Shows \> Folder:
    /media/realdebrid/tv.

11. In Prowlarr, add Real-Debrid as an indexer: Indexers \> Add Indexer
    \> search Real-Debrid \> paste your API key from
    real-debrid.com/apitoken \> Priority: 1 \> Test \> Save.

> [!TIP]
> Real-Debrid (priority 1) is now tried before any torrent site for every movie and TV request. If Real-Debrid has the file, it streams instantly. If not, Prowlarr automatically falls back to the torrent indexers at priority 25.

### Real-Debrid Mount Safety Rule

Any future script that touches /mnt/tank/realdebrid must first verify
the mount is alive. The folder path exists even when rclone fails, so
always check for the .mount-test marker:

```bash
if ! mountpoint -q /mnt/tank/realdebrid || ! ls /mnt/tank/realdebrid/.mount-test >/dev/null 2>&1; then
echo "Real-Debrid is not mounted. Stopping."
exit 1
fi
```

## Part 14 — Monthly Update Process

Do not auto-update the whole stack. Manual approval keeps failures
visible and recoverable.

> [!IMPORTANT]
> **Immich reached stable v2.0.0 in September 2025 and now follows semantic versioning.**
> Within the `v2.x.x` line, server and mobile-app versions are compatible across patch and minor releases — a v2.9 phone app talks to a v2.0 server and vice versa, no schema gymnastics required. The historical "Immich update is a one-way trap" warning no longer applies for in-major-line updates.
>
> The thing to still watch for is a **major version bump** (v3.0.0 someday). That would signal database / API changes that aren't backward-compatible. Read the release notes whenever a major number changes — for the current `v2.x.x` line, monthly updates are routine.

### Monthly Checklist

1. Glance at release notes for Immich, Jellyfin, and any *arr apps you
   care about. Within a major version line, look for "Breaking" /
   "Database Migration" headings — usually nothing in 2026 but worth
   the 30-second skim.

2. Run the config backup immediately before updating:

```bash
bash /mnt/apps/scripts/backup-app-config.sh
```

3. Confirm the backup file was created: `ls -lh /mnt/tank/backups/configs/`

4. **Optional — take a manually-named snapshot of `apps/appdata`** if
   you want a snapshot tied clearly to this update event: Storage \>
   `apps/appdata` \> Snapshots \> Add, name it something like
   `pre-update-$(date +%F)`. The automatic 4-hour snapshot from Part 7
   already gives you a recent point-in-time; the manual one is just a
   clearly-labelled marker that survives the 7-day auto-rotation.

5. In TrueNAS: Apps \> Installed \> media-stack. Use the Update or
   Redeploy button to pull new images and restart.

6. Check logs for any app: click on it in the TrueNAS Apps screen.

7. Open Jellyfin, Immich, Sonarr, and Radarr in a browser and confirm
   they work normally.

8. If you took the manual snapshot in step 4, keep it for at least one
   week before considering the update stable.

> [!WARNING]
> Do not update during a scrub job, virus scan, or large import. Pick a quiet hour. Run the backup first, then update.

> [!TIP]
> If anything breaks after the update, jump to **Part 15 — Recovery Scenarios**, specifically the "If an App Update Breaks Something" section. The combination of the snapshot (from step 4 or the auto-snapshots) and the tarball backup (from step 2) means recovery is always possible.

## Part 15 — Recovery Scenarios

### If an HDD Fails

The mirror keeps running on one drive — you do not lose data. Stay calm.

1. Check TrueNAS Alerts — it will show a DEGRADED warning with the
   failed drive's serial number.

2. Power off, replace the failed HDD with a new one of equal or
   greater size.

3. TrueNAS \> Storage \> click the tank pool \> Manage Devices \> find
   the failed disk \> Replace.

4. Select the new drive. TrueNAS starts resilvering (rebuilding the
   mirror). Takes several hours for 8 TB.

5. Wait for resilver to complete. Pool returns to ONLINE status.

### If the Apps SSD Fails

Media and photos on the HDD mirror are completely unaffected.

6. Replace the failed SSD with a new one of equal or greater size. If
   you set up the apps pool as a mirror in Part 3 and only one of the
   two SSDs failed, your stack kept running on the surviving disk —
   just replace the failed one and skip to "Mirrored apps pool" below.

7. TrueNAS \> Storage \> Create Pool \> name it `apps` \> select new
   disk(s). Pick the layout that matches what you set up in Part 3:
   - **Single-SSD pool**: select the new SSD, layout = Stripe.
   - **Mirrored apps pool**: select both new SSDs, layout = Mirror.
     (If only one drive failed and you're recovering an existing
     mirror, this is the **Replace** flow under Manage Devices, same
     as the HDD mirror procedure above — not a fresh Create Pool.)

8. Recreate datasets: `apps/appdata`, `apps/transcode`,
   `apps/downloads-incomplete`, `apps/scripts`, `apps/backups`.

9. Run the mkdir and permission commands from Part 4.

10. Restore the latest config tarball:

```bash
# Find and restore the most recent backup automatically:
LATEST=$(ls -t /mnt/tank/backups/configs/app-config-*.tar.gz | head -1)
echo "Restoring from: $LATEST"
cd / && tar -xzf "$LATEST"
# Restore the Immich database permission exception:
chown -R 999:999 /mnt/apps/appdata/immich-db
```

11. Redeploy media-stack from Apps \> Install via YAML (use the same
    compose file).

12. Test apps one by one.

### If the Boot SSD Fails

The boot SSD only holds the TrueNAS OS. Your tank pool, apps pool, and
all data on them are unaffected — ZFS pools are self-describing and can
be re-attached to a fresh TrueNAS install.

13. Replace the boot SSD with a new one (any 250 GB+ M.2 NVMe).

14. Reinstall TrueNAS Community Edition from a USB stick — same
    procedure as Part 2. Use the same admin username if you can
    (`truenas_admin` is the default), and configure the same network
    settings.

15. After first login, go to **Storage → Import Pool**. Both `tank` and
    `apps` should appear as importable. Import each one — TrueNAS
    re-attaches them with all datasets, snapshots, and contents intact.

16. Restore the maintenance scripts and config.env (they live on the
    apps pool, but the cron jobs are TrueNAS-side and need recreating):
    re-do **Part 8 → Schedule All Scripts** to register the cron jobs.
    Re-do **Part 7 → Create Snapshot Tasks** for the periodic
    snapshots.

17. Re-do **Part 6 → Step 6.3** (Install via YAML) to redeploy
    media-stack. The compose file is already in `/mnt/apps/scripts/`
    on the imported pool.

18. If you used Tailscale, re-do **Part 5** to register the new install.
    The Tailscale state under `/mnt/apps/appdata/tailscale` may carry
    over — try without re-authenticating first; if that doesn't work,
    generate a new auth key from the Tailscale admin.

### If an App Update Breaks Something

19. Stop the affected app from TrueNAS Apps \> media-stack.

20. Check logs by clicking on the container in the TrueNAS Apps screen.

21. If the app config database is corrupted: **stop every container
    that shares the `apps/appdata` dataset first** (in practice, that
    means stop the entire media-stack from Apps \> Installed \>
    media-stack). A snapshot rollback while a container is writing
    to the dataset corrupts open files — Postgres especially.

22. Then go to Storage \> `apps/appdata` \> Snapshots. Find the
    snapshot from before the update. Click Rollback.

23. Start the stack from TrueNAS Apps. Verify the affected app first.

24. Keep the snapshot until confident the app is working.

> [!WARNING]
> Rolling back `apps/appdata` affects ALL app configs, not just one app. Every Sonarr/Radarr/Lidarr import recorded since the snapshot, every Jellyfin watch-progress update, every Immich photo metadata change — all reverted. If only one app is broken, try restoring just its subfolder from the config tarball first (e.g. `tar -xzf <backup> -C / mnt/apps/appdata/sonarr` for just Sonarr).

## Part 16 — Troubleshooting

Always check TrueNAS Apps \> Installed \> media-stack first. Use the
container log buttons there before reaching for Shell commands.

| **Problem** | **First check** |
|:---|:---|
| Jellyfin cannot see media | Container path mapping — paths like /media/movies are inside the container, not on your host. Check the compose volumes section. |
| Immich database error on start | Run: chown -R 999:999 /mnt/apps/appdata/immich-db — then restart immich-db, immich-redis, immich-server, immich-machine-learning |
| qBittorrent cannot log in | Find the random startup password: docker logs qbittorrent 2\>&1 \| grep -i password |
| qBittorrent paths are wrong | Settings \> Downloads: Default Save Path = /data/downloads/complete, Incomplete = /downloads/incomplete, categories as set in 9.1 |
| Sonarr/Radarr cannot reach qBittorrent | Check hostname is "qbittorrent" (container name), not the NAS IP. Check both are on download-net. |
| rclone mount is empty after Part 13 | Check Zurg: curl http://localhost:9999 — should show HTML file listing. If not, Zurg failed to start. If Zurg is fine but rclone is empty, confirm rclone.conf uses url = http://localhost:9999/dav (not http://zurg:9999/dav — container names do not resolve from host scripts). |
| Real-Debrid library not in Jellyfin | Check .mount-test exists: ls /mnt/tank/realdebrid/.mount-test — if missing, rclone mount failed. Then run: bash /mnt/apps/scripts/rclone-mount.sh |
| Permission error starting any app | Re-run permissions from Part 4. Remember immich-db needs chown 999:999. |
| Disk space filling up | Check: du -sh /mnt/tank/data/media/\* — run cleanup: bash /mnt/apps/scripts/cleanup-downloads.sh — clear Jellyfin transcode: rm -rf /mnt/apps/transcode/jellyfin/\* |
| Tailscale remote access fails | Check auth key in config.env. Run: docker logs tailscale. Check tailscale.com/admin shows the NAS as connected. |

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

## Part 17 — Changing Settings

All settings live in config.env. Edit the file, then redeploy the stack
from TrueNAS Apps.

```bash
nano /mnt/apps/scripts/config.env
# Make your changes, then Ctrl+X > Y > Enter to save
# Redeploy from TrueNAS Apps > media-stack > Update/Redeploy
# OR restart a specific container only:
docker restart jellyfin
```

| **What to change** | **Variable in config.env** |
|:---|:---|
| Timezone | TZ="Asia/Jerusalem" — use a valid tz database string |
| qBittorrent password | QBIT_PASSWORD="..." |
| Immich database password | IMMICH_DB_PASS and POSTGRES_PASSWORD and DB_PASSWORD — change all three to the same value |
| USB backup on/off | ENABLE_USB_BACKUP="1" or "0" |
| USB drive UUID | USB_UUID="..." — find with: blkid \| grep -i usb |
| Webhook alerts URL | WEBHOOK_URL="https://..." |
| Tailscale auth key | TS_AUTHKEY="tskey-auth-k..." |
| Cleanup grace period | INCOMPLETE_DAYS="14" — days before stale incomplete downloads are deleted |
| Enable Real-Debrid wait | WAIT_FOR_RD="1" — set this after Part 13 is complete |

## Part 18 — Quick Reference

### All App Addresses

| **App** | **Local URL** | **Tailscale URL** |
|:---|:---|:---|
| TrueNAS | http://truenas.local or http://\[NAS-IP\] | http://\[TAILSCALE-IP\] |
| Jellyfin | http://\[NAS-IP\]:8096 | http://\[TAILSCALE-IP\]:8096 |
| Navidrome | http://\[NAS-IP\]:4533 | http://\[TAILSCALE-IP\]:4533 |
| Immich | http://\[NAS-IP\]:2283 | http://\[TAILSCALE-IP\]:2283 |
| Seerr | http://\[NAS-IP\]:5055 | http://\[TAILSCALE-IP\]:5055 |
| qBittorrent | http://\[NAS-IP\]:8090 | Admin only |
| Prowlarr | http://\[NAS-IP\]:9696 | Admin only |
| Sonarr | http://\[NAS-IP\]:8989 | Admin only |
| Radarr | http://\[NAS-IP\]:7878 | Admin only |
| Lidarr | http://\[NAS-IP\]:8686 | Admin only |
| Bazarr | http://\[NAS-IP\]:6767 | Admin only |
| Zurg status | http://\[NAS-IP\]:9999 | Admin only |

### Key File Paths

| **What**                   | **Path**                             |
|:---------------------------|:-------------------------------------|
| docker-compose.yml         | /mnt/apps/scripts/docker-compose.yml |
| config.env                 | /mnt/apps/scripts/config.env         |
| All app configs            | /mnt/apps/appdata/\[appname\]/       |
| Jellyfin transcode temp    | /mnt/apps/transcode/jellyfin/        |
| Incomplete downloads       | /mnt/apps/downloads-incomplete/      |
| Movies                     | /mnt/tank/data/media/movies/         |
| TV Shows                   | /mnt/tank/data/media/tv/             |
| Music                      | /mnt/tank/data/media/music/          |
| Photos                     | /mnt/tank/photos/library/            |
| Completed downloads        | /mnt/tank/data/downloads/complete/   |
| Quarantine (virus)         | /mnt/tank/data/downloads/quarantine/ |
| App config backups         | /mnt/tank/backups/configs/           |
| Real-Debrid virtual folder | /mnt/tank/realdebrid/                |
| Script logs                | /mnt/apps/scripts/\*.log             |

### Final Build Checklist

| **Step** | **Done?** |
|:---|:---|
| TrueNAS SCALE 24.10+ installed on SSD 1 only | ☐ |
| BIOS: Intel VT, IOMMU, ASPM, C-states all configured | ☐ |
| tank HDD mirror created and confirmed | ☐ |
| apps SSD pool created | ☐ |
| SSD TRIM enabled | ☐ |
| All datasets created (apps/appdata, apps/scripts, apps/transcode, apps/downloads-incomplete, tank/data, tank/photos, tank/realdebrid, tank/backups) | ☐ |
| Download dataset security: exec=off, setuid=off, devices=off | ☐ |
| Folders created with mkdir commands | ☐ |
| Permissions set: 568:568 for app folders, 999:999 for immich-db | ☐ |
| RENDER_GID found with getent group render | ☐ |
| config.env created and all values filled in | ☐ |
| docker-compose.yml created | ☐ |
| Stack deployed via TrueNAS Apps \> Install via YAML | ☐ |
| All containers show Running in TrueNAS Apps | ☐ |
| Tailscale authenticated, IP written down, MagicDNS enabled | ☐ |
| Phone connected to Tailscale | ☐ |
| ZFS snapshot tasks created (4 tasks) | ☐ |
| All 5 maintenance scripts created and scheduled | ☐ |
| qBittorrent: random password found in logs, changed, download paths set, categories created | ☐ |
| Prowlarr: account created, indexers added with correct priorities | ☐ |
| Sonarr: qBittorrent connection, Prowlarr sync, root folder, naming format | ☐ |
| Radarr: same as Sonarr but for movies | ☐ |
| Lidarr: same pattern, Lossless quality profile created | ☐ |
| Bazarr: Sonarr/Radarr connections, Hebrew+English, OpenSubtitles | ☐ |
| Jellyfin: setup wizard, VAAPI enabled, Playback Reporting plugin installed | ☐ |
| Navidrome: admin account created | ☐ |
| Immich: admin account, phone app installed, background backup enabled | ☐ |
| Seerr: Jellyfin login, Radarr/Sonarr connected | ☐ |
| Passwords set on all apps | ☐ |
| Jellyfin app on phone: both local and Tailscale addresses added | ☐ |
| Symfonium or Substreamer installed and connected to Navidrome | ☐ |
| Seerr added to phone home screen | ☐ |
| TEST: download something via torrent, confirm it imports to Jellyfin | ☐ |
| TEST: stream a movie from your phone using Tailscale away from home | ☐ |
| --- Phase 2: Real-Debrid --- |  |
| Zurg config.yml created with Real-Debrid API key | ☐ |
| rclone.conf created (type = webdav, url = http://localhost:9999/dav) | ☐ |
| rclone installed on host (curl https://rclone.org/install.sh \| bash) | ☐ |
| Init scripts registered in TrueNAS (enable-fuse + rclone-mount) | ☐ |
| Zurg service enabled in compose, stack redeployed | ☐ |
| /mnt/tank/realdebrid/ shows Real-Debrid content | ☐ |
| Real-Debrid libraries added to Jellyfin | ☐ |
| Real-Debrid indexer added to Prowlarr (priority 1) | ☐ |
| WAIT_FOR_RD="1" set in config.env, jellyfin wait script added | ☐ |
| TEST: request something in Seerr — streams from Real-Debrid instantly | ☐ |
| No port forwards open on router (verify in router admin page) | ☐ |

> [!NOTE]
> **Guide version**
> 
> TrueNAS SCALE Home Media Server — Final Edition
> Base architecture: v13 (hardlink-optimised tank/data dataset, TrueNAS UI snapshots, rclone mount as Post Init, RENDER_GID, Docker log limits, explicit subnets, Install via YAML)
> Additions: full app setup, phone app guide, pipelines, VAAPI details, indexer priorities, naming formats, Bazarr subtitle providers, qBittorrent startup password, BIOS specifics
> Intel Core Ultra 5 225 · Netanya, Israel