package no.nyta.roomlog.core

/**
 * Cuts the continuous capture into raw segments (ADR 0005): `[nStart, nEnd)`
 * ranges on the sample counter, cut at `epochStart + k·segmentSamples`
 * ("cap"), at a sample loss ("discontinuity", the last segment of an epoch)
 * and at the end of the run ("shutdown").
 *
 * The encoder downstream is a stream: samples handed to it cannot be taken
 * back. Silent loss, however, is decided a few blocks late (the timeline's
 * three-block rule), and those late blocks belong to the *new* epoch. So,
 * like the edge's deferred cuts in `capture.py`, blocks that arrive while a
 * late run is in progress are held here and released only when the run
 * resolves: into the current segment if it was a hiccup, into the new epoch
 * if it was a loss. The caller keeps the PCM of held blocks (a [SampleRing]
 * of a second or two is plenty) and copies out whatever [Event.Samples] names.
 *
 * Events come in order: `Open`, one or more `Samples`, `Close`, per segment.
 * `Open` is emitted lazily with the first samples, so an empty segment never
 * appears.
 */
class RawSegmenter(
    val segmentSamples: Long = SEGMENT_SAMPLES,
    /** Must equal the timeline's `latenessLimitNs`. */
    val latenessLimitNs: Long = 200_000_000,
) {
    enum class CutReason(val wire: String) {
        CAP("cap"), DISCONTINUITY("discontinuity"), SHUTDOWN("shutdown")
    }

    data class Segment(
        val epoch: Int,
        val nStart: Long,
        val nEnd: Long,
        val reason: CutReason,
        /** Audio was lost immediately before this segment. */
        val discontinuity: Boolean,
    ) {
        val nSamples: Long get() = nEnd - nStart
    }

    sealed interface Event {
        data class Open(val epoch: Int, val nStart: Long, val discontinuity: Boolean) : Event
        /** Append samples `[nStart, nEnd)` to the open segment. */
        data class Samples(val nStart: Long, val nEnd: Long) : Event
        data class Close(val segment: Segment) : Event
    }

    private class Cur(val epoch: Int, val nStart: Long, val discontinuity: Boolean) {
        var opened = false
    }

    private var cur: Cur? = null
    private var emittedTo = 0L
    private var heldFrom: Long? = null
    private var heldTo = 0L
    private var finished = false

    /** Earliest sample the caller may still be asked for (open segment or held blocks). */
    val retainFromN: Long
        get() = minOf(cur?.nStart ?: emittedTo, heldFrom ?: emittedTo)

    /** Samples currently held back pending a late-run decision. */
    val heldSamples: Long get() = heldFrom?.let { heldTo - it } ?: 0

    fun feed(r: Timeline.BlockResult): List<Event> {
        check(!finished) { "segmenter already shut down" }
        val out = ArrayList<Event>(4)
        val c = cur
        if (r.newEpoch) {
            val gap = r.epochStartN
            if (c != null) {
                // held blocks before the gap (an xrun after a late run) belong to the old epoch
                heldFrom?.let { emit(out, it, gap) }
                close(out, CutReason.DISCONTINUITY)
            }
            heldFrom = null
            // silent loss: the held blocks start exactly at the gap, nothing before it is pending
            check(c == null || emittedTo == gap) { "epoch ${r.epoch} starts at $gap but emitted to $emittedTo" }
            cur = Cur(r.epoch, gap, discontinuity = c != null)
            emittedTo = gap
            emit(out, gap, r.nEnd)
        } else {
            checkNotNull(c) { "first block must open an epoch" }
            if (r.latenessNs > latenessLimitNs) {
                if (heldFrom == null) heldFrom = r.nStart
                heldTo = r.nEnd
            } else {
                val from = heldFrom ?: r.nStart
                heldFrom = null
                emit(out, from, r.nEnd)
            }
        }
        return out
    }

    /** End of run: release anything held into the open segment and close it. */
    fun shutdown(): List<Event> {
        if (finished) return emptyList()
        finished = true
        val out = ArrayList<Event>(3)
        if (cur != null) {
            heldFrom?.let { emit(out, it, heldTo) }
            heldFrom = null
            close(out, CutReason.SHUTDOWN)
        }
        return out
    }

    private fun emit(out: MutableList<Event>, from: Long, to: Long) {
        check(from == emittedTo) { "non-contiguous samples: $from after $emittedTo" }
        var a = from
        while (a < to) {
            val c = cur!!
            val boundary = c.nStart + segmentSamples
            val end = minOf(to, boundary)
            if (!c.opened) {
                out += Event.Open(c.epoch, c.nStart, c.discontinuity)
                c.opened = true
            }
            out += Event.Samples(a, end)
            emittedTo = end
            if (end == boundary) {
                close(out, CutReason.CAP)
                cur = Cur(c.epoch, boundary, discontinuity = false)
            }
            a = end
        }
    }

    private fun close(out: MutableList<Event>, reason: CutReason) {
        val c = cur ?: return
        if (c.opened && emittedTo > c.nStart) {
            out += Event.Close(Segment(c.epoch, c.nStart, emittedTo, reason, c.discontinuity))
        }
        cur = null
    }

    companion object {
        /** 30.0 s at 16 kHz. */
        const val SEGMENT_SAMPLES = 480_000L
    }
}

/** The last [capacity] samples by absolute index, for copying out held blocks. */
class SampleRing(val capacity: Int) {
    private val buf = ShortArray(capacity)

    /** One past the newest sample written. */
    var nEnd = 0L
        private set

    val nStart: Long get() = maxOf(0L, nEnd - capacity)

    fun write(n: Long, samples: ShortArray, count: Int = samples.size) {
        require(n == nEnd) { "ring expects sample $nEnd, got $n" }
        for (i in 0 until count) buf[((n + i) % capacity).toInt()] = samples[i]
        nEnd = n + count
    }

    fun read(from: Long, to: Long): ShortArray {
        require(from in nStart..to && to <= nEnd) { "[$from, $to) not in ring [$nStart, $nEnd)" }
        return ShortArray((to - from).toInt()) { buf[((from + it) % capacity).toInt()] }
    }
}
