"""Sample counter anchored to UTC (design §3.3).

Pure module: no clocks are read here. The capture loop feeds one :class:`Block`
per PortAudio callback, carrying the clocks it read itself, and asks the
timeline for the UTC time of any absolute sample index.

Model
-----
``n`` is a 64-bit count of samples received in this run. An :class:`Epoch` is a
sample-continuity segment with an anchor ``(n_ref, mono_ref, real_ref)``; inside
it ``mono(n) = mono_ref + (n - n_ref) / rate`` and ``utc(n) = real_ref + (n -
n_ref) / rate``. A new epoch starts only when samples were lost.

Per block: ``t_b = m - L`` (arrival minus PortAudio's ADC-to-callback latency),
``lateness = t_b - mono(n_b)``, ``offset = r - m``.

* Sample loss: ``input_overflow`` set (authoritative) or lateness above
  ``lateness_limit`` on ``late_blocks`` consecutive blocks → new epoch.
* Clock step: ``offset`` differs from the epoch's ``real_ref - mono_ref`` by more
  than ``step_limit`` → ``real_ref`` of every epoch is shifted by the delta; ``n``
  and the epoch are untouched.
* Drift: every ``reref_interval`` a new anchor is added at the block with the
  smallest lateness in that window (callbacks are late, never early). Anchors
  are kept, and a sample maps through the last anchor at or before it, so a
  chunk stamped long after capture (unsynced hold) sees no accumulated drift.

All times are integer nanoseconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field

NS = 1_000_000_000


@dataclass(frozen=True)
class Block:
    """One callback's worth of audio plus the clocks read on arrival."""

    n_frames: int
    mono_ns: int  # time.monotonic_ns() in the callback
    real_ns: int  # time.time_ns() in the callback
    adc_latency_ns: int  # (time.currentTime - time.inputBufferAdcTime) in ns
    input_overflow: bool = False


@dataclass
class Epoch:
    """A sample-continuity segment.

    ``anchors`` is the history of ``(n_ref, mono_ref_ns)`` re-references, oldest
    first. Mapping a sample uses the last anchor at or before it, so stamping an
    old sample (unsynced hold) does not accumulate the drift absorbed since.
    ``real_ref_ns`` of the current anchor is ``mono_ref_ns + offset_ns``.
    """

    id: int
    offset_ns: int  # real - mono, shared by every anchor; shifted on clock steps
    anchors: list[tuple[int, int]] = field(default_factory=list)

    @property
    def n_ref(self) -> int:
        return self.anchors[-1][0]

    @property
    def mono_ref_ns(self) -> int:
        return self.anchors[-1][1]

    @property
    def real_ref_ns(self) -> int:
        return self.mono_ref_ns + self.offset_ns

    def anchor_for(self, n: int) -> tuple[int, int]:
        best = self.anchors[0]
        for a in self.anchors:
            if a[0] <= n:
                best = a
            else:
                break
        return best


@dataclass(frozen=True)
class BlockResult:
    """What the timeline concluded about one block."""

    n_start: int
    n_end: int
    epoch: int
    new_epoch: bool
    clock_step_ns: int | None
    lateness_ns: int


@dataclass
class Timeline:
    rate: int = 16000
    lateness_limit_ns: int = 200_000_000
    late_blocks: int = 3
    step_limit_ns: int = 50_000_000
    reref_interval_ns: int = 60 * NS

    n: int = 0
    epochs: dict[int, Epoch] = field(default_factory=dict)
    last_step_n: int | None = None
    last_step_ns: int | None = None
    _late_run: int = 0
    _window_start_ns: int | None = None
    _best: tuple[int, int, int] | None = None  # (lateness, n_b, t_b)

    # -- mapping -----------------------------------------------------------

    @property
    def epoch(self) -> Epoch | None:
        if not self.epochs:
            return None
        return self.epochs[max(self.epochs)]

    def _epoch(self, epoch_id: int | None) -> Epoch:
        if epoch_id is None:
            ep = self.epoch
            if ep is None:
                raise RuntimeError("timeline has no epoch yet")
            return ep
        return self.epochs[epoch_id]

    def _samples_to_ns(self, samples: int) -> int:
        return round(samples * NS / self.rate)

    def mono_ns(self, n: int, epoch_id: int | None = None) -> int:
        n_ref, mono_ref = self._epoch(epoch_id).anchor_for(n)
        return mono_ref + self._samples_to_ns(n - n_ref)

    def utc_ns(self, n: int, epoch_id: int | None = None) -> int:
        return self.mono_ns(n, epoch_id) + self._epoch(epoch_id).offset_ns

    # -- feeding -----------------------------------------------------------

    def feed(self, block: Block) -> BlockResult:
        n_b = self.n
        t_b = block.mono_ns - block.adc_latency_ns
        offset = block.real_ns - block.mono_ns
        ep = self.epoch

        new_epoch = ep is None
        lateness = 0
        if ep is not None:
            lateness = t_b - self.mono_ns(n_b)
            if block.input_overflow:
                new_epoch = True
            elif lateness > self.lateness_limit_ns:
                self._late_run += 1
                if self._late_run >= self.late_blocks:
                    new_epoch = True
            else:
                self._late_run = 0

        step: int | None = None
        if new_epoch:
            eid = 0 if ep is None else ep.id + 1
            ep = Epoch(id=eid, offset_ns=offset, anchors=[(n_b, t_b)])
            self.epochs[eid] = ep
            self._late_run = 0
            self._window_start_ns = t_b
            self._best = None
            lateness = 0
        else:
            delta = offset - ep.offset_ns
            if abs(delta) > self.step_limit_ns:
                for e in self.epochs.values():
                    e.offset_ns += delta
                step = delta
                self.last_step_n = n_b
                self.last_step_ns = block.mono_ns
            self._track_drift(ep, n_b, t_b, lateness)

        self.n = n_b + block.n_frames
        return BlockResult(
            n_start=n_b,
            n_end=self.n,
            epoch=ep.id,
            new_epoch=new_epoch,
            clock_step_ns=step,
            lateness_ns=lateness,
        )

    def _track_drift(self, ep: Epoch, n_b: int, t_b: int, lateness: int) -> None:
        if self._best is None or lateness < self._best[0]:
            self._best = (lateness, n_b, t_b)
        assert self._window_start_ns is not None
        if t_b - self._window_start_ns >= self.reref_interval_ns:
            _, bn, bt = self._best
            if bn > ep.n_ref:
                ep.anchors.append((bn, bt))
            self._window_start_ns = t_b
            self._best = None

    # -- helpers for stamping ---------------------------------------------

    def stepped_since(self, n_start: int) -> bool:
        """True when a clock step was applied at or after sample ``n_start``."""
        return self.last_step_n is not None and self.last_step_n >= n_start
