#!/usr/bin/env python3
"""Read-only by default. Explicit recovery changes never format or replace stores."""
import argparse,json,sys,subprocess
from pathlib import Path
import recovery_tools as recovery

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',default=str(Path(__file__).with_name('config.json')))
    sub=p.add_subparsers(dest='action',required=True)
    for name in ('check','checkpoint','reattach','snapshot','restore-test','resume'):
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
        if a.action=='check':
            recovery.mounts(config);version=recovery.verify_bundle(a.bundle,config);result={'status':'pass','reason':'Storage identities and preserved installer verified; this is not restore acceptance','bundle_version':version}
        elif a.action=='checkpoint':result=recovery.checkpoint(config,a.apply)
        elif a.action=='reattach':result=recovery.reattach(config,a.bundle,a.apply,a.confirm)
        elif a.action=='snapshot':result=recovery.snapshot(config,a.destination,a.apply,a.config)
        elif a.action=='resume':result=recovery.resume(config)
        else:result=recovery.restore_test(config,a.snapshot,a.workspace,a.port,a.apply,a.confirm)
        print(json.dumps(result,indent=2));return 1 if result.get('status')=='needs_setup' else 0
    except (OSError,ValueError,KeyError,TypeError,RuntimeError,subprocess.SubprocessError,KeyboardInterrupt) as exc:
        # Exception details can contain paths or API data; never echo secrets or SQL.
        print('Recovery failed:',str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__,file=sys.stderr);return 2
if __name__=='__main__':raise SystemExit(main())
