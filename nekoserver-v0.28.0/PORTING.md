# Supporting another Linux family

NekoServer is built in three layers. Most of the code never needs to know which Linux it runs on.

| Layer | What it holds | Where |
| --- | --- | --- |
| **Shared** | The apps, Docker Compose files, accounts, backups, the dashboard, the Rust runtime | everything else |
| **Family adapter** | Package manager, package names, service names, the family's own settings files | [`host.py`](host.py) |
| **Capability detection** | What this computer can do, found by looking instead of guessing from the name | [`capabilities.py`](capabilities.py) |

## Fixed requirements

Every supported system must have all of these. Setup checks them first and stops before changing anything when one is missing:

- an **x86-64** processor
- **systemd** 247 or newer (NekoServer's services, timers and storage guards are systemd units)
- **Python 3.11** or newer (to run setup)
- **Docker Engine** with the Compose plugin (setup installs them when missing)
- a **supported Linux family**, and no restart left pending by an earlier update

## What a family adapter provides

Each family is a small class in `host.py`:

```python
class Arch(Family):
    name = 'arch'
    label = 'Arch Linux / Nyarch'
    manager = 'pacman'
    ids = ('arch', 'nyarch')          # /etc/os-release ID
    like = 'arch'                     # /etc/os-release ID_LIKE word of derivatives
    services = {'samba': 'smb.service', 'tailscale': 'tailscaled.service'}
    settings = ('/etc/pacman.conf', '/etc/pacman.d/mirrorlist')

    def available(self, wanted): ...  # the repositories offer it
    def installed(self, wanted): ...
    def _install(self, names, timeout): ...
```

A family can also override:

- `prepare(missing)`, run before installing: conflict checks and vendor repositories. Debian refuses its own `docker.io` next to Docker's packages, and adds Docker's and Tailscale's repositories.
- `settle(missing)`, run after installing: undo what the packages did on their own. Debian's packages enable and start their services, so they are switched off again until NekoServer's own steps start them on the right drive.
- `reboot_pending(root)`: Debian and Ubuntu leave `/run/reboot-required` after an update that needs a restart.

- `packages` maps NekoServer's package names (the Arch names) to the family's own name or names, for example `'docker-compose': 'docker-compose-plugin'`.
- `services` maps a service to its systemd unit when the name differs. Samba is `smb.service` on Arch and Fedora, but `smbd.service` on Debian and Ubuntu.
- `settings` lists the family's package-manager settings, which host backups keep.
- `install()` is shared. It installs only the packages that are missing and never upgrades the rest of the system.

Shared code must never call a package manager itself. `tests/test_host.py` fails if `pacman`, `apt-get` or `dpkg` appears outside `host.py`. The only other place is `install.sh`, which installs Python and the basic tools before `host.py` can run, then calls `python3 host.py bootstrap` for the apps' packages.

## Rules that stay the same on every family

- **Storage.** A missing data disk must stop Docker, Samba and the apps rather than let them write to the system disk (`RequiresMountsFor=` drop-ins).
- **Docker.** An existing Docker installation and its data are never adopted or moved silently. Docker must not start before its data folder is on the SSD.
- **Apps listen on 127.0.0.1.** Ports Docker publishes skip host firewalls (ufw, firewalld), so the apps are reached only through the gateway, Tailscale and Samba, which are host services a firewall does govern.
- **Backups.** App data is portable between families. Host settings are not. A restore keeps numeric ownership but maps accounts on the destination, and it never blindly replaces the user database or a service's configuration.
- **Desktop features** (TV, OpenRGB lighting) are optional.

## Adding a family, step by step

1. Add the class to `host.py` and append it to `FAMILIES`.
2. Teach `install.sh` the family's bootstrap: how it is recognised, its update policy, the reboot-needed check and the basic tools' package names.
3. Map service names that differ. The Rust runtime lists every family's Samba unit (`runtime/src/collect.rs`, `host_snapshot.rs`) and uses the one that is installed.
4. Handle the family's security module. SELinux (Fedora) will need `:z` labels on bind mounts. AppArmor (on by default on Ubuntu) works with the profile Docker applies itself, so it needs nothing extra.
5. Check the firewall. Ports published by Docker bypass ufw and firewalld, so on such a family a firewall rule alone does not keep an app private. Setup must check which addresses each app port is published on.
6. Add tests with a fake package manager, as `tests/test_host.py` does for pacman.

## Status

| Family | Status |
| --- | --- |
| Arch Linux / Nyarch | Supported |
| Debian 12+ / Ubuntu 24.04+ (and systems built on them) | New in 0.28.0, not yet proven on a real install; TV setup is Arch-only for now |
| Fedora, openSUSE | Later |
