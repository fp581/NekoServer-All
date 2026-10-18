#!/usr/bin/env python3
"""Add-only verified copy of archive photos to mounted external disk. No deletion, no overwrites."""
import argparse, hashlib, os, subprocess, sys
from pathlib import Path
from neko_common import ensure_directory, safe_path, publish_no_replace

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def validate_mount(path,uuid):
    path=Path(path)
    if not path.is_dir() or path.is_symlink(): raise RuntimeError('Destination mount path invalid')
    actual=subprocess.run(['findmnt','-n','-o','UUID','--mountpoint',str(path)],capture_output=True,text=True)
    if actual.returncode or actual.stdout.strip().lower()!=uuid.lower(): raise RuntimeError('Wrong/unmounted external drive UUID; refusing backup')

def backup(source,dest,apply=False):
    source=safe_path(source).resolve(); dest=safe_path(dest).resolve()
    if not source.is_dir() or source.is_symlink() or dest==source or source in dest.parents or dest in source.parents: raise RuntimeError('Unsafe source/destination')
    n=0
    for path in sorted(source.rglob('*')):
        if path.is_symlink(): raise RuntimeError(f'Refusing source symlink: {path}')
        if not path.is_file() or path.name.endswith(('.neko-part','.sql.part')): continue
        rel=path.relative_to(source); target=safe_path(dest/rel)
        if target.is_symlink(): raise RuntimeError(f'Refusing target symlink: {target}')
        if target.exists():
            if not target.is_file() or digest(target)!=digest(path):
                raise RuntimeError(f'Name collision/content changed; never overwrite: {rel}')
            continue
        print(('COPY ' if apply else 'WOULD COPY ')+str(rel))
        if apply:
            ensure_directory(target.parent, 0o755)
            tmp=safe_path(target.with_name(target.name+'.neko-part'))
            if tmp.exists():
                if tmp.is_symlink() or not tmp.is_file():raise RuntimeError(f'Unsafe partial file: {tmp}')
                tmp.unlink()  # interrupted backup artifact only; never touch published files
            try:
                with path.open('rb') as src, tmp.open('xb') as out:
                    for block in iter(lambda:src.read(1024*1024),b''):out.write(block)
                    os.fchmod(out.fileno(), 0o644)
                    out.flush();os.fsync(out.fileno())
                if digest(tmp)!=digest(path):raise RuntimeError(f'Verification failed: {rel}')
                publish_no_replace(tmp,target)
            finally:
                tmp.unlink(missing_ok=True)
        n+=1
    print(f'{n} new photos/files '+('copied and verified' if apply else 'identified'))
    return n

def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--source',required=True); p.add_argument('--dest',required=True); p.add_argument('--uuid',required=True); p.add_argument('--apply',action='store_true'); a=p.parse_args(argv)
    if not a.uuid.strip(): p.error('external drive UUID required')
    if a.apply: validate_mount(a.dest,a.uuid)
    backup(a.source,a.dest,a.apply)
if __name__=='__main__':
    try: main()
    except Exception as e: print('FAILED:',e,file=sys.stderr); sys.exit(1)
