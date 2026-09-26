from __future__ import annotations

import pytest

from roomlog_server.config import Config, load_config, load_tokens

from conftest import REPO_ROOT


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("ROOMLOG_BIND", raising=False)
    monkeypatch.delenv("ROOMLOG_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.db_path == tmp_path / ".local/share/roomlog/roomlog.db"
    assert cfg.archive_dir == tmp_path / ".local/share/roomlog/archive"
    assert cfg.models_dir == tmp_path / ".local/share/roomlog/models"
    assert cfg.tokens_file == tmp_path / "tokens.toml"  # next to the (missing) server.toml
    assert cfg.language == "no"
    assert cfg.session_gap_s == 300 and cfg.window_s == 30 and cfg.batch_size == 50
    assert cfg.filters.blocklist[0] == "Teksting av"
    assert [b.type for b in cfg.backends] == ["local"]
    assert cfg.local_model_dir("NbAiLab/nb-whisper-medium") == cfg.models_dir / "NbAiLab--nb-whisper-medium"


def test_example_config_parses_and_env_bind_wins(tmp_path, monkeypatch):
    example = REPO_ROOT / "deploy" / "server" / "server.toml.example"
    monkeypatch.setenv("ROOMLOG_BIND", "100.79.124.57:8480")
    cfg = load_config(example)
    assert cfg.bind == "100.79.124.57:8480"
    assert [b.name for b in cfg.backends] == ["berget", "local"]
    berget = cfg.backends[0]
    assert berget.type == "openai"
    assert berget.base_url == "https://api.berget.ai/v1"
    assert berget.model == "NbAiLab/nb-whisper-large"
    assert berget.extra_fields == {"align": "true"}
    assert berget.api_key_file.endswith("secrets/berget.key")
    assert cfg.backends[1].model == "NbAiLab/nb-whisper-medium"
    assert cfg.backends[1].compute_type == "int8"
    assert cfg.language == "no"
    monkeypatch.delenv("ROOMLOG_BIND")
    cfg = load_config(example)
    assert cfg.bind == "127.0.0.1:8480"


def test_language_auto(tmp_path):
    p = tmp_path / "s.toml"
    p.write_text('[worker]\nlanguage = "auto"\n')
    assert load_config(p).language is None
    p.write_text('[worker]\nlanguage = "nn"\nbatch_size = 5\n[worker.filters]\nblocklist = ["x"]\nmin_avg_logprob = -2.0\n[sessions]\ngap_s = 120\n')
    cfg = load_config(p)
    assert cfg.language == "nn" and cfg.batch_size == 5
    assert cfg.filters.blocklist == ["x"] and cfg.filters.min_avg_logprob == -2.0
    assert cfg.session_gap_s == 120


def test_tokens(tmp_path):
    p = tmp_path / "tokens.toml"
    assert load_tokens(p) == {}
    p.write_text('oma = "abc"\npi-work = "def"\n')
    assert load_tokens(p) == {"abc": "oma", "def": "pi-work"}
    p.write_text('oma = "abc"\npi = "abc"\n')
    with pytest.raises(ValueError):
        load_tokens(p)
    p.write_text('oma = ""\n')
    with pytest.raises(ValueError):
        load_tokens(p)


def test_example_tokens_parse():
    example = REPO_ROOT / "deploy" / "server" / "tokens.toml.example"
    tokens = load_tokens(example)
    assert "oma" in tokens.values()
