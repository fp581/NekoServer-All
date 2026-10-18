# v0.18.0 schedule policy

Routine NekoServer media checkups are disabled, music audio has no automatic backup, and managed photo originals/database/external backups run weekly on Sunday night. Monthly update/recovery checks and app-state backups remain. [STORAGE.md](STORAGE.md) defines the current policy and supersedes older schedule/coverage descriptions below.

# Server OS power, logs and memory

The `server_os` module adds OS-wide safeguards to the existing GNOME TV power settings. New sample profiles include it. Existing customized profiles are preserved when upgrading; install the module explicitly with your own confirmed configuration.

```bash
python3 installer.py --module server_os --check
sudo bash install.sh --module server_os --apply
```

## Sleep and display

The module masks sleep, suspend, hibernate, hybrid-sleep and suspend-then-hibernate targets, installs a managed sleep policy that disables those operations and sets logind idle action to ignore. Normal desktop/systemd sleep requests are blocked. Display blanking can remain enabled: the TV going dark does not need to suspend the server. The TV module also retains its GNOME AC idle-suspend setting.

Existing customized target units/links or conflicting files at the module's owned drop-in paths are preserved and reported for review. OS masks apply immediately. The module reloads systemd and restarts journald but does not restart logind or interrupt desktop sessions; its logind idle setting loads on the next ordinary reboot. Administrator drop-ins may override configuration values, so acceptance checks both managed files and the live masked target state.

```bash
systemctl show suspend.target hibernate.target sleep.target --property=LoadState
systemd-analyze cat-config systemd/sleep.conf
systemd-analyze cat-config systemd/logind.conf
```

This policy covers standard OS sleep paths. Physical shutdown, loss of electricity, kernel failure and privileged direct hardware control remain outside an OS sleep setting. BIOS/UEFI restore-after-power-loss and a UPS are separate hardware setup choices.

## HDD standby

Default `server_os.hdd_policy` is `idle`, with `hdd_idle_minutes: 60`. The native worker applies a 60-minute standby timer (`hdparm -S 242`). See STORAGE.md for configurable intervals and wake-up sources. The optional `keep_spinning` policy remains supported. The installer installs hdparm through pacman when available and deploys a native Rust boot job. That job verifies the configured SSD/HDD UUIDs and filesystems, resolves the media filesystem to its physical parent disk, checks that it is a rotating internal SATA disk and configures only that disk's legacy standby timer (`hdparm -S 0` for `keep_spinning`). It resolves/checks the device again immediately before the command. No guessed `/dev/sda`, blanket drive command, periodic dummy reads or SSD power changes are used.

USB bridges, RAID/device-mapper and other unsupported transports are refused with setup pending. An accepted command means the legacy standby command succeeded; it does not prove every drive firmware/controller policy obeys it. Firmware EPC/APM, head parking and bridge idle timers are separate and are not modified automatically. Observe the intended drive after reboot and an idle period before calling its spindle policy verified.

```bash
systemctl status nekoserver-hdd-policy.service
sudo journalctl -u nekoserver-hdd-policy.service --no-pager
```

Keeping a NAS-rated drive spinning may suit an always-on workload and avoid repeated standby/wake transitions. It is not a universal promise of longer life; drive design, access patterns, temperature and power use matter. Use the actual model's guidance. SMART health/temperature monitoring remains enabled where supported and continues avoiding unnecessary wake requests.

Set `hdd_policy` to `unchanged` if you want manual/vendor power management. Reapplying that choice disables an earlier managed boot job without guessing replacement hardware settings. It does not restore a previous timer already changed in hardware; review the original settings and model guidance explicitly.

## System log bounds

Defaults are `SystemMaxUse=512M`, `RuntimeMaxUse=64M`, `MaxRetentionSec=14day` and `SystemKeepFree=1G`. Configure `journal_disk_mb`, `journal_ram_mb` and `journal_retention_days` under `server_os` to change them. Existing records are not explicitly vacuumed by the installer; normal journald rotation/retention enforces the new policy with journald's active-file granularity.

`RuntimeMaxUse` bounds journal files stored in the RAM-backed `/run` filesystem, not every byte of the journald process's memory. These limits cover the OS systemd journal. Docker/app logs, media downloads and application caches have separate policies; this release does not impose a new Docker logging driver on existing containers.

## RAM

Linux uses spare RAM for page cache and reclaims it when needed. Low *free* RAM by itself is normal. The dashboard's memory use is already calculated from `MemTotal - MemAvailable`, accounting for memory the kernel expects to reclaim. New warnings begin when available RAM falls to about 10% of total; critical alerts begin around 3%. Background delivery uses the existing debounce/grouping/retry settings and requires a configured ntfy destination.

The module does not install a cache-flushing timer, arbitrary swappiness tuning, automatic app restarts or guessed per-container RAM caps. Existing swap/zram settings remain intact. A genuinely leaking application, too many workloads or large app databases can still exhaust RAM: inspect process/container usage and swap activity, then fix or limit the specific workload. Cache flushing does not repair an application leak and can cause unnecessary I/O.

## Acceptance and reversal

```bash
sudo python3 acceptance.py --profile full --save
# Reboot normally and repeat; perform the server_idle witnessed check.
```

Automatic checks inspect the live masks, exact managed drop-ins, loaded native HDD command/guard and enabled successful HDD unit. Witnessed acceptance still requires real idle/reboot/network-job and HDD behavior checks. Fixture tests do not establish actual spindle behavior or hardware compatibility.

To reverse the managed sleep policy, stop selecting `server_os` in your profiles, review/remove only its marked drop-ins and explicitly unmask only the five targets it owns. Reboot normally to load the chosen logind policy. Restore HDD settings according to the original/model-specific policy; stopping a service alone does not undo a device command.

References: [Linux memory availability](https://www.kernel.org/doc/html/latest/filesystems/proc.html), [kernel warning against routine drop_caches](https://kernel.org/doc/html/latest/admin-guide/sysctl/vm.html), [systemd sleep policy](https://www.freedesktop.org/software/systemd/man/latest/systemd-sleep.conf.html), [journald limits](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html), [Seagate NAS design guidance](https://www.seagate.com/products/nas-drives/ironwolf-hard-drive/health/).
