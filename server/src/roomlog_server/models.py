"""`roomlog fetch-model`: huggingface_hub snapshot of the local backend's repo, `ct2/*` only.

Nothing here runs at import time; the download happens only when `fetch_model` is called.
"""

from __future__ import annotations

from pathlib import Path

from .backends.local import REVISION_FILE
from .config import Config

DEFAULT_REPO = "NbAiLab/nb-whisper-medium"


def model_repo_from_config(cfg: Config) -> str:
    for b in cfg.backends:
        if b.type == "local" and b.model:
            return b.model
    return DEFAULT_REPO


def fetch_model(cfg: Config, repo_id: str | None = None, revision: str | None = None) -> Path:
    from huggingface_hub import HfApi, snapshot_download  # network-capable; on demand only

    repo_id = repo_id or model_repo_from_config(cfg)
    local_dir = cfg.local_model_dir(repo_id)
    local_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=repo_id,
        revision=revision,
        allow_patterns=["ct2/*"],
        local_dir=str(local_dir),
    )
    sha = None
    try:
        sha = HfApi().model_info(repo_id, revision=revision).sha
    except Exception:  # offline after a cached download: keep whatever we had
        pass
    if sha:
        (local_dir / REVISION_FILE).write_text(sha + "\n")
    if not (local_dir / "ct2" / "model.bin").exists():
        raise RuntimeError(f"{repo_id} has no ct2/model.bin; the local backend needs a CTranslate2 export")
    return local_dir / "ct2"
