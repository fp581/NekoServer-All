import copy,hashlib,json,os,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from contextlib import ExitStack
from unittest.mock import patch
import gateway_tools as g
import network_access as net
import gateway_cert as cert
import gateway_checks
import installer
from modules import gateway_lan,gateway_tailnet,samba,recovery
ROOT=Path(__file__).resolve().parents[1];CONFIG=json.loads((ROOT/'config.json').read_text())
NODE='neko.tail123.ts.net'
class GatewayFixture(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.root.chmod(0o755);self.config=copy.deepcopy(CONFIG);self.config.update(data_mount=str(self.root/'ssd'),media_mount=str(self.root/'hdd'),external_backup_mount=str(self.root/'usb'));self.config['docker']['persistent_root']=self.config['data_mount'];self.config['gateway'].update(lan_ip='192.168.1.20',lan_cidr='192.168.1.0/24',confirmed_services=self.config['gateway']['services']);self.ssd=Path(self.config['data_mount']);self.ssd.mkdir();Path(self.config['media_mount']).mkdir()
 def tearDown(self):self.tmp.cleanup()

class GatewayTests(GatewayFixture):
 def test_selected_management_panels_rejected(self):
  for app in ('sonarr','radarr','qbittorrent','dashboard','../secret'):
   self.config['gateway']['services']=[app]
   with self.assertRaises(RuntimeError):g.selected(self.config)
 def test_unknown_duplicate_or_disabled_service_rejected(self):
  for apps in ([],['jellyfin','jellyfin'],None):
   self.config['gateway']['services']=apps
   with self.assertRaises((RuntimeError,TypeError)):g.selected(self.config)
  self.config['gateway']['services']=['immich'];self.config['photos']['deploy_immich']=False
  with self.assertRaises(RuntimeError):g.selected(self.config)
 def test_disabled_media_service_rejected(self):
  self.config['gateway']['services']=['navidrome'];self.config['media']['services']=['jellyfin']
  with self.assertRaises(RuntimeError):g.selected(self.config)
 def test_private_ip_rejects_public_loopback_tailnet_and_injection(self):
  for ip in ('0.0.0.0','127.0.0.1','100.64.0.1','8.8.8.8','::1','192.168.1.1;id','192.168.1.1\n'):
   with self.assertRaises(RuntimeError):net.private_ip(ip)
 def test_rfc1918_addresses_accepted(self):
  for ip in ('10.0.0.2','172.16.0.2','192.168.1.20'):self.assertEqual(net.private_ip(ip),ip)
 def test_subnet_rejects_noncanonical_broad_wrong_or_reserved(self):
  for ip,cidr in [('192.168.1.20','192.168.1.20/24'),('10.0.0.2','10.0.0.0/8'),('192.168.1.20','192.168.2.0/24'),('192.168.1.0','192.168.1.0/24'),('192.168.1.255','192.168.1.0/24')]:
   self.config['gateway'].update(lan_ip=ip,lan_cidr=cidr)
   with self.assertRaises(RuntimeError):g.lan(self.config)
 def test_virtual_tailnet_and_loopback_candidates_excluded(self):
  rows=[{'ifname':name,'addr_info':[{'family':'inet','scope':'global','local':'192.168.1.20','prefixlen':24}]} for name in ('lo','docker0','br-123','tailscale0','veth123','virbr0','enp3s0')]
  with patch.object(net,'command',return_value=json.dumps(rows)):self.assertEqual([r['interface'] for r in net.candidates()],['enp3s0'])
 def test_inactive_or_different_address_blocks_lan_guard(self):
  with patch.object(net,'candidates',return_value=[]):
   with self.assertRaises(RuntimeError):net.verify_lan(self.config)
 def test_matching_physical_subnet_passes_guard(self):
  with patch.object(net,'candidates',return_value=[{'ip':'192.168.1.20','cidr':'192.168.1.0/24'}]):net.verify_lan(self.config)
 def test_lan_render_has_no_wildcards_admin_public_ca_or_redirects(self):
  model=g.lan_model(self.config,Path('/portal'));self.assertTrue(model['admin']['disabled']);self.assertFalse(model['apps']['pki']['certificate_authorities']['local']['install_trust']);self.assertEqual(model['apps']['tls']['automation']['policies'][0]['issuers'],[{'module':'internal','ca':'local'}]);self.assertNotIn('0.0.0.0',json.dumps(model));self.assertNotIn('8096:8096',json.dumps(model))
  for v in model['apps']['http']['servers'].values():self.assertTrue(v['listen'][0].startswith('192.168.1.20:'));self.assertEqual(v['protocols'],['h1','h2']);self.assertTrue(v['automatic_https']['disable_redirects']);self.assertEqual(v['routes'][0]['match'][0]['remote_ip']['ranges'],['192.168.1.0/24'])
 def test_all_proxies_point_only_to_expected_loopback_apps(self):
  model=g.lan_model(self.config,Path('/portal'));targets=[v['routes'][0]['handle'][0]['upstreams'][0]['dial'] for name,v in model['apps']['http']['servers'].items() if name!='portal'];self.assertEqual(set(targets),{'127.0.0.1:8096','127.0.0.1:5055','127.0.0.1:2283','127.0.0.1:4533','127.0.0.1:8088'})
 def test_portal_serves_only_index_paths(self):self.assertEqual(g.lan_model(self.config,Path('/portal'))['apps']['http']['servers']['portal']['routes'][0]['match'][0]['path'],['/','/index.html'])
 def test_portal_has_selected_https_links_and_unchanged_cat(self):
  text=g.portal(self.config,'192.168.1.20');self.assertIn('https://192.168.1.20:8445/',text);self.assertIn((ROOT/'dashboard/logo.svg').read_text(),text);self.assertNotIn('9696',text);self.assertNotIn('password',text.lower())
 def test_unconfirmed_accounts_block_readiness(self):
  self.config['gateway']['confirmed_services']=[]
  with self.assertRaisesRegex(RuntimeError,'confirmation'):g.readiness(self.config)
 def test_noninteractive_unconfigured_lan_returns_pending_without_writes(self):
  self.config['gateway'].update(lan_ip='auto',lan_cidr='auto',confirmed_services=[]);ctx=SimpleNamespace(config=self.config,config_path=None)
  with patch.object(g.sys.stdin,'isatty',return_value=False):self.assertFalse(g.configure(ctx,'lan'))
  self.assertEqual(list(self.ssd.iterdir()),[])
 def test_existing_confirmed_configuration_never_prompts(self):
  with patch('builtins.input',side_effect=AssertionError('prompt')):self.assertTrue(g.configure(SimpleNamespace(config=self.config,config_path=None),'lan'))
 def test_dry_run_does_not_probe_or_install(self):
  ctx=SimpleNamespace(config=self.config,apply=False,log=lambda _:None)
  with patch.object(g,'apply_lan',side_effect=AssertionError('install')),patch.object(g,'apply_tailnet',side_effect=AssertionError('remote')):gateway_lan.run(ctx);gateway_tailnet.run(ctx)
 def test_profiles_keep_tailscale_optional_and_gateway_after_storage(self):
  mods=installer.load_modules(ROOT/'modules');lan=installer.ordered(mods,CONFIG['profiles']['lan_access']);remote=installer.ordered(mods,CONFIG['profiles']['remote_access']);full=installer.ordered(mods,CONFIG['profiles']['full']);self.assertNotIn('tailscale',lan);self.assertNotIn('tailscale',full);self.assertLess(lan.index('storage'),lan.index('gateway_lan'));self.assertLess(remote.index('tailscale'),remote.index('gateway_tailnet'))
 def test_port_collision_refused_before_listener_changes(self):
  from unittest.mock import MagicMock
  probe=MagicMock();probe.__enter__.return_value.bind.side_effect=OSError('busy')
  with patch.object(g.socket,'socket',return_value=probe),patch.object(g.health,'run',return_value=(1,'')):
   with self.assertRaisesRegex(RuntimeError,'unavailable'):g.port_preflight(self.config)
 def test_service_has_unprivileged_user_guard_and_persistent_tls_state(self):
  text=g.unit(self.config,Path('/code'),self.ssd/'gateway/caddy-state');self.assertIn('User=caddy',text);self.assertIn('ExecStartPre=+',text);self.assertIn('ReadWritePaths='+str(self.ssd/'gateway/caddy-state'),text);self.assertIn('AF_NETLINK',text);self.assertIn('InaccessiblePaths=-/run/docker.sock',text)
 def test_foreign_file_or_custom_edit_preserved(self):
  path=self.root/'foreign';path.write_text('CUSTOM')
  with self.assertRaises(RuntimeError):g.preflight({path:'new'},self.root/'ledger.json')
  self.assertEqual(path.read_text(),'CUSTOM')
 def test_only_recorded_managed_file_can_change(self):
  path=self.root/'managed';path.write_text('old');ledger=self.root/'ledger.json';ledger.write_text(json.dumps({'sha256':g.hashes({path:'old'})}));g.preflight({path:'new'},ledger);path.write_text('CUSTOM')
  with self.assertRaises(RuntimeError):g.preflight({path:'new'},ledger)
 def test_symlink_refused_before_gateway_write(self):
  path=self.root/'link';path.symlink_to(self.root/'target')
  with self.assertRaises(RuntimeError):g.preflight({path:'new'},self.root/'ledger')
 def test_samba_lan_does_not_reference_tailnet_or_allow_guests(self):
  opts={'share_mode':'lan','lan_ip':'192.168.1.20','lan_cidr':'192.168.1.0/24','shares':{'Photos':'photos/archive'}};text=samba.share_config(opts,Path('/media'),'tester');self.assertIn('interfaces = lo 192.168.1.20',text);self.assertIn('hosts allow = 127. 192.168.1.0/24',text);self.assertIn('read only = yes',text);self.assertNotIn('tailscale',text);self.assertIn('guest ok = no',text)
 def test_samba_invalid_mode_or_wildcard_rejected(self):
  for mode,ip in [('public','192.168.1.20'),('lan','0.0.0.0')]:
   with self.assertRaises(RuntimeError):samba.share_config({'share_mode':mode,'lan_ip':ip,'lan_cidr':'192.168.1.0/24'},Path('/media'),'tester')
 def test_preserved_bundle_contains_gateway_runtime(self):
  with patch.object(recovery.os.path,'ismount',return_value=True):saved=recovery.preserve(self.ssd,ROOT,self.config)
  for name in ('gateway_tools.py','gateway_guard.py','network_access.py','gateway_checks.py','gateway_cert.py'):self.assertTrue((saved/name).is_file())
 def test_export_rechecks_mount_and_never_reads_keys(self):
  with patch.object(cert.recovery_tools,'mounts',side_effect=RuntimeError('drive')):
   with self.assertRaises(RuntimeError):cert.export(self.config,self.root/'cert.crt')
  self.assertFalse((self.root/'cert.crt').exists())
 def test_export_rejects_private_key_content(self):
  source=self.ssd/'gateway/caddy-state/caddy/pki/authorities/local/root.crt';source.parent.mkdir(parents=True);source.write_text('PRIVATE KEY')
  with patch.object(cert.recovery_tools,'mounts'):
   with self.assertRaises(RuntimeError):cert.export(self.config,self.root/'cert.crt')
 def test_export_refuses_existing_destination(self):
  source=self.ssd/'gateway/caddy-state/caddy/pki/authorities/local/root.crt';source.parent.mkdir(parents=True);source.write_text('fixture');target=self.root/'cert.crt';target.write_text('KEEP')
  with patch.object(cert.recovery_tools,'mounts'),patch.object(cert.ssl,'PEM_cert_to_DER_cert',return_value=b'fixture'):
   with self.assertRaises(FileExistsError):cert.export(self.config,target)
  self.assertEqual(target.read_text(),'KEEP')
 def test_export_public_cert_fingerprint_and_permissions(self):
  source=self.ssd/'gateway/caddy-state/caddy/pki/authorities/local/root.crt';source.parent.mkdir(parents=True);source.write_text('fixture');target=self.root/'cert.crt'
  with patch.object(cert.recovery_tools,'mounts'),patch.object(cert.ssl,'PEM_cert_to_DER_cert',return_value=b'fixture'):digest=cert.export(self.config,target)
  self.assertEqual(digest,hashlib.sha256(b'fixture').hexdigest());self.assertEqual(target.stat().st_mode&0o777,0o644)

class TailnetTests(GatewayFixture):
 def test_canonical_rejects_funnel_foreground_service_or_unknown_routes(self):
  for v in ({'AllowFunnel':{NODE+':443':True}},{'Foreground':{'x':{}}},{'Services':{'svc:x':{}}},{'UNKNOWN':True}):
   with self.assertRaises(RuntimeError):g.canonical(v)
 def test_canonical_accepts_empty_config_only_known_background_shape(self):self.assertEqual(g.canonical({}),{'TCP':{},'Web':{}})
 def test_tailnet_routes_are_https_root_proxies_without_funnel(self):
  v=g.desired(NODE,['jellyfin','immich']);self.assertEqual(v['TCP'],{'8443':{'HTTPS':True},'8445':{'HTTPS':True}});self.assertEqual(v['Web'][NODE+':8445']['Handlers']['/']['Proxy'],'http://127.0.0.1:2283');self.assertNotIn('AllowFunnel',v)
 def apply_fixture(self,current=None,saved=None,fail_port=None):
  self.state=copy.deepcopy(current or {'TCP':{},'Web':{}});self.calls=[]
  if saved:
   path=self.ssd/'gateway/tailnet-owned.json';path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps({'format':'neko-tailnet-owned-v1',**saved}))
  def cmd(args,timeout=30,env=None):
   self.calls.append(args)
   if args[0]=='docker':return '["localhost","127.0.0.1"]' if 'config:system:get' in args else ''
   if args[:3]==['tailscale','serve','status']:return json.dumps(self.state)
   if args[:3]==['tailscale','status','--json']:return json.dumps({'BackendState':'Running','Self':{'DNSName':NODE+'.'}})
   port=next(x.split('=',1)[1] for x in args if x.startswith('--https='));key=NODE+':'+port
   if args[-1]=='off':self.state['TCP'].pop(port,None);self.state['Web'].pop(key,None);return ''
   self.state['TCP'][port]={'HTTPS':True};self.state['Web'][key]={'Handlers':{'/':{'Proxy':args[-1]}}}
   if fail_port==port:raise RuntimeError('PRIVATE ERROR')
   return ''
  with patch.object(g,'command',side_effect=cmd),patch.object(g,'readiness'),patch.object(g.recovery_tools,'mounts'),patch.object(g.recovery_tools,'require_root'):return g._apply_tailnet(self.config)
 def test_first_apply_owns_routes_without_reset_or_funnel(self):
  result=self.apply_fixture();self.assertEqual(result['services'],g.selected(self.config));self.assertEqual(self.state,g.desired(NODE,g.selected(self.config)));self.assertFalse(any('reset' in c or 'funnel' in c for c in self.calls));self.assertEqual((self.ssd/'gateway/tailnet-owned.json').stat().st_mode&0o777,0o600)
 def test_rerun_does_not_reconfigure_correct_routes(self):
  owned=g.desired(NODE,g.selected(self.config));self.apply_fixture(owned,{'host':NODE,'owned':owned,'status':'configured'});self.assertFalse(any('--bg' in c for c in self.calls))
 def test_foreign_route_preserved_without_mutation(self):
  foreign=g.desired(NODE,['jellyfin']);foreign['Web'][NODE+':8443']['Handlers']['/']['Proxy']='http://127.0.0.1:9999'
  with self.assertRaises(RuntimeError):self.apply_fixture(foreign)
  self.assertEqual(self.state,foreign);self.assertFalse(any('--bg' in c for c in self.calls));self.assertFalse((self.ssd/'gateway/tailnet-owned.json').exists())
 def test_removed_selection_turns_off_only_previous_owned_ports(self):
  owned=g.desired(NODE,g.selected(self.config));self.config['gateway']['services']=['jellyfin'];self.apply_fixture(owned,{'host':NODE,'owned':owned,'status':'configured'});self.assertEqual(self.state,g.desired(NODE,['jellyfin']))
 def test_partial_failure_turns_off_recognizable_owned_routes(self):
  with self.assertRaisesRegex(RuntimeError,'setup failed'):self.apply_fixture(fail_port='8444')
  self.assertEqual(self.state,{'TCP':{},'Web':{}});self.assertEqual(json.loads((self.ssd/'gateway/tailnet-owned.json').read_text())['status'],'pending')
 def test_changed_tailnet_identity_preserved_for_manual_review(self):
  with self.assertRaises(RuntimeError):self.apply_fixture(saved={'host':'other.tail.ts.net','owned':g.desired(NODE,['jellyfin'])})
  self.assertFalse(any('--bg' in c for c in self.calls))
 def test_unknown_or_malformed_owned_ports_cannot_be_adopted(self):
  self.assertFalse(g.gateway_owned(NODE,{'TCP':{'443':{'HTTPS':True}},'Web':{}}));self.assertFalse(g.gateway_owned(NODE,{'TCP':None,'Web':{}}))
 def test_unexpected_serve_state_cannot_pass_readiness(self):
  with patch.object(g,'tailnode',return_value=NODE),patch.object(g,'serve_state',return_value=g.desired(NODE,['jellyfin'])):self.assertNotEqual(gateway_checks.check(self.config,'tailnet')[0]['status'],'pass')

class LanModuleTests(GatewayFixture):
 def setUp(self):
  super().setUp();self.stack=ExitStack();self.calls=[];real=Path
  prefixes=('/etc/nekoserver-gateway','/etc/systemd/system','/usr/local/lib/nekoserver-gateway','/usr/local/share/nekoserver-gateway')
  def mapped(value,*args):
   p=real(value,*args)
   return self.root/'os'/str(p).lstrip('/') if any(str(p)==n or str(p).startswith(n+'/') for n in prefixes) else p
  owners={};realstat=Path.stat
  def fake_stat(path,*args,**kwargs):
   result=realstat(path,*args,**kwargs)
   if str(path) in owners:
    values=list(result);values[4],values[5]=owners[str(path)];return os.stat_result(values)
   return result
  self.stack.enter_context(patch.object(Path,'stat',fake_stat));self.stack.enter_context(patch.object(g.os,'chown',side_effect=lambda path,uid,gid:owners.update({str(path):(uid,gid)})))
  self.stack.enter_context(patch.object(g,'Path',side_effect=mapped));self.stack.enter_context(patch.object(g,'readiness'));self.stack.enter_context(patch.object(g,'port_preflight'));self.stack.enter_context(patch.object(g,'verify_lan'));self.stack.enter_context(patch.object(g.recovery_tools,'mounts'));self.stack.enter_context(patch.object(g.recovery_tools,'require_root'));self.stack.enter_context(patch.object(g.shutil,'which',return_value='/usr/bin/fixture'));self.stack.enter_context(patch.object(g.pwd,'getpwnam',return_value=SimpleNamespace(pw_uid=os.getuid() or 65534,pw_gid=os.getgid() or 65534)));self.stack.enter_context(patch.object(g,'command',side_effect=lambda args,**kwargs:self.calls.append(args) or ('["localhost","127.0.0.1"]' if 'config:system:get' in args else '')))
 def tearDown(self):self.stack.close();super().tearDown()
 def apply(self):return g._apply_lan(self.config)
 def test_lan_installs_private_sanitized_guard_and_selected_service_only(self):
  self.config['password']='PRIVATE';result=self.apply();cfg=self.root/'os/etc/nekoserver-gateway/storage.json';self.assertEqual(cfg.stat().st_mode&0o777,0o600);self.assertNotIn('PRIVATE',cfg.read_text());self.assertEqual(result['status'],'configured');self.assertIn(['systemctl','restart','nekoserver-gateway-lan.service'],self.calls);self.assertFalse(any('tailscale' in x for c in self.calls for x in c));code=self.root/('os/usr/local/lib/nekoserver-gateway/v'+g.VERSION);self.assertTrue((code/'network_access.py').exists())
 def test_rerun_keeps_ca_state(self):
  self.apply();state=self.ssd/'gateway/caddy-state';marker=state/'fixture.key';marker.write_text('KEEP');self.apply();self.assertEqual(marker.read_text(),'KEEP')
 def test_custom_config_preserved_before_commands(self):
  self.apply();cfg=self.root/'os/etc/nekoserver-gateway/lan.json';cfg.write_text('CUSTOM');self.calls=[]
  with self.assertRaises(RuntimeError):self.apply()
  self.assertFalse(self.calls);self.assertEqual(cfg.read_text(),'CUSTOM')
 def test_wrong_persistent_state_owner_refuses_restart(self):
  state=self.ssd/'gateway/caddy-state';state.mkdir(parents=True);os.chown(state,12345,12345) if os.geteuid()==0 else None
  if os.geteuid()!=0:self.skipTest('Wrong-owner fixture needs root')
  with self.assertRaisesRegex(RuntimeError,'owner'):self.apply()
  self.assertFalse(any(c[0]=='systemctl' for c in self.calls))
 def test_invalid_caddy_configuration_does_not_replace_installed_files(self):
  self.apply();cfg=self.root/'os/etc/nekoserver-gateway/lan.json';before=cfg.read_bytes();self.config['gateway']['services']=['jellyfin'];self.config['gateway']['confirmed_services']=['jellyfin'];self.calls=[]
  with patch.object(g,'command',side_effect=RuntimeError('fixture invalid')):
   with self.assertRaises(RuntimeError):self.apply()
  self.assertEqual(cfg.read_bytes(),before)
 def test_failed_restart_stops_only_our_gateway(self):
  def cmd(args,**kwargs):
   self.calls.append(args)
   if args[0]=='docker':return '["localhost","127.0.0.1"]' if 'config:system:get' in args else ''
   if args==['systemctl','restart','nekoserver-gateway-lan.service']:raise RuntimeError('fixture startup failure')
   return ''
  with patch.object(g,'command',side_effect=cmd):
   with self.assertRaises(RuntimeError):self.apply()
  self.assertIn(['systemctl','stop','nekoserver-gateway-lan.service'],self.calls);self.assertFalse(any(c[0]=='docker' and any(action in c for action in ('stop','down','rm')) for c in self.calls))

if __name__=='__main__':unittest.main()

class ReleaseWiringTests(unittest.TestCase):
 def test_full_onboarding_precedes_gateway_and_tailscale_is_optional(self):
  plan=installer.ordered(installer.load_modules(ROOT/'modules'),CONFIG['profiles']['full'])
  self.assertLess(plan.index('onboarding'),plan.index('gateway_lan'));self.assertNotIn('tailscale',plan)
  self.assertEqual(CONFIG['profiles']['network'],CONFIG['profiles']['networking'])
 def test_startup_guard_imports_from_only_installed_payloads(self):
  import shutil,subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   for name in ('gateway_guard.py','network_access.py','storage-guard.py','neko_common.py'):shutil.copyfile(ROOT/name,Path(tmp)/name)
   result=subprocess.run([sys.executable,str(Path(tmp)/'gateway_guard.py'),'--help'],cwd=tmp,capture_output=True,text=True,env={'PATH':'/usr/bin:/bin'})
   self.assertEqual(result.returncode,0,result.stderr)
