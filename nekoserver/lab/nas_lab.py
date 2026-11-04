#!/usr/bin/env python3
"""The interruption tests for drives on a network storage box (NAS) over iSCSI.

They need what GitHub's CI doesn't have (a kernel with iSCSI, a VM), so they run here,
before each release, on a Linux PC as root. They build an Ubuntu 24.04 VM (QEMU) as the
NekoServer PC and run a software iSCSI target (tgt) on this PC as the NAS. NekoServer's
own steps set the VM up: the NAS step (network_storage.py), the drive step (storage.py),
and the units server_os.py writes (drive-return and the NAS check). A stand-in service
plays the apps' writer. Then each scenario interrupts it and checks what NekoServer did.

  sudo python3 lab/nas_lab.py build              the VM image (once; about 10 minutes)
  sudo python3 lab/nas_lab.py run [SCENARIO...]  every scenario, or the ones named
  sudo python3 lab/nas_lab.py list               the scenarios

Needs: qemu-system-x86, qemu-utils, debootstrap, tgt, iptables, e2fsprogs, and a runtime
built from these sources (python3 runtime/build.py, or cargo build --release). Uses the
kernel installed on this PC (linux-image-generic). Everything lives in --work (default
/var/lib/neko-nas-lab); the NAS's disks are sparse files there.

Each scenario prints PASS or FAIL with what it saw; the exit code is the number failed."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
PORT = 3260
WRITER_LOG = 'writer.log'  # in the share: the writer's tries, one line each
TARGETS = {1: 'iqn.2026-11.lab.nas:media', 2: 'iqn.2026-11.lab.nas:backup'}
GB = 10**9
PACKAGES = (
    'systemd,systemd-sysv,dbus,udev,open-iscsi,iproute2,python3,util-linux,e2fsprogs,kmod,sudo,rsync,lsb-release,psmisc'
)
AGENT = """#!/bin/bash
# Runs each /lab/cmd/<n>.sh once: its output in /lab/out/<n>.txt, its exit code in <n>.done.
mkdir -p /lab/cmd /lab/out
while true; do
  for f in /lab/cmd/*.sh; do
    [ -e "$f" ] || continue
    n=$(basename "$f" .sh)
    [ -e /lab/out/$n.done ] && continue
    bash "$f" > /lab/out/$n.txt 2>&1; echo $? > /lab/out/$n.done
  done
  sleep 1
done
"""
# The apps' writer: a service that keeps writing to the media drive and logs each try.
WRITER = """#!/bin/bash
while true; do
  s=$(date +%s.%N)
  if timeout 900 bash -c 'echo x >> /mnt/nekoserver-media/writer.txt && sync -f /mnt/nekoserver-media/writer.txt' 2>/dev/null
  then r=ok; else r=FAIL; fi
  echo "$(date +%s) $r $(python3 -c "print(round($(date +%s.%N)-$s,1))")" >> /lab/writer.log
  sleep 1
done
"""


def sh(argv, check=True, **kw):
    return subprocess.run(argv, check=check, text=True, capture_output=True, **kw)


def kvm_works():
    """True when this PC can run the VM with KVM: /dev/kvm opens (it can be there with no
    KVM behind it, inside another VM)."""
    try:
        os.close(os.open('/dev/kvm', os.O_RDWR))
    except OSError:
        return False
    return True


class Lab:
    def __init__(self, work):
        self.work = Path(work)
        self.share = self.work / 'share'
        self.vm = None
        self.n = 0

    # ----- the NAS (tgt on this PC)
    def nas_up(self):
        if sh(['pgrep', '-x', 'tgtd'], check=False).returncode:
            subprocess.Popen(['tgtd', '-f'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
        sizes = {1: 300, 2: 120}
        for tid, iqn in TARGETS.items():
            lun = self.work / f'lun{tid}.img'
            if not lun.exists():
                sh(['truncate', '-s', f'{sizes[tid]}G', str(lun)])
            tgt = ['tgtadm', '--lld', 'iscsi', '--mode']
            sh([*tgt, 'target', '--op', 'new', '--tid', str(tid), '-T', iqn], check=False)
            sh([*tgt, 'logicalunit', '--op', 'new', '--tid', str(tid), '--lun', '1', '-b', str(lun)], check=False)
            sh([*tgt, 'target', '--op', 'bind', '--tid', str(tid), '-I', 'ALL'], check=False)

    def nas_off(self):
        """The NAS loses its power: gone at once, no goodbye to the PC."""
        sh(['pkill', '-9', '-x', 'tgtd'], check=False)
        time.sleep(1)

    def cut(self, on):
        """The network to the NAS cut (or put back): every packet to and from its port dropped."""
        for rule in (['--dport', str(PORT)], ['--sport', str(PORT)]):
            sh(['iptables', '-I' if on else '-D', 'INPUT', '-p', 'tcp', *rule, '-j', 'DROP'], check=not on and False)

    # ----- the PC (a QEMU VM)
    def fresh(self):
        """A fresh PC and fresh NAS disks for a run: the built image stays as it was (the
        run writes to a copy-on-write layer over it), and every disk starts empty, so no
        earlier run's settings, mounts or files are left."""
        self.nas_off()
        for name in ('run.qcow2', 'ssd.qcow2', 'lun1.img', 'lun2.img'):
            (self.work / name).unlink(missing_ok=True)
        (self.share / WRITER_LOG).unlink(missing_ok=True)
        sh(
            [
                'qemu-img',
                'create',
                '-q',
                '-f',
                'qcow2',
                '-F',
                'raw',
                '-b',
                str(self.work / 'root.img'),
                str(self.work / 'run.qcow2'),
            ]
        )
        sh(['qemu-img', 'create', '-q', '-f', 'qcow2', str(self.work / 'ssd.qcow2'), '250G'])

    def boot(self):
        kernel = max(Path('/boot').glob('vmlinuz-*-generic'))
        initrd = Path(str(kernel).replace('vmlinuz', 'initrd.img'))
        for stale in (*self.share.glob('cmd/*'), *self.share.glob('out/*')):
            stale.unlink()
        argv = [
            'qemu-system-x86_64', '-m', '2048', '-smp', '2', '-nographic', '-no-reboot',
            '-kernel', str(kernel), '-initrd', str(initrd),
            '-append', 'root=/dev/vda rw console=ttyS0 net.ifnames=0',
            '-drive', f'file={self.work / "run.qcow2"},format=qcow2,if=virtio',
            '-drive', f'file={self.work / "ssd.qcow2"},format=qcow2,if=virtio',
            '-netdev', 'user,id=n0', '-device', 'virtio-net-pci,netdev=n0',
            '-virtfs', f'local,path={self.share},mount_tag=lab,security_model=none,id=lab',
        ]  # fmt: skip
        if kvm_works():  # else QEMU's own CPU (slower, works anywhere)
            argv[1:1] = ['-enable-kvm']
        log = (self.work / 'console.log').open('w')
        self.vm = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.do('true', wait=900)

    def shut_down(self):
        if self.vm and self.vm.poll() is None:
            self.do('sync; systemctl poweroff', wait=5, check=False)
            try:
                self.vm.wait(240)
            except subprocess.TimeoutExpired:
                self.vm.kill()
        self.vm = None

    def do(self, script, wait=300, check=True):
        """Run `script` in the VM (through its agent); what it printed."""
        self.n += 1
        name = f'{self.n:04d}'
        (self.share / 'cmd' / f'{name}.sh').write_text(script + '\n')
        done = self.share / 'out' / f'{name}.done'
        deadline = time.time() + wait
        while not done.exists():
            if time.time() > deadline:
                if check:
                    raise RuntimeError(f'the VM did not answer within {wait} s: {script[:80]}')
                return ''
            time.sleep(1)
        said = (self.share / 'out' / f'{name}.txt').read_text()
        if check and done.read_text().strip() != '0':
            raise RuntimeError(f'{script[:80]} failed in the VM:\n{said[-1500:]}')
        return said

    def wait_for(self, script, minutes, every=10):
        """Ask until `script` exits 0 in the VM, at most `minutes`: True when it did."""
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            # The whole script quiet (findmnt prints its table), only its answer said.
            if (
                self.do(f'if ( {script} ) >/dev/null 2>&1; then echo yes; else echo no; fi', check=False).strip()
                == 'yes'
            ):
                return True
            time.sleep(every)
        return False

    def writes(self, since):
        """The writer's tries since `since` (seconds): [(ok, how long it took)]."""
        try:
            lines = (self.share / WRITER_LOG).read_text().splitlines()
        except OSError:
            return []
        tries = [line.split() for line in lines]
        return [(t[1] == 'ok', float(t[2])) for t in tries if len(t) == 3 and float(t[0]) >= since]


def build(lab):
    """The VM image: Ubuntu 24.04 with open-iscsi, this PC's kernel and modules, the agent;
    its SSD (an empty 250 GB disk) formatted ext4, as a person would."""
    work = lab.work
    work.mkdir(parents=True, exist_ok=True)
    (lab.share / 'cmd').mkdir(parents=True, exist_ok=True)
    (lab.share / 'out').mkdir(exist_ok=True)
    rootfs = work / 'rootfs'
    if not (rootfs / 'usr/sbin/iscsiadm').exists():
        shutil.rmtree(rootfs, ignore_errors=True)
        sh(['debootstrap', '--variant=minbase', '--components=main,universe', '--include=' + PACKAGES,
            'noble', str(rootfs), 'https://archive.ubuntu.com/ubuntu'])  # fmt: skip
    kernel = max(Path('/boot').glob('vmlinuz-*-generic')).name.removeprefix('vmlinuz-')
    # Into lib/modules/<kernel>: without the folder there, cp would make the copy lib/modules itself.
    (rootfs / 'lib/modules').mkdir(parents=True, exist_ok=True)
    sh(['cp', '-a', f'/lib/modules/{kernel}', str(rootfs / 'lib/modules/')])
    (rootfs / 'etc/hostname').write_text('lab\n')
    (rootfs / 'etc/fstab').write_text(
        '/dev/vda / ext4 defaults 0 1\nlab /lab 9p trans=virtio,version=9p2000.L,nofail 0 0\n'
    )
    (rootfs / 'lab').mkdir(exist_ok=True)
    (rootfs / 'etc/systemd/network/10-eth.network').write_text('[Match]\nName=eth*\n[Network]\nDHCP=yes\n')
    (rootfs / 'etc/modules-load.d/lab.conf').write_text('9p\n9pnet\n9pnet_virtio\niscsi_tcp\n')
    agent = rootfs / 'usr/local/bin/lab-agent'
    agent.write_text(AGENT)
    agent.chmod(0o755)
    units = rootfs / 'etc/systemd/system'
    (units / 'lab-agent.service').write_text(
        '[Unit]\nDescription=lab agent\nRequiresMountsFor=/lab\n[Service]\nExecStart=/usr/local/bin/lab-agent\n'
        '[Install]\nWantedBy=multi-user.target\n'
    )
    # The apps' stand-in: a writer to the media drive, started and stopped as Samba is.
    (units / 'smbd.service').write_text(
        '[Unit]\nDescription=stand-in for the apps (writes to the media drive)\nRequiresMountsFor=/mnt/nekoserver-media\n'
        '[Service]\nExecStart=/lab/writer.sh\n[Install]\nWantedBy=multi-user.target\n'
    )
    for name, target in (('lab-agent.service', 'multi-user'), ('smbd.service', 'multi-user')):
        link = units / f'{target}.target.wants' / name
        link.parent.mkdir(exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(f'/etc/systemd/system/{name}')
    networkd = units / 'multi-user.target.wants/systemd-networkd.service'
    networkd.unlink(missing_ok=True)
    networkd.symlink_to('/usr/lib/systemd/system/systemd-networkd.service')
    sh(['chroot', str(rootfs), 'passwd', '-d', 'root'])
    image = work / 'root.img'
    image.unlink(missing_ok=True)
    sh(['truncate', '-s', '8G', str(image)])
    sh(['mkfs.ext4', '-q', '-F', '-d', str(rootfs), str(image)])
    print(f'Built the lab VM in {work}')


def installed(lab):
    """NekoServer's NAS step, drive step and units, set up in the VM by its own code, from
    a fresh VM and fresh NAS disks (formatted as a person would after the NAS step)."""
    share = lab.share
    for old in ('neko', 'bin'):
        shutil.rmtree(share / old, ignore_errors=True)
    shutil.copytree(PROJECT, share / 'neko', ignore=shutil.ignore_patterns('target', '.git', '__pycache__', 'lab'))
    built = next(
        (
            p
            for p in (PROJECT / 'runtime/bin/nekoserver-runtime', PROJECT / 'runtime/target/release/nekoserver-runtime')
            if p.is_file()
        ),
        None,
    )
    if built is None:
        raise RuntimeError('Build the runtime first: python3 runtime/build.py (or cargo build --release in runtime/)')
    (share / 'bin').mkdir()
    shutil.copy2(built, share / 'bin/nekoserver-runtime')
    (share / 'writer.sh').write_text(WRITER)
    (share / 'writer.sh').chmod(0o755)
    (share / 'step.py').write_text(STEP)
    lab.do('systemctl stop smbd.service; mkfs.ext4 -q -F -L neko-ssd /dev/vdb')
    said = lab.do('python3 /lab/step.py nas', wait=600, check=False)
    lab.do(
        'for t in media backup; do mkfs.ext4 -q -F -L neko-$t /dev/disk/by-path/ip-10.0.2.2:3260-iscsi-iqn.2026-11.lab.nas:$t-lun-1; done; udevadm settle'
    )
    said += lab.do('python3 /lab/step.py drives', wait=600)
    said += lab.do('python3 /lab/step.py units', wait=300)
    lab.do('rm -f /lab/writer.log; systemctl start smbd.service')
    return said


# Run inside the VM: NekoServer's own steps with the answers a person would type.
STEP = r'''
import builtins, json, re, sys
sys.path.insert(0, '/lab/neko')
lines, current = [], {}
# Chosen by label (setup lists each drive's): sdX names change from one start to the next.
WANT = {'SSD': 'neko-ssd', 'HDD': 'neko-media', 'USB': 'neko-backup'}
def answer(prompt=''):
    if "NAS's address" in prompt:
        return '10.0.2.2'
    if 'Numbers of the ones' in prompt:
        return '1,2'
    if 'CHAP' in prompt or 'Number (Enter' in prompt:
        return ''
    role = 'SSD' if 'SSD' in prompt else 'HDD' if 'HDD' in prompt else 'USB'
    if prompt.lower().startswith(('number', 'choose')):
        for line in reversed(lines):
            m = re.match(r'\s+(\d+)\) (/dev/)?(\S+?)[: ].*named "' + WANT[role] + '"', line)
            if m:
                current['name'] = m.group(3)
                return m.group(1)
        return ''
    return current.get('name', '')
builtins.input = lambda prompt='': (print(prompt, end=''), answer(prompt))[1]
sys.stdin.isatty = lambda: True
CONFIG = '/lab/config.json'
class Ctx:
    apply, config_path = True, None
    def __init__(self, c): self.config = c
    def log(self, m):
        lines.extend(str(m).splitlines()); print(m)
    def persist_config(self): json.dump(self.config, open(CONFIG, 'w'), indent=1)
step = sys.argv[1]
if step == 'nas':
    c = json.load(open('/lab/neko/config.json'))
    c['network_storage'] = {'wanted': True}
    c['photos']['deploy_immich'] = True
    json.dump(c, open(CONFIG, 'w'), indent=1)
    from modules import network_storage
    print('RESULT', network_storage.run(Ctx(c)))
elif step == 'drives':
    from modules import storage
    c = json.load(open(CONFIG))
    ctx = Ctx(c)
    storage.run(ctx)
    ctx.persist_config()
elif step == 'units':
    import native_runtime, subprocess
    from modules import server_os
    c = json.load(open(CONFIG))
    binary = '/lab/bin/nekoserver-runtime'
    units = server_os.nas_watch_units(binary)
    devices = [server_os.drive_devices(c)[i] for i in (0, 1)]
    units['nekoserver-drive-return.service'] = server_os.drive_unit(binary, devices)
    for name, text in units.items():
        open('/etc/systemd/system/' + name, 'w').write(text)
    import os
    os.makedirs('/etc/nekoserver-os', exist_ok=True)
    json.dump(native_runtime.storage_config(c), open('/etc/nekoserver-os/drives.json', 'w'))
    for argv in (['daemon-reload'], ['enable', 'nekoserver-drive-return.service'], ['enable', '--now', 'nekoserver-nas-watch.timer']):
        subprocess.run(['systemctl', *argv], check=True)
'''


# ----- the scenarios: each returns (passed, what it saw)
def nas_starts_late(lab):
    """The PC starts while the NAS is off; the NAS comes on four minutes later. The drive
    must come back by itself, and the apps with it."""
    lab.shut_down()
    lab.nas_off()
    lab.boot()
    missing = lab.do('findmnt /mnt/nekoserver-media >/dev/null && echo mounted || echo missing').strip()
    time.sleep(240)
    lab.nas_up()
    back = lab.wait_for('findmnt /mnt/nekoserver-media && systemctl is-active --quiet smbd.service', 5)
    return missing == 'missing' and back, f'at start: {missing}; the drive and the apps back within 5 minutes: {back}'


def outage(seconds, label):
    def scenario(lab):
        lab.do('systemctl is-active --quiet smbd.service || systemctl start smbd.service')
        time.sleep(5)
        start = time.time()
        lab.cut(True)
        time.sleep(seconds)
        lab.cut(False)
        time.sleep(60)
        tries = lab.writes(start)
        failed = sum(1 for ok, _ in tries if not ok)
        longest = max((took for _, took in tries), default=0)
        writes = lab.do('touch /mnt/nekoserver-media/after && echo yes || echo no', check=False).strip()
        passed = failed == 0 and writes == 'yes' and longest >= seconds * 0.8
        return (
            passed,
            f'{label}: {len(tries)} writes, {failed} failed, the longest waited {longest} s; writes after: {writes}',
        )

    return scenario


def nas_power_cut(lab):
    """The NAS loses power for 40 s and starts again: writes wait, nothing fails."""
    lab.do('systemctl is-active --quiet smbd.service || systemctl start smbd.service')
    time.sleep(5)
    start = time.time()
    lab.nas_off()
    time.sleep(40)
    lab.nas_up()
    time.sleep(60)
    tries = lab.writes(start)
    failed = sum(1 for ok, _ in tries if not ok)
    return failed == 0 and tries, f'{len(tries)} writes, {failed} failed'


def outage_too_long(lab):
    """Past what the waits cover (shortened here, so the test takes minutes, not ten):
    the drive fails, the check stops the apps; the recovery menu repairs it and they start."""
    lab.do('for f in /sys/class/iscsi_session/session*/recovery_tmo; do echo 15 > $f; done; '
           'for f in /sys/block/sd*/device/timeout; do echo 5 > $f; done')  # fmt: skip
    lab.cut(True)
    time.sleep(90)
    lab.cut(False)
    stopped = lab.wait_for('! systemctl is-active --quiet smbd.service', 4)
    held = json.loads(lab.do('cat /run/nekoserver-nas-watch.json', check=False) or '{}').get('held')
    menu = (
        "import sys, json; sys.path.insert(0, '/lab/neko'); import recovery_wizard as w; "
        "a = iter(['y', 'y']); w.check_read_only(json.load(open('/lab/config.json')), lambda q='': next(a), print)"
    )
    repaired = lab.do(f'python3 -c "{menu}"', wait=900, check=False)
    started = lab.wait_for('systemctl is-active --quiet smbd.service', 3)
    # Back to the waits setup sets.
    lab.do('python3 /lab/step.py nas >/dev/null 2>&1', wait=600, check=False)
    passed = stopped and held is True and 'takes writes' in repaired and started
    return (
        passed,
        f'apps stopped: {stopped}; held: {held}; repaired: {"takes writes" in repaired}; apps back: {started}',
    )


def restart_with_nas(lab):
    """A plain restart: the drive is mounted after the network and the login, by itself."""
    lab.shut_down()
    lab.boot()
    back = lab.wait_for('findmnt /mnt/nekoserver-media && systemctl is-active --quiet smbd.service', 3)
    waits = lab.do(
        'cat /sys/block/sd*/device/timeout | sort -u; cat /sys/class/iscsi_session/session*/recovery_tmo | sort -u',
        check=False,
    ).split()
    return back and waits == ['120', '600'], f'mounted with the apps: {back}; waits after the restart: {waits}'


SCENARIOS = {
    'restart': restart_with_nas,
    'short-outage': outage(30, '30 s outage'),
    'long-outage': outage(200, '200 s outage'),
    'power-cut': nas_power_cut,
    'nas-starts-late': nas_starts_late,
    'outage-too-long': outage_too_long,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('action', choices=('build', 'run', 'list'))
    parser.add_argument('scenarios', nargs='*')
    parser.add_argument('--work', default='/var/lib/neko-nas-lab')  # root's alone, not a shared temp folder
    args = parser.parse_args()
    if args.action == 'list':
        for name, scenario in SCENARIOS.items():
            print(f'{name:16} {(scenario.__doc__ or "").split(chr(10))[0]}')
        return 0
    if os.geteuid() != 0:
        parser.error('Run it with sudo')
    lab = Lab(args.work)
    if args.action == 'build':
        build(lab)
        return 0
    names = args.scenarios or list(SCENARIOS)
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        parser.error('unknown scenarios: ' + ', '.join(unknown))
    failed = 0
    try:
        lab.fresh()
        lab.nas_up()
        lab.boot()
        print(installed(lab)[-600:])
        for name in names:
            try:
                passed, saw = SCENARIOS[name](lab)
            except RuntimeError as exc:
                passed, saw = False, str(exc)
            failed += not passed
            print(f'{"PASS" if passed else "FAIL"} {name}: {saw}', flush=True)
    finally:
        lab.cut(False)
        lab.shut_down()
    return failed


if __name__ == '__main__':
    sys.exit(main())
