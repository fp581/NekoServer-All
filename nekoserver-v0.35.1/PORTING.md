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
    like = ('arch',)                  # /etc/os-release ID_LIKE words of derivatives
    services = {'samba': 'smb.service', 'tailscale': 'tailscaled.service'}
    settings = ('/etc/pacman.conf', '/etc/pacman.d/mirrorlist')

    def available(self, wanted): ...  # the repositories offer it
    def has(self, package): ...       # this one package is installed
    def _install(self, names, timeout): ...
```

A family can also override:

- `prepare(missing)`, run before installing: conflict checks and vendor repositories. Debian refuses its own `docker.io` next to Docker's packages, and adds Docker's and Tailscale's repositories unless someone already defined them. It returns what `settle` must know about the system as it was before: whether `smb.conf` existed, and which services were already there.
- `settle(packages, before)`, run after installing, including after a failed one: undo what the packages did on their own. Debian's packages enable their services, so the new ones are switched off (and checked) until NekoServer's own steps start them on the right drive. Services that existed before are never touched.
- `journal`: where an installation is recorded before it starts, so one that fails, times out or is cut off is tidied up by `reconcile()`, at once when the package tools have stopped, or else at the next install or setup run.
- `protect(before)`, run just before installing: Debian masks the new services and writes a `policy-rc.d`, and both stay until `settle` has switched the services off.
- `locked()`: one package job at a time across processes (`/run/nekoserver-packages.lock`). `install.sh` also holds `/run/nekoserver-setup.lock` for the whole setup.
- `repair()`: brings NekoServer's own package-manager settings up to date on every setup run, whether or not anything needs installing. `install.sh` runs it (`python3 host.py repair`) before the package manager first runs.
- `reboot_pending(root)`: Debian and Ubuntu leave `/run/reboot-required` after an update that needs a restart.

- `packages` maps NekoServer's package names (the Arch names) to the family's own name or names, for example `'docker-compose': 'docker-compose-plugin'`.
- `services` maps a service to its systemd unit when the name differs. Samba is `smb.service` on Arch and Fedora, but `smbd.service` on Debian and Ubuntu.
- `settings` lists the family's package-manager settings, which host backups keep.
- `install()` is shared. It installs only the packages that are missing, never reinstalls or upgrades one that is there, and never upgrades the rest of the system.
- Commands whose output is read run with `LC_ALL=C`, so they answer in English on every system.

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
4. Handle the family's security module. SELinux (Fedora) is not supported yet: setup stops while it is enforcing (`capabilities.SELINUX_ENFORCING`). Docker CE runs containers without SELinux labels by default, so the open questions are the host services: Samba reading the HDD, Caddy binding ports 8442-8447, and systemd starting the runtime from the SSD. AppArmor (on by default on Ubuntu) works with the profile Docker applies itself, so it needs nothing extra.
5. Check the firewall. Ports published by Docker bypass ufw and firewalld, so on such a family a firewall rule alone does not keep an app private. Setup must check which addresses each app port is published on.
6. Add tests with a fake package manager, as `tests/test_host.py` does for pacman.

## Status

| Family | Status |
| --- | --- |
| Arch Linux / Nyarch (and systems built on them) | Supported; not yet proven on a real install |
| Debian 12+ / Ubuntu 24.04+ (and systems built on them) | Supported; not yet proven on a real install. TV setup is Arch/Nyarch-only for now |
| Fedora 42+ | Supported since 0.35.0; tried on Fedora 44 under WSL (no SELinux there). Setup stops while SELinux is enforcing until it is tested |
| RHEL 10+ / Rocky Linux / AlmaLinux / CentOS Stream | New (`Rhel`, built on the Fedora adapter): Docker's rhel/ or centos/ packages, Tailscale's rhel/<major>, Caddy and qrencode from EPEL. Tried on AlmaLinux 10 under WSL. Version 9 is refused (Python 3.9); Oracle Linux is not covered |
| openSUSE Leap 16+ / Tumbleweed / Slowroll | New (`Suse`, built on the Fedora adapter's rpm handling with zypper): everything from openSUSE's own repositories, no maker's repository. Tried on Leap 16 under WSL. Leap 15 is refused (Python 3.6); SLES is not covered |
