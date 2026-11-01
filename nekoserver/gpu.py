"""The server's graphics chips: what they are, their drivers, and their render devices.

Shared by the TV step (Vulkan for Steam) and the hardware video step (Jellyfin and
Immich transcoding).
"""

from pathlib import Path
import re
import subprocess

# Steam / games (TV step).
VULKAN = {'intel': ['vulkan-intel', 'lib32-vulkan-intel'], 'amd': ['vulkan-radeon', 'lib32-vulkan-radeon']}
# Video decode/encode on the server itself, plus vainfo to check it. The apps' own
# containers bring their own drivers; these are for the host and for checking.
VIDEO = {'intel': ['intel-media-driver', 'libva-utils'], 'amd': ['libva-mesa-driver', 'libva-utils']}
# PCI vendor IDs as /sys reports them.
PCI_VENDORS = {'0x8086': 'intel', '0x1002': 'amd', '0x10de': 'nvidia'}


def graphics(lspci=None, drm=Path('/sys/class/drm')):
    """Vendors of the display adapters: 'intel', 'amd' and/or 'nvidia'. From lspci, and
    from the kernel's own list (/sys/class/drm) where lspci isn't installed, so an
    NVIDIA-only PC still gets the NVIDIA explanation."""
    if lspci is None:
        try:
            lspci = subprocess.run(['lspci', '-mm'], capture_output=True, text=True, timeout=30, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            lspci = ''
    found = []
    for vendor in [*lspci_vendors(lspci), *drm_vendors(drm)]:
        if vendor not in found:
            found.append(vendor)
    return found


def lspci_vendors(lspci):
    out = []
    for line in lspci.splitlines():
        if not re.search(r'"(VGA compatible controller|3D controller|Display controller)"', line):
            continue
        lower = line.lower()
        out += [
            vendor
            for vendor, marks in (
                ('intel', ('intel',)),
                ('amd', ('advanced micro devices', 'amd/ati', 'ati technologies')),
                ('nvidia', ('nvidia',)),
            )
            if any(mark in lower for mark in marks)
        ]
    return out


def drm_vendors(drm):
    out = []
    for entry in sorted(Path(drm).glob('card*')):
        try:
            vendor = PCI_VENDORS.get((entry / 'device/vendor').read_text().strip())
        except OSError:
            continue
        if vendor:
            out.append(vendor)
    return out


def vulkan_packages(vendors):
    return [p for vendor in vendors for p in VULKAN.get(vendor, [])]


def render_nodes(drm=Path('/sys/class/drm')):
    """[(vendor, '/dev/dri/renderD128'), ...] for every graphics chip with a render device,
    in device order. Two chips of one vendor (two Intel, say) are both listed: when the
    first fails the video check, the second is tried."""
    nodes = []
    for entry in sorted(Path(drm).glob('renderD*')):
        try:
            vendor = PCI_VENDORS.get((entry / 'device/vendor').read_text().strip())
        except OSError:
            continue
        if vendor and Path('/dev/dri', entry.name).exists():
            nodes.append((vendor, '/dev/dri/' + entry.name))
    return nodes


def video_choices(nodes):
    """(vendor, render device, Jellyfin method, Immich method) for every usable chip, the
    preferred first: a machine with Intel and AMD graphics tries Intel, then AMD, and each
    vendor's chips in device order. Intel uses Quick Sync; AMD uses VA-API. NVIDIA needs
    the proprietary driver and container toolkit, which are not installed automatically."""
    return [
        (vendor, node, jellyfin, immich)
        for vendor, jellyfin, immich in (('intel', 'qsv', 'qsv'), ('amd', 'vaapi', 'vaapi'))
        for found, node in nodes
        if found == vendor
    ]
