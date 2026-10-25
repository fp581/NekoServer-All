"""0.39.6 follow-ups of the install review: the journal limits respect the administrator's
own, and the Samba step records whether Samba was on before NekoServer switched it on."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from modules import samba, server_os

DROPIN = server_os.JOURNAL_DROPIN


class JournalLimitTests(unittest.TestCase):
    """The reported case: NekoServer's journald drop-in silently overrode limits set in
    /etc/systemd/journald.conf, because a drop-in always wins over the main file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journald = Path(self.tmp.name) / 'journald.conf'
        self.journald.write_text('[Journal]\n#SystemMaxUse=\n')

    def dropin(self, name, text):
        folder = Path(str(self.journald) + '.d')
        folder.mkdir(exist_ok=True)
        (folder / name).write_text(text)

    def limits(self, config=None):
        return server_os.journal_limits(server_os.settings(config or {})[1], self.journald)

    def test_without_settings_of_yours_nekoservers_limits_are_set(self):
        wanted, notes = self.limits()
        self.assertEqual(wanted, {'SystemMaxUse': '512M', 'RuntimeMaxUse': '64M', 'MaxRetentionSec': '14day', 'SystemKeepFree': '1G'})
        self.assertEqual(notes, [])
        text = server_os.files({}, self.journald)[DROPIN]
        self.assertEqual(text, server_os.MARKER + '[Journal]\nSystemMaxUse=512M\nRuntimeMaxUse=64M\nMaxRetentionSec=14day\nSystemKeepFree=1G\n')

    def test_a_stricter_limit_of_yours_is_kept(self):
        self.journald.write_text('[Journal]\nSystemMaxUse=256M\nMaxRetentionSec=1week\nSystemKeepFree=4G\n')
        wanted, notes = self.limits()
        self.assertNotIn('SystemMaxUse', wanted)
        self.assertNotIn('MaxRetentionSec', wanted)
        self.assertNotIn('SystemKeepFree', wanted)
        self.assertEqual(wanted['RuntimeMaxUse'], '64M')
        text = '\n'.join(notes)
        self.assertIn('your SystemMaxUse=256M (' + str(self.journald) + ") is stricter than NekoServer's 512M, so yours is kept", text)
        self.assertNotIn('SystemMaxUse', server_os.files({}, self.journald)[DROPIN])

    def test_a_looser_limit_of_yours_is_replaced_and_said(self):
        self.journald.write_text('[Journal]\nSystemMaxUse=8G\nMaxRetentionSec=0\n')
        wanted, notes = self.limits()
        self.assertEqual((wanted['SystemMaxUse'], wanted['MaxRetentionSec']), ('512M', '14day'))
        text = '\n'.join(notes)
        self.assertIn("your SystemMaxUse=8G (" + str(self.journald) + ") is replaced by NekoServer's 512M", text)
        self.assertIn('set "journal_disk_mb" under "server_os"', text)
        self.assertIn("MaxRetentionSec=0", text)     # 0 is "no time limit": never stricter

    def test_the_last_file_journald_reads_counts(self):
        self.journald.write_text('[Journal]\nSystemMaxUse=128M\n')
        self.dropin('50-mine.conf', '[Journal]\nSystemMaxUse=2G\n')     # read after journald.conf: this one counts
        self.dropin('99-late.conf', '[Journal]\nRuntimeMaxUse=32M\n')   # read after NekoServer's: it wins anyway
        self.dropin(Path(DROPIN).name, server_os.MARKER + '[Journal]\nSystemMaxUse=1M\n')   # its own is never read as yours
        wanted, notes = self.limits()
        self.assertEqual(wanted['SystemMaxUse'], '512M')
        text = '\n'.join(notes)
        self.assertIn('50-mine.conf', text)
        self.assertIn("RuntimeMaxUse=32M (" + str(self.journald) + ".d/99-late.conf) is read after NekoServer's drop-in, so it wins", text)
        self.assertNotIn('=1M', text)

    def test_unreadable_values_and_other_sections_are_no_limit_of_yours(self):
        self.journald.write_text('[Journal]\nSystemMaxUse=lots\n[Other]\nRuntimeMaxUse=1M\n')
        wanted, notes = self.limits()
        self.assertEqual((wanted['SystemMaxUse'], wanted['RuntimeMaxUse']), ('512M', '64M'))
        self.assertEqual(len(notes), 1)
        self.assertEqual(server_os.time_seconds('1h 30min'), 5400)
        self.assertEqual(server_os.size_bytes('1G'), 1024 ** 3)
        self.assertIsNone(server_os.time_seconds('soon'))

    def test_setup_logs_what_it_did_with_your_limits(self):
        self.journald.write_text('[Journal]\nSystemMaxUse=8G\n')
        said = []
        server_os.log_journal_limits({}, said.append, self.journald)
        self.assertTrue(any('replaced by NekoServer' in line for line in said), said)


class SambaBeforeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.record = Path(self.tmp.name) / 'nekoserver-samba-before.json'
        self.managed = Path(self.tmp.name) / 'nekoserver.conf'

    @staticmethod
    def systemctl(state):
        return lambda argv, **_: mock.Mock(stdout=state)

    def remember(self, state):
        with mock.patch.object(samba, 'safe_path', side_effect=lambda p: Path(p)):
            samba.remember_service('smbd.service', self.managed, self.record, self.systemctl(state))

    def test_the_state_before_the_first_setup_is_recorded_once(self):
        self.remember('LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n')
        self.assertEqual(json.loads(self.record.read_text()),
                         {'format': 'neko-samba-before-v1', 'unit': 'smbd.service', 'enabled': True, 'active': True})
        self.remember('LoadState=not-found\n')      # a later run: kept as it was
        self.assertTrue(json.loads(self.record.read_text())['enabled'])

    def test_samba_not_installed_counts_as_off(self):
        self.remember('LoadState=not-found\nUnitFileState=\nActiveState=inactive\n')
        self.assertEqual(json.loads(self.record.read_text())['enabled'], False)

    def test_shares_set_up_by_an_earlier_release_get_no_record(self):
        # Samba is on now because NekoServer switched it on: that is not "on before".
        self.managed.write_text('# Managed by NekoServer\n')
        self.remember('LoadState=loaded\nUnitFileState=enabled\nActiveState=active\n')
        self.assertFalse(self.record.exists())

    def test_the_record_is_taken_before_samba_is_installed(self):
        source = (ROOT / 'modules/samba.py').read_text()
        self.assertLess(source.index('remember_service(service,managed)'), source.index("family.install(['samba'])"))
        self.assertLess(source.index('remember_service(service,managed)'), source.index("'enable','--now',service"))


if __name__ == '__main__':
    unittest.main()
