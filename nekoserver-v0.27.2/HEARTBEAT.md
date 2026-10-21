# Independent outage detection

## Guided setup

New interactive provider setup opens external monitor guidance and waits for yes/no; no reopens it. Configure the chosen independent provider and recipient yourself, then paste the private success-ping URL into the hidden Konsole prompt. Real missed-ping/receipt acceptance remains separate.


The `heartbeat` module installs an OS-local Rust oneshot worker and timer. Every five minutes by default it sends a small HTTPS GET to your monitor's private success-ping URL. It needs neither a mounted data drive nor Docker/the dashboard to run. This proves that this OS job can execute and reach that endpoint; it does not prove every app/storage/backup is healthy. Existing dashboard and ntfy monitoring handle those checks separately.

The missing-ping monitor must run **outside this PC**, with power/network independent of the failure you want to detect. Healthchecks.io or an independently hosted compatible endpoint is one option; no provider/account is created automatically. A monitor on this server cannot report its own power loss. A second PC on the same power/internet may share the same failure. Missing pings alone cannot distinguish a dead PC from broken internet/DNS/TLS or a disabled worker.

## Setup

1. Create a simple periodic check at your chosen outside monitor. Default: **period five minutes, grace ten minutes**. Alerts arrive approximately period plus grace after the last accepted ping, subject to the provider's delivery behavior. Match the period if you change `heartbeat.interval_seconds` (60..3600).
2. Configure and verify the monitor's alert integration/recipient. This is separate from your NekoServer ntfy subscription. No recipient is guessed and no email/phone messages are sent by the installer.
3. Copy the check's **base success-ping HTTPS URL**. Do not paste a `/start` or `/fail` URL. URL queries/fragments, embedded credentials, redirects and plaintext HTTP are refused. This release supports simple ASCII paths, including project-key/name routes; services requiring query-based authentication, headers or POST bodies need another client.
4. Apply the module interactively. The URL is prompted without echo and saved root-only outside the normal installer config.

```bash
python3 installer.py --module heartbeat --check
sudo bash install.sh --module heartbeat --apply
sudo systemctl start nekoserver-heartbeat.service
systemctl status nekoserver-heartbeat.timer
```

New `full` profiles include heartbeat. Existing customized profiles/configurations are preserved; select the module explicitly when upgrading. Blank/no URL in noninteractive setup reports `needs_setup` and never enables a fake successful monitor. If curl/CA certificates are absent, install them normally first. The endpoint is at `/etc/nekoserver-heartbeat/config.json` (root-only); authorized administrators can explicitly edit this file and reapply when changing destinations. Preserve the intended provider period/alert configuration separately.

The native binary is deployed under `/usr/local/lib/nekoserver-heartbeat/v0.27.2` (one folder per installed release). It uses the system CA trust, disables user curl configuration/proxy inheritance, bounds connection/request timeouts and refuses redirects/non-2xx responses. The secret URL is passed in a short-lived 0600 curl configuration, never in process arguments, the dashboard, public status or normal installer logs. Requests contain no logs, metrics, hostnames or application data; the receiving service necessarily sees the request URL, time and source address. Timer spacing bounds retries; no ping history is queued/replayed to hide a genuine gap.

The public runtime status contains only attempt/success times and pass/fail state. Failures preserve the last success without advancing it. Reboot clears this local runtime status; the external monitor retains its own history. A successful HTTP response proves only HTTP acknowledgement, not monitor registration or delivery to your phone/email. Healthchecks documents HTTP 200 even for unknown check UUIDs or rate-limited pings. Confirm the ping appears in the intended check before relying on it. Dashboard acceptance records that separately after you witness it. Selected host backups preserve the private configuration; treat those backups as secrets.

## Acceptance and maintenance

Run `sudo python3 acceptance.py --profile full --save` after applying current recovery/runtime modules and receiving a check-in. Its native/config check and current timer/check-in check are automatic. `outage_delivery` remains a witnessed check:

- Verify the provider has received a ping and the intended recipient/integration is enabled.
- Stop **only** the heartbeat timer using `sudo systemctl stop nekoserver-heartbeat.timer`.
- Wait longer than your period plus grace and witness the outside missed-ping alert. Don't stop the whole PC or unplug a data drive for this exercise.
- Restore it using `sudo systemctl enable --now nekoserver-heartbeat.timer` and `sudo systemctl start nekoserver-heartbeat.service`; witness provider recovery and another scheduled ping.

For planned outages pause the check at the outside monitor and remember to resume it. Merely stopping a local timer should produce the intended missed-ping alert. Do not treat fixtures or a monitor's HTTP acknowledgement as real phone receipt. The bundled tests contact only local TLS fixtures, not your provider.

References: [Healthchecks monitoring and period/grace behavior](https://healthchecks.io/docs/), [Pinging API](https://healthchecks.io/docs/http_api/).
