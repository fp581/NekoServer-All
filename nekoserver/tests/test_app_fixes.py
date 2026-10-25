"""The apps review: health checks that match what setup writes, hardware video that
remembers your choice, and imports, Nextcloud and onboarding that say what is left."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def client(category_field, category):
    """The download client media-connect.py makes in an Arr app."""
    return [{'name': 'Neko-qBittorrent', 'enable': True, 'implementation': 'QBittorrent', 'fields': [
        {'name': 'host', 'value': 'qbittorrent'}, {'name': 'port', 'value': 8080},
        {'name': 'username', 'value': 'u'}, {'name': category_field, 'value': category}]}]


class HealthAuditCategoryTests(unittest.TestCase):
    """Each Arr app names its qBittorrent category field after its media; the audit read
    a field "category" none of them has, so it said "still to set up" for ever."""
    def test_the_download_client_setup_makes_counts_as_configured(self):
        health = load('media_health_check', 'media-health.py')
        for app, field, category in (('sonarr', 'tvCategory', 'tv'), ('radarr', 'movieCategory', 'movies'),
                                     ('lidarr', 'musicCategory', 'music')):
            self.assertTrue(health.configured(app, 'downloadclient', client(field, category), [app]), app)
            self.assertFalse(health.configured(app, 'downloadclient', client(field, 'other'), [app]), app)

    def test_one_list_for_the_connector_and_the_audit(self):
        import qbit_setup
        connect = load('media_connect_check', 'media-connect.py')
        self.assertIs(connect.CATEGORY_FIELDS, qbit_setup.CATEGORY_FIELDS)


class Jellyfin:
    """Jellyfin's sign-in and encoding settings, as hardware video uses them."""
    def __init__(self, accel='none'):
        self.accel, self.saved = accel, None
        self.headers = {'Authorization': 'MediaBrowser Client="x"'}

    def call(self, endpoint, method='GET', payload=None):
        if endpoint == '/Users/AuthenticateByName':
            return {'AccessToken': 't', 'User': {'Policy': {'IsAdministrator': True}}}
        if endpoint == '/System/Configuration/encoding':
            if method == 'GET':
                return {'HardwareAccelerationType': self.accel, 'QsvDevice': ''}
            self.accel, self.saved = payload['HardwareAccelerationType'], payload
        return {}

    def close(self):
        pass


class HardwareVideoMemoryTests(unittest.TestCase):
    """Switched off by you stays off, also after a run that could not check the app."""
    CHIP = (('intel', '/dev/dri/renderD128', 'qsv', 'qsv'),)

    def setup_run(self, hv, report, jellyfin, check):
        before = hv.previous(report)
        with mock.patch('account_store.enabled', return_value=True), \
                mock.patch('account_store.get', return_value={'username': 'u', 'password': 'p' * 12}), \
                mock.patch('account_setup.wait'):
            done = hv.apps_on({'media': {'services': ['jellyfin']}}, self.CHIP, lambda line: None,
                              client_factory=lambda app: jellyfin, check=check, before=before)
        hv.outcome(mock.Mock(), report, self.CHIP, done, before)
        return done

    def test_an_inconclusive_run_does_not_forget_you_switched_it_off(self):
        from modules import hardware_video as hv
        works = lambda app, node: {'codecs': ['h264'], 'encode': True, 'qsv': True}
        not_running = lambda app, node: None                                       
        with tempfile.TemporaryDirectory() as d:
            report, jellyfin = Path(d) / 'hardware-video.json', Jellyfin()
            self.assertEqual(self.setup_run(hv, report, jellyfin, works)['jellyfin'], 'on (qsv)')
            jellyfin.accel = 'none'                                   # you switch it off
            self.assertEqual(self.setup_run(hv, report, jellyfin, not_running)['jellyfin'], hv.SOFTWARE)
            self.assertEqual(self.setup_run(hv, report, jellyfin, works)['jellyfin'], hv.SWITCHED_OFF)
            self.assertEqual(jellyfin.accel, 'none')

    def test_a_run_without_a_chip_keeps_the_memory(self):
        from modules import hardware_video as hv
        with tempfile.TemporaryDirectory() as d:
            report = Path(d) / 'reports/hardware-video.json'
            report.parent.mkdir()
            report.write_text(json.dumps({'apps': {'jellyfin': hv.SWITCHED_OFF}}))
            ctx = mock.Mock(apply=True, config={'data_mount': d, 'media': {'services': ['jellyfin']}})
            with mock.patch.object(hv.gpu, 'graphics', return_value=[]), \
                    mock.patch.object(hv.gpu, 'render_nodes', return_value=[]), \
                    mock.patch.object(hv, 'GPU_DEVICE', Path(d) / 'dri'):
                hv.run(ctx)
            self.assertEqual(hv.previous(report), {'jellyfin': hv.SWITCHED_OFF})


def chip_run(plain_ok, low_power_ok, entries=('VAEntrypointEncSliceLP',)):
    """docker as hardware video calls it: vainfo, then the test encodes."""
    lines = 'VAProfileH264Main : VAEntrypointVLD\n' + ''.join(f'VAProfileH264Main : {e}\n' for e in entries)
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ['docker', 'ps']:
            return mock.Mock(stdout='abc123\n', returncode=0)
        if argv[-1:] != ['-']:
            return mock.Mock(stdout=lines, returncode=0)          # vainfo
        ok = low_power_ok if '-low_power' in argv else plain_ok
        return mock.Mock(stdout='', returncode=0 if ok else 1)
    return run, calls


class LowPowerEncoderTests(unittest.TestCase):
    """Intel chips whose only H.264 encoder is the low-power one (Jasper Lake, say)."""
    def test_a_low_power_only_chip_is_tested_in_low_power_mode(self):
        from modules import hardware_video as hv
        run, calls = chip_run(plain_ok=False, low_power_ok=True)
        self.assertEqual(hv.probe('jellyfin', '/dev/dri/renderD128', run),
                         {'codecs': ['h264'], 'encode': True, 'low_power': True, 'qsv': True})
        self.assertIn('-low_power', calls[-1])            # Quick Sync tried in low-power mode too
        self.assertIn('h264_qsv', calls[-1])

    def test_a_chip_with_the_normal_encoder_is_not_tried_in_low_power_mode(self):
        from modules import hardware_video as hv
        run, calls = chip_run(plain_ok=False, low_power_ok=True,
                              entries=('VAEntrypointEncSlice', 'VAEntrypointEncSliceLP'))
        self.assertEqual(hv.probe('jellyfin', '/dev/dri/renderD128', run), {'codecs': ['h264'], 'encode': False})
        self.assertFalse(any('-low_power' in argv for argv in calls))

    def test_jellyfin_gets_its_low_power_switch_and_immich_vaapi_stays_in_software(self):
        from modules import hardware_video as hv
        video = {'codecs': ['h264'], 'encode': True, 'low_power': True, 'qsv': False}
        jellyfin = Jellyfin()
        said = hv.configure_jellyfin({'username': 'u', 'password': 'p'}, 'vaapi', '/dev/dri/renderD128', jellyfin, video)
        self.assertEqual(said, 'on (vaapi)')
        self.assertIs(jellyfin.saved['EnableIntelLowPowerH264HwEncoder'], True)
        immich = mock.Mock()
        self.assertEqual(hv.configure_immich({'username': 'u', 'password': 'p'}, 'vaapi', '/dev/dri/renderD128', immich, video),
                         hv.LOW_POWER_ONLY)
        immich.call.assert_not_called()
        self.assertIsNone(hv.outcome(mock.Mock(), Path(tempfile.mkdtemp()) / 'r.json', [('intel', '/dev/dri/renderD128', 'qsv', 'qsv')],
                                     {'jellyfin': 'on (vaapi)', 'immich': hv.LOW_POWER_ONLY}))

    def test_a_normal_chip_gets_no_low_power_switch(self):
        from modules import hardware_video as hv
        jellyfin = Jellyfin()
        hv.configure_jellyfin({'username': 'u', 'password': 'p'}, 'qsv', '/dev/dri/renderD128', jellyfin,
                              {'codecs': ['h264'], 'encode': True, 'qsv': True})
        self.assertNotIn('EnableIntelLowPowerH264HwEncoder', jellyfin.saved)


class GraphicsDeviceGoneTests(unittest.TestCase):
    """Docker refuses to start a container whose device is missing."""
    def test_immich_starts_without_the_device_when_the_pc_has_none(self):
        from modules import photos
        extra = ['-f', '/srv/nekoserver/compose/immich/hardware.yaml', '-f', '/srv/nekoserver/compose/immich/maintenance-images.json']
        with tempfile.TemporaryDirectory() as d, mock.patch.object(photos, 'compose_image_override', return_value=extra):
            said = []
            self.assertEqual(photos.usable_additions('compose.yaml', said.append, gpu=Path(d) / 'missing'),
                             ['-f', '/srv/nekoserver/compose/immich/maintenance-images.json'])
            self.assertTrue(said)
            self.assertEqual(photos.usable_additions('compose.yaml', said.append, gpu=d), extra)

    def test_the_hardware_video_step_takes_immichs_addition_out(self):
        from modules import hardware_video as hv
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'compose/immich' / hv.HARDWARE
            target.parent.mkdir(parents=True)
            target.write_text('{}')
            self.assertTrue(hv.drop_immich_chip({'data_mount': d}, lambda line: None))
            self.assertFalse(target.exists())
            self.assertFalse(hv.drop_immich_chip({'data_mount': d}, lambda line: None))


class UnmountedShareTests(unittest.TestCase):
    """A network share not mounted now is gone, like an unplugged drive."""
    def test_an_unmounted_share_is_gone_and_a_finished_import_does_not_nag(self):
        import source_import as si
        with tempfile.TemporaryDirectory() as d:
            media = Path(d) / 'hdd'
            (media / 'library/movies').mkdir(parents=True)
            (Path(d) / 'nas').mkdir()        # the share unmounted: an empty folder on this PC
            source = {'category': 'movies', 'type': 'remote', 'uri': 'smb://nas/movies',
                      'mounted_path': str(Path(d) / 'nas'), 'copy_to_library': True}
            with mock.patch.object(si, 'findmnt', return_value='ext4'), self.assertRaises(si.SourceGone):
                si.source_folder(source)
            with mock.patch.object(si, 'findmnt', return_value='ext4'):
                row = si.import_one(source, media, True, (None, None), None, {}, again=True)
                self.assertEqual(row['status'], 'needs_setup')
                self.assertIn('not found', row['reason'])
                row = si.import_one(source, media, True, (None, None), None, {si.source_key(source): 'movies'}, again=True)
                self.assertEqual(row['status'], 'imported_earlier')


class NextcloudProxyTests(unittest.TestCase):
    def test_docker_range_is_trusted_and_a_home_range_gateway_only_by_itself(self):
        import gateway_tools
        info = lambda *gateways: json.dumps([{'NetworkSettings': {'Networks': {
            f'n{i}': {'Gateway': g} for i, g in enumerate(gateways)}}}])
        for gateways, expected in ((('172.23.0.1',), ['172.16.0.0/12']),
                                   (('192.168.80.1',), ['172.16.0.0/12', '192.168.80.1'])):
            with mock.patch.object(gateway_tools, 'command', side_effect=['abc\n', info(*gateways)]):
                self.assertEqual(gateway_tools.cloud_proxies(['docker', 'compose']), expected)


class SeerrSonarrVersionTests(unittest.TestCase):
    def test_sonarr_5_is_not_refused(self):
        import seerr_providers as sp
        seerr, manager = mock.Mock(), mock.Mock()
        seerr.call.return_value = []
        manager.headers = {}
        manager.call.side_effect = lambda endpoint, *a: {'/system/status': {'version': '5.0.1.100', 'urlBase': ''},
                                                          '/rootfolder': []}[endpoint]
        with mock.patch.object(sp, 'app_key', return_value='key'), \
                self.assertRaisesRegex(sp.SetupRequired, 'root folder'):
            sp.provider(seerr, manager, '/srv', 'sonarr', False, input)


class QbitAutomaticModeTests(unittest.TestCase):
    class Fake:
        def __init__(self, automatic):
            self.preferences = {'save_path': '/data/downloads', 'auto_tmm_enabled': automatic}
            self.categories = {}

        def login(self, *a):
            pass

        def close(self):
            pass

        def call(self, endpoint, form=None, json_result=False):
            if endpoint == '/app/preferences':
                return dict(self.preferences)
            if endpoint == '/app/setPreferences':
                self.preferences.update(json.loads(form['json']))
            if endpoint == '/torrents/categories':
                return self.categories
            if endpoint == '/torrents/createCategory':
                self.categories[form['category']] = {'savePath': form['savePath']}
            return {}

    def test_switching_automatic_mode_on_is_said_once(self):
        import qbit_setup
        fake = self.Fake(False)
        said = []
        qbit_setup.configure('u', 'p', apply=True, categories=('music',), client=fake, output=said.append)
        self.assertEqual(sum('automatic mode' in line for line in said), 1)
        said.clear()
        qbit_setup.configure('u', 'p', apply=True, categories=('music',), client=fake, output=said.append)
        self.assertFalse(any('automatic mode' in line for line in said))


class OnboardingReasonTests(unittest.TestCase):
    def run_onboarding(self, d, code, checks):
        from modules import onboarding
        release = Path(d) / 'installer/releases' / ('v' + onboarding.VERSION)
        release.mkdir(parents=True)
        (release / 'onboard.py').write_text('')
        (Path(d) / 'reports').mkdir()
        (Path(d) / 'reports/onboarding.json').write_text(json.dumps({'checks': checks}))
        logs = []
        ctx = mock.Mock(apply=True, plan=(), log=logs.append,
                        config={'data_mount': d, 'onboarding': {}, 'media': {'services': ['sonarr']}})
        with mock.patch.object(onboarding.sys.stdin, 'isatty', return_value=False), \
                mock.patch.object(onboarding.account_store, 'enabled', return_value=False), \
                mock.patch.object(onboarding.subprocess, 'run', return_value=mock.Mock(returncode=code)), \
                mock.patch.object(onboarding.guided_setup, 'checkpoint', return_value=False):
            return onboarding.run(ctx)

    def test_a_failed_connector_is_a_step_left_with_its_reason(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.run_onboarding(d, 2, [{'check': 'http:sonarr', 'status': 'pass'},
                                                {'check': 'end_to_end_media', 'status': 'needs_setup', 'reason': 'witnessed'},
                                                {'check': 'jellyfin:tv', 'status': 'needs_setup', 'reason': 'later'},
                                                {'check': 'connector', 'status': 'fail'}])
        self.assertEqual(result['status'], 'needs_setup')
        self.assertIn('Prowlarr, the download client or the library folders', result['reason'])
        self.assertIn('sudo bash install.sh', result['reason'])

    def test_a_step_still_to_set_up_names_its_reason(self):
        with tempfile.TemporaryDirectory() as d:
            result = self.run_onboarding(d, 1, [{'check': 'seerr:provider:sonarr', 'status': 'needs_setup',
                                                 'reason': 'Profile selection skipped'}])
        self.assertIn('seerr:provider:sonarr: Profile selection skipped', result['reason'])


class NextcloudSettingsTests(unittest.TestCase):
    def test_a_missing_enabled_means_set_up_everywhere(self):
        import config_checks
        config = {'nextcloud': {'admin_user': 'admin'}}
        config_checks.nextcloud_default(config, ())
        self.assertIs(config['nextcloud']['enabled'], True)
        off = {'nextcloud': {'enabled': False}}
        config_checks.nextcloud_default(off, ('nextcloud',))
        self.assertIs(off['nextcloud']['enabled'], False)
        # No section and no Nextcloud step: nothing installed it.
        empty = {}
        config_checks.nextcloud_default(empty, ('media',))
        self.assertEqual(empty, {})
        config_checks.nextcloud_default(empty, ('nextcloud',))
        self.assertEqual(empty, {'nextcloud': {'enabled': True}})

    def test_its_database_gets_a_minute_to_stop(self):
        from modules import nextcloud
        config = {'data_mount': '/srv/nekoserver', 'media_mount': '/mnt/nekoserver-media', 'nextcloud': {}}
        self.assertEqual(nextcloud.model(config, 'nextcloud:31-apache')['services']['database']['stop_grace_period'], '60s')


class SourceWizardLevelTests(unittest.TestCase):
    """Its folders become the library's shows, films and artists."""
    def wizard(self):
        return load('source_wizard_levels', 'source-wizard.py')

    def test_a_single_shows_folder_or_loose_files_are_warned_about(self):
        wizard = self.wizard()
        with tempfile.TemporaryDirectory() as d:
            show = Path(d) / 'Breaking Bad'
            for name in ('Season 1', 'Season 02'):
                (show / name).mkdir(parents=True)
            self.assertIn('one show', wizard.level_warning(show, 'tv'))
            (Path(d) / 'film.mkv').write_text('')
            self.assertIn('video files', wizard.level_warning(d, 'movies'))
            shows = Path(d) / 'Shows'
            (shows / 'Breaking Bad').mkdir(parents=True)
            self.assertIsNone(wizard.level_warning(shows, 'tv'))
            self.assertIsNone(wizard.level_warning(Path(d) / 'not plugged in', 'tv'))

    def test_answering_no_to_the_warning_adds_nothing(self):
        wizard = self.wizard()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / 'Season 1').mkdir()
            replies = iter(['yes', 'tv', 'local', 'One show', d, 'no', 'no'])
            asked = []

            def reader(prompt):
                asked.append(prompt)
                return next(replies)
            with mock.patch('builtins.print'):
                sources = wizard.collect({}, reader)['sources']
        self.assertEqual(sources, [])
        self.assertTrue(any('contains your shows' in p for p in asked))


if __name__ == '__main__':
    unittest.main()
