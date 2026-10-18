import base64,hashlib,http.client,json,os,socket,ssl,subprocess,sys,tempfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));import gateway_tools as g
CADDY=os.environ.get('NEKO_TEST_CADDY','caddy');checks=[]
def check(name,condition):assert condition,name;checks.append({'check':name,'result':'pass'})
def free():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
class Upstream(BaseHTTPRequestHandler):
 protocol_version='HTTP/1.1'
 def log_message(self,*args):pass
 def answer(self,status,body,headers=None):
  self.send_response(status);self.send_header('Content-Length',str(len(body)))
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(body)
 def do_GET(self):
  if self.path=='/protected':self.answer(200 if self.headers.get('Authorization')=='Bearer fixture' else 401,b'auth');return
  if self.path=='/cookie':self.answer(200,b'cookie',{'Set-Cookie':'fixture_session=ok; Path=/; Secure; HttpOnly; SameSite=Lax'});return
  if self.path=='/range':self.answer(206,b'2345',{'Content-Range':'bytes 2-5/10','Accept-Ranges':'bytes'});return
  if self.path=='/socket':
   key=self.headers.get('Sec-WebSocket-Key');accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode();self.send_response(101);self.send_header('Upgrade','websocket');self.send_header('Connection','Upgrade');self.send_header('Sec-WebSocket-Accept',accept);self.end_headers();self.wfile.write(b'\x81\x04neko');self.wfile.flush();self.close_connection=True;return
  self.answer(200,json.dumps({'path':self.path,'proto':self.headers.get('X-Forwarded-Proto'),'host':self.headers.get('Host')}).encode())
 def do_POST(self):
  if self.headers.get('Transfer-Encoding')=='chunked':
   pieces=[]
   while True:
    n=int(self.rfile.readline().strip(),16)
    if not n:self.rfile.readline();break
    pieces.append(self.rfile.read(n));self.rfile.read(2)
   body=b''.join(pieces)
  else:body=self.rfile.read(int(self.headers['Content-Length']))
  self.answer(200,hashlib.sha256(body).hexdigest().encode())
up=ThreadingHTTPServer(('127.0.0.1',0),Upstream);threading.Thread(target=up.serve_forever,daemon=True).start()
try:
 with tempfile.TemporaryDirectory(prefix='neko-caddy-fixture-') as td:
  tmp=Path(td);portal=tmp/'portal';portal.mkdir();(portal/'index.html').write_text('Neko fixture portal');(portal/'private.txt').write_text('PRIVATE');cfg=json.loads((ROOT/'config.json').read_text());cfg['gateway'].update(lan_ip='192.168.1.20',lan_cidr='192.168.1.0/24',services=['jellyfin']);model=g.lan_model(cfg,portal);app_port=free();portal_port=free();deny_port=free()
  for name,server in model['apps']['http']['servers'].items():
   server['listen']=['127.0.0.1:'+str(portal_port if name=='portal' else app_port)];server['routes'][0]['match'][0].update(host=['127.0.0.1'],remote_ip={'ranges':['127.0.0.0/8']})
   if name!='portal':server['routes'][0]['handle'][0]['upstreams'][0]['dial']='127.0.0.1:'+str(up.server_port)
  denied=json.loads(json.dumps(model['apps']['http']['servers']['jellyfin']));denied['listen']=['127.0.0.1:'+str(deny_port)];denied['routes'][0]['match'][0]['remote_ip']['ranges']=['127.0.0.2/32'];model['apps']['http']['servers']['denied']=denied;model['apps']['tls']['automation']['policies'][0]['subjects']=['127.0.0.1'];path=tmp/'proxy.json';path.write_text(json.dumps(model));log=(tmp/'log').open('w');proc=subprocess.Popen([CADDY,'run','--config',str(path)],stdout=log,stderr=log,env={**os.environ,'XDG_DATA_HOME':td,'XDG_CONFIG_HOME':td})
  try:
   ca=tmp/'caddy/pki/authorities/local/root.crt';ready=False
   for i in range(100):
    if proc.poll() is not None:raise AssertionError((tmp/'log').read_text())
    if ca.exists():
     try:context=ssl.create_default_context(cafile=str(ca));conn=http.client.HTTPSConnection('127.0.0.1',app_port,context=context,timeout=2);conn.request('GET','/');res=conn.getresponse();res.read();conn.close();ready=True;break
     except (OSError,ssl.SSLError):pass
    time.sleep(.05)
   check('real Caddy HTTPS starts with trusted fixture CA',ready)
   def request(path='/',method='GET',body=None,headers=None,port=None,chunked=False):
    conn=http.client.HTTPSConnection('127.0.0.1',port or app_port,context=context,timeout=5);conn.request(method,path,body=body,headers=headers or {},encode_chunked=chunked);res=conn.getresponse();out=(res.status,dict(res.getheaders()),res.read());conn.close();return out
   status,headers,body=request('/nested/path?hello=neko');v=json.loads(body);check('root proxy retains paths and query',status==200 and v['path']=='/nested/path?hello=neko');check('proxy supplies HTTPS forwarding protocol',v['proto']=='https')
   check('application unauthenticated response retained',request('/protected')[0]==401);check('application bearer login header retained',request('/protected',headers={'Authorization':'Bearer fixture'})[0]==200)
   status,headers,body=request('/cookie');check('application secure cookie retained',headers['Set-Cookie']=='fixture_session=ok; Path=/; Secure; HttpOnly; SameSite=Lax')
   status,headers,body=request('/range',headers={'Range':'bytes=2-5'});check('media range response and headers retained',status==206 and body==b'2345' and headers['Content-Range']=='bytes 2-5/10')
   payload=b'neko-original-photo-fixture'*50000;check('large original upload transmitted without alteration',request('/upload',method='POST',body=payload)[2].decode()==hashlib.sha256(payload).hexdigest());check('chunked upload transmitted without alteration',request('/upload',method='POST',body=iter([payload[:1000],payload[1000:]]),chunked=True)[2].decode()==hashlib.sha256(payload).hexdigest())
   check('foreign host blocked',request(headers={'Host':'untrusted.invalid'})[0]==403);check('forbidden source blocked despite spoofed forwarding header',request(port=deny_port,headers={'X-Forwarded-For':'127.0.0.2'})[0]==403)
   check('portal served',request(port=portal_port)[2]==b'Neko fixture portal');check('portal cannot serve other files',request('/private.txt',port=portal_port)[0]==403)
   raw=context.wrap_socket(socket.create_connection(('127.0.0.1',app_port),timeout=5),server_hostname='127.0.0.1');raw.sendall(('GET /socket HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n').encode());header=b''
   while not header.endswith(b'\r\n\r\n'):header+=raw.recv(1)
   frame=raw.recv(6);raw.close();check('WebSocket upgrade and binary frame retained',b'101 Switching Protocols' in header and frame==b'\x81\x04neko')
   check('admin API disabled and HTTP/UDP listeners absent from model',model['admin']['disabled'] and all(s['protocols']==['h1','h2'] and s['automatic_https']['disable_redirects'] for s in model['apps']['http']['servers'].values()))
  finally:proc.terminate();proc.wait(timeout=10);log.close()
finally:up.shutdown();up.server_close()
version=subprocess.run([CADDY,'version'],capture_output=True,text=True,check=True).stdout.strip();(ROOT/'audit-evidence/gateway-proxy-checks.json').write_text(json.dumps({'method':'Actual isolated Caddy process + trusted fixture HTTPS CA and disposable HTTP/WebSocket upstream; no production apps or devices','caddy_version':version,'checks':checks},indent=2)+'\n');print(len(checks),'live HTTPS proxy checks passed;',version)
