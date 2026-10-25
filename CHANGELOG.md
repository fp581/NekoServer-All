# Changelog

## v0.40.0 — 2026-10-26

Bug fixes only. A review of the code that changed since 0.39.2, and every supported Linux family installed for real.

### Tried for real
- **Every family, on this release:** an update from 0.39.1 on openSUSE Tumbleweed and Ubuntu 24.04, an upgrade from 0.35.1 on Fedora 44 (four releases in one step), and the update or a fresh installation on AlmaLinux 10, Arch and openSUSE Leap 16. On each: setup finished every step it can do there (26 of 28: the Tailscale sign-in waits for a person, and the TV and desktop steps need a screen), the feature check passed every check (45 to 47), with the monthly update run for real (checksum check, snapshot, restore test of every app, update), and the fault suite passed 30 of 30 (the monthly update cut off at three points, a backup cut off, the HDD and the USB drive unplugged, an app crash, a full HDD, a Docker restart).
- The unit tests passed on all of them (939 Python tests on Python 3.12 to 3.14, 332 Rust tests).

### Fixed
- **"As of the last backup" restores the newest list, also after the clock was set back.** Each weekly copy's list of the files you had is named by the time it was made, and the restore takes the newest name. A clock that went back (a flat clock battery corrected later, a virtual machine catching up) gave the new list an older name than last week's, so the restore would have used last week's files. A new list now always takes a name after the newest one there. Found by the unit tests on the test rig, where WSL's clock stepped back two seconds.
- **A copy of a Btrfs drive is no longer taken for a pool.** Since 0.39.5 setup accepts a Btrfs pool over several drives (one filesystem UUID, a device on each drive). A drive copied with `dd` or a disk cloner has the same UUID too, and was offered as such a pool; mounted by UUID, the kernel could then write to either drive. Each device of a real pool has its own device UUID (`UUID_SUB`), so setup now checks that, and two drives with the same one stop setup as cloned drives, as before 0.39.5. The system's own drives are never checked (they are never offered).
- **Only the setup window itself can claim it.** The window's address works once: the first page load gets the session, and everything after needs it. A request of another kind (a POST) sent to the address first claimed it too; now only loading the page does.
- **The unit tests no longer depend on this PC's own NekoServer.** One test read the real `/etc/docker/daemon.json`, and failed on a PC running NekoServer whose SSD was mounted; a runtime test of the dashboard's app restart read the PC's own unfinished-job records, and failed on one with a backup left to finish.
- **The unit tests no longer stop at a question when run from a terminal.** One test let setup ask about the backup counts of releases before 0.30.0, and waited for an answer for ever; it now runs as without a keyboard, as in CI.
- **Setting up again after the uninstaller no longer runs every backup at once.** The uninstaller left systemd's record of when each NekoServer timer last ran. A later setup on that PC switched the timers on again, systemd counted every night since as a missed run, and the nightly app snapshot, the settings snapshot and the weekly USB copy all started right away: the apps stopped for the snapshot just as setup said it was done. The uninstaller now removes those records with the timers. Found by a fresh installation on AlmaLinux 10 after the uninstaller.

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there (the runtime is built again for this release).
- **The repository's history was cleaned**: old built runtimes and large recordings were taken out, so a clone is about a fifth of the size. A clone made before this cannot simply `git pull`. Clone again, then copy your `my-config.json` into the new `nekoserver` folder.

## v0.39.7 — 2026-10-25

### Fixed: backups
- **Restoring an earlier date can no longer return newer content.** When a Nextcloud file became a folder, the weekly copy moved the file's saved copy into `neko-replaced`, but the list of files from that earlier date still pointed at the old place. That date's restore then called the file missing, and if a new file later took the same name, it restored the new file's content and said its checksum matched. Each list now keeps the checksum of every file's content, and a restore goes by that checksum: it finds the saved copy wherever it was moved, and names a file as not restored rather than hand out other content. Lists made by 0.39.6 have no checksums; from those, a file that changed between file and folder later is left out (with a note), and "Everything ever kept" still has every saved copy.

## v0.39.6 — 2026-10-25

The improvements left out of 0.39.5, plus a fresh review of the drive checks, each with a regression test.

### New: restore only the files you had
- **Restoring photos or Nextcloud files can leave out what you deleted or renamed.** Each weekly copy now writes down which files you had at that moment. The recovery menu asks **Which files?**: as of the last backup (recommended), as of an earlier backup you pick, or everything ever kept. A backup made before 0.39.6 has no list yet; then it restores everything, as before (RECOVERY.md).
- **Nextcloud's database is copied every week.** The weekly USB backup now also saves Nextcloud's database (users, shares, calendars, contacts) on the HDD and the USB drive, the newest 4 in each place. Without a USB backup, only the monthly snapshot has it (STORAGE.md).

### Fixed: drives
- **A working Btrfs drive is no longer unmounted at every start.** The start-up check took a mounted Btrfs SSD or HDD for an unplugged one, unmounted it, mounted it again and restarted Docker.
- **Encrypted drives Linux won't unlock by itself are named.** Setup now refuses a drive that `/etc/crypttab` marks `noauto`, or an LVM volume or RAID array on an encrypted drive that is not unlocked at start, and says which one to add. Otherwise the apps would stay stopped after a restart.

### Fixed: apps
- **Taking an app out cleans up after it.** Removing Sonarr, Radarr, Lidarr or qBittorrent from `media.services` also removes the connections setup made to it in Prowlarr, the other apps and Seerr. Connections you made or changed yourself stay.
- **A new qBittorrent password reaches Sonarr, Radarr and Lidarr.** A password changed with `account-setup.py` is saved in their download clients too, and each setup run checks that they can still sign in.
- **qBittorrent's manual mode is kept when Lidarr is not used.** Automatic mode is now set only when Lidarr is selected (music needs it), and setup says when it switches it back on.
- **Imports can't be redirected while copying.** A file or folder swapped for a link during an import can no longer make it read outside the source or write outside the library. An imported copy is also never more open than its original: other accounts on the server can read it only when they could read the original.

### Fixed: dashboard
- **Wrong PINs lock out only the device that typed them.** One phone typing wrong PINs no longer locks out other devices or the server itself. Many devices guessing together are still slowed down. If you still use a 4-digit PIN from before 0.28.7, set a 6-digit one (`sudo bash install.sh`).
- **Only the control service can reach the lighting service (OpenRGB).** Before, any program on the server could change the lighting without the PIN. Network lighting devices (smart bulbs, light strips on the network) are not reachable from it any more.
- **The fixed fan speed slider starts at "Never slower than".** It showed speeds the fan would never really run at.
- The README says that apps can be restarted or closed from the dashboard (with the PIN), and the "busy" answer (E47) uses the standard reason "Conflict".

### Fixed: monthly update
- **Old app images are always cleaned up.** Also with `keep_snapshots` 1, after a rolled-back update, and after a run that stopped once it had downloaded updates. Each run's `retention.json` lists the images it removed.

### Fixed: uninstalling
- **Samba is switched off again when NekoServer switched it on.** It stays on when it was on before NekoServer, or when your own `smb.conf` uses it.
- **Nothing left behind goes unsaid.** The uninstaller names every file and step it could not remove or switch off, lists them at the end, and no longer says "Done." when something is left.
- **Run from a fresh download, it finds your drives** from setup's records in `/etc`, and says which settings it used.
- **Your own journal size limits are kept.** The server OS step keeps stricter limits you set yourself and says when it replaces a looser one.

### Upgrade notes
- Run `sudo bash install.sh` once after `git pull`, as usual. The "as of" restore works from the first weekly copy after the upgrade.

## v0.39.5 — 2026-10-25

A review round of the parts everyone meets first (installing and setting up, setting up the apps, the monthly update), then a strict review for edge cases, races and system failures, and a full independent review of the whole project, each finding checked against the code and fixed with a regression test.

### Fixed: setting up the apps
- **Lidarr can import music downloads.** qBittorrent puts a download in its category's folder only in its automatic mode, and starts new torrents in manual mode unless told otherwise, so an album landed on the HDD, where Lidarr (which sees only the SSD) could never find it. Setup now switches new torrents to automatic mode. Torrents you already have keep their mode.
- **Choosing an indexer in the terminal works.** Prowlarr refused every indexer chosen there (its catalogue leaves the indexer without a profile), so each one ended at "do it in the Prowlarr page". It now goes into Prowlarr's own profile, switched on.
- **Hardware video you switched off stays off.** Switching it off in Jellyfin or Immich was undone by the next `sudo bash install.sh`. **Intel graphics older than about 2014** (before Broadwell) use VA-API instead of Quick Sync, which they can't do on Linux: a one-second Quick Sync test encode decides, and a Quick Sync setting an earlier NekoServer made on such a chip is replaced.
- **Imported TV shows show up as shows.** Each imported folder went into a folder of its own inside the library, which Jellyfin read as one show (with the real shows as its seasons), and Sonarr, Radarr and Lidarr as one item. Imports now land in the library folder itself. What an earlier release imported is moved up there (a rename on the same drive, nothing copied again); a show already there under the same name is named for you to merge. A file already in the library that differs is never replaced.
- **Importing again is quick, and a source that is gone after its import no longer nags.** A rerun read every file twice (source and copy) to compare them; copies now keep their original's date, so they are recognised by size and date. A drive unplugged after its import finished is reported as "copied before", not as a step left to do.
- **New Jellyfin libraries are scanned at once,** so films and shows imported before them appear without waiting for Jellyfin's 12-hourly scan.
- **A failed onboarding says why,** not only the error's type.
- **NEXTCLOUD.md and MUSIC.md** say where your logins really are (the credentials file) and that a password you change in the app must also be told to setup (`account-setup.py`, **c**); following the old text made every later run stop at the account check.

### Fixed: installing and setup
- **Commands in the docs use your settings.** `sudo bash install.sh --module … --apply` without `--config` ran on the template `config.json` (no drives in it) and saved your answers into it, which git tracks, so the next `git pull` failed. Your `my-config.json` is now the default once the wizard has made it, and changes are never saved into the template.
- **What setup can't run without is said first.** SELinux enforcing (Fedora's default), an older systemd, a pending restart: said before minutes of package installs and the runtime build, and checked once more just before setup starts (an automatic update may have installed a kernel meanwhile).
- **Start in the setup window no longer says yes to everything.** It sent every switch as drawn, all on, so the TV (Steam), phone alerts and outage alerts were saved as wanted without being asked. Only the switches you press are saved; the TV and the alerts you leave alone are asked about when setup gets to them.
- **The setup window's last screen** shows setup's own verdict ("ready" or "almost ready"), the same as the terminal, the first checkup included.
- **A desktop whose Desktop folder is the home folder** (as xdg-user-dirs sets it once `~/Desktop` was deleted) gets its app shortcuts in the menu, instead of a failed step.
- **Ctrl+C at "Use them for this release?"** stops setup. It used to count as "no", make new settings, and never offer the earlier ones again.
- **A FAT32 or NTFS USB stick** is named, with how to make it usable (exFAT or ext4), instead of "connect it first".
- The welcome text and README no longer claim more than setup does (a full system update happens on Arch only; the timezone is the PC's own).

### Fixed: the monthly update
- **Updates are downloaded before any app stops.** The release lookups and image downloads ran after the apps had been stopped for the snapshot and its restore test, often for hours, and one GitHub or registry hiccup then cancelled the month. They now run first, with the apps still up, and a download refused for now is tried again (after 30 s, 1½ and 4 minutes); one that can never work (no such image, no access) is not. A new Immich release that can't be checked waits for next month without holding back the other apps.
- **The apps are never stopped in the morning, and big libraries still get their monthly snapshot.** On a big library, checking every copy could run past the night window. The check now reads each copy once instead of three times; the window is checked right before anything stops (past it, nothing is stopped or changed); and after a month whose night ran out, the next month takes its snapshot first and checks the copies the month after. If even that does not fit, the run fails and says how to give it a longer night.
- **A full or unmountable USB drive no longer cancels the monthly update.** Only damage found by the checksum check does; anything else is "done in part", with the reason.
- **Old app images are cleaned up.** Each update left the replaced images on the SSD for good (a few GB a month). NekoServer's own images from two updates ago that nothing uses are removed; your own images are never touched.
- **A rollback can start apps whose old image Docker lost** (`docker image prune -a`, say): it is loaded from the snapshot's `images.tar` first.
- **Nightly backups wait for an unfinished monthly update** (after a power cut, say) instead of saving its half-updated apps as the newest backup. **The weekly USB copy** no longer runs during the monthly update.
- **A run cut off while it tidied old snapshots** (a restart, the time limit) is finished as the installed update it is, not reported as a failure.
- **A later NekoServer release can finish an unfinished run** an earlier one started. A restore test cut off part-way has its unpacked copy removed (it took a second copy's space on the HDD for months), and a test container Docker can't remove never keeps the real apps from starting. The resume step at boot no longer waits for the backups' lock when there is nothing to finish.

### Fixed: backups and checksums
- **Damage found by the monthly checksum check stays on the dashboard and in the repair tool until it is repaired.** The next weekly or hourly copy (which compares only size and date) wiped it: the dashboard said "Checksums match", the phone got a false "RECOVERED", and the repair tool found nothing to repair. "Checksums match" now means a full check in the last 35 days.
- **A backup drive unplugged or failing during a backup** stops the backup with a clear message, instead of writing onto the system disk.
- **A full HDD no longer stops the nightly app backup for good:** what failed runs left is cleaned up, then older app backups if needed, always keeping the newest. A power cut while old backups are deleted no longer leaves a broken one that looks complete.
- **Files deleted or renamed during a backup** are skipped and counted instead of failing it; Nextcloud names that are not valid text are left out and listed; photos dated in the future (a camera with a wrong clock) are backed up; a copy dated in the future is checked by content. Music added in the last 10 minutes waits for the next run instead of failing it.
- **Errors say the system's reason** ("No space left on device"). If the USB drive fails to unmount, the real reason the copy failed is still shown. After the clock is put back, "last success" and alerts are no longer lost or held back.
- **The weekly USB copy no longer blocks the nightly backups for hours**, and never overlaps the monthly update. **Nightly backups wait only while a monthly update is cut off half-way** through installing or rolling back (the settings backup never waits), and an unfinished update is tried again every hour.
- **The dashboard says why a monthly run was "done in part"** (USB drive full, Immich release not checked).
- **A monthly run that stops before its update** no longer leaves a downloaded version for the next `sudo bash install.sh` to start untested: apps are pinned to the images they run before anything is downloaded.
- **The old-image clean-up** never removes an image that carries a name of your own, and also removes images from updates that were rolled back. Monthly runs that archived nothing no longer leave folders behind.
- **`sudo bash install.sh --finish-update`** lets a newer release finish a monthly update an earlier one left unfinished.
- **A restore that fails says why** (a full drive, a folder that already exists, a damaged file) and names the half-filled folder it left. A restored file is read back from the drive, not from memory, so damage on the drive is seen.
- The docs say which data is backed up nightly, weekly and monthly (the Nextcloud database only monthly), how to start a restored app at its exact saved versions, and that restoring an add-only copy brings back deleted and renamed files.

### Fixed: drives, recovery after reinstalling Linux
- **Reinstalling Linux keeps your earlier settings on the SSD,** also when the runtime is built again: they move with the kept copy, as `config.previous-<date-time>.json` (the oldest is always kept).
- **Reattaching checks first that the NekoServer program kept on the SSD runs on the new Linux** (one built on Arch, Ubuntu 24.04 or Fedora may not run on Debian 12); if not, it says to run `sudo bash install.sh` first and changes nothing. New `recover.py install-docker --apply` installs Docker the way setup does and leaves it stopped for reattaching (RECOVERY.md step 4).
- **More drive layouts work:** a Btrfs pool over several drives (it stopped setup with "Duplicate filesystem UUID"), LVM volumes, software RAID and encrypted (LUKS) drives that Linux unlocks at start. The disk Linux runs from is recognised from every place it is mounted (a Btrfs system partition, an EFI partition at `/efi`).
- **Setup's and the recovery tools' drive checks also check the Btrfs subvolume**, and refuse a mounted folder of an ext4 or XFS drive instead of saving it as a "subvolume" that stopped every later run.
- **Docker's settings are checked before they are written,** and never point Docker at the SSD before its drive guard exists: a setup cut off in between could let Docker fill the folder on the system disk. Settings Docker's service already sets as flags are named instead of leaving Docker unable to start.
- **An fstab line for the same drive or folder written another way** (`/dev/disk/by-uuid/`, `LABEL=`, `PARTUUID=`, a trailing slash) is found, instead of a second line being added. A slow mount (a big drive replaying its journal) is waited for up to 10 minutes. A drive choice is saved only once it passed the checks. The first fstab copy is never left cut off.
- **The shared lock never makes folders on the system disk** when the SSD is not mounted. When the HDD comes back, its power settings are applied again and the dashboard sees the drives again.
- RECOVERY.md: reinstall with a normal `sudo bash install.sh` first; the read-only mount commands for copying the settings off the SSD are really read-only; rebuilding Immich from the USB drive uses the recovery menu's newest-version restore.

### Fixed: setup, packages and interruptions
- **Upgraded servers get the steps newer releases added** (desktop shortcuts, hardware video): they are added to your `my-config.json` profiles (a step you took out stays out), and "ready" counts every step of the release.
- **The tools in the NekoServer folder use your `my-config.json`** without `--config` (`acceptance.py --save` from the docs failed on the template); the copy kept on the SSD uses its own settings. Settings you once put into the template `config.json` are recognised, with how to keep them.
- **Ctrl+C stops setup** at any question (it skipped that step); Ctrl+D leaves a question for later. **Closing the terminal or losing an SSH connection** no longer leaves services blocked from starting.
- **A slow package download** makes setup ask the package manager to stop and wait for it, instead of killing it; a pacman lock left by a cut-off pacman is removed. If automatic updates or a software centre are installing, setup says so and waits (up to 10 minutes). A package source that only gives a warning is tried again. A package that can't finish installing is named, with how to fix it. On Debian and Ubuntu, Steam's licence is accepted for the TV step.
- **A damaged Docker or Tailscale repository key** is downloaded again. **A wrong clock** is named, with `sudo timedatectl set-ntp true`.
- **The runtime build** retries its downloads, says what failed in one line, and is rebuilt when a build was cut off.
- **The setup window's address works once:** the browser that opened it gets a private session, so another account on the PC that reads the address can't answer setup's questions. A window closed mid-reply no longer prints errors into the terminal.
- **Folders setup makes are never left owned by root** after an interruption (TV, desktop shortcuts, app settings); earlier leftovers are repaired. A leftover file from a cut-off first save no longer blocks the account store. An old record on the system disk can no longer undo newer results. Connecting the media apps holds the backups' lock. Onboarding problems end as "needs setup" with the reason.
- **Samba share paths** may use only letters, digits, spaces and `. _ - /` (a line break could have added settings to Samba's configuration). Shared folders over Tailscale work after a restart: Samba waits up to a minute for the Tailscale address.
- **The TV step** brings its own YouTube and Steam shortcuts up to date when the browser or Steam moved, instead of stopping. Debian's Firefox ESR gets the Web Browser shortcut.

### Fixed: apps
- **The setup checkup no longer reports Sonarr, Radarr and Lidarr as "still to set up"** when their qBittorrent connection is correct, and **no longer fails Nextcloud when you switched it off**.
- **Imports:** a finished import is not copied again (Sonarr, Radarr or Lidarr may have renamed those files since; to copy what is new: `source_import.py --again`); a file the library already has differently is kept and named instead of refusing the whole source; earlier imports move up even with their drive unplugged; a network share no longer mounted after its import is "copied before". The source wizard asks for the folder that contains your shows, films or artists.
- **Hardware video you switched off stays off**, even after a run that couldn't check the app. Intel chips that encode only in low-power mode get hardware encoding in Jellyfin. Immich still starts when the graphics chip is gone (also in the monthly update).
- **Nextcloud keeps working over the home and Tailscale addresses** after its Docker network is recreated, and its database gets a minute to shut down cleanly. Seerr request setup accepts Sonarr 5.

### Fixed: dashboard, alerts and fans
- **Restore tests you witnessed stay recorded** through later checks (including the one setup runs at the end), and a complete server check no longer turns into "Finish checking your server" the next day; a recheck is suggested after a month.
- **Phone alerts now include** Tailscale signed out or about to sign out, monthly backups without a backup password, and the outside heartbeat not being reached. A 5-minute collection interval is no longer called "monitoring unavailable".
- **A hung drive no longer freezes the dashboard.** The heartbeat isn't called stale right after boot. The Sensors page no longer shows unconnected 127 °C probes.
- **Fans** keep following their curves while the control service waits for an app restart, survive a crashed request, and a fan that stops at its speed (with Detect fans run) is put to full speed with a note. A processor at its normal 95–100 °C under full load no longer triggers full speed or an urgent alert straight away: that happens at the chip's own limit, at 105 °C, or after 5 minutes above 95 °C. The fan speed slider starts at 20 %.
- The docs no longer call the phone dashboard read-only (changes need the PIN).

### Fixed: uninstalling
- **The uninstaller waits** while a monthly update or setup is running. **If it stops part-way**, NekoServer's services are switched back on. It replaces `/etc/fstab` and Docker's `daemon.json` in one step and keeps the old ones beside them, keeps Docker's start guard while Docker's data stays on the SSD, finishes or safely undoes a package install setup left half-done, and lists drives it couldn't unmount and anything it couldn't remove.
- **A preview of the uninstall without sudo** still says which folders it couldn't look into. On Python 3.14 those folders were quietly skipped instead.

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there (the runtime is built again for this release).
- If `git pull` stops because `config.json` has local changes (0.39.4 could save your answers into it): run `git checkout -- config.json` (your settings are in `my-config.json` and on the SSD), then `git pull` again.
- If you imported TV shows with an earlier release, this run moves them up into the TV library; Jellyfin then shows them as shows. If it names a show already in the library, compare and merge the two folders by hand.

## v0.39.4 — 2026-10-25

A review round of the parts no review had covered yet: recovery, the gateways and remote access, and the dashboard.

### Fixed: recovery
- **One damaged or missing file no longer stops a whole restore.** A photo damaged on the HDD (and so never copied to the USB drive), or a file a backup lost, made the recovery menu refuse every photo or Nextcloud restore from that backup, whichever backup was tried. Now the good files are restored. The damaged or missing ones are left out and named (in the restore folder's `NOT-RESTORED.txt`) so they can be taken from the other backup. A missing file is asked about first. A checksum list a drive lost mid-write no longer blocks the restore either: the files come back unchecked, and the restore says so.
- **A Nextcloud folder of your own named `neko-damaged`** (or a file named like NekoServer's checksum list) at the top of your files is restored like any other. Only the backup's own, at the top of the whole backup, is left out, the same line the backup program draws.
- **App and settings restores keep each file's owner and permissions,** so a file copied back works for its app (copy it with `sudo cp -a`). A night when Steam was open (no Steam files in it) is marked in the list of nights and named after the restore.
- **Names that end in a dot** (`notes.`) get their newest version back; **a file without an extension** (`README`) no longer takes another file's versions (`README.md`'s).
- **The monthly restore** says where the password files are when the snapshot was made without a backup key (in it, unencrypted). **Restore tests of a photo snapshot** accept copies on a desktop drive whose path has spaces (`/run/media/you/My Passport`). **A password-list restore that cannot unpack** (a full disk) says why and leaves nothing half-done, instead of a Python traceback.
- **Nextcloud files ending in `.part`** that are your own are backed up; only Nextcloud's own unfinished uploads are left out.
- **Restores and media imports never read the system's own folders** (`/etc`, `/root`, `/usr` and the like), even if a settings file or a backup's own record were changed to point there. Your drives, wherever they are mounted (`/srv`, `/mnt`, `/data`, a USB drive under `/media` or `/run/media`), and folders in `/home` work as before. A drive name may also have brackets and a few other signs (`My Passport (2)`).
- **The weekly USB copy waits** for a restore from the USB drive (up to 8 hours) and for a photo archive run still writing (up to 6 hours), instead of failing.

### Fixed: gateways and remote access
- **Tailscale routes are recognised by their port and where they lead,** not by the server's name or the dashboard's port. A renamed server (or one added to the tailnet again after a reinstall), a dashboard moved to another port, or a Serve route of your own on another port no longer stops remote-access setup with "Malformed gateway ownership journal". A route of your own is never touched. A route of yours on a port NekoServer needs is named, with what to do. A dashboard moved later keeps its phone address: the route follows after every setup run.
- **Nextcloud on this PC itself (`http://127.0.0.1:8088`) works with a gateway set up.** Nextcloud was told to build every link as https, so this PC's own browser was sent to https on that port, which fails. Both gateways already tell Nextcloud the client used https, so the forced setting is taken out (and removed where an earlier release set it). Its link address no longer flips between the two gateways' ports either.
- **Tailscale's 180-day sign-in.** Setup now says to switch off key expiry for the server. The dashboard warns two weeks before a sign-in runs out, and says so at once when the server is signed out.
- **Firewall advice for nftables** gets an `nft` command instead of a `ufw` one, and every firewall hint says to test the home addresses from a phone (the check from the PC itself passes through any firewall).
- **An old home address never opens another app.** A port given to an app stays with it while the app is not selected, and the app gets it back. A port the gateway closed a moment ago no longer looks taken.
- **Immich, Navidrome and Nextcloud are published only once their first account exists,** as Jellyfin and Seerr already were: an app reset or restored empty would otherwise let whoever opened it first become its administrator.

### Fixed: dashboard
- **An unplugged temperature probe no longer holds every fan at full speed.** Many boards report a probe input with nothing connected as 127 °C. Fan control and fan detection took it for overheating. Readings of 127 °C and above, and sensors reporting a fault, are not taken for temperatures.
- **Failing phone alerts and a failing outage monitor** now show as "needs attention", not only as a badge on one card.
- **A backup that keeps failing stays "Failed"** once it is also overdue, instead of dropping to "Overdue".
- **An app that runs but does not answer** raises an urgent alert, as the page shows it, and is not reported as recovered by phone.
- **A switched-off backup** no longer shows "schedule is off" among the issues. An alert sent about a switched-off backup or an app taken out of the list is dropped quietly, never followed by "RECOVERED".

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there (the runtime is built again for this release). Afterwards, in the Tailscale admin console, choose **Disable key expiry** for this server if it isn't already.

## v0.39.3 — 2026-10-25

The monthly run now starts by checking every backup copy, a guide for both ways of following the installation and for uninstalling, and a round of fixes to the uninstaller, the setup window, the backups and the dashboard.

### Changed
- **The full checksum check is the monthly run's first step: check, then back up, then update.** Every copy in the HDD photo archive, and every copy on the USB drive when it is plugged in, is read and compared with its recorded checksum before the monthly snapshot is taken and before any app is updated. Damage stops the run there, with nothing backed up or updated, and the dashboard says which files. The hourly photo archive and the weekly USB copy no longer do this every 4 weeks: they compare size and dates, so they stay quick. A USB drive that is not plugged in is checked in the next monthly run it is plugged in for. The setup space check no longer counts the 4-weekly check either.
- **A guide for the two ways of following the installation** (README, "Two ways to follow the installation"): how to open a terminal and start setup, how the setup window opens, how to open it again (the terminal now always shows its address), and what "Answer this step in the terminal" means. **Uninstalling** is now step by step: the dry run, the checklist (numbers switch lines on and off), the plan and the two confirmations.

### Fixed
- **The uninstaller removes only NekoServer's own folders.** It removed whole top-level folders of the drives, so a folder of your own there went too, and it trusted the settings' mount folders whatever was mounted there. Now only the folders NekoServer makes are removed (the plan lists the rest under "Not NekoServer's, so these stay"), and nothing at all on a drive whose UUID is not the one the settings name, or that is not mounted.
- **The uninstaller stops before deleting anything when something does not stop.** A service or timer that would not stop (or timed out), the fan control not handing the fans back to the motherboard, a container that stayed, or Docker that kept running: each now stops the uninstaller with "Nothing has been deleted yet" and what to do, instead of deleting files under a running program.
- **Samba accounts that were there before NekoServer stay.** The uninstaller removed every account in the share's "valid users". Setup now records the accounts it creates, and only those go; an account you had before is named as staying.
- **Only NekoServer's own Tailscale Serve routes are switched off.** `tailscale serve reset` removed your own routes too. The uninstaller now switches off, one by one, only the ports in NekoServer's record that are still exactly as it set them.
- **The PC can sleep again after uninstalling.** The sleep, logind and journald settings NekoServer added are removed, and the five sleep targets it masked are unmasked. Ones you had masked before NekoServer stay masked (setup now records them before masking).
- **Debian 12 builds the runtime with Debian's newer Rust.** Debian 12's own Rust (1.63) is older than the runtime needs (1.85), and the `rustc-1.85` names setup looked for exist only on Ubuntu. Setup now installs `rustc-web` and `cargo-web` (1.85 in bookworm, newer in bookworm-security), which provide the same commands. When an older `rustc` is already installed it does not replace it on its own: it says to run `sudo apt install rustc-web cargo-web`.
- **A late click in the setup window can no longer overwrite the settings.** A switch pressed, or Start pressed again, after setup had started wrote the window's old copy of the whole settings file over the one setup was updating (the drives it had found, say). Once setup starts, from the window or the terminal, the choices are fixed and later requests are refused; a switch before then changes only its own setting, on the file as it is.
- **Steps that ask in the terminal only say so in the window.** Connecting the media apps, music setup and the shared-folder password run programs of their own, whose questions never reached the window. The window now shows "Answer this step in the terminal" while they run.
- **Nextcloud added later is on the USB drive too.** The weekly USB copy kept copying only what was set up when it was installed. It is now brought up to date after every setup run: Nextcloud added later is copied from the next week on, and an Immich switched off is no longer asked for.
- **A Nextcloud-only USB copy no longer needs the old Immich database.** With Immich switched off its Compose file stays, and the weekly copy took that as a reason to copy the Immich database, reporting itself incomplete once the last copy was 8 days old. The database is now copied only with the photos.
- **A file that became a folder (or a folder that became a file) no longer blocks the Nextcloud backup.** The add-only copy kept the old `Projects` file, which stood in the way of the new `Projects/` folder, run after run, and files after it were not copied. The old one is now moved, with its dated copies and checksums, to `neko-replaced/<date>/` beside the user's files, and restores bring it back as "replaced earlier".
- **A file of yours named `.neko-…` is restored.** Every name starting with `.neko-` was taken for NekoServer's bookkeeping, so a Nextcloud file `.neko-notes.txt` was reported missing and stopped the restore. Only NekoServer's exact file names and patterns are left out now, in the backup program and the recovery wizard alike.
- **An approved Nextcloud major upgrade happens with an exact image tag too.** With `nextcloud:33.0.10-apache` (a tag setup accepts) the monthly update recognised version 33 and the approval for 34, but replaced only `nextcloud:33-apache`, so Nextcloud and its cron worker stayed on 33 and the run reported success. Every accepted tag form is moved now; the update fails (and is rolled back) when either service would stay behind, and it is committed only once Nextcloud itself reports the approved version.
- **OS-reinstall recovery accepts Docker's empty default store.** `recover.py reattach` refused `/var/lib/docker` when it held anything, even the empty folders Docker makes on its first start. It now uses the same test as setup: only a container, an image or a volume counts.
- **A Btrfs drive whose default subvolume is not the top level is set up.** With the default set to `/@data` (say) and no subvolume in the settings, setup mounted it and then refused what it found; on a retry it refused its own fstab line. The default tree it mounts is now saved as the drive's subvolume and its fstab line pinned to it, so a restart mounts the same files.
- **The dashboard's acceptance view goes "Needs recheck" when the USB drive or the photo archive changes.** Its fingerprint now includes the USB drive's UUID and filesystem, the archive folder and the weekly copy's schedule (the same in the setup code and the collector).
- **Switching Immich off no longer brings overdue-backup warnings.** Its archive and database jobs now show as "Switched off" (and the music backup when it is off), instead of eventually "Overdue". A schedule that stops while its app is still set up (switched off, or gone after it had run) is still flagged.
- **A server without a desktop can pass the final checkup.** The TV step is skipped on a PC with no desktop, but the acceptance checklist still asked for its launchers and the TV and controller test. Both now use the same decision (TV setup needs Steam on this Linux and a desktop), and any step setup recorded as not available on this PC is left out of the checks.
- **A media app taken out of `media.services` is stopped.** Setup deployed the apps still listed and left the removed one running, without the dashboard, the nightly backup or the monthly update looking after it. It is now stopped (`docker compose down`, without its volumes); its settings, data and Compose file stay, so adding it back starts it as it was.
- **Phone alerts and the outage monitor switched off are switched off.** Turned off in the setup window (or `"wanted": false`) after they were set up, both reused their saved address and switched their timers on again. Now the choice is read first and the timer stopped and disabled; the saved address stays for switching it back on. For the outage monitor setup also says to pause the check at the outside monitor, or it reports the outage it expects. A timer switched off by choice no longer shows as overdue on the dashboard.
- **With Immich and Nextcloud both off, the weekly USB copy is switched off too.** It stopped saying anything to copy but left its old timer running with the old selection. Its timer is now disabled (the copies on the USB drive stay).
- **A drive that comes back finishes the job it cut off.** When a drive disappeared while a backup or the monthly update had the apps stopped, the drive-return service mounted it and started Docker, but the apps the job had stopped stayed stopped (Docker leaves explicitly stopped containers alone). It now runs the jobs' own recovery after Docker: the monthly update's first (it may roll back), then the nightly app and host snapshots' and a photo snapshot's.
- **Hardware video encoding is switched on only on a chip that encodes.** The check accepted any chip that decodes, then switched on hardware encoding and a fixed list of formats. Some chips decode without an encoder (AMD's Radeon RX 6400 and 6500). Setup now reads which formats the chip decodes and whether it encodes, and a one-second test encode on the chip must pass: Jellyfin gets decoding of exactly those formats and encoding only when it passed; Immich, whose setting needs an encoder, stays in software on a decode-only chip.
- **The optional media monitor follows the app selection.** Its saved settings were written once, so it went on checking removed apps and missed new ones. They are brought up to date after every setup run.
- **A file with a backslash in its name is restored from a nightly snapshot.** Linux allows `\` in a name and the snapshots record it, but the restore refused any path with one, so the whole snapshot could not be restored. Only `..`, `.`, absolute paths and NUL are refused now.
- **Backup counts you chose are never lowered without asking.** Setup moved 14 nightly app and host backups to 7, and 8 Immich database copies to 4, on every run, taking any 14 or 8 for the default before v0.30.0, even one you had chosen. Now it asks once, when someone is at the keyboard, and keeps the answer (and without anyone there it keeps the number). A number seen after that is never touched again.
- **A home network on a bridge of your own (`br0`, `bridge0`) can be chosen.** Every interface starting with `br` was taken for a container's, so a server whose address is on its own bridge could not set up home access, and the gateway would not start. Only Docker's bridges (`docker0`, `br-` and 12 hex digits), libvirt's, Podman's, container links and tunnels are left out now, in setup and in the gateway's start check alike.
- **A Nextcloud user named `appdata_…` is backed up.** Every folder starting with `appdata_` was taken for Nextcloud's own (previews, caches), so such a user's files were silently left out of the USB copy and its space check. Only Nextcloud's own folder is left out now: the one its `config.php` names (`instanceid`), or, when that cannot be read, a name of the form Nextcloud gives it.
- **Recovery opens without the internet or Rust.** `sudo bash install.sh --recover` built the runtime first when it was missing (as on a reinstalled PC), which needs the internet. Recovery needs no runtime, so it no longer builds it; the next setup run does.
- **A database copy dated in the future no longer hides the fresh ones.** A copy made while the clock was wrong (named 2030, say) was taken as the newest for ever, so the fresh copies never reached the USB drive, and retention kept it while removing real ones. The newest copy with a believable date is used now; a future-dated one is kept, named in the job's result and never counted by retention.
- **A snapshot's recovery is no longer cut off while it waits.** The app and host snapshots' recovery may wait up to 3 hours for another job (the monthly update, say), but systemd gave it 20 and 5 minutes, and the same after every backup, so a backup that ended well could be reported failed. Recovery now returns at once when there is nothing to finish, and is given 3½ hours when there is.
- **"Restore tried" asks for the right restore tests.** A Nextcloud-only server was asked for a photo test, the music backup was not covered, and a USB copy of photos and Nextcloud files showed "Yes" after the photo test alone. Each backup now names the restore test of each kind of data it holds (photos, Nextcloud files, music, app settings, host and user files), "Yes" needs all of them, and the checklist has the two new ones (`cloud_restore`, `music_restore`).

### Tests
- A test that changed file owners for real failed as root in a container that may not do that; it now only records the change. A test ran setup on the template `config.json` itself and wrote to it; it uses a copy now. A test that read the real apt lists lock waited for minutes as a normal user. Regression tests for each fix above; the cognitive complexity of the changed functions stays within limits.

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there (the runtime is built again for this release). Setup records the Samba accounts it creates, and the sleep targets masked before it, from this release on. For an install made earlier, running `sudo bash install.sh` once does not add the account to the record (it already existed), so the uninstaller keeps it and says so; remove it yourself with `sudo smbpasswd -x <name>` if you want. The sleep targets are unmasked as long as NekoServer's sleep settings are still in place.

## v0.39.2 — 2026-10-25

Two things the uninstall-then-install cycle turned up, and four more real tries on the rig.

### Fixed
- **Setup after the uninstaller, or on a PC whose Docker holds nothing.** Found by reinstalling on Ubuntu after the uninstaller had run: Docker, back on its default store, had started once and made its empty folder tree, and the Docker step refused with "Docker is already set up on this PC and keeps its data in /var/lib/docker". A Docker store with no container, image or volume in it (just installed, or NekoServer uninstalled) is now taken over: a running one is stopped, and Docker runs from the SSD from then on. One with something in it still stops setup, as before.
- **Samba's untouched example `smb.conf` no longer stops the Samba step.** The same reinstall showed the other half: with the package's example back in place (the uninstaller puts it back; Samba installed before NekoServer leaves it there too), the step refused with "the Samba package's unchanged example, not your settings", and the nightly settings backup refused the unmanaged Samba. The step now sets that example aside as `smb.conf.packaged` itself, as the bootstrap already did for a package it installed, and says so. A `smb.conf` someone wrote still stops the step as before. With both fixes, the whole cycle on Ubuntu 24.04 (the uninstaller with every choice, then a fresh installation that took the settings left in the `nekoserver-v0.36.1` folder): 26 of 28 steps, 46 of 46 checks.

### Tried for real
- **The OS-reinstall recovery path, tried for real on openSUSE Tumbleweed** (RECOVERY.md, "Replace the OS and reattach the existing store"; until now only on AlmaLinux): the uninstaller stood in for the reinstall (NekoServer itself, the containers and the mounts gone, every drive's data kept), the storage module mounted the drives from the saved settings, the Docker step refused the existing store and pointed at `recover.py`, `recover.py check` / `reattach` / `reattach --apply --confirm <SSD UUID>` reattached it from the copy kept on the SSD, and a normal setup run brought everything back: 26 of 28 steps, 47 of 47 checks, passwords and photos as they were, all 16 containers up. No code change was needed.
- **The setup window's rough edges, tried for real:** setup started from the window's Start button; a question (Tailscale) stayed on the page after a refresh and was answered there, which the terminal showed as `no  (from the setup window)`; the window was then closed and the run went on from the terminal to the end. Nothing to fix.
- **The uninstaller on an older install of the Debian family, tried for real** (Ubuntu 24.04 with NekoServer 0.36.1, the 0.39.1 uninstaller, every choice): 22 seconds; no units, containers, networks, settings, Samba pieces, group, fstab lines or mounts left; Docker back on its default data-root; the apt repositories NekoServer added for Docker and Tailscale kept with their packages. One thing it showed: Samba's own example `smb.conf`, which setup sets aside as `smb.conf.packaged` when it writes its own, was left aside. The uninstaller now puts it back when NekoServer's `smb.conf` goes, and says so in the plan. Then on AlmaLinux 10 with NekoServer 0.31.0 (the RHEL family: `smb.service`, dnf repositories): the same, 22 seconds, the example `smb.conf` back in place, the repositories kept.

### Tests
- **A first mutation-testing pass** (`tests/MUTATION-NOTES.md`): cargo-mutants over the runtime's maintenance, backup, control and serve code (1,459 mutants) and mutmut over the deploy and kept-copy rules, the settings migration, the package maps and the maintenance guard (868 mutants, every one killed by the existing tests). Of the 559 Rust survivors, 25 new tests close the real gaps: above all the monthly update's Compose sandbox checks (`valid_model`, 54 survivors: a model with `privileged`, another network or PID namespace, a bind mount outside the app's folders or a port off loopback must be refused; one test covered one bind mount), the weekly copy's kept-names bookkeeping (the existing test never reached it), the dashboard sign-in's second device, the nvidia readings' freshness window. The rest are boundary swaps no input distinguishes, or defence in depth, each noted. No product bug turned up.

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there (the runtime is built again for this release).

## v0.39.1 — 2026-10-25

SonarCloud's review of the uninstaller, and what a dry run without sudo turned up.

### Fixed
- **A settings file can only name NekoServer's drives.** The uninstaller read the drives' mount folders from `my-config.json` and used them as they were. Now a value is used only when it is a plain absolute folder of its own (`/srv/nekoserver`, `/mnt/...`): the root, a system folder or a path with `..` is refused, named in the plan (`!!`) and the default used instead, and the mount check answers only for those folders. Nothing in a settings file can point the removal anywhere else.
- **A dry run without sudo** died on `/root`. A folder the account may not read is noted and the dry run says what it leaves out (`sudo bash uninstall.sh --dry-run` shows the whole plan); the "no app containers found" note also names the missing permission.
- `runtime/build.py` reads the executable from where cargo put it when `CARGO_TARGET_DIR` is set in the environment (a shared build folder), instead of looking in `runtime/target` and failing.
- Code shape: the plan builder is one function per choice, the removal one per step, the menu's toggle its own function; the unused parameter and name are gone; a repeated path is a constant; the tests use `return_value` and `side_effect` instead of lambdas.

## v0.39.0 — 2026-10-25

An uninstaller, and a README that shows the thing.

### Added
- **`sudo bash uninstall.sh`: NekoServer off this PC, what you choose, after two confirmations.** NekoServer itself (its 21 services and timers, programs, settings, the Samba shares and account it made, the Docker and Samba storage guards, the tailnet pages, its group, the desktop shortcuts) always goes. Everything else is a checklist: the apps' containers and networks; the apps' data on the SSD (with Docker's data-root setting put back); your photos; your movies and music; your Nextcloud files; the backups on the HDD and USB drive; the drive mounts (the fstab lines and the empty folders). First the whole plan, with every path and its size, then `yes`, then a phrase that names what goes (`remove NekoServer and delete my photos, backups`). Nothing is ever formatted, and packages NekoServer installed stay for you to keep or remove. `bash uninstall.sh --dry-run` only shows the plan. Tried for real on openSUSE Tumbleweed: every choice, 27 seconds, nothing left behind.
- **The README shows the thing:** the overview picture, and recordings of the real installation, the setup window, the dashboard and the uninstaller from the test rig.

### Fixed
- **The setup window showed the wrong text above a question.** Seen while recording: the drive question ("Choose number") came with the Tailscale step's instructions above it and no drive list, because the window dropped every line the installer had time-stamped (the drive list among them) and kept everything since the step's last heading (the earlier question's text among it). A question now shows what the terminal said since the last answer, or since its step started, with the time stamps taken off and the installer's step markers left out.

## v0.38.1 — 2026-10-24

openSUSE Tumbleweed tried for real, with the one fix it needed; the update flow in the fixed folder tried for real, and the dashboard's runtime copy brought under 0.38.0's replacement rule.

### Fixed
- **openSUSE: lighting is installed under its openSUSE name.** On Tumbleweed the dashboard step failed with `Installing openrgb failed: Did you mean OpenRGB?`. openSUSE spells the lighting package `OpenRGB`; zypper's search found it for `openrgb` anyway (it ignores case) and the install then did not. The openSUSE package map names it, and the availability check asks zypper for the exact spelling (`--case-sensitive`). Leap 16, which has no OpenRGB package, keeps skipping lighting as before.
- **The dashboard's runtime copy follows the same rule as every other copy.** 0.38.0 made a runtime copy NekoServer deployed itself replaceable when the runtime was built again from other sources, with a build record beside each copy; the dashboard step kept its own copy under the older rule and would have stopped with `The installed dashboard file ... differs from the one in this release`. It now installs the record beside its copy and replaces the copy under the same condition. Other dashboard files that differ (the page's files) still stop the step, as a change of yours would.
- **A copy is replaced only when its record names exactly that build.** The rule from 0.38.0 took any record beside a copy as proof that NekoServer put it there. Now the copy's checksum must match the record's: a copy someone put in place of NekoServer's (even with the record left beside it), a record that cannot be read, or a stale one all protect the copy, and setup stops with the same clear message as for a copy without a record. A stale record beside this release's own runtime is simply refreshed.
- **The copy of the release kept on the SSD follows the same rule.** Within one version, the recovery step refused to go on when the copy kept on the SSD (`installer/releases/v<VERSION>`) differed from the folder running setup, which stopped every step after it: a `git pull` into the same folder within a version, the case 0.38.0 set out to handle, got no further than this. Now a kept copy that is NekoServer's own and untouched (every file as its manifest recorded it) is set aside as `v<VERSION>.replaced-<time>` and this folder's files take its place, with a line in the log; one set aside earlier for the same version goes, and the set-aside copy is retired with its version. A kept copy someone changed is still never overwritten.
- **The build checks for room first.** `runtime/build.py` refuses to start when less than 1 GB is free where NekoServer's folder is, naming the folder and what is free, instead of Cargo failing halfway through with a full disk. (A build takes about 150 MB; the margin is for Cargo's cache and other things on the disk.)
- **An answer typed in the terminal that lost the race to the setup window is dropped.** When both were answering the same question and the window's answer arrived first, the line typed in the terminal stayed in its buffer and would have answered the next question. The terminal's input is cleared when the window wins.
- **An earlier settings file that cannot be read is named, never passed over silently.** Updating from a `nekoserver-v0.37.x` folder, setup offers the newest earlier `my-config.json` it finds (then the copies it kept on the SSD). A damaged one is now reported by path and the next readable one offered, instead of setup quietly taking an older file, or starting from scratch, without a word.
- A test of the "monthly update still running" guard asked the real system about its maintenance service (a default argument bound the real check at import time), so it failed on a PC where that job was running at that moment. The check is looked up when it runs.

### Upgrade notes
- `git pull` in `nekoserver`, then `sudo bash install.sh` there: it offers your settings (answer `yes`) and builds the runtime for this release (a minute or so).

## v0.38.0 — 2026-10-24

The release folder is simply `nekoserver` from now on, so `git pull` updates it in place and a release's changes show as changes; the runtime is built again when it would not run here; and the on-PC build was tried on the other families.

### Changed
- **One folder, `nekoserver`, for every release.** Until now each release lived in a folder named for its version (`nekoserver-v0.37.3`), which made every update move every file and left your `my-config.json` behind in the old folder. The version now lives in `neko_common.VERSION` (and `runtime/Cargo.toml`); the copies setup keeps on the SSD and under `/usr/local/lib` are still named by version, so nothing changes on an installed server. Updating is `git pull` then `sudo bash install.sh` in the same folder; coming from a versioned folder, setup still finds the settings left there and offers them.
- **The runtime is built again when it would not run here.** `install.sh` also checks that the runtime starts and names this release, not only that it was built from these sources: a copy built on another Linux (kept on the SSD through a reinstall and taken back by the recovery path) is rebuilt instead of failing later.
- **A runtime NekoServer deployed itself is replaced when it was built again from other sources** (a `git pull` into the same folder within one version). Each deployed copy now has its build record beside it (`nekoserver-runtime.build.json`); a copy without one (someone's own, or from a release before this one) is still never overwritten.
- The on-PC build was tried on Arch, openSUSE Leap and AlmaLinux as well (see below), after Ubuntu, Fedora and Debian in 0.37.1.
- Small things around the build and the bootstrap: when the runtime is built as the person who ran sudo, anything an earlier build left as root (`runtime/target`, `runtime/bin`) is handed to them first, instead of failing on permissions; pacman runs without its progress bars in `install.sh` (its output goes through the retry's log, where the bars came out garbled); the setup window opened by a Chromium-family browser is placed from the screen's real size (`xrandr`), low on the screen and always wholly on it, and left to the desktop where the size is unknown (Wayland). The tests no longer print the home-network and qBittorrent lines into the test log.

### Upgrade notes
- `git pull` renames the folder to `nekoserver`; run `sudo bash install.sh` there. Your `my-config.json` is still in the old `nekoserver-v0.37.x` folder: setup finds it and asks `Use them for this release (yes when updating)?`; answer `yes`. The old folder can be deleted afterwards. From now on updates stay in the same folder.
- The runtime is built for this release (a few minutes).

## v0.37.3 — 2026-10-24

Code-quality findings from SonarCloud's review of 0.37.1, and the CI fixed for a checkout without the built runtime. Nothing changes in how NekoServer works. (0.37.2 was the same work before the CI fix; it was never merged.)

### Changed
- The wizard's `run` has one return point; a build test keeps one call inside its failure check; an unused name in `runtime/build.py` is gone.
- Two tests that need a built runtime (the preserved installer bundle, the generated runtime commands) skip themselves where none is built instead of failing on a fresh checkout; the CI's Python job builds the runtime first, as a server has after `install.sh`. The recovery step says plainly when the runtime is not built yet.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.37.3`: it offers the settings left in the earlier folder, answers `yes`, and builds the runtime for this release (a few minutes).

## v0.37.1 — 2026-10-24

The runtime is now built on the PC that installs NekoServer instead of being kept in git, plus code-quality findings from SonarCloud's review of 0.37.0 and a fuller `.gitignore`. The setup window is unchanged.

### Changed
- **The Rust runtime is built on your PC, not kept in git.** The repository now holds the runtime's sources alone; the 4 MB executable and its build record (`runtime/bin/`, `runtime/build.json`) are no longer tracked. `sudo bash install.sh` builds the runtime when the release folder has none yet (the first run, and after each update): it installs Rust and a C compiler from your Linux's own packages, then runs `runtime/build.py`, which takes a few minutes. The build is for your PC itself (its own C library); `build.py` still makes the static release build when the toolchain has the musl target and rust-lld, as the CI does. The CI builds the runtime both ways on every change (with the oldest Rust the sources allow, 1.85, and with the pinned release toolchain) but commits nothing.
- The setup window's longer functions are split into smaller ones (the wizard's "Ready to start?" and its finish, the window's state and request handling, the answer wait); its page marks hidden parts without `!important`, uses `<output>` for the notice and a labelled answer box, and never leaves a promise unhandled.
- `.gitignore` covers Python and Rust build output, private settings and keys, what setup and the test rig leave behind (logs, state, moved-aside copies) and editor files.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.37.1`: it offers the settings left in the earlier folder; answer `yes`.
- The bundled runtime was rebuilt for this release (version number only).

## v0.37.0 — 2026-10-24

A setup window: `sudo bash install.sh` now opens a small page in the PC's browser, in the dashboard's look, that explains what is being set up and why, lets you leave out the optional parts, and asks every question setup asks so you can answer there or in the terminal. Tried end to end on Ubuntu 24.04 under WSL (the page followed a real run, showed its questions with their context, and an answer given in the page reached setup).

### Added
- **A setup window beside the terminal.** `sudo bash install.sh` now opens a small window in the PC's own browser (the dashboard's look; Chromium-family browsers open it as a window of its own sized for the bottom of the screen, Firefox as a normal window). Before setup starts it explains the optional parts (a TV, phone alerts, outage alerts, Nextcloud) in plain words, with what each installs, and lets you leave any of them out; **Start setup** there answers the terminal's "Ready to start?". While setup runs it shows the step under way with a one-line explanation, the packages it installs with what each one is for, the step's own messages and the state of every step. Every question setup asks is asked in the window too, with the terminal's last lines as context (the numbered drives, the instructions for a phone step): a text box, Yes/No buttons or a password box, and the answer can be given there or typed in the terminal, whichever comes first. At the end it shows the finishing screen. The page is served by setup itself on this PC only, behind an address nobody can guess, only while setup runs, and loads nothing from the internet. Without a desktop or a browser, setup prints the window's address and goes on in the terminal alone, as before.

### Changed
- SonarCloud's findings on 0.36.1: the retry's queries bind the package name per iteration, and two tests use one call inside their failure check.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.37.0`: it offers the settings left in the earlier folder; answer `yes`. The setup window opens on its own on a PC with a desktop.
- The bundled runtime was rebuilt for this release (version number only).

## v0.36.1 — 2026-10-24

The TV extras on every family that packages Steam, a retry when a package source does not answer, the fix for the test that failed on GitHub's runner in 0.36.0's CI, and SonarCloud's findings on 0.36.0. The TV step was tried for real on Fedora 44, Ubuntu 24.04 and Debian 13 under WSL (Steam installed from each system's own repositories after doing what the hint says), and the two systems without a Steam package were seen to say so.

### Added
- **The TV extras on every family with a Steam package.** The TV step (Steam Big Picture, a browser shortcut for YouTube, Bluetooth) was Arch/Nyarch-only; it now installs from the system's own repositories on Debian, Ubuntu, Fedora and openSUSE Tumbleweed/Slowroll too, with each family's names (`steam-installer` and Mesa's one Vulkan package with its 32-bit twin on Debian and Ubuntu, `mesa-vulkan-drivers.i686` on Fedora, `libvulkan_*-32bit` and `MozillaFirefox` on openSUSE; `bluetoothctl` comes inside `bluez` there). Steam lives in a part of the repositories that is off by default; NekoServer never switches it on itself: when the packages are not offered, the step stops and the log gives the exact command for this system (Arch's multilib; `dpkg --add-architecture i386` with contrib/non-free or multiverse; Fedora's `rpmfusion-nonfree-steam` repository from `fedora-workstation-repositories`; openSUSE's non-OSS repository), then setup is run again. RHEL and its rebuilds and openSUSE Leap 16 have no Steam package: the step reports itself unavailable there and names Flatpak as the way. Steam's shortcut and the guided handoff open it from `/usr/games` on Debian and Ubuntu; the YouTube shortcut uses the browser the PC already has, and Firefox is installed only where there is none.
- **A package source that does not answer is asked again.** A mirror hiccup, a download cut off or the network still coming up used to fail setup at once, on the first `apt-get update`, `dnf makecache`, `zypper refresh` or `pacman -Syu` as much as on an install later. Both `install.sh` and the installer now run the same command again after 20 seconds, three times in all, and say so; any other failure (a missing package, a conflict, a broken repository file) is still reported at once.

### Fixed
- **The runtime's fan-detection tests no longer depend on a fast machine.** Four of them slept 300 ms and then expected detection to have taken the first fan; on a slow runner (GitHub's) one failed. They wait for it now.

### Changed
- Code-quality findings from SonarCloud on 0.36.0: Samba's unit name is written once, `install.sh` lists the tools every family installs once, and two tests use unittest's own forms.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.36.1`: it offers the settings left in the earlier folder; answer `yes`.
- The bundled runtime was rebuilt for this release (its tests and the version number; nothing changes in what it does).
- A TV set up on Arch before keeps its shortcuts as they are. To add the TV extras on Debian, Ubuntu, Fedora or openSUSE Tumbleweed, set `"wanted": true` under `"tv"` in `my-config.json` (or answer yes when setup asks) and follow the hint about Steam's repository if setup gives one.

## v0.36.0 — 2026-10-24

openSUSE support, installed for real on Leap 16 under WSL with its own three virtual drives: setup reached 26 of 28 steps (the other two need the owner's Tailscale account and phone), every feature check passed (47) and the fault suite ran on it. Also a standing dashboard warning when the monthly backups are unencrypted, and a fix for a Docker version openSUSE's package reports.

### Added
- **openSUSE Leap 16 and newer, Tumbleweed and Slowroll.** `install.sh` and setup recognise them (anything naming opensuse in its `/etc/os-release`; SLES is not covered) and install with zypper. Everything comes from openSUSE's own repositories, Docker, Compose, Caddy and Tailscale included, so no maker's repository is written. zypper is told to wait for another zypper or YaST instead of failing at once. Leap 15 is refused before anything changes: its Python (3.6) is older than NekoServer needs. When the freshly installed Python cannot load parts of itself because the system's libraries are older than it (openSUSE's packages name a library, not its version; seen on a Leap 16 not updated for a while, where `xml.etree` failed on an old libexpat), setup brings the installed packages up to date (`zypper update`; `zypper dist-upgrade` on the rolling Tumbleweed and Slowroll) and goes on. Tried on Leap 16.0 under WSL.

### Changed
- **The dashboard says when the monthly backups are unencrypted.** When no backup password was chosen at setup, each monthly snapshot holds the app password files in plain text; until now only the monthly job's log said so. The dashboard now shows a standing warning, with the command that fixes it (`sudo python3 backup_secrets.py --setup`), as long as the monthly schedule exists without a backup key.

### Fixed
- **The recovery checkpoint takes a Docker version with a package suffix.** openSUSE's Docker calls itself `29.7.2_ce`; the checkpoint setup records for a later reattach refused that as not a version, so setup listed the checkpoint under "Still needs you" for good. It now reads the version by its leading numbers, as the reattach itself does, and when it refuses a Docker it says what it found (driver, store, version).

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.36.0`: it offers the settings left in the earlier folder; answer `yes`.
- The bundled runtime was rebuilt for this release (the checkpoint's version check and the version number).
- If your monthly backups were set up without a backup password, the dashboard now shows a warning until you choose one (`sudo python3 backup_secrets.py --setup` in the release folder). Nothing else changes.

## v0.35.1 — 2026-10-24

Code-quality findings from SonarCloud's review of 0.35.0. Nothing changes in how NekoServer works or what it says.

### Changed
- Names used in several places (Tailscale's service, the recovery tool, the settings template) are each written once.
- The home gateway's readiness check is split into smaller parts: checking that Jellyfin and Seerr finished their first-run setup, and checking the apps' containers.
- Tests: each test that expects a failure now runs only the call that should fail inside its check, and a set comparison and a mock use unittest's own forms.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.35.1`: it offers the settings left in the earlier folder; answer `yes`.
- The bundled runtime was rebuilt for this release (version number only).

## v0.35.0 — 2026-10-24

Fedora and RHEL support, and every supported Linux installed and broken on purpose for real: a fresh install on Arch, Ubuntu 24.04, Fedora 44 and AlmaLinux 10, Debian 13 updated from 0.30.0 the way `git pull` leaves it, and the fault suite on them (all under WSL, each with its own three virtual drives). WSL has no SELinux, so SELinux is the part still to be tried on a real Fedora or RHEL: see below. Versions 0.31.0 to 0.34.0 were not released.

### Added
- **Fedora 42 and newer.** `install.sh` and setup recognise Fedora and install with dnf. Docker CE and Tailscale come from their makers' repositories (NekoServer writes the repository definitions itself; dnf checks the packages with the makers' keys), Caddy, Samba and the tools from Fedora's own. Fedora's own container packages (`moby-engine`, `podman-docker`, `containerd`) are never mixed in: setup stops and says how to remove them. New services are kept from starting while they install and stay switched off until their own steps start them on the right drive, as on Debian. Samba's untouched example `smb.conf` is recognised by rpm's record of it and set aside. Only the package lists are refreshed (`dnf makecache`); upgrading Fedora stays your own routine. Reattaching the apps after reinstalling Linux ([RECOVERY.md](nekoserver-v0.35.0/RECOVERY.md)) is allowed on Fedora too, but has not been tried there yet.
- **RHEL 10 and its rebuilds: Rocky Linux, AlmaLinux, CentOS Stream.** `install.sh` and setup recognise them (anything naming rhel in its `/etc/os-release`) and install with dnf, as on Fedora:
  - Docker's packages come from Docker's repository for RHEL (RHEL itself) or for CentOS (the rebuilds); Tailscale's from Tailscale's repository for RHEL.
  - Caddy (the home gateway) and qrencode come from **EPEL**, Fedora's extra packages for these systems. Setup adds EPEL when it isn't there yet: from the system's own repositories (Rocky, AlmaLinux, CentOS Stream), or from EPEL's site on RHEL itself.
  - `runc` and `podman-docker`, which clash with Docker's packages, must be removed first: setup says so. Podman itself can stay.
  - SELinux and firewalld are handled as on Fedora.
  - Version 9 is refused before anything changes: its Python (3.9) is older than NekoServer needs. Oracle Linux is not covered.
  - Tried on AlmaLinux 10.2 under WSL: recognised by itself, it installed what it lacked (openssl, pciutils, EPEL), reached 26 of 28 steps (the other two need the owner's Tailscale account and phone), passed every feature check (45) and the whole fault suite (30: the monthly update and the nightly backup cut off at each stage, a crash, Docker restarting, the USB drive and the HDD unplugged, the HDD full). Moving the server to a reinstalled Linux was tried there too: a fresh AlmaLinux took the drives over as RECOVERY.md describes, the apps' Docker store was reattached, and every feature check passed (45). RHEL itself and Rocky Linux have not been tried yet.
- **SELinux.** NekoServer is not tested with SELinux yet, and Fedora and RHEL have it enforcing by default: setup stops at its system check while SELinux is enforcing, says what may be refused (the shared folders, the home gateway, the backup jobs), how to switch to permissive mode to try it, and which command's output to send (`sudo ausearch -m AVC -ts recent`) so the right rules can be added.
- **Memory is part of the system check.** It shows how much the PC has, and below 8 GB notes that the apps use about 2 GB when idle (measured on a full install), and more while Immich looks at new photos or Jellyfin converts a video, so with less those can be stopped for lack of memory. A note only: setup goes on. The README lists 8 GB under what you need.
- **Image-based systems are turned away before anything changes.** Fedora Silverblue and Kinoite, and SteamOS, call themselves Fedora or Arch, but keep their system files read-only, so installing NekoServer's packages would fail halfway. `install.sh` now says so and stops before the package manager runs.

### Changed
- The firewall note gives a `firewall-cmd` example where firewalld is on (Fedora), and the `ufw` one elsewhere.

### Fixed (every Linux)
- **Updating with `git pull` keeps your settings.** Each release has a folder named for its version, and `my-config.json` (git does not keep it) stayed in the earlier one: setup run from the new folder made a new settings file and started over, asking for the drives and passwords of a server already set up. Setup now finds the earlier settings, in the earlier release's folder or in the copy it keeps on the SSD, and asks `Use them for this release (yes when updating)?`; recovery (`--recover`) reads them too. The README now says how to update.
- **Nextcloud's first install is finished when the image gave up.** Nextcloud's image installs it on its first start only, retrying for about two minutes; with its database out of reach that long, every later start skipped the install, Nextcloud said "not installed" for good and the accounts step reported a login that could not match. After starting Nextcloud, setup now checks it and, once the image's own attempt is over, finishes the install with the same settings (taken from the container, so no password is on a command line).
- **The nightly settings backup no longer fails while OpenRGB writes its log.** It kept OpenRGB's whole folder, logs included, and OpenRGB writes a log while it runs: a backup that copied it as it changed (right after setup restarted OpenRGB, say) was refused as a whole with "Host inventory changed during snapshot", and nothing said which file. It now keeps OpenRGB's settings only (`OpenRGB.json` and `sizes.ors`), and the message names up to three of the files that changed.
- **A new drive with nothing on it is named, not called missing.** When no drive could be used for the SSD or HDD, setup explained drives formatted for Windows and drives in use, but about a brand-new, unformatted one it said "No other drive was found: check that it is connected". It now names that drive and says to format it as ext4 first.
- **An app being stopped when the monthly update or the nightly backup was cut off starts again.** When the job was stopped or crashed just as it stopped the apps (seen on Ubuntu: Sonarr), Docker was still finishing that app's stop when the tidy-up service started the apps again. The stop ended afterwards, and the app stayed down until the next restart, failing every later monthly run with "Managed application is not in a healthy running baseline". The tidy-up now lets each stop finish, then starts the apps. A second tidy-up started meanwhile (the one at the next start, say) waits for the first instead of failing with "Job already running or unsafe lock".
- **A nightly backup missed while the PC was off no longer fails when it starts.** The PC catches the missed backup up as it starts, at the same moment as the service that tidies up after a cut-off backup. The two share a lock, so the app backup failed ("Job already running or unsafe lock") and the dashboard showed it failed until the next night. The nightly app and settings backups now wait for that service. Tried on Fedora: the PC was switched off with both backups due, then on; both ran after it and succeeded.
- **Setup run during the monthly update says the update is running.** It said the update "did not finish" and told you to start the service that finishes a cut-off one, while the update was still working. It now says the update is running right now, and to wait for it to finish.
- **A drive check that fails names the drive.** The backups, the home gateway and the app jobs check the SSD and HDD before they start; when one was wrong or missing they said only "Backup storage identity mismatch". They now say which folder should hold which drive (its UUID and filesystem) and what was found there instead, or that nothing is mounted.
- **Docker installed as a snap is noticed.** Ubuntu offers Docker as a snap too. Tried on Ubuntu: installed next to a running server, its Docker took over the socket every docker command uses, so `docker ps` showed none of the apps (they kept running) and the backups, the monthly update and setup would have reached the wrong Docker. Setup now stops before installing Docker's packages beside it, and at the Docker step and the home gateway. It says what it found and how to undo it: remove the snap, restart Docker (the socket is gone until then), run setup again.
- **The recovery commands work as written.** RECOVERY.md and setup's messages said to `cd /srv/nekoserver/installer/releases/v0.30.0` and then run `sudo python3 recover.py …`. That folder is root's alone, so the `cd` failed for a normal user, and the command then ran whatever `recover.py` was in the current folder: in your own release folder, one that reads the blank template settings ("data_ssd: no configured UUID"). Every command now names the tool on the SSD by its full path. The settings copy taken from the SSD with `sudo cp` was root's alone; the steps now make it yours to edit (`sudo chown`), as setup leaves it. Found by reinstalling Linux (AlmaLinux) and taking the apps back as RECOVERY.md describes.
- **Another program on an app's port is named.** With something else already on an app's port (a Jellyfin installed beside NekoServer's, say), setup said only "Media projects failed: ['jellyfin']", and the home gateway "Application onboarding could not be verified". Setup now checks each app's ports before starting it and says which program holds which one (or which Docker container); the apps step lists each app's reason, and the gateway names the app that did not answer. Tried on AlmaLinux.
- **An app left without its network is put right.** That failed start (a port in use) left Docker starting Jellyfin with no network at all, so it ran, but nothing could reach it, even after the port was free again and setup ran. Setup now notices an app's container running without its network and recreates it. Tried on AlmaLinux: Jellyfin answered again.
- **The home gateway names the containers it could not verify** instead of saying only that the selected containers must use the verified mounts, networks and ports.
- Enabling the weekly photo timers no longer prints systemd's "Created symlink" notes on the screen.

### Fixed (Debian / Ubuntu)
- **Setup waits while the system refreshes its package lists.** Debian and Ubuntu refresh their lists themselves now and then (often just after the PC starts). Setup started then stopped at once with "Could not get lock /var/lib/apt/lists/lock", because apt waits for dpkg's lock but not for that one. It now waits for the refresh to finish, up to 10 minutes. Tried on Ubuntu with the lock held as setup started: it waited, then finished as usual.

### Fixed (Arch)
- **Setup waits for another pacman, and finishes an update cut off by a restart.** With pacman's lock in place (an updater at work, or an update cut off by a power cut), setup stopped at once with pacman's own "unable to lock database". It now waits up to 10 minutes for another pacman to finish. A lock older than the last restart, which nothing can still hold, is removed, and the system update that follows finishes the one that was cut off. A lock that stays is explained, with the command to remove it. Tried on Arch both ways: a lock dated before the last restart was removed and the update ran; a lock held while setup started was waited for until it went.

### Upgrade notes
- After `git pull`, run `sudo bash install.sh` in `nekoserver-v0.35.0`: it offers the settings left in `nekoserver-v0.30.0`; answer `yes`.
- The bundled runtime was rebuilt for this release.
- Nothing else to do on Arch, Debian and Ubuntu.

## v0.30.0 — 2026-10-23

Deliberate failures on the installed Debian and Arch servers: the monthly update and the backups cut off as by a power cut (also while the new app versions start), a drive unplugged and plugged back, the HDD filled up, and Linux reinstalled with the drives put back. What they found, and fewer backups kept:

### Added
- **The server comes back by itself when a drive returns.** When the SSD or HDD disappeared while the server ran (unplugged, a loose cable, a USB case that reset), Docker and every app stopped, as they must, so nothing is written in the drive's place. But when the drive came back, a dead mount stayed behind and everything stayed down until someone restarted the PC. A new service (*nekoserver-drive-return*, installed by the server-OS step) now runs whenever either drive appears: it releases the dead mount, mounts the drive again, checks that both drives are the right ones, and starts Docker, the shared folders and the home gateway again. The dashboard's "drive unavailable" alert says so.

### Changed
- **Fewer backups are kept: 7 nightly, 4 weekly, 3 monthly.** The nightly app and settings backups keep the newest 7 (was 14) and the weekly Immich database copies the newest 4 on the SSD and the HDD (was 8; the USB drive already kept 4); the monthly snapshots stay at 3. A settings file made from an earlier template still holds 14 and 8 without anyone having chosen them: setup moves those to 7 and 4 (and says so in its log), so older backups beyond the new counts are deleted after the next successful runs. A number you set yourself is kept. To keep more, set `keep_snapshots` under `app_backup` or `host_backup`, or `database_keep` under `photos`, in `my-config.json`.
- **Unfinished monthly snapshots are deleted too, once newer ones replace them.** A monthly run cut off by a power cut or a restart (or one whose restore test failed) leaves a snapshot of nearly full size on the HDD, and retention never deleted those, so each such night took its share of the HDD for good. Now one is deleted once `keep_snapshots` (3) complete snapshots newer than it exist, just as a complete snapshot of its age would be. Newer unfinished ones, the run in progress and folders the worker did not create are kept, as before. When the HDD is too full for a monthly run, its message lists the unfinished ones still kept, with their size.

### Fixed (every Linux)
- **A media drive in a USB case, or an SSD, no longer leaves setup waiting on you.** The HDD power-saving service sets only internal spinning SATA disks, and failed on every other drive at every start: setup listed "No-sleep and log limits" under *Still needs you* for good, and the dashboard showed "Drive power settings has stopped with an error", with nothing anyone could do about it. A drive in a USB case, an SSD, a virtual disk or an encrypted, LVM or RAID device now keeps its own power settings: the service ends normally and says why, and setup shows that line and goes on. A drive that refuses the setting is still reported.
- **Docker keeps its start guard when an earlier release is tidied away.** After an upgrade, setup removes the runtime copies of earlier releases that no service uses any more. It never looked inside drop-in folders, where Docker's start guard is named (`docker.service.d`), so a setup run without the Docker step (only the shared folders, say) right after downloading a new release could remove the guard Docker starts with: after the next restart Docker, and every app with it, would not start. Drop-ins are read now. The tidy-up also ran only when every step had passed, so a server with a step still pending (Tailscale not signed in, say) kept every earlier release's files for good; it runs after every setup run now, still removing only what nothing names.
- **A second restore test names the first one.** A restore test is left running for you to check; running another on the same port said only "Restore-test port is already in use". It now names the earlier test, gives the exact command that stops it, and mentions `--port`.
- **A run cut off by a power cut says so.** After the resume service had put things right, the dashboard showed the monthly job, the nightly app backup or the settings backup as failed with no reason. Each now says it was cut off and that what it had stopped was started again; the backups add that the earlier backups are kept, and the monthly job says at which point it was cut off (taking the snapshot, testing it, installing the updates), that the apps run their earlier versions, and that the next run tries again.
- **Setup run again after a monthly update starts the apps.** The monthly update pins each app to the exact image it tested (by ID), and since 0.28.11 setup downloads an app's images before starting it; an image cannot be downloaded by its ID, so every later setup run failed the media, photos and Nextcloud steps with "pull access denied for sha256, repository does not exist". Setup now downloads only images that have a name, checks that a pinned image is still on the PC, and says how to get it back if not.
- **Setup on a reinstalled Linux says how to take the apps back.** With the SSD of an earlier install put back after reinstalling Linux, setup stopped at the Docker step with "...is not empty; refusing to adopt existing Docker data without migration verification" and nothing about what to do. It now says the SSD holds an earlier install's apps and names the reattach steps in RECOVERY.md and their command. RECOVERY.md named a `recovery-config.json` that nothing creates; it now names the settings file and the copy setup keeps on the SSD. Reattaching no longer says it supports Arch only: Debian and Ubuntu were already allowed. And the Docker version that reattaching requires is now kept current by the nightly app backup: it was recorded only when setup ran, while the OS's own updates move Docker on, so after a reinstall the steps could ask for a Docker version the server had stopped using months before. A newer Docker than the recorded one is now accepted too, as a freshly installed Linux usually brings one (the OS's own updates move Docker on over the same store all the time); an older one is still refused, and after the takeover the record names the Docker now running.
- **The home gateway keeps its certificates when Linux is reinstalled.** Tried end to end: the installed server was moved to a freshly reset Debian with the same drives. The gateway refused its own certificates on the SSD ("Persistent TLS-state owner differs; preserve files and plan recovery manually"), because the new Linux's caddy account had another number. Setup now hands them to it when NekoServer itself gave them the old owner and no account on this PC has that number, so phones that trusted the server keep doing so; any other owner is still left alone, and the message names it and the command to use. RECOVERY.md also says how to read the settings copy on the SSD without writing to it, and what a reinstall asks for again (dashboard PIN, Samba and backup passwords, Tailscale).
- **The app-accounts step waits for Docker.** With Docker not running (on a reinstalled Linux, before the reattach), it asked to finish each of the 11 apps by hand, which could not succeed, and the next run asked again for each one. It now waits for the Docker step, like the app steps.
- **Backups made while the clock was wrong no longer crowd out the real ones.** Backups are named after the time they were made, and retention keeps the newest by name. One made while the server's clock ran a day or more ahead looked newest until the clock caught up, possibly years later: the nightly backups kept only the one just made besides it, and the monthly snapshots lost their history the same way. Such a backup now counts as the oldest (it was made before the clock was put right), and the log names it and says to check the clock.
- **A full drive says so on the dashboard.** From 97% the alert read "Media HDD is filling up: Review free space before uploads or backups", even with the drive completely full. It now says the drive is almost full, what stops (on the HDD downloads, the nightly backups and the monthly snapshot; on the SSD the apps themselves) and how to free space.
- **The nightly settings backup names the drive and the space it lacks.** A full HDD failed it with "Insufficient snapshot space"; it now names the HDD, the free and the needed gigabytes, and the reserve setting (`host_backup.minimum_free_gb`).
- **The shared-folders step shows why Samba did not start.** When `systemctl enable --now` failed, the step's message was systemd's "Created symlink …" notice; it now shows the failure itself ("Job for smb.service failed …").

### Fixed (Debian / Ubuntu)
- **The nightly settings backup runs on Debian and Ubuntu: winbind is switched off.** Their `samba` package also installs and starts `winbind`, which NekoServer's standalone shares never use, and the settings backup refused to run beside it ("Writer active or process probe failed"). Setup now keeps winbind from starting when it installs Samba, and the shared-folders step switches it (and nmbd) off on a server already set up, once Samba's settings are NekoServer's own. The backup's refusal names the program it found and how to stop it.
- **The nightly settings backup works on Debian and Ubuntu.** Two of the system files it keeps, `/etc/vconsole.conf` and `/etc/default/locale`, are links there, and a linked file was refused ("Symlink path refused"), which failed the whole backup every night. A linked file is now kept as a link, as links inside a folder already were; and the refusal, where it still applies, names the path.
- **The dashboard lists Samba once on Debian and Ubuntu.** There `smb.service` is another name for `smbd.service`, and the dashboard listed (and alerted about) the shared folders twice.

### Fixed (from code review)
- The Docker version check reads version numbers with a bounded pattern, so a long run of digits can no longer make it backtrack for a long time; the Samba service-state parsing uses a dict comprehension.

### Upgrade notes
- The bundled runtime was rebuilt for this release.
- Run `sudo bash install.sh` once after upgrading: the server-OS step installs the new drive-return service, the shared-folders step switches winbind off on Debian and Ubuntu, and the backup counts still at the earlier defaults (14 nightly, 8 weekly database copies) move to 7 and 4.

## v0.29.0 — 2026-10-23

First run of the installer on real Debian 13 and Arch systems (under WSL, with three loop-device drives). What it found:

### Fixed (every Linux)
- **The Tailscale sign-in link is shown.** The link comes from Tailscale's control server, which takes a few seconds on the first contact; setup waited 3 seconds and then said "No authorization link available. Run sudo tailscale up in another terminal window". It now waits up to 15 seconds, so the sign-in page opens as the step says.
- **A busy image registry no longer fails the app steps.** Setup downloads the images of a dozen apps in a row, and the registry answered some with "toomanyrequests" (it limits requests from one address), which failed the media step (Prowlarr) and the photos step (Immich) on the first run. A download refused for that reason, or cut off by the connection, is now tried again after a wait (30 s, then 1, 2, 4 and 8 minutes), and the log says so, with the registry's own reason. Other download failures still stop the step as before.
- **Sonarr, Radarr, Lidarr and Prowlarr get their account on the first run.** Setup gave each its sign-in settings and login, then checked the login at once; the app applies new sign-in settings only after a restart, so the check found no sign-in page and reported "not verified (identity/administrator check did not match)", and the next step asked you to finish every one of them by hand. Setup now restarts the app after giving it the login and waits for it to answer before checking.
- **Sonarr, Radarr, Lidarr and Prowlarr logins are verified.** After signing in with the saved login (which worked), setup proved the session with an API call; since Sonarr v4 the API wants its key even with a signed-in session, so every check ended "not verified (HTTP 401)" and the login step asked you to finish each app by hand. The session is now proved on the page the app itself loads after sign-in.
- **qBittorrent gets its account.** qBittorrent 5 answers a good login with an empty reply instead of "Ok.", so setup took every login for a failure, including the one with the temporary password from the app's log, and gave up ("identity/administrator check did not match"). Setup now goes by the session cookie the app sets.
- **Seerr gets its account, also when setup is run again.** Two things stopped it: once an earlier run had linked Seerr to Jellyfin, every later run sent the Jellyfin address again and Seerr refused it ("Jellyfin hostname already configured", HTTP 500); and Seerr 3.5 no longer gives the email in its sign-in answers, so setup took its own login for someone else's ("identity/administrator check did not match"). A linked Seerr is now signed into with the Jellyfin login alone, and the signed-in user is checked on Seerr's own account page.
- **Connecting the apps no longer waits three minutes for apps that are up.** The onboarding probe follows no redirects (since 0.28.8), but took an app's redirect to its sign-in page for "not answering", so it waited the full `onboarding.wait_seconds`, then reported every app but qBittorrent as "Startup wait ended; still unavailable" and `http:` needs_setup. A redirect, or a request to sign in, now counts as the app being up.
- **Prowlarr's indexer list is read whole.** The connector read at most 3 MB of an app's answer; Prowlarr's indexer catalogue is larger, so choosing indexers ended with "indexers: incomplete (Expecting value: line 108710 ...)". Up to 64 MB is read now, and a larger answer is named as such.
- **The connector signs into qBittorrent 5** (the same empty-reply and cookie-name change as above), so Sonarr, Radarr and Lidarr get their download client instead of "download clients: incomplete (qBittorrent authentication failed)".
- **Sonarr, Radarr and Lidarr get qBittorrent as their download client.** The connector set a field named `category`; the apps call it `tvCategory`, `movieCategory` and `musicCategory`, so every app ended with "unsupported schema field category; needs manual configuration".
- **Setup run again keeps the Prowlarr links it made.** Prowlarr masks the stored API key in its answers, and the connector compared it with the real key, so a second run reported every link as "existing connection differs; review in Prowlarr". The addresses are compared now; the key was checked when the link was made.
- **Jellyfin 12 accepts NekoServer's key again.** Jellyfin 12 no longer reads an API key from the `X-Emby-Token` header alone, so the library check ended "Jellyfin no longer accepts NekoServer's key (it was revoked or replaced)" and Seerr's library check failed with it, although the key was fine. The key is now sent the way Jellyfin 12 wants it (in the MediaBrowser Authorization header).
- **A rebuilt runtime of the same version says what to do.** When a release's runtime or dashboard files were rebuilt after that release was installed (as happened in this repository's history), every step that deploys them stopped with "Existing native runtime differs; custom file preserved". The message now names the file and says how to move the old copy away.
- **The monthly snapshot and update run on a server that is set up.** The runtime's own check of the media apps (the dashboard's "media" readings and the gate before the monthly update) had the same two mistakes as the connector, qBittorrent's category field name and Prowlarr's masked key, so Sonarr, Radarr, Lidarr and Prowlarr always read "needs setup" and the monthly job refused with "Live automated media connections are not ready". Both are read as the connector now reads them.
- **No indexer no longer stops the monthly update.** Choosing an indexer is optional (it needs an account with a provider); without one the apps cannot find requests, but they, their backups and the monthly update work. A missing indexer is now noted in the media report instead of counting as "needs setup" for the app.
- **The recovery restore test can be opened.** The restored Immich ran on a Docker "internal" network, for which Docker publishes no port, so it never answered at its test address (http://127.0.0.1:2284/) and the test failed although the import had worked. The test network is now an ordinary one without IP masquerading: the address works from this PC, and the restored app still cannot reach the internet or the other apps.
- **The recovery restore test gives the restored Immich 10 minutes to answer.** It allowed about 2 to 5 minutes; a freshly imported Immich starting next to every other app took longer, and the test ended "Isolated application did not become HTTP-ready" although the import had completed. The message now names the address and the commands that show what the restored app is doing.
- **Docker is started again after earlier failed starts.** When Docker had failed to start a few times before setup ran (its start guard missing after a drive problem, say), systemd refused the setup's own start with "start of the service was attempted too often" and the Docker step failed although everything was in place. Setup now clears that count before starting Docker.
- **A new release can change how Nextcloud's containers run.** The Nextcloud step compared the whole container definition with the installed one, so any change a release made to it (such as the cron fix below) stopped setup on an existing server with "Nextcloud's settings in my-config.json changed since it was installed". It now compares the settings that feed the definition (image, administrator, trusted hosts, public address, drives); with those unchanged, the new definition is written and applied.
- **The monthly snapshot and update go through with Nextcloud.** Nextcloud's cron container is built from the Apache image, whose stop signal is Apache's graceful SIGWINCH; crond ignores it, so stopping the apps for the monthly snapshot killed the container after 60 seconds (exit code 137) and the update was refused ("Unclean application shutdown"). It is now stopped with SIGTERM, through an init process, and stops at once. Setup run again rewrites the container; the nightly app backup is not affected.
- **The monthly snapshot's restore test can reach the restored apps.** Like the recovery restore test, it ran the restored apps on a Docker "internal" network, for which Docker publishes no port, so the health check ended "Private health port required". The test network is now an ordinary one without IP masquerading: the ports work on this PC, and the restored apps still cannot reach the outside.
- **A monthly run that runs out of night is "done in part", not failed.** When the snapshot and its restore tests took until the end of the night window, the job deferred the app updates (by design) but reported a failure, so the dashboard showed a failed monthly job and an alert went out although every backup was complete. It now records the run as done in part, with the updates named as what was left for the next run.
- **The monthly snapshot's restore test accepts Seerr.** Its probe asked Seerr's status, which makes Seerr ask GitHub whether an update exists; on the test network (no way out) that hangs, and the probe timed out ("curl exited with 28"). The probe now reads Seerr's public settings, which need nothing from outside.
- **The monthly snapshot's restore test accepts qBittorrent.** The restored qBittorrent answers on a port chosen for the test, and qBittorrent refuses a request whose Host header carries another port than its own (401), so the test failed on it ("curl exited with 22"). The probe now names qBittorrent's own port in the Host header.
- **The monthly snapshot's restore test accepts Immich.** The test copy of Immich's definition gained a `volumes: null` entry for its Redis (which has none), and Docker Compose refused the file ("services.redis.volumes must be a array"), so the monthly job failed after taking the snapshot and the apps stayed stopped until the resume service ran. A service without volumes is left as it is.
- **The monthly update names the app that did not stop cleanly.** "Unclean application shutdown; update refused" now says which app and project, whether it was still running, killed after 60 seconds or out of memory, and the command for its log.
- **The monthly update says which drive is short of space and by how much.** "Not enough space for backup and verified recovery" now names the drive, the free and the needed gigabytes, and the reserve setting (`maintenance.minimum_free_gb`).
- **Shared folders and the USB copy wait for the steps that make their folders.** When Docker could not start (so the media and photos steps did not run), the shared-folders step failed with "Share directory missing or unsafe: …/library" and the USB copy with "Archive folder missing", hiding the real cause. Both now say they are waiting for the media or photos step. A share folder you named yourself must still exist, and the message says how to take it out.
- **A chosen drive that is missing says what to do.** "Configured data_ssd UUID not present or not supported" now says which drive is not connected and, for a drive that was replaced, which line to take out of my-config.json so setup asks for the drive anew.
- **A failed command's message is cut at a line, not mid-word** ("rror toomanyrequests").
- **The HDD power-saving service says why it did not start.** "HDD policy rerun failed; inspect the managed service" now carries the service's own reason (for example that only an internal rotating SATA HDD is supported, as on a virtual disk), says that setup goes on without it, and names the log command.
- **The finishing screen never stops on the dashboard PIN file.** Its folder is root-only; shown without root, the screen crashed with "Permission denied". It now says the PIN state is unknown without root.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (its Jellyfin check sends the key the way Jellyfin 12 wants it).

## v0.28.11 — 2026-10-22

### Fixed (every Linux)
- **Downloads during setup say why they failed.** Finding the current Immich or Nextcloud release, and fetching Immich's compose file, ended with a bare "<urlopen error …>" when offline, "HTTP Error 403" when GitHub was limiting requests from the address, or "HTTP Error 404" for a release that is gone. Each now says which it is and what to do (connect to the internet; wait an hour; try later).
- **A slow internet connection no longer fails the app steps.** The apps' images (several GB for Immich) had to download within the 15 to 30 minutes allowed for starting an app, so on a slow connection the step failed with a timeout. The images are now downloaded first, with up to four hours, and the apps started afterwards; a download cut off with Ctrl+C goes on where it stopped next time. The log says when a download is running.
- **The phone setup page could not be made: the steps say so.** When the QR-code page failed to write (no qrencode, a read-only folder), the first phone step still said "A page with QR codes opened on this PC". It now says the addresses are shown app by app instead, and the log says why the page was not written.
- **The apps get three minutes to answer on their first start, not one.** Sonarr, Radarr and Lidarr set up their databases the first time they start, which can take longer than the 60 seconds the account and onboarding steps waited; both then reported "needs setup" for something that would have solved itself. The default wait is now 180 seconds (settable up to 600 in `onboarding.wait_seconds` and `accounts.wait_seconds`); the wait ends as soon as the apps answer.
- **A home-address port already used by another program: setup takes a free one.** The home HTTPS addresses use ports 8442 to 8447; when another program on the server had one, setup stopped with "LAN gateway address/port unavailable". It now takes the next free port from 8450-8499 for that app, says so, and keeps the choice in `"lan_ports"` under `"gateway"` so the address never changes later. The app menu page, the gateway, Nextcloud's trusted address, the finishing screen and the acceptance check all use the chosen port. The Tailscale addresses are not affected (they listen on the Tailscale address only).
- **The dashboard's port already used by another program: setup takes a free one and checks the page answers.** With port 8787 taken, the dashboard service restarted in a loop while setup reported the step as done. Setup now takes the next free port from 8788-8799, writes it to `"port"` under `"dashboard"` and says so; everything that shows the address (the app menu entry, the finishing screen, the Tailscale proxy) follows. After starting, setup waits up to 30 seconds for the page to answer and otherwise fails the step with the command that shows the service's log.
- **No second, manual sign-in check when the logins were just verified.** Before giving the apps their private addresses, setup opened every app in the browser and asked you to sign into each and confirm, although the accounts step had verified every saved login through the apps' own APIs a moment earlier. When every selected app's login is verified, that confirmation is recorded by itself; the manual check stays for logins that are not.
- **Two mounts stacked on one folder no longer confuse setup or the backups.** When a drive folder had been mounted twice (by hand, or a desktop automount on top of NekoServer's), `findmnt` lists both, and setup read the two lines as one answer: the Docker step said "Data root must itself be a mountpoint", the storage step "Wrong filesystem already mounted", and the backup guards refused with "identity mismatch". Every check now looks at the mount in effect (the top one), in the installer and in the backup program alike.
- **A failure while setting up the Tailscale addresses says what failed**, instead of "inspect the pending journal and current Serve routes locally".
- **A server without a desktop skips the TV and desktop-shortcut steps.** On a PC with no graphical session installed, the TV step asked whether a TV is connected and the shortcut step wrote shortcuts nobody could see. Both now report "not available on this system" instead.
- **An app login that could not be verified says why.** The setup log said only "pending; saved credentials preserved, no success claimed". It now gives the reason (the app did not answer, a wrong saved password, and so on), never the password. A wrong saved password for an app that already has an account now says to run `sudo python3 account-setup.py` and choose `c` to enter the real login.

## v0.28.10 — 2026-10-22

### Fixed (every Linux)
- **An app added later gets its login asked before it starts.** `sudo bash install.sh --module media --apply` (or `--module photos`) started a newly added app without choosing its login, so the next setup took the app for one already set up ("Existing app data: enter the current login") although nobody had ever made one. Both steps now choose the logins first, as Nextcloud already did.
- **A new home address from the router is asked again instead of failing.** When the router gave the server another address, LAN folder sharing and the home HTTPS addresses failed on every setup run with "Configured LAN address/subnet is not on an active physical LAN interface". Setup now says the address changed and asks for the home network again (without a terminal, it says to run setup in one).
- **Docker already used on this PC: setup says what to do.** It stopped with "use separate migration script" or "manual migration required", but no such script or guide existed. The message now names the folder Docker uses and points to a new section in STORAGE.md with the steps. Existing Docker data is still never moved or deleted automatically.
- **The Docker recovery checkpoint says why it wasn't saved**, instead of a fixed "Verified classic overlay2 Docker checkpoint still required".
- **Picking the drives: a typo is asked again, and "no drive" says why.** A wrong number, or a mistyped name at "Type sdb1 to confirm", ended the storage step (and with it the whole setup), and the USB backup picker did the same. Both now ask again (Enter still stops). When no drive can be used, setup lists each drive it found and why: formatted NTFS/exFAT/FAT (with how to format it as ext4), or already in use at another folder (with how to unmount it). The system disk is never listed.
- **Opening a setup page never installs a browser.** When Firefox wasn't installed (a Chrome or Chromium user), each sign-in page installed it first; on Ubuntu that is the Firefox snap, a large download in the middle of a step. Pages now open in the browser you already have.
- **The gateway and Tailscale steps say what failed.** "Gateway command failed; inspect local setup and HTTPS permissions" and "systemctl failed; inspect the local service/package status" now name the command and its own error line.
- **A drive you already mount from /etc/fstab: the message says what to do.** "Conflicting existing fstab entry" now shows the line, explains that NekoServer mounts the drive itself (with options that let the PC start when a drive is missing), and says to take the line out and unmount the drive.
- **Docker's or Tailscale's repository key can't be downloaded: the reason is named.** Without internet, setup ended with a bare "<urlopen error …>"; on a brand-new Ubuntu release the makers don't package for yet, with "HTTP Error 404". Setup now says which it is and what to do.
- **Your own smb.conf no longer blocks shared folders.** On a PC where Samba was set up by hand before, setup said only "refusing to alter it. Review manually." It now says how to move the file aside, or how to keep your own shares as well: add `include = /etc/samba/nekoserver.conf` to your `[global]` section, and setup accepts the file as it is.
- **Shared folders and the TV keep their account.** Both took "the person who ran sudo" on every run, so setup run later by another administrator (or with sudo from a second account) silently moved the shares to that person and set the TV up for them. Both now keep the account chosen the first time; `networking.samba_user` and `tv.user` name another on purpose (settings table in README.md).
- **A firewall that is on: the message names the ports.** It said "allow your home network to reach Samba (port 445) and the home address (gateway port)"; it now names ports 8442-8447 and gives the ufw command, and says Tailscale needs no rule.
- **A command that fails inside a step is reported with its own words.** The summary said "Command '[…]' returned non-zero exit status 1"; it now says which command failed and what it printed (or how long it ran before timing out).

### Changed
- **Files of earlier releases are tidied up.** Each release installs its own copy of the runtime in five places on the system disk and one on the SSD, and kept them for good (20 MB per release). When a setup run has finished, copies that no service uses any more are removed, and release bundles older than the two newest go too (Docker's start-guard folder included). The current release, and any folder a service still points at, are never touched.
- Sonar: two exception tests make a single call inside `assertRaises`.

## v0.28.9 — 2026-10-22

### Fixed (every Linux)
- **Setup runs again after the PC's timezone changes.** The media apps' settings files carry the timezone, and setup replaced one only when it matched what the current settings would write. With `"timezone": "auto"` (the default), setting the PC's timezone after setup made every later run fail the media step with "has local edits or different settings". Setup now keeps a checksum of each file it writes (`.neko-written` next to it), so it recognises its own unedited file whatever changed (timezone, a drive, the graphics chip) and replaces it after keeping a copy. Files from earlier releases are recognised when only the timezone differs. A file changed by hand is still left alone, and the message says how to get a fresh one. Changing `media.uid`/`gid` is still refused, now with a clear message: Navidrome and Seerr couldn't read their own settings and music as another user.
- **The drive space check no longer reads every photo when setup runs again.** On a re-run the result is only shown, yet the 4-weekly checksum comparison of the USB copies could read the whole photo archive on both drives (an hour or more on a big library). Re-runs now compare by size and date; a new install still checks in full.
- **Phone alerts: a topic set in my-config.json gets its access token, and can be changed.** With `notifications.endpoint` set in the settings, setup never asked for the token, so a protected topic refused every alert. Changing the topic later failed the step on every run. Setup now asks for the new topic's token and, when alerts already go elsewhere, asks you to confirm the switch. Without a terminal, alerts keep going to the earlier topic and setup says how to switch.
- **A guided setup without Navidrome goes ahead.** Taking Navidrome out of `media.services` made the whole setup refuse to start ("Music setup requires navidrome"). The music step now says there is no music player to set up; the music backup still keeps Lidarr's music.
- **"yes" means yes when adding media sources.** The source questions accepted only `y`: typing `yes` added nothing, and for "Copy this folder…?" it meant no, without a word. Both now take yes/no (Enter = no) and ask again otherwise; Ctrl+D keeps the sources added so far.
- **A typo in the media connector's lists is asked again.** A number outside the list (choosing an indexer or Lidarr's profiles) failed the step, and onboarding then stopped setup. It is now asked again (Enter or Ctrl+D skips). Ctrl+D elsewhere in the connector stops it cleanly instead of with a Python error.
- **A mistyped Jellyfin password during library setup asks you to try again**, instead of failing onboarding with "rerun after checking the service". (Only when saved app accounts are turned off.)
- **Too old a Python says what to do.** On a system based on an older release (Linux Mint 21 is Ubuntu 22.04), install.sh said "bring the host up to date", which doesn't bring Python 3.11. It now names the version found and the releases that have 3.11.

## v0.28.8 — 2026-10-22

### Fixed (every Linux)
- **Nights with Steam open can't delete the last backup of your Steam saves.** Snapshots made while Steam was open (without its files) counted towards `keep_snapshots` like any other, so a few such nights retired the last snapshot that had them. The newest snapshot with Steam's files is now kept until a newer one has them.
- **A restart cut off part-way is finished only when no backup or update needs the apps stopped.** At start, the control service started what an interrupted restart had stopped right away, without the shared lock or the check for an unfinished backup or update. It now waits for both, as a restart from the dashboard does, retrying every 30 seconds. Its record also moved from `/run` to `/etc/nekoserver-control`, so it survives a restart of the computer.
- **Setup no longer says "NekoServer is ready!" when the first checkup found problems.** The wizard ignored the checkup's result. A backup that finished but left something out (Steam open, say) counted as done, and checks still waiting for setup weren't listed. The checkup now reads each backup's own report (done, done in part, or did not finish) and lists every check that didn't pass. The finishing screen and the exit code include it.
- **"Restore newest" never takes a file of its own for an older version.** With a recorded version of `report.txt`, an older-looking `report.neko-20250101T120000Z.txt` saved in its own right was still treated as a version of it, and only one file came back. A dated copy now counts as a version only when the backup recorded it, or its note names it as the current copy. Older unrecorded versions come back as files of their own.
- **The unencrypted password list is checked before it is restored.** Taking it out of a monthly snapshot made without a backup key skipped the snapshot's checksum, so a changed archive gave changed passwords. `data.tar` is now checked against its recorded checksum first.
- **The monthly Nextcloud check only removes its own test files.** A test file left by a cut-off check was cleaned up by name prefix (`neko-maintenance-…`), which could match a file of yours. Each test file's exact name is now written down before it is made. Only those are removed, and only while they still hold what NekoServer wrote.
- **Btrfs drives mount the same tree after a restart.** A Btrfs drive already mounted with a subvolume (`subvol=@data`, say) got an fstab line without it, so a restart mounted the filesystem's default tree instead and the apps' files looked missing. The subvolume is now remembered in the settings (`storage.<drive>.subvolume`), written into fstab, checked after mounting, and part of the drive identity every backup checks.
- **TV setup finishes on KDE and XFCE.** The step asked for a GNOME power setting that other desktops don't have, and stayed "needs setup" although sleep was already blocked system-wide by the server-OS step. The system-wide block now counts on every desktop; GNOME's own setting is still switched off where it exists.
- **Nextcloud on its own gets its password step.** `--module nextcloud` failed with "Complete the account_credentials prompts before Nextcloud setup", because that step was only reached through the full and cloud profiles. Nextcloud now depends on it.
- **Repairing accounts leaves switched-off apps alone.** Running only the accounts step tried every login ever saved, including apps you had since switched off (whose containers are gone), and then asked you to finish setting them up. It now works on the apps set up in the settings; the other logins stay saved for when those apps come back.
- **A drive under /home is refused before anything is installed.** Such a mount passed the settings check, but NekoServer's services (dashboard, gateway, alerts) run with systemd's ProtectHome and can't see `/home`, `/root` or `/run/user`. The message now says to mount it under `/srv` or `/mnt`.
- **The HDD power setting works on a Btrfs drive.** For a Btrfs subvolume, the mount source reads `/dev/sdb1[/media]`; the drive-policy service took the whole text for a device name and refused it ("Simple physical device path required"), leaving the server-OS step unfinished. The device is now taken from before the bracket; the identity checks stay.
- **A PIN typed in another script's digits asks again instead of failing the step.** Setup accepted digits such as `١٢٣٤٥٦` (Python counts them as digits), the control service refused them, and the dashboard step failed. Setup now takes 0-9 only, and a PIN the service refuses is asked again.
- **Setup on a server that is already set up:**
  - **Shared folders follow your settings.** A share added, Immich switched on, write access, or another account made the Samba step fail for good with "Existing Samba config differs". NekoServer's own share file is now rewritten; only a file someone else wrote, or widening Tailscale-only sharing to the home network (confirm with `"allow_tailnet_to_lan": true`), stops setup.
  - **`my-config.json` stays yours to edit.** The first setup run saved it as root's (readable by root only), though the guide says to edit it and run setup again. It keeps its owner now.
  - **Changing `data_mount` works.** The shipped settings also carried `"persistent_root"` under `"docker"`, which had to match, so the edit the guide invites made setup refuse to start. The line is gone from the template (Docker always uses the app SSD), and an old settings file with it gets a message that says what to do.
  - **The gateway list follows the apps set up.** Left as shipped, `"services"` under `"gateway"` names every app, so a server without Immich or Nextcloud failed both gateway steps. The shipped list now follows your apps; a list you edited is used as it is, and an app in it that isn't set up is named.
  - **A Tailscale address failure says why.** With MagicDNS and HTTPS already on, a failure to set up the addresses (an app not running, say) sent you to the DNS settings page over and over without the reason. The reason is reported, and the step waits for you. Checks in every guided step now show the error they hit.
- **Setup messages and prompts:**
  - **Docker Compose failures say why.** Immich, Nextcloud and the server-OS step reported only "exit status 1"; the command's own message is in the log now.
  - **A mistyped Samba password is asked again** (up to 3 times) instead of failing the shared-folders step.
  - **A wrong number at the home-network question is asked again**; three wrong ones (or Ctrl+D) leave the choice for next time.
  - **Enter at the USB-drive question skips it, as offered**, instead of failing the step; Ctrl+D does the same.
  - **Choosing no backup key (three mismatches) still installs the monthly update**; its snapshots keep the password files unencrypted until a key exists, as the message says.
  - **Ctrl+D at any question** is recorded with a reason instead of a blank one; Ctrl+C while packages install ends with a short message, not a traceback.
  - The timezone message says when the PC's timezone couldn't be read and UTC is used; the dry-run messages show the configured backup times; "Docker: not installed yet" is no longer shown when Docker is installed but not started; an NVIDIA-only PC without `lspci` gets the NVIDIA explanation; a bundled copy that differs, and a changed Nextcloud setting, say what to do.
- **Timezone from `/etc/timezone` too.** On Debian and Ubuntu images where `/etc/localtime` is a copied file rather than a link, the PC's timezone couldn't be read and every backup silently ran at UTC times. `/etc/timezone` is read now, and one-word zones such as `Japan` or `CET` are accepted.
- **install.sh:** too old a Debian or Ubuntu (Python 3.11 needs Debian 12 or Ubuntu 24.04) is refused before apt runs, with the release named; free space is measured in MB (df rounded 4.1 GB up to 5); dpkg never asks about an edited settings file; the message after a kernel update no longer says "nothing else was changed"; a network timeout or HTTP error while repairing repositories is reported, not a traceback; the install time limit in the message matches the real one.
- **Health-check probes during onboarding** go to the apps directly (no proxy, no redirects), so a proxy kept by sudo can't make every app look down.
- **The Docker storage guard check** accepts a `data_mount` with a trailing slash; the recovery checkpoint no longer fails on a settings file without `media.uid`.
- **Switching Immich off removes its photo schedules.** Its weekly photo and database backups stayed scheduled and ran against the stopped Immich. They are now stopped and removed (one you wrote yourself is kept). The USB copy of Nextcloud's files keeps its own schedule.

### Changed
- Code tidied after the 0.28.7 review (no change in behaviour):
  - named constants for the photo archive's default folder and the encrypted password list's file name;
  - simpler parsing of Debian dependency entries;
  - plainer code for the graphics-device decision;
  - single-call exception tests.
- Version 0.28.8; the release folder is now `nekoserver-v0.28.8`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.7 — 2026-10-22

### Added
- **First checkup and backup at the end of setup.** Once every step has finished, the wizard runs the app health check and each backup you set up straight away, instead of waiting for the first night (or the first Sunday for the USB copy), then checks the whole server and saves the report for the dashboard. It names any backup that didn't finish (with the command to see its log) and any check that needs a look. Ctrl+C stops it; the backups then run at their usual times.
- **Desktop shortcuts on every Linux** (new step *Desktop shortcuts*): the dashboard, the web browser, Steam (if installed) and each app that was set up, on the desktop and in the app menu. Shortcuts NekoServer made earlier are brought up to date; ones you made or changed are kept. Before, only the TV step (Arch only) made a few.
- **The finishing screen says whether a dashboard PIN is set**, and the welcome screen asks you to have one ready.

### Changed
- **Steam Big Picture no longer opens by itself at login.** The `autostart_big_picture` setting is gone, and the autostart an older version added is removed (one you made yourself is kept). Big Picture is still a shortcut.
- **A new dashboard PIN has 6 to 8 digits** (a 4- or 5-digit one chosen before still works). After five wrong PINs changes lock for 5 minutes, and **each further five lock them twice as long** (up to a day). Before, the 5-minute lock started over each time, so a 4-digit PIN could be guessed in a few days.
- **Close app asks first**, like Force close. It sends Linux's ordinary "please quit" signal: many apps save or ask, but some quit straight away, so unsaved work can be lost. The menu no longer says it is "like closing its window".
- Messages and guides say "terminal" instead of "Konsole": on Ubuntu and Debian it is called Terminal.

### Fixed (every Linux)
- **Drives get 90 seconds to appear at startup** instead of 15. A big hard drive, or one in a USB case, could take longer; the HDD then wasn't mounted, and Docker and every app stayed stopped until someone stepped in. Existing installs get their drive lines updated; your original `/etc/fstab` backup is kept.
- **The dashboard keeps updating when the app SSD can't be written.** A full or read-only SSD made every reading fail, and the page kept showing the last, healthier one. Readings are published anyway, with an urgent "App SSD can't be written" alert.
- **A full drive reads 100%, not 95%.** The space Linux keeps for itself on ext4 (5%) was counted as free, so a drive full for the apps showed 95% and a "filling up" warning, never the urgent alert.
- **A phone whose clock is off no longer sees "old readings".** The page judged how old readings are by the phone's clock; a minute off dimmed live readings, a few minutes off showed every reading as old and stopped browser alerts. It now uses the server's time.
- **A service that has stopped is no longer green.** Docker waiting for a missing drive (or any service that runs all the time) showed "Starts by itself" in green when it wasn't running. It now shows *Not running* and raises an urgent alert; "recovered" is sent only once it runs again.
- **A backup cut off by a power cut no longer shows "Running" for days.** Its report still said running; now the collector sees its service has stopped and shows it as stopped part-way.
- **A health check made after an app started counts.** Docker's "Up 5 minutes" is rounded down, so a failed check from 5½ minutes ago could be discarded as older than the app's start. The exact start time is used now.
- **An app check report dated in the future** (the clock was wrong when it was written) is no longer counted as current.
- **The dashboard's web server restarts itself after a crash** in one of its workers, instead of slowly running out of workers.
- **Setup no longer gives up on the control service while it finishes an app restart.** Setup waited 60 seconds; the service may take up to 10 minutes to stop (its unit now allows 600 s), because it first lets an app restart finish: up to two parts, each up to 2 minutes to stop and 2 to start. If the service is ever stopped by force in the middle of one, what it had stopped is started again when it next starts.
- **exFAT USB drives: names kept from earlier weeks count too.** A new `documents/report.pdf` could still merge with an old `Documents/report.pdf` that stays on the backup (it is add-only). The check now includes the names already on the drive and in its ledger.
- **Repair asks when the HDD and USB ledgers disagree.** It trusted the HDD's checksum, replaced the USB copy and called both verified, though the USB copy then failed its own list. Now nothing changes until you choose which copy is right (or keep both); your choice is recorded in both lists.
- **"Restore newest" never merges two separate files.** A file named like a dated version (`report.neko-20261022T120000Z.txt`) but saved in its own right was taken for a version of `report.txt`, and only one came back. The backup now records which file each dated copy belongs to; older backups count a dated name as a version only beside the note naming the current copy, and not when the record says otherwise.
- **Nextcloud's USB backup no longer needs Immich.** With Nextcloud on and Immich off, setting up the weekly USB copy failed ("Archive folder missing"), and it waited for the photo steps. Nextcloud's files then had no copy off the server, since monthly snapshots leave them out. The USB copy now takes whichever of the two is set up, the photo schedule isn't installed without Immich, and the dashboard calls the job *USB copy (photos and files)*.
- **Monthly snapshots made without a backup key keep the app password list.** The job said the password files stayed (unencrypted) in `data.tar`, but `secrets/accounts` (APP-CREDENTIALS.txt and credentials.json) wasn't in it, and no encrypted archive was written either, so the snapshot had no password list at all. It is in `data.tar` now (root-only, and the manifest says it isn't encrypted), a rollback puts it back, and the recovery wizard's *Restore the password list* also offers these snapshots. With a backup key, nothing changes: the list is kept only in the encrypted archive.
- **Adding an app reaches the backups already installed.** After adding an app (setup run again with `--module media`), the nightly app backup and the monthly update kept working from the settings saved when they were installed, so the new app was left out until they were set up again. Both are now brought up to date after every setup run, like the dashboard's and the host backup's.
- **A damaged backup key is no longer reported as a wrong password.** The key copy and certificate stored in each monthly snapshot had no checksum, so a damaged key passed every check and the restore said "Wrong password". Both now have checksums: the snapshot check and retention catch the damage, and a restore says the key copy is damaged and uses this server's own copy of the same key when it has one.
- **Switching Immich off stops it.** With `"deploy_immich": false` after Immich was set up, it kept running while monitoring, backups and monthly updates left it out. Setup now removes its containers (under the lock backups and updates hold, and not during an unfinished monthly update); its photos, database and settings stay on the SSD.
- **A restore can't go inside a backup.** A destination inside the photo archive, the snapshots or the USB drive (or one around them) was accepted, and the restored files became part of that backup and of every later restore from it. Such a folder is now refused.
- **Jellyfin Quick Sync uses the chip that passed the check.** The device was written to Jellyfin's VA-API setting, which Quick Sync doesn't use. Quick Sync now gets *QSV Device*, VA-API *VA-API Device*.
- **Settings an older NekoServer made get their device.** Immich and Jellyfin set up by an earlier release kept choosing a device by themselves, because setup then reported "kept your setting". A setting NekoServer made (its last report says so) gets the device filled in. One someone else made is kept, but on a server with several chips the step says which device to choose instead of counting as done.
- **A second graphics chip of the same make is tried.** Only the first render device of each vendor was kept, so with two Intel chips the second was never tried when the first failed the video check.
- **Debian / Ubuntu: finishing an interrupted package installation follows Debian's rules for choices.** A dependency like `A (>= 2.0) | B` counted as met by any installed A, even 1.0, so an unfinished B that was needed stayed unfinished. A held package (`hi`) didn't count as installed, so an alternative was set up for nothing. Versions are now compared as dpkg does, an unversioned *Provides* never meets a versioned dependency, and held packages count.
- **Leaving Steam open no longer stops the whole nightly host backup.** With Steam (or its web helper, or Wine) running, the backup refused to run at all, so system and Samba settings weren't saved either, on a PC left on for Steam Remote Play. Now everything else is saved, Steam's own files are left out that night, and the run shows as partial with "Steam was open". Steam is never closed for you.
- **Shared folders work on a server without Immich.** The default shares include *Photos* (the photo archive), which only exists with Immich, so Samba setup failed on a media-only server ("Share directory missing"). The default Photos share is now left out without Immich; a share you added or changed yourself must still exist, as before.
- **Monthly update:**
  - The space check no longer turns down a drive with enough room. It counted every Docker container on the server, also ones that aren't NekoServer's, and the same image again for each container using it (20 containers sharing a 1 GB image counted as 20 GB), then asked the SSD for room for images that are saved on the HDD. Now it counts the selected apps' images once each, and each drive for what goes on it.
  - A finished update is no longer reported as failed when reopening Immich or Nextcloud needed a second try, or a power cut came after it was installed.
  - A rollback no longer unpacks the app data twice (once to the HDD for nothing), and it is never cut off part-way by a time limit: before, a large one could be stopped after 6 hours, leaving the apps stopped.
  - The unpacked copy used by the restore test is removed when the test fails, the rollback stages after a rollback, and the failed update's data that a rollback set aside is removed together with that month's snapshot. Before, each of these stayed for good and slowly filled the drives.
  - Nextcloud's check uses a new test file each try and removes one a cut-off try left behind, so one interrupted check can't make every later one fail.
- **Setup:**
  - **Ctrl+C ends setup cleanly** with a short message, and the step it stopped is recorded as stopped (not left "running"), so the next run starts it again. During recovery it tells you to run recovery again.
  - **A settings file missing a drive setting says which one** (and how to fix it), instead of a bare error.
  - **The first setup log isn't kept twice**: it moves to the SSD once the SSD is mounted.
  - **A typo in the outage-monitor or phone-alert address is asked again** (up to 3 times, with an example of the right form) instead of failing the step; a "/" copied at the end is dropped.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.
- Run `sudo bash install.sh` once: it updates the drive lines in `/etc/fstab`, the control service's stop time, the monthly update's time limits and the desktop shortcuts.

## v0.28.6 — 2026-10-22

### Fixed (every Linux)
- **Closing a desktop app reaches all of its processes.** Some apps put their own processes in a group below the app's own (a browser's helpers, a launcher's children). Those were neither asked to quit nor force-closed, and the app could count as closed while they kept running. Every group below the app's is included now.
- **A program started from another app says so.** Firefox opened from a terminal belongs to the terminal's group, so closing it closes the terminal too. The menu now names the group ("Close Konsole", "Konsole and everything started from it") instead of looking like it closes one program.
- **Two restart requests at once can't both start.** The second was refused with a message about a backup running. Only one app restarts at a time, and the message says so.
- **Immich without machine learning, or Nextcloud without its cron container, can be restarted.** Restart app refused with "one of its parts is missing" when an optional part was left out on purpose. Only the app's main container is required now.
- **A restart that can't start every part says so, and tries them all.** If Docker refused to start one part, the parts after it were left stopped and the message blamed stopping. Every part is started regardless, and the result names what to do.
- **A restart is never cut in half by setup.** When setup restarted the control service during an app restart, the service waited 80 seconds, which two parts stopping (up to 60 s each) could exceed. It now waits until the parts are started again (up to 200 s; the service's stop limit is 240 s), and no longer waits while it is merely checking that the app answers.
- **"Not responding" no longer outlives a restart.** The hourly app check from before a restart could label the app as not responding for up to an hour afterwards, even though Docker reported it running and the restart had seen it answer. A check older than the app's current start no longer counts.
- **Setup no longer complains about fans a moment too early.** With fan control switched off, setup looked for fans still held right after restarting the control service, before the service had handed them back. It now gives the service up to 15 seconds.
- **A restore that can't set a file's date still succeeds.** Restoring the saved modification times (new in 0.28.5) failed the whole restore if the filesystem refused a timestamp, after every file was already copied and verified. The restore now finishes and its report says `times_restored: false`.

### Changed
- Version 0.28.6; the release folder is now `nekoserver-v0.28.6`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.5 — 2026-10-22

### Added
- **Restart or close apps from the dashboard**: right-click on a computer, like a task manager, or tap the ⋯ button on a phone or tablet (it stands out there, since phones have no right-click):
  - **Server apps** (on *Your apps*, or any of their programs on *Processes*): **Restart app** stops the whole app and starts it again in order, then waits until it runs and answers. Its database and cache keep running: Immich restarts its server and machine learning, Nextcloud its web part and background jobs.
  - **Desktop apps** (opened on the server's own desktop): **Close app** asks it to quit. If it is still running 10 seconds later, the page says so and offers **Force close**, which warns first that unsaved work is lost.
  - The page shows *Stopping…* or *Restarting…*, then the result. A failure says why (it stopped again, its health check fails, it doesn't answer yet) and what to do.
  - **Protected**: databases and caches inside apps, backups, the monthly update, system services, the desktop itself and NekoServer are never stopped from here. Their menu says so.
  - **Every app action needs the PIN, on every device, the server too.** Without a PIN, app controls stay off.
  - **Backups and updates come first.** A restart waits while a backup or the monthly update runs, or while a cut-off one still has to be finished (new error E47). Only one app restarts at a time.
  - Restarts and closes are done by the control service (root), which checks each request itself. The web server still can't stop anything, and closing goes only to processes in a desktop session's own app group.
- **The Apps page shows each app's health.** It comes from the app's own health check: Docker's where the app has one, otherwise the hourly app check that asks it for a page. *Not responding* never comes from processor use alone, because converting a video can rightly use every core.

### Fixed (every Linux)
- **Long file names no longer stop a backup.** Copies were written as `name.neko-part` first, so a file whose name was 246 bytes or longer (255 is allowed) failed. A version of an edit (`name.neko-<date>.ext`) failed from about 224 bytes. The copy in progress now has a short name of its own. Dated versions and current-version notes that would be too long keep the start of the name plus a short code, and the full name is kept in the folder's `.neko-names.json`. The recovery wizard uses it to restore the file under its own name. This affects the photo archive, the USB copies and Nextcloud.
- **The USB drive keeps its checksum records when the HDD's are lost.** The weekly mirror replaced the USB's ledger with the HDD's. With the HDD's ledger gone, that left the USB with an empty one, which no longer said which copy of a damaged file is the good one. Both ledgers are now read and combined; neither loses a record, and a run that fails part way leaves the USB's ledger as it was. Two ledgers that record different checksums decide nothing: both copies are kept and reported. An unreadable USB ledger is set aside, never overwritten.
- **Nextcloud files whose names differ only in upper/lower case** (`Documents/report.pdf` and `documents/report.pdf`) are no longer merged on an exFAT USB drive. exFAT takes them as one name, so the two folders became one and the files looked like versions of each other. The first keeps its place; the others are left out and listed, and the backup shows as incomplete. An ext4 drive keeps them apart.
- **Restores give files their original modification times back.** Host and app snapshot restores set every file to today's date, which confuses synchronisation and tools that compare ages. Saved times are now set again on files and folders. Nightly app snapshots now record them too; host snapshots already did.
- **Immich uses the graphics chip that passed the check.** Setup set the method (`qsv` for Intel, say) but not the device, so on a computer with two chips Immich could pick the other one (an AMD chip with Intel's method) and fail. The tested device is now set as Immich's preferred hardware device.
- **The boot hook that restarts Immich after a cut-off snapshot works from a folder with spaces.** Its command wasn't quoted, so `Neko Server/recover.py` became two arguments and Immich stayed stopped. The paths are now quoted for systemd. The hook uses the installer copy that setup keeps on the SSD, which isn't moved or deleted like a download folder can be.
- **LAN folder sharing finishes on the first setup run.** With `share_mode: "lan"` and automatic LAN settings, Samba said "Needs setup" because only the LAN gateway step chose the home-network address, and that step ran after Samba. Samba and the gateway now share one choice, made by whichever runs first, so sharing folders no longer needs the HTTPS gateway.
- **Nextcloud with server-side encryption is no longer reported as backed up.** With Nextcloud's *Encryption* app on, the weekly USB copy took the encrypted files but not the keys needed to read them (`files_encryption`). The checksums passed, and the copy still could not be read after losing the server. The weekly backup now detects encryption and reports itself as incomplete, saying why. The recovery wizard warns that such files can't be read instead of suggesting an upload. Encryption is off by default.
- **Onboarding finishes with only Navidrome, or only qBittorrent.** Setup always asks onboarding to connect apps, and onboarding stopped with "No supported connectors selected" when none of the chosen apps had anything to connect. Running setup again didn't help. Onboarding now checks those apps' accounts and readiness and carries on.
- **A media import stops as soon as its drive is gone.** The import checked the drives before it started and again only after copying everything. If the HDD was unmounted midway, the next files went into the empty folder underneath, on the system disk, and could fill it. The drive is now checked before each file and again before each one is published, and the whole import stops at once.
- **The backup password is chosen and typed twice, never taken from sudo.** Setup checked the password by running `sudo -v` with it. Under a passwordless sudo rule that succeeds whatever was typed, so a typo could become the backup password unnoticed. Setup now asks for a backup password (at least 8 characters, your login password is fine), typed twice. Keys made before keep opening with the sudo password you had then.
- **Monthly snapshots without a backup key say what they really hold.** Setup said the app password files would be left out, but the monthly job kept them in `data.tar`, unencrypted (readable by root only), so a restore still has them. That behaviour is kept, and now setup, every monthly run's log, the snapshot's manifest (`"password_files": "unencrypted_in_data_tar"`) and MONTHLY-MAINTENANCE.md all say so.
- **The Rust test that failed in CI now and then is fixed.** Its stand-in for OpenRGB answered after 4 seconds while the client waits 3, so it passed only when the system happened to deliver early. It now answers within the client's limit and still proves the fans never wait for the lights.

### Fixed (Debian / Ubuntu)
- **Finishing half-installed packages no longer sets up unneeded alternatives.** For a dependency with a choice (`A | B`), both counted as needed, so an unfinished B was set up even when A was installed. Now nothing is added when one choice is already set up (or provided by an installed package); otherwise only the first unfinished one is.

### Changed
- NEXTCLOUD.md says what is really backed up. The monthly snapshot holds Nextcloud's application, settings and database, not your files; those get the weekly USB copy. The monthly restore test proves the restored database, not the whole app with your files.
- The control service (`nekoserver-control`) runs whenever the dashboard is installed, because app controls need it. With fan control switched off (`"fan_control": false`), it never takes over a fan, and any fan it held goes back to the motherboard. Setup checks that before carrying on.
- Version 0.28.5; the release folder is now `nekoserver-v0.28.5`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.
- No PIN yet? Run setup again in a terminal to choose one; app controls stay off without it.

## v0.28.4 — 2026-10-21

### Fixed (every Linux)
- **Every Immich original is now archived, whatever its format.** The photo archive copied only files with extensions on its own list. Files Immich accepts but the list lacked (`.jfif`, `.jpe`, `.jp2`, `.psd`, `.svg`, `.mxf`, several camera RAW formats) were left out without a word, and the job still reported success. The USB photo backup copies that archive, so they were missing there too. Now every regular file in Immich's upload and library folders is archived, whatever its name or extension, including Immich's own `.immich` folder markers, so formats Immich adds in later versions are covered too. Every copy is still checked by checksum, a file that changes while it is copied is not kept, and symlinks or special files still stop the run. The next archive run copies the files that were left out before.
- **Immich comes back after a manual snapshot that was cut off.** The manual Immich snapshot (`recover.py snapshot`) stops Immich's services and restarted them only in a `finally` block. That never runs after a power cut, an out-of-memory kill or `kill -9`, so Immich stayed offline until restarted by hand. Before stopping anything, the snapshot now records which services it stopped and installs a one-time boot hook. At the next boot, the hook (or `sudo python3 recover.py resume`, or the next snapshot) starts them again. The record and the hook are removed only once they are confirmed running.
- **A second graphics chip is tried when the first fails.** On a machine with Intel and AMD graphics, only Intel was checked. If it failed, both apps went to software even though the AMD chip might have worked. Each app now gets the first chip, Intel first, that passes the check inside that app.
- **The finishing screen shows the dashboard at its real address.** With `dashboard.port` changed (to 8899, say), it still said `http://127.0.0.1:8787`. It now uses the configured port, and the default port is written in one place instead of seven.
- **Sonarr, Radarr and Lidarr could change the media library while an update was still being checked.** They started normally, with a writable library and their background jobs running. An import, rename or delete in that window stayed after a rollback, while their databases went back, so the two could disagree. While checked they now see the library read-only, keeping their ports for the media connection audit. After the commit they start as usual.
- **Hardware video works with only one of Jellyfin and Immich.** With Immich but not Jellyfin, the chip was checked inside a Jellyfin that doesn't exist, so Immich was never switched over. With Jellyfin but not Immich, setup said "needs setup: Immich" for an app you turned off. Now only the apps this server runs are checked, each inside its own container, and only those count towards finishing the step. A chip that fails one app's check leaves that app in software; that is a result, not a step left to do.
- **A backup that systemd can't even start now raises an alert.** Its worker writes no report then, so the dashboard showed "Unknown — No recorded successful run" for ever, and nothing reached the phone. The collector now reads the backup service's own result: a run systemd couldn't start or that crashed counts as failed, with an alert. A backup that has never succeeded becomes overdue one interval plus a day after its schedule was set up.
- **A monthly update that failed before its snapshot no longer leaves passwords behind.** The saved app definitions (`*-old.json`, with database passwords) are written before the recovery journal exists. When an early check failed there was nothing to roll back, so they stayed in plain text in the snapshot folder on the HDD (root-only). They are now blanked after any failed run; a run a journal still needs is left alone.
- **Immich set up with its own worker split** (`IMMICH_WORKERS_EXCLUDE` in its settings) was left with no worker at all while the monthly update checked it, so every update failed its checks and rolled back. The exclusion is set aside for the check.

### Fixed (Debian / Ubuntu)
- **A package you hold** (`apt-mark hold docker-ce`, say) counted as not installed. Setup then tried to install it again, and apt refused because held packages may not change. Held packages now count as installed and are left alone.
- **Ubuntu's automatic security updates no longer make setup fail at random.** When they were running, apt stopped at once because dpkg was busy. apt now waits for them, up to 10 minutes.
- **Ubuntu's needrestart can no longer restart your running services during setup.** After an install it may restart services that use updated libraries, a Docker you already run included. It is switched off for NekoServer's package installs.
- **A new setup run waits for a cut-off installation that is still working.** After an interruption, setup already waited for apt and dpkg before tidying up. But a fresh setup run went straight into the tidy-up, so it could remove the record, the service masks and the start policy while a surviving dpkg was still installing. Every tidy-up now waits until apt and dpkg have stopped. If they are still busy, it keeps everything protected and says so. A `dpkg --audit` that fails also stops the tidy-up rather than counting as "nothing unfinished".
- **Finishing half-installed packages touches only NekoServer's.** It used `dpkg --configure -a`, which also sets up any unrelated package that is waiting (a half-installed Postfix, say), along with its problems. Now only this installation's packages and the unfinished packages they depend on are set up; others are left as they were.
- **Recovery works offline.** `install.sh --recover` repaired the Docker and Tailscale repositories first. Without a network, a missing signing key stopped it before the recovery wizard opened. Recovery now skips that repair; a recovery step that installs packages does what it needs then.
- **Ubuntu-based systems that name only Ubuntu** in their os-release (elementary OS, for one) passed `install.sh` and then failed at "not supported yet". They are recognised as Debian/Ubuntu everywhere.

### Changed
- The README treats Arch Linux, Nyarch, Debian and Ubuntu alike, and no longer points to the Arch-only edition, which is retired.
- Version 0.28.4; the release folder is now `nekoserver-v0.28.4`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.3 — 2026-10-21

### Fixed (every Linux)
- **Passwords no longer stay in monthly snapshots.** The definitions of Immich and Nextcloud as they run during the checks (`*-quarantine.json`, new in 0.28.2) hold the same database passwords as the other saved definitions. When the update was over they were not blanked like the others, so the passwords stayed in plain text in the snapshot on the HDD. They are blanked now; snapshots from 0.28.2 are cleaned at the next monthly run.
- **Nextcloud's file check leaves nothing behind.** Before and after each update, the check writes a small test file in the administrator's files and deletes it. The deleted file went to the trash bin, so one was left there every month. After a rollback, it was also a file the restored database knew nothing about. The trash bin is now paused for that one deletion, where Nextcloud allows it.
- **A failure after the update is committed says so.** If Immich or Nextcloud could not be opened up again, the message read like a failed update. It now says the update is installed and that the resume service retries opening them up, without rolling back.

### Fixed (Debian / Ubuntu)
- **Setup waits for dpkg as well as apt.** When an installation is cut off, apt-get stops and lets go of its lock, but the dpkg it started can keep working. Setup only checked apt's lock, so it could tidy up while dpkg was still installing. It now waits until dpkg's own lock is free too.
- **Packages left half set up are finished under protection.** A cut-off installation can leave packages unpacked but not set up. Setting them up later by hand (`dpkg --configure -a`) would start their services with nothing holding them back. Now NekoServer finishes setting them up itself while their services are still masked and kept from starting, then switches them off as usual. If that fails, the services stay protected and setup says what went wrong. Half-set-up packages that have nothing to do with NekoServer's installation are left to whoever installed them.
- **LMDE (Linux Mint Debian Edition) gets the right Docker and Tailscale repositories.** Its release name (`faye`) is not a Debian one; the Debian release it is built on (`DEBIAN_CODENAME`, `bookworm`) is used, as Ubuntu-based systems already use theirs.
- **A `policy-rc.d` that hangs or can't run** made setup stop with a Python error. It now counts as not refusing, so setup stops with the usual explanation of how to move it aside.
- **Samba's unchanged example installed before NekoServer** (for example with `smbclient`) stopped setup with "Existing unmanaged smb.conf; review manually". When Debian's record proves the file is the untouched example, the message now says so and how to move it aside. NekoServer still doesn't move it by itself, because Samba may be in use for something else.

### Changed
- Version 0.28.3; the release folder is now `nekoserver-v0.28.3`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.2 — 2026-10-21

### Fixed (every Linux)
- **A failed monthly update can no longer leave photos or files out of step with their database.** The updated apps used to start on their usual ports before every check had finished, so phones could reconnect and upload, edit or delete in that window. If a later check then failed, the rollback restored the old Immich and Nextcloud databases but not the photo library or user files, which aren't in the monthly snapshot. That could leave uploads with no database entry, or database entries pointing at deleted files.
  - While Immich and Nextcloud are checked, they run on a random port on this computer only, with no background jobs (no Nextcloud cron, Immich API only).
  - The update is committed (journal phase `committed`) only after every check and the media audit have passed. Only then do the two apps open up again.
  - A failure before the commit rolls back while nothing could have changed those files. A failure after it keeps the update and only retries opening the apps up.
  - Tests run the whole sequence with stand-ins for Docker and induce failures at each step. A real Docker run with an induced failure still needs to be done on a server.

### Fixed (Debian / Ubuntu)
- **An installation that times out is tidied up too.** The tidy-up ran only when apt finished on its own. A timeout skipped it, while the "don't start services" policy was already gone. Newly installed Samba services stayed enabled, so a restart before the next setup run could start them before their drive guards existed. Now:
  - The new services are masked before installing, so they cannot start, not even after a restart.
  - After any interruption, setup waits until apt and dpkg have really stopped, then tidies up.
  - The masks and the policy are removed only once the services are confirmed switched off. If that fails, they stay for the next run.
  - A mask the package set itself (`samba-ad-dc`) is left alone.
- **Two setups can't step on each other.**
  - `install.sh` takes a setup lock before doing anything, and the installer keeps it until setup ends; a second `sudo bash install.sh` stops right away.
  - Every package job takes a separate package lock: installing, tidying up and repairing repositories.
  - Tidying up a half-done installation now happens inside the installer's lock. A second process can no longer mistake a running installation for an abandoned one and remove its record and start protection.
- **Repository entries written over several lines are recognised.** In deb822 `.sources` files, a value can continue on the next lines (`URIs:` with the address underneath), and field names are not case-sensitive. NekoServer missed such entries and added a second, conflicting definition.
- **Repository problems are repaired before apt first runs.** A duplicate definition left by 0.28.0 made the first `apt-get update` fail before the repair could happen. `install.sh` now runs `python3 host.py repair` first.
- **Upgrades move the Tailscale key even when nothing needs installing.** The repair now updates NekoServer's own repository definitions on every run:
  - 0.28.0's Tailscale list moves to the key under `/etc/apt/keyrings`.
  - A missing key is downloaded again.
  - Offline, a definition that still works is kept as it is.
- **Samba's `smb.conf` is moved aside only when Debian's record proves it is the untouched example.** That record is the checksum `ucf` keeps for it. Without the record, the file stays, and the Samba step asks you to review it.

### Changed
- Code cleanup from SonarCloud's review of 0.28.1, with no change in behaviour:
  - The base package adapter's `prepare` no longer returns an unused default.
  - Two `dict(...)` calls became dictionary comprehensions.
  - Tests that expect an error now make only the call that should fail inside the check.
- Version 0.28.2; the release folder is now `nekoserver-v0.28.2`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.1 — 2026-10-21

### Fixed (Debian / Ubuntu)
- **A package installation that fails part-way is tidied up.** apt can install some packages and then fail. Before, the error came before the tidy-up, and a retry skipped the packages that were already in. Their services stayed switched on for the next restart, before NekoServer's drive guards existed. Samba's example `smb.conf` also stayed, so the Samba step refused it. Now each installation is recorded first (`/var/lib/nekoserver-host/package-install.json`), with what the system was like before. Whatever got installed is tidied up even when apt fails, and an installation cut off earlier is finished at the start of the next setup run.
- **Samba's example settings are told apart from yours.** The packaged `smb.conf` is set aside only when none existed before the installation and the file is still exactly what the package wrote (`ucf`'s record of it). An `smb.conf` you edited is never moved.
- **Existing repository definitions are respected.** Docker's and Tailscale's repositories are recognised by their address in every source file: `.list` or deb822 `.sources`, whatever the file is called, switched-off and commented entries excepted. A second definition with a different key made apt stop working ("Conflicting values set for option Signed-By"). NekoServer now adds its own only when there is none. If it finds its own next to someone else's, it removes its own. A file that has NekoServer's name but isn't NekoServer's is never overwritten.
- **Package checks work in every language.** The package tools and `systemctl` now run with `LC_ALL=C`, because their output is read: French apt prints `Candidat :`, so Caddy, video drivers, smartmontools and OpenRGB looked unavailable.
- **The Tailscale repository key is in host backups.** It now lives in `/etc/apt/keyrings` with the other keys. Restored repository settings whose key is missing get the key downloaded again. 0.28.0's Tailscale list is moved to the new key place.
- **Someone else's `policy-rc.d` can't let services start early.** If one exists, it must already refuse to start the services being installed. If it doesn't, setup stops before installing and explains how to move it aside for the installation. One left behind by an interrupted installation is removed.
- **Switching services off is checked.** After installing, setup checks that each new service is really disabled and stopped. If one isn't, setup stops and tells you the command. Services that aren't installed or are masked are left alone.
- **A Docker you already run is left running.** Installing only a missing part (Buildx, say) no longer reinstalls or upgrades the rest. It also no longer stops and disables Docker's service. Only services that are new with this installation are switched off, and packages already there are never reinstalled.
- **TV setup is "Not available on this system"** on Debian/Ubuntu, instead of passing. It isn't counted as done or as waiting. The finishing screen lists it on its own, and acceptance no longer asks for TV evidence there.

### Fixed
- **The installer copy kept on the SSD can set the dashboard up again.** It kept the dashboard's page, styles, scripts and logo, but not its app manifest and icons (`manifest.webmanifest`, `icon-192.png`, `icon-512.png`, `apple-touch-icon.png`). So after an OS rebuild, running dashboard setup from that copy stopped with "file not found"; the dashboard already installed kept working. The whole dashboard folder is now kept. A test checks that the kept copy has every file of the release, except tests and the template settings, and that dashboard setup can load its files from it.

### Changed
- The acceptance report's host check names both supported families.
- Version 0.28.1; the release folder is now `nekoserver-v0.28.1`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.28.0 — 2026-10-21

The first release of **NekoServer All**, the edition for all major Linux distributions. NekoServer for Arch continues separately.

### Added
- **Debian 12+ and Ubuntu 24.04+** (and systems built on them, such as Linux Mint 22). This is new and not yet proven on a real install, so try it on a spare PC or a virtual machine first.
  - `install.sh` only refreshes the package lists (`apt-get update`) and installs just what NekoServer needs. It never upgrades the rest of the system. If an earlier update is waiting for a restart (`/run/reboot-required`), it stops and asks for the restart before changing anything.
  - **Docker comes from Docker's official repository** (`docker-ce` with the Compose plugin) and Tailscale from Tailscale's. Their signing keys and repository lists are added only when no list for them exists yet. If Debian's own Docker packages (`docker.io`, `podman-docker` and so on) are installed, setup stops and asks you to remove them, rather than mixing the two.
  - Debian's packages start their services as they install. NekoServer prevents that while installing and leaves Docker, Caddy, Samba (with NetBIOS) and Tailscale switched off afterwards. Each one's own setup step switches it on once its drive and settings are ready, so Docker still never starts on the system disk.
  - Samba's example `smb.conf` from the package is moved aside to `smb.conf.packaged`. NekoServer then writes its own, as on Arch. A `smb.conf` that existed before is never touched.
  - Samba runs as `smbd.service`. The dashboard, notifications and the host backup find whichever Samba service is installed.
  - Host backups keep apt's sources, keys and preferences, plus `/etc/default/locale` and `/etc/default/keyboard`, instead of pacman's settings.
  - Package names are mapped; for example, the video drivers are `intel-media-va-driver`, `mesa-va-drivers` and `vainfo`. OpenRGB lighting is skipped when the system's repositories don't offer it.
  - The TV step (Steam Big Picture) is skipped on Debian and Ubuntu for now.
- **Setup checks the computer first.** Before changing anything, every setup run lists what it found and stops when a requirement is missing. The requirements are an x86-64 processor, Python 3.11 or newer, systemd 247 or newer, a supported Linux family and no restart pending from an earlier update. Docker and Compose versions, control groups v2, SELinux, AppArmor, a switched-on firewall (ufw, firewalld, nftables) and whether a desktop runs are reported as well (`capabilities.py`).
- **One place knows the Linux family** (`host.py`): its package manager, package names, service names, vendor repositories and the settings files that belong in host backups. [PORTING.md](nekoserver-v0.28.0/PORTING.md) explains how another family is added.

### Changed
- Every module that installed packages called `pacman` itself (Docker, Compose, Samba, Tailscale, Caddy, OpenRGB, smartmontools, hdparm, video drivers, the TV packages, Firefox/xdg-utils and qrencode). They now all ask `host.py`, and a test fails if `pacman`, `apt-get` or `dpkg` appears anywhere else. On Arch the same packages are installed in the same way.
- `install.sh` installs the apps' packages through `python3 host.py bootstrap` on both families. Arch still runs its full `pacman -Syu` first.
- `neko_common.arch_host()` was replaced by `host.supported()`.
- The guides say which steps differ between Arch and Debian/Ubuntu.
- CI runs on pushes to `main`.
- Version 0.28.0; the release folder is now `nekoserver-v0.28.0`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.8 — 2026-10-21

### Changed
- With `"rgb": false` the fan and light service no longer even opens the saved lighting file (it already ignored it).
- Version 0.27.8; the release folder is now `nekoserver-v0.27.8`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.7 — 2026-10-21

### Fixed
- **Switching off only RGB lighting no longer stops setup.** With fan control still on, setup stopped OpenRGB and then took the records of the fans still under control as a sign that fans had not gone back to the motherboard, and stopped with that error (reproduced). Those records are now only checked when fan control itself is switched off.

- **`"rgb": false` now really leaves lighting alone.** Setup removed NekoServer's OpenRGB service, but the fan and light service still read the saved lighting, asked OpenRGB every few seconds and set its saved colours again, so an OpenRGB you run yourself (its app with the SDK server on) could be overwritten. The service is now told lighting is off: it never reads, asks or changes lighting, and the page says lighting is switched off.
- **Setup tells the truth about old app snapshots.** It said "No automatic backup deletion", although by default the newest 14 complete snapshots are kept and older ones deleted. It now says what the setting does (and "none are deleted" only with `0`).
- **The app backup guide no longer contradicts itself.** One paragraph said symlinks, hard links, special files and nested filesystems make the backup fail; they are skipped and listed (hard links copied as ordinary files), as the guide already said earlier. Only custom writable mounts and folders shared with another container make it fail.

### Changed
- Version 0.27.7; the release folder is now `nekoserver-v0.27.7`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.6 — 2026-10-21

### Changed (code cleanup; no change in what the server does)
- **Removed unused code.** The old Python dashboard collector (replaced by the Rust one in 0.26.0) is gone; the small checks setup, the gateway and the acceptance checker still use moved to `status_checks.py`. A full scan for unused functions, variables, imports, page code, styles and page elements finds nothing left.
- **One definition instead of copies:**
  - one list of apps with their names and ports (`neko_common.APPS`), used by setup, checks, accounts, the TV shortcuts and the phone page (it was typed out in nine places);
  - one way to talk to the apps without proxies or redirects (`local_opener`, was three copies plus eight hand-built openers);
  - one place for systemd units and their "managed by NekoServer" check (`check_units` / `write_units`, was eight copies);
  - one Seerr sign-in, one check for source addresses;
  - in the Rust runtime: one HTTPS delivery (heartbeat and notifications), one way to open a backup job, stage and publish a snapshot (app and host snapshots), one "move without replacing", one fan-speed conversion, one table of error sentences;
  - on the page: one way to show fan speeds and temperatures.
- Duplicated code measured with jscpd: 14 copies (145 lines) before, 2 short ones (27 lines) left.
- **Bug-risk fixes found by a full Python lint pass (ruff):** helper functions inside loops now keep the value of their own loop pass; errors re-raised inside error handlers keep (or deliberately drop) their cause; loop variables are no longer overwritten; default arguments no longer call functions when a file is loaded; and every `subprocess.run` that checks the result itself now says so (`check=False`).
- **The shared command and report helpers are stricter:** only known programs with plain arguments are run, and only plain absolute paths are read (found by SonarCloud after the move).
- New tests check that names the installer and the runtime share (fan recovery files, settings folder, phone address port) stay the same in both.
- Version 0.27.6; the release folder is now `nekoserver-v0.27.6`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.5 — 2026-10-21

### Fixed (review of v0.27.4)
- **Undo can't bring back an old fan speed.** After saving 80 %, Undo read the page's older reading and put 40 % back, which a later name-only save then sent (reproduced). Each fan now remembers the setting the server last confirmed, and Undo returns to that.
- **A passing Tailscale problem no longer removes the phone address.** When Tailscale couldn't be asked during setup (not connected yet, restarting), the dashboard's Tailscale name was removed as if remote access had been switched off. The name already in use is now kept; it is only removed when remote access is switched off.
- **The acceptance check expects the same backup account as setup.** Setup keeps backing up the first account, but the check worked out the account again from whoever ran it and reported "needs setup". Both now use the same answer.

### Changed
- Version 0.27.5; the release folder is now `nekoserver-v0.27.5`.
- Code check: the host snapshot list is built in small steps, and a dashboard constant moved to the top of its file.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.4 — 2026-10-21

### Fixed (review of v0.27.3)
- **A damaged fan record is never "restored".** A recovery record with missing or wrong fields (valid JSON otherwise) switched the fan to mode 2, reported success and deleted the record. Records are now checked field by field; one that isn't a real setting is kept, moved aside and reported, and nothing is changed.
- **A save can't be undone by an older reading.** A reading asked for just before saving 80 % could arrive afterwards and put 40 % back in the editor, and renaming the fan then saved 40 % over the good change (reproduced). Readings asked for before the last save are no longer taken into the editor.
- **"All off" (and "Apply to all") win over edits still on their way.** A colour change sent a moment later could turn a light back on (reproduced).
- **A reset device is noticed whatever it shows.** The check accepted the right effect with the wrong colour or brightness, and an animation as a steady colour. It now checks the effect and its colour, or a steady mode with every light at the chosen colour.
- **Remote access set up after the dashboard now reaches it.** With the dashboard installed before Tailscale was connected, setting up remote access created the phone address, but the dashboard refused it (E41, reproduced). Every setup run now gives the dashboard its current Tailscale name (and restarts it) when that changed.
- **Lighting saved by 0.27.2 isn't lost.** Two different devices sharing a serial string were named by their i2c place in 0.27.2 and by serial in 0.27.3, and the 0.27.2 names weren't moved over (reproduced: both lights lost their choice). 0.27.2 names are now moved too.
- **Host snapshots keep backing up the same person.** With `"user": "auto"`, setup run by another administrator replaced the first user's Steam backup with their own. The account chosen the first time now stays chosen until it is named differently.

### Changed
- Version 0.27.4; the release folder is now `nekoserver-v0.27.4`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.3 — 2026-10-21

### Fixed (review of v0.27.2)
- **Fixed speeds keep their safety net.** A fan at a fixed speed kept running slowly when every temperature reading disappeared (only curves were covered). Losing every temperature now sends every controlled fan to full speed.
- **Failing fan speed sensors can't delay the safety checks.** Each unreadable speed sensor added up to 0.4 s of retries without checking temperatures or Stop, so ten failing sensors delayed the next check by 4 s (reproduced). All fans are now read together, and retries wait through the same safety checks as the rest of detection, at most twice per measurement.
- **Detect fans respects a sensor's own critical limit.** It only stopped at 85 °C, so a sensor with a 70 °C limit could run hot while fans were slowed. Detection now stops on the same limits as normal fan control, with its 85 °C ceiling on top.
- **A fan output that moved is never confused with another chip.** Linux reuses hwmon numbers; an old path could point at a different chip before the next look (reproduced: an unrelated chip's fan changed). Outputs now keep their resolved device folder and are checked to be the same chip on the same device before every change; a moved output is looked for again at once.
- **"Full speed" is claimed only when it was set.** When a fan refused to go back to the motherboard and full speed couldn't be set either, the page still said full speed. It now says the speed couldn't be set.
- **A fan is only taken over when its own setting can be read.** Otherwise its original setting would be unknown and couldn't be put back.
- **Damaged recovery records are kept and reported.** A record that couldn't be read was treated as empty and deleted. It is now kept (moved aside when the service starts), never written over, and the page and the restore command say so.
- **Conflicting recovery records can't undo a good restore.** Both records are now merged first, the main one winning, and each fan is restored once.
- **Lighting names moved at start are saved at once,** so lighting survives a restart that renumbers a device even if the dashboard was never opened.
- **Another device with the same serial no longer renames yours.** Serial clashes now only count between devices with the same name.
- **Lighting comes back after a device or OpenRGB reset.** NekoServer now checks every 10 seconds that each device still shows its choice, and sends it again when not.
- **Switching fan control off in setup is checked.** Failures stopping the services were ignored. Setup now stops with a clear message if the services keep running or a fan didn't go back to the motherboard (the record is kept when the service stops, so it can be checked).
- **Turn off wins over a colour still on its way.** A colour change sent a moment later turned the lights back on.
- **Undo shows what will be saved.** The speed labels and the name kept the discarded values.
- **Host snapshots include the new settings.** Fan curves, lighting choices, the PIN hash, the dashboard settings, OpenRGB's settings and the i2c driver setting were not backed up. They are now, and every setup run brings an existing list of places up to date.

### Changed
- Version 0.27.3; the release folder is now `nekoserver-v0.27.3`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.
- Run setup once so host snapshots pick up the new settings.

## v0.27.2 — 2026-10-20

### Fixed (review of v0.27.1)
- **Fan recovery records are never deleted early.** When a fan couldn't be handed back and the combined record couldn't be saved, both records were deleted; detection also deleted its record without checking that moving it had worked. Now a record is removed only after its replacement is safely on disk, and if saving fails both stay exactly as they were. When both records name the same fan, the one from before NekoServer touched it wins.
- **Detect fans won't start while a fan isn't back with the motherboard.** Otherwise it could record NekoServer's own manual setting as the "original" and later put that back. It now refuses with a short explanation.
- **An unreadable fan speed is no longer read as a slowdown.** A failed reading counted as 0 RPM, so detection could link a fan to the wrong output. Readings are retried, and a fan whose speed stays unreadable is left out of that measurement.
- **Lighting keeps its device after a restart.** A device was named by its connection path first, even `/dev/hidrawN`, which Linux may renumber. Now a usable unique serial number comes first, then a stable place (HID paths left out, i2c bus numbers dropped, slot addresses kept). Choices saved by 0.27.0 and 0.27.1 move to the new names.

### Changed
- Version 0.27.2; the release folder is now `nekoserver-v0.27.2`.
- Code check: the installer's file checks are split into three small steps, and the page copies a fan setting with `structuredClone`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.1 — 2026-10-20

### Fixed (review of v0.27.0)
- **Fans are handed back after a crash during Detect fans.** Detection kept the motherboard's own fan settings only in memory, so a crash during its 35 % test could leave fans in manual mode. Every fan's original setting is now written to disk *before* it is changed (for curves and for detection alike), the recovery command reads both records, and the service also puts back anything left over when it starts. Stopping the service now waits for a running detection to hand its fans back first.
- **Lighting can't delay cooling.** Talking to OpenRGB held the same lock as the fan loop, so a slow or hung OpenRGB could delay temperature checks and the full-speed safety response by seconds. Fans, lighting and the PIN now have separate locks, each request runs on its own, retrying lights has its own thread, and the page shows the last lighting answer while OpenRGB is busy.
- **Detect fans needs temperatures it can read.** A sensor that failed was simply ignored while fans were slowed down. Detection now won't start when no temperature can be read, and stops (handing the fans back) as soon as a watched temperature can no longer be read.
- **A fan that won't go back to the motherboard is never shown as handed back.** Errors putting back the motherboard's setting were ignored and the record of it dropped, so the page could say "Motherboard" while the fan stayed in manual mode. The result is now checked: until it works, the record is kept (in memory and on disk), the fan runs at full speed, the page says *Not handed back*, and NekoServer keeps trying, also after a restart.
- **Changes that can't be saved are refused.** Failures writing the fan or lighting settings were ignored, so a change could work until the next restart and then vanish. Such a change is now refused with the new code E46, and nothing changes.
- **The fan page no longer shows old readings as current.** After losing the connection it kept showing the last temperatures and speeds without a warning. They are now dimmed, the badge and banner show the error, and nothing can be changed until fresh readings arrive.
- **Edits made while saving are kept.** Changing a fan while its Save was on the way marked everything as saved although only the earlier values were sent. The page now tracks which edit was saved and asks you to save again.
- **Lighting controls follow changes.** After *Apply to all* or a change on another device, only the colour dot changed; the colour, brightness and effect controls now follow too, except while you're using them.
- **Brightness is honest about effects with their own colours.** Brightness only scales the chosen colour, so it did nothing for effects such as a rainbow. For those effects, colour and brightness are now switched off with a short explanation.
- **Lights are retried until every device got its setting.** A device missing when the lights were restored (still starting, plugged in later) was skipped for good. The service now keeps retrying until every saved device has received its lighting.
- **Identical lighting devices keep their own settings.** Two identical RAM sticks or controllers shared one name (name and description only), so they shared settings and appeared as one card. Each device is now named by where it is connected (or its serial number, or its position as a last resort), and the card shows where it is. Choices saved by 0.27.0 move to each matching device.

### Changed
- Version 0.27.1; the release folder is now `nekoserver-v0.27.1`.
- Smaller fixes from the code check: no `!important` in the stylesheet, and the dashboard installer is split into short steps.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.27.0 — 2026-10-20

### Added
- **Fan control that really controls the fans.** The new *Fans & lights* page finds every fan output Linux can drive (motherboard chip, graphics card, cooler) and runs each one by a temperature curve, at a fixed speed, or leaves it to the motherboard. Curves are edited by dragging their points (or with the arrow keys), with *Silent*, *Balanced* and *Performance* presets, a minimum speed, and a live marker for the temperature now. Written in Rust like the rest of the runtime.
- **Detect fans.** A guided test finds which fan is on which output: all fans to full speed, then each output slowed in turn while the speeds are watched. It can be stopped at any time, stops by itself at 85 °C, and always hands the fans back to the motherboard.
- **RGB lighting that changes.** OpenRGB is installed from Arch's own repository and runs as a service on this computer only (with the i2c driver for memory and motherboard lights). Each device gets a colour, brightness, an effect and on/off, plus *Apply to all*; choices come back after a restart.
- **A PIN for phones and tablets.** On the server itself changes need nothing more. Other devices (through Tailscale) can always look, but need the PIN chosen during setup to change fans or lights; five wrong tries lock changes for 5 minutes. Only a slow, salted hash is kept.
- New error codes: E21 (fan and light control unavailable), E43 (PIN needed), E44 (wrong PIN), E45 (too many tries). Help has two new guides, *Fans don't change* and *Lights don't change*.

### Safety
- Hardware is changed only by a new root service, `nekoserver-control`, reached over a private socket that only the dashboard's (still unprivileged) web server can use. Every request is checked: known outputs, sensors and lights, sensible ranges, and changes only from the dashboard page itself.
- Fans speed up at once and slow down gently; a controlled fan never goes below 20%; every controlled fan goes to full speed at 95 °C, near a sensor's own limit, or when its temperature can't be read. Whenever the service stops (a crash too) every output is handed back to the motherboard.
- `"fan_control": false` (or `"rgb": false`) under `"dashboard"` turns it off; setup then gives the fans back to the motherboard.

### Changed
- Version 0.27.0; the release folder is now `nekoserver-v0.27.0`.
- RGB devices moved from *Sensors* to *Fans & lights*; live readings no longer ask OpenRGB every minute.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.
- Run setup in a terminal to choose the PIN; without one, fans and lights can be changed on the server only.

## v0.26.2 — 2026-10-20

### Fixed (review of v0.26.1)
- **A missing Nextcloud folder no longer gives a successful weekly backup.** When the Nextcloud files folder was missing, the USB job said "nothing to copy" and could still pass without Nextcloud. It is now recorded as incomplete, with the reason on the dashboard and in notifications.
- **Service alerts reach your phone.** The new "stopped with an error" and "won't start after a restart" alerts had no notification label, so they were silently dropped (reproduced). They are now sent, and "RECOVERED" is sent only when the service list shows the service enabled or no longer failed (never when the list is missing).
- **"Restore tried" asks only about backups you have.** It counted placeholder rows for backups that aren't installed, so a server with only app backups (restore tested) showed "1 of 3" (reproduced). Only backups whose schedule is installed (even if switched off) or that have ever run count now.
- **A failed ownership restore no longer reports success.** The app and host restore wrote "original owners restored" into its report before changing any owner, so a failed `chown` left a report claiming success (reproduced). The report now says "incomplete" until every owner and permission has been applied.
- **NVIDIA figures are never shown as live when they are old.** They come from the minute-by-minute collector, and were merged into the live readings without checking its age. Each card now shows when its figures were measured, and once the collector is late they are left out with a note instead.

### Changed
- Version 0.26.2; the release folder is now `nekoserver-v0.26.2`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.26.1 — 2026-10-20

### Fixed (review of v0.26.0)
- **An open dashboard no longer keeps the media hard drive awake.** Live readings asked SATA drives for their temperature every few seconds, which resets the spin-down timer on some hard drives. Those temperatures now come from the drive health check (every 30 minutes, never waking a sleeping drive).
- **Readings are never shown as current when they aren't.** When live readings failed, Home kept showing the old ones under "Connected"; when the snapshot failed, "Everything looks good" could stay beside the error. Each feed is now tracked on its own: errors show on every page with their code, old live readings are dimmed and marked with their time, and the overall status includes failed requests.
- **"Restore tried" means the backups you have were restored.** It turned green for any completed checklist, including one without a single restore test (reproduced with the dashboard profile). It now counts only the witnessed restore tests for the backups this server makes (photos, apps, host and user files) and names the ones still to try.
- **Checksum results need a date.** A checksum report without a valid date, or older than two weeks, showed "Checksums match" (reproduced). It now shows "Not dated" or "Overdue", and the time of the last recorded check.
- **Keyboard and controller focus stays put.** Starring an app or a refresh rebuilt the cards and dropped focus to the top of the page (reproduced). Focus now returns to the same control.
- **A failed service is never green.** "Starts by itself" only looked at whether a service starts at boot, so an enabled Samba that had failed was green with no warning (reproduced). Working now and starting at boot are shown separately, and a failed service raises an alert (urgent for Docker, Tailscale, the dashboard and Samba).
- **No false "out of date" with a five-minute schedule.** Readings counted as late after a fixed 150 seconds, although a 300-second collection interval is allowed (reproduced: E12 after 151 s). The collector now publishes its interval, and the page and the acceptance check both allow two intervals plus 30 seconds.
- **A manual photo snapshot can't silently leave out an album.** Its file listing skipped folders it couldn't read, so a snapshot missing an album passed, and so did its verification (reproduced). Planning, copying and verifying now share one strict listing: anything unreadable stops the snapshot, which stays marked incomplete.
- **A manual snapshot checks that Immich stopped cleanly.** It now refuses to copy when a container is still running or was killed (exit 137 or out of memory), like the nightly app snapshot.
- **A corrected clock no longer postpones full checksum checks for years.** A check recorded while the clock was wrong (in the future) counted as recent. Future or invalid records now make the full check due, in the backup program and in the space estimate.
- **USB backups on exFAT stay private.** exFAT has no owners or permissions, so the backup's chmod was silently ignored and other local accounts could read the Nextcloud copies and database dumps. exFAT drives are now mounted for root only, and a drive already mounted readable by others is refused before anything private is copied. The restore helper also releases the drive (and unmounts it) when opening it fails.

### Changed
- Version 0.26.1; the release folder is now `nekoserver-v0.26.1`. v0.26.0 was merged before these fixes, so this release carries its own version (an installed release folder is never overwritten).
- The dashboard script was restructured for readability (SonarCloud maintainability: no nested conditions, small functions), with no change in behaviour. Folder walks check their path first (absolute, no `..`, no symlinks; recovery folders use the same plain characters as mount paths).

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.26.0 — 2026-10-20

### New dashboard
- **A new dashboard, in Rust, replacing the old one.** Same cat and colours, rebuilt for everyday use: Home (what needs attention, apps, storage and live readings), Your apps (with each app's processor and memory use), Backups, **Hardware**, **Sensors**, **Processes**, Server health, Needs attention and Help.
- **Hardware and sensors are found automatically** by the dashboard's own web server, with no setup and without root: processor (total, each core, clock, waiting work), memory and swap, every graphics card and AI chip (named from the system's PCI list; AMD load and video memory, Intel clock, NVIDIA through the collector), every hardware-monitoring chip (temperatures, fans, fan outputs, power, voltages; thermal zones when no chip reports a temperature), network traffic per card and Tailscale, drive activity, batteries and UPS, and RGB devices of a running OpenRGB server. Readings are taken only while a page asks for them. Nothing is ever written to fans, lighting or processes. `nekoserver-runtime hardware` prints what it finds.
- **Processes**, with the app each one belongs to (ffmpeg → Jellyfin). Names only: never command lines, environments or users.
- **Phone, tablet and TV:** a bottom tab bar on phones, an icon rail on tablets, larger touch targets and text (Settings → Large or TV).
- **Use it like an app instead of a browser tab:** it can be installed on phones, tablets and computers (Add to Home Screen / Install app) and opens in its own window. If the server can't be reached, the page still opens and says why. On the server, **NekoServer Dashboard** in the app menu opens it in an app window (Chromium, Chrome, Brave, Edge or Vivaldi when installed; otherwise the normal browser).
- **Short error codes** (E01 … E42) with one plain sentence each, shown at the top of the page and explained in Help and DASHBOARD.md.
- **Starts by itself:** the dashboard restarts if it ever stops (Restart=always). Server health lists what starts with the computer (Docker and the apps, Tailscale, the dashboard, Samba, the resume services, the schedules), and Needs attention warns when one of them is switched off.

### Fixed
- **Upgrading from v0.25.1 works.** The follow-up fixes below were merged while the release still called itself 0.25.1 everywhere inside (Python, the Rust runtime, its build manifest and install paths). Releases are installed into immutable per-version folders, so installing it over the real v0.25.1 was refused ("Installed release differs; refusing overwrite" and "Existing native runtime differs; custom file preserved", reproduced). This release is consistently 0.26.0 (0.25.2 to 0.25.31 were not used as internal versions), and a test now checks that the folder, Python, Cargo, config, README, CI and the install paths in the guides all name the same version. The immutable-file protection is unchanged.

### Fixed (follow-up review of v0.25.1)
- **Jellyfin-only setups pass the monthly check too.** The Python check used Jellyfin's own key, but the bundled runtime (used by the monthly update and the dashboard) still read the key only from Seerr's settings, so a Jellyfin-only server stayed "needs setup" and monthly maintenance was blocked (reproduced). The runtime now reads `secrets/jellyfin-readiness.key` first, exactly like Python, and both are tested against the same cases.
- **An update that starts while setup waits now stops setup.** Setup checked for an unfinished monthly update only before it began; a step waiting for the shared lock while an update started (and failed) then went on and started apps (reproduced). Every step that changes or starts apps now checks again once it holds the lock. Docker re-attachment and backup repair stay allowed, since they can be what lets a stuck update finish.
- **Repair finds the live original of a dated version.** A damaged `photo.neko-20261020T010000Z.jpg` was looked up in Immich under that dated name, so a live `photo.jpg` that still matched its recorded checksum was never used (reproduced). Repair now looks for `photo.jpg` and uses it only when its SHA-256 matches the ledger.
- **A revoked or replaced Jellyfin key is renewed by setup.** Setup kept NekoServer's Jellyfin key as long as its file was not empty, so a key revoked in Jellyfin stayed in use and the check kept failing even after rerunning setup (reproduced). Setup, while signed in as administrator, now compares the kept key with Jellyfin's active NekoServer keys and saves the current one (or creates a new one) when it is gone. Scheduled checks never change credentials: they report "Jellyfin no longer accepts NekoServer's key" and say to rerun setup.

### Changed
- Version 0.26.0; the release folder is now `nekoserver-v0.26.0`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.25.1 — 2026-10-20

### Fixed (from an outside review)
- **A verified file that disappears is now reported.** The checks only re-hashed the files that were still there, so a backed-up file deleted from the HDD archive (and from Immich) went unnoticed: the archive audit and the USB copy both passed (reproduced). Every run now compares the ledger's full list with the files actually present. A file missing from the HDD whose USB copy still matches is reported as *missing* and can be put back from the USB drive with **Repair damaged backup copies**; a file gone (or damaged) everywhere is CRITICAL. Missing files show on the dashboard and raise an alert.
- **A restore stops when a verified file is missing.** A restore whose ledger listed two files with only one left on the drive succeeded with "1 files matched" (reproduced). Before anything is chosen or copied, every file the ledger lists for the folder (or Nextcloud user) being restored must be present; otherwise the restore stops and names the missing files.
- **No setup while a monthly update is unfinished, even of the same version.** The check before setup only blocked an unfinished update started by another version; with the same version, setup went on and started Nextcloud before the pending rollback (reproduced in a simulation). Any unfinished monthly update now stops setup first, with the command that finishes or rolls it back.
- **Repair clears a warning only for copies it actually checked.** With the USB drive unplugged, repair checked only the HDD copy but also cleared the USB copy's warning (reproduced). Now a warning is cleared only when every copy it is about was checked: the USB warnings stay until the drive is connected, and the repair says so.
- **A clock jump never deletes the newest app or host snapshot.** Nightly app and host snapshots are pruned by the time in their names; with a snapshot dated in the future, a new one could be deleted right away (reproduced with retention 1). The snapshot just made is never removed, as for database copies since v0.24.6.

### Fixed (setup flow, from a wider review)
- **Setup can reach "ready" without looping on playback.** The media check always listed a real playback test as pending, so onboarding stayed unfinished however often it ran, and your confirmation was ignored (reproduced). Only automatic configuration checks now decide whether onboarding is finished. A real playback test is asked for once, afterwards: a yes is recorded; skipping it still finishes setup, and the finishing screen says "Playback verification pending".
- **Phone setup runs after everything it links to.** The Tailscale and phone step ran before the dashboard was installed but already asked you to test the dashboard on the phone. It now runs after all apps, the dashboard and the TV step, and the dashboard is only offered (and routed) once it is installed.
- **The dashboard's settings fingerprint uses the final setup choices.** Choices made after the dashboard step (TV, heartbeat, ...) changed the fingerprint an acceptance report must match, so a fresh report read as "outdated" (reproduced). The dashboard's collector settings are written again from the final configuration at the end of every setup run.
- **Jellyfin's check no longer needs Seerr.** The Jellyfin library check used the key stored in Seerr's settings, so a Jellyfin-only setup could never pass it (reproduced). Setup now creates Jellyfin's own NekoServer API key the one time it signs in as administrator, kept privately on the SSD (`secrets/jellyfin-readiness.key`, root only); Seerr's link is used only when Seerr is installed.
- **A media import no longer skips folders it cannot read.** The importer's folder walk ignored listing errors, so an unreadable album on a local drive or SMB/NFS share was left out and the import still said "copied" (reproduced). Imports now use the same strict listing as restores: if any folder or file of a source cannot be read, nothing is copied from that source, it is marked incomplete, and the report names the unreadable paths (relative to the source; the source's own location stays private). Files still being written (`.part`, `.tmp`, `.crdownload`, `.!qB`, `.neko-part`) are explicitly excluded.
- **Container logs are size-limited.** Docker keeps container logs forever by default, one way for the SSD to fill up. NekoServer's Docker settings now cap each container's log at 3 files of 10 MB, without overriding a log driver or limits you chose yourself. Containers made before keep unlimited logs until they are recreated after Docker restarts (a reboot, then the monthly update or rerunning setup); the acceptance check lists any that still do.

### Changed
- Version 0.25.1; the release folder is now `nekoserver-v0.25.1`. Two long functions were split for readability (no change in behaviour).
- **Steadier checksum ledger lock.** Opening a ledger now retries a busy lock for up to 2 seconds before reporting another writer: a program started at the same moment by another thread can briefly hold a copy of the lock (seen as an occasional test failure in CI). Paths read from the command line or settings are validated before use (installer state files, Jellyfin's key file).
- **Slimmer release.** Removed `INSTALLATION-GUIDE.pdf`, a v0.21.0 walk-through nothing linked to any more (the README is the current guide). Out-of-date guidance in the docs was corrected: old install paths and commands (for example NOTIFICATIONS.md's test command pointed at v0.15.1), DASHBOARD.md's claims that the dashboard is local-only and can still run in Python, and "use the v0.10.0 installer" in RECOVERY.md. DASHBOARD, NEXTCLOUD, HEARTBEAT and SERVER-OS guides are now linked from the README. Upgrade code for older releases is kept on purpose: it moves existing servers forward safely and protects old backups.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.25.0 — 2026-10-19

### Added: checksum ledger and integrity rules for every backup
Every backup tree (the HDD photo archive, its USB copy and the USB Nextcloud copy) now has a checksum ledger, `.neko-ledger.json`. See STORAGE.md, "How backups are verified".
- **Immutable checksums.** A file's SHA-256 is recorded when a copy of it is first verified and is never replaced, even when the file on a drive changes, so damage can never become the expected content.
- **Unverified files are flagged.** Files from before the ledger stay unverified (counted in the job log) until two independent copies agree (the archive and the Immich original, or the HDD and USB copies); then the agreed checksum is recorded.
- **HDD vs USB vs ledger.** The HDD matches and the USB doesn't: the USB copy is repaired from the HDD (the damaged one is kept in `neko-damaged/`). The USB matches and the HDD doesn't: the USB copy is kept, the HDD file is reported and never copied onto the USB drive. Neither matches: both are kept untouched and the job fails as CRITICAL. No checksum and the copies differ: both are kept and reported. The weekly job finishes every copy first, then fails with the file names.
- **A damaged archive file is no longer taken for an edit.** Before, a rotted archive file looked like a changed photo and got a new dated version. Now it is compared with its checksum. If the Immich original still matches, the archive file is repaired from it; otherwise it is reported.
- **Three-checksum copies.** Every copy is hashed while it streams, fsync'd and read back from disk, and the source is hashed again. It is published only when all three match and the source's size, modification and status-change times did not change. A source rewritten mid-copy (even with its size and mtime put back) is refused.
- **Durable, ordered ledgers.** Each ledger is written to `.neko-ledger.json.<uuid>.tmp`, fsync'd, renamed and its folder fsync'd, always after the files it lists. A lock keeps it to one writer. The USB drive gets its copy of the archive ledger after the files.
- **Fast checks vs deep audits.** Size and dates are used only to skip unchanged files. Every 4 weeks every copy on every drive is read in full against its checksum, which catches bit rot that leaves size and dates unchanged.
- **Strict enumeration and restore.** Backup trees with a symlink, a special file or an unreadable entry are reported, never silently skipped. The recovery wizard checks every restored file against the ledger and stops at a damaged, missing or unreadable file. Ledger paths must be plain relative paths (no leading `/`, no `..`); a ledger with any other path is not trusted at all.
- **Damage shows on the dashboard and can be repaired.** Each job saves what its checks found in `reports/integrity.json`. The dashboard's Backups page shows each tree as intact or needing repair, with the number of unverified files, and raises an alert (also sent to your phone) when anything is damaged. The alert clears only when a later check finds nothing. The new recovery menu option **Repair damaged backup copies** fixes each reported file from whichever copy still matches its SHA-256 checksum (HDD, USB drive or the Immich/Nextcloud original). Files with no checksum whose copies differ are settled by the original, or by you, and then recorded. Damaged copies are moved aside, never deleted. Without this, a damaged HDD file would have failed the weekly job every week.
- **SHA-256 everywhere.** Every backup checksum (ledgers, copies, database copies, snapshots) is SHA-256; tests check the published FIPS 180-2 test values in both the backup program and the Python tools, and the ledger accepts nothing but 64-character SHA-256 values.

### Changed
- Version 0.25.0; the release folder is now `nekoserver-v0.25.0`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.
- The first runs after upgrading record checksums as files are copied. Existing backups are reported as unverified until the next 4-weekly full check confirms them against a second copy.

## v0.24.6 — 2026-10-19

### Fixed (from an outside review)
- **An upgrade waits for an unfinished monthly update.** A monthly update can only be finished (or rolled back) by the program version that started it, and the installer replaced that program without checking, so the new version refused the old update's notes (`Maintenance recovery identity mismatch`) and the apps stayed stopped until someone ran the old program by hand (reproduced). `install.sh` now checks first, before the system update or any NekoServer file changes, and the installer checks again before running anything. If an update from another version is unfinished, they stop and give the exact command: `sudo systemctl start nekoserver-maintenance-resume.service` (still the old version, since nothing was replaced), then run `sudo bash install.sh` again. The version check inside the update itself stays.
- **The dashboard and qBittorrent's peer port cannot be the same.** A custom `dashboard.port` equal to `media.torrent_port` passed both checks, and one of them could not start. Both checks now refuse it.

### Hardened (from a review of failure modes)
- **A database copy cut off by a full disk is never kept.** If the disk fills up while the Immich database copy is written, a program in between can still report success. The nightly database copy and the manual Immich snapshot now require `pg_dump`'s own end-of-dump line; a cut-off copy is removed and the job fails. (Monthly snapshots and file copies already failed on a write error and are checksummed.)
- **A system clock jump never deletes the newest database copy.** Copies are named by their time and the oldest are removed by name; after a clock jump the copy just made could sort as "oldest". The copy just made is now never removed, and a newest copy dated in the future marks the USB backup incomplete with a hint to check the clock.
- Containers that do not stop cleanly were already covered: monthly maintenance and nightly app snapshots allow 60 seconds, then refuse to continue if any container was killed (exit code 137), ran out of memory or is still running.

### Changed
- Version 0.24.6; the release folder is now `nekoserver-v0.24.6`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.24.5 — 2026-10-19

### Fixed (from an outside review)
- **The USB space check counts damaged photos that the weekly job will replace.** Since v0.24.4 the weekly mirror keeps a damaged USB copy aside and writes the archive file again in full, but the estimate still counted a same-size copy as already done (reproduced: 0 bytes estimated while the backup kept the damaged file and wrote a replacement; with large videos this could run the drive out of space unexpectedly). Photos copied from the HDD archive are now estimated with the mirror's own rules: a file named by a current-version note is compared by checksum every week, every file is compared when the 4-weekly full check is due, and a copy that does not match counts its full size.
- **Restoring the password files checks the snapshot's checksum first.** `backup_secrets.py --restore` (and the recovery menu's password option) checked only that the encrypted archive existed. Encrypted data does not reveal damage on its own: a damaged archive decrypted "successfully" into wrong passwords (reproduced). The checksum recorded in the snapshot is now compared before anything is decrypted or created; a damaged archive, or one without a recorded checksum, is refused and nothing is restored. (`monthly-recovery.py` already did this.)
- **Restarting apps after an interrupted backup waits for the drives and the shared lock.** `app-resume` and `host-resume` (run at boot after an interrupted nightly backup) restarted containers and Samba and removed their notes before checking the drives, and without the lock monthly maintenance holds (reproduced with the bundled runtime: with the lock held and the wrong drive mounted, they still started everything). They, the nightly app and host backups, the photo archive and database jobs and the music backup now all check the drives, take the shared lock, check the drives again and only then resume anything. Tests cover the check and the order.
- **A nightly app snapshot is published only if it matches what was listed.** File sizes were recorded when the files were listed; a file rewritten before it was copied (installer and settings files can have other writers while the apps are stopped) was copied in its new form under the old size, the job passed, and the restore check later rejected the snapshot (reproduced: 1 byte listed, 18 copied). Everything is now listed again after copying, and the staged copy is checked file by file (size and checksum) against its own list before it is published. If anything changed, nothing is published and the job fails clearly; it runs again the next night.
- **The dashboard cannot use an app's port.** `dashboard.port` accepted 8088, which Nextcloud uses, so one of them could not start. The dashboard and media import checks now share one list of the ports the apps use (Nextcloud and the private Tailscale addresses included).

### Changed
- Version 0.24.5; the release folder is now `nekoserver-v0.24.5`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.24.4 — 2026-10-19

### Fixed (from an outside review)
- **A damaged USB copy is replaced, never left as the "current" version.** When a photo's copy on the USB drive was damaged (reproduced: `AAAA` → `BBBB`, then the USB copy of `BBBB` changed to `ZZZZ`), the weekly job saved the good content under another name while the current-version note still pointed at the damaged file, and "newest only" recovery restored `ZZZZ`. The HDD archive never changes a file it has saved, so a USB copy that differs from it is damaged: it is now moved to `neko-damaged/<time>/` on the USB drive (kept, never deleted, and outside the folders recovery reads) and the verified archive file is copied back under its own name. Before the run counts as done, every file a current-version note names is compared by checksum with the archive and replaced if it differs, so this is caught every week, not only in the 4-weekly full check.
- **The USB space check includes the whole photo archive.** It compared the Immich library on the SSD with the USB drive, but the weekly job copies the HDD archive, which also keeps photos deleted from Immich and every saved version (reproduced: 0 bytes estimated while a kept photo was still to be copied). It now compares the HDD archive with the USB drive, and adds the photos on the SSD that the archive does not hold yet (they reach the USB drive the same week). The room-to-grow margin uses the archive's size when it is larger than the library.
- **Hardware video setup waits for backups and monthly updates.** It wrote Immich's graphics settings and restarted Immich without the lock that backups and monthly maintenance hold, so a rerun of the installer could restart Immich while maintenance needed it stopped for a consistent snapshot or a rollback (reproduced: with the lock held it still wrote the settings and started Immich). Recreating Immich and switching Jellyfin and Immich over now happen while holding that lock, after checking the drives; when a backup is running, an unattended run stops with a clear message and an interactive one waits for it.
- **Phone alerts no longer announce a recovery without proof.** The list of things that need evidence before "RECOVERED" is sent left out Nextcloud, monthly maintenance and the music backup, so a failed job that was merely running again, or Nextcloud with an unreadable state, produced a recovery message (reproduced with the bundled runtime). The list now comes from the same app and job lists the dashboard uses, and recovery is only announced after a passed job or a running, not unhealthy app. A test checks every app and job.
- **A manual Immich snapshot records the version that made its database.** `recover.py snapshot --apply` read the Compose settings, the Immich version, the exact images and the running services before taking the lock that monthly maintenance holds. If an update finished in between, the snapshot paired the upgraded database with the old version and images, and still passed its checks (reproduced: database from v2.0.0, snapshot recorded v1.0.0). All of that is now read, and the drives checked again, only after the lock is taken.

### Changed
- Version 0.24.4; the release folder is now `nekoserver-v0.24.4`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.24.3 — 2026-10-19

### Fixed (from an outside review)
- **A failed monthly update can roll back with encryption on.** Since v0.24.2 each app's `.env` and Nextcloud's `config.php` go only into the encrypted part of the snapshot, but the automatic rollback restored from `data.tar` alone, so a failed update could leave Nextcloud without its configuration and the apps without their passwords. While an update runs, a root-only copy of those files is now kept on the data SSD (where the live files already are, never in the portable snapshot). The rollback checks it against its checksum and puts the files back before the old apps start, unattended. The copy is removed when the update or its rollback is over. If it is missing or damaged, the rollback stops and points to `monthly-recovery.py --secrets`. A test runs a failed update with encryption on.
- **"Restore the newest version" restores the content you last had.** After an edit was undone (`AAAA → BBBB → AAAA`), the backup correctly made no new copy, but recovery picked the dated `BBBB` because it had the latest date. Version dates also came from the file's own timestamp, which can be set back. When a file has saved versions, the backup now keeps a small hidden note (`.name.ext.neko-latest`) naming the copy that matches the file as the backup last saw it, and recovery restores that one. New version dates are the time the backup saw the change. The weekly USB job mirrors the HDD archive's notes as they are. Backups made before this release have no notes; recovery then uses the newest date, as before.
- **The USB drive's Immich database copy is checked.** A missing database copy, one more than 8 days old, or one made by an older release without a note now marks the weekly USB backup **incomplete** on the dashboard (it passed before). Each database copy now gets a `.sql.json` note when it is made, with the Immich version that made it (not the version installed when the USB copy runs), the time and its checksum. The checksum is checked before copying to the USB drive (a damaged copy is never copied) and a damaged USB copy is replaced. `IMMICH-VERSION.txt` names the version of the newest copy.
- **No database password left behind by an Immich update.** The update's new Immich settings, `.env` included, were written into the snapshot folder on the HDD and stayed there in plain text. They are now prepared in the private SSD work folder and removed after the update; copies left in older snapshots are deleted the next time maintenance runs.
- **A saved USB drive must be a separate disk too.** The drive picker refused another partition of the media HDD or the system disk, but a drive already named in the settings skipped the picker. Setup now refuses it when it is plugged in, and the weekly job checks before every run (through partitions, encryption and LVM) that the USB drive shares no disk with the system, the SSD or the HDD.
- **The USB space check counts same-size edits.** It treated a file as already copied whenever the sizes matched. It now decides like the backup itself: a copy of the same size written after the file last changed counts; otherwise the contents are compared (including saved versions), so an edit of the same size needs room again.
- **Turning Nextcloud off waits for backups and monthly updates.** Stopping a disabled Nextcloud skipped the lock that monthly maintenance and the backups hold, so it could stop Nextcloud during an update, fail its health check and trigger a rollback that started Nextcloud again. It now waits for that lock, and if an unfinished update is waiting to be resumed (which would start Nextcloud again) it says so instead of stopping it.

### Changed
- Version 0.24.3; the release folder is now `nekoserver-v0.24.3`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release.

## v0.24.2 — 2026-10-19

### Fixed (from an outside review)
- **`install.sh` exited silently before setup.** It called `df -P --output=avail`, which GNU df rejects; with `set -euo pipefail` and the error hidden, the installer stopped without a word. It now uses `df --output=avail`, and if the space cannot be measured it says so plainly and continues.
- **Weekly backups could miss a replaced file.** A copy was trusted when its size matched and it was written after the source's *modified* time, which tools that preserve timestamps can set back (reproduced: `AAAA` backed up, replaced by `BBBB` with the old timestamp, nothing copied). The check now uses the source's *status-change* time (ctime), which any rewrite or replacement moves forward and which cannot be set back. In addition, every 4 weeks the archive and USB jobs compare every existing copy by checksum.
- **Turning on the dashboard's Tailscale address broke the dashboard service, local access included.** The service passed `--public-host`, which the runtime's option table did not accept. The option is registered.
- **Incomplete USB backups were reported as successful.** When Nextcloud files were skipped (in use) or refused (names exFAT cannot store), the weekly job still recorded success. It now records **partial**, with counts and up to 50 file names; the dashboard shows *Incomplete* with the files, phone alerts warn, and it does not count as a full success.

- **Password files are no longer in plain text in monthly snapshots.** Encrypting `APP-CREDENTIALS.txt` left other copies readable: each app's Compose `.env` (database and Nextcloud admin passwords) and Nextcloud's `config.php` in `data.tar`, and the resolved app definitions (`*-old.json`, `*-new.json`, `*-rehearsal.json`, `result.json`). The `.env` files and `config.php` now go only into the encrypted `secrets.tar.cms` (left out of `data.tar`), and the saved definitions have their password values blanked once the update or its rollback is over (they are needed until then). Snapshots made before this release stay as they were until retention removes them. Without a backup key the files stay in `data.tar` so the snapshot remains restorable.
- **The USB drive can rebuild Immich on its own.** It held the photos but not the database, so albums, faces and users needed the SSD or HDD. The weekly USB job now also copies the newest Immich database copy (the one made that night; the newest 4 are kept) and `IMMICH-VERSION.txt` with the Immich version that made them (never the database password). RECOVERY.md has a "Rebuild Immich from the USB drive" section.
- **The recovery wizard described file versions backwards.** It called the dated `.neko-<date>` copies "older versions"; they are the later edits. It now offers to restore only the newest version of each file under its own name (the default), shows the date of each file that had several versions, and explains the naming correctly when every version is copied.
- **Every restore checks free space first.** Nightly snapshot restores, monthly snapshot unpacking and loading saved app versions now use the same check as photo and Nextcloud restores (refuse if less than 5 GB or 5% would be left free).
- **Nightly app and host backups use the configured timezone**, like every other schedule (they ran in the system's own timezone). A test checks every generated timer.

- **The USB backup must be a separate physical disk.** The picker excluded the SSD's and HDD's own filesystems but not other partitions on the same disks, so a second partition of the media HDD (or of the system disk) could be chosen, and one disk failure would take both copies. Only partitions on a disk that holds neither the system, the SSD nor the media HDD are offered now.
- **Phone alerts cover Nextcloud and monthly maintenance.** Notifications used a list of known alert names that left out `app:nextcloud`, `backup:maintenance`, `backup:music` and the new major-update notices, so those were shown on the dashboard but never sent. A test now checks every monitored app and job has a notification.
- **The Tailscale acceptance check accepts the installer's own routes.** It compared against the apps only, without the dashboard route the installer adds, and failed. Installation and the check now use one route calculation (`tailnet_routes`).
- **Failures say why and where.** The runtime printed one generic message and dropped every command's error output. It now keeps a short, sanitized reason (anything that looks like a password or key removed) and the step it was on ("copying photos and videos to the USB drive"), in the job report, on the dashboard ("Failed while …: …") and in its error output.

- **Old full snapshots on the USB drive are no longer removed automatically.** Releases before v0.21.1 wrote full monthly snapshots (photos and Nextcloud files included) to the USB drive, and the weekly job removed them once current backups existed, without checking that their files survive elsewhere. A file deleted from the server long ago could exist only there. The weekly job now keeps and lists them; the recovery menu (`sudo bash install.sh --recover`, "Old full snapshots on the USB drive") shows their dates and sizes, offers to copy them to the HDD first, and removes them only when you type DELETE. The automatic cleanup code is gone.
- **qBittorrent's music category must point to the music download folder.** Setup accepted an existing `music` category saving to `/data/downloads` (on the HDD, where Lidarr does not look) and reported success. Music must save to `/data/downloads/music` (on the SSD); a wrong but empty music category is corrected, one with torrents stops setup with instructions. TV and movies may still use the shared `/data/downloads`.
- **Turning Nextcloud off stops an installed Nextcloud.** `"enabled": false` only skipped installation, so an existing Nextcloud, its database, Redis and cron kept running and restarting. Its containers are now stopped (files and database kept; they stay stopped after a reboot), and turning it back on starts them again.

- **`--config` reaches the wizard and recovery.** `sudo bash install.sh --recover --config FILE` (and `--wizard --config FILE`) accepted the option but used the settings next to the installer, so recovery could look for backups on the wrong drives. Both now use the chosen file; without `--config` they use `my-config.json` as before.
- **Nextcloud acceptance results no longer turn the dashboard's acceptance panel to "unknown".** Python writes a manual check named `cloud_client` that the Rust dashboard did not know, so it treated every cloud/full report as invalid. It is known now, and a test checks that every manual check Python can write is known to the dashboard.
- **A pending app login can be corrected.** If creating an app's account did not finish and you completed the app's setup yourself with another login, the account tool kept retrying the saved login and never offered to change it. It now offers to enter the login you used; that switches the app to "existing account", so it is never created or reset.

- **The USB space check no longer counts old backup versions as free room.** It added everything already on the USB drive to its free space, as if those files could be reused. The backup only ever adds: earlier versions stay, and a changed file needs its full size again. With 100 GB of new files, 30 GB free and 150 GB of earlier versions, it gave no warning. It now adds up what the next weekly copy still has to write (files not on the drive yet, files whose copy differs, and this week's Immich database copy) and compares that, plus room to grow, with the free space.
- **An interrupted first setup of Immich or Nextcloud now finishes on the next run.** Each writes several settings files; stopping between them left a half-written set that every rerun refused ("Incomplete … deployment"). `compose.yaml` is now always written last, so it marks a finished step. A rerun that finds only the earlier files completes the step: Immich keeps the `.env` it wrote (its version and database password); Nextcloud writes its files again, since nothing has started yet. Half-written sets left by older releases are completed the same way as long as the app has no database yet; once it has one, the files are still kept for inspection.

- **`--config` paths are checked before they are read.** The installer, the setup wizard and recovery now accept only a `.json` file, with no `..` and no symlinks along the path (flagged by SonarCloud as a possible path traversal).

### Added: tests of how the pieces connect
- `tests/test_integration.py` runs the real `install.sh` preflight with the real `df` (only pacman, network tools and system identity stood in for), and checks every runtime command the installer writes into a systemd unit (22 commands) against the runtime's own option table, and against the bundled binary whenever it matches its sources. Both reported blockers fail these tests.

### Fixed
- **Dashboard app buttons work on the phone.** Opened over Tailscale, the dashboard's app buttons still pointed to `http://127.0.0.1:…`, which on a phone means the phone itself. They now open each everyday app's private Tailscale address (Jellyfin, Seerr, Immich, Navidrome, Nextcloud); the admin apps, which are only reachable on the server, say "Opens only on the server itself" instead of failing.

### Changed
- Version 0.24.2; the release folder is now `nekoserver-v0.24.2`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (version change only).

## v0.24.1 — 2026-10-19

### Added
- **Dashboard on your phone.** The read-only dashboard (drives, apps, backups, alerts) now also has a private Tailscale address, `https://<server>.ts.net:8448/`, on your own devices only (Tailscale Serve, never Funnel). It is on the phone setup page and the finishing screen. The dashboard server accepts only its loopback names and this server's own Tailscale name, and has no way to change anything. `"remote": false` under `"dashboard"` keeps it on the server only.
- **Hardware video in Jellyfin and Immich.** A new step, *Video using the graphics chip*, finds the graphics chip, installs its video drivers, checks from inside Jellyfin that the chip really decodes video, then turns on hardware transcoding: Quick Sync on Intel, VA-API on AMD, in both Jellyfin and Immich (Immich's server also gets the graphics device, kept across monthly updates in a `hardware.yaml` next to its Compose file). A setting you already chose in either app is kept; without a usable chip (or with NVIDIA) both apps keep working in software. See HARDWARE-VIDEO.md.
- Shared `gpu.py` for graphics detection (TV and hardware video).

### Fixed
- **Music no longer stays "unfinished" without Firefox.** Its optional desktop shortcut needed Firefox, which only the TV setup installed; with the TV extra turned down, music (and the finishing screen) stayed "needs setup" forever. A missing browser now just skips the shortcut; music works in any browser and on phones.

### Changed: less duplicated code (same behaviour)
- `app-recovery.py` and `host-recovery.py` were two near-identical 140-line copies. Both now share `snapshot_restore.py`; the commands and their output are unchanged, and the restore logic is covered by tests for the first time.
- One yes/no question (`guided_setup.yes_no`) replaces five copies (setup wizard, space check, recovery wizard, optional extras, guided steps).
- One Arch/Nyarch check (`neko_common.arch_host`) replaces three (Tailscale, TV, recovery); it also tolerates a missing or unusual `/etc/os-release`.
- One drive-list command for all drive pickers.
- Rust: one monthly-snapshot listing for maintenance, the weekly USB job and the dashboard, and one Immich database-copy name check.
- Version 0.24.1; the release folder is now `nekoserver-v0.24.1`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).
- Existing servers: add `"hardware_video"` after `"accounts"` in the `full` profile of `my-config.json` (or start from the new template), then run `sudo bash install.sh`. The dashboard's Tailscale address is added on that run too.

## v0.23.3 — 2026-10-19

### Fixed
- **Old full snapshots on the USB drive were removed once an HDD snapshot merely existed.** The weekly job now also checks that snapshot's archives against their checksums before removing them; a missing or altered archive keeps the USB snapshots.
- **The Seerr permission check still assumed user 1000.** Since v0.23.2 Seerr's folder belongs to your user, but the readiness check still checked it as 1000:1000 and reported a false "needs setup" for other users. It now checks the configured user, like every other app.
- **The system disk is checked before the system update.** The update and packages go on the system disk, before the drives are picked; with less than 5 GB free there, `install.sh` now stops before changing anything and says how to free space. (The SSD and HDD are still checked after you pick them, before any app is installed.)
- **Monthly retention could delete the last good snapshot.** Snapshots beyond the newest 3 were deleted without checking that the 3 kept are intact. If any kept one fails its checksum check, nothing older is deleted that month, and the log says why.
- **Running the installer during a night-time backup no longer fails with "Another recovery operation is active".** It waits for the backup (up to 30 minutes, and says so), then explains what to do if it is still running.
- The home-network step and the README now say to reserve the server's address in the router, so the home addresses keep working after a restart.
- Finishing screen: a list join no longer uses `sum()` (SonarCloud).

### Changed
- Version 0.23.3; the release folder is now `nekoserver-v0.23.3`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).

## v0.23.2 — 2026-10-19

### Fixed (from an outside review)
- **A monthly backup's SSD copy could be deleted while the HDD copy was missing or damaged.** Old SSD copies of monthly snapshots were removed when their manifest matched the HDD's, without checking the HDD archives themselves. Now the SSD copy is removed only when the HDD run is complete (restore-tested) and every HDD archive matches its checksum. Otherwise it stays, as possibly the only good copy.
- **A failed drive space check did not stop the installation.** Docker, the apps, Immich, Nextcloud, backups and maintenance still ran after the check failed (or you answered no). Every step that writes to the drives now waits for it; Tailscale, which does not, still runs.
- **Seerr could not use its settings folder with a user other than 1000.** It runs as your user (`media.uid`/`gid`), but its folder was created for 1000:1000 with mode 0750. The folder is now created for your user, and an existing one owned by 1000:1000 is handed to your user (that folder only; links are not followed).
- **A correct nightly app-backup setup failed its readiness check.** Setup writes the retention setting into the backup's private settings, but the check expected them without it and reported "needs setup". Both now use one shared definition.
- **The finishing screen could say "ready" with missing or unreadable state.** It now checks every step the installation is meant to run; a step with no record counts as unfinished.
- **The USB space check used the drive's size, not its free space**, so a large but nearly full drive passed. It now measures free space, plus what an earlier weekly copy already holds there (opening the drive read-only if needed).

### Fixed
- **No stale "newer Nextcloud" warning after an approved upgrade.** In the month an approved Nextcloud major was installed, maintenance first recorded "Nextcloud 32 is available (you have 31)" and the dashboard kept showing it for a month. The check is now skipped in the month a major is being installed.
- **Approving an Immich major survives a fix release.** The approval had to be the exact release (`"v3.0.1"`), so it stopped matching if Immich published v3.0.2 before the update night. You can now approve the major itself (`"immich_major_update": "v3"`); an exact release still works.
- **Acceptance checks leave out extras you turned down** (TV, phone alerts, outage alerts), so they no longer stay "not done" on the dashboard.
- **Answering "no" at the wizard's first question no longer says "Nothing was changed"**: by then the system update and packages are already installed, and it now says so.
- Docs: a big Nextcloud can take longer than the 5-minute health wait to upgrade; MONTHLY-MAINTENANCE.md explains raising `health_wait_seconds`.

### Changed
- Version 0.23.2; the release folder is now `nekoserver-v0.23.2`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).

## v0.23.1 — 2026-10-19

### Changed: install order
- **The system is updated first.** `sudo bash install.sh` now runs a full `pacman -Syu` (with the Arch keyring first, as Arch recommends) before installing anything, so the apps and their dependencies install at their current versions. If the update installed a new Linux kernel, it stops and asks you to restart, then run the same command again: Docker networking and Tailscale need the running kernel's modules. The recovery wizard (`--recover`) skips the update.
- **Every system package the apps need is installed up front**, before the wizard starts: `docker`, `docker-compose`, `tailscale`, `caddy`, `samba`, `hdparm`, `smartmontools`, `qrencode`, `xdg-utils`. A missing package now stops the setup at the start instead of halfway. (Installing Docker does not start it; the Docker step still sets it up on the SSD.)
- **Tailscale comes right after the system check**, before the drives and apps. The Tailscale addresses for the apps are set up before the home-network addresses.
- **The USB backup drive is chosen with the SSD and HDD**, so the early space check covers it. Before, it was only chosen at the weekly-backup step near the end, and the USB check never ran on a first install.

### Fixed
- **Weekly photo and Nextcloud copies no longer re-read the whole library.** They only ever copied missing files, but checked every existing copy by reading the photo and the copy in full (SSD and HDD, then HDD and USB) each week. A copy with the same size, written after the photo last changed, is now skipped without reading it; new and changed files are still copied and checksum-verified.
- **New major versions of Immich and Nextcloud are no longer silent.** Monthly maintenance recorded a new Immich major only inside the snapshot folder, and never checked Nextcloud at all, which stays on its installed major. Both now show on the dashboard, and you can approve one in `my-config.json` (`immich_major_update`, `nextcloud_major_update`); the next monthly update installs it with the usual snapshot, restore test and automatic rollback. Nextcloud is moved one major at a time.
- **Drives the desktop already opened (`/run/media/...`) can be chosen as the SSD or HDD.** Before, a freshly plugged-in drive was missing from the list.
- **Drive prompts no longer mention "1TB" and "12TB"**, and you confirm a drive by typing its short name (such as `sdb1`) instead of its long UUID.
- **Missed weekly backups run at the next start-up** (the PC was off on Sunday night) instead of being skipped for that week.
- Samba explains the password it asks for.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (Rust sources changed).
- Existing settings keep their old step order (it still works); the new order applies to new settings files.

## v0.23.0 — 2026-10-19

### Fixed
- **Running the installer again no longer repeats finished steps.** The Tailscale HTTPS step is skipped when MagicDNS and HTTPS are already on, the phone walkthrough is skipped when it was already confirmed for the same addresses (the QR page is still refreshed), and TV hardware tests already confirmed are not asked again.
- **Optional extras can be turned down.** TV setup, phone alerts (ntfy) and the outage monitor were part of the main installation with no way to say no, so servers without them stayed "almost ready" for good and were asked again on every run. Each is now asked once; `no` is saved as `"wanted": false` and counts as done. Change it to `true` to set one up later.
- **The drive space check no longer blocks a re-run.** Its minimums are for a new install; once Docker is on the SSD, low space is reported but nothing is stopped.
- **The phone steps and the QR page show the real password-list path** on your data SSD instead of always `/srv/nekoserver/...`.
- **The recovery wizard never shares the USB drive with the weekly backup** (it takes the same lock, and says to try later if the backup is running), so the backup cannot unmount the drive in the middle of a restore. It also checks free space first and refuses a restore that would leave less than 5 GB (or 5%) free.
- **TV setup installs the right Vulkan driver** for Intel or AMD graphics (it always installed the Intel one). With NVIDIA only, it points to the Arch wiki instead of guessing a driver.

### Changed
- Version 0.23.0; the release folder is now `nekoserver-v0.23.0`.

### Upgrade notes
- The bundled runtime must be rebuilt for this release (the version changed; the Rust sources did not).
- The first run after upgrading asks the three optional questions once (unless the extra is already set up).

## v0.22.0 — 2026-10-19

### Added
- **Setup wizard: `sudo bash install.sh`.** One command, no options. It explains what will happen, creates `my-config.json` by itself (your timezone and user filled in), runs the whole installation with every guided pause, and continues where it stopped when run again.
- **Early drive space check**, right after the drives are chosen and before anything is installed. It stops with a plain explanation if the SSD or HDD is too small (under 20 GB / 50 GB free) and warns, with a yes/no to continue, below the recommended sizes or when the USB drive is too small for the photos and Nextcloud files.
- **Tailscale in the main installation.** The wizard pauses to explain Tailscale, opens the sign-in page, and turns on secure addresses. Jellyfin, Seerr, Immich, Navidrome and Nextcloud get private addresses that work anywhere on your own devices: Tailscale Serve, never Funnel, no router changes. The admin apps stay on the server.
- **Phone setup, app by app, with a QR-code page.** For each app: which phone app to install (iPhone and Android), what to type, and which username to use. Passwords are never shown; the steps say where they are. The page is at `/usr/local/share/nekoserver-pages/phone-setup.html`.
- **Finishing screen**: what's done, what still needs you (in plain words), your home and Tailscale addresses, where the passwords and phone page are, and what to do next.
- **Recovery wizard: `sudo bash install.sh --recover`.** A menu for photos, Nextcloud files, the password list, app settings, Steam/Samba settings or a monthly snapshot. It lists backups by date, restores into a new folder only, and explains how to put things back.
- `"timezone": "auto"` (the new default) uses the PC's own timezone.

### Fixed
- **Old monthly snapshots on the USB drive are removed only when it is safe.** Before, they were removed after any weekly run, even one where Nextcloud files were skipped (in use, or names exFAT cannot store). Now the weekly job keeps them unless this week's copies were complete and a restore-tested monthly snapshot (plus, if they covered Immich, an Immich database copy) is on the HDD. It logs why it kept them.

### Changed
- Version 0.22.0; the release folder is now `nekoserver-v0.22.0`.
- The Tailscale and phone steps are rewritten in plain language.

### Upgrade notes
- **Existing servers:** add `"capacity"` after `"storage"`, and `"tailscale"`, `"gateway_tailnet"` after `"gateway_lan"`, in the `full` profile of your config. Or start from the new template; the wizard creates one if `my-config.json` does not exist.
- The bundled runtime must be rebuilt for this release (Rust sources and version changed).
- A monthly update interrupted *during* the upgrade from 0.21.1 cannot be resumed by 0.22.0 (it refuses a journal from another version); finish or resolve it with 0.21.1 first.

## v0.21.1 — 2026-10-19

### Changed: what is backed up where
- **USB drive: only add-only copies of Immich photos/videos and Nextcloud user files.** The weekly USB job now also copies each Nextcloud user's own files (not previews, trash or caches). Monthly maintenance no longer writes to the USB drive or needs it connected; the full monthly snapshots older releases put there are removed by the weekly job once its own copies succeed.
- **HDD: software, OS and photos.** Nightly app-state and host-state snapshots, the weekly add-only photo copy and Immich database dump, and the monthly pre-update snapshot of app settings, databases and container images. The monthly snapshot no longer contains the Immich photo library or Nextcloud files; its restore test checks the Immich and Nextcloud databases by SQL instead of starting those two apps.
- **Add-only copies never fail on a changed file.** A file that changed since it was copied (an edited document, an updated `.xmp` photo sidecar) used to fail the whole job ("Collision or changed original"). It is now kept beside the old copy as `name.neko-<date>.ext`; nothing is overwritten or deleted.

### Fixed
- **Lidarr imports no longer copy music.** Lidarr now mounts the SSD library once at `/data`, so `/data/downloads/music` and `/data/music` share one mount. Previously they were separate bind mounts, so imports failed to hardlink/move (EXDEV) and fell back to copying, doubling SSD use while seeding. Container paths and app settings are unchanged; mount checks in `acceptance.py`, `media-doctor.py`, `media-health.py` and the Rust runtime were updated to match.
- **Seerr honours `media.uid` / `media.gid`** instead of a hardcoded `1000:1000`.
- **Nextcloud works on both gateways.** Applying `remote_access` after the LAN gateway replaced Nextcloud's `overwritehost` with the Tailscale name, breaking Nextcloud on the home network for devices without Tailscale. Gateways now only add their address to `trusted_domains`; a gateway-set `overwritehost` from v0.21.0 is removed. The Docker hop is added to `trusted_proxies`, so Nextcloud sees real client addresses for login throttling and logs.
- **Monthly maintenance no longer fills the SSD and blocks itself.** Each month it copied the full snapshot (all photos, Nextcloud files, images) onto the SSD as well, and never deleted any, so the free-space check soon failed and container updates stopped. Snapshots now go to the HDD only, old SSD copies are reclaimed once the same run is on the HDD, and the newest `maintenance.keep_snapshots` complete snapshots (default 3) are kept.
- **Nightly app backups failed every night with the default apps.** qBittorrent's SSD music-downloads mount (and now Lidarr's SSD library) counted as "unmanaged writable state". The SSD library is media, not app state, and is now allowed.
- **Nightly app and host backups no longer abort on one odd file.** Symlinks, sockets, FIFOs, devices and nested filesystems are skipped and listed in the manifest; hard-linked files are copied normally.
- **Monthly updates no longer require a configured USB drive.** Maintenance still listed the USB photo backup as a required module, so without a USB drive set up the installer blocked it and monthly updates never ran.
- **Overlapping night jobs no longer fail each other.** The weekly photo backup, monthly maintenance and nightly app/host backups share one lock, which failed immediately when busy: the Sunday app backup failed whenever the photo job ran long, the nightly backups failed on maintenance nights, and maintenance was skipped for the month when the 1st was a Sunday. Scheduled jobs now wait up to 3 hours (app/host backup service limits raised from 2 h to 6 h to allow for it).
- **Weekly Immich database dumps no longer pile up forever.** Each week added another full `.sql` dump to the SSD and HDD (often hundreds of MB to GBs). The newest `photos.database_keep` dumps (default 8, about two months) are kept on each drive; `0` keeps all.
- **Jellyfin starts on machines without a GPU.** `/dev/dri` is only passed through when it exists.
- **Weekly Nextcloud copy to an exFAT USB drive** skips (and lists) files whose names exFAT cannot store, such as `Meeting 10:30.docx`, instead of failing the job.
- **Fresh Nextcloud installs avoid a brand-new major** (x.0.0/x.0.1) and install the previous major, since the Docker image can lag the release.
- **Password restore** accepts only absolute snapshot/destination paths without `..` or symlinks, and checks the snapshot is a real monthly snapshot (SonarCloud findings).
- CI: actions pinned by commit, Rust pinned to 1.99.1, PyYAML installed by hash from wheels only.
- **Timezone is configurable.** A new top-level `timezone` config key drives container `TZ`, Immich's `.env` and every systemd timer. It falls back to the old `media.timezone` / `maintenance.timezone` keys, then the host's `/etc/localtime`, then UTC. The default stays `Asia/Jerusalem`.

### Removed
- **Python fallback backend.** Every scheduled job and background service runs the bundled Rust program. Configs that say `runtime.backend: python`, or have no `runtime` setting, are treated as Rust; rerun the installer once to rewrite their units. Deleted: `photo-preserve.py`, `photo-preservation-job.py`, `photo-backup.py`, `photo-weekly.py`, `immich-db-backup.py`, `job_status.py`, `modules/_runtime.py`, `dashboard_server.py`, `gateway_guard.py`. `dashboard_collect.py`, `storage-guard.py` and `media-health.py` stay: they are libraries or manual tools.
- **Unused tools:** `media-doctor.py`, `migrate-docker.py` (referenced by nothing).
- **Historical docs and old test output:** `AUDIT.md`, `INSTALLATION-REVIEW.md`, `PLAN-REVIEW.md`, `RELEASE-CONTENTS.md`, `audit-evidence/`, and old version notes at the top of several docs. Tests now run in CI instead.
- Unused imports and one unused variable.

### Added
- **App password list encrypted in monthly HDD snapshots** (it is never written to the USB drive), opened with your sudo password (`backup_secrets.py`). The installer asks for the sudo password once and checks it; nightly runs need no password. Restore with `monthly-recovery.py --secrets` or `backup_secrets.py --restore`.
- **Retention** for monthly (`maintenance.keep_snapshots`, default 3) and nightly (`app_backup.keep_snapshots`, `host_backup.keep_snapshots`, default 14) snapshots. `0` keeps everything. Incomplete runs and anything the worker did not create are never deleted.
- **Optional qBittorrent peer port** (`media.torrent_port`, default closed). It is the only port allowed beyond localhost, and the installer, monthly worker and acceptance checks refuse any other.
- Docs: device certificate checks for phones/TVs on the LAN gateway.
- Upgrade path: unedited v0.21.0 Lidarr and Seerr manifests are recognised and migrated (`previous_templates()` in `modules/media.py`), alongside the existing Navidrome migration.
- Regression tests: `tests/test_review_fixes.py`, `tests/test_gateway_cloud_origin.py`, `tests/test_backup_secrets.py`, and Rust tests for retention, encryption, skipped files, the peer-port rule, versioned add-only copies, the Nextcloud copy and USB cleanup.
- GitHub Actions CI: Python compile/lint/tests, `docker compose config`, `cargo fmt`/`clippy`/`test`, a check that the bundled runtime matches its sources, and a static musl runtime build that can commit the rebuilt binary.
- `.gitignore`.

### Upgrade notes
- The bundled `runtime/bin/nekoserver-runtime` must be rebuilt for this release (Rust sources and version changed). Run the CI workflow with **commit_runtime**, or `python3 runtime/build.py` as a normal user.
- Rerunning the installer upgrades unedited v0.21.0 Lidarr and Seerr manifests in place, keeps the old one as `compose.before-v0.21.1.yaml`, and recreates those containers. A manifest you edited by hand is left alone and reported, as before.
- Rerun the installer interactively once so it can ask for your sudo password and create the backup key. Until then, monthly backups leave the password files out (they are never stored in plain text).
- **Old backups are deleted.** On the first successful runs after upgrading: HDD monthly snapshots beyond the newest 3, nightly snapshots beyond the newest 14, Immich database dumps beyond the newest 8, v0.21.0's SSD copies of monthly snapshots, and (after the weekly USB copy succeeds) every old full monthly snapshot on the USB drive. v0.21.0 monthly snapshots contain the password list in plain text; the HDD ones go once they fall outside the retention count. Set the `keep_snapshots` values before upgrading if you want to keep more.
- Rerun the `photo_backup` module (or the full profile) so the weekly USB job picks up the Nextcloud copy. The first run copies every Nextcloud file and can take a long time.
- `maintenance.require_external` is no longer used.
- Reapply the gateways (`full`, then `remote_access`) to apply the Nextcloud fix.
- The installation guide PDF still shows v0.21.0 paths; substitute `v0.21.1`.
