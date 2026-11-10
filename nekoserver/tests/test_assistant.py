"""The optional AI assistant's setup step (modules/assistant.py, ASSISTANT.md): asked once,
off unless chosen, a choice of the models that fit this PC with what each one costs it,
checks before anything is downloaded, and never in the way of anything else: its step
cannot fail setup, an upgrade never goes back because of it, and Ollama runs on this PC
only within hard limits."""

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import explanations  # noqa: E402
import neko_common  # noqa: E402
import upgrade  # noqa: E402


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assistant = load('nekoserver_assistant', 'modules/assistant.py')
installer = load('nekoserver_installer_for_assistant', 'installer.py')


def context(config, apply=True):
    said = []
    return SimpleNamespace(config=config, apply=apply, log=said.append, said=said, persist_config=lambda: None)


class Answer(io.BytesIO):
    """A urlopen answer: a readable body that works in `with`."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def installed_project(tmp):
    (Path(tmp) / 'compose/assistant').mkdir(parents=True)
    (Path(tmp) / 'compose/assistant/compose.yaml').write_text('services: {}\n')


class NeverFailsSetupTests(unittest.TestCase):
    """Whatever goes wrong in the assistant's step is said and left for later."""

    def test_any_error_becomes_a_step_that_waits_for_you(self):
        with mock.patch.object(assistant, '_run', side_effect=RuntimeError('the download was cut off')):
            result = assistant.run(context({}))
        self.assertEqual(result['status'], 'needs_setup')
        self.assertIn('the download was cut off', result['reason'])
        self.assertIn('Everything else works without it', result['reason'])

    def test_an_unexpected_error_does_not_escape_either(self):
        with mock.patch.object(assistant, '_run', side_effect=KeyError('services')):
            self.assertEqual(assistant.run(context({}))['status'], 'needs_setup')

    def test_a_pc_that_cannot_have_it_is_not_available_rather_than_a_problem(self):
        with mock.patch.object(assistant, '_run', side_effect=assistant.NotHere('the processor is armv7l')):
            result = assistant.run(context({}))
        self.assertEqual(result, {'status': 'unavailable', 'reason': 'the processor is armv7l'})

    def test_ctrl_c_still_stops_setup(self):
        ctx = context({})
        with mock.patch.object(assistant, '_run', side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            assistant.run(ctx)


class Ready:
    """The step with every check passing and nothing installed for real."""

    def __init__(self, test, memory=(16, 11), interactive=True):
        self.patches = [
            mock.patch.object(assistant.guided_setup, 'interactive', return_value=interactive),
            mock.patch.object(assistant.os, 'geteuid', return_value=0),
            mock.patch.object(assistant, 'verify_storage'),
            mock.patch.object(assistant, 'processor_ok'),
            mock.patch.object(assistant, 'port_free_for_us'),
            mock.patch.object(assistant, 'memory_now', return_value=memory),
            mock.patch.object(assistant, 'own_use_gib', return_value=0),
            mock.patch.object(assistant, 'check_space'),
            mock.patch.object(assistant, 'need_pin'),
            mock.patch.object(assistant, 'stop'),
            mock.patch.object(assistant, 'install'),
        ]
        self.test = test

    def __enter__(self):
        mocks = [p.start() for p in self.patches]
        self.test.addCleanup(mock.patch.stopall)
        self.stop, self.install = mocks[-2], mocks[-1]
        return self

    def __exit__(self, *exc):
        mock.patch.stopall()
        return False


class OffByDefaultTests(unittest.TestCase):
    def test_nobody_at_the_terminal_installs_nothing(self):
        ctx = context({})
        with Ready(self, interactive=False) as ready:
            self.assertIsNone(assistant.run(ctx))
        ready.install.assert_not_called()
        self.assertTrue(any('not asked' in line for line in ctx.said))

    def test_enter_at_the_choice_is_no_assistant(self):
        ctx = context({})
        with Ready(self) as ready, mock.patch('builtins.input', return_value=''), mock.patch('builtins.print'):
            self.assertIsNone(assistant.run(ctx))
        ready.install.assert_not_called()
        self.assertEqual(ctx.config['assistant'], {'wanted': False, 'model': 'auto'})

    def test_no_saved_stops_it_and_installs_nothing(self):
        ctx = context({'assistant': {'wanted': False}})
        with Ready(self) as ready:
            self.assertIsNone(assistant.run(ctx))
        ready.stop.assert_called_once()
        ready.install.assert_not_called()

    def test_a_model_chosen_is_saved_and_installed_with_its_limit(self):
        ctx = context({})
        with Ready(self) as ready, mock.patch('builtins.input', return_value='2'), mock.patch('builtins.print'):
            self.assertIsNone(assistant.run(ctx))
        self.assertEqual(ctx.config['assistant'], {'wanted': True, 'model': 'qwen3.5:4b'})
        ready.install.assert_called_once_with(ctx, 'qwen3.5:4b', '5g')

    def test_a_choice_made_before_is_kept_without_asking_even_unattended(self):
        ctx = context({'assistant': {'wanted': True, 'model': 'qwen3.5:2b-q4_K_M'}})
        with Ready(self, interactive=False) as ready, mock.patch('builtins.input', side_effect=AssertionError):
            self.assertIsNone(assistant.run(ctx))
        ready.install.assert_called_once_with(ctx, 'qwen3.5:2b-q4_K_M', '3g')

    def test_a_read_only_plan_asks_nothing(self):
        with mock.patch('builtins.input', side_effect=AssertionError):
            self.assertIsNone(assistant.run(context({}, apply=False)))

    def test_ctrl_d_at_the_choice_asks_again_next_time(self):
        ctx = context({})
        with Ready(self) as ready, mock.patch('builtins.input', side_effect=EOFError), mock.patch('builtins.print'):
            self.assertIsNone(assistant.run(ctx))
        self.assertNotIn('assistant', ctx.config)
        ready.install.assert_not_called()

    def test_the_setup_window_switch_makes_enter_the_recommended_model(self):
        ctx = context({'assistant': {'wanted': True, 'model': 'auto'}})
        with Ready(self) as ready, mock.patch('builtins.input', return_value=''), mock.patch('builtins.print'):
            assistant.run(ctx)
        ready.install.assert_called_once_with(ctx, 'qwen3.5:4b', '5g')

    def test_the_window_offers_it_switched_off(self):
        extra = next(e for e in explanations.EXTRAS if e['id'] == 'assistant')
        self.assertFalse(extra['on_default'])
        self.assertEqual((extra['section'], extra['key']), ('assistant', 'wanted'))

    def test_the_template_settings_decide_nothing(self):
        settings = json.loads((ROOT / 'config.json').read_text())
        self.assertEqual(settings['assistant'], {'model': 'auto'})


class PinTests(unittest.TestCase):
    """Asking Neko needs the dashboard PIN; without one no model is downloaded."""

    def step(self, tmp, pin=False, binary=True, interactive=True):
        pin_file = Path(tmp) / 'pin.json'
        if pin:
            pin_file.write_text('{}')
        program = Path(tmp) / 'nekoserver-runtime'
        if binary:
            program.write_text('')
        dashboard = SimpleNamespace(runtime_binary=lambda: program, choose_pin=mock.Mock(return_value=False))
        for p in (
            mock.patch.object(assistant, 'PIN_FILE', pin_file),
            mock.patch.object(assistant, 'dashboard_step', return_value=dashboard),
            mock.patch.object(assistant.guided_setup, 'interactive', return_value=interactive),
            mock.patch('builtins.print'),
        ):
            p.start()
        self.addCleanup(mock.patch.stopall)
        return dashboard

    def test_a_pin_chosen_before_is_enough(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = self.step(tmp, pin=True)
            assistant.need_pin(context({}))
            dashboard.choose_pin.assert_not_called()

    def test_without_a_pin_it_is_asked_and_skipping_it_installs_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = self.step(tmp)
            ctx = context({})
            with self.assertRaises(assistant.NotHere) as said:
                assistant.need_pin(ctx)
            dashboard.choose_pin.assert_called_once()
            self.assertIn('needs a dashboard PIN', str(said.exception))
            dashboard.choose_pin.return_value = True
            assistant.need_pin(context({}))  # a PIN chosen there: on with the install

    def test_unattended_or_without_the_dashboard_it_is_said(self):
        with tempfile.TemporaryDirectory() as tmp:
            dashboard = self.step(tmp, interactive=False)
            ctx = context({})
            with self.assertRaisesRegex(assistant.NotHere, 'needs a dashboard PIN'):
                assistant.need_pin(ctx)
            dashboard.choose_pin.assert_not_called()
        mock.patch.stopall()
        with tempfile.TemporaryDirectory() as tmp:
            self.step(tmp, binary=False)
            ctx = context({})
            with self.assertRaisesRegex(assistant.NotHere, 'set up the dashboard first'):
                assistant.need_pin(ctx)

    def test_no_pin_means_no_download_and_setup_goes_on(self):
        ctx = context({'assistant': {'wanted': True, 'model': 'qwen3.5:4b'}})
        with Ready(self, interactive=False) as ready:
            assistant.need_pin.side_effect = assistant.NotHere(assistant.NO_PIN)
            answer = assistant.run(ctx)
        ready.install.assert_not_called()
        ready.stop.assert_called_once()
        self.assertEqual(answer['status'], 'unavailable')
        self.assertIn('dashboard PIN', answer['reason'])

    def test_the_dashboard_is_told_once_it_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'config.json'
            assistant.tell_dashboard(settings)  # no dashboard: nothing written
            self.assertFalse(settings.exists())
            settings.write_text(json.dumps({'interval': 60, 'assistant': {'wanted': False}}))
            assistant.tell_dashboard(settings)
            self.assertEqual(json.loads(settings.read_text()), {'interval': 60, 'assistant': {'wanted': True}})


class ChoiceTests(unittest.TestCase):
    """Each choice says what it costs this PC; the biggest that fits is recommended."""

    def test_only_the_models_that_fit_are_offered(self):
        lines, answers = assistant.choices(16, 6.3, 8)
        text = '\n'.join(lines)
        self.assertEqual(answers, {'0': 'none', '': 'none', '1': 'qwen3.5:2b-q4_K_M', '2': 'qwen3.5:4b'})
        self.assertIn('2) qwen3.5:4b (recommended)', text)
        self.assertNotIn('1) qwen3.5:2b-q4_K_M (recommended)', text)
        self.assertIn('Not possible here: qwen3.5:9b needs about 9 GB free; this PC has 6.3 GB free', text)

    def test_each_choice_says_what_it_costs_this_pc(self):
        text = '\n'.join(assistant.choices(16, 6.3, 8)[0])
        self.assertIn('This PC has 16 GB of memory', text)
        self.assertIn('6.3 GB are free for the assistant', text)
        self.assertIn('Downloads 3.3 GB', text)
        self.assertIn('about 5 GB of your 16 GB', text)
        self.assertIn('up to 4 of your 8 processor cores', text)
        self.assertIn('Jellyfin', text)
        self.assertIn('5 minutes after the last question its memory is free again', text)
        self.assertIn('good everyday answers', text)

    def test_a_big_pc_gets_all_three(self):
        _, answers = assistant.choices(32, 18, 16)
        self.assertEqual(answers['3'], 'qwen3.5:9b')
        self.assertIn('3) qwen3.5:9b (recommended)', '\n'.join(assistant.choices(32, 18, 16)[0]))

    def test_no_room_for_any_is_not_available(self):
        ctx, typed, quiet = context({}), (lambda q: '1'), (lambda s: None)
        with self.assertRaises(assistant.NotHere) as caught:
            assistant.ask_model(ctx, 6, 1.2, 4, prompt=typed, output=quiet)
        self.assertIn('not enough free memory', str(caught.exception))

    def test_a_wrong_answer_is_asked_again(self):
        answers = iter(['7', 'two', '1'])
        said = []
        picked = assistant.ask_model(context({}), 16, 6.3, 8, prompt=lambda q: next(answers), output=said.append)
        self.assertEqual(picked, 'qwen3.5:2b-q4_K_M')
        self.assertEqual(said.count('Type a number from 0 to 2, or press Enter.'), 2)

    def test_the_memory_limit_is_the_models_measured_need(self):
        self.assertEqual(assistant.fitting({'assistant': {'model': 'qwen3.5:9b'}}, 9.5), ('qwen3.5:9b', 6.6, '9g'))

    def test_a_chosen_model_that_no_longer_fits_is_said(self):
        with self.assertRaises(assistant.NotHere) as caught:
            assistant.fitting({'assistant': {'model': 'qwen3.5:9b'}}, 6)
        self.assertIn('qwen3.5:9b needs about 9 GB free', str(caught.exception))

    def test_a_model_named_by_hand_gets_the_room_left(self):
        self.assertEqual(assistant.fitting({'assistant': {'model': 'gemma4:12b'}}, 10.4), ('gemma4:12b', 7, '10g'))
        with self.assertRaises(assistant.NotHere):
            assistant.fitting({'assistant': {'model': 'gemma4:12b'}}, 0.5)

    def test_no_room_any_more_stops_a_running_assistant(self):
        ctx = context({'assistant': {'wanted': True, 'model': 'qwen3.5:9b'}})
        with Ready(self, memory=(16, 8)) as ready:
            result = assistant.run(ctx)
        self.assertEqual(result['status'], 'unavailable')
        ready.stop.assert_called_once()
        ready.install.assert_not_called()

    def test_a_model_name_that_is_not_a_name_is_refused(self):
        self.assertEqual(assistant.options({'assistant': {'model': 'qwen3.5:2b-q4_K_M'}})['model'], 'qwen3.5:2b-q4_K_M')
        for bad in ('qwen3.5:4b; rm -rf /', '../models', 'Qwen3.5', '', 42):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                assistant.options({'assistant': {'model': bad}})


class CapabilityTests(unittest.TestCase):
    """Checked before anything is downloaded, from kernel files and Python alone."""

    def cpuinfo(self, flags):
        f = tempfile.NamedTemporaryFile('w', suffix='cpuinfo', delete=False)
        f.write(f'processor\t: 0\nflags\t\t: {flags}\n')
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return f.name

    def test_64_bit_intel_amd_and_arm_are_fine(self):
        good = self.cpuinfo('fpu sse4_1 sse4_2 avx avx2')
        self.assertEqual(assistant.processor_ok(lambda: 'x86_64', good), 'x86_64')
        self.assertEqual(assistant.processor_ok(lambda: 'aarch64', '/nonexistent'), 'aarch64')

    def test_other_processors_are_not_available(self):
        with self.assertRaises(assistant.NotHere) as caught:
            assistant.processor_ok(lambda: 'armv7l')
        self.assertIn('this one is armv7l', str(caught.exception))
        old = self.cpuinfo('fpu sse4_1')
        with self.assertRaises(assistant.NotHere) as caught:
            assistant.processor_ok(lambda: 'x86_64', old)
        self.assertIn('SSE4.2', str(caught.exception))

    def test_a_program_of_the_persons_own_on_the_port_keeps_it(self):
        def answering(address, timeout):
            return Answer(b'')

        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(assistant.NotHere) as caught:
            assistant.port_free_for_us({'data_mount': tmp}, connect=answering)
        self.assertIn('an Ollama of your own', str(caught.exception))

    def test_a_free_port_or_our_own_is_fine(self):
        def refused(address, timeout):
            raise ConnectionRefusedError

        with tempfile.TemporaryDirectory() as tmp:
            assistant.port_free_for_us({'data_mount': tmp}, connect=refused)
            installed_project(tmp)
            assistant.port_free_for_us({'data_mount': tmp}, connect=lambda *a, **k: self.fail('ours'))

    def test_the_checks_run_no_program_so_no_checking_tool_can_be_missing(self):
        """The rule for capability checks: a checking tool that is missing is installed,
        never skipped. These checks use kernel files and Python only, so there is none."""
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch('subprocess.run', side_effect=AssertionError('a program was run')),
            mock.patch('subprocess.Popen', side_effect=AssertionError('a program was run')),
        ):
            assistant.processor_ok(lambda: 'x86_64', self.cpuinfo('sse4_2'))
            assistant.port_free_for_us({'data_mount': tmp}, connect=mock.Mock(side_effect=OSError))
            assistant.memory_now()
            assistant.check_space({'data_mount': tmp}, 3.3, lambda p: SimpleNamespace(free=10**12))


class MemoryTests(unittest.TestCase):
    """Only what is free with every other app running, less a reserve for them."""

    def test_total_and_free_memory_are_read_from_meminfo(self):
        with tempfile.NamedTemporaryFile('w', suffix='meminfo') as f:
            f.write('MemTotal:       16303740 kB\nMemFree:  1 kB\nMemAvailable:   10485760 kB\n')
            f.flush()
            total, available = assistant.memory_now(f.name)
        self.assertAlmostEqual(total, 15.55, places=1)
        self.assertEqual(available, 10)
        self.assertEqual(assistant.memory_now('/nonexistent/meminfo'), (0, 0))

    def test_the_trial_vm_has_no_room(self):
        """The 0.49.0 trial: 6 GB with the other apps using 4.5 of it. Limits worked out
        from the total memory added up to more than was free, and the server stalled
        while the model loaded. Now there is no room, so no assistant."""
        room = assistant.memory_for_model(5.8, 1.5)
        self.assertEqual(assistant.fits(room), [])
        with self.assertRaises(assistant.NotHere):
            assistant.fitting({'assistant': {'model': 'qwen3.5:0.8b'}}, room)

    def test_the_reserve_is_twenty_percent_or_two_gb(self):
        self.assertAlmostEqual(assistant.memory_for_model(16, 11), 11 - 3.2)
        self.assertAlmostEqual(assistant.memory_for_model(8, 7), 7 - 2)

    def test_setup_run_again_counts_the_assistants_own_use_as_free(self):
        self.assertAlmostEqual(assistant.memory_for_model(16, 5, own=6), assistant.memory_for_model(16, 11))

    def test_its_own_use_is_read_from_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'data_mount': tmp}
            self.assertEqual(assistant.own_use_gib(config, run=lambda *a: self.fail('not running')), 0)
            installed_project(tmp)
            answers = iter(['abc\n', '1.5GiB / 5GiB\n'])
            self.assertAlmostEqual(
                assistant.own_use_gib(config, run=lambda *a: SimpleNamespace(stdout=next(answers))), 1.5
            )

            def broken(*args):
                raise RuntimeError('docker is not running')

            self.assertEqual(assistant.own_use_gib(config, run=broken), 0)

    def test_docker_sizes(self):
        self.assertAlmostEqual(assistant.size_gib('512MiB '), 0.5)
        self.assertAlmostEqual(assistant.size_gib('2GiB'), 2)
        self.assertEqual(assistant.size_gib('--'), 0)


class SpaceTests(unittest.TestCase):
    def test_too_little_ssd_space_is_not_available(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(assistant.NotHere) as caught:
            assistant.check_space({'data_mount': tmp}, 3.3, lambda p: SimpleNamespace(free=12 * 1000**3))
        self.assertIn('needs 19 GB free', str(caught.exception))

    def test_setup_run_again_does_not_count_what_is_there_already(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed_project(tmp)
            assistant.check_space({'data_mount': tmp}, 3.3, lambda p: SimpleNamespace(free=12 * 1000**3))
            with self.assertRaises(assistant.NotHere):
                assistant.check_space({'data_mount': tmp}, 3.3, lambda p: SimpleNamespace(free=8 * 1000**3))


class TemplateTests(unittest.TestCase):
    VALUES = {'DATA': '/srv/nekoserver', 'CPUS': '2', 'MODEL_MEMORY': '5g'}

    def rendered(self):
        return assistant.render((ROOT / 'compose/assistant.yaml').read_text(), self.VALUES)

    def test_every_value_is_filled_in(self):
        text = self.rendered()
        self.assertNotIn('${NEKO_', text)
        self.assertIn('cpus: "2"', text)
        self.assertIn('"/srv/nekoserver/appdata/assistant/models:/root/.ollama"', text)

    def test_a_value_missing_is_refused(self):
        with self.assertRaises(RuntimeError):
            assistant.render('cpus: "${NEKO_CPUS:-2}"', {})

    def test_ollama_alone_on_this_pc_only(self):
        text = self.rendered()
        self.assertEqual(text.count('image:'), 1)
        self.assertNotIn('open-webui', text)
        self.assertIn('"127.0.0.1:11434:11434"', text)
        self.assertEqual(text.count('11434:'), 1)
        self.assertNotIn('nekoserver-media', text)
        self.assertNotIn('networks:', text)

    def test_a_hard_memory_limit_and_the_lowest_priority(self):
        text = self.rendered()
        self.assertIn('mem_limit: "5g"', text)
        self.assertIn('memswap_limit: "5g"', text)  # no swap: past its limit it is stopped
        self.assertIn('oom_score_adj: 1000', text)
        self.assertIn('cpu_shares: 256', text)
        self.assertIn('OLLAMA_NUM_PARALLEL: "1"', text)
        self.assertIn('OLLAMA_MAX_LOADED_MODELS: "1"', text)
        self.assertIn(f'OLLAMA_KEEP_ALIVE: "{assistant.IDLE_MINUTES}m"', text)

    def test_the_image_is_pinned(self):
        text = (ROOT / 'compose/assistant.yaml').read_text()
        self.assertIn('image: ollama/ollama:0.40.16', text)
        self.assertNotIn(':latest', text)

    def test_the_port_is_the_shared_one(self):
        self.assertEqual(neko_common.OLLAMA_PORT, 11434)
        self.assertNotIn(11434, neko_common.APP_PORTS)


class InstallTests(unittest.TestCase):
    def test_its_folders_are_made_only_on_the_ssd(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            config = {'data_mount': tmp}
            inside = Path(tmp) / 'compose' / 'assistant'
            self.assertEqual(assistant.on_the_ssd(config, inside), inside)
            # A folder that leads out of the SSD through a link is refused.
            (Path(tmp) / 'appdata').symlink_to(elsewhere)
            led_out = Path(tmp) / 'appdata' / 'assistant' / 'models'
            with self.assertRaisesRegex(RuntimeError, 'outside the app SSD'):
                assistant.on_the_ssd(config, led_out)
            # The SSD's own path is checked where it comes in.
            with self.assertRaisesRegex(RuntimeError, 'Parent traversal|Unsafe mount path'):
                assistant.folders({'data_mount': '/srv/../etc'})

    def test_only_a_model_from_the_table_is_saved_as_the_choice(self):
        ctx = context({})
        assistant.save_choice(ctx, True, 'qwen3.5:4b')
        self.assertEqual(ctx.config['assistant'], {'wanted': True, 'model': 'qwen3.5:4b'})
        assistant.save_choice(ctx, True, 'something; else')
        self.assertEqual(ctx.config['assistant'], {'wanted': True, 'model': 'auto'})

    def test_install_writes_a_private_file_starts_it_and_reports_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'data_mount': tmp}
            ctx = context(config)
            started = []
            with (
                mock.patch.object(assistant.os, 'cpu_count', return_value=8),
                mock.patch.object(assistant, 'compose_up', side_effect=lambda base, what, log: started.append(base)),
                mock.patch.object(assistant, 'wait_until_answering'),
                mock.patch.object(assistant, 'fetch_model', return_value=('downloading', None)),
                mock.patch.object(assistant, 'tell_dashboard') as told,
            ):
                assistant.install(ctx, 'qwen3.5:4b', '5g')
            told.assert_called_once_with()
            manifest = Path(tmp) / 'compose/assistant/compose.yaml'
            self.assertEqual(oct(manifest.stat().st_mode & 0o777), '0o600')
            self.assertIn('cpus: "4"', manifest.read_text())
            self.assertIn('mem_limit: "5g"', manifest.read_text())
            self.assertEqual(started[0][-2:], ['-p', 'neko-assistant'])
            report = json.loads((Path(tmp) / 'reports/assistant.json').read_text())
            self.assertEqual(
                report,
                {
                    'format': 'neko-assistant-v1',
                    'model': 'qwen3.5:4b',
                    'model_id': None,
                    'cpus': '4',
                    'memory': '5g',
                    'model_ready': False,
                },
            )
            self.assertTrue(neko_common.assistant_installed({**config, 'assistant': {'wanted': True}}))
            self.assertTrue(any('Ask Neko' in line for line in ctx.said))

    def test_stopping_keeps_its_model_and_says_it_is_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed_project(tmp)
            models = Path(tmp) / 'appdata/assistant/models'
            models.mkdir(parents=True)
            ran = []
            assistant.stop({'data_mount': tmp}, print, run=lambda argv, *a: ran.append(argv))
            self.assertEqual(ran[0][-1], 'down')
            self.assertNotIn('-v', ran[0])
            self.assertTrue(models.is_dir())
            self.assertFalse(neko_common.assistant_installed({'data_mount': tmp, 'assistant': {'wanted': True}}))
            report = json.loads((Path(tmp) / 'reports/assistant.json').read_text())
            self.assertIsNone(report['model'])

    def test_stopping_an_assistant_never_installed_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ran = []
            assistant.stop({'data_mount': tmp}, print, run=lambda *a: ran.append(a))
            self.assertEqual(ran, [])

    def test_it_waits_for_ollama(self):
        answers = iter([OSError('not yet'), Answer(b'{}'), Answer(b'{"version": "0.40.16"}')])

        def opener(url, timeout):
            self.assertTrue(url.startswith('http://127.0.0.1:11434/'))
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        slept = []
        assistant.wait_until_answering(opener, slept.append, clock=lambda: 0)
        self.assertEqual(slept, [5, 5])

    def test_ollama_that_never_answers_is_said(self):
        clock = iter([0, 100, 200])
        with self.assertRaises(RuntimeError) as caught:
            assistant.wait_until_answering(mock.Mock(side_effect=OSError), lambda s: None, clock=lambda: next(clock))
        self.assertIn('did not answer within 2 minutes', str(caught.exception))

    def test_a_model_already_there_is_not_downloaded_again(self):
        ran = []

        def run(argv, timeout, what):
            ran.append(argv)
            return SimpleNamespace(stdout='NAME           ID   SIZE\nqwen3.5:4b     abc  3.3 GB\n')

        # Its ID (the start of its digest) goes into the report: which build was downloaded.
        self.assertEqual(assistant.fetch_model(['docker'], 'qwen3.5:4b', print, run), ('ready', 'abc'))
        self.assertEqual(len(ran), 1)

    def test_a_missing_model_downloads_in_the_background(self):
        ran, said = [], []

        def run(argv, timeout, what):
            ran.append(argv)
            return SimpleNamespace(stdout='NAME   ID   SIZE\n')

        self.assertEqual(assistant.fetch_model(['docker'], 'qwen3.5:4b', said.append, run), ('downloading', None))
        self.assertTrue(any('from the Ollama library' in line for line in said))
        self.assertEqual(ran[1][1:], ['exec', '-d', '-T', 'ollama', 'ollama', 'pull', 'qwen3.5:4b'])


class OthersComeFirstTests(unittest.TestCase):
    """NekoServer works the same without it: nothing needs it, an upgrade never goes back
    because of it."""

    def test_an_upgrade_never_goes_back_because_of_the_assistant(self):
        with mock.patch.object(upgrade, 'steps_done', return_value={'media', 'dashboard'}):
            reason = upgrade.works_on({}, 'v0.49.0', {'media', 'dashboard', 'assistant'}, lambda v: v, lambda v: [])
        self.assertIsNone(reason)

    def test_an_upgrade_still_goes_back_for_any_other_step(self):
        with mock.patch.object(upgrade, 'steps_done', return_value={'dashboard'}):
            reason = upgrade.works_on({}, 'v0.49.0', {'media', 'dashboard', 'assistant'}, lambda v: v, lambda v: [])
        self.assertEqual(reason, 'these steps worked before and do not now: media')

    def test_no_other_step_needs_it(self):
        modules = installer.load_modules(ROOT / 'modules')
        self.assertEqual(modules['assistant']['deps'], ['docker'])
        self.assertEqual([name for name, m in modules.items() if 'assistant' in m['deps']], [])

    def test_it_is_not_an_app_with_a_page_of_its_own(self):
        self.assertNotIn('assistant', neko_common.APPS)

    def test_no_unit_or_job_names_it(self):
        """Backups, updates and recovery never call it: nothing they write or run mentions it."""
        for path in [*ROOT.glob('modules/*.py'), ROOT / 'recovery_wizard.py', ROOT / 'upgrade.py']:
            if path.name in ('assistant.py', 'dashboard.py'):
                continue
            with self.subTest(path=path.name):
                self.assertNotIn('neko-assistant', path.read_text())
                self.assertNotIn('11434', path.read_text())

    def test_it_comes_in_the_full_setup_after_the_dashboard_and_its_pin(self):
        full = json.loads((ROOT / 'config.json').read_text())['profiles']['full']
        self.assertIn('assistant', full)
        self.assertLess(full.index('dashboard'), full.index('assistant'))
        self.assertTrue(callable(assistant.dashboard_step().runtime_binary))


class InstalledTests(unittest.TestCase):
    def test_wanted_and_set_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'data_mount': tmp, 'assistant': {'wanted': True}}
            self.assertFalse(neko_common.assistant_installed(config))
            installed_project(tmp)
            self.assertTrue(neko_common.assistant_installed(config))
            for other in ({'wanted': False}, {}, [], None):
                with self.subTest(other=other):
                    self.assertFalse(neko_common.assistant_installed({'data_mount': tmp, 'assistant': other}))


class ModelIsDownloadedTests(unittest.TestCase):
    """The user's rule: the model is downloaded during setup, never part of NekoServer."""

    def test_the_repository_holds_no_model(self):
        # Skipped only where git is not installed (a copy of the files without it); with git,
        # a failure to list the files fails the test, so CI can never pass it unchecked.
        if shutil.which('git') is None or not (ROOT.parent / '.git').exists():
            self.skipTest('no git checkout here')
        listed = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT.parent, capture_output=True, check=True, timeout=60)
        tracked = [name for name in listed.stdout.decode().split('\0') if name]
        self.assertGreater(len(tracked), 100, 'git listed too few files to be this repository')
        for name in tracked:
            path = ROOT.parent / name
            with self.subTest(name=name):
                self.assertFalse(name.endswith(('.gguf', '.safetensors', '.bin', '.onnx')), name)
                self.assertNotIn('models/', name)
                self.assertNotIn('manifests/registry.ollama.ai', name)
                self.assertFalse(re.search(r'(^|/)sha256-[0-9a-f]{16,}', name), name)
                if path.is_file():
                    self.assertLess(path.stat().st_size, 5 * 1024 * 1024, name)

    def test_the_kept_copy_of_nekoserver_never_takes_the_models(self):
        recovery = load('nekoserver_recovery_for_assistant', 'modules/recovery.py')
        for path in recovery.bundle_files(ROOT):
            with self.subTest(path=str(path)):
                relative = path.relative_to(ROOT).as_posix()
                self.assertFalse(relative.startswith('appdata'), relative)
                self.assertNotIn('models', relative.split('/')[:-1])

    def test_the_setup_choice_says_it_is_downloaded(self):
        text = ' '.join(assistant.EXPLAIN)
        self.assertIn('downloaded from the Ollama library', text)
        self.assertIn('needs the internet', text)


if __name__ == '__main__':
    unittest.main()
