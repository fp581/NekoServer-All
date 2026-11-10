"""The optional AI assistant (modules/assistant.py, ASSISTANT.md): asked once, never a must,
and never in the way of anything else: its step cannot fail setup, an upgrade never goes
back because of it, the gateway publishes it only once it works, and it runs within
limits of its own."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import account_setup  # noqa: E402
import gateway_tools  # noqa: E402
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
    return SimpleNamespace(config=config, apply=apply, log=said.append, said=said)


class Answer(io.BytesIO):
    """A urlopen answer: a readable body that works in `with`."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NeverFailsSetupTests(unittest.TestCase):
    """Whatever goes wrong in the assistant's step is said and left for later."""

    def test_any_error_becomes_a_step_that_waits_for_you(self):
        ctx = context({})
        with mock.patch.object(assistant, '_run', side_effect=RuntimeError('the download was cut off')):
            result = assistant.run(ctx)
        self.assertEqual(result['status'], 'needs_setup')
        self.assertIn('the download was cut off', result['reason'])
        self.assertIn('Everything else works without it', result['reason'])

    def test_an_unexpected_error_does_not_escape_either(self):
        ctx = context({})
        with mock.patch.object(assistant, '_run', side_effect=KeyError('services')):
            self.assertEqual(assistant.run(ctx)['status'], 'needs_setup')

    def test_too_small_a_pc_is_not_available_rather_than_a_problem(self):
        ctx = context({})
        with mock.patch.object(assistant, '_run', side_effect=assistant.NotHere('it needs 12 GB')):
            result = assistant.run(ctx)
        self.assertEqual(result, {'status': 'unavailable', 'reason': 'it needs 12 GB'})

    def test_ctrl_c_still_stops_setup(self):
        with mock.patch.object(assistant, '_run', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                assistant.run(context({}))


class AskedOnceTests(unittest.TestCase):
    def test_nobody_at_the_terminal_installs_nothing_and_waits_for_no_one(self):
        ctx = context({})
        with (
            mock.patch.object(assistant.guided_setup, 'wanted', return_value=None),
            mock.patch.object(assistant, 'install') as install,
        ):
            self.assertIsNone(assistant.run(ctx))
        install.assert_not_called()
        self.assertTrue(any('not asked' in line for line in ctx.said))

    def test_no_stops_it_and_installs_nothing(self):
        ctx = context({})
        with (
            mock.patch.object(assistant.guided_setup, 'wanted', return_value=False),
            mock.patch.object(assistant, 'stop') as stop,
            mock.patch.object(assistant, 'install') as install,
        ):
            self.assertIsNone(assistant.run(ctx))
        stop.assert_called_once()
        install.assert_not_called()

    def test_a_read_only_plan_asks_nothing(self):
        with mock.patch.object(assistant.guided_setup, 'wanted') as wanted:
            self.assertIsNone(assistant.run(context({}, apply=False)))
        wanted.assert_not_called()

    def test_the_question_says_it_is_optional_and_harmless(self):
        text = ' '.join(assistant.EXPLAIN)
        self.assertIn('skip', text)
        self.assertIn('never slows down or stops your other apps', text)

    def test_stopping_keeps_its_chats_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'data_mount': tmp}
            project = Path(tmp) / 'compose/assistant'
            project.mkdir(parents=True)
            (project / 'compose.yaml').write_text('services: {}\n')
            chats = Path(tmp) / 'appdata/assistant/chat'
            chats.mkdir(parents=True)
            ran, said = [], []
            assistant.stop(config, said.append, run=lambda argv, *a: ran.append(argv))
            self.assertEqual(ran[0][-1], 'down')
            self.assertNotIn('-v', ran[0])
            self.assertFalse((project / 'compose.yaml').exists())
            self.assertTrue((project / 'compose.yaml.stopped').exists())
            self.assertTrue(chats.is_dir())
            # Off: the dashboard tile and the gateway address go with it.
            self.assertFalse(neko_common.assistant_installed({'data_mount': tmp, 'assistant': {'wanted': True}}))

    def test_stopping_an_assistant_never_installed_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ran = []
            assistant.stop({'data_mount': tmp}, print, run=lambda *a: ran.append(a))
            self.assertEqual(ran, [])


class ModelTests(unittest.TestCase):
    def test_the_model_fits_the_memory(self):
        self.assertEqual(assistant.chosen_model({}, 32), ('qwen3.5:9b', 6.6))
        self.assertEqual(assistant.chosen_model({}, 24), ('qwen3.5:9b', 6.6))
        self.assertEqual(assistant.chosen_model({}, 16), ('qwen3.5:4b', 3.3))
        self.assertEqual(assistant.chosen_model({}, 12), ('qwen3.5:4b', 3.3))

    def test_too_little_memory_says_how_much_and_how_to_try_anyway(self):
        with self.assertRaises(assistant.NotHere) as caught:
            assistant.chosen_model({}, 6.2)
        self.assertIn('at least 12 GB', str(caught.exception))
        self.assertIn('this PC has 6 GB', str(caught.exception))
        self.assertIn('"model" under "assistant"', str(caught.exception))

    def test_a_model_named_by_hand_is_used_as_named(self):
        config = {'assistant': {'model': 'qwen3.5:2b'}}
        self.assertEqual(assistant.chosen_model(config, 6), ('qwen3.5:2b', assistant.UNKNOWN_MODEL_GB))

    def test_a_model_name_that_is_not_a_name_is_refused(self):
        for bad in ('qwen3.5:4b; rm -rf /', '../models', 'Qwen3.5', '', 42):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                assistant.options({'assistant': {'model': bad}})

    def test_settings_of_the_wrong_shape_are_refused(self):
        for bad in ({'assistant': []}, {'assistant': {'wanted': 'yes'}}):
            with self.subTest(bad=bad), self.assertRaises(RuntimeError):
                assistant.options(bad)

    def test_memory_is_read_from_meminfo(self):
        with tempfile.NamedTemporaryFile('w', suffix='meminfo') as f:
            f.write('MemTotal:       16303740 kB\nMemFree:  1 kB\n')
            f.flush()
            self.assertAlmostEqual(assistant.memory_gib(f.name), 15.55, places=1)
        self.assertEqual(assistant.memory_gib('/nonexistent/meminfo'), 0)


class RoomTests(unittest.TestCase):
    def test_too_little_ssd_space_is_not_available(self):
        usage = lambda path: SimpleNamespace(free=12 * 1000**3)  # noqa: E731
        with self.assertRaises(assistant.NotHere) as caught:
            assistant.check_space({'data_mount': '/srv/nekoserver'}, 3.3, usage)
        self.assertIn('needs 20 GB free', str(caught.exception))

    def test_enough_ssd_space_passes(self):
        usage = lambda path: SimpleNamespace(free=200 * 1000**3)  # noqa: E731
        assistant.check_space({'data_mount': '/srv/nekoserver'}, 6.6, usage)


class LimitTests(unittest.TestCase):
    """It never takes what the other apps need: half the cores, a memory cap."""

    def test_half_the_cores_and_a_memory_cap(self):
        self.assertEqual(assistant.limits(16, 8, 3.3), ('4', '6g'))
        self.assertEqual(assistant.limits(32, 16, 6.6), ('8', '12g'))

    def test_the_model_gets_at_least_what_it_needs_within_sixty_percent(self):
        self.assertEqual(assistant.limits(24, 4, 6.6), ('2', '10g'))
        self.assertEqual(assistant.limits(12, 4, 3.3), ('2', '6g'))

    def test_a_one_core_pc_still_gets_one(self):
        self.assertEqual(assistant.limits(12, 1, 3.3)[0], '1')
        self.assertEqual(assistant.limits(12, None, 3.3)[0], '1')


class TemplateTests(unittest.TestCase):
    VALUES = {
        'DATA': '/srv/nekoserver',
        'CPUS': '2',
        'MODEL_MEMORY': '6g',
        'MODEL': 'qwen3.5:4b',
        'TZ': 'Europe/London',
    }

    def rendered(self):
        return assistant.render((ROOT / 'compose/assistant.yaml').read_text(), self.VALUES)

    def test_every_value_is_filled_in(self):
        text = self.rendered()
        self.assertNotIn('${NEKO_', text)
        self.assertIn('cpus: "2"', text)
        self.assertIn('mem_limit: "6g"', text)
        self.assertIn('DEFAULT_MODELS: "qwen3.5:4b"', text)
        self.assertIn('"/srv/nekoserver/appdata/assistant/models:/root/.ollama"', text)

    def test_a_value_missing_is_refused(self):
        with self.assertRaises(RuntimeError):
            assistant.render('cpus: "${NEKO_CPUS:-2}"', {})

    def test_it_stays_out_of_the_way_of_the_other_apps(self):
        text = self.rendered()
        # Both containers: last in line for the processor, first to go without memory.
        self.assertEqual(text.count('cpu_shares: 256'), 2)
        self.assertEqual(text.count('oom_score_adj: 1000'), 2)
        self.assertEqual(text.count('mem_limit:'), 2)
        # Its own project and network: no other app's network, nothing published but
        # the chat page, and that on this PC only (the gateway publishes it further).
        self.assertNotIn('nekoserver-media', text)
        self.assertNotIn('networks:', text)
        self.assertEqual(text.count('ports:'), 1)
        self.assertIn('"127.0.0.1:8090:8080"', text)
        self.assertNotIn('11434:', text)

    def test_nobody_can_sign_up_and_nothing_is_sent_out(self):
        text = self.rendered()
        self.assertIn('ENABLE_SIGNUP: "false"', text)
        self.assertIn('ANONYMIZED_TELEMETRY: "false"', text)
        self.assertIn('ENABLE_OPENAI_API: "false"', text)
        # The secrets come from the private .env, never from the Compose file.
        self.assertIn('WEBUI_ADMIN_PASSWORD: "${WEBUI_ADMIN_PASSWORD}"', text)

    def test_the_images_are_pinned(self):
        text = (ROOT / 'compose/assistant.yaml').read_text()
        self.assertIn('image: ollama/ollama:0.40.16', text)
        self.assertIn('image: ghcr.io/open-webui/open-webui:v0.13.3', text)
        self.assertNotIn(':latest', text)
        self.assertNotIn(':main', text)

    def test_the_port_matches_the_shared_list(self):
        self.assertEqual(neko_common.app_port('assistant'), 8090)
        self.assertEqual(assistant.PORT, 8090)


class SecretTests(unittest.TestCase):
    def test_the_secret_key_is_kept_once_made(self):
        login = {'username': 'neko_ab@server.invalid', 'password': 'Pw-123456789abc'}
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / '.env'
            first = assistant.env_file(env, login)
            env.write_text(first)
            self.assertEqual(assistant.env_file(env, login), first)
        self.assertIn('WEBUI_ADMIN_EMAIL=neko_ab@server.invalid\n', first)
        self.assertIn('WEBUI_ADMIN_PASSWORD=Pw-123456789abc\n', first)

    def test_two_new_servers_get_two_keys(self):
        login = {'username': 'a@server.invalid', 'password': 'x' * 12}
        self.assertNotEqual(assistant.env_file('/nonexistent', login), assistant.env_file('/nonexistent', login))

    def test_the_login_is_one_of_nekoservers_own(self):
        self.assertIn('assistant', assistant.account_store.APPS)
        self.assertIn('assistant', assistant.account_store.EMAIL_LOGINS)
        # The accounts step never picks it: the assistant's own step makes and checks it.
        self.assertNotIn('assistant', assistant.account_store.selected({}, ('media', 'photos', 'nextcloud')))


class SignInTests(unittest.TestCase):
    LOGIN = {'username': 'neko_ab@server.invalid', 'password': 'secret-password'}

    def test_a_token_is_a_working_login(self):
        sent = []

        def opener(request, timeout):
            sent.append(json.loads(request.data))
            return Answer(b'{"token": "abc", "token_type": "Bearer"}')

        self.assertTrue(account_setup.assistant_signs_in(self.LOGIN, opener))
        self.assertEqual(sent, [{'email': 'neko_ab@server.invalid', 'password': 'secret-password'}])

    def test_a_refused_login_says_where_to_look_and_never_the_password(self):
        def opener(request, timeout):
            raise urllib.error.HTTPError(request.full_url, 400, 'bad', {}, None)

        with self.assertRaises(RuntimeError) as caught:
            account_setup.assistant_signs_in(self.LOGIN, opener)
        self.assertIn('ASSISTANT.md', str(caught.exception))
        self.assertNotIn('secret-password', str(caught.exception))

    def test_a_page_that_does_not_answer_is_said(self):
        def opener(request, timeout):
            raise ConnectionRefusedError('refused')

        with self.assertRaises(RuntimeError) as caught:
            account_setup.assistant_signs_in(self.LOGIN, opener)
        self.assertIn('did not answer', str(caught.exception))

    def test_the_sign_in_check_of_account_setup_uses_it(self):
        with (
            mock.patch.object(account_setup.store, 'get', return_value=self.LOGIN),
            mock.patch.object(account_setup, 'assistant_signs_in', return_value=True) as check,
        ):
            self.assertTrue(account_setup.signs_in({}, 'assistant'))
        check.assert_called_once_with(self.LOGIN)


class StartTests(unittest.TestCase):
    def test_it_waits_for_the_chat_page(self):
        answers = iter([OSError('not yet'), Answer(b'{"status": false}'), Answer(b'{"status": true}')])

        def opener(url, timeout):
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        slept = []
        assistant.wait_until_answering(opener, slept.append, clock=lambda: 0)
        self.assertEqual(slept, [5, 5])

    def test_a_page_that_never_answers_is_said(self):
        def opener(url, timeout):
            raise OSError('refused')

        clock = iter([0, 100, 400])
        with self.assertRaises(RuntimeError) as caught:
            assistant.wait_until_answering(opener, lambda s: None, clock=lambda: next(clock))
        self.assertIn('did not answer within 5 minutes', str(caught.exception))


class ModelDownloadTests(unittest.TestCase):
    BASE = ['docker', 'compose', '-f', 'x', '-p', 'neko-assistant']

    def test_a_model_already_there_is_not_downloaded_again(self):
        ran = []

        def run(argv, timeout, what):
            ran.append(argv)
            return SimpleNamespace(stdout='NAME           ID   SIZE\nqwen3.5:4b     abc  3.3 GB\n')

        self.assertEqual(assistant.fetch_model(self.BASE, 'qwen3.5:4b', print, run), 'ready')
        self.assertEqual(len(ran), 1)

    def test_a_missing_model_downloads_in_the_background(self):
        ran, said = [], []

        def run(argv, timeout, what):
            ran.append(argv)
            return SimpleNamespace(stdout='NAME   ID   SIZE\nqwen3.5:9b  abc  6.6 GB\n')

        self.assertEqual(assistant.fetch_model(self.BASE, 'qwen3.5:4b', said.append, run), 'downloading')
        self.assertEqual(ran[1][len(self.BASE) :], ['exec', '-d', '-T', 'ollama', 'ollama', 'pull', 'qwen3.5:4b'])
        self.assertTrue(any('in the background' in line for line in said))


class InstallTests(unittest.TestCase):
    """The whole install with Docker and the chat page stood in."""

    def test_install_writes_private_files_starts_it_and_checks_the_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'data_mount': tmp, 'timezone': 'UTC'}
            ctx = context(config)
            login = {'username': 'neko_ab@server.invalid', 'password': 'Pw-123456789abc'}
            started = []
            with (
                mock.patch.object(assistant, 'saved_login', return_value=login),
                mock.patch.object(assistant, 'host_timezone', return_value='UTC'),
                mock.patch.object(assistant, 'memory_gib', return_value=16),
                mock.patch.object(assistant.os, 'cpu_count', return_value=8),
                mock.patch.object(assistant, 'compose_up', side_effect=lambda base, what, log: started.append(base)),
                mock.patch.object(assistant, 'wait_until_answering'),
                mock.patch.object(assistant.account_setup, 'assistant_signs_in', return_value=True) as signed,
                mock.patch.object(assistant.account_store, 'update') as update,
                mock.patch.object(assistant, 'fetch_model', return_value='downloading'),
                mock.patch.object(assistant.guided_setup, 'login_command', return_value='show assistant'),
            ):
                assistant.install(ctx, 'qwen3.5:4b', 3.3)
            project = Path(tmp) / 'compose/assistant'
            self.assertEqual(oct((project / '.env').stat().st_mode & 0o777), '0o600')
            self.assertEqual(oct((project / 'compose.yaml').stat().st_mode & 0o777), '0o600')
            manifest = (project / 'compose.yaml').read_text()
            self.assertIn('cpus: "4"', manifest)
            self.assertIn('mem_limit: "6g"', manifest)
            self.assertNotIn('Pw-123456789abc', manifest)
            self.assertEqual(started[0][-2:], ['-p', 'neko-assistant'])
            signed.assert_called_once_with(login)
            update.assert_called_once_with(config, 'assistant', state='verified', allow_create=False)
            report = json.loads((Path(tmp) / 'reports/assistant.json').read_text())
            self.assertEqual(report['model'], 'qwen3.5:4b')
            self.assertFalse(report['model_ready'])
            self.assertTrue(neko_common.assistant_installed({**config, 'assistant': {'wanted': True}}))

    def test_a_login_that_does_not_work_is_never_marked_verified(self):
        """Then the gateway leaves the assistant out (it publishes only verified logins)."""
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(assistant, 'saved_login', return_value={'username': 'a@b', 'password': 'p' * 12}),
                mock.patch.object(assistant, 'host_timezone', return_value='UTC'),
                mock.patch.object(assistant, 'compose_up'),
                mock.patch.object(assistant, 'wait_until_answering'),
                mock.patch.object(
                    assistant.account_setup, 'assistant_signs_in', side_effect=RuntimeError('login refused')
                ),
                mock.patch.object(assistant.account_store, 'update') as update,
                self.assertRaises(RuntimeError),
            ):
                assistant.install(context({'data_mount': tmp}), 'qwen3.5:4b', 3.3)
            update.assert_not_called()

    def test_the_whole_step_turns_that_into_a_step_that_waits(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(assistant.guided_setup, 'wanted', return_value=True),
                mock.patch.object(assistant.os, 'geteuid', return_value=0),
                mock.patch.object(assistant, 'verify_storage'),
                mock.patch.object(assistant, 'memory_gib', return_value=16),
                mock.patch.object(assistant, 'check_space'),
                mock.patch.object(assistant, 'install', side_effect=RuntimeError('login refused')),
            ):
                result = assistant.run(context({'data_mount': tmp}))
        self.assertEqual(result['status'], 'needs_setup')
        self.assertIn('login refused', result['reason'])


class GatewayTests(unittest.TestCase):
    """The assistant gets a home and a Tailscale address only once it works, and never
    holds up the other apps' addresses."""

    def config(self, tmp, wanted=True, compose=True):
        config = {
            'data_mount': tmp,
            'media': {'services': ['jellyfin', 'seerr']},
            'photos': {'deploy_immich': False},
            'nextcloud': {'enabled': False},
            'gateway': {'services': list(gateway_tools.DEFAULT_SERVICES)},
            'assistant': {'wanted': wanted},
        }
        if compose:
            (Path(tmp) / 'compose/assistant').mkdir(parents=True)
            (Path(tmp) / 'compose/assistant/compose.yaml').write_text('services: {}\n')
        return config

    def test_a_working_assistant_is_published_after_the_others(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(gateway_tools, 'logins_verified', return_value=True),
        ):
            self.assertEqual(gateway_tools.selected(self.config(tmp)), ['jellyfin', 'seerr', 'assistant'])

    def test_an_assistant_whose_login_was_not_checked_is_left_out(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(gateway_tools, 'logins_verified', return_value=False),
        ):
            self.assertEqual(gateway_tools.selected(self.config(tmp)), ['jellyfin', 'seerr'])

    def test_an_assistant_not_installed_or_not_wanted_is_left_out(self):
        with mock.patch.object(gateway_tools, 'logins_verified', return_value=True):
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(gateway_tools.selected(self.config(tmp, compose=False)), ['jellyfin', 'seerr'])
            with tempfile.TemporaryDirectory() as tmp:
                self.assertEqual(gateway_tools.selected(self.config(tmp, wanted=False)), ['jellyfin', 'seerr'])

    def test_a_settings_file_without_a_gateway_list_still_works(self):
        """The list defaults to every everyday app: the assistant must not be in it, or a
        server without it would be told it is "in the gateway list but not set up"."""
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(tmp, compose=False)
            del config['gateway']
            config['media']['services'] = ['jellyfin', 'seerr', 'navidrome']
            config['photos']['deploy_immich'] = True
            config['nextcloud']['enabled'] = True
            self.assertNotIn('assistant', gateway_tools.selected(config))

    def test_the_assistant_is_never_written_into_the_saved_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.config(tmp)
            del config['gateway']
            config['media']['services'] = ['jellyfin', 'seerr', 'navidrome']
            config['photos']['deploy_immich'] = True
            config['nextcloud']['enabled'] = True
            ctx = SimpleNamespace(config=config, apply=True, log=lambda line: None, persist_config=lambda: None)
            with mock.patch.object(gateway_tools, 'logins_verified', return_value=True):
                gateway_tools.configure(ctx, 'tailnet')
            self.assertNotIn('assistant', config['gateway']['services'])
            self.assertIn('assistant', config['gateway']['confirmed_services'])
            # And the next run reads that list back without complaint.
            with mock.patch.object(gateway_tools, 'logins_verified', return_value=True):
                self.assertIn('assistant', gateway_tools.selected(config))

    def test_its_containers_are_not_part_of_the_gateway_check(self):
        seen = []

        def containers(config, photos=False):
            seen.append(list(config['media']['services']))
            return []

        with mock.patch('acceptance.containers', side_effect=containers):
            self.assertEqual(gateway_tools.unverified_containers({}, ['jellyfin', 'assistant']), [])
        self.assertEqual(seen, [['jellyfin']])

    def test_it_has_a_tailscale_port_of_its_own(self):
        self.assertEqual(gateway_tools.APPS['assistant'][2], 8449)
        self.assertIn('8449', gateway_tools.tailnet_ports())


class OthersComeFirstTests(unittest.TestCase):
    def test_its_ports_never_block_the_dashboard_or_the_torrent_port(self):
        self.assertNotIn(8090, neko_common.APP_PORTS)
        self.assertNotIn(8449, neko_common.APP_PORTS)
        self.assertIn(8096, neko_common.APP_PORTS)

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
        needing = [name for name, module in modules.items() if 'assistant' in module['deps']]
        self.assertEqual(needing, [])

    def test_it_comes_in_the_full_setup_before_the_shortcuts_and_addresses(self):
        full = json.loads((ROOT / 'config.json').read_text())['profiles']['full']
        self.assertIn('assistant', full)
        for later in ('dashboard', 'desktop', 'gateway_tailnet', 'gateway_lan'):
            with self.subTest(later=later):
                self.assertLess(full.index('assistant'), full.index(later))

    def test_the_template_asks_before_installing(self):
        settings = json.loads((ROOT / 'config.json').read_text())
        self.assertNotIn('wanted', settings['assistant'])
        self.assertEqual(settings['assistant']['model'], 'auto')


class InstalledTests(unittest.TestCase):
    def test_wanted_and_set_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {'data_mount': tmp, 'assistant': {'wanted': True}}
            self.assertFalse(neko_common.assistant_installed(config))
            (Path(tmp) / 'compose/assistant').mkdir(parents=True)
            (Path(tmp) / 'compose/assistant/compose.yaml').write_text('x')
            self.assertTrue(neko_common.assistant_installed(config))
            for other in ({'wanted': False}, {}, [], None):
                with self.subTest(other=other):
                    self.assertFalse(neko_common.assistant_installed({'data_mount': tmp, 'assistant': other}))


if __name__ == '__main__':
    unittest.main()
