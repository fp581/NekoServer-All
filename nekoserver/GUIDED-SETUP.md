# Guided setup: what it asks, and what to answer

`sudo bash install.sh` sets up your whole server in one go. It stops whenever it needs you, explains what to do, and waits for your answer. This page walks through every question in the order you will see it, and says what to answer.

The technical details (how the setup window works inside, every guided step, running without a screen) are at the end, under [For administrators](#for-administrators).

## Before you start

Have these ready:

- **Your drives plugged in and formatted**: an SSD, an HDD and (if you have one) a USB drive. Setup never formats a drive. It needs at least **20 GB free on the SSD** and **50 GB free on the HDD** to start; **100 GB** and **500 GB** or more are recommended.
- **Your phone**, with its app store working.
- **A PIN you will remember** (6 to 8 digits) for the dashboard.
- **About an hour**, and the PC connected to the internet.

Open a **terminal**: press **Ctrl+Alt+T**, or open the apps menu and search for **Terminal**. Then type (or copy and paste) these two lines, pressing Enter after each:

```bash
cd NekoServer-All/nekoserver
sudo bash install.sh
```

`sudo` asks for **your own login password**. Nothing appears while you type it; that is normal. Press Enter when done.

Answers are typed in the terminal and finished with Enter: `yes` or `no`, a number from a list, or a password. You can stop at any time with **Ctrl+C**. Running `sudo bash install.sh` again later continues where it stopped; nothing already done is lost.

## What setup asks, screen by screen

### 1. Getting the PC ready (no questions, 5 to 15 minutes)

Setup first installs the programs the apps need, and then builds NekoServer's own background program. The terminal shows a lot of text; you don't need to read it. On Arch, the whole system is updated first. There is no setup window yet during this part.

If it stops with an error here, it says what went wrong. Most often it is the internet connection, or a website that your network blocks. Fix that, then run `sudo bash install.sh` again.

### 2. Welcome, and the setup window

When the PC is ready, the terminal shows a short welcome text and asks `Ready to start? [yes/no]`. On a PC with a desktop, a small **setup window** opens in your web browser at the same time. It shows the same questions as the terminal, with buttons. You can answer in either place.

![The setup window](../docs/media/setup-window.webp)

The window first lists the **optional extras**, each with a switch:

- **A TV** connected to this PC (Steam Big Picture).
- **Phone alerts** when something fails.
- **Outage alerts** from outside, when the whole server goes quiet.
- **Nextcloud**, your own cloud drive (installed unless you switch it off).

Switch off what you don't want. Not sure? Leave the switches alone: setup asks about the TV and the alerts later, when it gets to them. Then press **Start setup** in the window, or type `yes` in the terminal.

### 3. Tailscale: reaching your server from anywhere

Tailscale is a free app that lets your own phone and laptop reach the server privately, at home or away. Setup opens the Tailscale sign-in page in your browser.

1. Create a free account, or sign in (with Google, Microsoft, Apple or GitHub).
2. Approve this PC when the page asks.
3. Go back to the terminal and answer `yes` to `Have you completed this step successfully?`.

Answer `no` to see the step again and reopen the page.

### 4. Your drives

Setup lists the drives it found, numbered, for example:

```text
  1) /dev/sdb1 931.5G ext4 UUID=7fde… mounted=-
  2) /dev/sdc1 3.6T ext4 UUID=12ab… mounted=-
Choose number (or ENTER to abort):
```

1. Type the **number** of your SSD and press Enter. Use the size to tell the drives apart.
2. Setup asks you to type the drive's name back, for example `Type sdb1 to confirm it is the SSD:`. Type it exactly (`sdb1`) and press Enter. This is a safety check, so the wrong drive is never used.
3. The same follows for the HDD, and then for the USB drive. No USB drive yet? Press Enter to skip it; you will be asked again at the weekly backup step.

### 5. Drive space

Setup checks that the drives have room. If a drive has less than the recommended space, it says so and asks `Continue with these drives anyway?`. Answer `yes` to go on; the drive will just fill up sooner.

If a drive is too small (less than 20 GB free on the SSD, or 50 GB on the HDD), setup stops. Free up space on that drive and run `sudo bash install.sh` again. To use a different drive instead: open `my-config.json` (in the same folder as `install.sh`), find the `"storage"` part, and delete the line `"uuid": "…",` under `"data_ssd"` (the SSD) or `"media_hdd"` (the HDD). Save the file and run `sudo bash install.sh` again: it asks for that drive anew.

### 6. App passwords

Every app gets its own administrator login. Setup asks:

```text
Logins for the apps: 1 = NekoServer makes a strong username and password for each app (recommended; you can see them later), 2 = you choose them app by app. Type 1 or 2:
```

Type `1` and press Enter (recommended). Setup then makes a long random password for each app and saves it on the server. You never have to remember them: [Seeing an app's password](#seeing-an-apps-password) shows how to look one up.

Choose `2` only if you want to type your own login for some apps. Your own passwords need 12 characters or more.

### 7. Installing the apps (no questions, 15 to 60 minutes)

Setup now downloads and starts the apps: Jellyfin, Immich, Nextcloud, Navidrome and the others. This is the longest wait. You can watch the progress in the setup window.

### 8. Questions only the terminal can answer

A few steps ask their questions in the terminal only. The setup window says **Answer this step in the terminal** while they run.

- **Connecting the media apps.** Setup may ask which download sources to add. If you don't know what to choose, just press Enter: that skips it. You can add sources later in Prowlarr.
- **Music** (only if the automatic check did not pass). If setup asks for `Listener username:` and a password, type the login of your music account. To see it, open a second terminal (Ctrl+Alt+T) and run the command in [Seeing an app's password](#seeing-an-apps-password) with `show navidrome_listener`.
- **The shared-folder password.** This is the password other computers at home use to open the shared folders. Type it, press Enter, then type it again. Nothing appears while you type.

Setup may also ask you to **play something** in Jellyfin as a test. That is optional: answer `yes` once a video played, or press Ctrl+C to skip it.

### 9. The dashboard PIN

The dashboard is a page that shows how your server is doing. Setup asks you to choose a **PIN of 6 to 8 digits**, typed twice (nothing appears while you type). With it you can change fans and lights from your phone, and restart an app from the dashboard. Press Enter instead to skip it; you can choose one later by running setup again.

### 10. The TV (optional)

If you did not switch it off in the window, setup asks whether a TV is connected to this PC. Answer `no` if you don't use one. With `yes`, it sets up Steam Big Picture and walks you through a few tests on the TV.

### 11. Your phone

Setup opens a **phone setup page** on the PC with a QR code for each app. Keep it open. For each app, the terminal tells you which phone app to install and what to type.

1. **Tailscale first.** Install **Tailscale** on the phone (or scan the first QR code), sign in with the **same account** as in step 3, allow the VPN, and switch it on. Answer `yes` once the phone app says **Connected**.
2. **Then each app**, one at a time: Jellyfin (movies and TV), Seerr (requests), Immich (photos), Navidrome (music), Nextcloud (files) and the dashboard. Scan the app's QR code with the phone camera to open its address, or type the address shown.
3. **Signing in.** The terminal shows the app's username. To see its password, open a **second terminal** on the PC (Ctrl+Alt+T) and run the command the step shows, for example:

   ```bash
   sudo python3 /srv/nekoserver/installer/releases/v0.41.1/account-setup.py show jellyfin
   ```

   It shows the username and password on the PC's screen, with a QR code you can scan with the phone to copy the password. Press Enter to hide it again. (Your server may keep its files somewhere other than `/srv/nekoserver`: copy the command exactly as the step shows it.)
4. **A test away from home.** Turn off Wi-Fi on the phone, keep Tailscale on, and open one of the apps. If it works on mobile data, it works anywhere. Turn Wi-Fi back on.

Answer `yes` after each app works on the phone. Answer `no` to see that app's instructions again.

### 12. Your home network

Setup asks which network connection links this PC to your router, for example:

```text
Which network connects this server to your home network (your router)?
1. enp3s0 · 192.168.1.20 · 192.168.1.0/24
Choose your home-network interface number:
```

Type the number (usually `1`) and press Enter. Other devices at home can then open the apps without Tailscale too. Tip: in your router, reserve this address for the server (often called "DHCP reservation" or "static lease"), so it doesn't change after a restart.

Setup may then open each app in the browser and ask you to check that it asks for a login. Answer `yes` once you have checked.

### 13. Phone alerts and outage alerts (optional)

Setup asks about each one, once. Answer `no` if you don't want it; nothing else depends on them. With `yes`, it explains what to set up on the phone or website, and asks for the private address to send the alerts to. To set one up later, see [NOTIFICATIONS.md](NOTIFICATIONS.md) and [HEARTBEAT.md](HEARTBEAT.md).

### 14. The backup password

Monthly backups keep the list of app passwords locked with a **backup password**. Choose one (8 characters or more; your login password is fine too) and type it twice. **Write it down somewhere safe**: getting the password list back from a backup needs it. You are asked only this once.

### 15. The first checkup, and the finishing screen

Setup checks every app, runs each backup once straight away, and checks the whole server. This takes a few minutes to an hour. Then it shows the **finishing screen**: what is done, anything that still needs you, and your app addresses.

If something still needs you, the finishing screen says what. Fix it, then run `sudo bash install.sh` again. It only redoes what is not finished.

## If something goes wrong

- **Setup stopped, or you closed the terminal?** Run `sudo bash install.sh` again in the same folder. It continues where it stopped. Steps you already confirmed (Tailscale, the phone, the TV tests) are not asked again.
- **Answered `yes` too early?** If setup can check the result, it shows the step again. If not, the finishing screen lists what is still untested.
- **Closed the setup window?** Reloading the page in the same browser brings it back. If the browser itself was closed, the address does not open again in a new browser (this keeps other programs out). Just go on in the terminal: every question is asked there too. The window comes back the next time you run `sudo bash install.sh`.
- **No setup window at all?** On a PC without a desktop, or when no browser could be opened, the terminal shows everything. Nothing is missing.

## Seeing an app's password

Your app passwords are saved on the server, and only the administrator can read them. To see one app's login on the screen (with a QR code for your phone), run this in a terminal:

```bash
sudo python3 /srv/nekoserver/installer/releases/v0.41.1/account-setup.py show
```

It lists the saved logins. Type a number to show one. Press Enter to hide it again (the screen is cleared), and press Enter on the list to stop. To go straight to one app, add its name: `show jellyfin`, `show immich`, `show seerr`, `show nextcloud`, or `show navidrome_listener` for music. Check that nobody else can see your screen first.

## Doing one part again later

To redo only Tailscale and the phone part:

```bash
sudo bash install.sh --profile remote_access --apply --config my-config.json
```

Nextcloud works on the home and the Tailscale address at the same time; redoing either part does not change that.

---

## For administrators

The rest of this page is the technical reference for the guided steps.

### The setup window, in detail

Beside the terminal, `sudo bash install.sh` opens a **setup window** in the PC's own browser once the packages are installed and the runtime is built, at the `Ready to start?` question. It is served by setup itself, on this PC only (`127.0.0.1`, a port chosen at random, behind an address nobody can guess) and only while setup runs. Every question setup asks is asked in both places, and either can answer it. Only the account that runs setup (or root) can open it: another account on the PC that sees its address gets a page saying so.

- **Before setup starts** it lists the optional parts (a TV, phone alerts, outage alerts, Nextcloud) with a plain explanation of each and what it installs, and switches to leave any of them out. A switch you press is saved to `my-config.json` at once (`"wanted"`, or Nextcloud's `"enabled"`): switched off, that step then counts as done without asking. A switch you leave alone decides nothing: setup asks about the TV, phone alerts and outage alerts when it gets to them, in the terminal and in the window. Nextcloud is on in the settings from the start, so it is installed unless you switch it off. **Start setup** in the window answers the terminal's "Ready to start?"; typing `yes` in the terminal works the same. Once setup has started (from either place) the choices are fixed: a switch pressed later, or a second **Start setup**, is refused (`409`) and the settings file is left as setup is writing it. A switch only ever changes its own setting, on the file as it is at that moment, so a value setup saved meanwhile (the drives it found) is never written back over.
- **While it runs** it shows the step under way with a one-line explanation, the packages that step installs with what each one is for, the step's own messages, and the list of all steps with their state. **When setup asks something**, the window asks it too: the terminal's last lines (the numbered drives, the instructions for a phone step) as context, then the question with a text box, Yes/No buttons for a yes/no question, or a password box for a hidden entry (a password or PIN, which the window never shows and the terminal does not echo). The answer can be given in the window or typed in the terminal, whichever comes first; the other side is told. An answer from the window travels only over this PC's loopback, to setup's own process.
- **Answer this step in the terminal.** Three steps run a program of their own whose questions reach only the terminal: connecting the media apps (download sources and sign-ins), music listener setup, and the shared-folder password (`smbpasswd`, typed twice). While one runs, the window shows **Answer this step in the terminal** with the step's name, and goes back to normal when it ends.
- **At the end** it shows the finishing screen (what is done, what still needs you, your app addresses).
- **Opening it again.** The page's session lives in a cookie of the browser that opened it first, so a reload in that browser works. Another browser (or the same one after it was closed, which drops the session cookie) gets "This setup window was already opened": a second program that learns the address cannot take the session over. The terminal keeps asking every question meanwhile.

With Chromium, Chrome, Brave or Edge the page opens as a window of its own, sized for the bottom of the screen; Firefox opens it as a normal window. On a PC without a desktop, or when no browser could be opened, setup prints the window's address instead and goes on in the terminal alone. The page needs no internet connection and loads nothing from outside the PC. The words it shows live in `explanations.py`; the events behind it in `setup_progress.py`; the server in `setup_gui.py`.

### The yes/no loop

During interactive `--apply`, a step that needs a browser, desktop app or phone:

1. Prints the step name, where to do it, what to enter and the result to check.
2. Opens the appropriate local browser page or installed desktop application as the normal desktop user.
3. Asks: `Have you completed this step successfully? [yes/no]`.
4. On **no**, repeats the instructions and opens the step again. An interrupted Tailscale authorization obtains/reopens its current authorization flow.
5. On **yes**, runs any available automatic verification. If it fails, the step repeats instead of reporting success.

`y`/`n` also work, case-insensitively. Other input is rejected. Ctrl+C or an unavailable input stream leaves the step pending, preserves saved account choices and allows an interactive reapply later. No answer is inferred from elapsed time. No terminal polling loop or Python service remains running after installation.

### Which steps use it?

- App accounts that automatic provisioning could not finish: open the app, use the saved local username/email and password, complete any app-requested verification, then recheck authenticated access. Existing credentials are not reset. Incorrect existing credentials can be corrected with `account-setup.py --apply` on reapply.
- Gateway security: open the selected apps, verify login and unwanted signup/sharing settings, then confirm yes. With managed accounts enabled the installer rechecks those accounts before persisting selected-service authorization.
- Tailscale: start the official CLI login, open only its official authorization link, then check `BackendState=Running`. Already-connected servers are not forced to authenticate again. No auth key or external identity password is collected by NekoServer.
- Private HTTPS: open the Tailscale DNS settings, explain MagicDNS/HTTPS and the certificate-name disclosure, then attempt the managed private routes. Conflicting unmanaged routes remain protected. No Funnel or router port forwarding is enabled.
- Phone setup: open installation guidance on the server, print the actual private app links, explain phone VPN permissions and each app's own login, then walk through photo/file/music/video and mobile-data tests. The server cannot launch apps or grant permissions on a different phone. Their confirmations are labelled user-witnessed, not independent automatic probes. Passwords are never printed by the phone step nor written to its page (`/usr/local/share/nekoserver-pages/phone-setup.html`, readable by every account). Each app's step names `account-setup.py show APP` from the release copy on the SSD instead: it shows one login, to root only, on a terminal only (it refuses a pipe or a file, so nothing lands in a log), passes the password to `qrencode` on its standard input rather than its command line, and clears the screen afterwards.
- Media sources/providers: after selecting a provider that needs credentials, explain its account/API requirements and open Prowlarr. Rejected provider setup can be completed in its UI and checked for a retained definition. End-to-end acquisition/playback remains a separate witnessed test.
- After an update, the steps the new release added are added to the `full` profile (and any other profile) in your `my-config.json`, in their place, with a line in the log for each; a step you took out of a profile yourself stays out, and steps you added stay. The finishing screen counts every step the release's `full` profile has, so a settings file from an older release can't read "ready" with a step never run.
- Optional extras (TV, ntfy phone alerts, outage monitor) are asked once with yes/no. `no` is saved as `"wanted": false` in `my-config.json` and the step counts as done; set it to `true` to set the extra up on a later run.
- Steps already confirmed are not repeated on a re-run: Tailscale sign-in and HTTPS, the phone walkthrough (for the same app addresses) and each TV hardware test.
- TV: open Steam, Bluetooth settings when selected, display settings and browser playback; explain main-PC Remote Play and Xbox-controller tests. Hardware results are user-confirmed; package/service checks remain automatic.
- New ntfy subscriptions and independent heartbeat monitors: open setup guidance, explain the work on the phone/provider, then collect private destination values in hidden console fields. Subscription/recipient choices are yours; the browser handoff sends no test messages.
- `acceptance.py --manual --save`: open relevant app pages and guide witnessed tests with the same yes/no loop. User confirmation never overrides a failed automatic check or performs a destructive restore for you.

### The order of the steps

The `full` profile runs: system, tailscale, storage, capacity, recovery, account_credentials, server_os, docker, recovery_checkpoint, media, photos, photo_preservation, photo_backup, nextcloud, accounts, hardware_video, onboarding, music, samba, media_monitor, dashboard (the PIN), tv, desktop, gateway_tailnet (the phone), gateway_lan (the home network), app_backup, notifications, heartbeat, host_backup, maintenance (the backup password). Then the first checkup and backup.

The Tailscale part, in order: connect this server (sign-in page, free account), turn on secure addresses (MagicDNS and HTTPS), create the private app addresses, then the phone: install Tailscale, then each app with its QR code from the page at `/usr/local/share/nekoserver-pages/phone-setup.html`, then a test on mobile data. If no sign-in page opens, the step explains the `sudo tailscale up` fallback.

### Email accounts

Immich and Seerr normally use an email-shaped **local app login**. This does not require an external mailbox login or imply that a verification email will be sent. Automatic local provisioning stays automatic. Generated `@server.invalid` logins cannot receive mail. If a service actually requests external identity sign-in or email verification, complete it in that service/email application and return to the terminal. External account passwords/2FA codes are never requested or saved by these handoffs. Supported machine checks must still pass.

### Desktop and noninteractive behavior

Launch from a terminal window (Konsole on KDE, Terminal on Ubuntu/GNOME) in the existing desktop session using sudo. The launcher drops to the original normal user and uses a minimal environment with that user's session bus/runtime. Root browsers, shell evaluation of URLs and forwarding of the root environment are avoided. The browser launch helper (xdg-utils) is installed when it is missing and an interactive desktop launch is needed.

If there is no usable local GUI, the launcher explains the manual page/app fallback and still waits for your actual yes/no answer in an interactive terminal. It does not assume that a process starting proves that a window appeared. A phone/other PC must still be operated on that device.

Read-only modes, background runs and runs without an interactive terminal never launch windows or block waiting for confirmation. Required unfinished work remains pending. `guided_setup.enabled=false` disables guided windows/prompts for intentional headless administration; it cannot invent a completed sign-in/phone test. Noninteractive setup still requires already configured, verified prerequisites. This is one-time Python setup; scheduled server work remains in Rust.

### Validation limits

Unit/integration fixtures exercise retry/cancel/EOF, real local account protocol contracts, machine-check failures, private state, preserved installer imports and command construction. Browser windows, real provider approval, installed app versions and actual phones/controllers still need acceptance on the intended machine. A user-confirmed record describes the result you witnessed; it is not a live automated phone or hardware probe.
