"""Chunk audio over the recall reader: auth, ranges, caching, containment, seek field."""
import http.client
import json
import threading
from contextlib import contextmanager

import pytest

from roomlog_server.reader_http import ReaderServer, reader_archive_dir
from roomlog_server.recall import Reader, parse

AUDIO = bytes(range(256)) * 40  # 10240 bytes standing in for an Ogg Opus file
SHA = 'a' * 64


def add_chunk(conn, path, sha=SHA, start=1000, kind='speech'):
    return conn.execute(
        """INSERT INTO chunks(sha256,kind,device_id,start_utc_ms,end_utc_ms,duration_ms,path,meta_json,
           run_id,epoch,received_utc_ms,model_id,status)
           VALUES(?,?,'nyta',?,?,4000,?,'{}','run',0,0,'current','done')""",
        (sha, kind, start, start + 4000, path)).lastrowid


def add_segment(conn, cid, start=1500, end=2500):
    return conn.execute(
        """INSERT INTO segments(chunk_id,device_id,idx,start_utc_ms,end_utc_ms,offset_ms,text,lang,model_id)
           VALUES(?,'nyta',0,?,?,0,'hello','no','current')""", (cid, start, end)).lastrowid


@pytest.fixture
def archive(cfg):
    cfg.archive_dir.mkdir(parents=True, exist_ok=True)
    (cfg.archive_dir / '2026' / '10' / '16').mkdir(parents=True)
    (cfg.archive_dir / '2026/10/16/chunk.opus').write_bytes(AUDIO)
    return cfg.archive_dir


@contextmanager
def serving(cfg, archive_dir):
    server = ReaderServer(('127.0.0.1', 0), Reader(cfg.db_path, archive_dir=archive_dir),
                          {'owner-secret': 'owner', 'device-secret': 'device'})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)

    def get(path, token='owner-secret', **headers):
        if token:
            headers['Authorization'] = 'Bearer ' + token
        client.request('GET', path, headers={k.replace('_', '-'): v for k, v in headers.items()})
        response = client.getresponse()
        return response.status, response.read(), response

    try:
        yield get
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_full_body_headers_and_auth(cfg, conn, archive):
    cid = add_chunk(conn, '2026/10/16/chunk.opus')
    with serving(cfg, archive) as get:
        assert get(f'/v1/recall/audio?chunk_id={cid}', token=None)[0] == 401
        status, body, _ = get(f'/v1/recall/audio?chunk_id={cid}', token='device-secret')
        assert status == 403 and json.loads(body)['error']['code'] == 'forbidden'
        assert get(f'/v1/recall/audio?chunk_id={cid}', token='wrong')[0] == 401
        status, body, r = get(f'/v1/recall/audio?chunk_id={cid}', Accept_Encoding='gzip')
        assert status == 200 and body == AUDIO
        assert r.getheader('Content-Type') == 'audio/ogg'
        assert r.getheader('Accept-Ranges') == 'bytes'
        assert r.getheader('Content-Length') == str(len(AUDIO))
        assert r.getheader('Content-Encoding') is None
        assert r.getheader('Cache-Control') == 'private, max-age=31536000, immutable'
        assert r.getheader('ETag') == f'"{SHA}"'
        status, body, _ = get(f'/v1/recall/audio?chunk_id={cid}', If_None_Match=f'"{SHA}"')
        assert status == 304 and body == b''


@pytest.mark.parametrize('header,start,end', [
    ('bytes=0-99', 0, 99), ('bytes=10000-', 10000, 10239), ('bytes=-40', 10200, 10239),
    ('bytes=10000-99999', 10000, 10239), ('bytes=5-5', 5, 5)])
def test_satisfiable_ranges(cfg, conn, archive, header, start, end):
    cid = add_chunk(conn, '2026/10/16/chunk.opus')
    with serving(cfg, archive) as get:
        status, body, r = get(f'/v1/recall/audio?chunk_id={cid}', Range=header)
        assert status == 206 and body == AUDIO[start:end + 1]
        assert r.getheader('Content-Range') == f'bytes {start}-{end}/{len(AUDIO)}'
        assert r.getheader('Content-Length') == str(end - start + 1)
        assert r.getheader('Accept-Ranges') == 'bytes'


@pytest.mark.parametrize('header', ['bytes=10240-', 'bytes=20000-30000', 'bytes=-0'])
def test_unsatisfiable_range(cfg, conn, archive, header):
    cid = add_chunk(conn, '2026/10/16/chunk.opus')
    with serving(cfg, archive) as get:
        status, body, r = get(f'/v1/recall/audio?chunk_id={cid}', Range=header)
        assert status == 416 and json.loads(body)['error']['code'] == 'range_not_satisfiable'
        assert r.getheader('Content-Range') == f'bytes */{len(AUDIO)}'


@pytest.mark.parametrize('header', ['bytes=0-1,5-6', 'items=0-1', 'bytes=9-3', 'bytes=x-'])
def test_unsupported_or_invalid_range_serves_whole_file(cfg, conn, archive, header):
    cid = add_chunk(conn, '2026/10/16/chunk.opus')
    with serving(cfg, archive) as get:
        status, body, _ = get(f'/v1/recall/audio?chunk_id={cid}', Range=header)
        assert status == 200 and body == AUDIO


def test_if_range_mismatch_serves_whole_file(cfg, conn, archive):
    cid = add_chunk(conn, '2026/10/16/chunk.opus')
    with serving(cfg, archive) as get:
        status, body, _ = get(f'/v1/recall/audio?chunk_id={cid}', Range='bytes=0-9', If_Range='"other"')
        assert status == 200 and body == AUDIO
        status, body, _ = get(f'/v1/recall/audio?chunk_id={cid}', Range='bytes=0-9', If_Range=f'"{SHA}"')
        assert status == 206 and body == AUDIO[:10]


def test_missing_unknown_and_fileless_chunks(cfg, conn, archive):
    gone = add_chunk(conn, '2026/10/16/gone.opus', sha='b' * 64)
    derived = add_chunk(conn, None, sha='c' * 64, kind='derived')
    directory = add_chunk(conn, '2026/10/16', sha='d' * 64)
    with serving(cfg, archive) as get:
        for cid in (gone, derived, directory):
            status, body, r = get(f'/v1/recall/audio?chunk_id={cid}')
            assert status == 404 and json.loads(body)['error']['code'] == 'audio_unavailable'
            assert r.getheader('Cache-Control') == 'private, no-store'
        status, body, _ = get('/v1/recall/audio?chunk_id=999999')
        assert status == 404 and json.loads(body)['error']['code'] == 'not_found'


@pytest.mark.parametrize('query', ['', 'chunk_id=', 'chunk_id=abc', 'chunk_id=-1', 'chunk_id=0',
                                   'chunk_id=1&chunk_id=1', 'chunk_id=1&x=2', 'chunk_id=99999999999999999999',
                                   'chunk_id=%2B1', 'chunk_id=%201'])
def test_invalid_parameters(cfg, conn, archive, query):
    with serving(cfg, archive) as get:
        status, body, _ = get('/v1/recall/audio?' + query)
        assert status == 400 and json.loads(body)['error']['code'] == 'invalid_parameter'


@pytest.mark.parametrize('path', ['../secret.opus', '/etc/passwd', '2026/../../secret.opus', 'escape/secret.opus'])
def test_paths_cannot_escape_archive(cfg, conn, archive, path):
    secret = archive.parent / 'secret.opus'
    secret.write_bytes(b'secret')
    (archive / 'escape').symlink_to(archive.parent)
    cid = add_chunk(conn, path)
    with serving(cfg, archive) as get:
        status, body, _ = get(f'/v1/recall/audio?chunk_id={cid}')
        assert status == 404 and b'secret' not in body and b'root:' not in body


def test_audio_lookup_is_read_only_and_capability_on(cfg, conn, archive):
    add_chunk(conn, '2026/10/16/chunk.opus')
    reader = Reader(cfg.db_path, archive_dir=archive)
    assert reader.read('info', {})['capabilities']['audio'] is True
    with reader.connection() as ro:
        with pytest.raises(Exception):
            ro.execute('DELETE FROM chunks')


def test_rows_carry_chunk_start(cfg, conn, archive):
    cid = add_chunk(conn, '2026/10/16/chunk.opus', start=1000)
    add_segment(conn, cid)
    span = conn.execute("""INSERT INTO dictation_spans(device_id,start_utc_ms,end_utc_ms,text,lang,engine,mode,
        origin,body_json,received_utc_ms) VALUES('nyta',1600,1700,'standalone','no','e','raw','o','{}',0)""").lastrowid
    conn.execute("""INSERT INTO segments(chunk_id,device_id,span_id,idx,start_utc_ms,end_utc_ms,offset_ms,text,lang,model_id)
        VALUES(NULL,'nyta',?,0,1600,1700,0,'standalone','no','dictation')""", (span,))
    reader = Reader(cfg.db_path, archive_dir=archive)
    window = dict(device_id='nyta', from_ms='1000', to_ms='5000', channel='all')
    rows = reader.read('transcript', parse('transcript', list(window.items())))['items']
    by_chunk = {r['chunk_id']: r['chunk_start_utc_ms'] for r in rows}
    assert by_chunk == {str(cid): 1000, None: None}
    hits = reader.read('search', parse('search', list(dict(window, q='hello').items())))['items']
    assert hits and hits[0]['chunk_start_utc_ms'] == 1000


def test_archive_dir_config(tmp_path):
    assert reader_archive_dir({'db_path': str(tmp_path / 'data/roomlog.db')}) == (tmp_path / 'data/archive').resolve()
    assert reader_archive_dir({'db_path': 'x.db', 'archive_dir': str(tmp_path / 'a')}) == (tmp_path / 'a').resolve()
