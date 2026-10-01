"""Separate HTTP reader and same-origin static host."""
import argparse
from contextlib import closing
import logging
import sqlite3
import hmac
import json
import mimetypes
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit
from .recall import LIMITS, Reader, RecallError, parse

class ReaderServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,reader,tokens,static_root=None):
        self.reader,self.tokens=reader,tokens
        self.static_root=Path(static_root).resolve() if static_root else None
        self.slots=threading.BoundedSemaphore(LIMITS['concurrent_reads'])
        super().__init__(address,Handler)

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,status,value):
        data=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','private, no-store'); self.send_header('Content-Length',str(len(data)))
        if status==503: self.send_header('Retry-After','1')
        if status==401: self.send_header('WWW-Authenticate','Bearer')
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        try:
            u=urlsplit(self.path)
            if u.path=='/healthz':
                with closing(self.server.reader.connection()) as conn: conn.execute('SELECT 1')
                self.send(200,dict(status='ready')); return
            if not u.path.startswith('/v1/recall/'): self.static(u.path); return
            auth=self.headers.get_all('Authorization',[])
            token=auth[0][7:] if len(auth)==1 and auth[0].startswith('Bearer ') else ''
            role=next((role for key,role in self.server.tokens.items() if hmac.compare_digest(key,token)),None)
            if role is None: raise RecallError(401,'unauthorized','Read credential required')
            if role!='owner': raise RecallError(403,'forbidden','Credential has no read role')
            route=u.path.removeprefix('/v1/recall/')
            if route not in ('info','transcript','timeline','sessions','spans','search'): raise RecallError(404,'not_found','Unknown route')
            p=parse(route,parse_qsl(u.query,keep_blank_values=True))
            if not self.server.slots.acquire(blocking=False): raise RecallError(503,'read_budget_exhausted','Reader is busy; retry')
            try: result=self.server.reader.read(route,p)
            finally: self.server.slots.release()
            self.send(200,result)
        except RecallError as e:
            error=dict(code=e.code,message=e.message)
            if e.field: error['field']=e.field
            self.send(e.status,dict(error=error))
        except (BrokenPipeError,ConnectionResetError): pass
        except sqlite3.Error:
            logging.getLogger(__name__).exception('Reader database failure')
            self.send(503,dict(error=dict(code='read_unavailable',message='Reader database unavailable')))
    def static(self,path):
        root=self.server.static_root
        if root is None: raise RecallError(404,'not_found','No static app configured')
        target=(root/unquote(path).lstrip('/')).resolve()
        if not target.is_relative_to(root): raise RecallError(404,'not_found','File not found')
        if target.is_dir(): target=(target/'index.html').resolve()
        if not target.is_relative_to(root): raise RecallError(404,'not_found','File not found')
        if not target.is_file(): raise RecallError(404,'not_found','File not found')
        data=target.read_bytes(); self.send_response(200)
        self.send_header('Content-Type',mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length',str(len(data))); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data)

def main(argv=None):
    p=argparse.ArgumentParser(prog='roomlog-reader'); p.add_argument('--config',required=True)
    args=p.parse_args(argv)
    with Path(args.config).expanduser().open('rb') as f: cfg=tomllib.load(f)
    with Path(cfg['tokens_file']).expanduser().open('rb') as f: tokens=tomllib.load(f).get('tokens',{})
    if not tokens or any(not isinstance(k,str) or not k or v not in ('owner','device') for k,v in tokens.items()): raise ValueError('Invalid reader credential roles')
    labels=cfg.get('sources',{})
    if any(not isinstance(v,dict) or v.get('display_as','device') not in ('device','room') or not isinstance(v.get('label',''),str) for v in labels.values()): raise ValueError('Invalid source presentation')
    host,port=cfg.get('bind','127.0.0.1:8540').rsplit(':',1)
    server=ReaderServer((host,int(port)),Reader(Path(cfg['db_path']).expanduser(),labels),tokens,Path(cfg['static_root']).expanduser() if cfg.get('static_root') else None)
    try: server.serve_forever()
    finally: server.server_close()
