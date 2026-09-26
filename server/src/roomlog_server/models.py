"""`roomlog fetch-model`: the local backend's ct2 snapshot and the pinned Silero VAD model.

Nothing here runs at import time; downloads happen only when the functions are called.
The Silero blob is the same file the edge installer fetches (`deploy/edge/install.sh`): it is
verified by its git blob sha1 and its SHA-256 is recorded next to the file, the way the
Whisper snapshot's revision is recorded next to the ct2 directory.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .backends.local import REVISION_FILE
from .config import Config
from .vad import MODEL_GIT_BLOB_SHA1, MODEL_VERSION, file_sha256

DEFAULT_REPO = "NbAiLab/nb-whisper-medium"
SILERO_URL = (f"https://raw.githubusercontent.com/snakers4/silero-vad/{MODEL_VERSION}"
              "/src/silero_vad/data/silero_vad.onnx")


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


# ---------------------------------------------------------------- Silero VAD


def blob_sha1(data: bytes) -> str:
    """git's blob hash: what the edge installer pins the model by."""
    h = hashlib.sha1()
    h.update(f"blob {len(data)}\0".encode())
    h.update(data)
    return h.hexdigest()


def vad_sha256_file(model_path: Path) -> Path:
    return model_path.with_name(model_path.name + ".sha256")


def _http_get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 (pinned https URL)
        return r.read()


def fetch_vad_model(cfg: Config, fetch: Callable[[str], bytes] | None = None,
                    expected_blob_sha1: str = MODEL_GIT_BLOB_SHA1, url: str = SILERO_URL) -> Path:
    """Put the pinned Silero model at `cfg.vad_model_path`; a good existing file is kept."""
    assert cfg.vad_model_path is not None
    path = Path(cfg.vad_model_path)
    sha_file = vad_sha256_file(path)
    if path.exists() and blob_sha1(path.read_bytes()) == expected_blob_sha1:
        if not sha_file.exists():
            sha_file.write_text(file_sha256(path) + "\n")
        return path
    data = (fetch or _http_get)(url)
    got = blob_sha1(data)
    if got != expected_blob_sha1:
        raise RuntimeError(f"Silero model blob sha1 mismatch: {got} != {expected_blob_sha1}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    sha_file.write_text(hashlib.sha256(data).hexdigest() + "\n")
    return path
