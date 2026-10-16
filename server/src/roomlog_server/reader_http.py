"""Separate HTTP reader and same-origin static host."""
import argparse
import gzip
import socket
from contextlib import closing
import logging
import sqlite3
import hmac
import json
import mimetypes
import os
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

def accepts_gzip(header):
    encodings = {}
    for part in header.split(','):
        name, *parameters = part.strip().lower().split(';')
        quality = 1.0
        for parameter in parameters:
            if parameter.strip().startswith('q='):
                try: quality = float(parameter.strip()[2:])
                except ValueError: quality = 0.0
        encodings[name] = quality
    return 0 < encodings.get('gzip', encodings.get('*', 0)) <= 1


AUDIO_CACHE='private, max-age=31536000, immutable'


def byte_range(header,size):
    """(first,last) for one satisfiable `bytes=` range; None to ignore the header (RFC 9110 14.2
    lets a server ignore multiple or malformed ranges and send the whole file)."""
    unit,_,spec=header.partition('=')
    first,dash,last=spec.strip().partition('-')
    if unit.strip().lower()!='bytes' or ',' in spec or not dash: return None
    if any(v and not (v.isascii() and v.isdigit() and len(v)<=19) for v in (first,last)): return None
    if not first:
        if not last: return None
        n=int(last)
        if n==0 or size==0: raise RecallError(416,'range_not_satisfiable','Requested range not satisfiable')
        return max(0,size-n),size-1
    a=int(first); b=int(last) if last else max(a,size-1)
    if b<a: return None
    if a>=size: raise RecallError(416,'range_not_satisfiable','Requested range not satisfiable')
    return a,min(b,size-1)


def reader_archive_dir(cfg):
    """`archive_dir` from reader.toml, else the server's default beside the database."""
    if cfg.get('archive_dir'): return Path(cfg['archive_dir']).expanduser().resolve()
    return (Path(cfg['db_path']).expanduser().parent/'archive').resolve()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def setup(self):
        super().setup()
        self.connection.settimeout(10)
        self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    def body_headers(self, data):
        if len(data) >= 1024 and accepts_gzip(self.headers.get('Accept-Encoding', '')):
            data = gzip.compress(data, compresslevel=1)
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Vary', 'Accept-Encoding')
        self.send_header('Content-Length', str(len(data)))
        return data
    def log_message(self,*args): pass
    def send(self,status,value,headers=()):
        data=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8')
        for name,header in headers: self.send_header(name,header)
        self.send_header('Cache-Control','private, no-store'); data=self.body_headers(data)
        if status==503: self.send_header('Retry-After','1')
        if status==401: self.send_header('WWW-Authenticate','Bearer')
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        try:
            if len(self.path)>8192: raise RecallError(400,'invalid_parameter','Request URL too long')
            u=urlsplit(self.path)
            if u.path=='/healthz':
                with closing(self.server.reader.connection()) as conn: conn.execute('SELECT 1')
                self.send(200,dict(status='ready')); return
            if not u.path.startswith('/v1/recall/'): self.static(u.path); return
            auth=self.headers.get_all('Authorization',[])
            token=auth[0][7:] if len(auth)==1 and auth[0].startswith('Bearer ') else ''
            role=next((role for key,role in self.server.tokens.items() if hmac.compare_digest(key.encode(),token.encode())),None)
            if role is None: raise RecallError(401,'unauthorized','Read credential required')
            if role!='owner': raise RecallError(403,'forbidden','Credential has no read role')
            route=u.path.removeprefix('/v1/recall/')
            if route not in ('info','transcript','timeline','sessions','spans','search','audio'): raise RecallError(404,'not_found','Unknown route')
            p=parse(route,parse_qsl(u.query,keep_blank_values=True))
            if not self.server.slots.acquire(blocking=False): raise RecallError(503,'read_budget_exhausted','Reader is busy; retry')
            try: result=self.server.reader.audio(p['chunk_id']) if route=='audio' else self.server.reader.read(route,p)
            finally: self.server.slots.release()
            if route=='audio': self.audio(*result); return
            self.send(200,result)
        except RecallError as e:
            error=dict(code=e.code,message=e.message)
            if e.field: error['field']=e.field
            self.send(e.status,dict(error=error),getattr(e,'headers',()))
        except (BrokenPipeError,ConnectionResetError): pass
        except sqlite3.Error:
            logging.getLogger(__name__).exception('Reader database failure')
            self.send(503,dict(error=dict(code='read_unavailable',message='Reader database unavailable')))
    def audio(self,target,sha256):
        """One chunk's Ogg Opus file. Content is fixed per chunk ID (its path holds its sha256 and
        speech chunks are never rewritten), so the browser may keep it privately for good."""
        etag=f'"{sha256}"'
        try: f=target.open('rb')
        except OSError: raise RecallError(404,'audio_unavailable','Chunk audio is not available')
        with f:
            size=os.fstat(f.fileno()).st_size
            common=[('ETag',etag),('Cache-Control',AUDIO_CACHE),('Accept-Ranges','bytes')]
            if etag in (t.strip() for t in self.headers.get('If-None-Match','').split(',')):
                self.send_response(304)
                for name,value in common: self.send_header(name,value)
                self.end_headers(); return
            span=None
            if self.headers.get('Range') and self.headers.get('If-Range',etag).strip()==etag:
                try: span=byte_range(self.headers['Range'],size)
                except RecallError as e:
                    e.headers=[('Content-Range',f'bytes */{size}'),('Accept-Ranges','bytes')]; raise
            first,last=span or (0,size-1)
            self.send_response(206 if span else 200)
            self.send_header('Content-Type','audio/ogg')
            for name,value in common: self.send_header(name,value)
            if span: self.send_header('Content-Range',f'bytes {first}-{last}/{size}')
            self.send_header('Content-Length',str(last-first+1))
            self.send_header('X-Content-Type-Options','nosniff'); self.end_headers()
            f.seek(first); remaining=last-first+1
            while remaining>0:
                block=f.read(min(65536,remaining))
                if not block: break
                self.wfile.write(block); remaining-=len(block)
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
        data=self.body_headers(data); self.send_header('Cache-Control','no-store')
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
    server=ReaderServer((host,int(port)),Reader(Path(cfg['db_path']).expanduser(),labels,reader_archive_dir(cfg)),tokens,Path(cfg['static_root']).expanduser() if cfg.get('static_root') else None)
    try: server.serve_forever()
    finally: server.server_close()
