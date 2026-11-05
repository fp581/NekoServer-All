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


class PageCatalogue(unittest.TestCase):
    def test_the_page_keeps_no_names_or_ports_of_its_own(self):
        page = (ROOT / 'dashboard/app.js').read_text()
        catalogue = page[page.index('const CATALOG = {') : page.index('\n};\n', page.index('const CATALOG = {'))]
        for app in SHARED['apps']:
            line = next(row for row in catalogue.splitlines() if row.strip().startswith(app['id'] + ':'))
            with self.subTest(app['id']):
                self.assertNotIn('port', line)
                self.assertNotIn('name', line)

    def test_the_gateway_takes_its_apps_and_ports_from_the_shared_list(self):
        import gateway_tools

        published = {app['id']: app['tailnet'] for app in SHARED['apps'] if 'tailnet' in app}
        self.assertEqual({app: row[2] for app, row in gateway_tools.APPS.items()}, published)
        self.assertEqual(gateway_tools.APPS['navidrome'][0], 'Music')


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


class ShellCopies(unittest.TestCase):
    def test_install_sh_names_the_shared_lock_and_journal(self):
        """install.sh runs before Python may be there, so it writes these two paths out:
        checked against the shared facts, so they can't drift."""
        script = (ROOT / 'install.sh').read_text()
        self.assertIn('exec 9>>' + SHARED['setup_lock'] + '\n', script)
        self.assertIn('-e ' + SHARED['recovery']['maintenance']['journal'] + ' ]]', script)
        for path in re.findall(r'/etc/nekoserver-backups/[a-z-]+\.json', script):
            known = [r['journal'] for r in SHARED['recovery'].values()] + [str(migrate.JOURNAL)]
            self.assertIn(path, known)


class OneColourSet(unittest.TestCase):
    def test_the_app_portal_uses_the_dashboards_colours(self):
        """The review of 0.47.1: the gateway's app portal kept the colours of before 0.47.0."""
        import gateway_tools

        cfg = json.loads((ROOT / 'config.json').read_text())
        cfg['gateway'] = {'services': ['jellyfin'], 'lan_ip': '192.168.1.20', 'lan_cidr': '192.168.1.0/24'}
        page = gateway_tools.portal(cfg, '192.168.1.20')
        self.assertIn((ROOT / 'dashboard/theme.css').read_text(), page)
        style = page[page.index('<style>') : page.index('</style>')]
        own = style.replace((ROOT / 'dashboard/theme.css').read_text(), '')
        self.assertIsNone(re.search(r'#[0-9a-fA-F]{3,6}\b', own), 'a colour of its own')


class SharedUnitNames(unittest.TestCase):
    def test_the_nas_watch_units_are_named_once(self):
        self.assertEqual(server_os.NAS_WATCH, SHARED['nas_watch']['service'])
        self.assertEqual(server_os.NAS_WATCH_TIMER, SHARED['nas_watch']['timer'])

    def test_a_drive_move_stops_every_shared_writer(self):
        self.assertLessEqual(set(SHARED['writers']), set(migrate.WRITERS))


if __name__ == '__main__':
    unittest.main()
