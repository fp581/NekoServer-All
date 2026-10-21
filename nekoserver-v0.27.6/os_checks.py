"""Read-only acceptance for managed no-sleep, log bounds and HDD boot policy."""
from pathlib import Path

import status_checks as health
from modules import server_os
from neko_common import safe_path


def check(config):
    rows = []
    def row(key, name, good, detail):
        rows.append({'id':key,'name':name,'status':'pass' if good else 'needs_setup','detail':detail})
    for name in server_os.TARGETS:
        code, value = health.run(['systemctl','show',name,'--property=LoadState'])
        row('os:'+name, 'Blocked '+name, code==0 and value.strip()=='LoadState=masked', 'The running systemd manager must report this sleep target masked.')
    for name, expected in server_os.files(config).items():
        good = False
        try:
            good = safe_path(name).read_text() == expected
        except (OSError,RuntimeError):
            pass
        row('os:'+Path(name).parent.name, 'Managed '+Path(name).parent.name, good, 'Managed no-sleep and journal drop-ins must match selected bounds; inspect conflicting administrator drop-ins separately.')
    if server_os.settings(config)[0] in ('keep_spinning','idle'):
        code, output = health.run(['systemctl','show','nekoserver-hdd-policy.service','--property=LoadState,ActiveState,UnitFileState,ExecMainStatus'])
        fields = dict(line.split('=',1) for line in output.splitlines() if '=' in line)
        good = code==0 and fields.get('LoadState')=='loaded' and fields.get('ActiveState')=='active' and fields.get('UnitFileState')=='enabled' and fields.get('ExecMainStatus')=='0'
        row('os:hdd_policy','HDD standby command',good,'Guarded boot service must succeed; actual spindle behavior, firmware/controller overrides and drive health still require hardware observation.')
    return rows
