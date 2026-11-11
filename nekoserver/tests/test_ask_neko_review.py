"""The review of 0.49.0's Ask Neko (2026-11-11): each finding has a test here that fails
without its fix, or in the runtime's own tests (assist.rs) for the Rust side."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import setup_progress  # noqa: E402
import wizard  # noqa: E402
from modules import assistant  # noqa: E402


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

            def read(file):
                """The steps' records, these settings, and nothing else."""
                if 'state' in str(file):
                    return states
                if file == path:
                    return json.loads(path.read_text())
                return {}

            with (
                mock.patch.object(wizard, 'read_json', side_effect=read),
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


def set_up_with(tmp, *models):
    """An app SSD where the assistant is set up and these models are downloaded."""
    project = Path(tmp) / 'compose/assistant'
    project.mkdir(parents=True)
    (project / assistant.MANIFEST).write_text('services: {}\n')
    for model in models:
        name, tag = assistant.tagged(model).rsplit(':', 1)
        record = Path(tmp) / 'appdata/assistant/models/models/manifests/registry.ollama.ai/library' / name / tag
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text('{}')
    return {'data_mount': tmp}


def free(gb):
    return lambda path: SimpleNamespace(free=gb * 1000**3)


class ModelSpaceTests(unittest.TestCase):
    """Finding 3: with the assistant set up, choosing another model asked only for the
    room kept for everything else, so a 6.6 GB model passed with 11 GB free."""

    def test_another_model_than_the_one_there_counts_its_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = set_up_with(tmp, 'qwen3.5:4b')
            with self.assertRaises(assistant.NotHere) as caught:
                assistant.check_space(config, 'qwen3.5:8b', 6.6, free(11))
            said = str(caught.exception)
            self.assertIn('needs 17 GB free on the app SSD', said)
            self.assertIn('6.6 GB for the model qwen3.5:8b', said)
            self.assertIn('10 GB kept free for everything else', said)
            self.assertIn('there are 11 GB', said)
            self.assertNotIn("Ollama's image", said)  # there already
            assistant.check_space(config, 'qwen3.5:8b', 6.6, free(17))

    def test_the_same_model_again_needs_only_the_room_kept_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = set_up_with(tmp, 'qwen3.5:4b')
            assistant.check_space(config, 'qwen3.5:4b', 3.3, free(11))

    def test_a_first_install_counts_the_image_and_the_model(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(assistant.NotHere) as caught:
            assistant.check_space({'data_mount': tmp}, 'qwen3.5:8b', 6.6, free(20))
        self.assertIn("for Ollama's image, 6.6 GB for the model qwen3.5:8b", str(caught.exception))

    def test_a_name_cannot_point_outside_ollamas_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = set_up_with(tmp, 'qwen3.5:4b')
            (Path(tmp) / 'appdata/assistant/secret').write_text('x')
            self.assertFalse(assistant.downloaded(config, '../../../../../../secret:x'))
            self.assertTrue(assistant.downloaded(config, 'qwen3.5:4b'))


class UntaggedModelTests(unittest.TestCase):
    """Finding 9: "qwen3.5" did not match the cached "qwen3.5:latest", so every setup
    run downloaded the model again."""

    LISTED = 'NAME             ID    SIZE\nqwen3.5:latest   abc   6.6 GB\n'

    def test_a_name_without_a_tag_is_its_latest(self):
        ran = []

        def run(argv, timeout, what):
            ran.append(argv)
            return SimpleNamespace(stdout=self.LISTED)

        self.assertEqual(assistant.fetch_model(['docker'], 'qwen3.5', print, run), ('ready', 'abc'))
        self.assertEqual(assistant.fetch_model(['docker'], 'qwen3.5:latest', print, run), ('ready', 'abc'))
        self.assertEqual(len(ran), 2, 'listed twice, pulled never')
        # Another tag of the same model is another model.
        self.assertEqual(assistant.fetch_model(['docker'], 'qwen3.5:8b', lambda s: None, run)[0], 'downloading')

    def test_the_space_check_knows_it_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = set_up_with(tmp, 'qwen3.5')  # kept by Ollama as qwen3.5:latest
            self.assertTrue(assistant.downloaded(config, 'qwen3.5'))
            self.assertTrue(assistant.downloaded(config, 'qwen3.5:latest'))
            assistant.check_space(config, 'qwen3.5', 6.6, free(11))

    def test_a_port_in_the_address_is_not_a_tag(self):
        self.assertEqual(assistant.tagged('nas.lan:5000/me/model'), 'nas.lan:5000/me/model:latest')
        self.assertEqual(assistant.tagged('me/model:q4'), 'me/model:q4')


if __name__ == '__main__':
    unittest.main()
