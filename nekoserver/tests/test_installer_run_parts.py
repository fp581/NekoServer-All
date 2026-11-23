"""The setup driver's own parts: the settings groups it checks, where it keeps its state,
which steps run, what an inspection run writes, and what a step that waits says."""

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config_checks
import installer

LOOKER = '''DEPENDENCIES = []
def run(ctx):
    ctx.log(f"plan={ctx.plan} only={ctx.only} previous={sorted(ctx.previous)}")
'''
BREAKER = '''DEPENDENCIES = []
def run(ctx):
    raise RuntimeError("the image could not be pulled")
'''
NEEDS_BREAKER = '''DEPENDENCIES = ['breaker']
def run(ctx):
    ctx.log("the waiting step ran")
'''


class InstallerRunPartsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'modules').mkdir()
        (self.root / 'modules' / 'looker.py').write_text(LOOKER)
        (self.root / 'modules' / 'breaker.py').write_text(BREAKER)
        (self.root / 'modules' / 'after_breaker.py').write_text(NEEDS_BREAKER)
        self.settings = json.loads((ROOT / 'config.json').read_text())
        for key in ('data_mount', 'media_mount', 'external_backup_mount'):
            self.settings[key] = str(self.root / key)
        self.settings['docker'] = {**self.settings.get('docker', {}), 'persistent_root': self.settings['data_mount']}
        self.config = self.root / 'settings.json'
        self.config.write_text(json.dumps(self.settings))
        self.state = self.root / 'state'
        quiet = mock.patch('builtins.print')
        quiet.start()
        self.addCleanup(quiet.stop)
        checks = mock.patch('config_checks.validate')
        checks.start()
        self.addCleanup(checks.stop)

    def args(self, **extra):
        return argparse.Namespace(
            **{
                'all': False,
                'profile': None,
                'module': 'looker',
                'retry_failed': False,
                'retry_step': None,
                'apply': True,
                'check': False,
                'dry_run': False,
                'config': str(self.config),
                'state_dir': str(self.state),
                **extra,
            }
        )

    def test_each_settings_group_must_be_a_group_of_settings(self):
        for group in ('runtime', 'modules', 'profiles', 'storage', 'media', 'photos', 'docker'):
            with self.subTest(group=group):
                settings = json.loads(json.dumps(self.settings))
                settings[group] = ['not', 'a', 'group']
                with self.assertRaises(installer.InstallerError) as caught:
                    installer.validate_config(settings)
                self.assertEqual(str(caught.exception), f'{group} must be an object')

    def test_the_steps_that_run_nekoservers_own_program_are_real_steps(self):
        """Their files are checked before such a step starts: a name that is no step
        would leave that step's run unchecked."""
        steps = {path.stem for path in (ROOT / 'modules').glob('*.py') if not path.stem.startswith('_')}
        self.assertEqual(installer.RUNTIME_STEPS - steps, set())

    def test_a_state_folder_given_by_hand_is_used_even_with_the_ssd_mounted(self):
        with mock.patch.object(installer.os.path, 'ismount', return_value=True):
            folders = installer.state_folders(self.args(), self.settings)
        self.assertEqual(folders, (self.state, self.state, False))

    def test_the_usual_state_folder_stays_put_while_the_ssd_is_not_mounted(self):
        with (
            mock.patch.object(installer, 'DEFAULT_STATE', self.state),
            mock.patch.object(installer.os.path, 'ismount', return_value=False),
        ):
            folders = installer.state_folders(self.args(), self.settings)
        self.assertEqual(folders, (self.state, self.state, False))

    def test_a_step_without_an_on_or_off_switch_is_left_off(self):
        modules = {'looker': {}, 'breaker': {}}
        settings = {'modules': {'looker': {}, 'breaker': {'enabled': True}}}
        chosen = installer.selected_steps(self.args(module=None), settings, {}, modules)
        self.assertEqual(chosen, ['breaker'])

    def test_the_state_folder_given_is_made_for_its_owner_alone(self):
        installer.make_state_folder(False, self.state, self.state, self.settings)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)

    def test_a_run_without_apply_writes_its_own_log_and_state_and_leaves_setups_alone(self):
        installer._execute(self.args(apply=False), self.root)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ['inspection-state.json', 'inspection.log'])
        looked = json.loads((self.state / 'inspection-state.json').read_text())
        self.assertEqual(list(looked), ['looker'])
        self.assertEqual(looked['looker']['status'], 'inspected')
        self.assertIn('plan=[\'looker\']', (self.state / 'inspection.log').read_text())

    def test_each_step_is_told_the_plan_the_step_asked_for_and_what_ran_before(self):
        self.state.mkdir()
        (self.state / 'state.json').write_text(json.dumps({'breaker': {'status': 'failed', 'reason': 'x'}}))
        installer._execute(self.args(), self.root)
        self.assertIn(
            "plan=['looker'] only=looker previous=['breaker']", (self.state / installer.INSTALL_LOG).read_text()
        )

    def test_a_step_waiting_for_one_that_failed_says_so_in_the_log_and_the_setup_window(self):
        settings = json.loads(self.config.read_text())
        settings['profiles'] = {**settings['profiles'], 'both': ['breaker', 'after_breaker']}
        self.config.write_text(json.dumps(settings))
        with mock.patch.object(installer.setup_progress, 'step') as window:
            with mock.patch.object(installer, 'retry_now', return_value=False):
                installer._execute(self.args(module=None, profile='both'), self.root)
        log = (self.state / installer.INSTALL_LOG).read_text()
        self.assertRegex(log, r'(?m)\] BLOCKED after_breaker: Waits for \S.* to finish first$')
        self.assertNotIn('the waiting step ran', log)
        said = {call.args[0]: call.args[1:] for call in window.call_args_list if call.args[1] != 'running'}
        self.assertEqual(said['after_breaker'][0], 'blocked')
        self.assertTrue(said['after_breaker'][1].startswith('Waits for '), said)
        self.assertEqual(said['breaker'][0], 'failed')
        self.assertIn('the image could not be pulled', said['breaker'][1])

    def test_a_step_that_finishes_tells_the_setup_window_with_no_reason(self):
        with mock.patch.object(installer.setup_progress, 'step') as window:
            installer._execute(self.args(), self.root)
        self.assertIn(mock.call('looker', 'success', ''), window.call_args_list)

    def test_retrying_one_step_runs_it_and_what_waited_for_it_not_what_it_needs(self):
        """Retry this step, at the end of a run: the step it needs is done already."""
        (self.root / 'modules' / 'needs_looker.py').write_text(
            "DEPENDENCIES = ['looker']\ndef run(ctx):\n    ctx.log('needs_looker ran')\n"
        )
        self.state.mkdir()
        (self.state / 'state.json').write_text(
            json.dumps({'looker': {'status': 'success'}, 'needs_looker': {'status': 'failed', 'reason': 'x'}})
        )
        with mock.patch.object(installer.setup_progress, 'event') as window:
            installer._execute(self.args(module=None, retry_step='needs_looker'), self.root)
        log = (self.state / installer.INSTALL_LOG).read_text()
        self.assertIn('needs_looker ran', log)
        self.assertNotIn('plan=', log)
        # The setup window keeps the plan of the run the retry came from.
        self.assertNotIn('plan', [call.args[0] for call in window.call_args_list])

    def test_retrying_a_step_keeps_the_profile_the_server_was_set_up_with(self):
        """The upgrade sets up the steps of the profile recorded: a retry is no profile run."""
        self.state.mkdir()
        record = self.state / installer.PROFILE_FILE
        record.write_text(json.dumps({'format': installer.PROFILE_FORMAT, 'profile': 'full'}))
        (self.state / 'state.json').write_text(json.dumps({'looker': {'status': 'failed', 'reason': 'x'}}))
        installer._execute(self.args(module=None, retry_step='looker'), self.root)
        self.assertIn('plan=', (self.state / installer.INSTALL_LOG).read_text())
        self.assertEqual(json.loads(record.read_text())['profile'], 'full')

    def test_the_steps_a_newer_release_knows_are_saved_in_the_settings_file(self):
        """With them the next upgrade can tell a step you took out from one that is new."""
        self.settings.pop(config_checks.KNOWN_STEPS, None)
        self.config.write_text(json.dumps(self.settings))
        installer._execute(self.args(), self.root)
        saved = json.loads(self.config.read_text())
        template = json.loads((ROOT / 'config.json').read_text())
        self.assertEqual(saved[config_checks.KNOWN_STEPS], template['profiles'])


if __name__ == '__main__':
    unittest.main()
