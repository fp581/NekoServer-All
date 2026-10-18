# Guided setup in Konsole

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
- TV: open Steam, Bluetooth settings when selected, display settings and browser playback; explain main-PC Remote Play and Xbox-controller tests. Hardware results are user-confirmed; package/service checks remain automatic.
- New ntfy subscriptions and independent heartbeat monitors: open setup guidance, explain the work on the phone/provider, then collect private destination values in hidden console fields. Subscription/recipient choices are yours; the browser handoff sends no test messages.
- `acceptance.py --manual --save`: open relevant app pages and guide witnessed tests with the same yes/no loop. User confirmation never overrides a failed automatic check or performs a destructive restore for you.

## Run the intended profiles

Use `full` first for base applications and local services. Remote access remains an explicit additional profile; it does not redeploy your apps:

```bash
NEKO_RELEASE=/srv/nekoserver/installer/releases/v0.21.0
sudo bash "$NEKO_RELEASE/install.sh" --profile remote_access --apply \
  --config "$NEKO_RELEASE/config.json"
```

This single interactive remote run now guides server sign-in, DNS/HTTPS, route creation, phone setup and mobile-data tests. Normally there is no separate manual `tailscale up` command or second profile run. If a local CLI cannot provide an authorization URL, the console explains the manual `sudo tailscale up` fallback. Reapply to resume anything left pending.

After a full-profile rerun, reapply remote_access if Tailscale should remain Nextcloud's primary address; the last gateway applied determines that origin.

## Email accounts

Immich and Seerr normally use an email-shaped **local app login**. This does not require an external mailbox login or imply that a verification email will be sent. Automatic local provisioning stays automatic. Generated `@server.invalid` logins cannot receive mail. If a service actually requests external identity sign-in or email verification, complete it in that service/email application and return to Konsole. External account passwords/2FA codes are never requested or saved by these handoffs. Supported machine checks must still pass.

## Desktop and noninteractive behavior

Launch from Konsole in the existing desktop session using sudo. The launcher drops to the original normal user and uses a minimal environment with that user's session bus/runtime. Root browsers, shell evaluation of URLs and forwarding of the root environment are avoided. Browser launch dependencies (xdg-utils/Firefox) are installed on the supported Arch/Nyarch host when missing and an interactive desktop launch is needed.

If there is no usable local GUI, the launcher explains the manual page/app fallback and still waits for your actual yes/no answer in an interactive terminal. It does not assume that a process starting proves that a window appeared. A phone/other PC must still be operated on that device.

Read-only modes, background runs and runs without an interactive terminal never launch windows or block waiting for confirmation. Required unfinished work remains pending. `guided_setup.enabled=false` disables guided windows/prompts for intentional headless administration; it cannot invent a completed sign-in/phone test. Noninteractive setup still requires already configured, verified prerequisites. This is one-time Python setup; scheduled server work remains in Rust.

## Validation limits

Unit/integration fixtures exercise retry/cancel/EOF, real local account protocol contracts, machine-check failures, private state, preserved installer imports and command construction. Browser windows, real provider approval, installed app versions and actual phones/controllers still need acceptance on the intended machine. A user-confirmed record describes the result you witnessed; it is not a live automated phone or hardware probe.
