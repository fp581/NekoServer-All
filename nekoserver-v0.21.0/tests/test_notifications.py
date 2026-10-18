"""Native delivery transitions and real curl/TLS transport against local fixtures."""
import copy
import http.server
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from modules import notifications

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / 'runtime/bin/nekoserver-runtime'


class NotificationTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() != 0:
            self.skipTest('Native worker requires root')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.cfg = {'endpoint': 'https://ntfy.example/private_topic', 'token': 'PRIVATE_TOKEN', 'debounce_seconds': 0, 'minimum_interval_seconds': 60}
        self.config.write_text(json.dumps(self.cfg))
        self.config.chmod(0o600)
        self.snapshot = self.root / 'status.json'
        self.state = self.root / 'state'
        self.runtime = self.root / 'runtime'
        self.log = self.root / 'requests.jsonl'
        self.env = {**os.environ, 'CAPTURE': str(self.log)}
        commands = self.root / 'commands'
        commands.mkdir()
        curl = commands / 'curl'
        curl.write_text('''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:];assert args[0]=='--disable'
assert '--proto' in args and args[args.index('--proto')+1]=='=https'
assert '--location' not in args and '--insecure' not in args
assert '--noproxy' in args and '--max-time' in args
path=Path(args[args.index('--config')+1]);assert path.stat().st_mode&0o777==0o600
values={};headers=[]
for line in path.read_text().splitlines():
 key,value=line.split(' = ',1);value=json.loads(value)
 if key=='header':headers.append(value)
 else:values[key]=value
with open(os.environ['CAPTURE'],'a') as out:out.write(json.dumps({'arguments':args,'values':values,'headers':headers})+'\\n')
print(os.environ.get('HTTP','200'))
''')
        curl.chmod(0o755)
        self.env['PATH'] = str(commands) + ':/usr/bin:/bin'
        self.write_snapshot([{'id': 'app:sonarr', 'level': 'warning', 'title': 'PRIVATE API KEY'}])

    def write_snapshot(self, alerts, **extra):
        saved = {'format': 'neko-dashboard-v1', 'generated_at': time.time(), 'alerts': alerts, 'applications': [{'id': 'sonarr', 'state': 'running', 'health': 'healthy'}], 'storage': [], 'backups': [], 'media': {'status': 'current'}, 'metrics': {'temperature': 40}}
        saved.update(extra)
        self.snapshot.write_text(json.dumps(saved))

    def run_worker(self, test=False):
        self.config.write_text(json.dumps(self.cfg))
        return subprocess.run([str(BIN), 'notify', '--config', str(self.config), '--snapshot', str(self.snapshot), '--state', str(self.state), '--runtime', str(self.runtime), *(['--test'] if test else [])], env=self.env, capture_output=True, text=True, timeout=35)

    def saved(self):
        return json.loads((self.state / 'state.json').read_text())

    def allow_next(self):
        saved = self.saved()
        saved['next_attempt'] = 0
        (self.state / 'state.json').write_text(json.dumps(saved))

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_sanitized_delivery_private_config_and_no_secret_arguments(self):
        result = self.run_worker()
        self.assertEqual(result.returncode, 0, result.stderr)
        request = self.requests()[0]
        self.assertNotIn('PRIVATE', request['values']['data-binary'])
        self.assertNotIn('PRIVATE', ' '.join(request['arguments']))
        self.assertIn('Authorization: Bearer PRIVATE_TOKEN', request['headers'])
        self.assertNotIn('PRIVATE', result.stdout + result.stderr)
        self.assertFalse((self.runtime / 'delivery.curl').exists())
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.state / 'state.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.runtime / 'delivery.json').read_text())['pending'], 0)

    def test_repeat_suppression_survives_separate_processes(self):
        self.assertEqual(self.run_worker().returncode, 0)
        self.allow_next()
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(len(self.requests()), 1)

    def test_eligible_problems_are_grouped(self):
        self.write_snapshot([{'id': 'app:sonarr', 'level': 'warning'}, {'id': 'backup:applications', 'level': 'critical'}, {'id': 'space:media_hdd', 'level': 'warning'}])
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(len(self.requests()), 1)
        message = self.requests()[0]['values']['data-binary']
        self.assertIn('Sonarr', message)
        self.assertIn('Media application backup', message)
        self.assertIn('Media HDD', message)

    def test_delivery_failure_retries_and_preserves_unsent_state(self):
        self.env['HTTP'] = '500'
        self.assertNotEqual(self.run_worker().returncode, 0)
        saved = self.saved()
        self.assertIsNone(saved['entries']['app:sonarr']['last_sent'])
        self.assertEqual(saved['attempts'], 1)
        self.assertGreater(saved['next_attempt'], saved['last_attempt'])
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(len(self.requests()), 1)
        self.allow_next()
        self.env['HTTP'] = '200'
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(len(self.requests()), 2)
        self.assertEqual(self.saved()['attempts'], 0)

    def test_recovery_follows_reported_problem(self):
        self.assertEqual(self.run_worker().returncode, 0)
        self.write_snapshot([])
        self.allow_next()
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertIn('RECOVERED', self.requests()[-1]['values']['data-binary'])
        self.assertFalse(self.saved()['entries'])

    def test_stale_snapshot_does_not_send_false_recovery(self):
        self.assertEqual(self.run_worker().returncode, 0)
        self.write_snapshot([], generated_at=time.time() - 500)
        self.allow_next()
        self.assertEqual(self.run_worker().returncode, 0)
        message = self.requests()[-1]['values']['data-binary']
        self.assertIn('Monitoring data unavailable', message)
        self.assertNotIn('RECOVERED', message)
        self.assertIn('app:sonarr', self.saved()['entries'])

    def test_unknown_or_absent_application_does_not_recover(self):
        self.assertEqual(self.run_worker().returncode, 0)
        self.write_snapshot([], applications=[])
        self.allow_next()
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(len(self.requests()), 1)

    def test_test_message_records_provider_acceptance(self):
        self.assertEqual(self.run_worker(True).returncode, 0)
        report = json.loads((self.runtime / 'delivery.json').read_text())
        self.assertEqual(report['status'], 'pass')
        self.assertIsNotNone(report['last_success'])
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_redirect_and_auth_failure_are_not_success(self):
        for code in ('302', '401', '403', '429'):
            self.env['HTTP'] = code
            self.assertNotEqual(self.run_worker(True).returncode, 0)
            self.assertEqual(json.loads((self.runtime / 'delivery.json').read_text())['status'], 'fail')

    def test_new_destination_gets_new_alert_ledger(self):
        self.assertEqual(self.run_worker().returncode, 0)
        self.cfg['endpoint'] = 'https://ntfy.example/new_topic'
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(len(self.requests()), 2)

    def test_public_secret_config_is_refused(self):
        self.config.chmod(0o644)
        self.assertNotEqual(self.run_worker().returncode, 0)
        self.assertFalse(self.log.exists())

    def test_unsafe_destination_and_header_injection_refused(self):
        for value in ('http://host/topic', 'https://user:password@host/topic', 'https://host/topic\nheader=bad'):
            self.cfg['endpoint'] = value
            self.assertNotEqual(self.run_worker().returncode, 0)
        self.cfg['endpoint'] = 'https://host/topic'
        self.cfg['token'] = 'bad\nX-Secret: injected'
        self.assertNotEqual(self.run_worker(True).returncode, 0)
        self.assertFalse(self.log.exists())

    def test_debounced_app_and_immediate_drive_alert(self):
        self.cfg['debounce_seconds'] = 180
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertFalse(self.log.exists())
        self.write_snapshot([{'id': 'mount:data_ssd', 'level': 'critical'}])
        self.assertEqual(self.run_worker().returncode, 0)
        self.assertEqual(len(self.requests()), 1)

    def test_real_curl_https_post_authentication_and_no_redirect(self):
        cert, key = self.root / 'cert.pem', self.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'], check=True, capture_output=True)
        received = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                received.append((self.path, body, self.headers.get('Authorization')))
                self.send_response(200 if self.path == '/topic' else 302)
                self.send_header('Location', 'https://localhost:1/leak')
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.env['PATH'] = '/usr/bin:/bin'
            self.env['CURL_CA_BUNDLE'] = str(cert)
            self.cfg['endpoint'] = f'https://localhost:{server.server_port}/topic'
            self.assertEqual(self.run_worker(True).returncode, 0)
            self.assertEqual(received[0][2], 'Bearer PRIVATE_TOKEN')
            self.assertIn(b'NekoServer test', received[0][1])
            self.cfg['endpoint'] = f'https://localhost:{server.server_port}/redirect'
            self.assertNotEqual(self.run_worker(True).returncode, 0)
            self.assertEqual(len(received), 2)
            del self.env['CURL_CA_BUNDLE']
            self.assertNotEqual(self.run_worker(True).returncode, 0)
            self.assertEqual(len(received), 2, 'untrusted TLS must prevent HTTP delivery')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class SetupTests(unittest.TestCase):
    def test_missing_destination_remains_setup_without_enabling_timer(self):
        config = json.loads((ROOT / 'config.json').read_text())
        with patch.object(notifications, 'PRIVATE_CONFIG', '/nonexistent-neko-fixture/config.json'), patch.object(notifications.sys.stdin, 'isatty', return_value=False), patch.object(notifications.subprocess, 'run') as command:
            result = notifications.run(SimpleNamespace(config=config, apply=True, log=lambda _: None))
        self.assertEqual(result['status'], 'needs_setup')
        command.assert_not_called()

    def test_install_reuses_private_token_and_enables_timer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = json.loads((ROOT / 'config.json').read_text())
            private = root / 'private.json'
            private.write_text(json.dumps({'endpoint':'https://host/topic','token':'PRIVATE_TOKEN'}))
            private.chmod(0o600)
            real_path = Path
            def mapped(value, *args):
                path = real_path(value, *args)
                return root / str(path).lstrip('/') if str(path).startswith(('/etc/', '/usr/local/')) else path
            with patch.object(notifications, 'PRIVATE_CONFIG', str(private)), patch.object(notifications, 'Path', side_effect=mapped), patch.object(notifications.shutil, 'which', return_value='/usr/bin/curl'), patch.object(notifications.subprocess, 'run') as command, patch.object(notifications.getpass, 'getpass') as prompt:
                notifications.run(SimpleNamespace(config=config,apply=True,log=lambda _:None))
            prompt.assert_not_called()
            saved = json.loads(private.read_text())
            self.assertEqual(saved['token'],'PRIVATE_TOKEN')
            self.assertEqual(private.stat().st_mode&0o777,0o600)
            self.assertIn(['systemctl','enable','--now','nekoserver-notifications.timer'], [call.args[0] for call in command.call_args_list])
            self.assertNotIn('PRIVATE_TOKEN',(root/'etc/systemd/system/nekoserver-notifications.service').read_text())

    def test_endpoint_and_timing_validation(self):
        for value in ('http://host/topic', 'https://host:0/topic', 'https://host/topic?token=private'):
            self.assertFalse(notifications.endpoint(value))
        self.assertTrue(notifications.endpoint('https://host:8443/private_topic'))
        for bad in (True, -1, 10000000, '300'):
            with self.assertRaises(RuntimeError):
                notifications.options({'notifications': {'minimum_interval_seconds': bad}})

    def test_service_does_not_depend_on_application_ssd(self):
        units = notifications.definitions('/usr/local/lib/neko/runtime', '/etc/neko/private.json')
        service = units['nekoserver-notifications.service']
        self.assertNotIn('RequiresMountsFor', service)
        self.assertIn('StateDirectoryMode=0700', service)
        self.assertIn('ProtectSystem=strict', service)
        self.assertNotIn('PRIVATE_TOKEN', service)
