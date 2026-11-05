"""The facts setup and the runtime both use are written once, in runtime/src/shared.json.

Python reads the file, the runtime builds it in. These tests fail when a copy of one of
those facts comes back in either half, so the two can never quietly disagree."""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'modules'))

import migrate  # noqa: E402
import neko_common  # noqa: E402
import server_os  # noqa: E402

SHARED = json.loads((ROOT / 'runtime/src/shared.json').read_text())
RUST = {path.name: path.read_text() for path in (ROOT / 'runtime/src').glob('*.rs')}


def rust_code(text):
    """The source without its tests (they may spell out what they expect)."""
    return text.split('#[cfg(test)]', 1)[0]


class AppCatalogue(unittest.TestCase):
    def test_setup_takes_every_app_from_the_shared_catalogue(self):
        expected = {app['id']: (app['name'], app['role'], app['port']) for app in SHARED['apps']}
        self.assertEqual(neko_common.APPS, expected)
        self.assertEqual(neko_common.MEDIA_APPS, tuple(app['id'] for app in SHARED['apps'] if app['media']))

    def test_every_app_has_its_own_port(self):
        ports = [app['port'] for app in SHARED['apps']]
        self.assertEqual(len(ports), len(set(ports)))
        self.assertTrue(all(isinstance(port, int) and 0 < port < 65536 for port in ports))

    def test_the_runtime_keeps_no_list_of_its_own(self):
        # An app name next to its port, or a fixed-size list of app names, is a copy.
        copy = re.compile(r'\("(?:jellyfin|sonarr|qbittorrent)",\s*(?:"[^"]*",\s*)*\d{2,5}\)')
        names = re.compile(r'const [A-Z_]+: \[&str; \d+\] = \[\s*"jellyfin"')
        for name, text in RUST.items():
            code = rust_code(text)
            with self.subTest(name):
                self.assertIsNone(copy.search(code))
                self.assertIsNone(names.search(code))


class RecoveryRecords(unittest.TestCase):
    PLACES = [record['journal'] for record in SHARED['recovery'].values()] + [SHARED['setup_lock']]

    def test_no_journal_or_setup_lock_is_written_out_twice(self):
        sources = {path: path.read_text() for path in ROOT.rglob('*.py') if not {'tests', 'lab'} & set(path.parts)}
        sources.update({ROOT / 'runtime/src' / name: rust_code(text) for name, text in RUST.items()})
        for path, text in sources.items():
            for place in self.PLACES:
                with self.subTest(path=path.name, place=place):
                    self.assertNotIn(f"'{place}'", text)
                    self.assertNotIn(f'"{place}"', text)

    def test_setup_and_the_uninstaller_look_for_the_records_the_runtime_writes(self):
        import uninstaller
        import upgrade

        self.assertEqual(upgrade.JOURNAL, Path(SHARED['recovery']['upgrade']['journal']))
        self.assertEqual(str(upgrade.SETUP_LOCK), SHARED['setup_lock'])
        self.assertEqual(uninstaller.SETUP_LOCKS[0], SHARED['setup_lock'])
        self.assertEqual(str(neko_common.MAINTENANCE_JOURNAL), SHARED['recovery']['maintenance']['journal'])


class SharedUnitNames(unittest.TestCase):
    def test_the_nas_watch_units_are_named_once(self):
        self.assertEqual(server_os.NAS_WATCH, SHARED['nas_watch']['service'])
        self.assertEqual(server_os.NAS_WATCH_TIMER, SHARED['nas_watch']['timer'])

    def test_a_drive_move_stops_every_shared_writer(self):
        self.assertLessEqual(set(SHARED['writers']), set(migrate.WRITERS))
        # The gateway writes to the drives too; before 0.47.1 the move left it running.
        self.assertIn('nekoserver-gateway-lan.service', migrate.WRITERS)


if __name__ == '__main__':
    unittest.main()
