"""`account-setup.py show`: one app's login on the PC's screen (D16).

The phone guide used to send you to  sudo cat .../APP-CREDENTIALS.txt  for every app,
to read a 32-letter password and type it on the phone, about five times."""

import importlib.util
import json
import subprocess
import sys
import tempfile
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
    'navidrome_listener': {'username': 'neko_listen', 'password': 'Pw-Music-0123456789abcdefghijk', 'state': 'planned'},
}


class Screen:
    def __init__(self, tty=True):
        self.tty = tty

    def isatty(self):
        return self.tty


class WhereTheLoginsAreTests(unittest.TestCase):
    """The setup review (S4): the account step run by hand ended with "Private credential
    inventory: <a file only root can read>" and no word on how to see a password."""

    def test_every_ending_of_the_account_step_says_how_to_show_a_login(self):
        from modules import accounts
        from neko_common import VERSION

        with tempfile.TemporaryDirectory() as folder:
            config = {'data_mount': folder}
            where, how = accounts.where_saved(config)
        self.assertIn('a file only the administrator can read', where)
        self.assertNotIn('/', where)  # no path to a file a beginner cannot open
        self.assertIn(f'sudo python3 {folder}/installer/releases/v{VERSION}/account-setup.py show  (it lists', how)
        self.assertIn('QR code', how)
        # Both endings use it: the step in setup and account-setup.py run by hand; neither
        # names the file alone any more, nor tells anyone to read it with cat.
        by_hand = (ROOT / 'account-setup.py').read_text()
        step = (ROOT / 'modules/accounts.py').read_text()
        self.assertIn('for line in where_saved(config):', by_hand)
        self.assertIn('for line in where_saved(ctx.config):', step)
        for source in (by_hand, step):
            self.assertNotIn('Private credential inventory', source)
            self.assertNotIn('private APP-CREDENTIALS.txt updated', source)
            self.assertNotIn('sudo cat', source)

    def test_no_other_message_of_setup_points_at_the_file_alone(self):
        """Every file of setup that names the passwords file: only the one
        that writes it, and the recovery menu, whose file is a copy taken out of a backup
        and comes with the command that opens it."""
        named = sorted(
            path.relative_to(ROOT).as_posix()
            for path in [*ROOT.glob('*.py'), *ROOT.glob('modules/*.py')]
            if 'APP-CREDENTIALS.txt' in path.read_text(encoding='utf-8')
        )
        self.assertEqual(named, ['account_store.py', 'recovery_wizard.py'])
        recovery = (ROOT / 'recovery_wizard.py').read_text(encoding='utf-8')
        self.assertEqual(recovery.count('APP-CREDENTIALS.txt'), recovery.count('Open it with:  sudo cat'))


class ShowLoginTests(unittest.TestCase):
    def run_show(self, app=None, answers=('',), tty=True, root=True, logins=LOGINS, qr=lambda text: ''):
        said, asked = [], list(answers)

        def prompt(question):
            said.append(question)
            if not asked:
                raise EOFError
            return asked.pop(0)

        with (
            mock.patch.object(setup.os, 'geteuid', return_value=0 if root else 1000),
            mock.patch.object(setup, 'saved_logins', return_value=logins),
        ):
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

    def test_navidromes_two_logins_say_which_one_is_yours(self):
        # 0.50.0, item 11. A server set up before: the listener is the phones' login.
        self.assertIn('not an administrator', setup.SHOW_HINT['navidrome_listener'])
        self.assertIn('The administrator, for managing Navidrome', setup.SHOW_HINT['navidrome'])
        # One whose Navidrome logins were made by 0.50.0 or later: the administrator is.
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / 'my-config.json'
            settings.write_text(json.dumps({'music': {'your_login': 'administrator'}}))
            with mock.patch.object(setup, 'settings_path', side_effect=Path):
                hints = setup.show_hints(settings)
                self.assertIs(setup.show_hints(Path(folder) / 'missing.json'), setup.SHOW_HINT)
        self.assertEqual(hints['navidrome'], 'Your Navidrome login, on your phone and computer too.')
        self.assertIn("NekoServer's own login for checking the music library", hints['navidrome_listener'])
        said = []
        entry = {'username': 'neko_music', 'password': 'p' * 24, 'state': 'verified'}
        setup.show_entry('navidrome', entry, lambda _: '', said.append, lambda text: '', hints)
        self.assertIn('  Your Navidrome login, on your phone and computer too.', said)

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
        with (
            mock.patch.object(setup.shutil, 'which', return_value='/usr/bin/qrencode'),
            mock.patch.object(setup.subprocess, 'run', return_value=result) as run,
        ):
            self.assertEqual(setup.terminal_qr('Pw-Jelly'), 'QR-DRAWING')
        self.assertNotIn('Pw-Jelly', ' '.join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs['input'], 'Pw-Jelly')
        _, text = self.run_show('jellyfin', qr=lambda text: 'QR-DRAWING')
        self.assertIn('scan this code', text)
        self.assertIn('QR-DRAWING', text)

    def test_a_drive_problem_is_said_plainly(self):
        with (
            mock.patch.object(setup.os, 'geteuid', return_value=0),
            mock.patch.object(setup, 'saved_logins', side_effect=RuntimeError('Storage identity mismatch')),
        ):
            said = []
            code = setup.show('/c.json', 'jellyfin', output=said.append, screen=Screen())
        self.assertEqual(code, 2)
        self.assertIn('Check that the SSD is connected', said[0])


if __name__ == '__main__':
    unittest.main()
