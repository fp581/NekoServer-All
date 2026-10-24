#!/usr/bin/env python3
"""Build the locked Linux runtime and write its record (build.json).

Static (musl, linked by rust-lld) when the toolchain has the musl standard library and
rust-lld, as the release builds are; otherwise for this PC alone, with its own C
library, which is what `sudo bash install.sh` builds on the PC that installs NekoServer
(git keeps the sources, not the binary). As an ordinary user, or as root from
install.sh (NEKOSERVER_BUILD_AS_ROOT=1)."""
import hashlib,json,os,platform,re,shutil,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parent
STATIC='x86_64-unknown-linux-musl'
def oldest_rust():
    """(major, minor) of the oldest Rust that builds the runtime (Cargo.toml's rust-version)."""
    match=re.search(r'^rust-version\s*=\s*"(\d+)\.(\d+)',(ROOT/'Cargo.toml').read_text(),re.MULTILINE)
    return (int(match[1]),int(match[2])) if match else (0,0)
def rust_version(rustc):
    match=re.search(r'rustc (\d+)\.(\d+)',subprocess.run([rustc,'--version'],capture_output=True,text=True,check=False).stdout or '')
    return (int(match[1]),int(match[2])) if match else (0,0)
def pairs(which=shutil.which,listing=None):
    """Every (cargo, rustc) on PATH: the plain pair, then the versioned ones Ubuntu
    installs beside an older plain Rust (cargo-1.85 with rustc-1.85), newest first."""
    found=[]
    if which('cargo') and which('rustc'):found.append((which('cargo'),which('rustc')))
    names=listing if listing is not None else [p.name for d in os.environ.get('PATH','').split(os.pathsep) for p in Path(d or '.').glob('cargo-1.*') if d]
    versions=sorted({tuple(int(n) for n in name[len('cargo-'):].split('.')[:2]) for name in names if re.fullmatch(r'cargo-1\.\d+(\.\d+)?',name)},reverse=True)
    for major,minor in versions:
        cargo,rustc=which(f'cargo-{major}.{minor}'),which(f'rustc-{major}.{minor}')
        if cargo and rustc:found.append((cargo,rustc))
    return found
def toolchain():
    """(cargo, rustc, host triple, sysroot, rustc line) of a Rust on PATH that is new
    enough; cargo is told which rustc goes with it (RUSTC)."""
    oldest=oldest_rust()
    for cargo,rustc in pairs():
        if rust_version(rustc)>=oldest:
            os.environ['RUSTC']=rustc
            details=subprocess.check_output([rustc,'-vV'],text=True);host=next(line.split(': ',1)[1] for line in details.splitlines() if line.startswith('host: '))
            sysroot=Path(subprocess.check_output([rustc,'--print','sysroot'],text=True).strip())
            return cargo,rustc,host,sysroot,details.splitlines()[0]
    raise RuntimeError('Rust %d.%d or newer (cargo and rustc) is needed; sudo bash install.sh installs it on the server'%oldest)
def plan(host,sysroot):
    """(target, cargo arguments): static when the toolchain can, else this PC's own."""
    linker=sysroot/'lib/rustlib'/host/'bin/rust-lld'
    if (sysroot/'lib/rustlib'/STATIC/'lib').is_dir() and linker.is_file():
        return STATIC,['--target',STATIC,'--config',f'target.{STATIC}.linker='+json.dumps(str(linker))]
    return host,[]
def main():
    if os.geteuid()==0 and os.environ.get('NEKOSERVER_BUILD_AS_ROOT')!='1':raise RuntimeError('Build as an ordinary user (or let sudo bash install.sh build it)')
    if platform.system()!='Linux' or platform.machine()!='x86_64':raise RuntimeError('This release build targets x86_64 Linux')
    cargo,rustc,host,sysroot,rustc_line=toolchain()
    target,arguments=plan(host,sysroot)
    # The runtime itself is always compiled again: cargo trusts file times, and after the
    # clock went back an older build looked newer than the sources, so the binary came out
    # unchanged while the manifest named the new sources. Dependencies stay cached.
    subprocess.run([cargo,'clean','--release',*(['--target',target] if arguments else []),'-p','nekoserver-runtime'],cwd=ROOT,check=True)
    subprocess.run([cargo,'build','--release','--locked',*arguments],cwd=ROOT,check=True)
    built=ROOT/'target'/(target if arguments else '')/'release/nekoserver-runtime'
    blob=built.read_bytes();out=ROOT/'bin/nekoserver-runtime';out.parent.mkdir(exist_ok=True);out.write_bytes(blob);out.chmod(0o750)
    sources=[ROOT/'Cargo.toml',ROOT/'Cargo.lock',ROOT/'build.py',*sorted((ROOT/'src').glob('*.rs'))]
    version=subprocess.check_output([out,'--version'],text=True).strip().split()[-1]
    metadata={'version':version}
    metadata.update(target=target,static=target==STATIC,rustc=rustc_line,binary_sha256=hashlib.sha256(blob).hexdigest(),source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    (ROOT/'build.json').write_text(json.dumps(metadata,indent=2)+'\n');print(('Static' if target==STATIC else 'This PC\'s')+' runtime built and its record written')
if __name__=='__main__':main()
