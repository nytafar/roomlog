"""Tailscale identity sign-in on the reader (ADR 0009): a trusted proxy on oma sets
`Tailscale-User-Login`; the reader honours it only for configured logins on a loopback bind."""
import http.client
import json
import threading
from contextlib import contextmanager

import pytest

from roomlog_server.reader_http import ReaderServer, build_server, decode_login
from roomlog_server.recall import Reader

OWNER = 'nytafar@github'


@contextmanager
def serving(cfg, logins=()):
    server = ReaderServer(('127.0.0.1', 0), Reader(cfg.db_path),
                          {'owner-secret': 'owner', 'device-secret': 'device'}, None, logins)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def get(port, headers=(), path='/v1/recall/info'):
    client = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        client.putrequest('GET', path)
        for name, value in headers:
            client.putheader(name, value)
        client.endheaders()
        response = client.getresponse()
        return response.status, json.loads(response.read()), response.getheader('WWW-Authenticate')
    finally:
        client.close()


def login(value):
    return [('Tailscale-User-Login', value)]


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
        (login(OWNER) + login(OWNER), 401),  # repeated header: refuse to guess
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


def test_login_is_decoded_as_tsnet_proxy_encodes_it():
    # tsnet-proxy sends mime.QEncoding.Encode("utf-8", login): plain ASCII passes through,
    # anything else becomes RFC 2047 encoded words.
    assert decode_login(OWNER) == OWNER
    assert decode_login('=?utf-8?q?bl=C3=A5b=C3=A6r@github?=') == 'blåbær@github'
    assert decode_login('=?utf-8?q?a?= =?utf-8?q?b@github?=') == 'ab@github'
    assert decode_login('=?utf-8?q?=FF@github?=') is None
    assert decode_login('') == ''


def test_encoded_login_matches_its_decoded_entry(cfg, conn):
    with serving(cfg, ['blåbær@github']) as port:
        assert get(port, login('=?utf-8?q?bl=C3=A5b=C3=A6r@github?='))[0] == 200


@pytest.mark.parametrize('host', ['0.0.0.0', '100.64.0.1', '192.168.80.1', 'oma.tailf63b9a.ts.net', ''])
def test_identity_on_a_non_loopback_bind_refuses_to_start(cfg, conn, host):
    with pytest.raises(ValueError, match='loopback'):
        ReaderServer((host, 0), Reader(cfg.db_path), {'owner-secret': 'owner'}, None, [OWNER])


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
    ('tailscale_logins = []\n', ()),
    (f'bind = "127.0.0.1:0"\ntailscale_logins = ["{OWNER}"]\n', (OWNER,)),
    (f'bind = "localhost:0"\ntailscale_logins = ["{OWNER}"]\n', (OWNER,)),
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
    f'bind = "0.0.0.0:0"\ntailscale_logins = ["{OWNER}"]\n',
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
