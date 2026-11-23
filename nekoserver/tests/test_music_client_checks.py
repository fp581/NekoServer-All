"""music_client: the music check reads its credential only from the SSD, and only talks to
Navidrome on this server. SonarCloud asked (0.52.2) for both to be proved where they are
used; these tests hold what a person sees to what it was before."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import music_client  # noqa: E402

GOOD = music_client.credential('listener', 'a password')


class LocalServerOnly(unittest.TestCase):
    def test_other_addresses_are_refused(self):
        for base in (
            'http://example.com:4533',
            'https://127.0.0.1:4533',
            'http://127.0.0.1:',
            'http://127.0.0.1:123456',
            'http://127.0.0.1:4533/x',
            'http://127.0.0.1:٤٥',  # Arabic-Indic digits are not a port
        ):
            with self.subTest(base=base), self.assertRaisesRegex(RuntimeError, 'only contacts the local server'):
                music_client.inspect(GOOD, base)

    def test_the_local_server_is_called(self):
        opener = mock.Mock()
        opener.open.side_effect = OSError('nothing listens')
        with (
            mock.patch.object(music_client, 'local_opener', return_value=opener),
            self.assertRaisesRegex(RuntimeError, 'Local music API unavailable'),
        ):
            music_client.inspect(GOOD, 'http://127.0.0.1:4533')
        self.assertTrue(opener.open.call_args[0][0].startswith('http://127.0.0.1:4533/rest/ping.view?'))


class CredentialOnTheSsd(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(os.path.realpath(tmp.name))
        self.config = {'data_mount': str(self.data)}
        self.file = self.data / 'appdata/navidrome/nekoserver-client.json'

    def test_no_credential_needs_setup(self):
        self.assertEqual(music_client.check(self.config)['status'], 'needs_setup')

    def test_a_credential_others_can_read_fails(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text(json.dumps(GOOD))
        self.file.chmod(0o644)
        self.assertEqual(music_client.check(self.config)['status'], 'fail')

    def test_a_private_root_credential_is_checked(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text(json.dumps(GOOD))
        self.file.chmod(0o600)
        seen = os.stat(self.file)
        owned = os.stat_result((seen.st_mode, *seen[1:4], 0, *seen[5:]))
        with (
            mock.patch.object(music_client.os, 'stat', return_value=owned),
            mock.patch.object(music_client, 'inspect', return_value={'status': 'pass'}) as inspect,
        ):
            self.assertEqual(music_client.check(self.config), {'status': 'pass'})
        self.assertEqual(inspect.call_args[0][0], GOOD)

    @unittest.skipIf(os.name == 'nt', 'symbolic links need extra rights on Windows')
    def test_a_link_out_of_the_ssd_is_refused(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        (self.data / 'appdata').mkdir()
        os.symlink(elsewhere.name, self.data / 'appdata/navidrome')
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            music_client.check(self.config)


if __name__ == '__main__':
    unittest.main()
