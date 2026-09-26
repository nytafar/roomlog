package no.nyta.roomlog.core

import java.util.TreeMap
import kotlin.math.abs

/**
 * Sample counter anchored to UTC (ADR 0002). Port of
 * `edge/src/roomlog_edge/timeline.py`; see its docstring for the model.
 *
 * Pure: no clocks are read here. The capture loop feeds one [Block] per read,
 * carrying the clocks it read itself, and asks for the UTC time of any
 * absolute sample index. On Android `monoNs` is `elapsedRealtimeNanos()`
 * (BOOTTIME, survives suspend) and `realNs` is `monoNs` plus the server
 * offset from [ClockOffset] when synced, else the device wall clock.
 *
 * All times are integer nanoseconds. Not thread-safe: one capture thread owns it.
 */
class Timeline(
    val rate: Int = 16000,
    val latenessLimitNs: Long = 200_000_000,
    val lateBlocks: Int = 3,
    val stepLimitNs: Long = 50_000_000,
    val rerefIntervalNs: Long = 60 * NS,
) {
    /** One read's worth of audio plus the clocks read on arrival. */
    data class Block(
        val nFrames: Int,
        val monoNs: Long,
        val realNs: Long,
        /** Arrival minus capture time of the block's first sample. */
        val adcLatencyNs: Long,
        val inputOverflow: Boolean = false,
    )

    data class Anchor(val n: Long, val monoNs: Long)

    /**
     * A sample-continuity segment. [anchors] is the history of re-references,
     * oldest first; a sample maps through the last anchor at or before it.
     */
    class Epoch(val id: Int, offsetNs: Long, first: Anchor) {
        /** real − mono, shared by every anchor; shifted on clock steps. */
        var offsetNs: Long = offsetNs
            internal set
        val anchors: MutableList<Anchor> = mutableListOf(first)

        /** First sample of the next epoch, once known. */
        var endN: Long? = null
            internal set

        val nRef: Long get() = anchors.last().n
        val monoRefNs: Long get() = anchors.last().monoNs
        val realRefNs: Long get() = monoRefNs + offsetNs

        /** Number of anchors with `n <= x` (Python's `bisect_right(anchors, (x, 1 << 62))`). */
        private fun countAtOrBefore(x: Long): Int {
            var lo = 0
            var hi = anchors.size
            while (lo < hi) {
                val mid = (lo + hi) ushr 1
                if (anchors[mid].n <= x) lo = mid + 1 else hi = mid
            }
            return lo
        }

        fun anchorFor(n: Long): Anchor {
            val last = anchors.last()
            if (n >= last.n) return last
            return anchors[maxOf(countAtOrBefore(n) - 1, 0)]
        }

        /** Drop anchors no sample at or after [retainFromN] maps through. */
        fun prune(retainFromN: Long) {
            val i = countAtOrBefore(retainFromN)
            if (i > 1) anchors.subList(0, i - 1).clear()
        }
    }

    /** What the timeline concluded about one block. */
    data class BlockResult(
        val nStart: Long,
        val nEnd: Long,
        val epoch: Int,
        val newEpoch: Boolean,
        val clockStepNs: Long?,
        val latenessNs: Long,
        /** == nStart except for silent loss, where it is earlier. */
        val epochStartN: Long,
    )

    var n: Long = 0
        private set
    private val _epochs = TreeMap<Int, Epoch>()
    val epochs: Map<Int, Epoch> get() = _epochs
    var lastStepN: Long? = null
        private set
    var lastStepNs: Long? = null
        private set
    var lastObserveMonoNs: Long? = null
        private set
    var stepCount: Int = 0
        private set

    /** Anchors and epochs before this sample may be pruned. */
    var retainFromN: Long = 0

    private var lateRun = 0
    private var lateFirst: Anchor? = null // (n_b, t_b) of the first late block
    private var windowStartNs: Long? = null
    private var best: Triple<Long, Long, Long>? = null // (lateness, n_b, t_b)

    // -- mapping -----------------------------------------------------------

    val epoch: Epoch? get() = if (_epochs.isEmpty()) null else _epochs.lastEntry().value

    private fun epochOf(epochId: Int?): Epoch =
        if (epochId == null) epoch ?: throw IllegalStateException("timeline has no epoch yet")
        else _epochs[epochId] ?: throw NoSuchElementException("no epoch $epochId")

    /** `round(samples * NS / rate)`, exact and half-even like Python's `round`. */
    fun samplesToNs(samples: Long): Long {
        val q = Math.floorDiv(samples, rate.toLong())
        val r = Math.floorMod(samples, rate.toLong()) // 0 <= r < rate
        val num = r * NS
        var frac = num / rate
        val rem = num % rate
        if (rem * 2 > rate || (rem * 2 == rate.toLong() && frac % 2 == 1L)) frac++
        return q * NS + frac
    }

    fun monoNs(n: Long, epochId: Int? = null): Long {
        val a = epochOf(epochId).anchorFor(n)
        return a.monoNs + samplesToNs(n - a.n)
    }

    fun utcNs(n: Long, epochId: Int? = null): Long = monoNs(n, epochId) + epochOf(epochId).offsetNs

    // -- feeding -----------------------------------------------------------

    fun feed(block: Block): BlockResult {
        val nB = n
        val tB = block.monoNs - block.adcLatencyNs
        var offset = block.realNs - block.monoNs
        var ep = epoch

        var newEpoch = ep == null
        var lateness = 0L
        var start = Anchor(nB, tB)
        var step: Long? = null
        if (ep != null) {
            lateness = tB - monoNs(nB)
            if (block.inputOverflow) {
                newEpoch = true
            } else if (lateness > latenessLimitNs) {
                lateRun += 1
                if (lateFirst == null) lateFirst = Anchor(nB, tB)
                if (lateRun >= lateBlocks) {
                    newEpoch = true
                    start = lateFirst!!
                }
            } else {
                lateRun = 0
                lateFirst = null
            }
            // a clock step is applied to every existing epoch, whether or not
            // this block also opens a new one
            val observed = lastObserveMonoNs
            if (observed != null && block.monoNs <= observed) {
                // Queued before an out-of-band observation corrected the clock;
                // their old realtime offset must not undo it.
                offset = ep.offsetNs
            } else {
                step = applyOffset(offset, nB, block.monoNs)
            }
        }

        if (newEpoch) {
            val eid = if (ep == null) 0 else ep.id + 1
            ep?.endN = start.n
            ep = Epoch(eid, offset, start)
            _epochs[eid] = ep
            lateRun = 0
            lateFirst = null
            windowStartNs = start.monoNs
            best = null
            prune()
        } else {
            trackDrift(ep!!, nB, tB, lateness)
        }

        n = nB + block.nFrames
        return BlockResult(
            nStart = nB, nEnd = n, epoch = ep.id, newEpoch = newEpoch, clockStepNs = step,
            latenessNs = lateness, epochStartN = start.n,
        )
    }

    private fun applyOffset(offset: Long, atN: Long, monoNs: Long): Long? {
        val ep = epoch ?: return null
        val delta = offset - ep.offsetNs
        if (abs(delta) <= stepLimitNs) return null
        for (e in _epochs.values) e.offsetNs += delta
        lastStepN = atN
        lastStepNs = monoNs
        stepCount += 1
        return delta
    }

    /** Apply a clock step seen outside a read (e.g. a `/v1/time` probe right
     *  before re-stamping held segments). Returns the delta if one was applied. */
    fun observeOffset(realNs: Long, monoNs: Long): Long? {
        val delta = applyOffset(realNs - monoNs, n, monoNs)
        if (delta != null) lastObserveMonoNs = monoNs
        return delta
    }

    private fun prune() {
        val keep = retainFromN
        val newest = _epochs.lastKey()
        for (eid in _epochs.keys.toList()) {
            val e = _epochs.getValue(eid)
            val end = e.endN
            if (end != null && end <= keep && eid != newest) _epochs.remove(eid) else e.prune(keep)
        }
    }

    private fun trackDrift(ep: Epoch, nB: Long, tB: Long, lateness: Long) {
        val b = best
        if (b == null || lateness < b.first) best = Triple(lateness, nB, tB)
        val ws = windowStartNs ?: throw IllegalStateException("drift window not started")
        if (tB - ws >= rerefIntervalNs) {
            val (_, bn, bt) = best!!
            if (bn > ep.nRef) ep.anchors.add(Anchor(bn, bt))
            windowStartNs = tB
            best = null
            prune()
        }
    }

    // -- helpers for stamping ---------------------------------------------

    /** True when a clock step was applied while `[nStart, nEnd)` was being
     *  captured (observed at a block inside that range). */
    fun steppedIn(nStart: Long, nEnd: Long): Boolean {
        val s = lastStepN ?: return false
        return s in nStart until nEnd
    }

    companion object {
        const val NS = 1_000_000_000L
    }
}
