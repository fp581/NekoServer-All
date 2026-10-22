# NekoServer dashboard

Your server's home page: open your apps, see backups and drives, and watch the hardware (processor, memory, graphics, temperatures, fans, network) live, **set fan curves and RGB lighting**, and **restart an app or close a desktop app** (right-click it, as in a task manager). Nothing else on the server can be changed from it. When something needs a command, it shows the command for you to copy into a terminal window on the server (Konsole on KDE, Terminal on Ubuntu/GNOME).

## Open it

| Where | Address |
| --- | --- |
| On the server | **http://127.0.0.1:8787/**, or **NekoServer Dashboard** in the app menu (opens in its own window) |
| Phone, tablet or another computer | **https://yourserver.tailXXXX.ts.net:8448/** with Tailscale switched on (setup prints your exact address) |

The Tailscale address only works for devices signed in to your own Tailscale account (Tailscale Serve, never Funnel). To keep the dashboard on the server only, set `"remote": false` under `"dashboard"` in your settings and run `sudo bash install.sh` again. Connecting Tailscale after the dashboard was installed is fine: the next setup run gives the dashboard its Tailscale address. The port 8787 can be changed with `"port"` under `"dashboard"`; when another program on the server already uses it, setup takes the next free port from 8788-8799 by itself, writes it there and says so (the end of setup and the app menu entry show the right address).

### Use it like an app (no Chrome needed)

The dashboard can be installed as an app, so it opens in its own window or full screen without tabs or address bar, and gets the cat icon:

- **iPhone / iPad (Safari):** Share (□↑) → **Add to Home Screen**.
- **Android (Chrome, Edge, Samsung Internet):** menu ⋮ → **Install app** (or **Add to Home screen**).
- **Windows, macOS, Linux (Chrome, Edge, Brave):** the install icon in the address bar, or menu → **Install NekoServer**.
- **On the server:** the **NekoServer Dashboard** menu entry opens it in an app window when Chromium, Chrome, Brave, Edge or Vivaldi is installed; otherwise in your normal browser (Firefox can't install sites as apps).

Installing needs the secure Tailscale address (https) or the server's own address. Settings → *Use it like an app* shows the right steps for the device you're on.

## The pages

| Page | What you see |
| --- | --- |
| **Home** | Overall status, the three most urgent things, apps, storage, uptime and live processor, memory, temperature and network. |
| **Your apps** | Every installed app with its health, processor and memory use, and an Open link. Star favourites to keep them first. Right-click an app (on a phone, tap ⋯) to restart it. |
| **Backups** | Each scheduled backup, whether its files match their SHA-256 checksums, and whether a restore was tried. |
| **Hardware** | Processor (total, each core, clock, waiting work), memory, every graphics card and AI chip (NPU), network traffic, drive activity, batteries and UPS. Updates every 4 seconds. |
| **Sensors** | Every temperature sensor (grouped: processor, graphics, drives, motherboard…), fan speeds and fan outputs, power use and voltages. |
| **Fans & lights** | Fan curves by temperature, fixed speeds or the motherboard's own control for every fan output; finding which fan is on which output; colours, brightness and effects for RGB lighting. |
| **Processes** | What's running, including inside each app, with processor and memory use. Search and sort. Right-click a program (on a phone, tap ⋯) to restart its app, or to close an app opened on the desktop. |
| **Server health** | Drive space and health (SMART), **what starts by itself after a restart**, the outside heartbeat, phone notifications, app checks and waiting updates. |
| **Needs attention** | Everything to look at, most urgent first, each with "What should I do?". |
| **Help** | Step-by-step guides, the error codes below, installing it as an app, and a privacy-safe summary to share. |

On a phone the main pages are in the bar at the bottom; **More** opens the rest. On a tablet the menu becomes a slim icon rail. Settings (⚙) has a light theme and larger text (Large, or TV for across the room).

## Sensors are found automatically

There is nothing to configure. The dashboard reads what Linux reports:

- **Temperatures, fans, fan outputs, power, voltages:** every hardware-monitoring chip (`/sys/class/hwmon`): processor (coretemp, k10temp, zenpower), graphics (amdgpu, nouveau, i915, xe), NVMe drives, motherboard chips (nct67xx, it87, asus, gigabyte…), Wi-Fi cards and liquid coolers. **SATA drive temperatures are not read live:** on some hard drives that resets the spin-down timer, so an open dashboard could keep the media drive awake. They come from the drive health check instead (every 30 minutes, never waking a sleeping drive). If none report a temperature (some mini PCs, virtual machines), the system's thermal zones are used instead.
- **Graphics cards and AI chips:** every display adapter and NPU, named from the system's PCI list. AMD cards report their load and video memory; Intel graphics report their clock; NVIDIA cards are read through the NVIDIA driver once a minute.
- **Network and drives:** wired, Wi-Fi and Tailscale traffic; activity of every physical drive.
- **Batteries and UPS:** a laptop battery or a USB UPS.

A missing reading shows "—" or "No load reading", never a made-up 0. If a sensor you expect is missing, see Help → *A sensor is missing*. Most motherboard chips need `lm_sensors` (`sudo pacman -S lm_sensors` on Arch, `sudo apt-get install lm-sensors` on Debian/Ubuntu, then `sudo sensors-detect` and a restart). To see everything the dashboard can read on this computer:

```bash
/usr/local/lib/nekoserver-dashboard/v0.28.11/nekoserver-runtime hardware
```

## Fans & lights

Like a fan-control app, built in (and written in Rust, so it is quick and light):

- **Every fan output is found automatically** (each `pwm` output Linux can control on the motherboard chip, graphics card or cooler). Give one a name if you like ("Processor fan").
- **Three ways to run an output:** *Motherboard* (the BIOS stays in charge, the default), *Fixed* (one speed), or *Curve*: drag the points (2 to 8) to set the speed for each temperature, and choose which temperature it follows. *Silent*, *Balanced* and *Performance* are ready-made starting points. The yellow line shows the temperature now. Nothing changes until you press **Save**.
- **Detect fans** finds which fan is on which output: every fan spins up to full speed, then each output is slowed in turn while the fan speeds are watched (about a minute per output, and loud). Stop it whenever you like; it stops by itself if anything gets hot (85 °C, or near a sensor's own critical limit) or a temperature can no longer be read, and it won't start when no temperature can be read at all, or while a fan hasn't gone back to the motherboard. A fan whose speed can't be read is left out rather than guessed, and retrying it never pauses the temperature checks or Stop.
- **Safety:** speeds rise at once and fall gently (5% every 2 seconds), so fans don't hunt up and down. A controlled fan never goes below 20% (you can set a higher floor). Every controlled fan (curve or fixed speed) goes to full speed at 95 °C, 3 °C below a sensor's own critical limit, when its curve's temperature can't be read, or when no temperature can be read at all. A fan is only taken over when its own setting can be read, so it can always be put back; and before every change NekoServer checks the fan output still belongs to the same chip (Linux reuses hwmon numbers when devices come and go). If the control service stops for any reason, a crash included, every output is handed back to the motherboard: the motherboard's own setting is written down before a fan is changed, and kept until it has really been put back. A fan the motherboard won't take back runs at full speed (shown as *Not handed back*; if even full speed can't be set, the page says so) while NekoServer keeps trying. A damaged recovery record is never deleted: it is kept and the page asks you to check the fans. Switching fan control off in setup only finishes once every fan is back with the motherboard.
- **RGB lighting** goes through **OpenRGB**, which setup installs from the system's own repository when it has one (Arch does; Debian and Ubuntu may not, and lighting is then skipped) and runs as a service (answering this computer only). Each device gets a colour, brightness, an effect and on/off; *Apply to all* sets every device at once. Your choice comes back after a restart, also for a device that shows up later or was reset (NekoServer checks every 10 seconds that each device still shows its choice). Effects that make their own colours (a rainbow, say) can't be dimmed, so colour and brightness are switched off for them. Identical devices (two matching RAM sticks) are told apart by their serial number, or else by where they are connected (a slot address, never a path Linux may renumber after a restart). Memory and motherboard lighting need the i2c driver, which setup switches on; restart once after setup.

**Who can change them:** on the server itself, anyone using the dashboard. Phones, tablets and other computers (through Tailscale) can always look, but need the **PIN** chosen during setup to change anything; a device stays unlocked for 12 hours or until the page is closed. Five wrong PINs lock changes for 5 minutes, and each further five twice as long. A PIN has 6 to 8 digits (one chosen before 0.28.7 with 4 or 5 still works). Only a slow, salted hash of the PIN is stored (`/etc/nekoserver-control/pin.json`). To choose a new PIN: `sudo rm /etc/nekoserver-control/pin.json`, then run setup again in a terminal.

To leave fans and lights alone completely, set `"fan_control": false` under `"dashboard"` in your settings (or `"rgb": false` for lighting only) and run setup again: the motherboard takes over again. With `"rgb": false` NekoServer leaves lighting completely alone: it never reads or changes it, not even through an OpenRGB you run yourself.

## Restart or close apps

On a computer, right-click an app on **Your apps**, or a program on **Processes**, like in the Windows Task Manager. **On a phone or tablet, tap the ⋯ button** next to it (phones have no right-click). With the keyboard, use the ⋯ button or the context-menu key.

- **Server apps** (Jellyfin, Immich, Nextcloud and the others): **Restart app** stops the app and starts it again, all of its parts together and in order, then waits until it runs and answers again (up to 4 minutes). Its **database and cache keep running**: Immich restarts its server and machine learning, Nextcloud its web part and its background jobs. The card shows *Restarting…*, then the result.
- **Desktop apps** (anything opened on the server's own desktop, such as Firefox, Steam or Dolphin): **Close app** asks it to quit, with Linux's ordinary "please quit" signal. Many apps save or ask first, but some quit straight away and **unsaved work in them can be lost**, so Close asks first too; save your work in the app on the server if you can. A program started from another app (Firefox opened from a terminal, say) belongs to that app's group, and the menu says so: closing goes to the whole group, the terminal included. If it is still running after 10 seconds (it may be asking whether to save a file, or not responding), the page says so and offers **Force close**, which stops it at once. **Unsaved work in it is lost**, so Force close always asks first.
- **Protected, never stopped from here**: databases and caches inside apps, backups, the monthly update, system services, the desktop itself (panel, window manager, sound) and NekoServer. Their menu says so.

**Every one of these needs the PIN, on every device, the server itself too** (fans and lights don't need it on the server). The page asks for it the first time; it stays unlocked on that device for 12 hours or until the page is closed. Without a PIN (none chosen during setup), app controls stay off: run setup again in a terminal to choose one.

**Backups and updates come first.** While a backup or the monthly update runs (or one that was cut off still has to be finished), Restart app waits: the menu says which job is running, and the server refuses it too (E47). Only one app restarts at a time. If a restart is ever cut off part-way (the computer loses power, say), the parts it had stopped are started again once no backup or update needs the apps stopped (its note is kept in `/etc/nekoserver-control`).

**"Not responding" means the app's own health check failed**: Docker's health check where the app has one (Immich, Seerr), or the hourly app check that asks the app for a page (*App health checks* in Server health). A busy processor never counts: converting a video can rightly keep every core at 100%. Close a desktop app yourself if it seems stuck.

## Error codes

When something goes wrong, the dashboard shows a short code at the top of the page. "What does this mean?" opens this list.

| Code | Means | What to do |
| --- | --- | --- |
| E01 | Can't reach the server | Check the server is on. Away from home, turn on Tailscale on this device. |
| E02 | The server is slow to answer | It may be busy with a backup or update. The page keeps trying by itself. |
| E10 | Readings not ready yet | Normal for a minute after the server starts. |
| E11 | Readings could not be read | Usually fixes itself within a minute. If not, run setup again. |
| E12 | Readings are out of date | No new reading for two collection intervals plus 30 seconds (150 s at the default 60 s): Help → *Readings are old or missing*. |
| E20 | Live readings unavailable | They come back by themselves. If not: `sudo systemctl restart nekoserver-dashboard.service`. Until then, older live readings are dimmed and marked with their time. |
| E21 | Fan and light control unavailable | The control service isn't running: Help → *Fans don't change*. |
| E40 | Page not found | Use the menu to go back. |
| E41 | Open the dashboard from its own address | Use 127.0.0.1:8787 on the server, or the Tailscale address. |
| E42 | Request not supported | Reload the page. (For a fan or light change, the message says what was wrong.) |
| E43 | Enter your PIN to change this | Phones, tablets and other computers need the PIN chosen during setup for fans and lights; restarting or closing apps needs it on every device. Press *Unlock changes* (the page asks by itself for app controls). |
| E44 | Wrong PIN | Check the PIN and try again. |
| E45 | Too many tries; wait a while | Five wrong PINs lock changes for 5 minutes; each further five, twice as long. |
| E46 | The change could not be saved | Nothing was changed, because it wouldn't survive a restart. The server's system drive may be full or failing. |
| E47 | Not now: something else is using it | A backup, the monthly update or another app restart is running (or a cut-off one still has to be finished). Try again when it has. The message says which. |
| E48 | Protected: can't be stopped from here | Databases, backups, system services and the desktop itself are never stopped from the dashboard. Restart the whole app instead, or close only apps opened on the desktop. |

## Starts by itself

After a restart (or a power cut) everything comes back without you:

- the drives mount, then **Docker** starts (it waits for both drives) and starts every app again;
- **Tailscale** reconnects, with its phone addresses;
- the **dashboard** starts, takes its first readings 15 seconds after boot and every minute after that, and restarts itself if it ever stops.

Server health → *Starts by itself* shows each of these: whether it starts at boot and whether it is working now. A service that stopped with an error is never shown green, even when it starts at boot; Needs attention lists it (urgent for Docker, Tailscale, the dashboard and Samba), together with anything switched off.

## Install or update

The dashboard is part of the normal setup (the full profile installs it). To install or refresh just the dashboard:

```bash
sudo bash install.sh --profile dashboard --apply
```

Setup refreshes its service definitions and its menu entry. Its files are kept per release under `/usr/local/lib/nekoserver-dashboard/v0.28.11` (one folder per release, and when a setup run has finished, the folders of earlier releases that no service uses any more are removed; a changed file there stops setup rather than being overwritten). A menu entry or launcher you wrote yourself at the same place is kept.

## What runs, and what it can see

Three parts, all from the bundled Rust program (see RUST-RUNTIME.md), plus OpenRGB for lighting:

- **The collector** (`nekoserver-dashboard-collect`, root, every minute): drives, SMART, apps and their resource use, backups, checks and what starts at boot. It writes one public snapshot, `/run/nekoserver-dashboard/status.json`, with selected fields only (no passwords, no file lists). Private history and SMART state stay on the SSD; it refuses to read them when the SSD's identity is wrong.
- **The web server** (`nekoserver-dashboard`, an unprivileged throwaway user): serves the page, the snapshot, live hardware readings and the process list. It listens on 127.0.0.1 only, can't reach Docker, the app SSD or the settings, answers only its own addresses, refuses cross-site requests and never changes hardware itself: fan and light changes are passed to the control service. Live readings are taken only while a page asks for them.
- **The control service** (`nekoserver-control`, root, because only root may set fan speeds and stop programs): the only part that changes anything. It listens on a private socket that only the web server can use, checks every request (ranges, known outputs, the PIN; for app controls, which app or program it is and whether a backup runs), and its settings are kept in `/etc/nekoserver-control`. It runs whenever the dashboard is installed; with fan control switched off it never takes a fan over. When it stops, the fans go back to the motherboard; an app it is restarting is finished first.
- **OpenRGB** (`nekoserver-openrgb`): talks to the lighting; it can only be reached from this computer.

Process names are shown, never command lines, environments or user names. The page keeps favourites and settings in your browser only; the summary download in Help contains counts and versions, nothing private.

```bash
systemctl status nekoserver-dashboard.service nekoserver-dashboard-collect.timer nekoserver-control.service nekoserver-openrgb.service
sudo journalctl -u nekoserver-dashboard-collect.service -n 50
```

## What the readings mean

| Reading | Source |
| --- | --- |
| Processor | Time spent busy between two readings (`/proc/stat`), total and per core. 100% for a process = one full core. |
| Waiting work | Load average over 1, 5 and 15 minutes; below the number of threads means the processor keeps up. |
| Memory | Total minus available memory (`/proc/meminfo`). |
| Temperature on Home | Hottest processor sensor. Warnings at 80 °C, critical at 95 °C (monitoring limits, not chip limits). |
| Drives | Space from the mounted, identity-checked SSD and HDD; health from read-only SMART every 30 minutes. A sleeping drive is not woken, so its health can show "Not checked yet". |
| Apps | Docker containers by their NekoServer project. "Running" doesn't prove an app works; the app checks and the acceptance checklist do. |
| Backups | Each job's last attempt, last success and schedule. Checksums show whether copied files matched at the **last recorded check**; a check with no valid date, or older than two weeks, is not shown as passed. *Restore tried* counts only the witnessed restore tests for the backups this server makes (photos, apps, host and user files), and says which were tested. |

Warnings appear for missing drives, drives 90% full (critical at 97%), SMART problems, stopped apps, failed or overdue backups, damaged backup files, and anything that won't start after a restart.

## Browser alerts and phone notifications

Settings → *Turn on browser alerts* shows urgent problems as notifications while the page is open on that device. For alerts that reach your phone when nothing is open, set up phone notifications (NOTIFICATIONS.md); for an alert when the whole server goes quiet, the outside heartbeat (HEARTBEAT.md).
