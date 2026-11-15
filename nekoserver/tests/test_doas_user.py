"""Deep check 298: a tool started with doas (doas python3 migrate.py) read SUDO_USER
alone, which doas does not set, so it took the server for nobody's or the "neko" account.
install.sh took DOAS_USER over; now every tool does (neko_common.adopt_doas_user)."""

import os
import pwd
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import neko_common


class DoasUserTests(unittest.TestCase):
    def test_a_tool_started_with_doas_knows_whose_server_it_is(self):
        me = pwd.getpwuid(os.getuid())
        env = {k: v for k, v in os.environ.items() if not k.startswith(('SUDO_', 'DOAS_'))}
        env['DOAS_USER'] = me.pw_name
        said = subprocess.run(
            [
                sys.executable,
                '-c',
                'import os, recovery_wizard; print(os.environ.get("SUDO_USER"), '
                'recovery_wizard.owner({"media": {"uid": 4321, "gid": 4321}}))',
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split(None, 1)
        self.assertEqual(said[0], me.pw_name)
        if me.pw_uid:  # (root is never taken as the owner)
            self.assertEqual(said[1].strip(), f'({me.pw_uid}, {me.pw_gid})')

    def test_sudo_wins_and_an_unknown_account_sets_no_uid(self):
        environ = {'DOAS_USER': 'neko', 'SUDO_USER': 'alice'}
        neko_common.adopt_doas_user(environ)
        self.assertEqual(environ, {'DOAS_USER': 'neko', 'SUDO_USER': 'alice'})
        environ = {'DOAS_USER': 'no-such-account-here', 'SUDO_USER': ''}
        neko_common.adopt_doas_user(environ)
        self.assertEqual(environ['SUDO_USER'], 'no-such-account-here')
        self.assertNotIn('SUDO_UID', environ)


if __name__ == '__main__':
    unittest.main()
