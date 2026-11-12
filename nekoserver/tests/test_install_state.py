"""0.50.0, page P00 ("NekoServer was found on this PC"): install_state tells apart, from
what is really on the PC, a PC with nothing, a settings file alone, a setup that was cut
off (and where), a working server (and its release) and an earlier release's folder, and
returns only the choices that apply. Wizard checks T01 (a fresh PC is asked nothing about
an existing server), T02 (a cut-off setup goes on at the step that did not finish) and
T06 (changing a setting is offered where there are settings to change)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import install_state  # noqa: E402
from install_state import CHANGE, CONTINUE, RESTORE, UPDATE  # noqa: E402

TEMPLATE = json.loads((ROOT / 'config.json').read_text())
PLAN = TEMPLATE['profiles']['full']
THIS = install_state.VERSION
OLDER = '0.49.0'
PLANTED = 'planted-' + 'x' * 20  # stands for a password; must never come back


class Pc:
    """A PC made of folders: NekoServer's own folder, the system's schedules, the app
    drive, the place setup keeps its record before a drive is chosen, and the records an
    upgrade or a drive move leaves while it runs."""

    def __init__(self, top):
        self.top = Path(top)
        self.root = self.top / 'download' / 'nekoserver'
        self.units = self.top / 'systemd'
        self.data = self.top / 'ssd'
        self.early = self.top / 'early-state'
        self.upgrade = self.top / 'etc' / 'upgrade.json'
        self.move = self.top / 'etc' / 'migrate.json'
        for folder in (self.root, self.units, self.early, self.upgrade.parent):
            folder.mkdir(parents=True)
        (self.root / 'config.json').write_text(json.dumps(TEMPLATE))

    def settings(self, folder=None, **changes):
        """The person's settings: the template with drives chosen (and `changes`)."""
        config = json.loads(json.dumps(TEMPLATE))
        config['data_mount'] = str(self.data)
        config['storage']['data_ssd']['uuid'] = 'aaaa-1111'
        config['storage']['media_hdd']['uuid'] = 'bbbb-2222'
        config['notifications'] = {**config.get('notifications', {}), 'token': PLANTED}
        config.update(changes)
        folder = folder or self.root
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'my-config.json').write_text(json.dumps(config))
        return config

    def steps(self, where=None, **ended):
        """Setup's record: every step done, except those named with how they ended."""
        states = {step: {'status': 'success'} for step in PLAN}
        for step, status in ended.items():
            states[step] = {'status': status, 'reason': f'it said {PLANTED}'}
        folder = where or self.data / 'installer' / 'state'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'state.json').write_text(json.dumps(states))

    def schedule(self, version):
        (self.units / 'nekoserver-app-backup.service').write_text(
            f'[Service]\nExecStart=/srv/nekoserver/installer/runtime/v{version}/nekoserver-runtime app-snapshot\n'
        )

    def found(self):
        journals = ((install_state.UPGRADE, self.upgrade), (install_state.DRIVE_MOVE, self.move))
        return install_state.found(self.root, None, self.units, self.early, journals)


class FoundTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.pc = Pc(folder.name)

    def test_a_fresh_pc_has_nothing_and_no_choice(self):
        found = self.pc.found()
        self.assertEqual(found['kind'], install_state.NOTHING)
        self.assertIs(found['show'], False)
        self.assertEqual(found['choices'], [])
        self.assertIsNone(found['default'])
        self.assertIsNone(found['version'])
        self.assertIsNone(found['job'])

    def test_a_downloaded_copy_alone_is_not_a_server(self):
        # The folder holds a git checkout: the template, the programs, an old release note.
        (self.pc.root / '.git').mkdir()
        (self.pc.root / 'installer.py').write_text('# NekoServer\n')
        self.assertEqual(self.pc.found()['kind'], install_state.NOTHING)

    def test_settings_setup_made_and_nobody_answered_are_not_a_saved_configuration(self):
        # Setup writes my-config.json from the template, with the account the apps run as,
        # before its first question; stopping there leaves a PC with nothing.
        made = json.loads(json.dumps(TEMPLATE))
        made['media'] = {**made.get('media', {}), 'uid': 1234, 'gid': 1234}
        (self.pc.root / 'my-config.json').write_text(json.dumps(made))
        found = self.pc.found()
        self.assertEqual((found['kind'], found['show'], found['choices']), (install_state.NOTHING, False, []))

    def test_a_settings_file_alone_offers_only_to_change_it(self):
        self.pc.settings()
        found = self.pc.found()
        self.assertEqual(found['kind'], install_state.SETTINGS_ONLY)
        self.assertIs(found['show'], True)
        self.assertEqual(found['choices'], [CHANGE])
        self.assertEqual(found['settings'], str(self.pc.root / 'my-config.json'))
        self.assertIsNone(found['version'])
        self.assertIsNone(found['location'])

    def test_a_setup_cut_off_goes_on_at_the_first_step_that_did_not_finish(self):
        self.pc.settings()
        self.pc.steps(media='failed', onboarding='blocked', dashboard='running')
        found = self.pc.found()
        self.assertEqual(found['kind'], install_state.UNFINISHED)
        self.assertEqual(found['job']['kind'], install_state.SETUP)
        self.assertEqual(found['job']['step'], 'media')
        self.assertEqual(found['job']['left'], [s for s in PLAN if s in ('media', 'onboarding', 'dashboard')])
        self.assertEqual(found['choices'][0], CONTINUE)
        self.assertEqual(found['default'], CONTINUE)
        self.assertIn(CHANGE, found['choices'])
        self.assertNotIn(UPDATE, found['choices'])

    def test_a_setup_cut_off_before_the_app_drive_was_chosen_is_found_too(self):
        self.pc.settings()
        early = {step: 'success' for step in PLAN[:2]}
        (self.pc.early / 'state.json').write_text(
            json.dumps({**{s: {'status': v} for s, v in early.items()}, PLAN[2]: {'status': 'failed'}})
        )
        found = self.pc.found()
        self.assertEqual((found['kind'], found['job']['step']), (install_state.UNFINISHED, PLAN[2]))
        self.assertEqual(found['default'], CONTINUE)

    def test_a_step_never_recorded_counts_as_not_finished(self):
        self.pc.settings()
        states = {step: {'status': 'success'} for step in PLAN if step != 'nextcloud'}
        folder = self.pc.data / 'installer' / 'state'
        folder.mkdir(parents=True)
        (folder / 'state.json').write_text(json.dumps(states))
        self.assertEqual(self.pc.found()['job']['step'], 'nextcloud')

    def test_a_finished_server_is_installed_with_its_release(self):
        self.pc.settings()
        self.pc.steps()
        self.pc.schedule(THIS)
        found = self.pc.found()
        self.assertEqual(found['kind'], install_state.INSTALLED)
        self.assertEqual(found['version'], THIS)
        self.assertEqual(found['this_version'], THIS)
        self.assertEqual(found['location'], str(self.pc.data.resolve()))
        self.assertIsNone(found['job'])
        self.assertEqual(found['choices'], [CHANGE, RESTORE])
        self.assertNotIn(CONTINUE, found['choices'])
        self.assertNotIn(UPDATE, found['choices'])

    def test_a_server_on_an_older_release_is_offered_the_update_first(self):
        self.pc.settings()
        self.pc.steps()
        self.pc.schedule(OLDER)
        found = self.pc.found()
        self.assertEqual((found['kind'], found['version']), (install_state.INSTALLED, OLDER))
        self.assertEqual(found['choices'], [UPDATE, CHANGE, RESTORE])
        self.assertEqual(found['default'], UPDATE)

    def test_the_release_is_read_from_the_copy_on_the_app_drive_when_no_schedule_names_one(self):
        self.pc.settings()
        self.pc.steps()
        for version, whole in ((OLDER, True), ('0.48.2', True), ('9.9.9', False)):
            kept = self.pc.data / 'installer' / 'releases' / ('v' + version)
            kept.mkdir(parents=True)
            if whole:  # a folder without its list of files is not a release setup ran from
                (kept / 'release-manifest.json').write_text('{}')
        self.assertEqual(self.pc.found()['version'], OLDER)

    def test_steps_that_wait_for_the_person_leave_a_working_server(self):
        self.pc.settings()
        self.pc.steps(notifications='needs_setup', photo_backup='needs_setup')
        self.pc.schedule(THIS)
        found = self.pc.found()
        self.assertEqual(found['kind'], install_state.INSTALLED)
        self.assertIsNone(found['job'])
        self.assertEqual(found['waiting'], [s for s in PLAN if s in ('notifications', 'photo_backup')])
        self.assertNotIn(CONTINUE, found['choices'])

    def test_an_optional_step_that_failed_does_not_make_the_setup_unfinished(self):
        self.pc.settings()
        self.pc.steps(assistant='failed')
        self.pc.schedule(THIS)
        found = self.pc.found()
        self.assertEqual((found['kind'], found['job']), (install_state.INSTALLED, None))

    def test_an_upgrade_or_a_drive_move_that_was_cut_off_comes_first(self):
        self.pc.settings()
        self.pc.steps(media='failed')
        self.pc.schedule(OLDER)
        for journal, kind in ((self.pc.move, install_state.DRIVE_MOVE), (self.pc.upgrade, install_state.UPGRADE)):
            journal.write_text(json.dumps({'format': 'x', 'phase': 'setup'}))
            found = self.pc.found()
            self.assertEqual(found['kind'], install_state.UNFINISHED)
            self.assertEqual(found['job'], {'kind': kind, 'step': None, 'left': []})
            self.assertEqual(found['default'], CONTINUE)
            self.assertNotIn(UPDATE, found['choices'])

    def test_only_an_earlier_releases_folder_offers_its_settings(self):
        for version in ('0.36.0', '0.37.2'):
            self.pc.settings(self.pc.root.parent / ('nekoserver-v' + version))
        (self.pc.root.parent / 'nekoserver-notes').mkdir()
        found = self.pc.found()
        self.assertEqual(found['kind'], install_state.OLDER_FOLDER)
        self.assertEqual(found['settings_from'], '0.37.2')
        self.assertEqual(found['settings'], str(self.pc.root.parent / 'nekoserver-v0.37.2' / 'my-config.json'))
        self.assertEqual(found['choices'], [CHANGE])
        self.assertIsNone(found['version'])

    def test_a_new_copy_beside_a_running_server_is_the_update_route(self):
        # A newer release downloaded next to the folder the server was set up from.
        self.pc.settings(self.pc.root.parent / ('nekoserver-v' + OLDER))
        self.pc.steps()
        self.pc.schedule(OLDER)
        found = self.pc.found()
        self.assertEqual((found['kind'], found['version']), (install_state.INSTALLED, OLDER))
        self.assertEqual(found['choices'][0], UPDATE)
        self.assertEqual(found['settings_from'], OLDER)

    def test_a_server_whose_settings_are_not_here_offers_nothing_that_needs_them(self):
        self.pc.schedule(THIS)  # the schedules are on the system disk; nothing else is found
        found = self.pc.found()
        self.assertEqual((found['kind'], found['version']), (install_state.INSTALLED, THIS))
        self.assertEqual(found['choices'], [])
        self.assertIsNone(found['settings'])

    def test_restore_waits_for_settings_that_name_the_drives(self):
        config = self.pc.settings()
        config['storage']['media_hdd']['uuid'] = ''
        (self.pc.root / 'my-config.json').write_text(json.dumps(config))
        self.pc.steps()
        self.pc.schedule(THIS)
        self.assertEqual(self.pc.found()['choices'], [CHANGE])

    def test_a_record_reached_through_a_link_out_of_the_app_drive_is_not_read(self):
        self.pc.settings()
        outside = self.pc.top / 'elsewhere'
        self.pc.steps(where=outside)
        (self.pc.data / 'installer').mkdir(parents=True)
        (self.pc.data / 'installer' / 'state').symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.pc.found()['kind'], install_state.SETTINGS_ONLY)

    def test_no_value_of_a_setting_and_no_reason_text_comes_back(self):
        self.pc.settings()
        self.pc.steps(media='failed', accounts='needs_setup')
        self.pc.schedule(OLDER)
        self.pc.upgrade.write_text(json.dumps({'format': 'x', 'secret': PLANTED}))
        for found in (self.pc.found(), install_state.look(self.pc.root, None, self.pc.units, self.pc.early)):
            self.assertNotIn(PLANTED, json.dumps(found))
        self.assertIn(PLANTED, (self.pc.root / 'my-config.json').read_text())  # it was there to leak


class JudgeTests(unittest.TestCase):
    """`judge` alone, on what `look` returns: every kind from the facts, nothing else."""

    SEEN = {
        'this_version': (0, 50, 0),
        'settings': None,
        'older': [],
        'location': '/srv/nekoserver',
        'units': False,
        'unit_versions': [],
        'kept_versions': [],
        'engine': False,
        'states': {},
        'plan': ['storage', 'docker', 'media', 'assistant'],
        'optional': ['assistant'],
        'jobs': [],
        'drives_named': False,
    }

    def judged(self, **facts):
        return install_state.judge({**self.SEEN, **facts})

    def test_every_kind_is_reached_and_shows_the_page_except_nothing(self):
        done = {'storage': 'success', 'docker': 'success', 'media': 'success'}
        cases = (
            ({}, install_state.NOTHING),
            ({'settings': '/x/my-config.json'}, install_state.SETTINGS_ONLY),
            ({'older': [((0, 37, 0), '/x/nekoserver-v0.37.0/my-config.json')]}, install_state.OLDER_FOLDER),
            ({'settings': '/x/my-config.json', 'states': {**done, 'media': 'failed'}}, install_state.UNFINISHED),
            ({'settings': '/x/my-config.json', 'states': done}, install_state.INSTALLED),
            ({'engine': True}, install_state.INSTALLED),
        )
        for facts, kind in cases:
            found = self.judged(**facts)
            self.assertEqual(found['kind'], kind, facts)
            self.assertIs(found['show'], kind != install_state.NOTHING, facts)

    def test_a_choice_is_there_only_when_it_applies(self):
        done = {'storage': 'success', 'docker': 'success', 'media': 'success'}
        base = {'settings': '/x/my-config.json', 'states': done, 'units': True, 'drives_named': True}
        same = self.judged(**base, unit_versions=[(0, 50, 0)])
        newer = self.judged(**base, unit_versions=[(0, 51, 0)])
        older = self.judged(**base, unit_versions=[(0, 48, 2), (0, 49, 0)])
        unknown = self.judged(**base)
        self.assertEqual(same['choices'], [CHANGE, RESTORE])
        self.assertEqual(newer['choices'], [CHANGE, RESTORE])  # never an "update" to an older release
        self.assertEqual(older['choices'], [UPDATE, CHANGE, RESTORE])
        self.assertEqual(older['version'], '0.49.0')
        self.assertEqual((unknown['version'], unknown['choices']), (None, [CHANGE, RESTORE]))
        for found in (same, newer, older, unknown):
            self.assertNotIn(CONTINUE, found['choices'])


if __name__ == '__main__':
    unittest.main()
