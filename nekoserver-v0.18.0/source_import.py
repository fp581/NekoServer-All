#!/usr/bin/env python3
"""Verified, add-only media copies from local folders or existing network mounts."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from neko_common import safe_path, atomic_json, publish_no_replace, ensure_directory, verify_storage


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def source_folder(source):
    if source.get('type')=='indexer':return None
    if source.get('type')=='remote':
        value=source.get('mounted_path')
        if not value:raise RuntimeError('Remote source needs an already mounted local folder')
        path=safe_path(value)
        result=subprocess.run(['findmnt','-n','-o','FSTYPE','--target',str(path)],capture_output=True,text=True,timeout=10)
        if result.returncode or result.stdout.strip() not in ('cifs','nfs','nfs4'):
            raise RuntimeError('Remote folder is not on a verified SMB/NFS mount')
    elif source.get('type')=='local':path=safe_path(source['path'])
    else:raise RuntimeError('Unknown source type')
    if not path.is_absolute() or not path.is_dir():raise RuntimeError('Source folder is not accessible')
    return path


def copy_source(source,media,apply=False,uid=None,gid=None):
    category=source.get('category')
    if category not in ('movies','tv','music'):raise RuntimeError('Invalid media category')
    folder=source_folder(source)
    if folder is None:return {'status':'reference_only','copied':0,'reason':'Configure this indexer through Prowlarr'}
    identity=hashlib.sha256((category+'\0'+str(folder.resolve())).encode()).hexdigest()[:16]
    target=safe_path(Path(media)/'library'/category/('source-'+identity))
    if folder.resolve()==target.resolve() or folder.resolve() in target.resolve().parents or target.resolve() in folder.resolve().parents:
        raise RuntimeError('Source and destination overlap')
    # No source may be inside this managed category, even under a different import namespace.
    managed=(Path(media)/'library'/category).resolve()
    if folder.resolve()==managed or managed in folder.resolve().parents:raise RuntimeError('Source already belongs to this managed library')
    entries=[];required=0
    for base,dirs,files in os.walk(folder,followlinks=False):
        parent=Path(base)
        if any((parent/d).is_symlink() for d in dirs):raise RuntimeError('Symlink directory in source')
        for name in sorted(files):
            current=safe_path(parent/name)
            if not current.is_file():raise RuntimeError('Non-regular source file')
            if name.endswith(('.neko-part','.part','.tmp')):continue
            dest=safe_path(target/current.relative_to(folder))
            if dest.exists():
                if not dest.is_file() or digest(current)!=digest(dest):raise RuntimeError('Existing imported file differs; refusing overwrite')
                continue
            entries.append((current,dest,current.stat()));required+=current.stat().st_size
    if required>shutil.disk_usage(media).free:raise RuntimeError('Not enough free space on target library filesystem')
    for current,dest,stat in entries:
        if not apply:continue
        ensure_directory(dest.parent, 0o2775, uid, gid)
        temp=safe_path(dest.with_name(dest.name+'.neko-part'))
        if temp.exists():
            if not temp.is_file():raise RuntimeError('Unsafe partial import')
            temp.unlink()
        try:
            with current.open('rb') as reader,temp.open('xb') as writer:
                for chunk in iter(lambda:reader.read(1024*1024),b''):writer.write(chunk)
                os.fchmod(writer.fileno(), 0o664)
                writer.flush();os.fsync(writer.fileno())
                if uid is not None:os.fchown(writer.fileno(),uid,gid)
            after=current.stat()
            if after.st_size!=stat.st_size or after.st_mtime_ns!=stat.st_mtime_ns or digest(temp)!=digest(current):
                raise RuntimeError('Source changed during import')
            publish_no_replace(temp,dest)
        finally:temp.unlink(missing_ok=True)
    return {'status':'copied' if apply else 'preview','copied':len(entries) if apply else 0,'new_files':len(entries),'bytes':required,'destination':str(target)}


def run(config,sources,apply=False,verify=None):
    media=safe_path(config['media_mount']);data=safe_path(config['data_mount'])
    if not os.path.ismount(media) or not os.path.ismount(data):raise RuntimeError('Mounted data SSD and media HDD required')
    uid,gid=int(config['media'].get('uid',1000)),int(config['media'].get('gid',1000))
    if apply and (uid<=0 or gid<=0):raise RuntimeError('Positive media UID/GID required')
    if apply and os.geteuid()!=0:raise RuntimeError('Use sudo to create media owned by the configured UID/GID')
    rows=[]
    for index,source in enumerate(sources):
        try:
            result=copy_source(source,data if source.get('category')=='music' else media,apply,uid if apply else None,gid if apply else None)
        except (RuntimeError,OSError,KeyError,ValueError):
            result={'status':'needs_setup','reason':'Source inaccessible, unmounted, overlapping, changed, unsafe or conflicting; inspect source before retry'}
        # Inventory may contain private URLs; reports never serialize source entries.
        rows.append(dict(source_number=index+1,category=source.get('category'),**result))
    if verify: verify()
    atomic_json(data/'reports'/'source-import.json',{'apply':apply,'sources':rows})
    return 1 if any(r['status']=='needs_setup' for r in rows) else 0


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(Path(__file__).with_name('config.json')))
    parser.add_argument('--sources')
    parser.add_argument('--data-root')
    parser.add_argument('--media-root')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--selected-only',action='store_true',help='Copy only inventory entries explicitly selected in the source wizard')
    args=parser.parse_args(argv)
    try:
        config=json.loads(safe_path(args.config).read_text())
        if args.data_root:config['data_mount']=args.data_root
        if args.media_root:config['media_mount']=args.media_root
        inventory=safe_path(args.sources or str(Path(config['data_mount'])/'config'/'media-sources.json'))
        sources=json.loads(inventory.read_text()).get('sources',[])
        if args.selected_only:sources=[s for s in sources if s.get('copy_to_library') is True]
        if args.apply: verify_storage(config)
        result=run(config,sources,args.apply,verify=(lambda:verify_storage(config)) if args.apply else None)
        print('Source import:', 'incomplete; check source-import report' if result else 'complete' if args.apply else 'preview complete')
        return result
    except (OSError,RuntimeError,ValueError,KeyError):
        print('Source import unavailable; check configured mounts and source inventory',file=sys.stderr);return 2


if __name__=='__main__':sys.exit(main())
