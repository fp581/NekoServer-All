#!/usr/bin/env python3
"""Consistent logical Immich Postgres dump using pg_dump inside database container."""
import argparse, datetime, os, subprocess, sys
from pathlib import Path
from neko_common import safe_path, publish_no_replace

def backup(compose,env,destination,apply=False):
    compose=safe_path(compose);env=safe_path(env);destination=safe_path(destination)
    if not compose.is_file() or not env.is_file():raise RuntimeError('Immich manifest/.env missing')
    if destination.is_symlink():raise RuntimeError('Backup folder is symlinked')
    if not apply:
        print('Would create a timestamped pg_dump via docker compose exec -T database');return None
    destination.mkdir(parents=True,exist_ok=True)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    target=destination/f'immich-{stamp}.sql';temp=target.with_suffix('.sql.part')
    if target.exists() or temp.exists():raise RuntimeError('Backup filename already exists')
    # Official Immich database service is named database. Prefer database container
    # env defaults supplied by the official Compose file.
    cmd=['docker','compose','-p','neko-immich','-f',str(compose),'--env-file',str(env),'exec','-T','database','sh','-ec','exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-acl']
    try:
        with temp.open('xb') as out:
            os.fchmod(out.fileno(),0o600)
            result=subprocess.run(cmd,stdout=out,stderr=subprocess.PIPE,timeout=1800)
            out.flush();os.fsync(out.fileno())
        if result.returncode or temp.stat().st_size<100:
            raise RuntimeError('pg_dump failed or unexpectedly small dump; inspect database locally')
        # Publish only after pg_dump exits successfully and the output is flushed.
        publish_no_replace(temp,target)
        print('Database dump created:',target)
        return target
    except Exception:
        temp.unlink(missing_ok=True)
        raise

def main():
    p=argparse.ArgumentParser();p.add_argument('--compose',required=True);p.add_argument('--env',required=True);p.add_argument('--dest',required=True);p.add_argument('--apply',action='store_true');a=p.parse_args()
    backup(a.compose,a.env,a.dest,a.apply)
if __name__=='__main__':
    try:main()
    except Exception as exc:print('FAILED:',exc,file=sys.stderr);sys.exit(1)
