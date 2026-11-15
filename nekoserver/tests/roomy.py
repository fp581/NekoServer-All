"""Make a test's restore see a roomy drive, whatever this computer has free.

Every restore first checks the drive keeps 5 GB (or 5% of it) free (neko_common.ensure_room).
The tests write into a temporary folder on this computer, so with less than about 13 GB
free there, every test of how a restore behaves failed with "not enough free space".
Tests about room itself patch the free space they want inside the test, which wins over this."""

import shutil
from unittest import mock

TB = 1024**4
ROOMY = shutil._ntuple_diskusage(total=2 * TB, used=TB, free=TB)


def roomy_drive(test):
    """Give `test` (a TestCase, from its setUp) a drive with 1 TB free until it ends."""
    patcher = mock.patch('shutil.disk_usage', return_value=ROOMY)
    patcher.start()
    test.addCleanup(patcher.stop)
