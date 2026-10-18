#!/usr/bin/env python3
"""Non-destructive import of Immich originals into separate, permanent archive.

Scans Immich 'upload' and 'library' trees only. Never modifies Immich files.
Preserves original relative paths to avoid filename collisions. Writes .part
and atomically publishes verified files. Skips recently modified originals.
"""
import argparse, hashlib, os, sys, time
from pathlib import Path
from neko_common import safe_path, publish_no_replace, ensure_directory
PHOTO_EXT={'.jpg','.jpeg','.png','.heic','.heif','.webp','.gif','.tif','.tiff','.bmp','.dng','.cr2','.cr3','.nef','.arw','.raf','.rw2','.orf','.pef','.srw','.avif','.jxl'}
PHOTO_EXT.update({'.raw','.nrw','.k25','.kdc','.3fr','.fff','.mrw','.mos','.x3f','.sr2','.srf','.rwl','.iiq','.erf','.hif','.xmp'})
VIDEO_EXT={'.mov','.mp4','.m4v','.3gp','.3gpp','.avi','.mkv','.webm','.mpg','.mpeg','.mts','.m2ts','.wmv','.flv','.vob','.ts','.insv'} # Live Photo companions and user videos

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def transfer(source,archive,apply=False,min_age=600,now=None):
    source=safe_path(source);archive=safe_path(archive);now=time.time() if now is None else now
    if not source.is_dir() or source.is_symlink():raise RuntimeError('Immich source missing or symlinked')
    if archive.is_symlink() or (archive.exists() and not archive.is_dir()):raise RuntimeError('Invalid archive')
    if source.resolve()==archive.resolve() or source.resolve() in archive.resolve().parents or archive.resolve() in source.resolve().parents:raise RuntimeError('Source and archive must be separate')
    seen=copied=skipped=0
    for section in ('upload','library'):
        root=source/section
        if not root.exists():continue
        if root.is_symlink():raise RuntimeError('Symlinked Immich section')
        for base,dirs,files in os.walk(root,followlinks=False):
            base=Path(base)
            for dirname in dirs:
                if (base/dirname).is_symlink():raise RuntimeError('Symlink in Immich library')
            for filename in sorted(files):
                current=base/filename
                if current.is_symlink():raise RuntimeError('Symlink in Immich library')
                if current.suffix.lower() not in PHOTO_EXT|VIDEO_EXT:continue
                stat=current.stat()
                if now-stat.st_mtime<min_age:skipped+=1;continue
                rel=current.relative_to(source);target=archive/rel;seen+=1
                if target.is_symlink() or any(p.is_symlink() for p in target.parents if p!=archive.parent and p!=Path('/')):raise RuntimeError('Symlink archive destination')
                if target.exists():
                    if not target.is_file():raise RuntimeError(f'Non-file archive collision: {rel}')
                    if digest(target)!=digest(current):raise RuntimeError(f'Changed original/collision; no overwrite: {rel}')
                    continue
                if not apply:continue
                ensure_directory(target.parent, 0o755)
                # Create temp file exclusively, ensure verified bytes and stable source.
                temp=safe_path(target.with_name(target.name+'.neko-part'))
                if temp.exists():
                    if temp.is_symlink() or not temp.is_file():raise RuntimeError(f'Unsafe partial archive: {temp}')
                    temp.unlink()
                with current.open('rb') as reader, temp.open('xb') as writer:
                    for chunk in iter(lambda:reader.read(1024*1024),b''):writer.write(chunk)
                    os.fchmod(writer.fileno(), 0o644)
                    writer.flush();os.fsync(writer.fileno())
                if current.stat().st_size!=stat.st_size or current.stat().st_mtime_ns!=stat.st_mtime_ns or digest(temp)!=digest(current):
                    temp.unlink(missing_ok=True);raise RuntimeError(f'Source changed during copy: {rel}')
                # Atomic publish without overwrite (hardlink then unlink temp on same filesystem).
                try:publish_no_replace(temp,target)
                finally:temp.unlink(missing_ok=True)
                copied+=1
    print(f'Immich archive: {seen} mature originals, {copied} new verified, {skipped} recently modified skipped')
    return copied

def main():
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--archive',required=True);p.add_argument('--min-age',type=int,default=600);p.add_argument('--apply',action='store_true');a=p.parse_args()
    if a.min_age<60:raise RuntimeError('min-age must be at least 60 seconds')
    transfer(a.source,a.archive,a.apply,a.min_age)
if __name__=='__main__':
    try:main()
    except Exception as exc:print(f'FAILED: {exc}',file=sys.stderr);sys.exit(1)
