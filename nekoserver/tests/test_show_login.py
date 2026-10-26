"""`account-setup.py show`: one app's login on the PC's screen (D16).

The phone guide used to send you to  sudo cat .../APP-CREDENTIALS.txt  for every app,
to read a 32-letter password and type it on the phone, about five times."""
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location('account_setup_show', ROOT / 'account-setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

LOGINS = {
    'jellyfin': {'username': 'neko_ab12cd', 'password': 'Pw-Jelly-0123456789abcdefghijk', 'state': 'verified'},
    'navidrome_listener': {'username': 'neko_listen', 'password': 'Pw-Music-0123456789abcdefghijk',
                           'state': 'planned'},
}


class Screen:
    def __init__(self, tty=True):
        self.tty = tty

    def isatty(self):
        return self.tty


class ShowLoginTests(unittest.TestCase):
    def run_show(self, app=None, answers=('',), tty=True, root=True, logins=LOGINS, qr=lambda text: ''):
        said, asked = [], list(answers)

        def prompt(question):
            said.append(question)
            if not asked:
                raise EOFError
            return asked.pop(0)
        with mock.patch.object(setup.os, 'geteuid', return_value=0 if root else 1000), \
                mock.patch.object(setup, 'saved_logins', return_value=logins):
            code = setup.show('/c.json', app, prompt=prompt, output=said.append, qr=qr, screen=Screen(tty))
        return code, '\n'.join(said)

    def test_one_app_shows_its_login_with_a_warning_then_clears_the_screen(self):
        code, text = self.run_show('jellyfin')
        self.assertEqual(code, 0)
        self.assertLess(text.index('nobody else can see it'), text.index('Pw-Jelly-0123456789abcdefghijk'))
        self.assertIn('Username: neko_ab12cd', text)
        self.assertNotIn('Pw-Music', text)
        self.assertTrue(text.endswith(setup.CLEAR_SCREEN))

    def test_the_listener_says_what_it_is_for(self):
        code, text = self.run_show('navidrome-listener')
        self.assertEqual(code, 0)
        self.assertIn('account for playing music', text)
        self.assertIn('has not confirmed this login yet', text)

    def test_never_shown_without_sudo_or_into_a_file(self):
        for kwargs, why in (({'root': False}, 'sudo'), ({'tty': False}, 'only on a screen')):
            with self.subTest(why=why):
                code, text = self.run_show('jellyfin', **kwargs)
                self.assertEqual(code, 2)
                self.assertIn(why, text)
                self.assertNotIn('Pw-Jelly', text)

    def test_the_list_shows_one_login_at_a_time(self):
        code, text = self.run_show(None, answers=('9', '2', '', ''))
        self.assertEqual(code, 0)
        self.assertIn('1. Jellyfin administrator', text)
        self.assertIn('Type one of the numbers above', text)
        self.assertIn('Pw-Music', text)
        self.assertNotIn('Pw-Jelly', text)

    def test_an_unknown_app_names_the_saved_ones_and_no_password(self):
        code, text = self.run_show('jelyfin')
        self.assertEqual(code, 2)
        self.assertIn('jellyfin, navidrome_listener', text)
        self.assertNotIn('Pw-', text)

    def test_nothing_saved_yet_points_to_setup(self):
        code, text = self.run_show('jellyfin', logins={})
        self.assertEqual(code, 1)
        self.assertIn('sudo bash install.sh', text)

    def test_ctrl_c_at_the_prompt_still_clears_the_screen(self):
        code, text = self.run_show('jellyfin', answers=())
        self.assertEqual(code, 0)
        self.assertTrue(text.endswith(setup.CLEAR_SCREEN))

    def test_the_qr_code_gets_the_password_on_its_input_not_its_command_line(self):
        """Every account can read another program's command line (/proc/PID/cmdline)."""
        result = subprocess.CompletedProcess([], 0, stdout='QR-DRAWING')
        with mock.patch.object(setup.shutil, 'which', return_value='/usr/bin/qrencode'), \
                mock.patch.object(setup.subprocess, 'run', return_value=result) as run:
            self.assertEqual(setup.terminal_qr('Pw-Jelly'), 'QR-DRAWING')
        self.assertNotIn('Pw-Jelly', ' '.join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs['input'], 'Pw-Jelly')
        _, text = self.run_show('jellyfin', qr=lambda text: 'QR-DRAWING')
        self.assertIn('scan this code', text)
        self.assertIn('QR-DRAWING', text)

    def test_a_drive_problem_is_said_plainly(self):
        with mock.patch.object(setup.os, 'geteuid', return_value=0), \
                mock.patch.object(setup, 'saved_logins', side_effect=RuntimeError('Storage identity mismatch')):
            said = []
            code = setup.show('/c.json', 'jellyfin', output=said.append, screen=Screen())
        self.assertEqual(code, 2)
        self.assertIn('Check that the SSD is connected', said[0])


if __name__ == '__main__':
    unittest.main()
