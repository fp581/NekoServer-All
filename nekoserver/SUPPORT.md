# What is supported

Not every way of setting up NekoServer has been tried equally hard. Every release so far is a beta: even a **tested** setup hasn't run on many real servers yet, so try a full round on your own PC (play a video, upload a photo, restore a file) before trusting it. This page says, for each choice, how much you can lean on it. Setup follows the same list: it says so when you pick something experimental, and it refuses what it cannot run safely instead of guessing.

| Level | What it means for you |
|---|---|
| **Tested** | Covered by every release's automatic tests, and tried end to end on a test machine. |
| **Experimental** | Built with the same safety checks and covered by the automatic tests, but tried end to end less (or only in a test lab). Keep a backup you have tried restoring before it holds the only copy of anything, and tell us what happens. |
| **Not supported** | Setup refuses it, with a message that says what to do instead. |

## Drives

| Setup | Level | Notes |
|---|---|---|
| SSD, media drive and USB backup drive connected to this PC (inside it or by USB) | **Tested** | The layout NekoServer was made for. |
| The media drive or the backup drive on a NAS over **iSCSI** | **Experimental** | STORAGE.md, "Drives on a network storage box (NAS)". Tried in a test lab with a real iSCSI target: restarts, the NAS starting after the PC, network cuts short and long, a power cut. Not yet on a real NAS box. |
| A NAS login with a password (**CHAP**) | **Experimental** | Tried in the test lab: a wrong password is refused, the right one connects, and it is kept when setup runs again. |
| Choosing per app where its files go (photos, music, Nextcloud on the SSD or the media drive) | **Experimental** | New in 0.46.0. Every backup, restore and check follows the choice; the default layout (photos and music on the SSD) is the tested one. |
| The **SSD** (the apps' settings and databases) on a NAS | **Not supported** | The databases need a disk that never goes away while they run. Setup never offers a NAS disk for the SSD. |
| Drives shared by a NAS as folders (**NFS** or **SMB/Samba**) | **Not supported** | The apps need a real Linux filesystem (owners, permissions, file locks). Use iSCSI instead; most NAS boxes offer it. |
| A drive that is just a folder of another drive | **Not supported** | Setup stops and says to unmount it: only a whole drive (or a Btrfs subvolume) can be a NekoServer drive. |
| The same app's files on both the SSD and the media drive | **Not supported** | Setup can't tell which ones the app uses. It stops and asks you to say which (`"locations"` in `my-config.json`). |

## Linux

| System | Level | Notes |
|---|---|---|
| **Ubuntu 24.04** | **Tested** | Every release's automatic tests run on it, and the NAS test lab is an Ubuntu 24.04 machine. |
| **openSUSE Tumbleweed** | **Tested** | The test rig the recordings on the front page come from. |
| **Arch Linux** / **Nyarch**, **Debian 12** or newer, other Ubuntu-based systems | **Experimental** | The automatic tests cover their package handling; less often tried end to end. |
| **Fedora**, **RHEL** (Rocky, AlmaLinux), **openSUSE Leap** | **Experimental** | Tried only in a test setup without SELinux. PORTING.md says how each one is handled. |
| Anything else, or a processor that isn't x86-64 | **Not supported** | Setup checks this first and stops before changing anything. |

## When something moves between levels

A choice becomes **tested** only after it has run on real servers through at least one stabilization release (a release with fixes only, no new features, such as 0.46.1). The CHANGELOG says when one moves.
