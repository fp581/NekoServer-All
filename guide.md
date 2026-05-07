<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td style="text-align: center;"><p><strong>YOUR COMPLETE</strong></p>
<p><strong>TrueNAS SCALE Home Media Server</strong></p>
<p>Final Edition — Complete Beginner-Friendly Build</p>
<p>ZFS · Real-Debrid · Tailscale · Self-Healing · Remote Access</p>
<p><em>Jellyfin · Navidrome · Immich · Sonarr · Radarr · Lidarr · Bazarr
· Seerr</em></p>
</tr>
</tbody>
</table>

|  |
|:---|
| **🎯 What you will have when you finish this guide** |
| 🎬 Movies and TV — open Seerr on your phone, tap Request. The movie plays in Jellyfin within 2 minutes via Real-Debrid, and downloads locally in the background. |
| 🎵 Music — your own private Spotify. Add an artist once in Lidarr, get every album downloaded and available in Symfonium or Substreamer on your phone. |
| 📷 Photos — your phone photos back up automatically when on WiFi. Search by face, object, or date. Works like Google Photos but on your own drives. |
| 🌍 Remote access — all apps reachable from anywhere via Tailscale. No port forwarding. No domain name. No monthly cost beyond the NAS itself. |
| 🛡️ Self-healing — ZFS snapshots, nightly config backups, virus scanning, health checks. Resilient to drive failure and accidental mistakes. |
| 📱 Plays on phone, Apple TV, Samsung, Android TV, Fire TV, Roku, Chromecast, and any web browser. |
|  |

|  |
|:---|
| **📋 How to use this guide** |
| 1\. Read one part at a time. Do only the steps in that part. |
| 2\. Do not skip ahead unless the guide tells you to. |
| 3\. If a step says "in the TrueNAS Shell" — click the \>\_ icon in the top-right corner of the TrueNAS web page. A black command window opens. |
| 4\. If a step says "in the TrueNAS web page" — that means clicking buttons in your browser. |
| 5\. Every code block is safe to copy and paste in full. Do not retype — paste. |
| 6\. If you feel unsure, stop and re-read the step before pressing anything. Slow is safe. |
|  |

**Part 0 — Understand What You Are Building**

Before touching any hardware, understand the complete picture. This is a
server that sits silently at home, giving you the experience of
Netflix + Spotify + Google Photos — but on your own hardware, under your
own control.

**The Three Storage Layers**

| **Layer** | **Device** | **Pool / Path** | **Purpose** |
|:---|:---|:---|:---|
| Operating system | SSD 1 | TrueNAS boot device | TrueNAS SCALE only — nothing else |
| App data | SSD 2 | apps pool (/mnt/apps/) | App databases, configs, transcode temp, incomplete downloads — fast random I/O |
| Main storage | 2x 8 TB IronWolf | tank mirror (/mnt/tank/) | Media, photos, completed downloads, backups — large and redundant |

|  |
|:---|
| **💡 Tip** |
| Why two SSDs? The apps SSD handles all the small random writes: Immich database, Jellyfin metadata, active torrent pieces. This keeps the HDD mirror doing large sequential reads and writes — what spinning drives do best. The result is snappier apps without wearing out the HDDs. |
|  |

**Every App and What It Does**

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

**What Is Real-Debrid?**

Real-Debrid costs about €4 per month. Sign up at real-debrid.com. Think
of it as a giant media warehouse in the cloud:

- Real-Debrid has already cached millions of movies and TV shows on its
  fast servers

- When you request a movie, Real-Debrid streams it to Jellyfin instantly
  — no waiting for a download to finish

- At the same time, qBittorrent downloads a local copy in the background

- Next time you watch it, Jellyfin streams from your local drive —
  faster, uses no Real-Debrid quota

|  |
|:---|
| **🔴 Important** |
| Real-Debrid is Phase 2 in this guide. Do the entire Phase 1 first: prove that local media works, Jellyfin plays, and downloads import correctly. Then add Real-Debrid on top in Part 13. |
|  |

**How Jellyfin Picks What to Stream — Automatic Priority**

| **Priority** | **Source** | **What happens** |
|:---|:---|:---|
| 1st — Always wins | Your local drive (/mnt/tank/data/media/) | File is on your drives. Fastest. Zero Real-Debrid quota used. |
| 2nd | Real-Debrid virtual folder (/mnt/tank/realdebrid/) | Streams instantly from Real-Debrid. qBittorrent downloads local copy simultaneously. |
| 3rd | Torrent download in progress | File appears in Jellyfin once qBittorrent finishes. |

|  |
|:---|
| **💡 Tip** |
| You never choose manually. Local always wins. Real-Debrid fills in what you do not have locally yet. |
|  |

**The Network Model — What Needs Internet and What Does Not**

| **Traffic type** | **Needed?** | **Example** | **How this guide handles it** |
|:---|:---|:---|:---|
| Outbound internet | Yes | Real-Debrid, indexers, subtitles, metadata, updates | Always allowed — your NAS needs to reach the internet |
| Public inbound internet | No | Random people connecting directly from the web | Blocked — do not open router port forwards |
| Private inbound (remote you) | Yes | Your phone or laptop reaching the NAS when away from home | Tailscale only — encrypted private tunnel |
| Torrent peer inbound | Optional | Better torrent speeds | Not required — Real-Debrid works outbound only |

**Music and Video Are Completely Separate**

|  | **VIDEO — Jellyfin** | **MUSIC — Navidrome** |
|:---|:---|:---|
| Content | Movies, TV shows | Albums, singles, artists |
| Source | Real-Debrid OR local drive | Local drive only — no Real-Debrid |
| Storage | Never deleted automatically — your choice | Never deleted — grows forever |
| Phone app | Jellyfin app (free) | Symfonium (Android) or Substreamer (iOS) |
| Port | 8096 | 4533 |

**Part 1 — Hardware**

| **Component** | **Role in this build** |
|:---|:---|
| Intel Core Ultra 5 225 | CPU + built-in Arc iGPU (hardware video transcoding) + NPU |
| 32 GB DDR5 RAM | TrueNAS, ZFS ARC cache, and all running containers |
| MAXSUN iCraft B860M CROSS PRO | Motherboard |
| SSD 1 (e.g. Corsair T500 1 TB) | TrueNAS OS boot drive — nothing else ever stored here |
| SSD 2 (e.g. Corsair T500 1 TB) | Apps pool — databases, transcode, incomplete downloads |
| 2x Seagate IronWolf 8 TB | tank mirror — media, photos, completed downloads, backups |
| Lian Li SP750 V2 Gold 750 W | Power supply |
| Wired Ethernet cable | Required — never use Wi-Fi for a NAS |

**Cable Connections**

- SSD 1 → M.2 slot M2_1 on the motherboard (OS drive)

- SSD 2 → M.2 slot M2_2 on the motherboard (apps drive)

- IronWolf Drive 1 → SATA port 1

- IronWolf Drive 2 → SATA port 2

- Ethernet cable → motherboard ethernet port → your router or switch

- Keyboard and monitor → connect temporarily for first install only

|  |
|:---|
| **⚠ Warning** |
| Always use a wired Ethernet cable. Wi-Fi causes mysterious transfer failures and timeouts that are very hard to diagnose. |
|  |

**Part 2 — Install TrueNAS SCALE**

This part installs the operating system onto SSD 1. You need a keyboard,
monitor, and a USB drive connected to the NAS for this part only.

**Step 2.1 — Create the USB Installer**

1.  Download balenaEtcher for free from balena.io/etcher on your regular
    computer.

2.  Download the latest TrueNAS SCALE ISO from truenas.com/truenas-scale
    (about 1.5 GB). Make sure you are on TrueNAS SCALE 24.10 or newer
    for best Intel Arc iGPU support.

3.  Open balenaEtcher. Click "Flash from file" and select the ISO file.
    Click "Select target" and choose your USB drive. Click Flash. Wait
    about 5 minutes, then safely eject.

|  |
|:---|
| **⚠ Warning** |
| Do not select your SSD or HDD as the flash target. That would erase your drive. |
|  |

**Step 2.2 — BIOS Setup**

On the MAXSUN iCraft B860M CROSS PRO motherboard, the key to enter BIOS
is Delete. Press it immediately and repeatedly as soon as the screen
lights up after powering on — about once per second. If you miss it,
just power off and try again.

4.  Plug the USB into the NAS and power it on.

5.  Press the Delete key repeatedly as soon as the screen lights up. A
    colourful settings screen appears. This is the BIOS.

6.  Use arrow keys to navigate. Find the Boot tab. Find "Boot Option
    \#1" and change it to your USB drive. The drive appears by its brand
    name.

7.  Find the Advanced tab. Find CPU Configuration. Find "Intel
    Virtualization Technology" and set it to Enabled.

8.  In the same area, find "IOMMU" or "Intel VT-d" and set it to
    Enabled. This lets Docker containers use the Intel graphics chip for
    video transcoding.

9.  For best idle power consumption: look for ASPM (PCIe Active State
    Power Management) and enable it. Look for CPU C-states or Package
    C-state and set to Auto or Enabled. These settings let the CPU and
    PCIe devices sleep properly when idle.

10. Critical for headless use: your NAS will sit in a cupboard with no
    monitor. By default, some motherboards disable the iGPU when no
    screen is connected. Find the setting labelled "Primary Display",
    "Primary Graphics", or "iGPU Multi-Monitor" — it may be in the
    Advanced tab or a Chipset/Graphics sub-menu. Set it to IGFX, iGPU,
    or Internal Graphics. This forces the Intel Arc iGPU to stay active
    even with no monitor plugged in. Without this, /dev/dri will be
    empty and Jellyfin hardware transcoding will silently fail.

11. Press F10 to save and exit. The NAS restarts from the USB.

**Step 2.3 — Install TrueNAS**

The installer is a blue text menu. Use arrow keys to move and Enter to
select.

12. Select "Install/Upgrade" from the menu.

13. The next screen asks which disk to install on. You will see a list.
    Your SSD 1 will be the smallest drive — around 1 TB, NOT the 8 TB
    IronWolf drives. Select it. If you see two similarly-sized SSDs,
    double-check the serial numbers against the stickers on the drives.

14. TrueNAS warns that the disk will be erased. Confirm. This only
    erases SSD 1.

15. It asks you to create an admin account with a username and password.
    Write this down.

16. Installation takes about 10 minutes. When it finishes, select
    Reboot. While rebooting, remove the USB drive so TrueNAS boots from
    SSD 1.

**Step 2.4 — First Login**

On your regular PC (not the NAS), open a web browser.

17. In the address bar, type http://truenas.local and press Enter. If
    that does not work, the NAS shows its IP address on the monitor
    during boot — type http:// followed by that address, for example
    http://192.168.1.50.

18. A login page appears. Enter the username and password you created
    during installation.

19. A short setup wizard appears. It asks about storage and networking.
    Click Next or Skip through everything — do not configure storage
    here. You will do that in Parts 3 and 4.

20. You land on the TrueNAS Dashboard. The NAS is running.

|                                                                    |
|:-------------------------------------------------------------------|
| **💡 Tip**                                                         |
| TrueNAS SCALE is completely free. No license, no trial, no expiry. |
|                                                                    |

**Part 3 — Create Storage Pools**

A pool is a logical storage container that spans one or more physical
drives. You will create two pools: one on the HDDs for media and data,
one on the SSD for app databases and working files.

Before you start: write down the serial number from the sticker on the
back of each IronWolf drive. TrueNAS shows drives by model and serial
number so you can tell them apart.

**Pool 1: tank — HDD Mirror**

| **Setting** | **Value** |
|:---|:---|
| Pool name | tank |
| Disks | Both 8 TB IronWolf HDDs |
| Layout | Mirror — both drives store identical data. One can fail without losing anything. |
| Purpose | Media, photos, completed downloads, backups |

21. In TrueNAS, click Storage in the left sidebar.

22. Click Create Pool in the top right corner.

23. In the Name field, type: tank

24. Under Available Disks, find both 8 TB IronWolf drives. Tick the
    checkbox next to each one.

25. Click Add Vdev, then select Mirror. Both drives move into the mirror
    vdev area. This is correct — a mirror means both drives store the
    same data.

26. Confirm the layout shows Mirror with both drives inside it.

27. Click Create Pool. TrueNAS asks you to confirm by typing a word.
    Type exactly what it asks and confirm. Pool creation takes about 1
    minute.

|  |
|:---|
| **⚠ Warning** |
| Creating a pool erases the selected drives. Make sure both IronWolf drives are empty and you have NOT selected the SSDs. |
|  |

**Pool 2: apps — SSD**

| **Setting** | **Value**                                                    |
|:------------|:-------------------------------------------------------------|
| Pool name   | apps                                                         |
| Disk        | SSD 2 only                                                   |
| Layout      | Single disk — no mirror, but backed up nightly to tank       |
| Purpose     | App databases, transcode temp, incomplete downloads, scripts |

28. Still in Storage, click Create Pool again.

29. Name it: apps

30. Select SSD 2 only. Do not select SSD 1 (TrueNAS is installed there —
    it should not appear in the list at all) and do not select the HDDs.

31. Click Add Vdev, then Stripe. A single-drive pool has no redundancy.
    That is intentional — the apps SSD is backed up to the mirrored tank
    pool every night.

32. Click Create Pool and confirm.

|  |
|:---|
| **ℹ️ Why not use the SSD as ZFS cache?** |
| A ZFS L2ARC cache only helps when the same blocks are read repeatedly and RAM is already exhausted. For this NAS, the real improvement is putting the Immich database, Jellyfin metadata, active torrent writes, and transcode temp files on SSD — workloads that are random, constant, and small. A separate apps pool gives you this AND keeps app data completely separate from media. |
|  |

|  |
|:---|
| **ℹ️ Enable SSD TRIM** |
| After creating the apps pool, go to Storage \> Disks. Find your app SSD and make sure TRIM is enabled. TRIM tells the SSD which blocks are no longer used, keeping write performance healthy over time as databases and log files change constantly. Without TRIM, the apps SSD can slow down noticeably after months of use. |
|  |

**Part 4 — Create Datasets and Folders**

A dataset is like a special folder with extra powers. It can have its
own snapshots, permissions, and security settings. You use datasets
instead of plain folders because TrueNAS can protect and snapshot them
independently.

|  |
|:---|
| **ℹ️ The path rule** |
| If the pool is named apps and you create a dataset called appdata, the full path is /mnt/apps/appdata. If the pool is named tank and you create a dataset called photos, the full path is /mnt/tank/photos. The pool name is always part of the path. |
|  |

How to create one dataset: In TrueNAS, click Storage, click the
three-dot menu (⋮) next to the pool name, click Add Dataset, type the
dataset name, and click Save. Repeat for each dataset below.

**Datasets on the apps pool (SSD)**

| **Dataset path** | **Purpose** |
|:---|:---|
| apps/appdata | All app configs and databases — Jellyfin, Sonarr, Radarr, Immich, and all others |
| apps/scripts | Your maintenance scripts and config.env |
| apps/backups | Temporary local backup workspace |
| apps/transcode | Jellyfin transcoding temp files — heavy I/O belongs on SSD |
| apps/downloads-incomplete | Active qBittorrent incomplete downloads — constant writes belong on SSD |

**Datasets on the tank pool (HDD mirror)**

| **Dataset path** | **Purpose** |
|:---|:---|
| tank/data | Media AND completed downloads in one dataset — critical for hardlinks (see note below) |
| tank/photos | Immich photo library — important personal data belongs on mirrored storage |
| tank/realdebrid | rclone/Zurg virtual mount target (Phase 2 — Part 13) |
| tank/backups | Nightly backups of app configs from the SSD — protects against SSD failure |

|  |
|:---|
| **🔴 Important** |
| tank/data must be ONE dataset. Do not create tank/data/media or tank/data/downloads as separate datasets. |
| Sonarr and Radarr can only hardlink files inside the same ZFS dataset. A hardlink is an instant file move — no copying, no waiting for a 50 GB file to be duplicated. If downloads are in one dataset and media is in another, every import becomes a slow copy-then-delete. |
| By keeping /mnt/tank/data/downloads/ and /mnt/tank/data/media/ inside the single tank/data dataset, imports are instant. |
| Triple-check in TrueNAS: Storage → tank pool. You should see "data" as a dataset. You should NOT see "media" or "downloads" as separate datasets under it. If they exist as datasets, delete them before adding any files. |
|  |

**Dataset Security Settings — Download Folders Only**

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

**Create the Folders**

After datasets exist, create the subfolders. Open TrueNAS Shell (click
the \>\_ icon in the top-right corner of the TrueNAS web page). A black
command window opens.

Paste each block and press Enter:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p># App SSD subfolders</p>
<p>mkdir -p
/mnt/apps/appdata/{bazarr,clamav,immich-db,immich-ml,jellyfin,lidarr,navidrome,prowlarr,qbittorrent,radarr,rclone,seerr,sonarr,tailscale,zurg}</p>
<p>mkdir -p
/mnt/apps/{backups,scripts,transcode/jellyfin,downloads-incomplete}</p>
<p># Tank HDD subfolders — all under tank/data (one dataset, instant
hardlinks)</p>
<p>mkdir -p /mnt/tank/data/media/{movies,tv,music}</p>
<p>mkdir -p /mnt/tank/data/downloads/complete/{movies,tv,music}</p>
<p>mkdir -p /mnt/tank/data/downloads/quarantine</p>
<p># Other tank folders</p>
<p>mkdir -p /mnt/tank/photos/library</p>
<p>mkdir -p /mnt/tank/realdebrid/{movies,tv}</p>
<p>mkdir -p /mnt/tank/backups/configs</p></td>
</tr>
</tbody>
</table>

**Set Permissions**

Apps run as user 568 (the TrueNAS apps user). Give that user ownership
of the folders so apps can read and write their data. Without this step,
apps fail with permission errors.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>chown -R 568:568 /mnt/apps/appdata /mnt/apps/transcode
/mnt/apps/downloads-incomplete</p>
<p>chown -R 568:568 /mnt/tank/data /mnt/tank/photos
/mnt/tank/realdebrid</p>
<p>chmod -R 775 /mnt/apps/appdata /mnt/apps/transcode
/mnt/apps/downloads-incomplete</p>
<p>chmod -R 775 /mnt/tank/data /mnt/tank/photos /mnt/tank/realdebrid</p>
<p># Scripts folder — owned by the current logged-in user</p>
<p>SCRIPT_OWNER="$(id -un)"</p>
<p>chown -R "$SCRIPT_OWNER":568 /mnt/apps/scripts</p>
<p>chmod -R 775 /mnt/apps/scripts</p></td>
</tr>
</tbody>
</table>

|  |
|:---|
| **ℹ️ Immich database exception** |
| The Immich database container runs internally as user 999, not 568. You must give that user ownership of just the Immich database folder: |
| chown -R 999:999 /mnt/apps/appdata/immich-db |
|  |
| Important: run this command again any time you restore from backup, recreate the folder, or do a broad permission repair. A backup restore can accidentally put the database folder back under user 568, causing Immich to fail with permission errors on the next start. |
|  |

|  |
|:---|
| **ℹ️ Find the GPU render group ID** |
| Jellyfin and Immich need to join the GPU render group. Find its number by running this in TrueNAS Shell: |
| getent group render |
| \# Output looks like: render:x:107: |
| \# The number (107 in this example) is your RENDER_GID. |
| \# Write it down — you will put it in config.env in the next part. |
|  |

|  |
|:---|
| **ℹ️ Verify /dev/dri exists before continuing** |
| After TrueNAS is installed and running (no USB, no monitor), confirm the Intel Arc iGPU is actually visible to the OS. In TrueNAS Shell: |
| ls /dev/dri |
| \# You should see: card0 renderD128 |
| \# If the output is empty, try the Arrow Lake fix below first, then check BIOS. |
| \# This is the most common reason Jellyfin hardware transcoding appears to work but does nothing. |
|  |

|  |
|:---|
| **ℹ️ Arrow Lake force_probe — required for Intel Core Ultra (Arrow Lake)** |
| The Intel Core Ultra 5 225 uses the Arrow Lake architecture. In TrueNAS SCALE 24.10, the Linux kernel may not load the i915 graphics driver automatically for this chip. Even with the correct BIOS settings and /dev/dri absent, the fix is a single kernel parameter. |
| Step 1 — In TrueNAS: System Settings \> Advanced \> Sysctl \> Add. |
| Variable: ix_diagnostics_force_probe |
| Value: 7120 |
| (7120 is the PCI device ID for Arrow Lake integrated graphics. This tells the i915 driver to probe this device ID even though it is not yet in the official support list.) |
|  |
| Step 2 — Reboot the NAS. |
|  |
| Step 3 — Verify in TrueNAS Shell: |
| ls /dev/dri |
| \# You should now see: card0 renderD128 |
|  |
| If that specific value does not work on your board, the fallback is the wildcard: |
| Variable: ix_diagnostics_force_probe |
| Value: \* |
| (The wildcard forces the driver to probe all unknown Intel GPU device IDs. It is broader but safe on a dedicated NAS.) |
|  |
| Without this step, /dev/dri may stay empty on Core Ultra hardware regardless of BIOS settings. |
|  |

**Part 5 — Remote Access: Tailscale First**

Install Tailscale before anything else. It gives you a secure private
tunnel from your phone and laptop to the NAS — from anywhere in the
world — without opening any router ports.

|  |
|:---|
| **🔴 Important** |
| Do not open any router port forwards while building this system. Not for TrueNAS, not for Jellyfin, not for any app. Tailscale provides all remote access. This is the single most important security decision in the guide. |
|  |

**Step 5.1 — Create a Tailscale Account and Auth Key**

33. Go to tailscale.com and create a free account.

34. Go to tailscale.com/settings/keys and click "Generate auth key".

35. Set it to Reusable and set expiry to No expiry. A home server should
    not go offline because a key expired while you were away. A
    one-time-use key will work for the first boot but the NAS will
    silently lose its Tailscale identity the next time the container is
    recreated (after a stack update or a TrueNAS upgrade). Reusable +
    non-expiring means the NAS reconnects automatically every time.

36. Copy the key. It looks like: tskey-auth-kXXXXXXXXXXX. Save it
    somewhere safe — you will put it in config.env in Part 6.

|  |
|:---|
| **ℹ️ How Tailscale identity is preserved across restarts** |
| The Tailscale container stores its authenticated state (its identity on your private network) in the volume mounted at /var/lib/tailscale — which maps to /mnt/apps/appdata/tailscale on your SSD. |
| As long as that folder exists and has the correct files, the NAS reconnects automatically after any restart, update, or container recreation without needing a new auth key. |
|  |
| If you ever delete /mnt/apps/appdata/tailscale or recreate the apps pool, the NAS loses its Tailscale identity and needs the auth key again. That is why the key must be Reusable — so you can re-authenticate without generating a new one. |
|  |
| The nightly config backup (Part 8) backs up this folder to the mirrored HDD, so even an apps SSD failure does not permanently lose your Tailscale identity. |
|  |

**Step 5.2 — Connect Your Phone**

37. Install the Tailscale app on your phone — free on App Store and
    Google Play.

38. Sign in with the same Tailscale account.

39. Tap the toggle to connect. Leave Tailscale running in the background
    permanently — it uses almost no battery and connects automatically
    when you leave home.

**Step 5.3 — Enable MagicDNS (Recommended)**

MagicDNS gives your NAS a readable name instead of a number like
100.64.12.34.

40. Go to tailscale.com/admin/dns in your browser.

41. Click Enable MagicDNS. Your NAS will be reachable as truenas-nas
    (the hostname you set in the Tailscale container) instead of its IP
    address.

|  |
|:---|
| **💡 Tip** |
| After setup is complete, your NAS has two addresses: a local IP (e.g. 192.168.1.50) for when you are at home, and a Tailscale IP (e.g. 100.64.12.34 or the MagicDNS name) for when you are away. The apps work the same way at both addresses. |
|  |

**Access Model**

| **App** | **At home (local)** | **Away from home** | **Reasoning** |
|:---|:---|:---|:---|
| Jellyfin, Immich, Navidrome, Seerr | NAS IP | Tailscale IP | Family watches locally, you watch remotely |
| qBittorrent, Sonarr, Radarr, Prowlarr, etc. | NAS IP | Tailscale IP (admin only) | Management tools — no need to be public |
| TrueNAS web page | NAS IP | Tailscale IP only | Never expose the storage OS to the internet |

**Part 6 — The Docker Stack**

The entire app stack is defined in two files: config.env (your personal
settings) and docker-compose.yml (the app blueprint). You create both
files, then deploy through the TrueNAS Apps UI.

**Step 6.1 — Create config.env**

config.env is a plain text file that holds all your settings in one
place. Every script and every container reads from it so you never type
the same value twice.

42. Open TrueNAS Shell (click the \>\_ icon in the top-right corner).

43. Paste this to create the scripts folder and open the config file in
    the nano editor:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>mkdir -p /mnt/apps/scripts</p>
<p>nano /mnt/apps/scripts/config.env</p></td>
</tr>
</tbody>
</table>

44. The nano editor opens. Paste the template below:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>TZ="Asia/Jerusalem"</p>
<p>PUID="568"</p>
<p>PGID="568"</p>
<p>RENDER_GID=""</p>
<p>TS_AUTHKEY=""</p>
<p>QBIT_PASSWORD="ChangeMe_qBit123"</p>
<p>IMMICH_DB_PASS="ChangeMe_DB456"</p>
<p>POSTGRES_USER="immich"</p>
<p>POSTGRES_DB="immich"</p>
<p>POSTGRES_PASSWORD="ChangeMe_DB456"</p>
<p>DB_HOSTNAME="immich-db"</p>
<p>DB_USERNAME="immich"</p>
<p>DB_DATABASE_NAME="immich"</p>
<p>DB_PASSWORD="ChangeMe_DB456"</p>
<p>REDIS_HOSTNAME="immich-redis"</p>
<p>WAIT_FOR_RD="0"</p>
<p>ENABLE_USB_BACKUP="1"</p>
<p>USB_UUID=""</p>
<p>WEBHOOK_URL=""</p>
<p>INCOMPLETE_DAYS="14"</p></td>
</tr>
</tbody>
</table>

45. Now fill in YOUR values by moving the cursor to each line and
    changing the value:

| **Variable** | **What to put here** | **Where to find it** |
|:---|:---|:---|
| TZ | Your timezone, e.g. Asia/Jerusalem | Full list at: en.wikipedia.org/wiki/List_of_tz_database_time_zones |
| RENDER_GID | The GPU render group number | Run: getent group render — use the middle number (e.g. 107) |
| TS_AUTHKEY | Your Tailscale auth key | From tailscale.com/settings/keys — starts with tskey-auth-k... |
| QBIT_PASSWORD | A strong password for qBittorrent | You choose — 12+ characters |
| IMMICH_DB_PASS + POSTGRES_PASSWORD + DB_PASSWORD | One strong password for all three | You choose — keep all three the same value or Immich breaks |
| USB_UUID | Leave blank for now | Fill in Part 9 after plugging in a USB drive |
| WEBHOOK_URL | Leave blank for now | Optional — fill in Part 12 if you want alerts |

46. Press Ctrl+X, then Y, then Enter to save.

|  |
|:---|
| **⚠ Warning** |
| POSTGRES_PASSWORD, POSTGRES_PASSWORD, and DB_PASSWORD must all be the same value. One is read by the database container and the others by the Immich server container. If they differ, Immich will fail to connect to its own database. |
|  |

**Step 6.2 — Create docker-compose.yml**

This file is the blueprint that tells Docker which apps to run, which
folders they can access, which ports they use, and which containers can
talk to each other.

|  |
|:---|
| **🔴 Important** |
| Hardlink rule: qBittorrent, Sonarr, Radarr, and Lidarr all mount /mnt/tank/data on the host as /data inside the container. This is deliberate — completed downloads and final media are in the same ZFS dataset, so imports are instant hardlinks instead of slow file copies. |
|  |

|  |
|:---|
| **ℹ️ Docker subnet note** |
| The compose file uses explicit 172.31.x.x subnets so Docker does not randomly pick a range that overlaps your home LAN or Tailscale addresses. If your home network already uses 172.31.10.x, 172.31.20.x, 172.31.30.x, or 172.31.40.x, change those subnet numbers before deploying. |
| Never use 172.17.0.0/16 as a custom subnet. That is the Docker default bridge network range and it is almost always already in use. Assigning it to a named network creates invisible routing conflicts that are very hard to debug. |
|  |
| If you need to change the 172.31.x.x ranges, pick something in 172.20.x.x through 172.30.x.x that your home router does not use. Most home routers use 192.168.x.x or 10.x.x.x, so the 172.31.x.x range in the guide is safe for almost everyone. |
|  |
| Tailscale uses the 100.64.x.x range (CGNAT space) and does not conflict with 172.31.x.x. |
|  |

47. In TrueNAS Shell, open the compose file in nano:

|                                           |
|-------------------------------------------|
| nano /mnt/apps/scripts/docker-compose.yml |

48. Paste the full compose file below. When done, press Ctrl+X → Y →
    Enter to save.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>version: "3.8"</p>
<p>x-logging: &amp;default-logging</p>
<p>driver: "json-file"</p>
<p>options:</p>
<p>max-size: "10m"</p>
<p>max-file: "3"</p>
<p>networks:</p>
<p>download-net:</p>
<p>driver: bridge</p>
<p>ipam:</p>
<p>config:</p>
<p>- subnet: 172.31.10.0/24</p>
<p>media-net:</p>
<p>driver: bridge</p>
<p>ipam:</p>
<p>config:</p>
<p>- subnet: 172.31.20.0/24</p>
<p>request-net:</p>
<p>driver: bridge</p>
<p>ipam:</p>
<p>config:</p>
<p>- subnet: 172.31.30.0/24</p>
<p>subtitle-net:</p>
<p>driver: bridge</p>
<p>ipam:</p>
<p>config:</p>
<p>- subnet: 172.31.40.0/24</p>
<p>services:</p>
<p># ── TAILSCALE ──────────────────────────────────────────────────</p>
<p>tailscale:</p>
<p>image: tailscale/tailscale:latest</p>
<p>container_name: tailscale</p>
<p>hostname: truenas-nas</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>environment: [TS_STATE_DIR=/var/lib/tailscale]</p>
<p>volumes:</p>
<p>- /mnt/apps/appdata/tailscale:/var/lib/tailscale</p>
<p>- /dev/net/tun:/dev/net/tun</p>
<p>cap_add: [NET_ADMIN, NET_RAW]</p>
<p>network_mode: host</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p># ── REAL-DEBRID (Phase 2 — comment out all lines of both
services</p>
<p># until Part 13. Add # at the start of each line below.) ─────</p>
<p>zurg:</p>
<p>image: ghcr.io/debridmediamanager/zurg-testing:latest</p>
<p>container_name: zurg</p>
<p>restart: unless-stopped</p>
<p># Zurg only needs its config folder and a port to serve WebDAV.</p>
<p># Do NOT mount /mnt/tank/realdebrid here — rclone-mount.sh
manages</p>
<p># that host path. Mounting it from both Zurg and rclone causes a</p>
<p># FUSE "transport endpoint is not connected" error.</p>
<p>volumes:</p>
<p>- /mnt/apps/appdata/zurg:/config</p>
<p>ports: ["9999:9999"]</p>
<p>networks: [download-net]</p>
<p>logging: *default-logging</p>
<p># ── DOWNLOADERS
─────────────────────────────────────────────────</p>
<p>qbittorrent:</p>
<p>image: lscr.io/linuxserver/qbittorrent:latest</p>
<p>container_name: qbittorrent</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>environment: [WEBUI_PORT=8090]</p>
<p>volumes:</p>
<p>- /mnt/apps/appdata/qbittorrent:/config</p>
<p>- /mnt/tank/data:/data</p>
<p>- /mnt/apps/downloads-incomplete:/downloads/incomplete</p>
<p>ports: ["8090:8090"]</p>
<p>networks: [download-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p>clamav:</p>
<p>image: clamav/clamav:latest</p>
<p>container_name: clamav</p>
<p>environment: [CLAMAV_NO_MILTERD=true]</p>
<p>volumes:</p>
<p>- /mnt/apps/appdata/clamav:/var/lib/clamav</p>
<p>- /mnt/tank/data/downloads/complete:/scandir</p>
<p>- /mnt/tank/data/downloads/quarantine:/quarantine</p>
<p>networks: [download-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p># ── INDEXERS / AUTOMATION
───────────────────────────────────────</p>
<p>prowlarr:</p>
<p>image: lscr.io/linuxserver/prowlarr:latest</p>
<p>container_name: prowlarr</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>volumes: ["/mnt/apps/appdata/prowlarr:/config"]</p>
<p>ports: ["9696:9696"]</p>
<p>networks: [download-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p>sonarr:</p>
<p>image: lscr.io/linuxserver/sonarr:latest</p>
<p>container_name: sonarr</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>volumes:</p>
<p>- /mnt/apps/appdata/sonarr:/config</p>
<p>- /mnt/tank/data:/data</p>
<p>ports: ["8989:8989"]</p>
<p>networks: [download-net, media-net, subtitle-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p>radarr:</p>
<p>image: lscr.io/linuxserver/radarr:latest</p>
<p>container_name: radarr</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>volumes:</p>
<p>- /mnt/apps/appdata/radarr:/config</p>
<p>- /mnt/tank/data:/data</p>
<p>ports: ["7878:7878"]</p>
<p>networks: [download-net, media-net, subtitle-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p>lidarr:</p>
<p>image: lscr.io/linuxserver/lidarr:latest</p>
<p>container_name: lidarr</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>volumes:</p>
<p>- /mnt/apps/appdata/lidarr:/config</p>
<p>- /mnt/tank/data:/data</p>
<p>ports: ["8686:8686"]</p>
<p>networks: [download-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p>bazarr:</p>
<p>image: lscr.io/linuxserver/bazarr:latest</p>
<p>container_name: bazarr</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>volumes:</p>
<p>- /mnt/apps/appdata/bazarr:/config</p>
<p>- /mnt/tank/data/media/movies:/movies</p>
<p>- /mnt/tank/data/media/tv:/tv</p>
<p>ports: ["6767:6767"]</p>
<p>networks: [subtitle-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p># ── MEDIA SERVERS
────────────────────────────────────────────────</p>
<p>jellyfin:</p>
<p>image: lscr.io/linuxserver/jellyfin:latest</p>
<p>container_name: jellyfin</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>devices: [/dev/dri:/dev/dri]</p>
<p>group_add: ["${RENDER_GID}"]</p>
<p>volumes:</p>
<p>- /mnt/apps/appdata/jellyfin:/config</p>
<p>- /mnt/apps/transcode/jellyfin:/transcode</p>
<p>- /mnt/tank/data/media/movies:/media/movies:ro</p>
<p>- /mnt/tank/data/media/tv:/media/tv:ro</p>
<p>- /mnt/tank/realdebrid:/media/realdebrid:ro</p>
<p>ports: ["8096:8096"]</p>
<p>networks: [media-net, request-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p>navidrome:</p>
<p>image: deluan/navidrome:latest</p>
<p>container_name: navidrome</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>volumes:</p>
<p>- /mnt/apps/appdata/navidrome:/data</p>
<p>- /mnt/tank/data/media/music:/music:ro</p>
<p>ports: ["4533:4533"]</p>
<p>networks: [media-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p># ── PHOTOS
──────────────────────────────────────────────────────</p>
<p>immich-server:</p>
<p>image: ghcr.io/immich-app/immich-server:release</p>
<p>container_name: immich-server</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>devices: [/dev/dri:/dev/dri]</p>
<p>group_add: ["${RENDER_GID}"]</p>
<p>volumes:</p>
<p>- /mnt/tank/photos/library:/usr/src/app/upload</p>
<p>ports: ["2283:2283"]</p>
<p>depends_on: [immich-db, immich-redis]</p>
<p>networks: [media-net, request-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p>immich-machine-learning:</p>
<p>image: ghcr.io/immich-app/immich-machine-learning:release</p>
<p>container_name: immich-machine-learning</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>devices: [/dev/dri:/dev/dri]</p>
<p>group_add: ["${RENDER_GID}"]</p>
<p>volumes: ["/mnt/apps/appdata/immich-ml:/cache"]</p>
<p>networks: [media-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p>immich-redis:</p>
<p>image: redis:6-alpine</p>
<p>container_name: immich-redis</p>
<p>networks: [media-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p>immich-db:</p>
<p>image:
ghcr.io/immich-app/postgres:16-vectorchord0.4.3-pgvectors0.2.0</p>
<p>container_name: immich-db</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>volumes: ["/mnt/apps/appdata/immich-db:/var/lib/postgresql/data"]</p>
<p>networks: [media-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p>
<p># ── REQUESTS
────────────────────────────────────────────────────</p>
<p>seerr:</p>
<p>image: ghcr.io/seerr-team/seerr:latest</p>
<p>container_name: seerr</p>
<p>env_file: [/mnt/apps/scripts/config.env]</p>
<p>volumes: ["/mnt/apps/appdata/seerr:/app/config"]</p>
<p>ports: ["5055:5055"]</p>
<p>networks: [request-net, media-net]</p>
<p>logging: *default-logging</p>
<p>restart: unless-stopped</p></td>
</tr>
</tbody>
</table>

**Step 6.3 — Deploy via TrueNAS Apps UI**

Do not use "docker compose up -d" in the Shell as the normal way to run
the stack. TrueNAS is an appliance and its Apps system should own app
deployment so it can manage updates and restarts properly.

49. In TrueNAS, click Apps in the left sidebar.

50. Click Discover Apps.

51. Click the three-dot menu (⋮) in the top-right area of the screen.

52. Click "Install via YAML".

53. Give it the name: media-stack

54. In the YAML box, paste the full contents of the docker-compose.yml
    file you just saved. Or if your TrueNAS version supports it, use
    this simpler YAML instead:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>include:</p>
<p>- /mnt/apps/scripts/docker-compose.yml</p></td>
</tr>
</tbody>
</table>

55. Click Save. Wait for TrueNAS to deploy all containers.

56. Go to Apps \> Installed. Confirm media-stack shows all containers as
    Running.

|  |
|:---|
| **ℹ️ If TrueNAS complains about RENDER_GID** |
| Some TrueNAS YAML screens do not apply config.env substitutions at compose-time. If you see an error about \${RENDER_GID}, replace those two instances in the YAML with the actual number (e.g. 107): |
| group_add: \["107"\] |
|  |
| Only replace the two group_add entries — do not change any other variables. |
|  |

|  |
|:---|
| **ℹ️ If TrueNAS blocks a host path (SMB + Apps conflict)** |
| TrueNAS Electric Eel has a safety feature that blocks an app from using a host path that is also shared via SMB. This is common if you share /mnt/tank/data over SMB so you can drag files from your PC. You will see an error like "Host path is already in use" or "Host Path Safety Check" when deploying the stack. |
| Fix: Apps \> Settings \> Advanced Settings \> uncheck "Enable Host Path Safety Checks" \> Save \> redeploy media-stack. |
|  |
| This is a conscious choice, not a random click. The folder permissions set in Part 4 keep the data safe. The safety check exists to prevent accidents — disabling it is fine as long as you understand that both SMB and Docker containers will be touching the same folders. |
|  |
| Why you might have SMB enabled: it is useful for copying large files (movies, music) from your PC directly onto the NAS before asking Sonarr or Radarr to manage them. SMB and Docker containers can share the same data folder safely — TrueNAS is just being cautious by default. |
|  |

|  |
|:---|
| **ℹ️ Zurg at first launch** |
| Zurg will fail to start until you create its config file in Part 13. That is expected. Comment out the zurg service (add \# to the start of every line of that section) until you reach Part 13. Everything else should start correctly. |
|  |

**Verify the stack started correctly**

Wait 2-3 minutes after deployment, then check in TrueNAS Apps \>
Installed \> media-stack. Most containers should show Running. If any
show an error, check the container logs by clicking on it in the TrueNAS
UI.

For quick Shell checks (read-only troubleshooting only):

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>docker ps --format "table {{.Names}}\t{{.Status}}"</p>
<p># Most should show "Up (X minutes)"</p>
<p># Zurg will show Restarting until Part 13 — that is expected</p>
<p># If Jellyfin or other apps fail, check logs:</p>
<p>docker logs jellyfin | tail -30</p></td>
</tr>
</tbody>
</table>

**Part 7 — ZFS Snapshots**

Snapshots are save points. TrueNAS takes a photograph of your data
automatically so you can roll back to any previous state if an update
breaks something, a file gets accidentally deleted, or an import goes
wrong.

|  |
|:---|
| **🔴 Important** |
| Snapshot retention warning: ZFS snapshots hold on to old disk blocks even after files are deleted or moved. |
| If you move a 50 GB movie from downloads to media while a snapshot still remembers the old location, that 50 GB stays on disk until the snapshot expires. |
| Keep tank/data retention at 14 days or less at first. Long retention can make tank look full even after you have deleted files. |
| This is normal ZFS behaviour — just keep retention times reasonable. |
|  |

|  |
|:---|
| **⚠ Warning** |
| Do NOT install sanoid via apt-get or any package manager on TrueNAS SCALE. It is unsupported on the TrueNAS base OS and a system update can break or remove it. Use the built-in UI snapshots below instead. |
|  |

**Create Snapshot Tasks in TrueNAS UI**

Go to Data Protection in the left sidebar \> Periodic Snapshot Tasks \>
Add. Create one task for each row below:

The Add form has these fields: Dataset (type or select the path),
Recursive (Yes = also snapshot sub-datasets), Snapshot Lifetime (how
long to keep each snapshot), Schedule (how often to take a new one).

| **Dataset** | **Recursive** | **Schedule** | **Retention** | **Why** |
|:---|:---|:---|:---|:---|
| apps/appdata | Yes | Every 4 hours | 7 days | App configs change often — roll back quickly if an update breaks an app |
| tank/photos | Yes | Daily at 01:00 | 30 days | Photos are precious — long retention |
| tank/data | Yes | Daily at 01:30 | 14 days | Media and downloads together — keep retention short (see warning above) |
| tank/backups | Yes | Daily at 02:00 | 30 days | Config tarballs — long retention |

**How to Roll Back a Snapshot**

If something goes wrong — bad import, broken update, accidental
deletion:

57. Go to Storage \> find the dataset \> click Snapshots.

58. Find the snapshot from before the problem. Click it.

59. Select Rollback. TrueNAS shows a warning and asks you to confirm by
    typing exactly what it shows in the dialog.

60. The dataset rolls back to that point in time. Restart affected apps
    from Apps \> Installed.

|  |
|:---|
| **⚠ Warning** |
| Rolling back destroys all changes made after that snapshot. Only roll back the specific dataset that has the problem (e.g. apps/appdata), never the entire tank pool unless you truly mean to revert all your media. |
|  |

**Part 8 — Maintenance Scripts**

Four scripts automate predictable, low-risk maintenance tasks. None of
them delete your media library. All use "set -Eeuo pipefail" which means
they fail loudly if something goes wrong instead of silently continuing.

|  |
|:---|
| **ℹ️ How to create these scripts** |
| Each script is created with nano. Open TrueNAS Shell, run the nano command shown, paste the script content, then press Ctrl+X → Y → Enter to save. Then run chmod +x on the file to make it runnable. |
| \# How to make a script runnable: |
| chmod +x /mnt/apps/scripts/script-name.sh |
|  |
| \# How to test a script immediately: |
| bash /mnt/apps/scripts/script-name.sh |
|  |

**Script 1: backup-app-config.sh — Nightly SSD Backup**

This is the most important script. The apps SSD has no redundancy. This
backs it up to the mirrored HDD pool every night. If the SSD dies, you
restore from here.

|                                             |
|---------------------------------------------|
| nano /mnt/apps/scripts/backup-app-config.sh |

Paste this content:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>#!/usr/bin/env bash</p>
<p>set -Eeuo pipefail</p>
<p>SRC="/mnt/apps/appdata"</p>
<p>SCRIPT_SRC="/mnt/apps/scripts"</p>
<p>DEST="/mnt/tank/backups/configs"</p>
<p>DATE="$(date +%F_%H-%M-%S)"</p>
<p>LOG="/mnt/apps/scripts/backup-app-config.log"</p>
<p>mkdir -p "$DEST"</p>
<p>echo "[backup] Starting $DATE" &gt;&gt; "$LOG"</p>
<p>tar -czf "$DEST/app-config-$DATE.tar.gz" "$SRC" "$SCRIPT_SRC"
&gt;&gt; "$LOG" 2&gt;&amp;1</p>
<p>find "$DEST" -name "app-config-*.tar.gz" -type f -mtime +30 -print
-delete &gt;&gt; "$LOG" 2&gt;&amp;1</p>
<p>echo "[backup] Finished $DATE" &gt;&gt; "$LOG"</p></td>
</tr>
</tbody>
</table>

|                                                 |
|-------------------------------------------------|
| chmod +x /mnt/apps/scripts/backup-app-config.sh |

**Script 2: photo-backup-usb.sh — USB Drive Photo Backup**

Creates a physical copy of your photos on a USB drive. The USB is
unmounted when not in use so it cannot be affected by ransomware or NAS
problems.

First, plug in your USB drive and find its UUID:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>blkid</p>
<p># Find the line matching your USB drive (small size, type ext4 or
exfat)</p>
<p># Copy the UUID value — it looks like:
a1b2c3d4-e5f6-7890-abcd-123456789012</p>
<p># Add it to config.env: USB_UUID="your-uuid-here"</p></td>
</tr>
</tbody>
</table>

|                                            |
|--------------------------------------------|
| nano /mnt/apps/scripts/photo-backup-usb.sh |

Paste this content:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>#!/usr/bin/env bash</p>
<p>set -Eeuo pipefail</p>
<p>source /mnt/apps/scripts/config.env</p>
<p>[ "${ENABLE_USB_BACKUP:-0}" != "1" ] &amp;&amp; exit 0</p>
<p>[ -z "${USB_UUID:-}" ] &amp;&amp; exit 0</p>
<p>MOUNT=/mnt/usb-photo-backup</p>
<p>mkdir -p "$MOUNT"</p>
<p>mount UUID="$USB_UUID" "$MOUNT"</p>
<p>if ! mountpoint -q "$MOUNT"; then</p>
<p>echo "[photo-backup] USB drive did not mount. Stopping."</p>
<p>exit 1</p>
<p>fi</p>
<p>trap 'umount "$MOUNT" 2&gt;/dev/null || true' EXIT</p>
<p>rsync -a --ignore-existing --no-perms /mnt/tank/photos/library/
"$MOUNT/photos/"</p>
<p>sync</p></td>
</tr>
</tbody>
</table>

|                                                |
|------------------------------------------------|
| chmod +x /mnt/apps/scripts/photo-backup-usb.sh |

**Script 3: cleanup-downloads.sh — Daily Junk Removal**

Removes torrent junk files from completed downloads and cleans up stale
incomplete downloads. Does NOT touch your media library.

|                                             |
|---------------------------------------------|
| nano /mnt/apps/scripts/cleanup-downloads.sh |

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>#!/usr/bin/env bash</p>
<p>set -Eeuo pipefail</p>
<p>COMPLETE="/mnt/tank/data/downloads/complete"</p>
<p>INCOMPLETE="/mnt/apps/downloads-incomplete"</p>
<p>LOG="/mnt/apps/scripts/cleanup-downloads.log"</p>
<p>INCOMPLETE_DAYS="${INCOMPLETE_DAYS:-14}"</p>
<p>echo "[cleanup] Starting $(date)" &gt;&gt; "$LOG"</p>
<p># Delete torrent junk (NFO, SFV, screenshots, sample clips)</p>
<p># -not -path '*/.*' skips hidden folders including .zfs snapshot
directories</p>
<p>find "$COMPLETE" -not -path '*/.*' -type f \( \</p>
<p>-iname "*.nfo" -o -iname "*.sfv" -o -iname "*.url" -o \</p>
<p>-iname "*.txt" -o -iname "*sample*" -o -iname "*featurette*" \</p>
<p>\) -print -delete &gt;&gt; "$LOG" 2&gt;&amp;1</p>
<p># Delete stale incomplete downloads</p>
<p>find "$INCOMPLETE" -not -path '*/.*' -type f -mtime
+"$INCOMPLETE_DAYS" -print -delete &gt;&gt; "$LOG" 2&gt;&amp;1</p>
<p># Remove empty folders</p>
<p>find "$COMPLETE" "$INCOMPLETE" -not -path '*/.*' -type d -empty
-print -delete &gt;&gt; "$LOG" 2&gt;&amp;1</p>
<p>echo "[cleanup] Finished $(date)" &gt;&gt; "$LOG"</p></td>
</tr>
</tbody>
</table>

|                                                 |
|-------------------------------------------------|
| chmod +x /mnt/apps/scripts/cleanup-downloads.sh |

**Script 4: scan-downloads.sh — Scheduled Virus Scan**

Runs ClamAV inside its container to scan completed downloads. Scheduled
daily instead of triggered by qBittorrent — simpler and more reliable.

|                                          |
|------------------------------------------|
| nano /mnt/apps/scripts/scan-downloads.sh |

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>#!/usr/bin/env bash</p>
<p>set -Eeuo pipefail</p>
<p>LOG="/mnt/apps/scripts/clamav-scan.log"</p>
<p>echo "[clamav] Starting $(date)" &gt;&gt; "$LOG"</p>
<p># --exclude-dir skips .zfs snapshot directories</p>
<p># || true prevents the script from failing when infected files are
found</p>
<p># (ClamAV returns exit code 1 when it finds threats — that is
normal)</p>
<p>docker exec clamav clamscan --recursive \</p>
<p>--exclude-dir='(^|/)\.zfs' \</p>
<p>--move=/quarantine --quiet /scandir &gt;&gt; "$LOG" 2&gt;&amp;1 ||
true</p>
<p>echo "[clamav] Finished $(date)" &gt;&gt; "$LOG"</p></td>
</tr>
</tbody>
</table>

|                                              |
|----------------------------------------------|
| chmod +x /mnt/apps/scripts/scan-downloads.sh |

**Script 5: health-check.sh — Container Status Log**

Logs which containers are running. This is a read-only helper — it does
NOT auto-restart anything. The reason: auto-restart hides failures. Logs
let you see that something keeps crashing so you can investigate.

|                                        |
|----------------------------------------|
| nano /mnt/apps/scripts/health-check.sh |

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>#!/usr/bin/env bash</p>
<p>set -Eeuo pipefail</p>
<p>LOG="/mnt/apps/scripts/health-check.log"</p>
<p>WEBHOOK_URL="${WEBHOOK_URL:-}"</p>
<p>APPS="jellyfin navidrome immich-server immich-db immich-redis
immich-machine-learning qbittorrent prowlarr sonarr radarr lidarr bazarr
seerr clamav tailscale"</p>
<p>echo "[health] Check $(date)" &gt;&gt; "$LOG"</p>
<p>for app in $APPS; do</p>
<p>if docker ps --format '{{.Names}}' | grep -qx "$app"; then</p>
<p>echo "[health] OK: $app" &gt;&gt; "$LOG"</p>
<p>else</p>
<p>echo "[health] DOWN: $app" &gt;&gt; "$LOG"</p>
<p>if [ -n "$WEBHOOK_URL" ]; then</p>
<p>MSG=$(printf '{"text": "NAS: %s is down"}' "$app")</p>
<p>curl -sf -X POST "$WEBHOOK_URL" -H "Content-Type: application/json"
-d "$MSG" || true</p>
<p>fi</p>
<p>fi</p>
<p>done</p></td>
</tr>
</tbody>
</table>

|                                            |
|--------------------------------------------|
| chmod +x /mnt/apps/scripts/health-check.sh |

**Schedule All Scripts**

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

|  |
|:---|
| **ℹ️ Maintenance job schedule rule** |
| Do not let these jobs overlap. The schedule above keeps them separated: |
| 03:00 — Config backup (before anything else) |
| 04:00 — Cleanup downloads (low I/O) |
| 04:30 — Virus scan (heavy I/O on downloads folder) |
| 06:00 — USB photo backup (avoids overlapping Immich database jobs) |
| Schedule Jellyfin and Immich internal scheduled tasks around 04:00 too — put them in the Jellyfin/Immich settings so their database maintenance does not fight the virus scan. |
|  |

**Part 9 — First-Time App Setup**

Set up apps in this order. Each step builds on the previous one. If
something breaks, you know exactly which app caused it.

|  |
|:---|
| **ℹ️ Use the quick reference table (Part 18) for all app URLs** |
| When the guide says "open qBittorrent", go to http://\[NAS-IP\]:8090 in your browser. Replace \[NAS-IP\] with your actual server IP address, for example 192.168.1.50. |
|  |

**9.1 — qBittorrent**

What it does: downloads torrent files to /mnt/apps/downloads-incomplete/
(SSD), then moves completed files to /mnt/tank/data/downloads/complete/
(HDD).

|  |
|:---|
| **🔴 Important** |
| linuxserver/qbittorrent generates a RANDOM password on first start. You must find it before you can log in. Run this in TrueNAS Shell: |
| docker logs qbittorrent 2\>&1 \| grep -i password |
| \# Output: "A temporary password is provided for this session: AbCdEf1234" |
| \# Copy that password. You will use it to log in now, then change it. |
|  |

61. Open qBittorrent at http://\[NAS-IP\]:8090. Log in with username
    admin and the temporary password from the step above.

62. Go to Settings (gear icon) \> Web UI \> Authentication. Change the
    password to your QBIT_PASSWORD from config.env. Save.

63. Go to Settings \> Downloads. Set Default Save Path to:
    /data/downloads/complete

64. Enable "Keep incomplete torrents in:" and set to:
    /downloads/incomplete

65. Save. Then go to Tools \> Torrent Categories \> Add and create these
    three categories:

| **Category** | **Save path**                   |
|:-------------|:--------------------------------|
| movies       | /data/downloads/complete/movies |
| tv           | /data/downloads/complete/tv     |
| music        | /data/downloads/complete/music  |

**9.2 — Prowlarr**

What it does: a central search engine that manages all your indexers
(sources for finding movies, TV, and music) and shares them with Sonarr,
Radarr, and Lidarr.

66. Open Prowlarr at http://\[NAS-IP\]:9696. Complete the setup wizard
    and create an admin account.

67. Go to Settings \> General \> Authentication. Set Forms-based
    authentication, enter a username and password, and restart Prowlarr.

68. Add torrent indexers: click Indexers \> Add Indexer. Add these in
    order:

| **Indexer** | **Priority** | **How to add** | **Notes** |
|:---|:---|:---|:---|
| 1337x | 25 | Search "1337x" \> click it \> no credentials needed \> Test (green) \> Save | General fallback for movies and TV |
| YTS | 25 | Search "YTS" \> click \> Test \> Save | Best quality for movies specifically |
| EZTV | 25 | Search "EZTV" \> click \> Test \> Save | Best for TV shows |
| Redacted (optional) | 10 | Requires a free account at redacted.ch. Search "Redacted" \> enter credentials \> Priority: 10 \> Test \> Save | Lossless FLAC music |
| Orpheus (optional) | 10 | Requires account at orpheus.network. Same process as Redacted. Priority: 10 | Lossless FLAC music |
| Real-Debrid | 1 | Add in Part 13 after Real-Debrid is set up | Always tried first — instant streaming |

|  |
|:---|
| **💡 Tip** |
| The priority number controls which source is tried first for every download request. Lower number = tried first. Priority 1 (Real-Debrid) is always tried before priority 25 (torrent sites). You add Real-Debrid in Part 13 — for now, the torrent fallbacks above work fine. |
|  |

**9.3 — Sonarr**

What it does: monitors TV shows, automatically downloads new episodes as
they air, and moves them to the media library.

69. Open Sonarr at http://\[NAS-IP\]:8989. Go to Settings \> General \>
    Security \> Authentication Required. Set Forms-based, enter username
    and password. Restart Sonarr.

70. Connect to qBittorrent: Settings \> Download Clients \> + \>
    qBittorrent:

| **Field** | **Value** |
|:---|:---|
| Host | qbittorrent (container name — never use the NAS IP for container-to-container connections) |
| Port | 8090 |
| Username | admin |
| Password | your QBIT_PASSWORD |
| Category | tv |

71. Click Test (should turn green), then Save.

72. Connect to Prowlarr: in Sonarr, go to Settings \> General and copy
    the API Key shown there. Now open Prowlarr \> Settings \> Apps \> +
    \> Sonarr. Paste the Sonarr API key. Click Test, then Save. Prowlarr
    will now push all your indexers to Sonarr automatically.

73. Set root folder: Settings \> Media Management \> Root Folders \> +
    \> type /data/media/tv

74. Set naming format: Settings \> Media Management \> Rename Episodes:
    ON. Find the "Episode Format" field and type:

|                                                              |
|--------------------------------------------------------------|
| {Series Title} - S{season:00}E{episode:00} - {Episode Title} |

This creates files like: Breaking Bad - S01E01 - Pilot.mkv — essential
for correct subtitle matching.

**9.4 — Radarr**

What it does: same as Sonarr but for movies.

75. Open Radarr at http://\[NAS-IP\]:7878. Same authentication setup as
    Sonarr.

76. Same qBittorrent connection — only change the Category to: movies

77. Same Prowlarr sync — copy Radarr API key from Settings \> General,
    paste into Prowlarr \> Settings \> Apps \> + \> Radarr.

78. Root folder: /data/media/movies

79. Naming format: Settings \> Media Management \> Rename Movies: ON.
    Movie Format field:

|                                |
|--------------------------------|
| {Movie Title} ({Release Year}) |

**9.5 — Lidarr**

What it does: follows artists and automatically downloads new albums and
discographies.

80. Open Lidarr at http://\[NAS-IP\]:8686. Same authentication setup.

81. Same qBittorrent connection — Category: music

82. Same Prowlarr sync — copy Lidarr API key, add Lidarr in Prowlarr \>
    Settings \> Apps.

83. Root folder: /data/media/music

84. Naming: Settings \> Media Management \> Artist folder: {Artist Name}
    — Album folder: {Album Title} ({Release Year}) — Track format:

|                            |
|----------------------------|
| {track:00} - {Track Title} |

85. Create a quality profile: Settings \> Profiles \> Quality Profiles
    \> +. Name it Lossless. Set FLAC at the top, then MP3 320 kbps. Set
    the cutoff (minimum acceptable quality) to MP3 192 kbps — anything
    worse gets rejected. Save. Use this profile whenever you add an
    artist.

|  |
|:---|
| **🎵 How to add music to your library** |
| 1\. Open Lidarr at http://\[NAS-IP\]:8686 |
| 2\. Click Artists in the left sidebar |
| 3\. Click Add New |
| 4\. Search for any artist name, e.g. Radiohead |
| 5\. Click the artist from the results |
| 6\. Set Quality Profile to Lossless |
| 7\. Set Root Folder to /data/media/music |
| 8\. Click Add Artist |
| 9\. Lidarr finds all albums, qBittorrent downloads them, Navidrome picks them up within minutes |
| 10\. New albums by that artist download automatically on release day — you never need to do anything again |
|  |

**9.6 — Bazarr**

What it does: automatically downloads Hebrew and English subtitles for
everything Sonarr and Radarr manage.

86. Open Bazarr at http://\[NAS-IP\]:6767.

87. Connect to Sonarr: Settings \> Sonarr \> Enable. Set Hostname to
    sonarr (container name), Port to 8989. Copy the API key from Sonarr
    \> Settings \> General and paste it here. Click Test — should turn
    green. Save.

88. Connect to Radarr: Settings \> Radarr \> Enable. Hostname: radarr,
    Port: 7878. Paste Radarr API key. Test and Save.

89. Add languages: Settings \> Languages \> + \> add Hebrew. + again \>
    add English. Save.

90. Add subtitle provider: Settings \> Providers \> + \>
    OpenSubtitles.com. Register for a free account at opensubtitles.com,
    then enter your username and password here. Save.

**9.7 — Jellyfin**

What it does: streams your local media and Real-Debrid content to any
device. The paths below are container paths — Docker translates them to
your actual hard drive folders automatically.

91. Open Jellyfin at http://\[NAS-IP\]:8096. The first-time setup wizard
    appears. Create an admin account.

92. When the wizard asks for media libraries, add two libraries for now:

    - Type: Movies \> Folder: /media/movies \> Name: Movies

    - Type: Shows \> Folder: /media/tv \> Name: TV Shows

Note: /media/movies and /media/tv are paths INSIDE the container — they
map to /mnt/tank/data/media/movies and /mnt/tank/data/media/tv on your
drives. You will add the Real-Debrid library (/media/realdebrid) in Part
13 after it is working.

93. Finish the wizard.

94. Enable hardware video transcoding — this uses your Intel Arc
    graphics chip to convert video 10x faster than the CPU:
    Administration Dashboard (person icon top-right \> Dashboard) \>
    Playback \> Hardware Acceleration \> select VAAPI from the dropdown
    \> VA-API Device: /dev/dri/renderD128 \> tick all codec checkboxes:
    H264, HEVC, VP8, VP9, AV1, MPEG2 \> tick Enable Tone Mapping \>
    Transcoding temp path: /transcode \> Save.

|  |
|:---|
| **ℹ️ VA-API vs QuickSync (QSV) for Intel Core Ultra / Arrow Lake** |
| The guide recommends VA-API, which is the correct choice for Arrow Lake. Here is why: |
| Arrow Lake uses the xe kernel driver, which is newer than the i915 driver that QuickSync (QSV) was built around. |
|  |
| VA-API works through the standard Linux GPU abstraction layer and is fully supported on xe. QuickSync (QSV) on Arrow Lake can be unstable or produce errors like "Failed to create a MFX session". |
|  |
| In short: always choose VA-API for this hardware. If you previously selected QSV and transcoding is failing or producing corrupted output, go back to Administration Dashboard \> Playback \> Hardware Acceleration, switch to VAAPI, and save. |
|  |
| The /dev/dri/renderD128 device path is correct for both. The only thing changing is the acceleration method in the dropdown. |
|  |

95. Install the Playback Reporting plugin: Administration Dashboard \>
    Plugins \> Catalog \> search "Playback Reporting" \> Install \>
    restart Jellyfin.

96. Schedule Jellyfin internal tasks for quiet hours: Administration
    Dashboard \> Scheduled Tasks. Find Database Optimization, Library
    Refresh, and Metadata tasks. Set them to run around 4:00 AM.

**9.8 — Navidrome**

What it does: serves your music library. First account you create
becomes the admin.

97. Open Navidrome at http://\[NAS-IP\]:4533. Create your admin account
    (first account = admin automatically).

98. Wait a few minutes for the initial music scan to complete. New
    albums added by Lidarr appear within minutes of being imported.

99. Create separate accounts for family members — Settings \> Users \>
    Add User. Never share the admin account.

**9.9 — Immich**

What it does: backs up your phone photos and videos, with AI-powered
face and object search.

100. Open Immich at http://\[NAS-IP\]:2283. Click Get Started and create
     your admin account.

101. Go to Administration \> Settings \> Storage Template if you want to
     organise photos by date.

102. Schedule Immich background jobs for quiet hours: Administration \>
     Jobs \> Machine Learning, Thumbnail Generation, and Smart Search.
     Set them around 4:30 AM.

**Set up the Immich phone app**

103. Install the Immich app on your phone — free on App Store and Google
     Play.

104. Open the app. Server URL: http://\[TAILSCALE-IP\]:2283 (use
     Tailscale IP so it works both at home and away).

105. Log in with your admin username and password.

106. Tap your profile photo in the top-right corner \> Background Backup
     \> turn it ON \> set to WiFi only. Now every new photo backs up
     automatically when you are on WiFi.

107. Create additional Immich accounts for family members from the
     Immich web UI: Administration \> Users \> + New User. They install
     the app and log in with their own accounts.

**9.10 — Seerr**

What it does: a friendly request interface. Family members open Seerr on
their phone, search for any movie or show, tap Request, and it appears
in Jellyfin within minutes.

108. Open Seerr at http://\[NAS-IP\]:5055. The setup wizard appears.

109. Click "Sign In with Jellyfin". Enter your Jellyfin server URL
     (http://\[NAS-IP\]:8096) and your Jellyfin admin credentials. Click
     Connect. Seerr inherits all Jellyfin users.

110. Settings \> Services \> Radarr \> Add. Server: radarr (container
     name), Port: 7878, API Key: copy from Radarr \> Settings \>
     General. Test, then Save.

111. Settings \> Services \> Sonarr \> Add. Server: sonarr, Port: 8989,
     API Key: from Sonarr \> Settings \> General. Test, then Save.

112. Phone bookmark: open Seerr in your phone browser at
     http://\[TAILSCALE-IP\]:5055. iPhone: tap Share \> Add to Home
     Screen. Android: tap the three-dot menu \> Add to Home Screen. It
     works like an app from your home screen.

**9.11 — Set Passwords on All Apps**

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

|  |
|:---|
| **💡 Tip** |
| Use a different strong password for each app. Bitwarden (free at bitwarden.com) is an excellent password manager. A passphrase like "correct-horse-battery-staple" is both strong and memorable — 4+ words, 16+ characters. |
|  |

**Part 10 — Phone and TV App Setup**

All apps use your Tailscale IP when outside home. At home, use the local
NAS IP. Keep Tailscale running in the background on your phone at all
times — almost no battery, connects automatically.

**Movies and TV — Jellyfin**

| **Setting** | **Value** |
|:---|:---|
| App | Jellyfin — free on App Store and Google Play |
| Add server (home WiFi) | http://\[NAS-IP\]:8096 — fast local streaming |
| Add server (away / Tailscale) | http://\[TAILSCALE-IP\]:8096 — works everywhere |
| Tip | Add BOTH addresses. The app uses whichever responds first — at home it uses the fast local connection, away it uses Tailscale. |
| Notifications | Tap profile icon \> Notifications \> New Episodes ON |
| Quality | Settings \> Max Bitrate \> Original for best quality on WiFi |

**Jellyfin on TV — The Big Screen**

| **TV Platform** | **App name** | **How to set up** |
|:---|:---|:---|
| Apple TV | Jellyfin (official, App Store) | Search Jellyfin in tvOS App Store. Open app \> Add Server \> http://\[NAS-IP\]:8096 or Tailscale IP. |
| Samsung / LG Smart TV | Jellyfin (official) | Search in your TV app store. Must be on home WiFi to use NAS IP. |
| Android TV / Google TV | Jellyfin (official, Google Play) | Search Jellyfin. Same setup as above. Works with Tailscale IP too. |
| Amazon Fire TV | Jellyfin (official, Amazon Appstore) | Search Jellyfin in Amazon Appstore. NAS IP or Tailscale IP. |
| Roku | Jellyfin (Roku Channel Store) | Search Jellyfin in Roku store. Enter NAS IP during setup. |
| Chromecast | Cast from Jellyfin phone app | No TV app needed. Tap the cast icon in the Jellyfin phone app. |
| Any computer | http://\[NAS-IP\]:8096 in browser | No app needed — Jellyfin works in any web browser. |

**Music — Symfonium (Android) and Substreamer (iOS)**

Navidrome is your music server. Symfonium and Substreamer are the phone
apps that connect to it — they look and feel like Spotify with album
art, playlists, and offline downloads.

**Android — Symfonium (~€5 one-time)**

| **Step** | **What to do** |
|:---|:---|
| 1\. Install | Search Symfonium in Google Play Store and install. |
| 2\. Add server | Open app \> tap + \> Media Provider \> select Navidrome |
| 3\. Server URL | http://\[NAS-IP\]:4533 at home — or — http://\[TAILSCALE-IP\]:4533 everywhere |
| 4\. Login | Your Navidrome username and password |
| Offline music | Tap the download icon on any album to save it for offline listening |
| Playlists | Create playlists in Symfonium — they sync back to Navidrome |

**iOS — Substreamer (free)**

| **Step**        | **What to do**                                         |
|:----------------|:-------------------------------------------------------|
| 1\. Install     | Search Substreamer in App Store and install.           |
| 2\. Add server  | Settings \> Add Server                                 |
| 3\. Server type | Select Subsonic — Navidrome is Subsonic-compatible     |
| 4\. Server URL  | http://\[TAILSCALE-IP\]:4533 — works at home and away  |
| 5\. Login       | Your Navidrome username and password. Save.            |
| Offline         | Long-press any album \> Download for offline listening |

**Photos — Immich App**

| **Step** | **What to do** |
|:---|:---|
| 1\. Install | Search Immich on App Store or Google Play. Free. |
| 2\. Server URL | http://\[TAILSCALE-IP\]:2283 — works at home and away |
| 3\. Login | Your Immich admin username and password |
| 4\. Enable backup | Tap profile photo \> Background Backup \> ON \> WiFi only |
| Done | Every new photo backs up automatically when on home WiFi |
| Family | Create additional accounts in Immich web UI. Others install the app and log in. |

**Requesting Content — Seerr**

| **Step** | **What to do** |
|:---|:---|
| Open | Go to http://\[TAILSCALE-IP\]:5055 in your phone browser |
| Log in | Use your Jellyfin username and password |
| Add to phone | iPhone: tap Share \> Add to Home Screen. Android: tap menu \> Add to Home Screen. Works like an app. |
| Request a movie | Tap Search \> type the name \> tap Request \> done |
| It appears | Usually within 1-2 minutes if Real-Debrid has it (after Part 13). Otherwise after the torrent downloads. |

**Part 11 — How Everything Works Together**

**The Movie/TV Pipeline — From Request to Playing**

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>You open Seerr on your phone and search "Dune Part Two" → tap
Request</p>
<p>↓</p>
<p>Seerr sends the request to Radarr</p>
<p>↓</p>
<p>Radarr asks Prowlarr to find the file</p>
<p>↓</p>
<p>Prowlarr checks Real-Debrid first (priority 1)</p>
<p>│</p>
<p>├─ REAL-DEBRID HAS IT:</p>
<p>│ File appears in /mnt/tank/realdebrid/ immediately (virtual
folder)</p>
<p>│ Jellyfin scans the library and adds it — streamable RIGHT NOW</p>
<p>│ qBittorrent simultaneously downloads a local copy to
downloads/complete/</p>
<p>│ Bazarr downloads Hebrew + English subtitles automatically</p>
<p>│ When local download finishes → Radarr moves it to
/data/media/movies/</p>
<p>│ Jellyfin now streams from local drive (faster, uses no RD
quota)</p>
<p>│</p>
<p>└─ REAL-DEBRID DOES NOT HAVE IT:</p>
<p>Prowlarr tries 1337x / YTS (priority 25) automatically</p>
<p>qBittorrent downloads the torrent to /apps/downloads-incomplete/
(SSD)</p>
<p>When done → moves to /tank/data/downloads/complete/movies/ (HDD)</p>
<p>Radarr hardlinks it instantly to /tank/data/media/movies/ (same
dataset!)</p>
<p>Jellyfin detects the new file</p>
<p>Bazarr adds subtitles</p></td>
</tr>
</tbody>
</table>

**The TV Show Pipeline**

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>You add "Severance" in Sonarr (or request via Seerr)</p>
<p>↓</p>
<p>Sonarr downloads all existing episodes using the movie pipeline
above</p>
<p>↓</p>
<p>Sonarr MONITORS the show forever via RSS feed</p>
<p>New episode released on Apple TV+?</p>
<p>Sonarr detects it within minutes → downloads → appears in Jellyfin
automatically</p>
<p>You do nothing — it just appears</p>
<p>↓</p>
<p>Bazarr finds subtitles for each new episode automatically</p>
<p>↓</p>
<p>Repeat forever for every show you follow</p></td>
</tr>
</tbody>
</table>

**The Music Pipeline**

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>You add "Radiohead" in Lidarr</p>
<p>↓</p>
<p>Lidarr searches Prowlarr (music indexers Redacted/Orpheus at priority
10)</p>
<p>↓</p>
<p>qBittorrent downloads to /mnt/apps/downloads-incomplete/ (SSD — fast
writes)</p>
<p>↓</p>
<p>Download finishes → file moves to
/mnt/tank/data/downloads/complete/music/</p>
<p>↓</p>
<p>Lidarr hardlinks it instantly to
/mnt/tank/data/media/music/Radiohead/...</p>
<p>(hardlink works because downloads and media are in the same tank/data
dataset)</p>
<p>↓</p>
<p>Navidrome detects new albums within minutes</p>
<p>↓</p>
<p>Albums appear in Symfonium or Substreamer on your phone</p>
<p>↓</p>
<p>New album released? Lidarr detects and downloads automatically</p>
<p>↓</p>
<p>Your music library grows forever — never automatically
deleted</p></td>
</tr>
</tbody>
</table>

**What Runs Automatically Every Night**

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

**Part 12 — Optional Webhook Alerts**

If you set WEBHOOK_URL in config.env, the health-check.sh script sends
you a message whenever a container goes down. Works with Discord,
Telegram, Slack, ntfy, and any JSON webhook.

| **Service** | **How to get your webhook URL** |
|:---|:---|
| ntfy.sh (recommended — free) | URL: https://ntfy.sh/YOUR_UNIQUE_TOPIC — install ntfy app on phone and subscribe to the same topic name |
| Discord | Server Settings \> Integrations \> Webhooks \> New Webhook \> Copy URL |
| Telegram | Create a bot via @BotFather, get the token, URL format: https://api.telegram.org/bot{TOKEN}/sendMessage |
| Slack | App directory \> Incoming Webhooks \> Add \> choose channel \> Copy URL |

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p># Edit config.env and add your webhook URL:</p>
<p>nano /mnt/apps/scripts/config.env</p>
<p># Set: WEBHOOK_URL="https://ntfy.sh/my-nas-alerts-abc123"</p>
<p># Test it (run in TrueNAS Shell):</p>
<p>source /mnt/apps/scripts/config.env</p>
<p>curl -sf "$WEBHOOK_URL" -d "Test alert from NAS"</p>
<p># You should receive a notification on your phone
immediately</p></td>
</tr>
</tbody>
</table>

**Part 13 — Real-Debrid + Zurg + rclone (Phase 2)**

|  |
|:---|
| **🔴 Important** |
| Only do this after Phase 1 is working. Prove that: Jellyfin streams local media, Sonarr/Radarr import a download correctly, and the nightly config backup succeeds. Then add Real-Debrid on top. |
|  |

**What Is Real-Debrid (Quick Recap)**

Real-Debrid costs about €4 per month. Sign up at real-debrid.com. It is
a cloud service that has already downloaded and cached millions of
movies and TV shows on its fast servers. When you request something,
your NAS fetches it from Real-Debrid instantly instead of waiting for a
torrent.

- Your NAS only needs outbound internet access to Real-Debrid — no
  public inbound ports needed

- Get your API key at: real-debrid.com/apitoken (log in, then go to that
  page, copy the long string)

**Step 13.1 — Create the Zurg Config**

Zurg is the bridge between your NAS and Real-Debrid. It creates a
virtual folder containing all your Real-Debrid content.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>mkdir -p /mnt/apps/appdata/zurg</p>
<p>nano /mnt/apps/appdata/zurg/config.yml</p></td>
</tr>
</tbody>
</table>

Paste this content. Replace YOUR_REAL_DEBRID_API_KEY with your actual
key from real-debrid.com/apitoken:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>token: YOUR_REAL_DEBRID_API_KEY</p>
<p>port: 9999</p>
<p>concurrent_workers: 20</p>
<p>check_for_changes_every_secs: 10</p>
<p>retain_folder_name_extension: true</p>
<p>directories:</p>
<p>movies:</p>
<p>group_order: 10</p>
<p>group: media</p>
<p>filters:</p>
<p>- regex: ".*"</p>
<p>shows:</p>
<p>group_order: 20</p>
<p>group: media</p>
<p>filters:</p>
<p>- regex: ".*"</p></td>
</tr>
</tbody>
</table>

**Step 13.2 — Create the rclone Config**

rclone mounts the Zurg virtual folder so Jellyfin can see it as a normal
folder on disk.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>mkdir -p /mnt/apps/appdata/rclone</p>
<p>nano /mnt/apps/appdata/rclone/rclone.conf</p></td>
</tr>
</tbody>
</table>

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>[zurg]</p>
<p>type = webdav</p>
<p>url = http://localhost:9999/dav</p>
<p>vendor = other</p></td>
</tr>
</tbody>
</table>

|  |
|:---|
| **🔴 Important** |
| The URL must be http://localhost:9999/dav — not http://zurg:9999/dav. |
| rclone runs as a script on the TrueNAS host, not inside Docker. Host processes cannot resolve Docker container names like "zurg". They can only reach Docker containers through published ports on localhost. |
|  |
| Zurg publishes port 9999 to the host (ports: \["9999:9999"\] in the compose). That is how the host-side rclone finds it: localhost:9999. |
|  |
| Using http://zurg:9999/dav in rclone.conf causes a "connection refused" or DNS error that is very confusing because the Zurg container IS running correctly — you just cannot reach it by name from outside Docker. |
|  |

**Step 13.3 — Enable Zurg in the Compose and Create the Mount**

Zurg runs as a container (in docker-compose.yml). The rclone mount runs
as a Post Init script so the folder is ready before Docker starts.

113. Open the compose file and remove the \# characters from all lines
     of the zurg service section (uncomment it):

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>nano /mnt/apps/scripts/docker-compose.yml</p>
<p># Find the zurg section and remove all # characters from the start of
each line</p>
<p># Press Ctrl+X &gt; Y &gt; Enter to save</p></td>
</tr>
</tbody>
</table>

114. Create the FUSE config pre-init script (enables the mount to be
     readable by Jellyfin):

|                                                   |
|---------------------------------------------------|
| nano /mnt/apps/scripts/enable-fuse-allow-other.sh |

Paste:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>#!/usr/bin/env bash</p>
<p>set -Eeuo pipefail</p>
<p>sed -i 's/#user_allow_other/user_allow_other/'
/etc/fuse.conf</p></td>
</tr>
</tbody>
</table>

|                                                       |
|-------------------------------------------------------|
| chmod +x /mnt/apps/scripts/enable-fuse-allow-other.sh |

115. Create the rclone mount script:

|                                        |
|----------------------------------------|
| nano /mnt/apps/scripts/rclone-mount.sh |

Paste:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>#!/usr/bin/env bash</p>
<p>set -Eeuo pipefail</p>
<p>sleep 10 # Give TrueNAS networking and FUSE time to settle after
boot</p>
<p>mkdir -p /mnt/tank/realdebrid</p>
<p>source /mnt/apps/scripts/config.env</p>
<p>rclone mount zurg: /mnt/tank/realdebrid \</p>
<p>--config /mnt/apps/appdata/rclone/rclone.conf \</p>
<p>--allow-other \</p>
<p>--uid "${PUID:-568}" \</p>
<p>--gid "${PGID:-568}" \</p>
<p>--attr-timeout 10s \</p>
<p>--dir-cache-time 24h \</p>
<p>--daemon</p>
<p># Wait for the mount to become ready (up to 60 seconds)</p>
<p>for i in $(seq 1 30); do</p>
<p>if mountpoint -q /mnt/tank/realdebrid; then</p>
<p>touch /mnt/tank/realdebrid/.mount-test</p>
<p>echo "Real-Debrid mounted successfully"</p>
<p>exit 0</p>
<p>fi</p>
<p>sleep 2</p>
<p>done</p>
<p>echo "Real-Debrid mount did not become ready in time"</p>
<p>exit 1</p></td>
</tr>
</tbody>
</table>

|                                            |
|--------------------------------------------|
| chmod +x /mnt/apps/scripts/rclone-mount.sh |

|  |
|:---|
| **ℹ️ rclone on TrueNAS host** |
| TrueNAS SCALE does not ship with rclone. You need to install it. In TrueNAS Shell: |
| \# Check if rclone is available: |
| which rclone |
|  |
| \# If not found, install it: |
| curl https://rclone.org/install.sh \| bash |
|  |
| \# Verify: |
| rclone version |
|  |

116. Register both scripts as Post Init in TrueNAS:

- Go to System Settings \> Advanced Settings \> Init/Shutdown Scripts \>
  Add

- First script: Type = Script, path =
  /mnt/apps/scripts/enable-fuse-allow-other.sh, When = Post Init, enable
  it

- Second script: Type = Script, path =
  /mnt/apps/scripts/rclone-mount.sh, When = Post Init, enable it

117. Redeploy media-stack from TrueNAS Apps \> media-stack \>
     Update/Redeploy so Zurg starts.

118. Verify everything is working:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p># Check Zurg is serving your Real-Debrid library:</p>
<p>curl http://localhost:9999</p>
<p># Should show an HTML page listing your Real-Debrid content</p>
<p># Check rclone mounted the folder:</p>
<p>ls /mnt/tank/realdebrid</p>
<p># Should show movie and TV folder names from your Real-Debrid
library</p></td>
</tr>
</tbody>
</table>

|  |
|:---|
| **ℹ️ About the .mount-test marker** |
| The rclone-mount.sh script creates the file /mnt/tank/realdebrid/.mount-test automatically when it mounts successfully. The Jellyfin wait script and the safety rule checks look for this file. |
| The file is created by rclone-mount.sh — not by you manually. It does not exist until the script runs successfully for the first time. |
|  |
| After TrueNAS boots and the Post Init script runs, verify the marker was created: |
| ls /mnt/tank/realdebrid/.mount-test |
| \# If the file exists, the mount is healthy and Jellyfin will start. |
|  |
| \# If the file is missing, the mount failed. Check: |
| cat /var/log/syslog \| grep rclone |
| \# Or run the script manually to see the error: |
| bash /mnt/apps/scripts/rclone-mount.sh |
|  |
| You never need to create .mount-test by hand. Its absence always means the mount script did not finish successfully — investigate that first. |
|  |

**Step 13.4 — Set WAIT_FOR_RD=1 and Add Jellyfin Library**

119. In config.env, change WAIT_FOR_RD to 1:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>nano /mnt/apps/scripts/config.env</p>
<p># Change: WAIT_FOR_RD="0" → WAIT_FOR_RD="1"</p></td>
</tr>
</tbody>
</table>

120. Create the Jellyfin wait script. The linuxserver Jellyfin image
     automatically runs any script placed in /config/custom-cont-init.d/
     inside the container. Since /config maps to
     /mnt/apps/appdata/jellyfin on your drive, save the script there
     directly — no compose volume needed:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p># Create the folder that linuxserver Jellyfin watches for startup
scripts:</p>
<p>mkdir -p /mnt/apps/appdata/jellyfin/custom-cont-init.d</p>
<p>nano
/mnt/apps/appdata/jellyfin/custom-cont-init.d/wait-for-rd.sh</p></td>
</tr>
</tbody>
</table>

Paste:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>#!/usr/bin/with-contenv bash</p>
<p>if [ "${WAIT_FOR_RD:-0}" != "1" ]; then exit 0; fi</p>
<p>for i in $(seq 1 60); do</p>
<p>if cd /media/realdebrid &amp;&amp; ls .mount-test &gt;/dev/null
2&gt;&amp;1; then</p>
<p>exit 0</p>
<p>fi</p>
<p>sleep 5</p>
<p>done</p>
<p>echo "Real-Debrid mount marker not found. Jellyfin will not
start."</p>
<p>exit 1</p></td>
</tr>
</tbody>
</table>

|                                                                       |
|-----------------------------------------------------------------------|
| chmod +x /mnt/apps/appdata/jellyfin/custom-cont-init.d/wait-for-rd.sh |

|  |
|:---|
| **ℹ️ Why this location works** |
| The linuxserver Jellyfin container runs every executable script it finds in /config/custom-cont-init.d/ before Jellyfin starts. Since the compose file maps /mnt/apps/appdata/jellyfin as /config inside the container, placing the script in /mnt/apps/appdata/jellyfin/custom-cont-init.d/ means it runs automatically on every container start — no compose changes needed. |
| The old approach (mounting a script via a compose volume) works too, but requires a compose redeploy to update the script. The appdata path approach lets you edit or replace the script at any time without touching the compose file. |
|  |

121. Redeploy media-stack from TrueNAS Apps (the script will be picked
     up automatically on next start — no compose edit needed).

122. Add the Real-Debrid library to Jellyfin: Administration Dashboard
     \> Libraries \> + \> Type: Movies \> Folder:
     /media/realdebrid/movies. Add another: Type: Shows \> Folder:
     /media/realdebrid/tv.

123. In Prowlarr, add Real-Debrid as an indexer: Indexers \> Add Indexer
     \> search Real-Debrid \> paste your API key from
     real-debrid.com/apitoken \> Priority: 1 \> Test \> Save.

|  |
|:---|
| **💡 Tip** |
| Real-Debrid (priority 1) is now tried before any torrent site for every movie and TV request. If Real-Debrid has the file, it streams instantly. If not, Prowlarr automatically falls back to the torrent indexers at priority 25. |
|  |

**Real-Debrid Mount Safety Rule**

Any future script that touches /mnt/tank/realdebrid must first verify
the mount is alive. The folder path exists even when rclone fails, so
always check for the .mount-test marker:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>if ! mountpoint -q /mnt/tank/realdebrid || ! ls
/mnt/tank/realdebrid/.mount-test &gt;/dev/null 2&gt;&amp;1; then</p>
<p>echo "Real-Debrid is not mounted. Stopping."</p>
<p>exit 1</p>
<p>fi</p></td>
</tr>
</tbody>
</table>

**Part 14 — Monthly Update Process**

Do not auto-update the whole stack. Apps like Immich and its database
can have breaking changes. Manual approval is safer and keeps failures
visible and recoverable.

**Monthly Checklist**

124. Read release notes for Immich and any other app you care about
     (check their GitHub pages).

125. Run the config backup immediately before updating:

|                                             |
|---------------------------------------------|
| bash /mnt/apps/scripts/backup-app-config.sh |

126. Confirm the backup file was created: ls -lh
     /mnt/tank/backups/configs/

127. Take a manual snapshot of apps/appdata: Storage \> apps/appdata \>
     Snapshots \> Add.

128. In TrueNAS: Apps \> Installed \> media-stack. Use the Update or
     Redeploy button to pull new images and restart.

129. Check logs for any app: click on it in the TrueNAS Apps screen.

130. Open Jellyfin, Immich, Sonarr, and Radarr in a browser and confirm
     they work normally.

131. Keep the snapshot for at least one week before considering the
     update stable.

|  |
|:---|
| **⚠ Warning** |
| Do not update during a scrub job, virus scan, or large import. Pick a quiet hour. Run the backup first, then update. |
|  |

**Part 15 — Recovery Scenarios**

**If an HDD Fails**

The mirror keeps running on one drive — you do not lose data. Stay calm.

132. Check TrueNAS Alerts — it will show a DEGRADED warning with the
     failed drive's serial number.

133. Power off, replace the failed HDD with a new one of equal or
     greater size.

134. TrueNAS \> Storage \> click the tank pool \> Manage Devices \> find
     the failed disk \> Replace.

135. Select the new drive. TrueNAS starts resilvering (rebuilding the
     mirror). Takes several hours for 8 TB.

136. Wait for resilver to complete. Pool returns to ONLINE status.

**If the Apps SSD Fails**

Media and photos on the HDD mirror are completely unaffected.

137. Replace SSD 2 with a new one of equal or greater size.

138. TrueNAS \> Storage \> Create Pool \> name it apps \> select new SSD
     \> Stripe.

139. Recreate datasets: apps/appdata, apps/transcode,
     apps/downloads-incomplete, apps/scripts, apps/backups.

140. Run the mkdir and permission commands from Part 4.

141. Restore the latest config tarball:

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p># Find the latest backup:</p>
<p>ls -lh /mnt/tank/backups/configs/</p>
<p># Restore it (replace FILENAME with the actual file):</p>
<p>cd / &amp;&amp; tar -xzf
/mnt/tank/backups/configs/app-config-YYYY-MM-DD_HH-MM-SS.tar.gz</p>
<p># Restore the Immich database permission exception:</p>
<p>chown -R 999:999 /mnt/apps/appdata/immich-db</p></td>
</tr>
</tbody>
</table>

142. Redeploy media-stack from Apps \> Install via YAML (use the same
     compose file).

143. Test apps one by one.

**If an App Update Breaks Something**

144. Stop the affected app from TrueNAS Apps \> media-stack.

145. Check logs by clicking on the container in the TrueNAS Apps screen.

146. If the app config database is corrupted: Storage \> apps/appdata \>
     Snapshots. Find the snapshot from before the update. Click
     Rollback.

147. Restart the app from TrueNAS Apps.

148. Keep the snapshot until confident the app is working.

|  |
|:---|
| **⚠ Warning** |
| Rolling back apps/appdata affects ALL app configs, not just one app. If only one app is broken, try restoring just its subfolder from the config tarball first. |
|  |

**Part 16 — Troubleshooting**

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

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p># Useful read-only Shell checks:</p>
<p>docker ps --format "table {{.Names}}\t{{.Status}}"</p>
<p>docker logs jellyfin | tail -50</p>
<p>docker logs immich-server | tail -50</p>
<p>docker logs qbittorrent | tail -50</p>
<p># Storage checks:</p>
<p>zfs list -t snapshot | grep tank</p>
<p>du -sh /mnt/tank/data/media/*</p>
<p>du -sh /mnt/apps/appdata/*</p>
<p>ls -lh /mnt/tank/backups/configs/</p></td>
</tr>
</tbody>
</table>

**Part 17 — Changing Settings**

All settings live in config.env. Edit the file, then redeploy the stack
from TrueNAS Apps.

<table>
<colgroup>
<col style="width: 100%" />
</colgroup>
<tbody>
<tr>
<td><p>nano /mnt/apps/scripts/config.env</p>
<p># Make your changes, then Ctrl+X &gt; Y &gt; Enter to save</p>
<p># Redeploy from TrueNAS Apps &gt; media-stack &gt;
Update/Redeploy</p>
<p># OR restart a specific container only:</p>
<p>docker restart jellyfin</p></td>
</tr>
</tbody>
</table>

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

**Part 18 — Quick Reference**

**All App Addresses**

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

**Key File Paths**

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

**Final Build Checklist**

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

|  |
|:---|
| **📋 Guide version** |
| TrueNAS SCALE Home Media Server — Final Edition |
| Base architecture: v13 (hardlink-optimised tank/data dataset, TrueNAS UI snapshots, rclone mount as Post Init, RENDER_GID, Docker log limits, explicit subnets, Install via YAML) |
| Additions: full app setup, phone app guide, pipelines, VAAPI details, indexer priorities, naming formats, Bazarr subtitle providers, qBittorrent startup password, BIOS specifics |
| Intel Core Ultra 5 225 · Netanya, Israel |
|  |
