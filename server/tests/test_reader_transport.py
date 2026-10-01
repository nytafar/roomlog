"""Wire size and connection reuse for interactive recall."""
import gzip
import http.client
import json
import threading

from roomlog_server.recall import Reader
from roomlog_server.reader_http import ReaderServer


def test_reader_reuses_connection_and_compresses_negotiated_responses(cfg, conn, tmp_path):
    (tmp_path / 'index.html').write_text('recall page ' * 1000)
    server = ReaderServer(('127.0.0.1', 0), Reader(cfg.db_path), {'owner': 'owner'}, tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    try:
        client.request('GET', '/', headers={'Accept-Encoding': 'gzip'})
        response = client.getresponse()
        wire = response.read()
        assert response.version == 11
        assert response.getheader('Content-Encoding') == 'gzip'
        assert response.getheader('Vary') == 'Accept-Encoding'
        assert len(wire) < len(gzip.decompress(wire)) / 5
        socket = client.sock
        client.request('GET', '/v1/recall/info', headers={'Authorization': 'Bearer owner'})
        response = client.getresponse()
        assert response.status == 200
        assert 'sources' in json.loads(response.read())
        assert client.sock is socket and socket is not None
        client.request('GET', '/v1/recall/info')
        response = client.getresponse()
        assert response.status == 401
        response.read()
        for encoding in ('identity', 'gzip;q=0', 'gzip;q=0, *;q=1'):
            client.request('GET', '/', headers={'Accept-Encoding': encoding})
            response = client.getresponse()
            assert response.getheader('Content-Encoding') is None
            assert response.read() == b'recall page ' * 1000
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join()
