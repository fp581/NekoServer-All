"""The review of 0.49.0's Ask Neko (2026-11-11): each finding has a test here that fails
without its fix, or in the runtime's own tests (assist.rs) for the Rust side."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import setup_progress  # noqa: E402
import wizard  # noqa: E402


class OptionalAssistantTests(unittest.TestCase):
    """Finding 2: the assistant's step turns anything that goes wrong into "needs a look",
    which made the installer's exit code non-zero, so the first checkup and backup never
    ran and the screen said "almost ready" on a server where everything else worked."""

    DONE = ('system', 'storage', 'docker')

    def after(self, assistant, others='success'):
        """after_setup with every other step `others` and the assistant's record as given:
        (exit code, the screen's text, how often the first checkup ran)."""
        setup_progress.enable()
        self.addCleanup(setup_progress.disable)
        plan = [*self.DONE, 'assistant']
        setup_progress.event('plan', steps=plan)
        states = {name: {'status': others, 'reason': 'x'} for name in self.DONE}
        states['assistant'] = assistant
        out = []
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'my-config.json'
            path.write_text(json.dumps({'data_mount': d, 'profiles': {'full': plan}}))
            checkup = mock.Mock(return_value=True)
            with (
                mock.patch.object(
                    wizard,
                    'read_json',
                    side_effect=lambda p: (
                        states if 'state' in str(p) else json.loads(path.read_text()) if p == path else {}
                    ),
                ),
                mock.patch.dict(sys.modules, {'first_checkup': mock.Mock(run=checkup)}),
            ):
                # The installer's own code: 1 whenever a step is not "success".
                code = wizard.after_setup(1, path, Path(d), out.append, None)
        return code, '\n'.join(str(line) for line in out), checkup.call_count

    NEEDS_A_LOOK = {'status': 'needs_setup', 'reason': 'The model download was cut off; run setup again to finish it.'}

    def test_an_assistant_that_needs_a_look_does_not_stop_the_checkup_or_the_headline(self):
        code, text, checkups = self.after(self.NEEDS_A_LOOK)
        self.assertEqual(checkups, 1, 'the first checkup and backup run')
        self.assertEqual(code, 0)
        self.assertIn('NekoServer is ready!', text)
        # Still listed, with its reason, under a heading of its own.
        self.assertIn('Needs a look (optional: everything else works without it):', text)
        self.assertIn('The model download was cut off; run setup again to finish it.', text)
        self.assertNotIn('What needs you', text)

    def test_a_failed_assistant_step_is_optional_too(self):
        code, text, checkups = self.after({'status': 'failed', 'reason': 'Docker could not start it'})
        self.assertEqual((code, checkups), (0, 1))
        self.assertIn('Docker could not start it', text)

    def test_another_step_that_did_not_finish_still_counts(self):
        code, text, checkups = self.after(self.NEEDS_A_LOOK, others='failed')
        self.assertEqual((code, checkups), (1, 0))
        self.assertNotIn('NekoServer is ready!', text)
        self.assertIn('What needs you', text)

    def test_everything_done_is_as_before(self):
        code, text, checkups = self.after({'status': 'success'})
        self.assertEqual((code, checkups), (1, 0), 'the installer said 1 for a reason of its own: no checkup')
        self.assertNotIn('Needs a look (optional', text)


if __name__ == '__main__':
    unittest.main()
