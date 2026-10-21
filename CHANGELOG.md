# Changelog

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
