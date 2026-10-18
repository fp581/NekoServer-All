"""Native heartbeat HTTPS privacy/failure tests; no external provider is contacted."""
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

import dashboard_collect as health
from modules import heartbeat

ROOT=Path(__file__).resolve().parents[1]
BIN=ROOT/'runtime/bin/nekoserver-runtime'
RUNTIME=Path('/run/nekoserver-heartbeat')


class WorkerTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid()!=0 or RUNTIME.exists(): self.skipTest('Isolated root runtime required')
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.config=self.root/'private.json';self.value={'endpoint':'https://monitor.example/PRIVATE/key'}
        self.config.write_text(json.dumps(self.value));self.config.chmod(0o600)
        self.addCleanup(lambda: __import__('shutil').rmtree(RUNTIME,ignore_errors=True))
        commands=self.root/'commands';commands.mkdir();self.log=self.root/'request'
        script='''#!/usr/bin/python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:];assert args[0]=='--disable';assert '--location' not in args and '--insecure' not in args
assert args[args.index('--proto')+1]=='=https';assert '--max-time' in args
p=Path(args[args.index('--config')+1]);assert p.stat().st_mode&0o777==0o600
Path(os.environ['CAPTURE']).write_text(json.dumps({'args':args,'config':p.read_text()}))
print(os.environ.get('HTTP','200'));sys.exit(int(os.environ.get('EXIT','0')))
'''
        p=commands/'curl';p.write_text(script);p.chmod(0o755)
        self.env={**os.environ,'PATH':str(commands)+':/usr/bin:/bin','CAPTURE':str(self.log)}
    def run_worker(self):
        self.config.write_text(json.dumps(self.value))
        return subprocess.run([str(BIN),'heartbeat','--config',str(self.config)],env=self.env,capture_output=True,text=True,timeout=35)
    def report(self):return json.loads((RUNTIME/'status.json').read_text())
    def test_ping_secret_not_in_args_or_status(self):
        result=self.run_worker();self.assertEqual(result.returncode,0,result.stderr)
        req=json.loads(self.log.read_text());self.assertNotIn('PRIVATE',' '.join(req['args']))
        self.assertNotIn('PRIVATE',json.dumps(self.report())+result.stdout+result.stderr)
        self.assertIn('request = "GET"',req['config']);self.assertNotIn('data-binary',req['config'])
        self.assertFalse((RUNTIME/'request.curl').exists());self.assertFalse(self.report()['provider_notifications_verified'])
    def test_failures_preserve_last_success_and_return_failure(self):
        self.assertEqual(self.run_worker().returncode,0);old=self.report()['last_success']
        for code in ('302','401','429','500'):
            self.env['HTTP']=code;self.assertNotEqual(self.run_worker().returncode,0)
            self.assertEqual(self.report()['status'],'fail');self.assertEqual(self.report()['last_success'],old)
    def test_network_error_not_success(self):
        self.env['EXIT']='7';self.assertNotEqual(self.run_worker().returncode,0);self.assertIsNone(self.report()['last_success'])
    def test_public_config_refused(self):
        self.config.chmod(0o644);self.assertNotEqual(self.run_worker().returncode,0);self.assertFalse(self.log.exists())
    def test_invalid_url_never_contacts_destination(self):
        for value in ('http://monitor/x','https://user@monitor/x','https://monitor/x\nheader=bad','https://monitor/x/fail'):
            self.value['endpoint']=value;self.assertNotEqual(self.run_worker().returncode,0);self.assertFalse(self.log.exists())
    def test_success_after_failure(self):
        self.env['HTTP']='500';self.assertNotEqual(self.run_worker().returncode,0)
        self.env['HTTP']='200';self.assertEqual(self.run_worker().returncode,0);self.assertEqual(self.report()['status'],'pass')
    def test_real_https_and_certificate_validation(self):
        cert,key=self.root/'cert',self.root/'key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost'],check=True,capture_output=True)
        received=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path);self.send_response(200 if self.path=='/ping' else 302);self.send_header('Location','https://localhost:1/leak');self.end_headers()
            def log_message(self,*args):pass
        server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cert,key);server.socket=ctx.wrap_socket(server.socket,server_side=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.env.update(PATH='/usr/bin:/bin',CURL_CA_BUNDLE=str(cert));self.value['endpoint']=f'https://localhost:{server.server_port}/ping'
            self.assertEqual(self.run_worker().returncode,0);self.assertEqual(received,['/ping'])
            self.value['endpoint']=f'https://localhost:{server.server_port}/redirect';self.assertNotEqual(self.run_worker().returncode,0);self.assertEqual(len(received),2)
            self.env.pop('CURL_CA_BUNDLE');self.assertNotEqual(self.run_worker().returncode,0);self.assertEqual(len(received),2)
        finally:server.shutdown();server.server_close();thread.join(2)


class SetupTests(unittest.TestCase):
    def test_https_endpoint_validation(self):
        self.assertTrue(heartbeat.endpoint('https://hc-ping.com/abc'))
        for value in ('https://host:0/x','https://host/x?token=secret','https://host/x/start','http://host/x',None):self.assertFalse(heartbeat.endpoint(value))
    def test_interval_validation(self):
        for value in (True,0,59,3601,'300'):
            with self.assertRaises(RuntimeError):heartbeat.interval({'heartbeat':{'interval_seconds':value}})
    def test_units_do_not_depend_on_ssd_or_dashboard(self):
        units=heartbeat.definitions({},'/runtime','/private')
        self.assertNotIn('RequiresMountsFor',str(units));self.assertNotIn('dashboard',str(units));self.assertNotIn('python',str(units))
        self.assertIn('OnUnitInactiveSec=300s',units['nekoserver-heartbeat.timer'])
    def test_missing_endpoint_defers_without_timer(self):
        with patch.object(heartbeat,'PRIVATE','/nonexistent-neko-fixture/heartbeat.json'),patch.object(heartbeat.sys.stdin,'isatty',return_value=False),patch.object(heartbeat.subprocess,'run') as cmd:
            result=heartbeat.run(SimpleNamespace(config={'runtime':{'backend':'rust'}},apply=True,log=lambda _:None))
        self.assertEqual(result['status'],'needs_setup');cmd.assert_not_called()
    def test_sanitized_dashboard_status_and_staleness(self):
        saved={'format':'neko-heartbeat-v1','status':'pass','last_attempt':100,'last_success':100,'endpoint':'PRIVATE'}
        with patch.object(health,'read_json',return_value=saved),patch.object(health,'timer_status',return_value={'installed':True,'active':True}):
            row=health.heartbeat_status({},120);self.assertEqual(row['status'],'pass');self.assertNotIn('PRIVATE',json.dumps(row))
            self.assertEqual(health.heartbeat_status({},600)['status'],'stale')
    def test_install_preserves_secret_and_private_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);private=root/'private';private.write_text(json.dumps({'endpoint':'https://monitor.example/PRIVATE'}));private.chmod(0o600)
            real_path=Path
            def mapped(value,*args):
                p=real_path(value,*args)
                return root/str(p).lstrip('/') if str(p).startswith('/etc/systemd/system') else p
            with patch.object(heartbeat,'PRIVATE',str(private)),patch.object(heartbeat,'Path',side_effect=mapped),patch.object(heartbeat.native_runtime,'deploy',return_value=root/'runtime'),patch.object(heartbeat.shutil,'which',return_value='/usr/bin/curl'),patch.object(heartbeat.subprocess,'run') as cmd:
                heartbeat.run(SimpleNamespace(config={'runtime':{'backend':'rust'}},apply=True,log=lambda _:None))
            self.assertEqual(private.stat().st_mode&0o777,0o600)
            self.assertIn(['systemctl','enable','--now','nekoserver-heartbeat.timer'],[c.args[0] for c in cmd.call_args_list])
