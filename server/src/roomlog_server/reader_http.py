"""Separate HTTP reader and same-origin static host."""
import argparse
import gzip
import socket
from contextlib import closing
import logging
import sqlite3
import hmac
import ipaddress
import json
import mimetypes
import os
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit
from .recall import LIMITS, Reader, RecallError, parse

IDENTITY_HEADER='Tailscale-User-Login'


def is_loopback(host):
    """True only for a host that can never be reached from another machine."""
    if host=='localhost': return True
    try: return ipaddress.ip_address(host.strip('[]')).is_loopback
    except ValueError: return False


def valid_login(value):
    """A login tsnet-proxy sends unencoded: Go's mime.QEncoding passes printable ASCII through
    as is, so only those logins can be compared raw (no decoding of client-shaped input)."""
    return isinstance(value,str) and bool(value) and value==value.strip() and all(' '<=c<='~' for c in value)


def device_address(value):
    """A tailnet IP in canonical form (IPv4-mapped IPv6 as IPv4), so differently written IPv6
    still compares equal. Raises ValueError for anything else, scoped addresses included."""
    if not isinstance(value,str): raise ValueError(f'not an IP address: {value!r}')
    ip=ipaddress.ip_address(value)
    if ip.version==6 and ip.scope_id: raise ValueError(f'scoped address: {value!r}')
    return ip.ipv4_mapped if ip.version==6 and ip.ipv4_mapped else ip


PROC_NET_TCP=('/proc/net/tcp','/proc/net/tcp6')


def proc_address(field,byteorder=sys.byteorder):
    """`0100007F:2160` from /proc/net/tcp{,6} as (ip_address, port). The address is 32-bit words
    in host byte order; IPv4-mapped IPv6 comes back as IPv4."""
    hexip,hexport=field.split(':')
    raw=bytes.fromhex(hexip)
    if len(raw) not in (4,16): raise ValueError(field)
    if byteorder=='little': raw=b''.join(raw[i:i+4][::-1] for i in range(0,len(raw),4))
    ip=ipaddress.ip_address(raw)
    if ip.version==6 and ip.ipv4_mapped: ip=ip.ipv4_mapped
    return ip,int(hexport,16)


def socket_uid(lines,local,remote,byteorder=sys.byteorder):
    """uid of the established socket whose own end is `local` and far end is `remote`, both
    (ip_address, port), from /proc/net/tcp-format lines; None if absent or unparsable."""
    for line in lines:
        fields=line.split()
        if len(fields)<8 or fields[3]!='01': continue  # 01 = TCP_ESTABLISHED; skips the header
        try:
            if proc_address(fields[1],byteorder)==local and proc_address(fields[2],byteorder)==remote: return int(fields[7])
        except ValueError: continue
    return None


def peer_uid(peer,ours,tables=PROC_NET_TCP):
    """uid owning the client end of a loopback connection: its row has the client's address
    as local and ours as remote. None when it can't be found (not Linux, unreadable /proc)."""
    try: local=(ipaddress.ip_address(peer[0]),peer[1]); remote=(ipaddress.ip_address(ours[0]),ours[1])
    except ValueError: return None
    for table in tables:
        try:
            with open(table) as f: uid=socket_uid(f,local,remote)
        except OSError: continue
        if uid is not None: return uid
    return None


class ReaderServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address,reader,tokens,static_root=None,identity_logins=(),identity_hosts=None,identity_devices=()):
        # The identity header is only as trustworthy as the path to this socket. Loopback keeps
        # other machines out; the peer-uid check (Handler.identity_role) keeps other local
        # accounts out; the Host pin keeps DNS-rebinding browser pages out. Anywhere but
        # loopback a peer could set it, so refuse to start rather than honour or drop it.
        if identity_logins and not is_loopback(address[0]):
            raise ValueError(f'tailscale_logins needs a loopback bind, not {address[0]!r}')
        if any(not valid_login(v) for v in identity_logins):
            raise ValueError('tailscale_logins entries must be printable ASCII without surrounding spaces')
        # A login alone names the user, not the device: every user-owned node (oma itself, and
        # through it every container and local account) is WhoIs'd as the owner. So identity
        # also needs the client's tailnet IP, as tsnet-proxy reports it, to be a listed device.
        if identity_logins and not identity_devices:
            raise ValueError('tailscale_logins needs tailscale_devices: a login alone is any software on any of your nodes')
        self.identity_devices=frozenset(device_address(v) for v in identity_devices)
        self.identity_logins=frozenset(identity_logins)
        self.reader,self.tokens=reader,tokens
        self.static_root=Path(static_root).resolve() if static_root else None
        self.slots=threading.BoundedSemaphore(LIMITS['concurrent_reads'])
        super().__init__(address,Handler)
        # tsnet-proxy rewrites Host to its target, `localhost:<local port>` (httputil SetURL with
        # http://localhost:<port>); a browser page can only send its own origin's host.
        self.identity_hosts=frozenset(h.lower() for h in (identity_hosts if identity_hosts is not None else [f'localhost:{self.server_port}']))

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


def etag_matches(header,etag):
    """If-None-Match weak comparison (RFC 9110 13.1.2): `*`, or any listed tag equal to ours
    once `W/` is dropped from both."""
    tags=[t.strip() for t in header.split(',')]
    return '*' in tags or etag.removeprefix('W/') in (t.removeprefix('W/') for t in tags)


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
        if self.close_connection: self.send_header('Connection','close')
        self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        try:
            # GET bodies are never read, so with keep-alive a body would be parsed as the next
            # request (smuggling through a pooling proxy). Refuse it and drop the connection.
            if self.headers.get_all('Transfer-Encoding') or any(v.strip()!='0' for v in self.headers.get_all('Content-Length',[])):
                self.close_connection=True
                raise RecallError(400,'invalid_request','GET requests must not carry a body')
            if len(self.path)>8192: raise RecallError(400,'invalid_parameter','Request URL too long')
            u=urlsplit(self.path)
            if u.path=='/healthz':
                with closing(self.server.reader.connection()) as conn: conn.execute('SELECT 1')
                self.send(200,dict(status='ready')); return
            if not u.path.startswith('/v1/recall/'): self.static(u.path); return
            role=self.identity_role()
            if role is None:
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
    def identity_role(self):
        """'owner' for exactly one allowed Tailscale-User-Login, compared raw, with exactly one
        pinned Host, exactly one X-Forwarded-For naming a listed device, from a loopback peer
        whose socket is ours (same uid); else None, and the bearer check decides as without
        identity sign-in."""
        logins=self.server.identity_logins
        if not logins or not is_loopback(self.client_address[0]): return None
        values=self.headers.get_all(IDENTITY_HEADER,[]); hosts=self.headers.get_all('Host',[])
        if len(values)!=1 or values[0] not in logins: return None
        if len(hosts)!=1 or hosts[0].lower() not in self.server.identity_hosts: return None
        # tsnet-proxy's Rewrite: Go strips inbound X-Forwarded-* first, then SetXForwarded sets
        # exactly the client's tailnet IP (bare, no brackets, for IPv6).
        forwarded=self.headers.get_all('X-Forwarded-For',[])
        if len(forwarded)!=1: return None
        try: device=device_address(forwarded[0].strip())
        except ValueError: return None
        if device not in self.server.identity_devices: return None
        if peer_uid(self.client_address,self.connection.getsockname())!=os.getuid(): return None
        return 'owner'
    def audio(self,target,sha256):
        """One chunk's Ogg Opus file. Content is fixed per chunk ID (its path holds its sha256 and
        speech chunks are never rewritten), so the browser may keep it privately for good."""
        etag=f'"{sha256}"'
        try: f=target.open('rb')
        except OSError: raise RecallError(404,'audio_unavailable','Chunk audio is not available')
        with f:
            size=os.fstat(f.fileno()).st_size
            common=[('ETag',etag),('Cache-Control',AUDIO_CACHE),('Accept-Ranges','bytes')]
            if etag_matches(self.headers.get('If-None-Match',''),etag):
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
        try: target=(root/unquote(path).lstrip('/')).resolve()
        except (ValueError,OSError): raise RecallError(404,'not_found','File not found')  # e.g. an embedded NUL
        if not target.is_relative_to(root): raise RecallError(404,'not_found','File not found')
        # stat and read can fail too (ENAMETOOLONG, EACCES); none of that is the client's business
        try:
            if target.is_dir(): target=(target/'index.html').resolve()
            if not target.is_relative_to(root) or not target.is_file(): raise RecallError(404,'not_found','File not found')
            data=target.read_bytes()
        except (ValueError,OSError): raise RecallError(404,'not_found','File not found')
        self.send_response(200)
        self.send_header('Content-Type',mimetypes.guess_type(target.name)[0] or 'application/octet-stream')
        data=self.body_headers(data); self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data)

def build_server(config_path):
    with Path(config_path).expanduser().open('rb') as f: cfg=tomllib.load(f)
    with Path(cfg['tokens_file']).expanduser().open('rb') as f: tokens=tomllib.load(f).get('tokens',{})
    if not tokens or any(not isinstance(k,str) or not k or v not in ('owner','device') for k,v in tokens.items()): raise ValueError('Invalid reader credential roles')
    labels=cfg.get('sources',{})
    if any(not isinstance(v,dict) or v.get('display_as','device') not in ('device','room') or not isinstance(v.get('label',''),str) for v in labels.values()): raise ValueError('Invalid source presentation')
    logins=cfg.get('tailscale_logins',[])
    if not isinstance(logins,list) or any(not valid_login(v) for v in logins): raise ValueError('tailscale_logins must be a list of printable-ASCII login names')
    hosts=cfg.get('tailscale_hosts')
    if hosts is not None and (not isinstance(hosts,list) or any(not isinstance(v,str) or not v or v!=v.strip() for v in hosts)): raise ValueError('tailscale_hosts must be a list of Host values')
    host,port=cfg.get('bind','127.0.0.1:8540').rsplit(':',1)
    devices=cfg.get('tailscale_devices',[])
    if not isinstance(devices,list): raise ValueError('tailscale_devices must be a list of tailnet IP addresses')
    return ReaderServer((host,int(port)),Reader(Path(cfg['db_path']).expanduser(),labels,reader_archive_dir(cfg)),tokens,Path(cfg['static_root']).expanduser() if cfg.get('static_root') else None,logins,hosts,devices)

def main(argv=None):
    p=argparse.ArgumentParser(prog='roomlog-reader'); p.add_argument('--config',required=True)
    server=build_server(p.parse_args(argv).config)
    try: server.serve_forever()
    finally: server.server_close()
