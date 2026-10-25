#!/usr/bin/env python3
"""Read-only by default. Explicit recovery changes never format or replace stores."""
import argparse,json,sys,subprocess
from pathlib import Path
import recovery_tools as recovery
from neko_common import checked_settings, default_settings

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=checked_settings,default=default_settings(Path(__file__).parent))
    sub=p.add_subparsers(dest='action',required=True)
    for name in ('check','checkpoint','reattach','snapshot','restore-test','resume','install-docker'):
        cmd=sub.add_parser(name)
        if name not in ('check','resume'):cmd.add_argument('--apply',action='store_true')
        if name in ('check','reattach'):cmd.add_argument('--bundle',type=Path,default=Path(__file__).parent)
        if name in ('reattach','restore-test'):cmd.add_argument('--confirm',default='')
        if name=='snapshot':cmd.add_argument('--destination',required=True,type=Path)
        if name=='restore-test':
            cmd.add_argument('--snapshot',required=True,type=Path);cmd.add_argument('--workspace',required=True,type=Path);cmd.add_argument('--port',type=int,default=2284)
    a=p.parse_args(argv)
    try:
        config=json.loads(Path(a.config).read_text())
        result=run(a,config)
        print(json.dumps(result,indent=2));return 1 if result.get('status')=='needs_setup' else 0
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError,KeyboardInterrupt) as exc:
        # Exception details can contain paths or API data; never echo secrets or SQL.
        print('Recovery failed:',str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__,file=sys.stderr);return 2

def check(config,bundle):
    """The drives and the kept installer, and whether its program runs on this Linux
    (Docker's start guard is that program: reattach needs it to run)."""
    recovery.mounts(config);version=recovery.verify_bundle(bundle,config)
    problem=recovery.runtime_problem(bundle)
    if problem:return {'status':'needs_setup','reason':problem,'bundle_version':version}
    return {'status':'pass','reason':'Storage identities and preserved installer verified; this is not restore acceptance','bundle_version':version}

def run(a,config):
    if a.action=='check':return check(config,a.bundle)
    if a.action=='checkpoint':return recovery.checkpoint(config,a.apply)
    if a.action=='reattach':return recovery.reattach(config,a.bundle,a.apply,a.confirm)
    if a.action=='snapshot':return recovery.snapshot(config,a.destination,a.apply,a.config)
    if a.action=='resume':return recovery.resume(config)
    if a.action=='install-docker':return recovery.install_docker(config,a.apply)
    return recovery.restore_test(config,a.snapshot,a.workspace,a.port,a.apply,a.confirm)

if __name__=='__main__':raise SystemExit(main())
