#!/usr/bin/env python3
"""Verified, add-only media copies from local folders or existing network mounts."""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path
from neko_common import UnreadableEntries, findmnt, partial_name, safe_path, atomic_json, publish_no_replace, ensure_directory, strict_files, verify_storage


def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


class SourceGone(RuntimeError):
    """The source folder is not there (a drive unplugged after its import, say)."""


def source_key(source):
    """A source's identity in the record of finished imports (never its path itself)."""
    where=source.get('mounted_path') if source.get('type')=='remote' else source.get('path')
    return hashlib.sha256((str(source.get('category'))+'\0'+str(where)).encode()).hexdigest()[:16]


def source_folder(source):
    if source.get('type')=='indexer':return None
    if source.get('type')=='remote':
        value=source.get('mounted_path')
        if not value:raise RuntimeError('Remote source needs an already mounted local folder')
        path=safe_path(value)
        if findmnt('FSTYPE',path,'--target') not in ('cifs','nfs','nfs4'):
            raise RuntimeError('Remote folder is not on a verified SMB/NFS mount')
    elif source.get('type')=='local':path=safe_path(source['path'])
    else:raise RuntimeError('Unknown source type')
    if not path.is_absolute() or not path.is_dir():raise SourceGone('Source folder is not accessible')
    return path


# Files still being written by another program; never imported.
TEMPORARY=('.neko-part','.part','.tmp','.crdownload','.!qB')


class DriveGone(Exception):
    """The destination drive is no longer the mounted drive it was: the whole import stops
    (not just this source), before anything more is written."""


# The kernel's table of what is mounted where (a fixed file, read fresh each time).
MOUNT_TABLE=Path('/proc/self/mountinfo')


def mount_id(root):
    """The kernel's ID of the mount at `root`, or None when nothing is mounted there. A
    new mount (the drive plugged in again, or another one) gets a new ID."""
    found=None
    for line in MOUNT_TABLE.read_text(encoding='utf-8',errors='replace').splitlines():
        fields=line.split()
        # Spaces and other odd characters appear as octal escapes (\040 for a space).
        point=re.sub(r'\\([0-7]{3})',lambda m:chr(int(m[1],8)),fields[4]) if len(fields)>4 else None
        if point==str(root):found=fields[0]   # the last one mounted there is the one in use
    return found


class DriveGuard:
    """The destination's own drive, remembered while it is mounted. Checked before each
    file and again before it is published: if the drive was unmounted, a copy would land
    in the empty folder underneath, on the system disk, and could fill it."""
    def __init__(self,root):
        self.root=Path(root)
        self.mount=mount_id(self.root)
        if self.mount is None:raise DriveGone('Destination drive is not mounted')
        self.device=None   # the drive's own device number, from the first folder checked

    def check(self,path):
        path=Path(path)
        while not path.exists() and path!=self.root and self.root in path.parents:
            path=path.parent   # a folder still to be made: check where it will be made
        try:
            device=os.stat(path).st_dev
        except OSError:
            device=None
        if self.device is None:self.device=device
        if mount_id(self.root)!=self.mount or device is None or device!=self.device:
            raise DriveGone('The destination drive is no longer mounted; the import stopped before writing more')


class IncompleteSource(RuntimeError):
    """Parts of a source could not be read; `paths` are relative to the source folder."""
    def __init__(self,paths):
        self.paths=paths
        super().__init__('Could not read: '+', '.join(paths[:5]))


def import_file(current,dest,stat,uid,gid,guard=None):
    """Copy one file next to its destination, check it, then publish it (never over an
    existing file). The destination drive is checked before writing and before publishing."""
    if guard:guard.check(dest.parent)
    ensure_directory(dest.parent, 0o2775, uid, gid)
    if guard:guard.check(dest.parent)
    # A short name of its own: a 255-byte name plus a suffix would be too long.
    temp=safe_path(dest.with_name(partial_name(dest.name)))
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
        # The original's date, so a later run recognises the copy without reading both.
        os.utime(temp,ns=(stat.st_atime_ns,stat.st_mtime_ns))
        if guard:guard.check(temp)
        publish_no_replace(temp,dest)
    finally:temp.unlink(missing_ok=True)


def legacy_target(folder,media,category):
    """Where releases before 0.39.5 put this source: a folder of its own inside the
    library. Jellyfin read that folder as one TV show (with the real shows as its
    seasons), and the Arr apps as one movie, artist or show."""
    identity=hashlib.sha256((category+'\0'+str(folder.resolve())).encode()).hexdigest()[:16]
    return safe_path(Path(media)/'library'/category/('source-'+identity))


def import_target(folder,media,category):
    """The library folder itself (library/tv, say), checked not to overlap the source:
    each show, movie or artist folder of the source lands where Jellyfin, Navidrome and
    the Arr apps look for one. A file already there that differs is never replaced."""
    target=safe_path(Path(media)/'library'/category)
    managed=target.resolve()
    here=folder.resolve()
    if here==managed or managed in here.parents or here in managed.parents:
        raise RuntimeError('Source and destination overlap')
    return target


def move_legacy(folder,media,category,guard=None):
    """Move what an earlier release imported into this source's own folder up into the
    library, folder by folder (same drive: a rename, nothing copied). Names already in
    the library stay where they are; they are returned so the report can name them."""
    legacy=legacy_target(folder,media,category)
    if not legacy.is_dir():return []
    if guard:guard.check(legacy)
    left=[]
    for item in sorted(legacy.iterdir()):
        place=legacy.parent/item.name
        if item.is_symlink() or place.exists() or place.is_symlink():
            left.append(item.name);continue
        os.rename(item,place)
    if not left:legacy.rmdir()
    return left


def already_copied(current,dest):
    """`dest` is the copy of `current` an earlier run made, or this raises: a file there
    that differs is never replaced. Same size and date (copies keep their original's
    date): not read again. Otherwise (a copy from before 0.39.5) the contents decide."""
    if not dest.is_file():raise RuntimeError('Existing imported file differs; refusing overwrite')
    have,want=dest.stat(),current.stat()
    if (have.st_size,have.st_mtime_ns)==(want.st_size,want.st_mtime_ns):return
    if digest(current)!=digest(dest):raise RuntimeError('Existing imported file differs; refusing overwrite')


def planned_copies(folder,target):
    """[(file, where it goes, its stat)] for every file not imported yet. Strict listing:
    a folder that cannot be read makes this source incomplete (nothing is copied),
    instead of silently leaving an album out."""
    try:
        found=strict_files(folder,lambda here,name:name.endswith(TEMPORARY))
    except UnreadableEntries as exc:
        raise IncompleteSource([Path(p).relative_to(folder).as_posix() if Path(p).is_relative_to(folder) else '.'
                                for p,_ in exc.problems]) from None
    entries=[]
    for found_path,_ in sorted(found):
        current=safe_path(found_path)
        dest=safe_path(target/current.relative_to(folder))
        if dest.exists():
            already_copied(current,dest)
            continue
        entries.append((current,dest,current.stat()))
    return entries


def copy_source(source,media,apply=False,uid=None,gid=None,guard=None):
    category=source.get('category')
    if category not in ('movies','tv','music'):raise RuntimeError('Invalid media category')
    folder=source_folder(source)
    if folder is None:return {'status':'reference_only','copied':0,'reason':'Configure this indexer through Prowlarr'}
    target=import_target(folder,media,category)
    left=move_legacy(folder,media,category,guard) if apply else []
    if left:
        return {'status':'needs_setup','copied':0,'reason':'An earlier import of this source is in '+legacy_target(folder,media,category).name+
                ' in the library; these folders could not be moved up because the library already has a folder of that name: '
                +', '.join(left[:10])+'. Compare and merge them by hand, then run setup again'}
    entries=planned_copies(folder,target)
    required=sum(stat.st_size for _,_,stat in entries)
    if required>shutil.disk_usage(media).free:raise RuntimeError('Not enough free space on target library filesystem')
    for current,dest,stat in entries:
        if apply:import_file(current,dest,stat,uid,gid,guard)
    return {'status':'copied' if apply else 'preview','copied':len(entries) if apply else 0,'new_files':len(entries),'bytes':required,'destination':str(target)}


def finished_imports(record_path):
    """{source key: category} of the sources whose import finished (empty when none or
    the record can't be read)."""
    try:
        finished=json.loads(record_path.read_text()).get('finished',{})
    except (OSError,ValueError,AttributeError):
        return {}
    return finished if isinstance(finished,dict) else {}


def save_finished(record_path,finished):
    record_path.parent.mkdir(parents=True,exist_ok=True)
    atomic_json(record_path,{'format':'neko-imported-sources-v1','finished':finished})


def gone(source,finished):
    """A source folder that isn't there: unplugged after its import finished is nothing
    left to do; never imported is a step left."""
    if source_key(source) in finished:
        return {'status':'imported_earlier','copied':0,'reason':'Source not connected now; its files were copied before'}
    return {'status':'needs_setup','reason':'Source folder not found: connect the drive, or take it out of the source list'}


def import_one(source,root,apply,owner,guard,finished):
    """One source copied into `root`'s library: its row for the report. A finished
    import is recorded in `finished`. DriveGone stops the whole import (it is raised)."""
    try:
        result=copy_source(source,root,apply,*owner,guard)
    except SourceGone:
        return gone(source,finished)
    except IncompleteSource as exc:
        # Only paths inside the source are reported, never the source's own location.
        return {'status':'needs_setup','reason':f'Nothing was copied: {len(exc.paths)} folders or files could not be read: '+', '.join(exc.paths[:10])}
    except (RuntimeError,OSError,KeyError,ValueError):
        return {'status':'needs_setup','reason':'Source inaccessible, unmounted, overlapping, changed, unsafe or conflicting; inspect source before retry'}
    if apply and result['status']=='copied':finished[source_key(source)]=source.get('category')
    return result


def run(config,sources,apply=False,verify=None):
    media=safe_path(config['media_mount']);data=safe_path(config['data_mount'])
    if not os.path.ismount(media) or not os.path.ismount(data):raise RuntimeError('Mounted data SSD and media HDD required')
    uid,gid=int(config['media'].get('uid',1000)),int(config['media'].get('gid',1000))
    if apply and (uid<=0 or gid<=0):raise RuntimeError('Positive media UID/GID required')
    if apply and os.geteuid()!=0:raise RuntimeError('Use sudo to create media owned by the configured UID/GID')
    rows=[]
    record_path=data/'config'/'imported-sources.json'
    finished=finished_imports(record_path)
    guards={data:DriveGuard(data),media:DriveGuard(media)} if apply else {}
    owner=(uid,gid) if apply else (None,None)
    for index,source in enumerate(sources):
        root=data if source.get('category')=='music' else media
        result=import_one(source,root,apply,owner,guards.get(root),finished)
        # Inventory may contain private URLs; reports never serialize source entries.
        rows.append(dict(source_number=index+1,category=source.get('category'),**result))
    if verify: verify()
    if apply:save_finished(record_path,finished)
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
    except DriveGone as exc:
        print('Source import stopped: '+str(exc)+'. Mount the drive again (or restart the computer), then run it again.',file=sys.stderr);return 2
    except (OSError,RuntimeError,ValueError,KeyError):
        print('Source import unavailable; check configured mounts and source inventory',file=sys.stderr);return 2


if __name__=='__main__':sys.exit(main())
