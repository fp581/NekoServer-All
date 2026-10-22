#!/usr/bin/env python3
"""Build the locked static Linux runtime as an ordinary user; refresh its manifest."""
import hashlib,json,os,platform,shutil,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parent
TARGET='x86_64-unknown-linux-musl'
def main():
    if os.geteuid()==0:raise RuntimeError('Build as an ordinary user, then install using the Python installer')
    if platform.system()!='Linux' or platform.machine()!='x86_64':raise RuntimeError('This release build targets x86_64 Linux')
    cargo=shutil.which('cargo');rustc=shutil.which('rustc')
    if not cargo or not rustc:raise RuntimeError('Install an ordinary-user Rust toolchain and add the musl target first')
    details=subprocess.check_output([rustc,'-vV'],text=True);host=next(line.split(': ',1)[1] for line in details.splitlines() if line.startswith('host: '));sysroot=Path(subprocess.check_output([rustc,'--print','sysroot'],text=True).strip());linker=sysroot/'lib/rustlib'/host/'bin/rust-lld'
    subprocess.run([cargo,'build','--release','--locked','--target',TARGET,'--config',f'target.{TARGET}.linker='+json.dumps(str(linker))],cwd=ROOT,check=True)
    blob=(ROOT/'target'/TARGET/'release/nekoserver-runtime').read_bytes();target=ROOT/'bin/nekoserver-runtime';target.parent.mkdir(exist_ok=True);target.write_bytes(blob);target.chmod(0o750)
    sources=[ROOT/'Cargo.toml',ROOT/'Cargo.lock',ROOT/'build.py',*sorted((ROOT/'src').glob('*.rs'))]
    version=subprocess.check_output([target,'--version'],text=True).strip().split()[-1]
    metadata={'version':version}
    metadata.update(target=TARGET,rustc=details.splitlines()[0],binary_sha256=hashlib.sha256(blob).hexdigest(),source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    (ROOT/'build.json').write_text(json.dumps(metadata,indent=2)+'\n');print('Static runtime built and checksums refreshed')
if __name__=='__main__':main()
