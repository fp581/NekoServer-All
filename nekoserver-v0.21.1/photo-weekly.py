#!/usr/bin/env python3
"""Mount configured external photo backup drive, copy only new photos, safely unmount."""
import argparse, importlib.util, os, subprocess, sys, fcntl
from pathlib import Path
from neko_common import verify_storage, safe_path, mount_path
from job_status import execute
HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('photo_copy',HERE/'photo-backup.py'); copy=importlib.util.module_from_spec(spec);spec.loader.exec_module(copy)

def run(uuid,fstype,source,destination,media_root=None):
    if os.geteuid()!=0: raise RuntimeError('Scheduled backup must run as root')
    source=safe_path(source); destination=mount_path(destination)
    if fstype not in ('ext4','xfs','btrfs','exfat'): raise RuntimeError('Unsupported external filesystem')
    if not uuid or '/' in uuid or ' ' in uuid: raise RuntimeError('Unsafe UUID')
    if not source.is_dir() or source.is_symlink() or destination.is_symlink(): raise RuntimeError('Unsafe backup location')
    # Source must be the expected mounted media HDD; do not back up empty path on OS drive.
    expected_source=mount_path(media_root) if media_root else source.parents[1] # /mnt/nekoserver-media/photos/archive -> /mnt/nekoserver-media
    if source.resolve() == destination.resolve() or destination.resolve() in source.resolve().parents or source.resolve() in destination.resolve().parents:
        raise RuntimeError('Source and destination must be separate')
    if expected_source.resolve() not in source.resolve().parents:
        raise RuntimeError('Archive is outside the media mount')
    source_mount=subprocess.run(['findmnt','-n','--mountpoint',str(expected_source)],capture_output=True)
    if source_mount.returncode: raise RuntimeError(f'Media drive not mounted at {expected_source}')
    device=Path('/dev/disk/by-uuid')/uuid
    if not device.exists(): raise RuntimeError('External drive missing; nothing copied')
    actual=subprocess.check_output(['blkid','-s','TYPE','-o','value',str(device)],text=True).strip()
    if actual!=fstype: raise RuntimeError('External drive filesystem mismatch')
    destination.mkdir(parents=True,exist_ok=True)
    if any(destination.iterdir()) and subprocess.run(['findmnt','-n','--mountpoint',str(destination)],stdout=subprocess.DEVNULL).returncode:
        raise RuntimeError('Refusing to mount over nonempty destination')
    already=subprocess.run(['findmnt','-n','--mountpoint',str(destination)],stdout=subprocess.DEVNULL).returncode==0
    mounted_here=False
    try:
        if not already:
            subprocess.run(['mount','-o','rw,nosuid,nodev','UUID='+uuid,str(destination)],check=True,timeout=30)
            mounted_here=True
        copy.validate_mount(destination,uuid)
        copy.backup(source,destination,True)
    finally:
        if mounted_here: subprocess.run(['umount',str(destination)],check=True,timeout=45)

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--uuid',required=True);ap.add_argument('--fstype',required=True);ap.add_argument('--source',required=True);ap.add_argument('--dest',required=True);ap.add_argument('--media-root');ap.add_argument('--data'); ap.add_argument('--config',required=True);a=ap.parse_args()
    import json
    config=json.loads(safe_path(a.config).read_text())
    if config['data_mount'] != a.data or config['media_mount'] != a.media_root:
        raise RuntimeError('Backup paths must match verified configuration')
    verify_storage(config)
    verify=lambda:verify_storage(config, ('data_ssd',))
    def operation():
        run(a.uuid,a.fstype,a.source,a.dest,a.media_root)
        verify_storage(config)
    with open('/run/nekoserver-photo-backup.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        execute(Path(a.data) if a.data else None,'external',operation, verify=verify)
if __name__=='__main__':
    try: main()
    except Exception as exc: print('Photo backup FAILED:',exc,file=sys.stderr);sys.exit(1)
