#!/usr/bin/env python3
"""Export only the LAN public root certificate without overwriting a destination."""
import argparse,hashlib,json,os,ssl
from pathlib import Path
import recovery_tools
from neko_common import safe_path

def export(config,destination):
    recovery_tools.mounts(config);source=safe_path(Path(config['data_mount'])/'gateway/caddy-state/caddy/pki/authorities/local/root.crt');payload=source.read_bytes()
    if b'PRIVATE KEY' in payload or len(payload)>32000:raise RuntimeError('Invalid public certificate')
    der=ssl.PEM_cert_to_DER_cert(payload.decode());target=safe_path(destination)
    with target.open('xb') as out:os.fchmod(out.fileno(),0o644);out.write(payload);out.flush();os.fsync(out.fileno())
    return hashlib.sha256(der).hexdigest()
def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,default=Path(__file__).with_name('config.json'));p.add_argument('--export',required=True,type=Path);a=p.parse_args();print('Public CA certificate exported. SHA-256 fingerprint: '+export(json.loads(safe_path(a.config).read_text()),a.export))
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Certificate export failed; verify mounts, CA and a new destination. No private key is exported.') from None
