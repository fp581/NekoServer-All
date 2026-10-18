import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock

import guided_setup as guide
from modules import tailscale, gateway_tailnet, accounts


class GuidedInteractionTests(unittest.TestCase):
    def setUp(self):
        self.ctx = SimpleNamespace(config={}, apply=True, log=Mock())
        tty = patch.object(guide.sys.stdin, 'isatty', return_value=True)
        tty.start(); self.addCleanup(tty.stop)

    def flow(self, answers, **kwargs):
        out = []
        with patch.object(guide, 'launch', return_value=True) as launch:
            done = guide.checkpoint(self.ctx, 'App sign-in', ['Do the external step.'],
                                    [('url', 'https://example.com/')],
                                    prompt=Mock(side_effect=answers), output=out.append, **kwargs)
        return done, launch, out

    def test_no_reopens_and_repeats_instructions(self):
        done, launch, out = self.flow(['no', 'n', 'yes'])
        self.assertTrue(done); self.assertEqual(launch.call_count, 3)
        self.assertEqual(out.count('1. Do the external step.'), 3)
        self.ctx.log.assert_not_called()

    def test_invalid_answer_does_not_relaunch_or_complete(self):
        done, launch, out = self.flow(['maybe', '', 'y'])
        self.assertTrue(done); self.assertEqual(launch.call_count, 1)
        self.assertEqual(out.count('Please type yes or no.'), 2)

    def test_yes_cannot_bypass_machine_verification(self):
        verifier = Mock(side_effect=[False, True])
        done, launch, out = self.flow(['yes', 'yes'], verify=verifier)
        self.assertTrue(done); self.assertEqual(launch.call_count, 2)
        self.assertEqual(verifier.call_count, 2)
        self.assertTrue(any('automatic check has not passed' in line for line in out))

    def test_verifier_error_is_retried(self):
        done, launch, out = self.flow(['yes', 'yes'], verify=Mock(side_effect=[RuntimeError('SECRET'), True]))
        self.assertTrue(done); self.assertEqual(launch.call_count, 2)
        self.assertNotIn('SECRET', '\n'.join(out))

    def test_no_does_not_run_verifier(self):
        verifier = Mock(return_value=True)
        done, _, _ = self.flow(['no', 'yes'], verify=verifier)
        self.assertTrue(done); self.assertEqual(verifier.call_count, 1)

    def test_eof_or_interrupt_never_claims_completion(self):
        for error in (EOFError(), KeyboardInterrupt()):
            done, _, out = self.flow([error])
            self.assertFalse(done)
            self.assertTrue(any('remains pending' in line for line in out))

    def test_headless_readonly_and_disabled_do_not_launch(self):
        for apply, enabled, tty in [(False, True, True), (True, False, True), (True, True, False)]:
            self.ctx.apply = apply; self.ctx.config = {'guided_setup': {'enabled': enabled}}
            with patch.object(guide.sys.stdin, 'isatty', return_value=tty):
                done, launch, _ = self.flow(['yes'])
            self.assertFalse(done); launch.assert_not_called()

    def test_launch_failure_shows_manual_fallback_and_retry(self):
        with patch.object(guide, 'launch', return_value=False) as launch:
            out = []
            done = guide.checkpoint(self.ctx, 'App', ['Open it'], [('url', 'https://example.com/')],
                                    prompt=Mock(side_effect=['no', 'yes']), output=out.append)
        self.assertTrue(done); self.assertEqual(launch.call_count, 2)
        self.assertEqual(out.count('Open this address manually: https://example.com/'), 2)

    def test_prepare_renews_auth_url_on_no_without_logging(self):
        prep = Mock(side_effect=[[('url', 'https://login.tailscale.com/a/one')], [('url', 'https://login.tailscale.com/a/two')]])
        with patch.object(guide, 'launch', return_value=True) as launch:
            self.assertTrue(guide.checkpoint(self.ctx, 'Login', ['Approve'], prepare=prep,
                                             prompt=Mock(side_effect=['no', 'yes']), output=Mock()))
        self.assertEqual([x.args[1] for x in launch.call_args_list], ['https://login.tailscale.com/a/one', 'https://login.tailscale.com/a/two'])
        self.ctx.log.assert_not_called()

    def test_prepare_failure_does_not_leak_or_claim_verification(self):
        out=[]
        self.assertFalse(guide.checkpoint(self.ctx, 'Login', ['Approve'], prepare=Mock(side_effect=RuntimeError('SECRET')),
                                          verify=lambda:False, prompt=Mock(side_effect=['yes', EOFError()]), output=out.append))
        self.assertNotIn('SECRET', '\n'.join(out))


class DesktopSafetyTests(unittest.TestCase):
    def test_reject_unsafe_urls(self):
        for value in ['file:///etc/passwd','javascript:alert(1)','http://example.com/','https://user:password@example.com/','https://example.com/\nabc','steam://run/123']:
            self.assertFalse(guide.valid_url(value),value)
        self.assertTrue(guide.valid_url('http://127.0.0.1:2283/'))
        self.assertTrue(guide.valid_url('https://server.tail123.ts.net:8445/'))

    def test_root_cannot_be_desktop_user(self):
        user=SimpleNamespace(pw_uid=0,pw_name='root',pw_shell='/bin/bash',pw_dir='/root')
        with patch.dict(os.environ,{'SUDO_USER':'root'},clear=True),patch.object(guide.pwd,'getpwuid',return_value=user):
            with self.assertRaisesRegex(RuntimeError,'non-root'):guide.desktop_command({},['firefox'])

    def test_sudo_identity_mismatch_refused(self):
        with patch.dict(os.environ,{'SUDO_USER':'neko','SUDO_UID':'1001'},clear=True),patch.object(guide.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=1000)):
            with self.assertRaisesRegex(RuntimeError,'identity'):guide.desktop_command({},['firefox'])

    def test_launch_is_unprivileged_argv_without_shell(self):
        ctx=SimpleNamespace(apply=True,config={})
        with patch.object(guide,'interactive',return_value=True),patch.object(guide,'desktop_command',side_effect=lambda cfg,args:['runuser','-u','neko','--','env','-i','HOME=/home/neko',*args]),patch.object(guide.shutil,'which',return_value='/fixture'),patch.object(guide.subprocess,'Popen') as proc:
            proc.return_value.wait.return_value=0
            self.assertTrue(guide.launch(ctx,'https://example.com/?value=$(touch-no)'))
        args,kwargs=proc.call_args
        self.assertEqual(args[0][:6],['runuser','-u','neko','--','env','-i'])
        self.assertEqual(args[0][-1],'https://example.com/?value=$(touch-no)')
        self.assertNotIn('shell',kwargs)

    def test_minimal_environment_owned_home_and_runtime(self):
        from neko_common import safe_path
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)/'home';home.mkdir();runtime=Path(tmp)/'runtime';runtime.mkdir();(runtime/'bus').touch()
            user=SimpleNamespace(pw_uid=1000,pw_name='neko',pw_shell='/bin/bash',pw_dir=str(home));original=Path.stat
            def owned(path,*args,**kwargs):
                value=original(path,*args,**kwargs)
                if path not in (home,runtime):return value
                class Owned:
                    st_uid=1000
                    def __getattr__(self,k):return getattr(value,k)
                return Owned()
            with patch.dict(os.environ,{'SUDO_USER':'neko','SUDO_UID':'1000','DISPLAY':':1','PRIVATE_TOKEN':'SECRET'},clear=True),patch.object(guide.pwd,'getpwnam',return_value=user),patch.object(guide,'safe_path',side_effect=lambda p:runtime if str(p)=='/run/user/1000' else safe_path(p)),patch.object(Path,'stat',autospec=True,side_effect=owned):
                cmd=guide.desktop_command({},['/usr/bin/xdg-open','https://example.com/'])
            self.assertIn('DISPLAY=:1',cmd);self.assertIn('DBUS_SESSION_BUS_ADDRESS=unix:path='+str(runtime/'bus'),cmd)
            self.assertNotIn('SECRET',' '.join(cmd));self.assertIn('-i',cmd)


class TailscaleFlowTests(unittest.TestCase):
    def test_json_stream_extracts_official_auth_url(self):
        text=json.dumps({'AuthURL':'https://login.tailscale.com/a/token','QR':'not-for-log'})+'\n'+json.dumps({'Error':'timed out'})
        with patch.object(tailscale.subprocess,'run',return_value=SimpleNamespace(stdout=text)) as run:
            self.assertEqual(tailscale.login_targets(),[('url','https://login.tailscale.com/a/token')])
        self.assertEqual(run.call_args.args[0],['tailscale','up','--json','--timeout=3s'])

    def test_untrusted_auth_host_is_not_opened(self):
        with patch.object(tailscale.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps({'AuthURL':'https://evil.example/a/token'}))),patch.object(tailscale,'connected',return_value=False),patch('builtins.print'):
            self.assertEqual(tailscale.login_targets(),[('url','https://console.tailscale.com/admin/machines')])

    def test_malformed_or_unapproved_backend_not_authenticated(self):
        for text in ('','bad-json','[]','{"BackendState":"NeedsMachineAuth"}'):
            with patch.object(tailscale.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=text)):
                self.assertFalse(tailscale.connected())

    def test_run_checks_after_browser_yes(self):
        ctx=SimpleNamespace(config={},apply=True,log=Mock())
        with patch.object(tailscale,'detect_arch',return_value=True),patch.object(tailscale.os,'geteuid',return_value=0),patch.object(tailscale.shutil,'which',return_value='/fixture'),patch.object(tailscale,'run_cmd'),patch.object(tailscale,'connected',side_effect=[False,True]) as status,patch.object(guide,'interactive',return_value=True),patch.object(tailscale,'login_targets',return_value=[('url','https://login.tailscale.com/a/token')]),patch.object(guide,'launch',return_value=True),patch('builtins.input',return_value='yes'),patch('builtins.print'):
            self.assertIsNone(tailscale.run(ctx))
        self.assertEqual(status.call_count,2)
        self.assertNotIn('token',' '.join(str(c) for c in ctx.log.call_args_list))

    def test_connected_server_not_forced_to_login_again(self):
        ctx=SimpleNamespace(config={},apply=True,log=Mock())
        with patch.object(tailscale,'detect_arch',return_value=True),patch.object(tailscale.os,'geteuid',return_value=0),patch.object(tailscale.shutil,'which',return_value='/fixture'),patch.object(tailscale,'run_cmd'),patch.object(tailscale,'connected',return_value=True),patch.object(guide,'checkpoint') as prompt:
            tailscale.run(ctx)
        prompt.assert_not_called()


class IntegrationTests(unittest.TestCase):
    def test_pending_account_no_reopens_yes_rechecks(self):
        from contextlib import nullcontext
        ctx=SimpleNamespace(config={},apply=True,log=Mock())
        with patch.object(accounts,'lock',side_effect=lambda cfg:nullcontext()),patch.object(accounts.account_setup,'provision',side_effect=[['immich'],[]]) as provision,patch.object(guide,'interactive',return_value=True),patch.object(guide,'launch',return_value=True) as launch,patch('builtins.input',side_effect=['no','yes','yes']),patch('builtins.print'):
            self.assertEqual(accounts.finish_pending(ctx,['immich']),[])
        self.assertEqual(provision.call_count,2);self.assertEqual(launch.call_count,3)

    def gateway(self,confirmed):
        with tempfile.TemporaryDirectory() as tmp:
            ctx=SimpleNamespace(config={'data_mount':tmp},apply=True,log=Mock())
            result={'status':'configured','links':{'immich':'https://node.tail123.ts.net:8445/'}}
            def checkpoint(*args,**kwargs):return kwargs['verify']() if 'verify' in kwargs else confirmed
            with patch.object(gateway_tailnet.gateway_tools,'selected'),patch.object(gateway_tailnet.gateway_tools,'configure',return_value=True),patch.object(gateway_tailnet.gateway_tools,'apply_tailnet',return_value=copy.deepcopy(result)),patch.object(guide,'interactive',return_value=True),patch.object(guide,'checkpoint',side_effect=checkpoint):
                returned=gateway_tailnet.run(ctx)
            return returned,json.loads((Path(tmp)/'reports/gateway-tailnet.json').read_text())

    def test_phone_confirmation_remains_witnessed_evidence(self):
        returned,saved=self.gateway(True)
        self.assertEqual(returned['client_acceptance'],'user_confirmed')
        self.assertIn('no independent phone probe',saved['client_acceptance_evidence'])

    def test_phone_cancel_leaves_routes_configured_and_client_pending(self):
        returned,saved=self.gateway(False)
        self.assertEqual(returned['status'],'needs_setup')
        self.assertEqual(saved['status'],'configured');self.assertEqual(saved['client_acceptance'],'needs_setup')


class AcceptanceAndSecurityTests(unittest.TestCase):
    def test_guided_witness_cannot_override_automatic_failure(self):
        import acceptance
        config={'data_mount':'/srv/nekoserver'}
        report={'automatic':[acceptance.item('disk','Disk','fail','')],
                'manual':[acceptance.item('music_playback','Music','needs_setup','Play music')]}
        with patch.object(acceptance,'load_json',return_value={'links':{'navidrome':'https://node.tail123.ts.net:8446/'}}),patch.object(guide,'checkpoint',return_value=True) as checkpoint:
            acceptance.guided_manual(config,report)
        self.assertEqual(report['manual'][0]['evidence'],'user_reported')
        self.assertFalse(report['acceptance_complete'])
        self.assertIn(('url','https://node.tail123.ts.net:8446/'),checkpoint.call_args.args[3])

    def test_cancelled_witness_stays_pending(self):
        import acceptance
        report={'automatic':[acceptance.item('disk','Disk','pass','')],
                'manual':[acceptance.item('photo_preservation','Photo','needs_setup','Upload')]}
        with patch.object(acceptance,'load_json',return_value={}),patch.object(guide,'checkpoint',return_value=False):
            acceptance.guided_manual({'data_mount':'/srv/nekoserver'},report)
        self.assertEqual(report['manual'][0]['status'],'needs_setup')
        self.assertFalse(report['acceptance_complete'])

    def test_security_no_or_cancel_never_persists_exposure_confirmation(self):
        import gateway_tools
        config=json.loads((Path(__file__).resolve().parents[1]/'config.json').read_text())
        ctx=SimpleNamespace(config=config,apply=True,persist_config=Mock())
        with patch.object(gateway_tools.sys.stdin,'isatty',return_value=True),patch.object(guide,'app_security',return_value=False):
            self.assertFalse(gateway_tools.configure(ctx,'tailnet'))
        self.assertEqual(config['gateway']['confirmed_services'],[])
        ctx.persist_config.assert_not_called()

    def test_security_yes_records_only_selected_services(self):
        import gateway_tools
        config=json.loads((Path(__file__).resolve().parents[1]/'config.json').read_text())
        ctx=SimpleNamespace(config=config,apply=True,persist_config=Mock())
        with patch.object(gateway_tools.sys.stdin,'isatty',return_value=True),patch.object(guide,'app_security',return_value=True),patch('builtins.input',side_effect=AssertionError('duplicate security prompt')):
            self.assertTrue(gateway_tools.configure(ctx,'tailnet'))
        self.assertEqual(config['gateway']['confirmed_services'],config['gateway']['services'])
        ctx.persist_config.assert_called_once()

    def test_app_security_runs_authenticated_checks_after_yes(self):
        from contextlib import nullcontext
        import account_setup
        import recovery_tools
        ctx=SimpleNamespace(config={'accounts':{'enabled':True}},apply=True,log=Mock())
        def checkpoint(*args,**kwargs):return kwargs['verify']()
        with patch.object(guide,'checkpoint',side_effect=checkpoint),patch.object(recovery_tools,'lock',return_value=nullcontext()),patch.object(account_setup,'provision',return_value=[]) as provision:
            self.assertTrue(guide.app_security(ctx,['immich'],{'immich':2283}))
        self.assertEqual(provision.call_args.args[1],['immich'])


class ProviderHandoffTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('guided_media_connector',Path(__file__).resolve().parents[1]/'media-connect.py')
        self.connector=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.connector)
        self.entry={'name':'Owned source','implementation':'OwnedSource','fields':[{'name':'apiKey','label':'API key','required':True}]}

    def test_cancelled_provider_preparation_does_not_send_credentials(self):
        def request(app,key,path,*args):return [self.entry] if path=='/indexer/schema' else []
        with patch.object(self.connector,'app_key',return_value='LOCALKEY'),patch.object(self.connector,'request',side_effect=request) as api,patch.object(self.connector,'choose',return_value=self.entry),patch('builtins.input',return_value=''),patch.object(guide,'interactive',return_value=True),patch.object(guide,'checkpoint',return_value=False),patch.object(self.connector.getpass,'getpass',side_effect=AssertionError('must not collect credentials')),patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError,'preparation pending'):
                self.connector.configure_indexers(Path('/ssd'),True,{})
        self.assertTrue(all(len(c.args)==3 for c in api.call_args_list))

    def test_rejected_provider_can_be_completed_in_ui_and_verified(self):
        count=0
        def request(app,key,path,*args):
            nonlocal count
            if path=='/indexer/schema':return [self.entry]
            if args:raise RuntimeError('Provider rejected')
            count+=1
            return [] if count==1 else [self.entry]
        def checkpoint(*args,**kwargs):return kwargs['verify']() if 'verify' in kwargs else True
        with patch.object(self.connector,'app_key',return_value='LOCALKEY'),patch.object(self.connector,'request',side_effect=request),patch.object(self.connector,'choose',side_effect=[self.entry,None]),patch('builtins.input',return_value=''),patch.object(guide,'checkpoint',side_effect=checkpoint) as handoff,patch.object(self.connector.getpass,'getpass',return_value='PROVIDERKEY'),patch('builtins.print'):
            self.connector.configure_indexers(Path('/ssd'),True,{})
        self.assertEqual(handoff.call_count,2)
        self.assertGreaterEqual(count,3)

    def test_provider_preview_never_launches_handoff(self):
        def request(app,key,path,*args):return [self.entry] if path=='/indexer/schema' else []
        with patch.object(self.connector,'app_key',return_value='LOCALKEY'),patch.object(self.connector,'request',side_effect=request),patch.object(self.connector,'choose',side_effect=[self.entry,None]),patch('builtins.input',return_value=''),patch.object(guide,'checkpoint',side_effect=AssertionError('preview launch')),patch.object(self.connector.getpass,'getpass',return_value='KEY'),patch('builtins.print'):
            self.connector.configure_indexers(Path('/ssd'),False,{})
