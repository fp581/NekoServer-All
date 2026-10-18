"""Account choice/privacy tests and real loopback account protocol fixtures."""
import base64
import copy
import hashlib
import http.cookiejar
import http.server
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from unittest.mock import patch

import account_store as store
import account_setup as setup
import music_client
from modules import nextcloud

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT.parents[1] / 'compose-review/docker-compose'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'data_mount': self.temp.name}

    def test_generated_distinct_credentials_private_and_not_echoed(self):
        messages = []
        store.collect(self.config, list(store.APPS), prompt=lambda _: '1', output=messages.append)
        value = store.load(self.config)
        self.assertEqual(len(value['apps']), len(store.APPS))
        self.assertEqual(len({v['username'] for v in value['apps'].values()}), len(store.APPS))
        self.assertEqual(len({v['password'] for v in value['apps'].values()}), len(store.APPS))
        export = (store.folder(self.config) / 'APP-CREDENTIALS.txt').read_text()
        for app, entry in value['apps'].items():
            self.assertEqual(len(entry['password']), 32)
            self.assertIn('=== ' + store.APPS[app][0] + ' ===', export)
            self.assertIn('PLANNED', export)
            self.assertNotIn(entry['password'], '\n'.join(messages))
        for name in ('credentials.json', 'APP-CREDENTIALS.txt'):
            self.assertEqual((store.folder(self.config) / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(store.folder(self.config).stat().st_mode & 0o777, 0o700)

    def test_custom_konsole_prompts_and_confirmation(self):
        answers = iter(['2', '2', 'Lidor', 'Lidor'])
        # First confirmation fails, then the second attempt matches exactly.
        password_values = iter(['Different12345!', 'Mismatch12345!', 'Real$Password123!\\', 'Real$Password123!\\'])
        messages = []
        store.collect(self.config, ['jellyfin'], prompt=lambda _: next(answers),
                      password_prompt=lambda _: next(password_values), output=messages.append)
        entry = store.get(self.config, 'jellyfin')
        self.assertEqual(entry['username'], 'Lidor')
        self.assertEqual(entry['password'], 'Real$Password123!\\')
        self.assertTrue(any('did not match' in row for row in messages))
        self.assertNotIn(entry['password'], '\n'.join(messages))

    def test_rerun_preserves_choices_and_repairs_missing_export(self):
        store.collect(self.config, ['jellyfin'], prompt=lambda _: '1', output=lambda _: None)
        before = store.load(self.config)
        (store.folder(self.config) / 'APP-CREDENTIALS.txt').unlink()
        store.collect(self.config, ['jellyfin'], prompt=lambda _: self.fail('Unexpected prompt'), output=lambda _: None)
        self.assertEqual(before, store.load(self.config))
        self.assertTrue((store.folder(self.config) / 'APP-CREDENTIALS.txt').exists())

    def test_existing_data_requires_existing_login_and_no_creation(self):
        root = Path(self.temp.name) / 'appdata/jellyfin'
        root.mkdir(parents=True)
        (root / 'existing.xml').write_text('existing app')
        answers = iter(['1', 'ExistingUser'])
        store.collect(self.config, ['jellyfin'], prompt=lambda _: next(answers),
                      password_prompt=lambda _: 'oldpw', output=lambda _: None)
        self.assertFalse(store.get(self.config, 'jellyfin')['allow_create'])
        self.assertEqual(store.load(self.config)['apps']['jellyfin']['password'], 'oldpw')

    def test_unsafe_store_is_refused(self):
        store.collect(self.config, ['jellyfin'], prompt=lambda _: '1', output=lambda _: None)
        path = store.folder(self.config) / 'credentials.json'
        path.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'private'):
            store.load(self.config)
        path.chmod(0o600)
        os.link(path, path.with_name('hardlink'))
        with self.assertRaisesRegex(RuntimeError, 'linked'):
            store.load(self.config)

    def test_writable_parent_cannot_host_account_passwords(self):
        parent = Path(self.temp.name) / 'secrets'
        parent.mkdir(mode=0o777)
        parent.chmod(0o777)
        with self.assertRaisesRegex(RuntimeError, 'parent'):
            store.load(self.config)
        self.assertFalse((parent / 'accounts').exists())

    def test_symlink_export_is_never_overwritten(self):
        store.collect(self.config, ['jellyfin'], prompt=lambda _: '1', output=lambda _: None)
        path = store.folder(self.config) / 'APP-CREDENTIALS.txt'
        path.unlink()
        victim = Path(self.temp.name) / 'victim'
        victim.write_text('unchanged')
        path.symlink_to(victim)
        with self.assertRaises(RuntimeError):
            store.save(self.config, store.load(self.config))
        self.assertEqual(victim.read_text(), 'unchanged')

    def test_pending_existing_login_can_be_corrected_without_reset(self):
        store.collect(self.config, ['jellyfin'], prompt=lambda _: '1', output=lambda _: None)
        store.update(self.config, 'jellyfin', state='needs_setup', allow_create=False)
        answers = iter(['c', 'CurrentUser'])
        store.collect(self.config, ['jellyfin'], prompt=lambda _: next(answers),
                      password_prompt=lambda _: 'CurrentPassword123!', output=lambda _: None)
        entry = store.get(self.config, 'jellyfin')
        self.assertEqual(entry['username'], 'CurrentUser')
        self.assertEqual(entry['password'], 'CurrentPassword123!')
        self.assertEqual(entry['state'], 'planned')
        self.assertFalse(entry['allow_create'])

    def test_email_logins_and_password_control_characters(self):
        with self.assertRaises(ValueError):
            store.validate_login('immich', 'not-an-email', 'Password12345!')
        for value in ('short', 'Password1234\nInjected'):
            with self.assertRaises(ValueError):
                store.validate_login('jellyfin', 'Lidor', value)

    def test_selected_scope_respects_apps_and_disabled_cloud(self):
        cfg = {'media': {'services': ['navidrome', 'jellyfin']}, 'nextcloud': {'enabled': False}, 'photos': {'deploy_immich': True}}
        self.assertEqual(store.selected(cfg, ['media']), ['jellyfin', 'navidrome', 'navidrome_listener'])
        self.assertEqual(store.selected(cfg, ['nextcloud', 'photos']), ['immich'])

    def test_custom_password_roundtrips_compose_dotenv(self):
        if not COMPOSE.exists():
            self.skipTest('Official Compose parser required')
        values = ['Abcd$xyz#12! hello', "a'bcde1234567", 'abc\\def123456', 'abcdef12345\\', "abc\\'123456789", 'Abc"def123456', 'éå🐱$VARIABLE123']
        for value in values:
            with self.subTest(value=value):
                p = Path(self.temp.name)
                (p / '.env').write_text('PASS=' + nextcloud.quote_env(value) + '\n')
                (p / 'compose.json').write_text(json.dumps({'services': {'fixture': {'image': 'alpine', 'environment': {'PASS': '${PASS}'}}}}))
                result = subprocess.run([str(COMPOSE), '--env-file', str(p / '.env'), '-f', str(p / 'compose.json'), 'config', '--environment'], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = next(row.removeprefix('PASS=') for row in result.stdout.splitlines() if row.startswith('PASS='))
                self.assertEqual(actual, value)


class Server:
    """Stateful protocol fixture: passwords, cookies and persisted users are checked."""
    def __init__(self, app, entries, peers):
        self.app, self.entries, self.peers = app, entries, peers
        self.users = {}
        self.created = 0
        self.writes = 0
        self.complete = False
        self.settings = {'username': '', 'password': '', 'authenticationMethod': 'none', 'authenticationRequired': 'enabled'}
        self.qbit = {'web_ui_username': 'admin', 'web_ui_password': 'Temporary12345!', 'bypass_local_auth': False, 'bypass_auth_subnet_whitelist_enabled': False}
        self.library_grants = {}
        self.password = None
        state = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, value=None, status=200, cookie=None, text=False):
                self.send_response(status)
                if cookie:
                    self.send_header('Set-Cookie', cookie + '; Path=/')
                if status == 302:
                    self.send_header('Location', '/external-redirect-must-not-be-followed')
                self.end_headers()
                if value is not None:
                    self.wfile.write((value if isinstance(value, bytes) else str(value).encode()) if text else json.dumps(value).encode())

            def handle_request(self):
                path = urllib.parse.urlsplit(self.path).path
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                form = urllib.parse.parse_qs(raw.decode()) if self.headers.get('Content-Type', '').startswith('application/x-www-form-urlencoded') else {}
                body = json.loads(raw) if raw and not form and self.headers.get('Content-Type') == 'application/json' else {}
                method = self.command
                entry = state.entries[state.app]
                username, password = entry['username'], entry['password']
                ok = lambda u, p: u == username and p == password
                cookie = self.headers.get('Cookie', '')
                if path == '/external-redirect-must-not-be-followed':
                    return self.respond({'leaked': True}, 500)
                if state.app == 'jellyfin':
                    if path == '/System/Info/Public':return self.respond({'Id': 'server-id', 'StartupWizardCompleted': state.complete})
                    if path == '/Users/AuthenticateByName':
                        if not state.password or not ok(body.get('Username'), body.get('Pw')) or state.password != password:return self.respond({}, 401)
                        return self.respond({'AccessToken': 'token', 'User': {'Id': 'admin-id', 'Name': username, 'Policy': {'IsAdministrator': True}}})
                    if path == '/Startup/User':
                        if method == 'GET':return self.respond({'Name': 'default'})
                        if state.password:return self.respond({}, 403)
                        state.password = body['Password'];state.created += 1;return self.respond(None, 204)
                    if path == '/Startup/RemoteAccess':
                        if body != {'EnableRemoteAccess': False, 'EnableAutomaticPortMapping': False}:return self.respond({}, 400)
                        return self.respond(None, 204)
                    if path == '/Startup/Complete':state.complete = True;return self.respond(None, 204)
                    if path == '/Sessions/Logout':return self.respond(None, 204)
                elif state.app == 'immich':
                    if path == '/api/server/ping':return self.respond({'res': 'pong'})
                    if path == '/api/server/config':return self.respond({'isInitialized': bool(state.password)})
                    if path == '/api/auth/login':
                        if not state.password or not ok(body.get('email'), body.get('password')) or state.password != password:return self.respond({}, 401)
                        return self.respond({'accessToken': 'token', 'isAdmin': True, 'userEmail': username})
                    if path == '/api/auth/admin-sign-up':
                        if state.password:return self.respond({}, 400)
                        state.password = body['password'];state.created += 1;return self.respond({'id': 'admin-id'})
                    if path == '/api/auth/logout':return self.respond({'successful': True})
                elif state.app == 'navidrome':
                    if path == '/ping':return self.respond(b'pong', text=True)
                    if path.startswith('/rest/'):
                        user = state.users.get(query.get('u', [''])[0]);salt = query.get('s', [''])[0]
                        if not user or hashlib.md5((user['password'] + salt).encode()).hexdigest() != query.get('t', [''])[0]:return self.respond({'subsonic-response': {'status': 'failed'}})
                        result = {'status': 'ok'}
                        if 'getUser' in path:result['user'] = {'username': user['userName'], 'adminRole': user['isAdmin']}
                        if 'getMusicFolders' in path:result['musicFolders'] = {'musicFolder': [{'id': 1, 'name': 'Music'}] if state.library_grants.get(user['id']) else []}
                        if 'getAlbumList2' in path:result['albumList2'] = {'album': []}
                        return self.respond({'subsonic-response': result})
                    if path in ('/auth/login', '/auth/createAdmin'):
                        if path.endswith('createAdmin'):
                            if state.users:return self.respond({}, 403)
                            state.users[body['username']] = {'userName': body['username'], 'password': body['password'], 'id': 'admin-id', 'isAdmin': True};state.created += 1
                        user = state.users.get(body.get('username'))
                        if not user or user['password'] != body.get('password'):return self.respond({}, 401)
                        return self.respond({'id': user['id'], 'username': user['userName'], 'isAdmin': user['isAdmin'], 'token': 'native-token'})
                    if self.headers.get('X-ND-Authorization') != 'Bearer native-token':return self.respond({}, 401)
                    if path == '/api/library/':return self.respond([{'id': 1}])
                    if path == '/api/user/':
                        if method == 'GET':return self.respond(list(state.users.values()))
                        if body['userName'] in state.users:return self.respond({}, 409)
                        user = {**body, 'id': 'listener-id'};state.users[body['userName']] = user;state.created += 1;return self.respond(user)
                    if path == '/api/user/listener-id/library/' and method == 'PUT':
                        state.library_grants['listener-id'] = body['libraryIds'];return self.respond([])
                elif state.app in setup.ARR:
                    prefix = '/api/' + setup.ARR[state.app][0]
                    if path == '/login':
                        if not ok(form.get('username', [''])[0], form.get('password', [''])[0]) or state.settings['password'] != password:return self.respond(None, 302)
                        return self.respond(None, 302, cookie=state.app + 'Auth=valid')
                    authenticated = self.headers.get('X-Api-Key') == 'fixture-api-key' or state.app + 'Auth=valid' in cookie
                    if not authenticated:return self.respond({}, 401)
                    if path == prefix + '/system/status':return self.respond({'version': 'fixture'})
                    if path == prefix + '/config/host':
                        if method == 'PUT':state.settings = body;state.writes += 1
                        return self.respond(state.settings)
                elif state.app == 'qbittorrent':
                    if path == '/api/v2/auth/login':
                        if form.get('username', [''])[0] != state.qbit['web_ui_username'] or form.get('password', [''])[0] != state.qbit['web_ui_password']:return self.respond(b'Fails.', text=True)
                        return self.respond(b'Ok.', cookie='SID=valid', text=True)
                    if 'SID=valid' not in cookie:return self.respond({}, 403)
                    if path == '/api/v2/app/version':return self.respond(b'v5.fixture', text=True)
                    if path == '/api/v2/app/preferences':return self.respond(state.qbit)
                    if path == '/api/v2/app/setPreferences':state.qbit.update(json.loads(form['json'][0]));state.writes += 1;return self.respond(b'', text=True)
                    if path == '/api/v2/auth/logout':return self.respond(b'', cookie='SID=; Max-Age=0', text=True)
                elif state.app == 'seerr':
                    if path == '/api/v1/status':return self.respond({'version': 'fixture'})
                    if path == '/api/v1/settings/public':return self.respond({'initialized': state.complete})
                    if path == '/api/v1/auth/local':
                        if not state.password or not ok(body.get('email'), body.get('password')) or state.password != password:return self.respond({}, 401)
                        return self.respond({'id': 1, 'email': username}, cookie='connect.sid=valid')
                    if path == '/api/v1/auth/jellyfin':
                        jf = state.entries['jellyfin']
                        if body.get('username') != jf['username'] or body.get('password') != jf['password'] or body.get('hostname') != 'jellyfin' or body.get('serverType') != 2:return self.respond({}, 401)
                        state.created += 1;return self.respond({'id': 1, 'email': body['email']}, cookie='connect.sid=valid')
                    if path == '/api/v1/auth/logout':return self.respond(None, 204, cookie='connect.sid=; Max-Age=0')
                    if 'connect.sid=valid' not in cookie:return self.respond({}, 401)
                    if path == '/api/v1/user/1/settings/password':
                        if method == 'GET':return self.respond({'hasPassword': bool(state.password)})
                        if state.password:return self.respond({}, 403)
                        state.password = body['newPassword'];state.writes += 1;return self.respond(None, 204)
                    if path == '/api/v1/settings/initialize':state.complete = True;return self.respond({'initialized': True})
                elif state.app == 'nextcloud':
                    if path == '/status.php':return self.respond({'installed': True, 'maintenance': False, 'needsDbUpgrade': False})
                    expected = 'Basic ' + base64.b64encode((username + ':' + password).encode()).decode()
                    if method == 'PROPFIND' and self.headers.get('Authorization') == expected:return self.respond(b'<d:multistatus xmlns:d="DAV:"/>', 207, text=True)
                    return self.respond({}, 401)
                return self.respond({'unsupported': path}, 404)

            do_GET = do_POST = do_PUT = do_PROPFIND = handle_request

        self.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=lambda: self.http.serve_forever(poll_interval=0.02), daemon=True)
        self.thread.start()

    def close(self):
        self.http.shutdown();self.http.server_close();self.thread.join()


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'data_mount': str(self.root), 'accounts': {'enabled': True, 'wait_seconds': 0}}
        store.collect(self.config, list(store.APPS), prompt=lambda _: '1', output=lambda _: None)
        self.entries = store.load(self.config)['apps']
        self.servers = {}
        for app in store.APPS:
            if app == 'navidrome_listener':continue
            self.servers[app] = Server(app, self.entries, self.servers)
            self.addCleanup(self.servers[app].close)
            folder = self.root / 'compose' / app;folder.mkdir(parents=True)
            (folder / 'compose.yaml').write_text('managed fixture')
        for app in setup.ARR:
            p = self.root / 'appdata' / app;p.mkdir(parents=True)
            (p / 'config.xml').write_text('<Config><ApiKey>fixture-api-key</ApiKey></Config>')
        self.ids = {hashlib.sha256(app.encode()).hexdigest(): app for app in self.servers}

    def factory(self, app):
        return setup.Client(app, port=self.servers[app].http.server_address[1])

    def docker(self, args, input_text=None):
        if args[0] == 'ps':
            project = next(value.split('neko-')[-1] for value in args if value.startswith('label=com.docker.compose.project='))
            return hashlib.sha256(project.encode()).hexdigest() + '\n'
        if args[0] == 'inspect':
            app = self.ids[args[1]];service = 'immich-server' if app == 'immich' else app
            source = self.root / ('photos/immich-library' if app == 'immich' else 'appdata/nextcloud/html' if app == 'nextcloud' else 'appdata/' + app)
            port = 80 if app == 'nextcloud' else store.APPS[app][1]
            return json.dumps([{'State': {'Running': True}, 'Config': {'Labels': {'com.docker.compose.project': 'neko-' + app, 'com.docker.compose.service': service}},
                                'Mounts': [{'Type': 'bind', 'Source': str(source)}],
                                'HostConfig': {'PortBindings': {str(port) + '/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(store.APPS[app][1])}]}}}])
        if args[0] == 'logs':return 'The WebUI administrator password was not set. A temporary password is provided for this session: Temporary12345!\n'
        if args[0] == 'compose':return json.dumps({'enabled': True, 'groups': ['admin']})
        self.fail('Unexpected host command')

    def run_accounts(self, apps=None, docker=None):
        inspect = music_client.inspect
        messages = []
        with patch.object(setup, 'docker', side_effect=docker or self.docker), patch.object(music_client, 'inspect', side_effect=lambda value: inspect(value, base=self.factory('navidrome').base)):
            pending = setup.provision(self.config, apps or list(store.APPS), output=messages.append, client_factory=self.factory)
        return pending, messages

    def test_all_accounts_created_verified_and_rerun_without_rotation(self):
        pending, messages = self.run_accounts()
        self.assertEqual(pending, [], messages)
        first = copy.deepcopy(store.load(self.config))
        self.assertTrue(all(v['state'] == 'verified' and v['allow_create'] is False for v in first['apps'].values()))
        self.assertTrue(music_client.credential_path(self.config).is_file())
        counters = {name: (server.created, server.writes) for name, server in self.servers.items()}
        pending, messages = self.run_accounts()
        self.assertEqual(pending, [], messages)
        self.assertEqual(first, store.load(self.config))
        self.assertEqual(counters, {name: (server.created, server.writes) for name, server in self.servers.items()})
        for entry in self.entries.values():self.assertNotIn(entry['password'], '\n'.join(messages))

    def test_changed_existing_password_is_not_reset(self):
        self.assertEqual(self.run_accounts(['jellyfin'])[0], [])
        self.servers['jellyfin'].password = 'ChangedOutsideInstaller123!'
        pending, _ = self.run_accounts(['jellyfin'])
        self.assertEqual(pending, ['jellyfin'])
        self.assertEqual(self.servers['jellyfin'].password, 'ChangedOutsideInstaller123!')
        self.assertEqual(store.get(self.config, 'jellyfin')['state'], 'needs_setup')
        self.assertEqual(self.run_accounts(['jellyfin'])[0], ['jellyfin'])
        self.assertEqual(self.servers['jellyfin'].created, 1)

    def test_other_account_remains_untouched(self):
        self.servers['immich'].password = 'ExistingUnrelatedAccount123!'
        pending, _ = self.run_accounts(['immich'])
        self.assertEqual(pending, ['immich'])
        self.assertEqual(self.servers['immich'].created, 0)

    def test_wrong_project_identity_blocks_before_any_signup(self):
        def wrong(args, input_text=None):
            value = self.docker(args)
            if args[0] == 'inspect':
                records = json.loads(value);records[0]['Config']['Labels']['com.docker.compose.project'] = 'unrelated-project';value = json.dumps(records)
            return value
        pending, _ = self.run_accounts(['jellyfin'], docker=wrong)
        self.assertEqual(pending, ['jellyfin'])
        self.assertEqual(self.servers['jellyfin'].created, 0)

    def test_wrong_mount_or_public_port_blocks_credentials(self):
        for change in ('mount', 'port'):
            def wrong(args, input_text=None):
                value = self.docker(args)
                if args[0] == 'inspect':
                    records = json.loads(value)
                    if change == 'mount':records[0]['Mounts'][0]['Source'] = '/wrong/disk'
                    else:records[0]['HostConfig']['PortBindings']['8096/tcp'][0]['HostIp'] = '0.0.0.0'
                    value = json.dumps(records)
                return value
            self.assertEqual(self.run_accounts(['jellyfin'], docker=wrong)[0], ['jellyfin'])
            self.assertEqual(self.servers['jellyfin'].created, 0)

    def test_failed_array_login_is_not_verified_with_api_key(self):
        server = self.servers['sonarr']
        server.settings.update(username=self.entries['sonarr']['username'], password='WrongExistingPassword123!', authenticationMethod='forms')
        pending, _ = self.run_accounts(['sonarr'])
        self.assertEqual(pending, ['sonarr'])
        self.assertEqual(server.writes, 0)

    def test_docker_failure_is_sanitized_and_never_prints_secret(self):
        fake = type('Result', (), {'returncode': 1, 'stdout': 'secret-value', 'stderr': 'secret-value'})()
        with patch.object(setup.subprocess, 'run', return_value=fake):
            with self.assertRaises(RuntimeError) as ctx:setup.docker(['logs', 'fixture'])
        self.assertNotIn('secret-value', str(ctx.exception))


class ModuleTests(unittest.TestCase):
    def test_readonly_account_modules_never_prompt_or_write(self):
        from modules import account_credentials, accounts
        from types import SimpleNamespace
        ctx = SimpleNamespace(config={'accounts': {'enabled': True}}, apply=False, log=lambda _: None)
        with patch.object(store, 'collect', side_effect=AssertionError('prompted')), patch.object(setup, 'provision', side_effect=AssertionError('mutated')):
            account_credentials.run(ctx);accounts.run(ctx)

    def test_noninteractive_prompt_is_pending_without_generated_passwords(self):
        from modules import account_credentials
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as temp:
            cfg = {'data_mount': temp, 'accounts': {'enabled': True}, 'media': {'services': ['jellyfin']}}
            ctx = SimpleNamespace(config=cfg, apply=True, log=lambda _: None, plan=['media'])
            with patch.object(account_credentials, 'verify_storage'), patch.object(account_credentials.sys.stdin, 'isatty', return_value=False):
                self.assertEqual(account_credentials.run(ctx)['status'], 'needs_setup')
            self.assertFalse(store.folder(cfg).exists())
