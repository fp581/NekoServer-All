# NekoServer

**Turn a Linux PC into a home media server**, with your own video library, phone photo backup, music streaming and private cloud drive, set up by one guided installer.

| | |
| --- | --- |
| 🎬 **Movies & TV** | Jellyfin, with Seerr for requests and Sonarr / Radarr / Prowlarr / qBittorrent to find and download |
| 📷 **Photos** | Immich: automatic backup from your phone |
| 🎵 **Music** | Navidrome (with Lidarr) |
| ☁️ **Files** | Nextcloud: your own cloud drive |
| 🛡️ **Backups** | Weekly photo and file copies to an HDD and a USB drive, nightly app backups, tested monthly updates |
| 🏠 **Access** | Secure from home, and optionally from anywhere with Tailscale |

The installer creates every app account, connects the apps to each other, and walks you through anything that needs you, one yes/no step at a time. It never formats a drive.

## Get started

NekoServer runs on **Arch Linux, Nyarch, Debian 12+, Ubuntu 24.04+, Fedora 42+, RHEL 10+ and openSUSE Leap 16+ / Tumbleweed** (with Rocky Linux, AlmaLinux and CentOS Stream 10+; and systems built on Arch, Debian or Ubuntu, such as Linux Mint), with more to come. Fedora, RHEL and openSUSE support are new and were tried only in a test setup without SELinux: testers on those are very welcome. It hasn't been proven on a real install yet, so try it on a spare PC or a virtual machine first.

You need a 64-bit PC with one of those installed, plus an SSD, an HDD and a USB drive, already formatted.

```bash
git clone https://github.com/fp581/NekoServer-All.git
cd NekoServer-All/nekoserver
```

Then follow the step-by-step guide: **[nekoserver/README.md](nekoserver/README.md)**.

Already running NekoServer? Run `git pull`, then `sudo bash install.sh` in the same folder: your settings stay where they are ([Updating to a newer release](nekoserver/README.md#install)).

## More

- What changed in each version: [CHANGELOG.md](CHANGELOG.md)
- How support for another Linux family is added: [PORTING.md](nekoserver/PORTING.md)
- Every change is tested automatically in GitHub Actions. The tests simulate the hardware, so try a full round on your own PC (play a video, upload a photo, restore a file) before trusting it with your only copy of anything.
