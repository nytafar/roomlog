"""Tailscale identity sign-in on the reader (ADR 0009): a trusted proxy on oma sets
`Tailscale-User-Login` and `X-Forwarded-For`; the reader honours them only for configured
logins on listed devices, with the proxy's Host, from a same-uid loopback peer, on a loopback
bind."""
import http.client
import ipaddress
import json
import os
import socket
import threading
from contextlib import contextmanager

import pytest

from roomlog_server import reader_http
from roomlog_server.reader_http import ReaderServer, build_server, peer_uid, proc_address, socket_uid
from roomlog_server.recall import Reader

OWNER = 'nytafar@github'
PHONE4, PHONE6 = '100.70.214.44', 'fd7a:115c:a1e0::1601:d62e'
OMA4 = '100.79.124.57'  # user-owned: WhoIs says OWNER for anything on it, containers included
DEVICES = [PHONE4, PHONE6]


@contextmanager
def serving(cfg, logins=(), hosts=None, devices=DEVICES):
    server = ReaderServer(('127.0.0.1', 0), Reader(cfg.db_path),
                          {'owner-secret': 'owner', 'device-secret': 'device'}, None, logins, hosts, devices)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def get(port, headers=(), path='/v1/recall/info', host=None):
    """GET with Host as tsnet-proxy sends it (`localhost:<port>`) unless `host` says otherwise;
    host='' sends none, a list sends each."""
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    host = f'localhost:{port}' if host is None else host
    try:
        client.putrequest('GET', path, skip_host=True)
        for value in ([host] if isinstance(host, str) else host):
            if value:
                client.putheader('Host', value)
        for name, value in headers:
            client.putheader(name, value)
        client.endheaders()
        response = client.getresponse()
        return response.status, json.loads(response.read()), response.getheader('WWW-Authenticate')
    finally:
        client.close()


def login(value, xff=PHONE4):
    """Identity headers as tsnet-proxy sends them for a request from device `xff`."""
    return [('Tailscale-User-Login', value)] + ([('X-Forwarded-For', xff)] if xff else [])


def bearer(token):
    return [('Authorization', f'Bearer {token}')]


def test_allowed_login_reads_as_owner(cfg, conn):
    with serving(cfg, [OWNER]) as port:
        status, body, _ = get(port, login(OWNER))
        assert status == 200 and 'sources' in body
        assert get(port, login(OWNER), '/v1/recall/sessions?from_ms=0&to_ms=1000')[0] == 200


def test_allowed_login_also_beats_a_weaker_bearer(cfg, conn):
    with serving(cfg, [OWNER]) as port:
        assert get(port, login(OWNER) + bearer('device-secret'))[0] == 200
        assert get(port, login(OWNER) + bearer('wrong'))[0] == 200


@pytest.mark.parametrize('logins', [[OWNER], [], ()])
def test_without_a_matching_login_the_bearer_check_is_unchanged(cfg, conn, logins):
    cases = [
        ([], 401), (login('someone@github'), 401), (login(''), 401),
        (login(OWNER.upper()), 401),
        (login(OWNER) + login(OWNER, None), 401),  # repeated header: refuse to guess
        (bearer('device-secret'), 403), (login('someone@github') + bearer('device-secret'), 403),
        (bearer('owner-secret'), 200), (login('someone@github') + bearer('owner-secret'), 200),
    ]
    if not logins:  # an empty list honours nobody, the owner's login included
        cases += [(login(OWNER), 401), (login(OWNER) + bearer('device-secret'), 403)]
    with serving(cfg, logins) as port:
        for headers, expected in cases:
            status, body, challenge = get(port, headers)
            assert status == expected, headers
            if expected == 401:
                assert body['error']['code'] == 'unauthorized' and challenge == 'Bearer'
            if expected == 403:
                assert body['error']['code'] == 'forbidden'


def test_unknown_routes_stay_not_found_under_identity(cfg, conn):
    with serving(cfg, [OWNER]) as port:
        assert get(port, login(OWNER), '/v1/recall/nope')[0] == 404
        assert get(port, [], '/v1/recall/nope')[0] == 401


@pytest.mark.parametrize('xff', [PHONE4, PHONE6, 'FD7A:115C:A1E0:0:0:0:1601:D62E', '::ffff:100.70.214.44'])
def test_listed_devices_get_in(cfg, conn, xff):
    with serving(cfg, [OWNER]) as port:
        assert get(port, login(OWNER, xff))[0] == 200


def test_listed_devices_match_in_canonical_form(cfg, conn):
    with serving(cfg, [OWNER], devices=['FD7A:115C:A1E0:0000::1601:D62E']) as port:
        assert get(port, login(OWNER, PHONE6))[0] == 200


@pytest.mark.parametrize('forwarded', [
    [],                                    # missing
    [OMA4],                                # oma itself: any container or local account
    ['100.104.237.20'],                    # another tailnet device, not listed
    [PHONE4, PHONE4],                      # two headers
    [OMA4 + ', ' + PHONE4], [PHONE4 + ', ' + OMA4], [PHONE4 + ',' + PHONE4],  # a list
    ['phone'], [''], ['[' + PHONE6 + ']'], [PHONE4 + ':443'], ['fe80::1%eth0'], ['unknown'],
])
def test_identity_needs_exactly_one_listed_device(cfg, conn, forwarded):
    headers = login(OWNER, None) + [('X-Forwarded-For', v) for v in forwarded]
    with serving(cfg, [OWNER]) as port:
        status, body, _ = get(port, headers)
        assert status == 401 and body['error']['code'] == 'unauthorized'
        assert get(port, headers + bearer('owner-secret'))[0] == 200
        assert get(port, headers + bearer('device-secret'))[0] == 403


def test_logins_without_devices_refuse_to_start(cfg, conn):
    for devices in ([], ()):
        with pytest.raises(ValueError, match='tailscale_devices'):
            ReaderServer(('127.0.0.1', 0), Reader(cfg.db_path), {'owner-secret': 'owner'}, None, [OWNER], None, devices)


def test_login_is_compared_raw_never_decoded(cfg, conn):
    # tsnet-proxy Q-encodes only non-ASCII logins, which the config refuses; an encoded word
    # (Q or B, any charset) for the allowed login is just a different string.
    with serving(cfg, [OWNER]) as port:
        for value in ('=?utf-8?q?nytafar@github?=', '=?utf-8?b?bnl0YWZhckBnaXRodWI=?=',
                      '=?iso-8859-1?q?nytafar=40github?='):
            assert get(port, login(value))[0] == 401


@pytest.mark.parametrize('host', ['evil.example:{port}', '127.0.0.1:{port}', 'localhost', 'localhost:1',
                                  '', ['localhost:{port}', 'localhost:{port}']])
def test_identity_needs_the_proxy_host(cfg, conn, host):
    # DNS rebinding: a page at http://evil.example:8540 resolved to 127.0.0.1 is same-origin with
    # the reader and may send the identity header, but its Host stays its own.
    with serving(cfg, [OWNER]) as port:
        sent = [h.format(port=port) for h in host] if isinstance(host, list) else host.format(port=port)
        status, body, _ = get(port, login(OWNER), host=sent)
        assert status == 401 and body['error']['code'] == 'unauthorized'
        assert get(port, login(OWNER) + bearer('owner-secret'), host=sent)[0] == 200
        assert get(port, login(OWNER), host='LocalHost:{}'.format(port))[0] == 200


def test_configured_hosts_replace_the_default(cfg, conn):
    with serving(cfg, [OWNER], ['oma.tailf63b9a.ts.net:8540']) as port:
        assert get(port, login(OWNER), host='oma.tailf63b9a.ts.net:8540')[0] == 200
        assert get(port, login(OWNER))[0] == 401


def test_identity_needs_a_peer_with_our_uid(cfg, conn, monkeypatch):
    with serving(cfg, [OWNER]) as port:
        assert get(port, login(OWNER))[0] == 200  # the real /proc lookup finds us
        for uid in (os.getuid() + 1, 0, None):  # another account, root, or lookup failure
            monkeypatch.setattr(reader_http, 'peer_uid', lambda peer, ours, uid=uid: uid)
            assert get(port, login(OWNER))[0] == 401
            assert get(port, login(OWNER) + bearer('owner-secret'))[0] == 200


# Rows as Linux writes them on a little-endian host: a client 127.0.0.1:54434 owned by uid 1000
# connected to a reader on 127.0.0.1:8540 (uid 1000 also owns the reader's accepted end).
TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:215C 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 101 1 0 100 0 0 10 0
   1: 0100007F:D4A2 0100007F:215C 06 00000000:00000000 03:00000000 00000000     0        0 0 3 0
   2: 0100007F:215C 0100007F:D4A2 01 00000000:00000000 00:00000000 00000000  1000        0 102 1 0 20 4 30 10 -1
   3: 0100007F:D4A2 0100007F:215C 01 00000000:00000000 00:00000000 00000000  1001        0 103 1 0 20 4 30 10 -1
   4: garbage
""".splitlines()
TCP6 = """\
  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:D4A3 00000000000000000000000001000000:215C 01 00000000:00000000 00:00000000 00000000  1002        0 104 1 0
   1: 0000000000000000FFFF00000100007F:D4A4 0000000000000000FFFF00000100007F:215C 01 00000000:00000000 00:00000000 00000000  1003        0 105 1 0
""".splitlines()


def addr(ip, port):
    return ipaddress.ip_address(ip), port


def test_proc_net_tcp_rows_are_parsed():
    assert proc_address('0100007F:215C', 'little') == addr('127.0.0.1', 8540)
    assert proc_address('7F000001:215C', 'big') == addr('127.0.0.1', 8540)
    assert proc_address('00000000000000000000000001000000:215C', 'little') == addr('::1', 8540)
    assert proc_address('0000000000000000FFFF00000100007F:0050', 'little') == addr('127.0.0.1', 80)
    reader = addr('127.0.0.1', 8540)
    # the client's row, not the reader's accepted end nor the TIME_WAIT row of an older connection
    assert socket_uid(TCP, addr('127.0.0.1', 54434), reader, 'little') == 1001
    assert socket_uid(TCP, addr('127.0.0.1', 1), reader, 'little') is None
    assert socket_uid(TCP6, addr('::1', 54435), addr('::1', 8540), 'little') == 1002
    assert socket_uid(TCP6, addr('127.0.0.1', 54436), reader, 'little') == 1003


def test_peer_uid_of_a_real_loopback_connection(tmp_path):
    with socket.create_server(('127.0.0.1', 0)) as listener, \
            socket.create_connection(listener.getsockname(), timeout=5) as client:
        accepted, peer = listener.accept()
        with accepted:
            assert peer_uid(peer, accepted.getsockname()) == os.getuid()
            assert peer_uid(peer, accepted.getsockname(), (str(tmp_path / 'missing'),)) is None
            assert peer_uid(('127.0.0.1', 1), accepted.getsockname()) is None


@pytest.mark.parametrize('host', ['0.0.0.0', '100.64.0.1', '192.168.80.1', 'oma.tailf63b9a.ts.net', ''])
def test_identity_on_a_non_loopback_bind_refuses_to_start(cfg, conn, host):
    with pytest.raises(ValueError, match='loopback'):
        ReaderServer((host, 0), Reader(cfg.db_path), {'owner-secret': 'owner'}, None, [OWNER], None, DEVICES)


@pytest.mark.parametrize('host', ['0.0.0.0', ''])
def test_non_loopback_bind_without_identity_still_starts(cfg, conn, host):
    server = ReaderServer((host, 0), Reader(cfg.db_path), {'owner-secret': 'owner'}, None, [])
    server.server_close()


def write_config(tmp_path, cfg, extra):
    tokens = tmp_path / 'reader-tokens.toml'
    tokens.write_text('[tokens]\n"owner-secret" = "owner"\n')
    path = tmp_path / 'reader.toml'
    path.write_text(f'db_path = "{cfg.db_path}"\ntokens_file = "{tokens}"\n{extra}')
    return path


@pytest.mark.parametrize('extra,expected', [
    ('', ()),
    (f'tailscale_logins = ["{OWNER}"]\ntailscale_devices = ["{PHONE4}"]\ntailscale_hosts = ["oma.tailf63b9a.ts.net:8540", "localhost:0"]\n', (OWNER,)),
    (f'tailscale_devices = ["{PHONE4}"]\n', ()),
    ('tailscale_logins = []\n', ()),
    (f'bind = "127.0.0.1:0"\ntailscale_logins = ["{OWNER}"]\ntailscale_devices = ["{PHONE4}", "{PHONE6}"]\n', (OWNER,)),
    (f'bind = "localhost:0"\ntailscale_logins = ["{OWNER}"]\ntailscale_devices = ["{PHONE6}"]\n', (OWNER,)),
])
def test_config_reads_allowed_logins(cfg, conn, tmp_path, extra, expected):
    server = build_server(write_config(tmp_path, cfg, extra if 'bind' in extra else 'bind = "127.0.0.1:0"\n' + extra))
    try:
        assert server.identity_logins == frozenset(expected)
    finally:
        server.server_close()


@pytest.mark.parametrize('extra', [
    'tailscale_logins = "nytafar@github"\n',
    'tailscale_logins = [""]\n',
    'tailscale_logins = [1]\n',
    'tailscale_logins = ["blåbær@github"]\n',
    'tailscale_logins = [" nytafar@github"]\n',
    'tailscale_logins = ["nyta\\tfar@github"]\n',
    'tailscale_hosts = "localhost:8540"\n',
    'tailscale_hosts = [""]\n',
    f'bind = "0.0.0.0:0"\ntailscale_logins = ["{OWNER}"]\ntailscale_devices = ["{PHONE4}"]\n',
    f'tailscale_logins = ["{OWNER}"]\n',
    f'tailscale_logins = ["{OWNER}"]\ntailscale_devices = []\n',
    f'tailscale_logins = ["{OWNER}"]\ntailscale_devices = "{PHONE4}"\n',
    f'tailscale_logins = ["{OWNER}"]\ntailscale_devices = ["phone"]\n',
    f'tailscale_logins = ["{OWNER}"]\ntailscale_devices = ["100.70.214.0/24"]\n',
    f'tailscale_logins = ["{OWNER}"]\ntailscale_devices = [1]\n',
    'tailscale_devices = ["phone"]\n',
])
def test_config_refuses_bad_identity_settings(cfg, conn, tmp_path, extra):
    if 'bind' not in extra:
        extra = 'bind = "127.0.0.1:0"\n' + extra
    with pytest.raises(ValueError):
        build_server(write_config(tmp_path, cfg, extra))


def test_proxy_unit_targets_the_example_reader_bind():
    from conftest import REPO_ROOT
    import tomllib
    deploy = REPO_ROOT / 'deploy' / 'server'
    reader = tomllib.loads((deploy / 'reader.example.toml').read_text())
    host, port = reader['bind'].rsplit(':', 1)
    assert host == '127.0.0.1' and 'tailscale_logins' not in reader  # opt-in, off by default
    exec_start = next(line for line in (deploy / 'roomlog-tsnet-proxy.service').read_text().splitlines()
                      if line.startswith('ExecStart='))
    assert exec_start.split()[-3:] == ['roomlog', port, '443'] and '--https' in exec_start
