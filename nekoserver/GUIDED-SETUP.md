# Guided setup in the terminal

## The setup window

Beside the terminal, `sudo bash install.sh` opens a **setup window** in the PC's own browser (a small window; put it where you like, the bottom of the screen works well). It is served by setup itself, on this PC only (`127.0.0.1`, a port chosen at random, behind an address nobody can guess) and only while setup runs. Every question setup asks is asked in both places, and either can answer it.

- **Before setup starts** it lists the optional parts (a TV, phone alerts, outage alerts, Nextcloud) with a plain explanation of each and what it installs, and switches to leave any of them out. Each switch is saved to `my-config.json` at once (`"wanted"`, or Nextcloud's `"enabled"`); the step then counts as done without asking. **Start setup** in the window answers the terminal's "Ready to start?"; typing `yes` in the terminal works the same. Once setup has started (from either place) the choices are fixed: a switch pressed later, or a second **Start setup**, is refused (`409`) and the settings file is left as setup is writing it. A switch only ever changes its own setting, on the file as it is at that moment, so a value setup saved meanwhile (the drives it found) is never written back over.
- **While it runs** it shows the step under way with a one-line explanation, the packages that step installs with what each one is for, the step's own messages, and the list of all steps with their state. **When setup asks something**, the window asks it too: the terminal's last lines (the numbered drives, the instructions for a phone step) as context, then the question with a text box, Yes/No buttons for a yes/no question, or a password box for a hidden entry (a password or PIN, which the window never shows and the terminal does not echo). The answer can be given in the window or typed in the terminal, whichever comes first; the other side is told. An answer from the window travels only over this PC's loopback, to setup's own process.
- **Answer this step in the terminal.** Three steps run a program of their own whose questions reach only the terminal: connecting the media apps (download sources and sign-ins), music listener setup, and the shared-folder password (`smbpasswd`, typed twice). While one runs, the window shows **Answer this step in the terminal** with the step's name, and goes back to normal when it ends.
- **At the end** it shows the finishing screen (what is done, what still needs you, your app addresses) and can be closed.

With Chromium, Chrome, Brave or Edge the page opens as a window of its own, sized for the bottom of the screen; Firefox opens it as a normal window. On a PC without a desktop, or when no browser could be opened, setup prints the window's address instead and goes on in the terminal alone. When the window did open, the terminal still shows its address, so a window closed by mistake can be opened again in a browser on this PC. The page needs no internet connection and loads nothing from outside the PC. The words it shows live in `explanations.py`; the events behind it in `setup_progress.py`; the server in `setup_gui.py`.

During interactive `--apply`, a step that needs a browser, desktop app or phone now:

1. Prints the step name, where to do it, what to enter and the result to check.
2. Opens the appropriate local browser page or installed desktop application as the normal desktop user.
3. Asks: `Have you completed this step successfully? [yes/no]`.
4. On **no**, repeats the instructions and opens the step again. An interrupted Tailscale authorization obtains/reopens its current authorization flow.
5. On **yes**, runs any available automatic verification. If it fails, the step repeats instead of reporting success.

`y`/`n` also work, case-insensitively. Other input is rejected. Ctrl+C or an unavailable input stream leaves the step pending, preserves saved account choices and allows an interactive reapply later. No answer is inferred from elapsed time. No terminal polling loop or Python service remains running after installation.

## Which steps use it?

- App accounts that automatic provisioning could not finish: open the app, use the saved local username/email and password, complete any app-requested verification, then recheck authenticated access. Existing credentials are not reset. Incorrect existing credentials can be corrected through `account-setup.py` on reapply.
- Gateway security: open the selected apps, verify login and unwanted signup/sharing settings, then confirm yes. With managed accounts enabled the installer rechecks those accounts before persisting selected-service authorization. The interactive apply flow no longer requires typing SECURED after its yes/no prompt.
- Tailscale: start the official CLI login, open only its official authorization link, then check `BackendState=Running`. Already-connected servers are not forced to authenticate again. No auth key or external identity password is collected by NekoServer.
- Private HTTPS: open the Tailscale DNS settings, explain MagicDNS/HTTPS and the certificate-name disclosure, then attempt the managed private routes. Conflicting unmanaged routes remain protected. No Funnel or router port forwarding is enabled.
- Phone setup: open installation guidance on the server, print the actual private app links, explain phone VPN permissions and each app's own login, then walk through photo/file/music/video and mobile-data tests. The server cannot launch apps or grant permissions on a different phone. Their confirmations are labelled user-witnessed, not independent automatic probes.
- Media sources/providers: after selecting a provider that needs credentials, explain its account/API requirements and open Prowlarr. Rejected provider setup can be completed in its UI and checked for a retained definition. End-to-end acquisition/playback remains a separate witnessed test.
- Optional extras (TV, ntfy phone alerts, outage monitor) are asked once with yes/no. `no` is saved as `"wanted": false` in `my-config.json` and the step counts as done; set it to `true` to set the extra up on a later run.
- Steps already confirmed are not repeated on a re-run: Tailscale sign-in and HTTPS, the phone walkthrough (for the same app addresses) and each TV hardware test.
- TV: open Steam, Bluetooth settings when selected, display settings and browser playback; explain main-PC Remote Play and Xbox-controller tests. Hardware results are user-confirmed; package/service checks remain automatic.
- New ntfy subscriptions and independent heartbeat monitors: open setup guidance, explain the work on the phone/provider, then collect private destination values in hidden console fields. Subscription/recipient choices are yours; the browser handoff sends no test messages.
- `acceptance.py --manual --save`: open relevant app pages and guide witnessed tests with the same yes/no loop. User confirmation never overrides a failed automatic check or performs a destructive restore for you.

## How to run it

`sudo bash install.sh` runs everything, including Tailscale and the phone steps, in one guided session. Running it again continues where it stopped.

The Tailscale part, in order: connect this server (sign-in page, free account), turn on secure addresses (MagicDNS and HTTPS), create the private app addresses, then the phone: install Tailscale, then each app with its QR code from the page at `/usr/local/share/nekoserver-pages/phone-setup.html`, then a test on mobile data. If no sign-in page opens, the step explains the `sudo tailscale up` fallback.

To redo only the remote part later:

```bash
sudo bash install.sh --profile remote_access --apply --config my-config.json
```

Nextcloud works on both the home and the Tailscale address at the same time; rerunning either profile does not change that.

## Email accounts

Immich and Seerr normally use an email-shaped **local app login**. This does not require an external mailbox login or imply that a verification email will be sent. Automatic local provisioning stays automatic. Generated `@server.invalid` logins cannot receive mail. If a service actually requests external identity sign-in or email verification, complete it in that service/email application and return to the terminal. External account passwords/2FA codes are never requested or saved by these handoffs. Supported machine checks must still pass.

## Desktop and noninteractive behavior

Launch from a terminal window (Konsole on KDE, Terminal on Ubuntu/GNOME) in the existing desktop session using sudo. The launcher drops to the original normal user and uses a minimal environment with that user's session bus/runtime. Root browsers, shell evaluation of URLs and forwarding of the root environment are avoided. Browser launch dependencies (xdg-utils/Firefox) are installed on a supported host (Arch/Nyarch, Debian/Ubuntu or Fedora) when missing and an interactive desktop launch is needed.

If there is no usable local GUI, the launcher explains the manual page/app fallback and still waits for your actual yes/no answer in an interactive terminal. It does not assume that a process starting proves that a window appeared. A phone/other PC must still be operated on that device.

Read-only modes, background runs and runs without an interactive terminal never launch windows or block waiting for confirmation. Required unfinished work remains pending. `guided_setup.enabled=false` disables guided windows/prompts for intentional headless administration; it cannot invent a completed sign-in/phone test. Noninteractive setup still requires already configured, verified prerequisites. This is one-time Python setup; scheduled server work remains in Rust.

## Validation limits

Unit/integration fixtures exercise retry/cancel/EOF, real local account protocol contracts, machine-check failures, private state, preserved installer imports and command construction. Browser windows, real provider approval, installed app versions and actual phones/controllers still need acceptance on the intended machine. A user-confirmed record describes the result you witnessed; it is not a live automated phone or hardware probe.
