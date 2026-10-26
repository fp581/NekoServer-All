"""The docs team's fixes from the 0.40.1 release test: recovery messages that say what to
do next (IT-16), acceptance text that matches this release (IT-15), and user guides
that start with the user (D13, D14, D15)."""
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import acceptance
import host
import recover
import recovery_tools

AGAIN = 'run this command again'


def doc(name):
    return (ROOT / name).read_text()


class RecoveryMessageTests(unittest.TestCase):
    """IT-16: "Recovery command unavailable or timed out: docker" when Docker simply
    was not installed."""

    def failure(self, error):
        with mock.patch.object(recovery_tools.subprocess, 'run', side_effect=error), \
                self.assertRaises(RuntimeError) as caught:
            recovery_tools.command(['docker', 'info'], timeout=5)
        return str(caught.exception)

    def test_a_missing_program_is_named_with_how_to_get_it(self):
        text = self.failure(FileNotFoundError(2, 'No such file or directory', 'docker'))
        self.assertIn('"docker" is not installed', text)
        self.assertIn('sudo bash install.sh', text)
        self.assertIn(AGAIN, text)
        self.assertNotIn('unavailable or timed out', text)

    def test_a_slow_program_says_wait_and_where_to_look(self):
        text = self.failure(subprocess.TimeoutExpired(['docker'], 5))
        self.assertIn('did not answer within 5 seconds', text)
        self.assertIn('journalctl -u docker', text)

    def test_a_failing_program_says_where_to_look_and_never_its_output(self):
        result = subprocess.CompletedProcess(['docker'], 1, stdout=b'', stderr=b'POSTGRES_PASSWORD=secret')
        with mock.patch.object(recovery_tools.subprocess, 'run', return_value=result), \
                self.assertRaises(RuntimeError) as caught:
            recovery_tools.command(['docker', 'info'])
        self.assertIn('systemctl status docker', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    def run_main(self, argv, error):
        err = io.StringIO()
        with mock.patch.object(recover, 'run', side_effect=error), \
                mock.patch.object(recover.Path, 'read_text', return_value='{}'), redirect_stderr(err):
            code = recover.main(['--config', '/c.json', *argv])
        return code, err.getvalue()

    def test_a_missing_folder_is_named_with_what_to_do(self):
        code, text = self.run_main(['check'], FileNotFoundError(2, 'No such file or directory', '/mnt/x/restored'))
        self.assertEqual(code, 2)
        self.assertIn('/mnt/x/restored', text)
        self.assertIn('sudo mkdir -p', text)
        self.assertIn('Nothing was formatted', text)
        self.assertNotIn('Recovery failed: FileNotFoundError', text)

    def test_other_failures_say_what_kind_and_never_the_raw_details(self):
        code, text = self.run_main(['check'], ValueError('{"password": "secret"}'))
        self.assertEqual(code, 2)
        self.assertIn('could not be read', text)
        self.assertNotIn('secret', text)

    def test_checkpoint_says_whether_it_was_saved_and_what_next(self):
        with mock.patch.object(recover.recovery, 'checkpoint', return_value={'docker_version': '29.0.0'}):
            self.assertIn('--apply', recover.checkpoint({}, False)['reason'])
            self.assertIn('Saved on the SSD', recover.checkpoint({}, True)['reason'])

    def test_sudo_is_asked_for_in_words(self):
        with mock.patch.object(recovery_tools.os, 'geteuid', return_value=1000), \
                self.assertRaises(RuntimeError) as caught:
            recovery_tools.require_root()
        self.assertIn('sudo  in front', str(caught.exception))


class AcceptanceTextTests(unittest.TestCase):
    """IT-15: stale text in ACCEPTANCE.md and acceptance.py."""

    def test_container_logs_do_not_pass_without_docker(self):
        with mock.patch.object(acceptance.health, 'run', return_value=(None, '')):
            unbounded = acceptance.log_rotation()
        self.assertIsNone(unbounded)
        status, detail = acceptance.log_rotation_finding(unbounded)
        self.assertEqual(status, 'needs_setup')
        self.assertIn('not checked', detail)

    def test_supported_linux_lists_every_family(self):
        for supported in (True, False):
            _, detail = acceptance.host_finding(supported)
            for family in host.FAMILIES:
                self.assertIn(family.label, detail)
        text = doc('ACCEPTANCE.md')
        for family in host.FAMILIES:
            self.assertIn(family.label.split(' / ')[0], text)
        for stale in ('512GB', 'Bazzite'):
            self.assertNotIn(stale, text)


class UserGuideOrderTests(unittest.TestCase):
    """D13-D15: each guide starts with what a home user does; the technical part is kept,
    under a heading that says it is for administrators."""

    def test_each_guide_starts_with_the_user_and_ends_with_the_administrator(self):
        for name, first in (('GUIDED-SETUP.md', '## What setup asks, screen by screen'),
                            ('RECOVERY.md', '## Which situation are you in?'),
                            ('DASHBOARD.md', '## Open it')):
            with self.subTest(doc=name):
                text = doc(name)
                admin = text.index('# For administrators')
                self.assertLess(text.index(first), admin)
                user_part = text[:admin]
                for jargon in ('BackendState', '409', 'Funnel', 'POSIX', 'RESTORE-ISOLATED', 'hwmon',
                               'systemd-socket-proxyd', '.neko-inventory', 'explanations.py'):
                    self.assertNotIn(jargon, user_part)

    def test_the_technical_details_are_kept(self):
        for name, kept in (('GUIDED-SETUP.md', ('BackendState=Running', 'explanations.py', '(`409`)')),
                           ('RECOVERY.md', ('RESTORE-ISOLATED', '.neko-inventory', 'POSIX')),
                           ('DASHBOARD.md', ('systemd-socket-proxyd', 'Funnel', '/sys/class/hwmon'))):
            for words in kept:
                with self.subTest(doc=name, words=words):
                    self.assertIn(words, doc(name))

    def test_no_author_notes_left(self):
        text = doc('RECOVERY.md')
        for note in ('512GB', '256GB', 'Bazzite'):
            self.assertNotIn(note, text)

    def test_passwords_are_shown_with_the_show_command(self):
        for name in ('GUIDED-SETUP.md', 'ACCOUNT-SETUP.md', 'MUSIC.md'):
            with self.subTest(doc=name):
                self.assertIn('account-setup.py show', doc(name))


if __name__ == '__main__':
    unittest.main()
