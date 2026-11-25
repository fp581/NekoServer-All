"""The dashboard's Drives page (1.0.1, storage from the dashboard; 1.0.1-contract.md): what
the last storage job's end says, the line each disk is shown with, and the typed confirm
compared as the server compares it."""

import re
import unittest
from pathlib import Path

from test_dashboard_page import NODE, page

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(NODE, 'node is not installed')
class DrivesPageTests(unittest.TestCase):
    def test_the_last_job_is_said_in_its_own_words_for_every_end(self):
        said = page(
            ('driveOutcome', {'working': True, 'step_words': 'Copying your photos while the apps keep running'}),
            ('driveOutcome', {'working': False, 'result': {'state': 'done', 'message': 'Your photos are on d3.'}}),
            ('driveOutcome', {'working': False, 'result': {'state': 'failed', 'message': 'Not enough room.'}}),
            (
                'driveOutcome',
                {'working': False, 'result': {'state': 'undone', 'message': 'Put back.', 'power_cut': True}},
            ),
            (
                'driveOutcome',
                {'working': False, 'result': {'state': 'resumed', 'message': 'Moved.', 'power_cut': True}},
            ),
            ('driveOutcome', {'working': False, 'unfinished': True, 'result': {'state': 'working'}}),
            ('driveOutcome', {'working': False, 'result': {'state': 'working', 'message': 'Copying'}}),
            ('driveOutcome', {'working': False, 'result': None}),
            ('driveOutcome', None),
        )
        cut = ' The power went off during it.'
        self.assertEqual(
            said,
            [
                ['starting', 'Working on it…', 'Copying your photos while the apps keep running'],
                ['pass', 'Done', 'Your photos are on d3.'],
                ['fail', 'Not done', 'Not enough room.'],
                ['warning', 'Put back as it was', 'Put back.' + cut],
                ['pass', 'Finished after a restart', 'Moved.' + cut],
                [
                    'warning',
                    'Finished at the next start',
                    'A change was cut off. It is finished or put back when the server starts again.',
                ],
                # A run that ended without saying how is never "working" for ever.
                ['fail', 'Not done', 'It stopped before it had finished. Nothing else was changed.'],
                None,
                None,
            ],
        )

    def test_the_typed_confirm_ignores_case_and_runs_of_spaces_as_the_server_does(self):
        said = page(
            ('confirmMatches', ['4 TB 4567', '4 TB 4567']),
            ('confirmMatches', ['  4  tb   4567 ', '4 TB 4567']),
            ('confirmMatches', ['4 TB 4568', '4 TB 4567']),
            ('confirmMatches', ['4TB 4567', '4 TB 4567']),
            ('confirmMatches', ['', '']),
            ('confirmMatches', ['anything', None]),
            ('confirmMatches', ['Delete photos', 'delete photos']),
        )
        self.assertEqual(said, [True, True, False, False, False, False, True])

    def test_a_disk_is_one_line_of_what_it_is(self):
        said = page(
            (
                'diskLine',
                {'model': 'WDC WD40EFRX', 'size_bytes': 4000787030016, 'transport': 'sata', 'rotational': True},
            ),
            ('diskLine', {'model': 'Samsung 990', 'size_bytes': 1e12, 'transport': 'nvme', 'rotational': False}),
            ('diskLine', {'size_bytes': 16e9, 'transport': 'usb'}),
            ('diskLine', {}),
        )
        self.assertEqual(
            said,
            # bytes() keeps the number and its unit together (a no-break space).
            ['WDC WD40EFRX · 4.0 TB · SATA HDD', 'Samsung 990 · 1.0 TB · NVMe SSD', 'Disk · 16.0 GB · USB', 'Disk · —'],
        )

    def test_a_move_says_all_apps_pause_for_its_last_step_and_how_long_that_took_last_time(self):
        said = page(
            ('moveWords', ['photos', None]),
            ('moveWords', ['photos', {'state': 'done', 'paused_seconds': None}]),
            ('moveWords', ['films', {'state': 'done', 'paused_seconds': 42}]),
            ('moveWords', ['films', {'state': 'undone', 'paused_seconds': 1}]),
            ('moveWords', ['films', {'state': 'done', 'paused_seconds': -3}]),
            ('moveWords', ['films', {'state': 'done', 'paused_seconds': '9'}]),
        )
        plain = (
            'are copied while the apps keep running. For the last step all apps pause and start again by themselves.'
        )
        self.assertEqual(
            said,
            [
                f'Your photos {plain}',
                f'Your photos {plain}',
                f'Your films {plain} Last time that took 42 seconds.',
                f'Your films {plain} Last time that took 1 second.',
                f'Your films {plain}',
                f'Your films {plain}',
            ],
        )

    def test_a_kind_backed_up_on_its_own_drive_says_same_drive(self):
        said = page(
            ('kindPlace', {'kind': 'photos', 'place_words': 'Extra drive d3', 'same_drive': True}),
            ('kindPlace', {'kind': 'photos', 'place_words': 'Extra drive d3', 'same_drive': False}),
            ('kindPlace', {}),
            ('kindPlace', {'kind': 'music', 'place_words': 'Extra drive d3', 'same_drive': True}),
        )
        self.assertEqual(said[0][:2], ['warning', 'Extra drive d3 · same drive as its backup'])
        self.assertIn('not against losing that drive', said[0][2])
        self.assertIn('weekly copy on the USB drive is on another drive', said[0][2])
        self.assertEqual(said[1], ['pass', 'Extra drive d3', None])
        self.assertEqual(said[2], ['pass', 'Not known', None])
        # The weekly USB copy holds no music.
        self.assertIn('Music is not in the weekly USB copy', said[3][2])

    def test_a_move_beside_the_backups_needs_its_own_tick(self):
        said = page(
            ('besideTick', {'beside': ['photos']}),
            ('besideTick', {'beside': []}),
            ('besideTick', {}),
        )
        self.assertEqual(said, [True, False, False])
        source = (ROOT / 'dashboard/app.js').read_text()
        self.assertIn('beside_ok: true', source)

    def test_the_page_is_in_the_menu_and_needs_the_control_service(self):
        html = (ROOT / 'dashboard/index.html').read_text(encoding='utf-8')
        nav = re.findall(r'data-route="([a-z]+)"', html)
        self.assertEqual(nav[nav.index('media') + 1], 'drives')  # beside Media folders
        self.assertIn('data-page="drives"', html)
        self.assertIn('<div id="drives-page"></div>', html)
        self.assertEqual(html.count('id="drives"'), 1)  # Server health's drive cards keep theirs
        app = (ROOT / 'dashboard/app.js').read_text(encoding='utf-8')
        for named in ('CONTROL_ROUTES', 'NEEDS_CONTROL'):
            (line,) = re.findall(named + r' = new Set\(\[([^\]]*)\]\)', app)
            self.assertIn("'drives'", line, named)
        # Every change goes through the contract's routes; the page names no command.
        for route in ('storage-state', 'storage-look', 'drive-use', 'drive-release', 'data-move'):
            self.assertIn("'" + route + "'", app)
        for route in ('data-move-back', 'data-delete-old', 'mirror-add'):
            self.assertIn("'" + route + "'", app)
        self.assertNotIn('/dev/sd', app)


if __name__ == '__main__':
    unittest.main()
