"""T3-R8b (after T1-R8): on a rebuilt server, a backup made before the rebuild opens only
with the old server's backup password, not the one the new setup chose. The recovery
texts said "the backup password chosen during setup", and the person typed the new one."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backup_secrets  # noqa: E402
import nextcloud_restore  # noqa: E402
import recovery_wizard  # noqa: E402

THE_OLD_ONE = 'the old password, not a new one chosen since'


def answers(*values):
    it = iter(values)
    return lambda question='': next(it)


class RebuiltServerPasswordWordsTests(unittest.TestCase):
    def test_the_password_list_review_names_the_old_servers_password(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        run = root / 'usb/passwords/monthly-20261101T040000Z'
        run.mkdir(parents=True)
        (run / recovery_wizard.SEALED).write_bytes(b'sealed')
        out = []
        with (
            mock.patch.object(recovery_wizard, 'new_folder', return_value=root / 'restored'),
            mock.patch.object(recovery_wizard, 'backup_trees', return_value=[]),
        ):
            # The list, then "no" at the review: nothing is unpacked.
            recovery_wizard.restore_password_list(
                {}, [(run, '2026-11-01 (copy on the USB drive)')], answers('1', 'no'), out.append
            )
        said = ' '.join(' '.join(out).split())
        self.assertIn('the backup password of the server that made this list', said)
        self.assertIn(THE_OLD_ONE, said)
        self.assertIn('before 0.28.5: the sudo password you had then', said)
        self.assertNotIn('chosen during setup', said)
        self.assertFalse((root / 'restored').exists())

    def test_setups_nextcloud_offer_names_the_old_servers_password(self):
        backup = SimpleNamespace(
            manifest={'created_at': 1793000000}, dump=None, sealed=True, nightly=False, date='2026-11-01'
        )
        lines, _ = nextcloud_restore.question('/mnt/nekoserver-media/cloud/files', backup)
        said = ' '.join(lines)
        self.assertIn('the backup password of the server that made this backup', said)
        self.assertIn(THE_OLD_ONE, said)
        self.assertNotIn('chosen during setup', said)

    def test_a_wrong_password_says_whose_password_it_is(self):
        self.assertIn('the backup password of the server that made this backup', backup_secrets.WRONG_PASSWORD)
        self.assertIn('on a rebuilt server, the old one', backup_secrets.WRONG_PASSWORD)
        self.assertNotIn('you chose during setup', backup_secrets.WRONG_PASSWORD)

    def test_recovery_md_says_the_same(self):
        page = (ROOT / 'RECOVERY.md').read_text(encoding='utf-8')
        self.assertIn('the backup password of the server that made the snapshot', page)
        self.assertNotIn('you need the backup password chosen during setup', page)


if __name__ == '__main__':
    unittest.main()
