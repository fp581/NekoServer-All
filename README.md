# NekoServer All

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

This is the edition of NekoServer for all major Linux distributions. **It runs on Arch Linux, Nyarch, Debian 12+ and Ubuntu 24.04+**, with more to come. Debian and Ubuntu support is new and not yet proven on a real install, so try it on a spare PC or a virtual machine first. For the Arch-only edition, see [NekoServer for Arch](https://github.com/fp581/NekoServer---Arch-based-media-server).

You need a 64-bit PC with one of those installed, plus an SSD, an HDD and a USB drive, already formatted.

```bash
git clone https://github.com/fp581/NekoServer-All.git
cd NekoServer-All/nekoserver-v0.28.3
```

Then follow the step-by-step guide: **[nekoserver-v0.28.3/README.md](nekoserver-v0.28.3/README.md)**.

## More

- What changed in each version: [CHANGELOG.md](CHANGELOG.md)
- How support for another Linux family is added: [PORTING.md](nekoserver-v0.28.3/PORTING.md)
- Every change is tested automatically in GitHub Actions. The tests simulate the hardware, so try a full round on your own PC (play a video, upload a photo, restore a file) before trusting it with your only copy of anything.
