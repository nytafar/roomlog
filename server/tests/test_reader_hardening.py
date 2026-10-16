"""Reader request framing, odd static paths and search addresses."""
import json
import socket
import threading
from contextlib import contextmanager

import pytest

from roomlog_server.reader_http import ReaderServer
from roomlog_server.recall import Reader, parse


@contextmanager
def serving(cfg, root=None):
    server = ReaderServer(('127.0.0.1', 0), Reader(cfg.db_path), {'owner-secret': 'owner'}, root)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def exchange(port, raw):
    with socket.create_connection(('127.0.0.1', port), timeout=5) as s:
        s.sendall(raw)
        chunks = []
        while True:
            try:
                data = s.recv(65536)
            except (ConnectionResetError, TimeoutError):
                break
            if not data:
                break
            chunks.append(data)
        return b''.join(chunks)


SMUGGLED = b'GET /v1/recall/info HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer owner-secret\r\n\r\n'


@pytest.mark.parametrize('path', ['/healthz', '/', '/v1/recall/info', '/v1/recall/audio?chunk_id=1'])
@pytest.mark.parametrize('framing', [
    b'Content-Length: %d\r\n' % len(SMUGGLED),
    b'Transfer-Encoding: chunked\r\n',
])
def test_get_with_body_is_rejected_and_closed(cfg, conn, tmp_path, path, framing):
    (tmp_path / 'index.html').write_text('app')
    body = SMUGGLED if framing.startswith(b'Content') else b'%x\r\n%s\r\n0\r\n\r\n' % (len(SMUGGLED), SMUGGLED)
    request = (b'GET %s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer owner-secret\r\n' % path.encode()
               + framing + b'\r\n' + body)
    with serving(cfg, tmp_path) as port:
        reply = exchange(port, request)
    assert reply.count(b'HTTP/1.1 ') == 1, reply
    assert reply.startswith(b'HTTP/1.1 400 ')
    assert b'connection: close' in reply.lower()
    assert b'"invalid_request"' in reply


def test_get_with_zero_content_length_keeps_working(cfg, conn, tmp_path):
    (tmp_path / 'index.html').write_text('app')
    with serving(cfg, tmp_path) as port:
        reply = exchange(port, b'GET /healthz HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
    assert reply.startswith(b'HTTP/1.1 200 ')


@pytest.mark.parametrize('path', ['/%00', '/a%00b/index.html', '/v1/recall/%00'])
def test_null_bytes_are_not_found(cfg, conn, tmp_path, path):
    (tmp_path / 'index.html').write_text('app')
    with serving(cfg, tmp_path) as port:
        reply = exchange(port, b'GET %s HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer owner-secret\r\nConnection: close\r\n\r\n' % path.encode())
    assert reply.startswith(b'HTTP/1.1 404 '), reply


def test_search_address_of_point_row_is_a_valid_window(cfg, conn):
    cid = conn.execute("""INSERT INTO chunks(sha256,device_id,start_utc_ms,end_utc_ms,duration_ms,path,meta_json,run_id,
        epoch,received_utc_ms,model_id,status) VALUES('sha','nyta',1000,5000,4000,'x','{}','run',0,0,'m','done')""").lastrowid
    for start, end in ((2200, 2200), (2300, 2400)):
        conn.execute("""INSERT INTO segments(chunk_id,device_id,idx,start_utc_ms,end_utc_ms,offset_ms,text,lang,model_id)
            VALUES(?,'nyta',0,?,?,0,'needle','no','m')""", (cid, start, end))
    reader = Reader(cfg.db_path)
    hits = reader.read('search', parse('search', [('from_ms', '0'), ('to_ms', '10000'), ('q', 'needle')]))['items']
    addresses = sorted((h['address']['from_ms'], h['address']['to_ms']) for h in hits)
    assert addresses == [(2200, 2201), (2300, 2400)]
    for a, b in addresses:
        parse('transcript', [('device_id', 'nyta'), ('from_ms', str(a)), ('to_ms', str(b))])


def test_unstatable_static_paths_are_not_found(cfg, conn, tmp_path):
    root = tmp_path / 'public'
    root.mkdir()
    (root / 'index.html').write_text('app')
    locked = root / 'locked'
    locked.mkdir()
    (locked / 'index.html').write_text('hidden')
    locked.chmod(0)
    try:
        with serving(cfg, root) as port:
            for path in ('/' + 'a' * 300, '/' + 'a' * 300 + '/', '/locked/index.html', '/locked/'):
                reply = exchange(port, b'GET %s HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n' % path.encode())
                assert reply.startswith(b'HTTP/1.1 404 '), (path, reply)
                assert b'hidden' not in reply
    finally:
        locked.chmod(0o755)
