import http.client,json,os,socket,statistics,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];RUST=Path(os.environ['NEKO_TEST_SERVER_BINARY'])
def port():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
def rss(pid):
 return int(next(s for s in Path(f'/proc/{pid}/status').read_text().splitlines() if s.startswith('VmRSS:')).split()[1])
results={'method':'Three separate process starts; identical localhost assets/status, 20 sequential requests then in-process RSS snapshot through a fixture-only metrics route. Root transport fixtures only because workspace maps no unprivileged UID; production native root refusal tested separately. Not whole-server memory or hardware acceptance.','dashboard':{},'archive':{}}
with tempfile.TemporaryDirectory() as t:
 t=Path(t);measure=t/'measure';subprocess.run(['gcc','-O2','-Wall','-Wextra','-Werror',str(ROOT/'tests/runtime_measure.c'),'-o',str(measure)],check=True);status=t/'status.json';status.write_text(json.dumps({'format':'neko-dashboard-v1','version':'0.15.1'}));samples={'python':[],'rust':[]}
 for n in range(3):
  for kind in ('python','rust'):
   p=port();args=[str(RUST),'serve','--assets',str(ROOT/'dashboard'),'--status',str(status),'--port',str(p)] if kind=='rust' else [sys.executable,'-c',"import sys;sys.path.insert(0,sys.argv[1]);exec(sys.argv[5])",str(ROOT),str(ROOT/'dashboard'),str(status),str(p),'''from dashboard_server import handler
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
base=handler(Path(sys.argv[2]),Path(sys.argv[3]),int(sys.argv[4]))
class FixtureHandler(base):
 def do_GET(self):
  if self.path=='/__fixture_metrics':
   status=Path('/proc/self/status').read_text();rss=int(next(line for line in status.splitlines() if line.startswith('VmRSS:')).split()[1]);self.reply(200,json.dumps({'rss_kib':rss}).encode())
  else:super().do_GET()
ThreadingHTTPServer(('127.0.0.1',int(sys.argv[4])),FixtureHandler).serve_forever()''']
   proc=subprocess.Popen(args,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   try:
    deadline=time.time()+5
    while time.time()<deadline:
     try:
      with socket.create_connection(('127.0.0.1',p),timeout=.1):break
     except OSError:time.sleep(.02)
    for _ in range(20):
     conn=http.client.HTTPConnection('127.0.0.1',p,timeout=5);conn.request('GET','/api/status');response=conn.getresponse();assert response.status==200;response.read();conn.close()
    time.sleep(.1);conn=http.client.HTTPConnection('127.0.0.1',p,timeout=5);conn.request('GET','/__fixture_metrics');response=conn.getresponse();samples[kind].append(json.loads(response.read())['rss_kib']);conn.close()
   finally:proc.terminate();proc.wait(timeout=5)
 for kind,values in samples.items():results['dashboard'][kind]={'rss_kib_samples':values,'median_rss_kib':statistics.median(values)}
 results['dashboard']['reduction_percent']=round(100*(1-results['dashboard']['rust']['median_rss_kib']/results['dashboard']['python']['median_rss_kib']),1)
 source=t/'immich/upload/user';source.mkdir(parents=True);photo=source/'original.mp4'
 with photo.open('wb') as out:
  chunk=b'Neko'*262144
  for _ in range(64):out.write(chunk)
 os.utime(photo,(time.time()-1200,)*2);samples={'python':[],'rust':[]}
 for n in range(3):
  for kind in ('python','rust'):
   archive=t/(kind+str(n));record=t/'memory.txt';args=[str(ROOT/'runtime/bin/nekoserver-runtime'),'archive','--source',str(t/'immich'),'--archive',str(archive),'--apply'] if kind=='rust' else [sys.executable,str(ROOT/'photo-preserve.py'),'--source',str(t/'immich'),'--archive',str(archive),'--apply']
   started=time.perf_counter();subprocess.run([str(measure),str(record),*args],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE);elapsed=time.perf_counter()-started;assert (archive/'upload/user/original.mp4').stat().st_size==photo.stat().st_size;samples[kind].append({'peak_rss_kib':int(record.read_text()),'elapsed_seconds':round(elapsed,4)})
 for kind,values in samples.items():results['archive'][kind]={'samples':values,'median_peak_rss_kib':statistics.median(v['peak_rss_kib'] for v in values),'median_elapsed_seconds':statistics.median(v['elapsed_seconds'] for v in values)}
 results['archive']['source_bytes']=photo.stat().st_size;results['archive']['note']='64 MiB disposable file on workspace filesystem, repeated with warm cache; timings do not predict HDD performance. Production native archive executable, original Python archive command.'
(ROOT/'audit-evidence/runtime-benchmark.json').write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
