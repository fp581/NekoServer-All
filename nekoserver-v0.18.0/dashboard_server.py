#!/usr/bin/env python3
"""Unprivileged loopback-only dashboard. Exact GET routes; no commands or management APIs."""
import argparse,json,os
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from neko_common import safe_path
ASSETS={'/':('index.html','text/html; charset=utf-8'),'/index.html':('index.html','text/html; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/logo.svg':('logo.svg','image/svg+xml')}
CSP="default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; object-src 'none'"

def handler(assets,status,port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,code,body=b'',mime='application/json'):
            self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',CSP);self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer');self.end_headers()
            if self.command!='HEAD':self.wfile.write(body)
        def allowed(self):
            valid={f'127.0.0.1:{port}',f'localhost:{port}'}
            return self.headers.get('Host') in valid and (not self.headers.get('Origin') or self.headers['Origin'] in {'http://'+x for x in valid}) and self.headers.get('Sec-Fetch-Site') not in ('cross-site',)
        def do_GET(self):
            if not self.allowed():self.reply(403);return
            path=urlsplit(self.path).path
            try:
                if path=='/api/status':
                    file=safe_path(status)
                    if file.stat().st_size>2000000:raise ValueError()
                    data=file.read_bytes();value=json.loads(data)
                    if not isinstance(value,dict) or value.get('format')!='neko-dashboard-v1':raise ValueError()
                    self.reply(200,data);return
                if path not in ASSETS:self.reply(404);return
                name,mime=ASSETS[path];self.reply(200,safe_path(assets/name).read_bytes(),mime)
            except (OSError,RuntimeError,ValueError):self.reply(503,b'{"status":"unavailable"}')
        def do_HEAD(self):self.do_GET()
        def do_POST(self):self.reply(405)
        do_PUT=do_POST;do_DELETE=do_POST;do_PATCH=do_POST;do_OPTIONS=do_POST
    return Handler

def server(assets,status,port):return ThreadingHTTPServer(('127.0.0.1',port),handler(assets,status,port))

def main():
    p=argparse.ArgumentParser();p.add_argument('--assets',required=True,type=Path);p.add_argument('--status',default='/run/nekoserver-dashboard/status.json',type=Path);p.add_argument('--port',type=int,default=8787);a=p.parse_args()
    if os.geteuid()==0:raise RuntimeError('Run the HTTP server as an unprivileged user')
    if not 1024<=a.port<=65535:raise RuntimeError('Use an unprivileged dashboard port')
    server(a.assets,a.status,a.port).serve_forever()
if __name__=='__main__':main()
