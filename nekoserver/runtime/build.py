#!/usr/bin/env python3
"""Build the locked Linux runtime and write its record (build.json).

Static (musl, linked by rust-lld) when the toolchain has the musl standard library and
rust-lld, as the release builds are; otherwise for this PC alone, with its own C
library, which is what `sudo bash install.sh` builds on the PC that installs NekoServer
(git keeps the sources, not the binary). As an ordinary user, or as root from
install.sh (NEKOSERVER_BUILD_AS_ROOT=1)."""
import hashlib,json,os,platform,re,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
STATIC='x86_64-unknown-linux-musl'
def oldest_rust():
    """(major, minor) of the oldest Rust that builds the runtime (Cargo.toml's rust-version)."""
    match=re.search(r'^rust-version\s*=\s*"(\d+)\.(\d+)',(ROOT/'Cargo.toml').read_text(),re.MULTILINE)
    return (int(match[1]),int(match[2])) if match else (0,0)
def rust_version(rustc):
    match=re.search(r'rustc (\d+)\.(\d+)',subprocess.run([rustc,'--version'],capture_output=True,text=True,timeout=60,check=False).stdout or '')
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
            details=subprocess.check_output([rustc,'-vV'],text=True,timeout=60);host=next(line.split(': ',1)[1] for line in details.splitlines() if line.startswith('host: '))
            sysroot=Path(subprocess.check_output([rustc,'--print','sysroot'],text=True,timeout=60).strip())
            return cargo,rustc,host,sysroot,details.splitlines()[0]
    raise RuntimeError('Rust %d.%d or newer (cargo and rustc) is needed; sudo bash install.sh installs it on the server'%oldest)
def plan(host,sysroot):
    """(target, cargo arguments): static when the toolchain can, else this PC's own."""
    linker=sysroot/'lib/rustlib'/host/'bin/rust-lld'
    if (sysroot/'lib/rustlib'/STATIC/'lib').is_dir() and linker.is_file():
        return STATIC,['--target',STATIC,'--config',f'target.{STATIC}.linker='+json.dumps(str(linker))]
    return host,[]
NEEDED_MB=1024   # free space the build wants here: the build folder takes ~150 MB, Cargo's cache ~30 MB, with room for the rest
def free_megabytes(path=ROOT,statvfs=os.statvfs):
    """Free space (MB) on the filesystem holding path, for the account building."""
    s=statvfs(path);return s.f_bavail*s.f_frsize//(1024*1024)
def built_path(target,arguments,environ=os.environ):
    """Where cargo put the executable: under CARGO_TARGET_DIR when that is set (a shared
    build folder), else runtime/target; in the target's own folder for a cross build."""
    base=Path(environ.get('CARGO_TARGET_DIR') or ROOT/'target')
    return base/(target if arguments else '')/'release/nekoserver-runtime'
FETCH_TRIES=3;FETCH_PAUSE=20   # crates.io can be slow or away for a moment: its downloads are tried this often
def fetch(cargo,target_arguments,run=None,sleep=time.sleep,say=print):
    """Download the locked dependencies, tried again a little later when that fails; the
    build after it then needs no network (--offline), so only this part is retried."""
    for attempt in range(1,FETCH_TRIES+1):
        if (run or subprocess.run)([cargo,'fetch','--locked',*target_arguments],cwd=ROOT,check=False).returncode==0:return
        if attempt<FETCH_TRIES:say(f'Downloading the runtime\'s dependencies failed; trying again in {FETCH_PAUSE} seconds ({attempt+1} of {FETCH_TRIES})...');sleep(FETCH_PAUSE)
    raise RuntimeError(f'Downloading the runtime\'s dependencies failed {FETCH_TRIES} times (the lines above say why). Check that this PC is connected to the internet, then run  sudo bash install.sh  again.')
def cargo_step(argv,what):
    """One cargo step; a failure is said in one line (cargo has printed why just above)."""
    if subprocess.run(argv,cwd=ROOT,check=False).returncode:raise RuntimeError(f'{what} failed (the lines above say why). Fix that, then run  sudo bash install.sh  again.')
def replace_file(path,data,mode):
    """path gets data whole or not at all (written beside it, then renamed over it): a
    build cut off half-way never leaves half a binary, or a record of another one."""
    temporary=path.with_name('.'+path.name+'.new');temporary.write_bytes(data);temporary.chmod(mode);os.replace(temporary,path)
def main():
    if os.geteuid()==0 and os.environ.get('NEKOSERVER_BUILD_AS_ROOT')!='1':raise RuntimeError('Build as an ordinary user (or let sudo bash install.sh build it)')
    if platform.system()!='Linux' or platform.machine()!='x86_64':raise RuntimeError('This release build targets x86_64 Linux')
    free=free_megabytes()
    if free<NEEDED_MB:raise RuntimeError(f'Building the runtime needs about {NEEDED_MB} MB free where NekoServer\'s folder is ({ROOT.parent}); {free} MB is free. Make room there and run  sudo bash install.sh  again.')
    cargo,_,host,sysroot,rustc_line=toolchain()
    target,arguments=plan(host,sysroot)
    # The runtime itself is always compiled again: cargo trusts file times, and after the
    # clock went back an older build looked newer than the sources, so the binary came out
    # unchanged while the manifest named the new sources. Dependencies stay cached.
    # The download comes first, with its retries: cleaning one package reads crates.io's
    # index too, so after it everything runs offline and a crates.io that does not answer
    # is reported as such, not as a failed clean.
    target_arguments=['--target',target] if arguments else []
    fetch(cargo,target_arguments)
    cargo_step([cargo,'clean','--release','--offline',*target_arguments,'-p','nekoserver-runtime'],'Cleaning the earlier runtime build')
    cargo_step([cargo,'build','--release','--locked','--offline',*arguments],'Compiling the runtime')
    built=built_path(target,arguments)
    # The binary first, its record (build.json) last: install.sh compares the two, so a
    # build cut off between them is built again.
    blob=built.read_bytes();out=ROOT/'bin/nekoserver-runtime';out.parent.mkdir(exist_ok=True);replace_file(out,blob,0o750)
    sources=[ROOT/'Cargo.toml',ROOT/'Cargo.lock',ROOT/'build.py',*sorted((ROOT/'src').glob('*.rs'))]
    version=subprocess.check_output([out,'--version'],text=True,timeout=60).strip().split()[-1]
    metadata={'version':version}
    metadata.update(target=target,static=target==STATIC,rustc=rustc_line,binary_sha256=hashlib.sha256(blob).hexdigest(),source_sha256={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    replace_file(ROOT/'build.json',(json.dumps(metadata,indent=2)+'\n').encode(),0o644);print(('Static' if target==STATIC else 'This PC\'s')+' runtime built and its record written')
def cli():
    """main() for the command line: a failure in one plain line, never a traceback."""
    try:main()
    except KeyboardInterrupt:print('\nStopped. Run  sudo bash install.sh  again to build the runtime.',file=sys.stderr);return 130
    except (RuntimeError,OSError,subprocess.SubprocessError) as exc:print('ERROR: building the runtime: '+str(exc),file=sys.stderr);return 1
    return 0
if __name__=='__main__':sys.exit(cli())
