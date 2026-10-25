# Background health notifications

## Guided setup

New interactive subscription setup opens the phone subscription instructions and waits for yes/no; no reopens them. Private destination/token entry stays in the terminal. Real delivery receipt still needs its witnessed acceptance check.


Notifications run through a Rust systemd worker even when the dashboard browser is closed. Delivery uses an HTTPS **ntfy topic** on your chosen provider or self-hosted ntfy server. Subscribe the intended phone to that topic. Use an access-controlled topic and appropriate publishing credentials: an obscure public topic name alone is not access control.

The installer does not create an ntfy account, reserve a topic, install a phone app or guess a recipient. Hosting, subscriptions, provider limits and any charges remain your choice. No external notification was sent while building/testing this release; delivery tests used local fixtures.

## Setup

Keep your existing confirmed configuration. The native executable is included; a Rust compiler is not needed on the server. curl and the system CA certificates must be available.

```bash
python3 installer.py --module notifications --check
sudo bash install.sh --module notifications --apply
```

Installation asks privately for the full topic URL, such as `https://ntfy.example.com/my_private_topic`, and an optional bearer access token. The token is kept in root-only `/etc/nekoserver-notifications/config.json`, outside the installer config, dashboard, service command arguments and copied installer release bundles. A rerun preserves the existing destination/token and refreshes timings. You can also put the topic URL in `notifications.endpoint` in `my-config.json`: setup then asks only for its token. To send alerts to another topic later, change `notifications.endpoint` and run `sudo bash install.sh --module notifications --apply` in a terminal: it asks you to confirm the switch and asks for the new topic's token (without a terminal, alerts keep going to the earlier topic). To change only the token, edit this private file locally as root; do not paste it into logs or support reports. Its permissions must remain `0600`, owned by root.

If installation is noninteractive and no destination exists, the module returns `needs_setup` without enabling a new timer. It does not silently claim successful delivery. The new `full` sample profile includes notifications; older configured profiles are preserved, so use the explicit module command when upgrading. Delivery credentials/state are currently OS-local and need separate protected recovery after an OS replacement.

Only HTTPS URLs with a hostname/IP, optional port and a single topic path are accepted. URL credentials, query tokens and redirects are refused. TLS verification stays enabled. A self-hosted private CA must be trusted normally by the server. The sender bypasses proxy environment settings; direct network access to the provider is required.

## Test on your phone

```bash
sudo /usr/local/lib/nekoserver-notifications/v0.39.6/nekoserver-runtime notify \
  --config /etc/nekoserver-notifications/config.json \
  --state /var/lib/nekoserver-notifications \
  --runtime /run/nekoserver-notifications \
  --test
systemctl status nekoserver-notifications.timer
sudo journalctl -u nekoserver-notifications.service --no-pager
```

The explicit test sends a labelled test message and records whether the provider accepted it. Check receipt on the subscribed phone with the dashboard closed. Provider acceptance is not proof of notification display, delivery latency or phone permissions. Test the actual phone/network/provider combination, including any ntfy requirements for iOS/self-hosted push.

## Alerts and grouping

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

## Monitoring and permissions

The worker reads the sanitized collector snapshot and uses fixed approved alert labels. It sends no source paths, API keys, raw exception text, SQL, personal filenames or arbitrary snapshot titles. Native process errors stay generic. Temporary HTTPS configuration is root-only and removed after each completed send; a killed worker may leave it inside the private runtime directory until overwritten or reboot.

The OS-local ledger is private under `/var/lib/nekoserver-notifications`; transient delivery status is under `/run/nekoserver-notifications`. This lets the worker run when the application SSD is missing. The service has a read-only system view, private state/runtime directories and no Docker socket access. The root collector publishes only sanitized delivery status to the unprivileged dashboard. The dashboard shows missing setup, inactive scheduling, stale status, failed delivery or last provider acceptance. Existing browser-local notifications remain a separate option.

Notifications require the PC to be powered, the OS/service to function and outbound connectivity. A total PC outage cannot be reported by that same PC: for that, set up the outside heartbeat (HEARTBEAT.md), which an independent monitor watches. Backup snapshots can stop apps longer than the debounce on large datasets; adjust the schedule/debounce if maintenance alerts are unwanted.

## Acceptance

```bash
# In the NekoServer folder (it reads your my-config.json). Add --manual to record the
# tests you witness yourself; a test recorded before is kept by later checks.
sudo python3 acceptance.py --profile full --save
```

Automatic checks inspect the native schedule, protected configuration, timer and provider acceptance. A separate witnessed check requires actual phone receipt with the browser closed, problem/recovery delivery and safe network-interruption recovery. Real reboot/systemd/provider/phone behavior remains pending until tested on the installed server.

Protocol references: [ntfy publishing and authentication](https://docs.ntfy.sh/publish/), [ntfy phone subscriptions](https://docs.ntfy.sh/subscribe/phone/), [curl transport options](https://curl.se/docs/manpage.html).
