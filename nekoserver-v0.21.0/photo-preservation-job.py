#!/usr/bin/env python3
"""Periodic job: require expected mounted devices then archive originals and dump DB."""
import argparse,importlib.util,os,subprocess,sys,fcntl
from pathlib import Path
from neko_common import verify_storage, safe_path, relative_path, publish_no_replace
from job_status import execute
ROOT=Path(__file__).resolve().parent

def imported(filename,name):
    spec=importlib.util.spec_from_file_location(name,ROOT/filename);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def run(data,media,min_age,db_backup=False,archive_dir='photos/archive'):
    data=Path(data);media=Path(media)
    if os.geteuid()!=0:raise RuntimeError('Root needed for mounted storage and database backup')
    for directory in (data,media):
        if directory.is_symlink() or subprocess.run(['findmnt','-n','--mountpoint',str(directory)],stdout=subprocess.DEVNULL).returncode:raise RuntimeError(f'Expected mount missing: {directory}')
    source=data/'photos'/'immich-library';archive=safe_path(media/relative_path(archive_dir))
    preserve=imported('photo-preserve.py','neko_preserve')
    preserve.transfer(source,archive,True,min_age)
    if db_backup:
        db=imported('immich-db-backup.py','neko_db')
        created=db.backup(data/'compose'/'immich'/'compose.yaml',data/'compose'/'immich'/'.env',data/'backups'/'immich-database',True)
        # Second independent physical copy on media HDD, never overwrite.
        import hashlib
        def checksum(path):
            h=hashlib.sha256()
            with path.open('rb') as stream:
                for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
            return h.hexdigest()
        target_dir=safe_path(media/'backups'/'immich-database');target_dir.mkdir(parents=True,exist_ok=True)
        target=target_dir/created.name
        temp=safe_path(target.with_name(target.name+'.part'))
        with created.open('rb') as original, temp.open('xb') as copy:
            os.fchmod(copy.fileno(),0o600)
            for part in iter(lambda:original.read(1024*1024),b''):copy.write(part)
            copy.flush();os.fsync(copy.fileno())
        if checksum(created)!=checksum(temp):
            temp.unlink(missing_ok=True)
            raise RuntimeError('Database secondary copy checksum mismatch')
        publish_no_replace(temp,target)
        print('Database backup verified on SSD and HDD:',target)

def main():
    p=argparse.ArgumentParser();p.add_argument('--data',required=True);p.add_argument('--media',required=True);p.add_argument('--min-age',type=int,default=600);p.add_argument('--db-backup',action='store_true');p.add_argument('--archive-dir',default='photos/archive');p.add_argument('--config',required=True);a=p.parse_args()
    import json
    config=json.loads(safe_path(a.config).read_text())
    if config['data_mount'] != a.data or config['media_mount'] != a.media:
        raise RuntimeError('Backup paths must match verified configuration')
    verify_storage(config)
    verify=lambda:verify_storage(config, ('data_ssd',))
    def operation():
        run(a.data,a.media,a.min_age,a.db_backup,a.archive_dir)
        verify_storage(config)
    with open('/run/nekoserver-photo-preservation.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        execute(Path(a.data),'database' if a.db_backup else 'archive',operation, verify=verify)
if __name__=='__main__':
    try:main()
    except Exception as exc:print('FAILED:',exc,file=sys.stderr);sys.exit(1)
