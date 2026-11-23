"""onboard.py's steps, as the report it writes and the programs it runs (0.52.2 split
main into helpers; mutation test T6 found that nothing checked what main put together).

The drives, the apps and the programs onboarding runs are fakes; what is checked is the
report setup and the dashboard read (reports/onboarding.json), the exit code, and the
commands it gives the importer and the connector."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import onboard  # noqa: E402

APPS = ['jellyfin', 'seerr', 'sonarr', 'radarr', 'prowlarr', 'qbittorrent']
FIRST = {'check': 'drives', 'status': 'pass'}
LIBRARIES = [
    {'check': 'jellyfin:libraries', 'status': 'pass'},
    {'check': 'seerr:request_providers', 'status': 'needs_setup'},
]
PROVIDERS = [{'check': 'seerr:providers', 'status': 'pass'}]
READY = [{'check': 'apps_ready', 'status': 'pass'}]
NOTHING_TO_CONNECT = 'No app connections apply to the selected apps; checking their accounts and readiness.'


class Onboarding:
    """One run of onboard.main with every outside part faked."""

    def __init__(self, apps=APPS, exits=None):
        self.apps = apps
        self.exits = exits or {}  # program file name -> its exit code
        self.ran = {}  # program file name -> its arguments
        self.reports = []
        self.report_calls = []
        self.music = object()  # the music library onboarding found
        self.checked = []  # the settings the apps' readiness checks were given

    def run_program(self, script, argv):
        self.ran[Path(script).name] = [str(x) for x in argv]
        return self.exits.get(Path(script).name, 0)

    def report(self, root, films, check_http, apps, music):
        self.report_calls.append(check_http)
        assert music is self.music, 'the report is made without the music library found'
        return [dict(FIRST)]

    def readiness(self, config):
        self.checked.append(config)
        return [dict(r) for r in READY]

    def __call__(self, *options):
        tmp = tempfile.TemporaryDirectory()
        self.root, self.media = Path(tmp.name) / 'ssd', Path(tmp.name) / 'hdd'
        # The settings name other folders than the drives given on the command line
        # (--data-root and --media-root): the drives given are the ones used.
        config = {'data_mount': '/srv/old-ssd', 'media_mount': '/srv/old-hdd', 'media': {'services': self.apps}}
        said = io.StringIO()
        with (
            tmp,
            mock.patch.object(onboard, 'checked_settings', lambda value: value),
            mock.patch.object(onboard, 'read_settings', return_value=config),
            mock.patch.object(onboard, 'app_wanted', return_value=True),
            mock.patch.object(onboard, 'the_two_drives', return_value=(self.root, self.media)),
            mock.patch.object(onboard, 'report', self.report),
            mock.patch.object(onboard, 'music_library', side_effect=self.music_library),
            mock.patch.object(onboard, 'files_root', side_effect=self.files_root),
            mock.patch.object(onboard, 'safe_report', lambda output, rows: self.reports.append(list(rows))),
            mock.patch.object(onboard, 'run_program', self.run_program),
            mock.patch.object(onboard.media_libraries, 'configure', return_value=[dict(r) for r in LIBRARIES]),
            mock.patch.object(onboard.seerr_providers, 'configure', return_value=[dict(r) for r in PROVIDERS]),
            mock.patch.object(onboard.seerr_providers, 'request_folders', return_value=[]),
            mock.patch('existing_page.libraries', return_value=[]),
            mock.patch.object(onboard.media_readiness, 'check', side_effect=self.readiness),
            contextlib.redirect_stdout(said),
        ):
            self.code = onboard.main(['--config', '/x/my-settings.json', *options])
        self.said = said.getvalue().splitlines()
        return self

    def music_library(self, config):
        assert (config['data_mount'], config['media_mount']) == (str(self.root), str(self.media))
        return self.music

    def files_root(self, config, kind):
        assert (config['data_mount'], kind) == (str(self.root), 'movies')
        return self.media / 'library'

    @property
    def written(self):
        """The checks of the last report written."""
        return [(r['check'], r['status']) for r in self.reports[-1]]


class FullOnboardingTests(unittest.TestCase):
    def test_every_step_ends_up_in_the_report_setup_reads(self):
        run = Onboarding()(
            '--configure', '--libraries', '--request-providers', '--sources', '--import-sources', '--apply'
        )
        self.assertEqual(
            run.written,
            [
                ('drives', 'pass'),
                ('source_inventory', 'pass'),
                ('source_import', 'pass'),
                ('jellyfin:libraries', 'pass'),  # Seerr's providers come from their own step
                ('seerr:providers', 'pass'),
                ('connector', 'pass'),
                ('apps_ready', 'pass'),
            ],
        )
        self.assertEqual(run.code, 0)
        self.assertEqual([(c['data_mount'], c['media_mount']) for c in run.checked], [(str(run.root), str(run.media))])
        self.assertEqual(run.said[:2], ['       pass  drives', f'Report: {run.root}/reports/onboarding.json'])

    def test_the_connector_is_asked_for_what_the_apps_set_up_need(self):
        run = Onboarding()('--configure', '--apply')
        cmd = run.ran['media-connect.py']
        for option in ('--root-folders', '--applications', '--download-clients', '--apply'):
            self.assertIn(option, cmd)
        self.assertNotIn('--indexers', cmd)
        self.assertIn('Configuring selected Arr root folders, Prowlarr and download-client connections.', run.said)
        self.assertNotIn(NOTHING_TO_CONNECT, run.said)

    def test_a_connector_that_fails_fails_onboarding(self):
        run = Onboarding(exits={'media-connect.py': 1})('--configure', '--apply')
        self.assertIn(('connector', 'fail'), run.written)
        self.assertEqual(run.code, 2)

    def test_without_http_checks_the_end_to_end_check_is_left_to_the_person(self):
        run = Onboarding()('--configure', '--skip-http')
        self.assertEqual(run.report_calls, [False, False])
        self.assertIn(('end_to_end_media', 'needs_setup'), run.written)
        self.assertNotIn(('apps_ready', 'pass'), run.written)

    def test_apps_with_nothing_to_connect_are_only_checked(self):
        run = Onboarding(apps=['jellyfin'])('--configure')
        self.assertNotIn('media-connect.py', run.ran)
        self.assertIn(NOTHING_TO_CONNECT, run.said)
        self.assertNotIn('connector', [check for check, _ in run.written])

    def test_jellyfins_libraries_are_not_called_nothing_to_do(self):
        run = Onboarding(apps=['jellyfin'])('--configure', '--libraries')
        self.assertNotIn(NOTHING_TO_CONNECT, run.said)


class SourceStepTests(unittest.TestCase):
    def test_sources_chosen_in_the_wizard_alone_are_copied(self):
        run = Onboarding()('--sources', '--libraries')
        self.assertIn('--selected-only', run.ran['source_import.py'])
        self.assertNotIn('--apply', run.ran['source_import.py'])

    def test_an_import_of_everything_has_no_selection(self):
        run = Onboarding()('--import-sources', '--apply', '--configure')
        self.assertNotIn('--selected-only', run.ran['source_import.py'])
        self.assertIn('--apply', run.ran['source_import.py'])
        self.assertNotIn('source-wizard.py', run.ran)

    def test_each_outcome_of_the_two_programs_has_its_status(self):
        for wizard, importer, expected in (
            (0, 1, [('source_inventory', 'pass'), ('source_import', 'needs_setup')]),
            (2, 2, [('source_inventory', 'fail'), ('source_import', 'fail')]),
        ):
            with self.subTest(wizard=wizard, importer=importer):
                run = Onboarding(exits={'source-wizard.py': wizard, 'source_import.py': importer})('--sources')
                self.assertEqual(run.written[1:3], expected)


class WithoutConnectorTests(unittest.TestCase):
    def test_library_steps_alone_say_how_to_go_on(self):
        run = Onboarding()('--libraries')
        self.assertIn('Library/source steps complete. Use --configure for Arr/download-client setup.', run.said)
        self.assertEqual(
            run.written,
            [
                ('drives', 'pass'),
                ('jellyfin:libraries', 'pass'),
                ('seerr:request_providers', 'needs_setup'),
                ('apps_ready', 'pass'),
            ],
        )

    def test_a_report_alone_changes_nothing_and_says_so(self):
        run = Onboarding()('--report-only')
        self.assertIn('No API configuration changed. Use --configure or --libraries for interactive setup.', run.said)
        self.assertEqual(run.ran, {})

    def test_request_providers_without_the_connector_are_in_the_report(self):
        run = Onboarding()('--request-providers', '--skip-http')
        self.assertEqual(
            run.written,
            [('drives', 'pass'), ('seerr:providers', 'pass'), ('accounts_libraries_playback', 'needs_setup')],
        )


class OptionTests(unittest.TestCase):
    def refused(self, *options):
        onboarding = Onboarding()
        with contextlib.redirect_stderr(io.StringIO()) as said, self.assertRaises(SystemExit):
            onboarding(*options)
        return said.getvalue().splitlines()[-1].split(': error: ', 1)[1]

    def test_a_wait_is_up_to_ten_minutes(self):
        self.assertEqual(self.refused('--wait', '601'), '--wait must be 0..600')
        self.assertEqual(Onboarding()('--report-only', '--wait', '600', '--skip-http').code, 1)

    def test_a_report_alone_does_not_import(self):
        self.assertEqual(self.refused('--report-only', '--sources'), '--report-only cannot configure or import sources')


class DriveTests(unittest.TestCase):
    def two_drives(self, data, media, mounted=lambda path: True):
        """the_two_drives for `--data-root data --media-root media`; `mounted` says which
        folders are mounted drives."""
        parser = self.parser = mock.Mock()
        parser.error.side_effect = SystemExit(2)
        args = mock.Mock(data_root=data, media_root=media)
        with mock.patch.object(onboard, 'mount_ok', side_effect=lambda path: mounted(str(path))):
            return onboard.the_two_drives(parser, args, {})

    def test_two_mounted_drives_are_taken(self):
        with tempfile.TemporaryDirectory() as ssd, tempfile.TemporaryDirectory() as hdd:
            found = self.two_drives(ssd, hdd)
            self.assertEqual(found, (Path(ssd), Path(hdd)))

    def test_one_drive_given_twice_or_one_not_mounted_is_refused(self):
        with tempfile.TemporaryDirectory() as ssd, tempfile.TemporaryDirectory() as hdd:
            for data, media, mounted in (
                (ssd, ssd, lambda p: True),
                (ssd, hdd, lambda p: p == ssd),
                (ssd, hdd, lambda p: p == hdd),
            ):
                with self.subTest(data=data, media=media), self.assertRaises(SystemExit):
                    self.two_drives(data, media, mounted)
                self.parser.error.assert_called_once_with(
                    'Expected two distinct mounted data/media filesystems; refusing to proceed'
                )


class PrintedTests(unittest.TestCase):
    def test_each_result_is_one_line_with_its_reason(self):
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            onboard.printed(
                [
                    {'check': 'jellyfin', 'status': 'pass'},
                    {'check': 'seerr', 'status': 'needs_setup', 'reason': 'no admin yet'},
                ]
            )
        self.assertEqual(said.getvalue().splitlines(), ['       pass  jellyfin', 'needs_setup  seerr: no admin yet'])

    def test_apps_still_starting_are_named_after_the_wait(self):
        said = io.StringIO()
        args = mock.Mock(skip_http=False, wait=30)
        with (
            mock.patch.object(onboard, 'wait_ready', return_value=['radarr', 'sonarr']),
            contextlib.redirect_stdout(said),
        ):
            onboard.wait_for_apps(args, APPS)
        self.assertEqual(said.getvalue().splitlines(), ['Startup wait ended; still unavailable: radarr, sonarr'])


if __name__ == '__main__':
    unittest.main()
