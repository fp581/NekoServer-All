# Alerts on your phone

NekoServer can send a message to your phone when something on the server needs you: a drive that is missing, a backup that failed, an app that stopped, the server running hot. The message arrives even when the dashboard is closed. It is optional: without it the dashboard still shows everything.

The messages go through **ntfy** (say "notify"), a free notification app. In ntfy you follow a **topic**, which is like a private channel with an address, such as `https://ntfy.sh/neko-k3v9…`: the server sends its alerts to that address, and every phone that follows the topic gets them.

The technical details (what is sent and when, the files, the timings) are at the end, under [For administrators](#for-administrators).

## Set it up

1. Run `sudo bash install.sh` in the NekoServer folder and switch on **Send alerts to my phone** on the first page. (Did you answer no before? Run setup again and switch it on; or set `"wanted": true` under `"notifications"` in `my-config.json`.)
2. When setup reaches the step, it shows what to do on the phone:
   1. Install the free app **ntfy** on your phone and open it.
   2. In the app, add a topic to follow. Its name is all that keeps others from reading your alerts, so take a name nobody could guess. Setup makes one for you and shows its full address.
   3. Type the same address into setup (it is hidden while you type, like a password). If your ntfy server gave you an **access token** (a key that lets NekoServer send to a private topic), type that too; most people leave it empty.
3. Setup sends one test message, called **NekoServer test**, and asks `Did the test message arrive on your phone?`. Look at the phone and answer.

Answered no? The address is hidden while you type, so a typo cannot be seen. Check the address in the ntfy app, and that the phone lets ntfy show notifications; then run `sudo bash install.sh` again: it asks for the address again and sends a new test.

Enter alone at the address (in the setup window: **Skip for now**) leaves alerts for later. Nothing else waits for them.

## Later

- **Alerts stopped arriving?** The dashboard then says *Phone alerts are not being delivered*. Run `sudo bash install.sh` in a terminal: it sends a test message, and when the alert server refuses the saved address or access token (a token taken back in ntfy, say), it asks for both again and tests the new ones. A network problem is said as one, and the step waits for the next run.
- **To change the address later:** put the new one in `"endpoint"` under `"notifications"` in `my-config.json`, then run `sudo bash install.sh --module notifications --apply` in a terminal and answer yes: it asks you to confirm the switch and asks for the new topic's token. Without a terminal, alerts keep going to the earlier topic.
- **To switch alerts off:** run `sudo bash install.sh` and switch **Send alerts to my phone** off on the first page.
- **The whole PC is off?** A PC that is off cannot send its own alert. For that, set up the outside heartbeat ([HEARTBEAT.md](HEARTBEAT.md)): a service outside your home that warns you when the server stops checking in.

Who can read your alerts: on the public ntfy server (`ntfy.sh`), anyone who knows a topic's address can read it without signing in. A long random name makes it hard to guess; it is not a lock. The alerts name what is wrong ("the media drive is missing"), never your files, passwords or addresses. For a topic only you can read, use an ntfy server with accounts and an access token.

---

## For administrators

The rest of this page is the technical reference.

Notifications run through a Rust systemd worker (`nekoserver-notifications.timer`, every minute) even when the dashboard browser is closed. Delivery uses an HTTPS ntfy topic on your chosen provider or self-hosted ntfy server. Use an access-controlled topic and appropriate publishing credentials: an obscure public topic name alone is not access control.

The installer does not create an ntfy account, reserve a topic or install a phone app. Hosting, subscriptions, provider limits and any charges remain your choice. The only message setup sends is the test message, to the address you typed.

### The step on its own

The worker is part of NekoServer's own program, which setup builds on this PC. curl and the system CA certificates must be available (setup installs them).

```bash
python3 installer.py --module notifications --check
sudo bash install.sh --module notifications --apply
```

The step asks for the full topic address, such as `https://ntfy.example.com/my_private_topic`, and an optional bearer access token; both are typed hidden, in the terminal or in the setup window. They are kept in root-only `/etc/nekoserver-notifications/config.json`, outside the installer config, dashboard, service command arguments and copied installer release bundles. A rerun preserves the existing destination/token and refreshes timings. You can also put the topic address in `notifications.endpoint` in `my-config.json`: setup then asks only for its token. The private file itself must stay `0600`, owned by root; do not paste it into logs or support reports.

If installation is noninteractive and no destination exists, the module returns `needs_setup` without enabling a new timer. It does not silently claim successful delivery. The `full` profile includes notifications, and an update adds the step to the profiles of an older `my-config.json` (a step you took out yourself stays out). Delivery credentials/state are OS-local: after replacing Linux, setup asks for the address again.

Only HTTPS URLs with a hostname/IP, optional port and a single topic path are accepted. URL credentials, query tokens and redirects are refused. TLS verification stays enabled. A self-hosted private CA must be trusted normally by the server. The sender bypasses proxy environment settings; direct network access to the provider is required.

### Test on your phone

```bash
sudo /usr/local/lib/nekoserver-notifications/v0.51.3/nekoserver-runtime notify \
  --config /etc/nekoserver-notifications/config.json \
  --state /var/lib/nekoserver-notifications \
  --runtime /run/nekoserver-notifications \
  --test
systemctl status nekoserver-notifications.timer
sudo journalctl -u nekoserver-notifications.service --no-pager
```

The explicit test sends a labelled test message and records whether the provider accepted it; it says in the terminal what happened and what to check. Check receipt on the subscribed phone with the dashboard closed. An alert server takes a message for a topic name with a typo in it just the same (ntfy.sh makes any topic it is sent to), so "accepted" does not prove the address is the one your phone follows. During setup, answering that the test did not arrive leaves the step waiting, and the next `sudo bash install.sh` asks for the address again (changing it: "Later", above). Provider acceptance is not proof of notification display, delivery latency or phone permissions. Test the actual phone/network/provider combination, including any ntfy requirements for iOS/self-hosted push.

### Alerts and grouping

| Condition | Behavior |
| --- | --- |
| Missing/wrong mounted drive | Immediately eligible for a critical alert; subject to global delivery spacing/backoff |
| Low disk space, SMART warnings, CPU temperature high for five minutes (or at the chip's own limit) | Warn from current collector findings |
| Tailscale signed out or its sign-in running out, monthly backups without a backup password, the outside heartbeat not reached, phone alerts that could not be delivered for a while | Warn like the others, so a problem shown on the dashboard also reaches the phone |
| Failed/overdue photo or media application backups | Report the affected job; a running/unknown job does not establish recovery |
| Media app unavailable/unhealthy | Wait through the configured debounce to avoid brief restart interruptions |
| Failed/stale media connection checks | Report the media connection audit status; no raw API response is sent |
| Missing, malformed or stale dashboard monitoring | Report monitoring unavailability and retain previously reported problems |
| Previously reported problem confirmed healthy | Group a recovery notice with other eligible updates |

Default timings are a 60-second worker interval, 180-second debounce, 5-minute minimum between normal batches and a 12-hour repeat interval for persistent problems. Set `notifications.debounce_seconds`, `notifications.minimum_interval_seconds` and `notifications.repeat_seconds` in your installer config and reapply the module. Repeats and escalation join a single batch; brief problems that were never delivered produce no recovery message.

HTTP failures, authentication rejection, rate limiting and network errors keep the alert unsent and schedule exponential retry delays from one minute up to one hour. A retry time more than an hour ahead can only come from a clock that was ahead and was put back, so it is ignored and the alert is sent at the next check instead of waiting until the clock reaches it again. Success alone advances the delivered ledger. Changing the private destination or token starts a new destination ledger. Delivery is at least once: a crash after the provider accepts a message but before local state is committed can cause a duplicate.

### Monitoring and permissions

The worker reads the sanitized collector snapshot and uses fixed approved alert labels. It sends no source paths, API keys, raw exception text, SQL, personal filenames or arbitrary snapshot titles. Native process errors stay generic. Temporary HTTPS configuration is root-only and removed after each completed send; a killed worker may leave it inside the private runtime directory until overwritten or reboot.

The OS-local ledger is private under `/var/lib/nekoserver-notifications`; transient delivery status is under `/run/nekoserver-notifications`. This lets the worker run when the application SSD is missing. If its record of sent alerts (`state.json` there) is ever damaged (a power cut while it was written, say), it is moved aside as `state.json.damaged-<date>`, the service log names both files, and alerts start fresh; a problem already sent may be sent once more. The service has a read-only system view, private state/runtime directories and no Docker socket access. The root collector publishes only sanitized delivery status to the unprivileged dashboard. The dashboard shows missing setup, inactive scheduling, stale status, failed delivery or last provider acceptance. Existing browser-local notifications remain a separate option.

Notifications require the PC to be powered, the OS/service to function and outbound connectivity. A total PC outage cannot be reported by that same PC: for that, set up the outside heartbeat (HEARTBEAT.md), which an independent monitor watches. Backup snapshots can stop apps longer than the debounce on large datasets; adjust the schedule/debounce if maintenance alerts are unwanted.

### Acceptance

```bash
# In the NekoServer folder (it reads your my-config.json). Add --manual to record the
# tests you witness yourself; a test recorded before is kept by later checks.
sudo python3 acceptance.py --profile full --save
```

Automatic checks inspect the native schedule, protected configuration, timer and provider acceptance. A separate witnessed check requires actual phone receipt with the browser closed, problem/recovery delivery and safe network-interruption recovery. Real reboot/systemd/provider/phone behavior remains pending until tested on the installed server.

Protocol references: [ntfy publishing and authentication](https://docs.ntfy.sh/publish/), [ntfy phone subscriptions](https://docs.ntfy.sh/subscribe/phone/), [curl transport options](https://curl.se/docs/manpage.html).
