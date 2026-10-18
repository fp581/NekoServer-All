"""Deploy versioned background jobs to persistent SSD with atomic copy."""
import os, shutil, hashlib
from pathlib import Path
from neko_common import VERSION, safe_path
FILES=('job_status.py','neko_common.py','photo-preserve.py','immich-db-backup.py','photo-preservation-job.py','photo-backup.py','photo-weekly.py')
def deploy(data, root):
    data=Path(data);root=Path(root);dest=safe_path(data/'installer'/'runtime'/('v'+VERSION))
    if dest.is_symlink():raise RuntimeError('Runtime directory is a symlink')
    dest.mkdir(parents=True,exist_ok=True)
    for name in FILES:
        source=root/name;target=dest/name
        if not source.is_file() or source.is_symlink() or target.is_symlink():raise RuntimeError('Unsafe runtime source/destination: '+name)
        payload=source.read_bytes()
        if target.exists():
            if target.read_bytes()!=payload:raise RuntimeError('Installed script differs, inspect before replacing: '+str(target))
            continue
        tmp=dest/(name+'.tmp')
        if tmp.exists():raise RuntimeError('Interrupted runtime installation detected: '+str(tmp))
        with tmp.open('xb') as f:
            f.write(payload);f.flush();os.fsync(f.fileno())
        os.replace(tmp,target);os.chmod(target,0o750)
    return dest
