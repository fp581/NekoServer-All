#!/usr/bin/env python3
"""Explicit one-time Docker migration; does not run as part of install.sh."""
import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path
from modules import docker


def cmd(args, timeout=1200):
    return docker.call(args, timeout=timeout)


def migrate(root, apply=False, config=None):
    root = Path(root)
    _, source = docker.mounted_data_root(root)
    target = root / 'docker-engine'
    old = Path('/var/lib/docker')
    daemon_file = Path('/etc/docker/daemon.json')
    if config is None: raise RuntimeError('Full storage configuration required for guarded migration')
    if daemon_file.exists():
        original=json.loads(daemon_file.read_text())
        if original.get('data-root',str(old)) != str(old): raise RuntimeError('Migration source differs from /var/lib/docker')
        conf=dict(original);conf['data-root']=str(target)
    else: conf={'data-root':str(target)}
    if not old.is_dir() or not any(old.iterdir()):
        raise RuntimeError('No source Docker data at /var/lib/docker; migration not needed')
    if target.exists() and any(target.iterdir()):
        raise RuntimeError('Destination already contains data; refusing to merge or overwrite')
    for program in ('rsync', 'systemctl', 'findmnt', 'docker'):
        if not shutil.which(program):
            raise RuntimeError(f'Missing required tool: {program}')
    docker.verify_classic_store()
    conf.setdefault('features',{})['containerd-snapshotter']=False
    print(f'Migration: {old} -> {target} (SSD {source})')
    if not apply:
        print('DRY RUN ONLY: use sudo python3 migrate-docker.py --apply after backups')
        return
    if os.geteuid() != 0:
        raise RuntimeError('--apply requires root')
    # Preserve existing source and config for operator-controlled rollback.
    cmd(['systemctl', 'stop', 'docker.service', 'docker.socket'])
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'docker.service']).returncode == 0:
        raise RuntimeError('Docker remains running; refusing migration')
    if not target.exists():
        target.mkdir(mode=0o700)
    # rsync exit code is checked; a partially copied target intentionally blocks rerun until reviewed.
    cmd(['rsync', '-aHAX', '--numeric-ids', str(old) + '/', str(target) + '/'], timeout=3600)
    if not any(target.iterdir()):
        raise RuntimeError('Destination empty after copy')
    backup = daemon_file.with_name('daemon.json.nekoserver.bak')
    if daemon_file.exists():
        if backup.exists():
            raise RuntimeError('Existing daemon.json backup; review before migrating')
        shutil.copy2(daemon_file, backup)
    docker.write_atomic(daemon_file, json.dumps(conf, indent=2) + '\n')
    override = Path('/etc/systemd/system/docker.service.d/10-nekoserver-storage.conf')
    docker.install_storage_guard(config)
    cmd(['systemctl', 'start', 'docker.service'])
    actual = cmd(['docker', 'info', '--format', '{{.DockerRootDir}}'])
    if actual != str(target):
        cmd(['systemctl', 'stop', 'docker.service'])
        raise RuntimeError(f'Docker started on unexpected data root: {actual}')
    print('MIGRATION COMPLETE. Old /var/lib/docker retained for manual rollback; do not delete until verifying containers and volumes.')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--config', default=str(Path(__file__).resolve().parent / 'config.json'))
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text())
    migrate(cfg['data_mount'], args.apply, cfg)
