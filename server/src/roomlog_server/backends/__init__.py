"""Backend factory and the router that implements first-healthy-with-failover (§4.4a)."""

from __future__ import annotations

import logging
import time

import numpy as np

from ..config import BackendConfig, Config
from .base import Backend, BackendError, BackendUnavailable, Segment, Transcript, Word

__all__ = [
    "Backend", "BackendError", "BackendUnavailable", "Segment", "Transcript", "Word",
    "Router", "NoBackendAvailable", "build_backend", "build_backends",
]

log = logging.getLogger("roomlog.backends")


def build_backend(bcfg: BackendConfig, cfg: Config) -> Backend:
    if bcfg.type == "local":
        from .local import LocalBackend
        return LocalBackend(bcfg, cfg)
    if bcfg.type == "openai":
        from .openai_compat import OpenAICompatBackend
        return OpenAICompatBackend(bcfg)
    if bcfg.type == "whisper_cpp":
        from .whisper_cpp import WhisperCppBackend
        return WhisperCppBackend(bcfg)
    if bcfg.type == "fake":
        from .fake import FakeBackend
        return FakeBackend(bcfg)
    raise ValueError(f"unknown backend type {bcfg.type!r} for backend {bcfg.name!r}")


def build_backends(cfg: Config) -> list[Backend]:
    return [build_backend(b, cfg) for b in cfg.backends]


class NoBackendAvailable(Exception):
    """Every configured backend failed its probe or its request."""


class Router:
    """Ordered backends; the first whose probe passes is used, the next on transient failure.

    Probe results are cached for `probe_ttl_s` so a sleeping Mac or an unreachable cloud is
    not re-probed on every window. A backend that fails mid-request is marked down for the
    same period.
    """

    def __init__(self, backends: list[Backend], probe_ttl_s: float = 60.0,
                 clock=time.monotonic) -> None:
        self.backends = list(backends)
        self.probe_ttl_s = probe_ttl_s
        self._clock = clock
        self._state: dict[str, tuple[bool, float]] = {}  # name → (healthy, checked_at)

    def _healthy(self, b: Backend) -> bool:
        st = self._state.get(b.name)
        now = self._clock()
        if st is not None and now - st[1] < self.probe_ttl_s:
            return st[0]
        try:
            ok = bool(b.probe())
        except Exception as e:  # a probe must never take the worker down
            log.warning("probe of %s raised: %s", b.name, e)
            ok = False
        self._state[b.name] = (ok, now)
        if not ok:
            log.info("backend %s unavailable", b.name)
        return ok

    def _mark_down(self, b: Backend) -> None:
        self._state[b.name] = (False, self._clock())

    def select(self) -> Backend | None:
        for b in self.backends:
            if self._healthy(b):
                return b
        return None

    def transcribe(self, audio: np.ndarray, language: str | None,
                   start_at: Backend | None = None) -> tuple[Transcript, Backend]:
        tried = 0
        errors: list[str] = []
        skipping = start_at is not None
        for b in self.backends:
            if skipping:
                if b is start_at:
                    skipping = False
                else:
                    continue
            if not self._healthy(b):
                continue
            tried += 1
            try:
                return b.transcribe_limited(audio, language), b
            except BackendUnavailable as e:
                log.warning("backend %s failed, falling through: %s", b.name, e)
                errors.append(f"{b.name}: {e}")
                self._mark_down(b)
        raise NoBackendAvailable("; ".join(errors) if errors else "no healthy backend")
