# NekoServer dashboard

The dashboard is your server's home page. It shows your apps, your backups and drives, and the hardware (processor, memory, graphics, temperatures, fans, network) live. You can also **set fan speeds and RGB lighting**, and **restart an app or close a desktop app**. Nothing else on the server can be changed from it. When something needs a command, the dashboard shows the command for you to copy into a terminal on the server.

The technical details (what runs behind it, how sensors are read, fan safety) are at the end, under [For administrators](#for-administrators).

## Open it

| Where | Address |
| --- | --- |
| On the server | **http://127.0.0.1:8787/**, or **NekoServer Dashboard** in the app menu (opens in its own window) |
| Phone, tablet or another computer | **https://yourserver.tailXXXX.ts.net:8448/** with Tailscale switched on (setup shows your exact address, and the phone setup page has a QR code for it) |

The phone address works only on your own devices, signed in to your Tailscale account. Nothing is open to the internet.

### Use it like an app

The dashboard can be installed as an app: it then opens in its own window, without tabs or address bar, with the cat icon.

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
| **Backups** | Each scheduled backup, whether its copies were checked and found intact, and whether a restore was tried. |
| **Hardware** | Processor (total, each core, clock, waiting work), memory, every graphics card and AI chip, network traffic, drive activity, batteries and UPS. Updates every 4 seconds. |
| **Sensors** | Every temperature sensor (grouped: processor, graphics, drives, motherboard…), fan speeds, power use and voltages. |
| **Fans & lights** | Fan speeds by temperature, fixed speeds, or the motherboard's own control, for every fan; finding which fan is which; colours, brightness and effects for RGB lighting. |
| **Processes** | What's running, including inside each app, with processor and memory use. Search and sort. Right-click a program (on a phone, tap ⋯) to restart its app, or to close an app opened on the desktop. |
| **Server health** | Drive space and health, RAID arrays and Btrfs pools (each drive of an array, rebuilds with their progress, and the error counters that show a drive going bad early), **what starts by itself after a restart**, the outside heartbeat, phone notifications, app checks and waiting updates. |
| **Needs attention** | Everything to look at, most urgent first, each with "What should I do?". |
| **Help** | Step-by-step guides, the error codes below, installing it as an app, and a privacy-safe summary to share. |

On a phone the main pages are in the bar at the bottom; **More** opens the rest. On a tablet the menu becomes a slim icon rail. Settings (⚙) has a light theme and larger text (Large, or TV for across the room).

## Fans & lights

Like a fan-control app, built in:

- **Every fan is found by itself.** Give one a name if you like ("Processor fan").
- **Three ways to run a fan:** *Motherboard* (the PC's own setting stays in charge; this is the default), *Fixed* (one speed), or *Curve*: drag the points to set the speed for each temperature. *Silent*, *Balanced* and *Performance* are ready-made starting points. Nothing changes until you press **Save**.
- **Detect fans** finds which fan is which. Every fan spins up to full speed, then each one is slowed in turn. It is loud, and takes about 10 seconds per fan. You can stop it at any time.
- **It is built to be safe.** A fan NekoServer controls never runs slower than 20%, goes to full speed when things get hot or a temperature can't be read, and goes back to the motherboard's own control whenever NekoServer stops.
- **RGB lighting** (on the motherboard, memory, graphics card and USB devices): pick a colour, brightness and effect per device, or *Apply to all*. Your choice comes back after a restart. Memory and motherboard lighting need one restart of the PC after setup.

To leave fans and lights completely alone, set `"fan_control": false` under `"dashboard"` in `my-config.json` (or `"rgb": false` for the lighting only) and run `sudo bash install.sh` again. The motherboard then takes over again.

## Your PIN

**On the server itself**, anyone using the dashboard can change fans and lights. **Phones, tablets and other computers** can always look, but need the **PIN** chosen during setup to change anything. **Restarting or closing apps needs the PIN on every device**, the server too. Once unlocked, a device stays unlocked for 12 hours, or until the page is closed.

Five wrong PINs lock out that device for 5 minutes (each further five, twice as long). Other devices, and the server itself, are not locked out. The PIN box says how many tries are left, and when a device is locked out, how long it must wait and until what time (on that device's clock).

**Forgot it, or want a new one?** In a terminal on the server, run these two lines. The first deletes only the old PIN (nothing else); the second is the normal setup, which then asks for a new PIN:

```bash
sudo rm /etc/nekoserver-control/pin.json
cd NekoServer-All/nekoserver && sudo bash install.sh
```

No PIN chosen during setup? Then app controls stay off, and fans and lights can be changed on the server only. Run `sudo bash install.sh` again in a terminal to choose one.

## Restart or close apps

On a computer, right-click an app on **Your apps**, or a program on **Processes**, as in the Windows Task Manager. **On a phone or tablet, tap the ⋯ button** next to it. With the keyboard, use the ⋯ button or the context-menu key.

- **Server apps** (Jellyfin, Immich, Nextcloud and the others): **Restart app** stops the app and starts it again, then waits until it answers again (up to 4 minutes). Its database keeps running. The card shows *Restarting…*, then the result.
- **Desktop apps** (anything opened on the server's own desktop, such as Firefox, Steam or Dolphin): **Close app** asks it to quit. Many apps save or ask first, but some quit straight away and **unsaved work in them can be lost**, so Close asks you first. If the app is still running after 10 seconds (it may be asking whether to save a file), the page offers **Force close**, which stops it at once. **Unsaved work in it is lost**, so Force close always asks first. A program started from another app (Firefox opened from a terminal, say) is closed together with that app; the menu says so.
- **Protected, never stopped from here:** databases inside apps, backups, the monthly update, system services, the desktop itself and NekoServer. Their menu says so.

**Backups and updates come first.** While a backup or the monthly update runs, Restart app waits, and the menu says which job is running (E47). Only one app restarts at a time.

**"Not responding"** means the app's own health check failed. Without a health check, a running app shows as *Running*: try opening it. A busy processor never counts as "not responding": converting a video can rightly keep every core at 100%.

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
| E44 | Wrong PIN | Check the PIN and try again. The message says how many tries this device has left; after the last one, how long it must wait. |
| E45 | Too many tries; wait a while | The message says how long, and until what time. Five wrong PINs lock out that device for 5 minutes; each further five, twice as long. Other devices and the server itself are not locked out. When the message says *other devices together* typed too many wrong PINs, phones and tablets get one more try every two hours; on the server itself you can still unlock. |
| E46 | The change could not be saved | Nothing was changed, because it wouldn't survive a restart. The server's system drive may be full or failing. |
| E47 | Not now: something else is using it | A backup, the monthly update or another app restart is running (or a cut-off one still has to be finished). Try again when it has. The message says which. |
| E48 | Protected: can't be stopped from here | Databases, backups, system services and the desktop itself are never stopped from the dashboard. Restart the whole app instead, or close only apps opened on the desktop. |

## After a restart or a power cut

Everything comes back without you: the drives, then the apps, Tailscale (with your phone addresses) and the dashboard itself. **Server health → *Starts by itself*** shows each of these. Anything that did not come back is listed under **Needs attention**.

## Browser alerts and phone notifications

Settings → *Turn on browser alerts* shows urgent problems as notifications while the page is open on that device. For alerts that reach your phone when nothing is open, set up phone notifications ([NOTIFICATIONS.md](NOTIFICATIONS.md)); for an alert when the whole server goes quiet, the outside heartbeat ([HEARTBEAT.md](HEARTBEAT.md)).

## A sensor is missing

Many motherboards need a small extra program, `lm_sensors`, before their temperatures and fans show up. Install it with the line for your Linux, then run `sudo sensors-detect` (answer the questions with Enter) and restart the PC:

```bash
sudo pacman -S lm_sensors          # Arch, Nyarch
sudo apt-get install lm-sensors    # Debian, Ubuntu, Linux Mint
sudo dnf install lm_sensors        # Fedora, RHEL, Rocky Linux, AlmaLinux
sudo zypper install sensors        # openSUSE
```

A missing reading shows "—" or "No load reading", never a made-up 0. Help → *A sensor is missing* has more.

---

## For administrators

The rest of this page is the technical reference.

### Remote access and the port

The Tailscale address only works for devices signed in to your own Tailscale account (Tailscale Serve, never Funnel). To keep the dashboard on the server only, set `"remote": false` under `"dashboard"` in your settings and run `sudo bash install.sh` again. Connecting Tailscale after the dashboard was installed is fine: the next setup run gives the dashboard its Tailscale address. The port 8787 can be changed with `"port"` under `"dashboard"`; when another program on the server already uses it, setup takes the next free port from 8788-8799 by itself, writes it there and says so (the end of setup and the app menu entry show the right address).

### How sensors are found

There is nothing to configure. The dashboard reads what Linux reports:

- **Temperatures, fans, fan outputs, power, voltages:** every hardware-monitoring chip (`/sys/class/hwmon`): processor (coretemp, k10temp, zenpower), graphics (amdgpu, nouveau, i915, xe), NVMe drives, motherboard chips (nct67xx, it87, asus, gigabyte…), Wi-Fi cards and liquid coolers. **SATA drive temperatures are not read live:** on some hard drives that resets the spin-down timer, so an open dashboard could keep the media drive awake. They come from the drive health check instead (every 30 minutes, never waking a sleeping drive). If none report a temperature (some mini PCs, virtual machines), the system's thermal zones are used instead.
- **Graphics cards and AI chips:** every display adapter and NPU, named from the system's PCI list. AMD cards report their load and video memory; Intel graphics report their clock; NVIDIA cards are read through the NVIDIA driver once a minute.
- **Network and drives:** wired, Wi-Fi and Tailscale traffic; activity of every physical drive.
- **Batteries and UPS:** a laptop battery or a USB UPS.

To see everything the dashboard can read on this computer:

```bash
/usr/local/lib/nekoserver-dashboard/v0.41.1/nekoserver-runtime hardware
```

### Fan control in detail

Fan control is written in Rust, as part of NekoServer's runtime.

- **Every fan output is found automatically** (each `pwm` output Linux can control on the motherboard chip, graphics card or cooler).
- **Curves** have 2 to 8 points and follow the temperature you choose. The yellow line shows the temperature now.
- **Detect fans** stops by itself if anything gets hot (85 °C, or near a sensor's own critical limit) or a temperature can no longer be read, and it won't start when no temperature can be read at all, or while a fan hasn't gone back to the motherboard. A fan whose speed can't be read is left out rather than guessed, and retrying it never pauses the temperature checks or Stop.
- **Safety:** speeds rise at once and fall gently (5% every 2 seconds), so fans don't hunt up and down. A controlled fan never goes below 20% (you can set a higher floor). Every controlled fan (curve or fixed speed) goes to full speed 3 °C below a sensor's own critical limit, at 105 °C, after five minutes above 95 °C (today's processors run at 95-100 °C under full load by design, so one such reading is no emergency), when its curve's temperature can't be read, or when no temperature can be read at all. A fan that stops at the speed it is given (some fans can't run at 20%) is noticed when *Detect fans* has linked it to its output: after a few seconds it gets full speed, and the page asks you to raise *Never slower than*. A fan is only taken over when its own setting can be read, so it can always be put back; and before every change NekoServer checks the fan output still belongs to the same chip (Linux reuses hwmon numbers when devices come and go). If the control service stops for any reason, a crash included, every output is handed back to the motherboard: the motherboard's own setting is written down before a fan is changed, and kept until it has really been put back. A fan the motherboard won't take back runs at full speed (shown as *Not handed back*; if even full speed can't be set, the page says so) while NekoServer keeps trying. A damaged recovery record is never deleted: it is kept and the page asks you to check the fans. Switching fan control off in setup only finishes once every fan is back with the motherboard.

### RGB lighting in detail

**RGB lighting** goes through **OpenRGB**, which setup installs from the system's own repository when it has one (Arch does; Debian and Ubuntu may not, and lighting is then skipped) and runs as a service. OpenRGB runs as root, so only the control service can reach it: OpenRGB runs in a private network of its own that no program on the computer can connect to, and the only way in is a socket only root may open (`/run/nekoserver-openrgb/openrgb.sock`), which systemd's socket proxy passes on into that network. Lighting also needs that proxy (`systemd-socket-proxyd`, part of systemd); setup looks for it and skips lighting on a system without it. Lighting devices reached over the network (smart bulbs, Wi-Fi light strips) are not supported for that reason; lighting inside the PC and on USB is. Each device gets a colour, brightness, an effect and on/off; *Apply to all* sets every device at once. Your choice comes back after a restart, also for a device that shows up later or was reset (NekoServer checks every 10 seconds that each device still shows its choice). Effects that make their own colours (a rainbow, say) can't be dimmed, so colour and brightness are switched off for them. Identical devices (two matching RAM sticks) are told apart by their serial number, or else by where they are connected (a slot address, never a path Linux may renumber after a restart). Memory and motherboard lighting need the i2c driver, which setup switches on; restart once after setup. With `"rgb": false` NekoServer leaves lighting completely alone: it never reads or changes it, not even through an OpenRGB you run yourself.

### The PIN in detail

Each device is counted on its own (by its Tailscale address, which Tailscale sets for each device, or else its Tailscale login), so a phone typing wrong PINs never locks out another phone, a tablet signed in to the same Tailscale account, or the server itself. All other devices together may type 50 wrong PINs; after that only one more try every two hours is allowed among them, so many devices guessing side by side are stopped too, and the message then says that other devices used up the tries. The server itself is never locked out by other devices. A program on the server that pretends to be a Tailscale device is counted as itself (by its account), so it can't use up the devices' tries either. A PIN has 6 to 8 digits (one chosen before 0.28.7 with 4 or 5 still works). Only a slow, salted hash of the PIN is stored (`/etc/nekoserver-control/pin.json`); setup asks for a PIN only when that file is missing.

### App restarts in detail

**Restart app** stops all of an app's parts together and in order, then starts them again. Its **database and cache keep running**: Immich restarts its server and machine learning, Nextcloud its web part and its background jobs. **Close app** sends Linux's ordinary "please quit" signal (SIGTERM) to every process of the app's systemd unit in `app.slice`; **Force close** sends SIGKILL. The server refuses a restart while a backup or the monthly update runs, or one that was cut off still has to be finished (E47). If a restart is ever cut off part-way (the computer loses power, say), the parts it had stopped are started again once no backup or update needs the apps stopped (its note is kept in `/etc/nekoserver-control`).

"Not responding" comes from Docker's health check where the app has one (Immich, Seerr), or, when you switched them on (`"enabled": true` under `"monitoring"`; they are off by default), the hourly app checks that ask the app for a page (*App health checks* in Server health).

### Starts by itself, in detail

After a restart (or a power cut):

- the drives mount, then **Docker** starts (it waits for both drives) and starts every app again;
- **Tailscale** reconnects, with its phone addresses;
- the **dashboard** starts, takes its first readings 15 seconds after boot and every minute after that, and restarts itself if it ever stops.

Server health → *Starts by itself* shows each of these: whether it starts at boot and whether it is working now. A service that stopped with an error is never shown green, even when it starts at boot; Needs attention lists it (urgent for Docker, Tailscale, the dashboard and Samba), together with anything switched off.

### Install or update

The dashboard is part of the normal setup (the full profile installs it). To install or refresh just the dashboard:

```bash
sudo bash install.sh --profile dashboard --apply
```

Setup refreshes its service definitions and its menu entry. Its files are kept per release under `/usr/local/lib/nekoserver-dashboard/v0.41.1` (one folder per release, and when a setup run has finished, the folders of earlier releases that no service uses any more are removed; a changed file there stops setup rather than being overwritten). A menu entry or launcher you wrote yourself at the same place is kept.

### What runs, and what it can see

Three parts, all from the bundled Rust program (see RUST-RUNTIME.md), plus OpenRGB for lighting:

- **The collector** (`nekoserver-dashboard-collect`, root, every minute): drives, SMART, apps and their resource use, backups, checks and what starts at boot. It writes one public snapshot, `/run/nekoserver-dashboard/status.json`, with selected fields only (no passwords, no file lists). Private history and SMART state stay on the SSD; it refuses to read them when the SSD's identity is wrong.
- **The web server** (`nekoserver-dashboard`, an unprivileged throwaway user): serves the page, the snapshot, live hardware readings and the process list. It listens on 127.0.0.1 only, can't reach Docker, the app SSD or the settings, answers only its own addresses, refuses cross-site requests and never changes hardware itself: fan and light changes are passed to the control service. Live readings are taken only while a page asks for them.
- **The control service** (`nekoserver-control`, root, because only root may set fan speeds and stop programs): the only part that changes anything. It listens on a private socket that only the web server can use, checks every request (ranges, known outputs, the PIN; for app controls, which app or program it is and whether a backup runs), and its settings are kept in `/etc/nekoserver-control`. It runs whenever the dashboard is installed; with fan control switched off it never takes a fan over. When it stops, the fans go back to the motherboard; an app it is restarting is finished first. It uses the computer's own network, but only its 127.0.0.1: after restarting an app it checks there that the app answers (0.39.6 to 0.40.0 gave it a private network, where no app ever answers, so every *Restart app* ended as a failure after 4 minutes).
- **OpenRGB** (`nekoserver-openrgb`): talks to the lighting. It runs in a private network of its own. The control service reaches it through a socket only root may open (`nekoserver-openrgb-proxy.socket`); on the first connection systemd starts `nekoserver-openrgb-proxy.service` (systemd's socket proxy) inside OpenRGB's private network, which passes the requests on. So no other program on the computer can reach OpenRGB (and skip the PIN), and OpenRGB shares no `/tmp` with the control service. Setup stops the control service and lighting together and starts them again in order (OpenRGB, its socket, the control service), so an upgrade takes effect at once, without a restart of the computer; it then checks that OpenRGB really is in a private network, and stops it if not. The acceptance check (ACCEPTANCE.md) checks both: the control service on the computer's own network, OpenRGB in its own.

Process names are shown, never command lines, environments or user names. The page keeps favourites and settings in your browser only; the summary download in Help contains counts and versions, nothing private.

```bash
systemctl status nekoserver-dashboard.service nekoserver-dashboard-collect.timer nekoserver-control.service nekoserver-openrgb.service nekoserver-openrgb-proxy.socket
sudo journalctl -u nekoserver-dashboard-collect.service -n 50
```

### What the readings mean

| Reading | Source |
| --- | --- |
| Processor | Time spent busy between two readings (`/proc/stat`), total and per core. 100% for a process = one full core. |
| Waiting work | Load average over 1, 5 and 15 minutes; below the number of threads means the processor keeps up. |
| Memory | Total minus available memory (`/proc/meminfo`). |
| Temperature on Home | Hottest processor sensor. An alert after five minutes at 80 °C or more (urgent after five minutes above 95 °C), or at once at the chip's own critical limit or 105 °C: one hot reading under load is normal. |
| Drives | Space from the mounted, identity-checked SSD and HDD; health from read-only SMART every 30 minutes. A sleeping drive is not woken, so its health can show "Not checked yet". |
| Apps | Docker containers by their NekoServer project. "Running" doesn't prove an app works; the app checks and the acceptance checklist do. An app setup has not got to yet (setup stopped before it) shows as *Not set up yet*, with one item saying to run setup again, and nothing to open or restart; it is not counted as stopped. The *Steam Big Picture* card appears only once setup has set up the TV step. While the app SSD or the media HDD is missing, the apps it stopped are named in that drive's alert instead of one alert each. |
| Backups | Each job's last attempt, last success and schedule. A last success saved while the clock was ahead (later put back) is kept and shown as "now" rather than disappearing; the same goes for the heartbeat's and the notifications' last success. Checksums (SHA-256) show whether every copy matched at the **last full check**, which the monthly update makes (for the USB drive, when it is plugged in on that night); the hourly and weekly copies compare sizes and dates only, so they never count as a check, and damage a full check found stays listed until it is repaired. A full check with no valid date, or older than 35 days, is not shown as passed. *Restore tried* counts the witnessed restore tests for each kind of data this server backs up (photos, Nextcloud files, music, app settings, host and user files), says which were tested, and shows **Yes** only once every one of them has passed. A USB copy that holds both photos and Nextcloud files needs both tests. A backup that is switched off (its app is not set up) shows as *Switched off* and needs no test. Every backup needs both the app SSD and the media HDD: while one of them is missing, each backup shows *Can't run now* (not its last result), the HDD photo archive's checksum result shows *Drive missing*, and only the drive's alert is raised. A monthly update that was undone (the updated apps did not start well, so the apps were put back on their previous version, and work) shows as *Update undone*: a warning, not an urgent failure; it is tried again the next month. Next runs are shown in this device's own time. A long reason is shortened at a whole sentence, with *Show more* for the rest. |

Warnings appear for missing drives, drives 90% full (critical at 97%), SMART problems, stopped apps, failed or overdue backups, damaged backup files, and anything that won't start after a restart.
