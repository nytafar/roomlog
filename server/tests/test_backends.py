"""Remote backends against in-process fake servers. No external host is ever contacted."""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest

from roomlog_server.audio import from_wav_bytes, to_wav_bytes
from roomlog_server.backends import BackendError, BackendUnavailable, Router, build_backend
from roomlog_server.backends.openai_compat import OpenAICompatBackend, parse_verbose_json
from roomlog_server.backends.whisper_cpp import WhisperCppBackend
from roomlog_server.config import BackendConfig, Config

from conftest import make_config

BERGET_RESPONSE = {
    "text": "Hei, dette er en test.",
    "duration": 3.2,
    "language": "no",
    "segments": [
        {"id": 0, "start": 0.0, "end": 1.5, "text": "Hei, dette"},
        {"id": 1, "start": 1.5, "end": 3.2, "text": " er en test."},
    ],
    "words": [
        {"word": "Hei,", "start": 0.1, "end": 0.5, "score": 0.98},
        {"word": "dette", "start": 0.6, "end": 1.4, "score": 0.9},
        {"word": "er", "start": 1.6, "end": 1.8, "score": 0.95},
        {"word": "en", "start": 1.9, "end": 2.1, "score": 0.9},
        {"word": "test.", "start": 2.2, "end": 3.1, "score": 0.99},
    ],
}

WHISPER_CPP_RESPONSE = {
    "task": "transcribe",
    "language": "no",
    "duration": 3.0,
    "text": "Hallo verden.",
    "segments": [
        {"id": 0, "seek": 0, "start": 0.0, "end": 1.4, "text": " Hallo", "tokens": [1],
         "temperature": 0.0, "avg_logprob": -0.3, "compression_ratio": 1.1, "no_speech_prob": 0.02},
        {"id": 1, "seek": 0, "start": 1.4, "end": 3.0, "text": " verden.", "tokens": [2],
         "temperature": 0.0, "avg_logprob": -0.4, "compression_ratio": 1.0, "no_speech_prob": 0.01},
    ],
}


def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    boundary = content_type.split("boundary=")[1].encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in body.split(b"--" + boundary):
        part = part.strip()
        if not part or part == b"--":
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        head_s = head.decode()
        name = re.search(r'name="([^"]+)"', head_s).group(1)
        fn = re.search(r'filename="([^"]+)"', head_s)
        if fn:
            files[name] = (fn.group(1), data)
        else:
            fields[name] = data.decode()
    return fields, files


class FakeState:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.status = 200
        self.response: dict = BERGET_RESPONSE
        self.fail_times = 0  # 503 for the first n requests


def make_fake_server(state: FakeState, inference_path: str = "/v1/audio/transcriptions"):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # noqa: D401
            pass

        def _json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            state.requests.append({"method": "GET", "path": self.path,
                                   "auth": self.headers.get("Authorization")})
            if state.status == 401:
                self._json(401, {"error": {"message": "bad key", "type": "auth", "code": None}})
            elif self.path in ("/v1/models", "/"):
                self._json(200, {"data": []})
            else:
                self._json(404, {"error": "nope"})

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n)
            fields, files = parse_multipart(body, self.headers["Content-Type"])
            state.requests.append({"method": "POST", "path": self.path, "fields": fields,
                                   "files": files, "auth": self.headers.get("Authorization")})
            if self.path != inference_path:
                self._json(404, {"error": {"message": "no such route", "type": "x", "code": None}})
                return
            if state.fail_times > 0:
                state.fail_times -= 1
                self._json(503, {"error": {"message": "overloaded", "type": "server_error", "code": None}})
                return
            if state.status != 200:
                self._json(state.status, {"error": {"message": "bad", "type": "invalid_request_error", "code": None}})
                return
            self._json(200, state.response)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


@pytest.fixture
def berget(tmp_path):
    state = FakeState()
    srv = make_fake_server(state)
    key = tmp_path / "berget.key"
    key.write_text("sk-test-123\n")
    bcfg = BackendConfig(
        name="berget", type="openai", model="NbAiLab/nb-whisper-large",
        base_url=f"http://127.0.0.1:{srv.server_address[1]}/v1",
        api_key_file=str(key), extra_fields={"align": "true"}, timeout_s=5,
    )
    yield state, bcfg, build_backend(bcfg, make_config(tmp_path))
    srv.shutdown()
    srv.server_close()


def test_openai_backend_request_and_response(berget):
    state, bcfg, backend = berget
    assert isinstance(backend, OpenAICompatBackend)
    assert backend.supports_words
    assert backend.model_id == "NbAiLab/nb-whisper-large"
    assert backend.probe() is True
    assert state.requests[-1] == {"method": "GET", "path": "/v1/models", "auth": "Bearer sk-test-123"}

    audio = np.linspace(-0.5, 0.5, 16000 * 3).astype(np.float32)
    t = backend.transcribe(audio, "no")
    req = state.requests[-1]
    assert req["path"] == "/v1/audio/transcriptions"
    assert req["auth"] == "Bearer sk-test-123"
    assert req["fields"] == {"model": "NbAiLab/nb-whisper-large", "response_format": "verbose_json",
                             "language": "no", "align": "true"}
    name, data = req["files"]["file"]
    assert name == "audio.wav" and data[:4] == b"RIFF"
    decoded, rate = from_wav_bytes(data)
    assert rate == 16000 and len(decoded) == 16000 * 3
    assert np.allclose(decoded, audio, atol=1e-3)

    assert t.language == "no" and t.duration == 3.2
    assert [s.text for s in t.segments] == ["Hei, dette", " er en test."]
    assert [w.word for w in t.segments[0].words] == ["Hei,", "dette"]
    assert [w.word for w in t.segments[1].words] == ["er", "en", "test."]
    assert t.segments[0].words[0].probability == 0.98
    assert t.segments[0].avg_logprob is None  # Berget carries no stats
    assert t.has_words


def test_openai_backend_language_auto_omits_field(berget):
    state, bcfg, backend = berget
    backend.transcribe(np.zeros(1600, np.float32), None)
    assert "language" not in state.requests[-1]["fields"]


def test_openai_backend_errors(berget):
    state, bcfg, backend = berget
    state.status = 400
    with pytest.raises(BackendError):
        backend.transcribe(np.zeros(1600, np.float32), "no")
    state.status = 200
    state.fail_times = 1
    with pytest.raises(BackendUnavailable):
        backend.transcribe(np.zeros(1600, np.float32), "no")
    # connection refused → unavailable, probe False
    dead = build_backend(BackendConfig(name="dead", type="openai", model="m",
                                       base_url="http://127.0.0.1:1/v1", timeout_s=1), Config(data_dir="/tmp/x"))
    assert dead.probe() is False
    with pytest.raises(BackendUnavailable):
        dead.transcribe(np.zeros(1600, np.float32), "no")


def test_router_fails_over_on_5xx_then_recovers(berget, tmp_path):
    state, bcfg, backend = berget
    from roomlog_server.backends.fake import FakeBackend
    local = FakeBackend(name="local", model_id="nb-medium")
    clock = [0.0]
    router = Router([backend, local], probe_ttl_s=60, clock=lambda: clock[0])
    state.fail_times = 1
    t, used = router.transcribe(np.ones(16000, np.float32), "no")
    assert used is local
    # berget is marked down for the TTL, then probed again and used
    t, used = router.transcribe(np.ones(16000, np.float32), "no")
    assert used is local
    clock[0] = 61
    t, used = router.transcribe(np.ones(16000, np.float32), "no")
    assert used is backend
    assert used.model_id == "NbAiLab/nb-whisper-large"


def test_router_select_and_no_backend():
    from roomlog_server.backends import NoBackendAvailable
    from roomlog_server.backends.fake import FakeBackend
    r = Router([FakeBackend(name="a", healthy=False), FakeBackend(name="b", healthy=False)], probe_ttl_s=0)
    assert r.select() is None
    with pytest.raises(NoBackendAvailable):
        r.transcribe(np.ones(160, np.float32), "no")
    b = FakeBackend(name="b")
    r = Router([FakeBackend(name="a", healthy=False), b], probe_ttl_s=0)
    assert r.select() is b


def test_parse_verbose_json_variants():
    # words nested per segment (OpenAI style with timestamp_granularities)
    t = parse_verbose_json({
        "text": "x", "language": "en", "duration": 1.0,
        "segments": [{"id": 0, "start": 0, "end": 1, "text": "x",
                      "words": [{"word": "x", "start": 0, "end": 1, "probability": 0.5}],
                      "avg_logprob": -0.1, "compression_ratio": 1.0, "no_speech_prob": 0.0}],
    })
    assert t.segments[0].words[0].probability == 0.5
    assert t.segments[0].avg_logprob == -0.1
    # text only
    t = parse_verbose_json({"text": "bare tekst", "duration": 2.0})
    assert len(t.segments) == 1 and t.segments[0].end == 2.0 and not t.has_words
    # empty
    t = parse_verbose_json({"text": "", "segments": [], "words": []})
    assert t.segments == []
    with pytest.raises(BackendError):
        parse_verbose_json("text")  # type: ignore[arg-type]


@pytest.fixture
def whisper_cpp(tmp_path):
    state = FakeState()
    state.response = WHISPER_CPP_RESPONSE
    srv = make_fake_server(state, inference_path="/inference")
    bcfg = BackendConfig(name="mac", type="whisper_cpp", model="nb-whisper-medium-ggml",
                         base_url=f"http://127.0.0.1:{srv.server_address[1]}", timeout_s=5)
    yield state, build_backend(bcfg, make_config(tmp_path))
    srv.shutdown()
    srv.server_close()


def test_whisper_cpp_backend(whisper_cpp):
    state, backend = whisper_cpp
    assert isinstance(backend, WhisperCppBackend)
    assert backend.supports_words is False
    assert backend.probe() is True
    t = backend.transcribe(np.zeros(16000 * 3, np.float32), "no")
    req = state.requests[-1]
    assert req["path"] == "/inference"
    assert req["fields"]["response_format"] == "verbose_json"
    assert req["fields"]["language"] == "no"
    assert req["files"]["file"][1][:4] == b"RIFF"
    assert [s.text for s in t.segments] == [" Hallo", " verden."]
    assert t.segments[0].avg_logprob == -0.3
    assert all(s.words is None for s in t.segments)
    assert not t.has_words


def test_local_backend_is_lazy_and_probe_checks_model_dir(tmp_path):
    import sys
    cfg = make_config(tmp_path)
    bcfg = BackendConfig(name="local", type="local", model="NbAiLab/nb-whisper-medium")
    backend = build_backend(bcfg, cfg)
    assert "faster_whisper" not in sys.modules
    assert backend.model_dir == cfg.models_dir / "NbAiLab--nb-whisper-medium" / "ct2"
    assert backend.probe() is False
    with pytest.raises(BackendUnavailable):
        backend.transcribe(np.zeros(1600, np.float32), "no")
    assert "faster_whisper" not in sys.modules
    (backend.model_dir).mkdir(parents=True)
    (backend.model_dir / "model.bin").write_bytes(b"")
    (backend.model_dir.parent / "REVISION").write_text("abc123\n")
    backend = build_backend(bcfg, cfg)
    assert backend.probe() is True
    assert backend.model_revision == "abc123"


def test_wav_roundtrip():
    a = np.array([0.0, 0.5, -0.5, 1.0, -1.0, 2.0], np.float32)
    b, rate = from_wav_bytes(to_wav_bytes(a))
    assert rate == 16000
    assert np.allclose(b, [0, 0.5, -0.5, 1.0, -1.0, 1.0], atol=1e-3)


def test_unknown_backend_type(tmp_path):
    with pytest.raises(ValueError):
        build_backend(BackendConfig(name="x", type="nope"), make_config(tmp_path))


def test_openai_probe_fails_on_missing_key_or_rejected_key(berget, tmp_path):
    state, bcfg, backend = berget
    assert backend.probe() is True
    state.status = 401
    assert backend.probe() is False
    state.status = 200
    missing = build_backend(BackendConfig(name="b", type="openai", model="m", base_url=bcfg.base_url,
                                          api_key_file=str(tmp_path / "absent.key"), timeout_s=5),
                            make_config(tmp_path))
    assert missing.probe() is False
    nokey = build_backend(BackendConfig(name="b", type="openai", model="m", base_url=bcfg.base_url,
                                        timeout_s=5), make_config(tmp_path))
    assert nokey.probe() is True  # no key configured at all (a local OpenAI-compatible server)
    assert state.requests[-1]["auth"] is None
