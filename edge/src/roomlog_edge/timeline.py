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
  ``lateness_limit`` on ``late_blocks`` consecutive blocks → new epoch. In the
  silent case the epoch starts at the first late block (``epoch_start_n`` in the
  result), so the caller closes the old chunk before that sample.
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

import bisect
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
    end_n: int | None = None  # first sample of the next epoch, once known

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
        last = self.anchors[-1]
        if n >= last[0]:
            return last
        i = bisect.bisect_right(self.anchors, (n, 1 << 62))
        return self.anchors[max(i - 1, 0)]

    def prune(self, retain_from_n: int) -> None:
        """Drop anchors no sample at or after ``retain_from_n`` maps through."""
        i = bisect.bisect_right(self.anchors, (retain_from_n, 1 << 62))
        if i > 1:
            del self.anchors[:i - 1]


@dataclass(frozen=True)
class BlockResult:
    """What the timeline concluded about one block."""

    n_start: int
    n_end: int
    epoch: int
    new_epoch: bool
    clock_step_ns: int | None
    lateness_ns: int
    epoch_start_n: int  # == n_start except for silent loss, where it is earlier


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
    step_count: int = 0
    retain_from_n: int = 0  # anchors and epochs before this sample may be pruned
    _late_run: int = 0
    _late_first: tuple[int, int] | None = None  # (n_b, t_b) of the first late block
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
        start = (n_b, t_b)
        step: int | None = None
        if ep is not None:
            lateness = t_b - self.mono_ns(n_b)
            if block.input_overflow:
                new_epoch = True
            elif lateness > self.lateness_limit_ns:
                self._late_run += 1
                if self._late_first is None:
                    self._late_first = (n_b, t_b)
                if self._late_run >= self.late_blocks:
                    new_epoch = True
                    start = self._late_first
            else:
                self._late_run = 0
                self._late_first = None
            # a clock step is applied to every existing epoch, whether or not
            # this block also opens a new one
            step = self._apply_offset(offset, n_b, block.mono_ns)

        if new_epoch:
            eid = 0 if ep is None else ep.id + 1
            if ep is not None:
                ep.end_n = start[0]
            ep = Epoch(id=eid, offset_ns=offset, anchors=[start])
            self.epochs[eid] = ep
            self._late_run = 0
            self._late_first = None
            self._window_start_ns = start[1]
            self._best = None
            self._prune()
        else:
            self._track_drift(ep, n_b, t_b, lateness)

        self.n = n_b + block.n_frames
        return BlockResult(
            n_start=n_b,
            n_end=self.n,
            epoch=ep.id,
            new_epoch=new_epoch,
            clock_step_ns=step,
            lateness_ns=lateness,
            epoch_start_n=start[0],
        )

    def _apply_offset(self, offset: int, n: int, mono_ns: int) -> int | None:
        ep = self.epoch
        if ep is None:
            return None
        delta = offset - ep.offset_ns
        if abs(delta) <= self.step_limit_ns:
            return None
        for e in self.epochs.values():
            e.offset_ns += delta
        self.last_step_n = n
        self.last_step_ns = mono_ns
        self.step_count += 1
        return delta

    def observe_offset(self, real_ns: int, mono_ns: int) -> int | None:
        """Apply a clock step seen outside a callback (e.g. right before
        re-stamping held chunks). Returns the delta if one was applied."""
        return self._apply_offset(real_ns - mono_ns, self.n, mono_ns)

    def _prune(self) -> None:
        keep = self.retain_from_n
        for eid in sorted(self.epochs):
            e = self.epochs[eid]
            if e.end_n is not None and e.end_n <= keep and eid != max(self.epochs):
                del self.epochs[eid]
            else:
                e.prune(keep)

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
            self._prune()

    # -- helpers for stamping ---------------------------------------------

    def stepped_in(self, n_start: int, n_end: int) -> bool:
        """True when a clock step was applied while ``[n_start, n_end)`` was
        being captured (observed at a block inside that range)."""
        return self.last_step_n is not None and n_start <= self.last_step_n < n_end
