package no.nyta.roomlog.core

import kotlin.math.abs

/**
 * Turns `AudioRecord` reads into [Timeline.Block]s whose capture time comes
 * from the stream's own timestamp, not from when the read returned.
 *
 * One instance per `AudioRecord` (frame indices restart with the record).
 * Every read passes `getTimestamp(TIMEBASE_BOOTTIME)` when it succeeded:
 * `(framePosition, nanoTime)`, "frame `framePosition` was captured at
 * `nanoTime`", on the same clock as the arrival time
 * (`elapsedRealtimeNanos()`).
 *
 * Capture time. Each timestamp gives an intercept `c = nanoTime −
 * framePosition/rate`; the capture time of frame `f` is `c + f/rate`. The
 * intercept in use is the median of the last few accepted timestamps, so a
 * single jittery timestamp moves nothing: a timestamp off by more than
 * [jitterTolNs] is a candidate, dropped as an outlier unless [confirmReads]
 * candidates in a row agree. `adcLatencyNs` is then `arrival − capture`, so a
 * read thread that stalls while the buffer holds the audio produces no
 * lateness at all: the timeline sees on-time blocks and opens no epoch.
 *
 * Loss. Only an overrun of the client buffer, or a glitch upstream, loses
 * frames. Two signals, because platforms differ in whether `framePosition`
 * counts frames dropped in an overrun:
 * - it counts them: the backlog (`framePosition` minus frames read) exceeds
 *   what the buffer can hold after this read, which is impossible without
 *   loss. The excess is the loss; a single dropped period registers.
 * - it does not: the intercept jumps forward by the lost duration. Confirmed
 *   by [confirmReads] agreeing timestamps, or by a single one when it is the
 *   first timestamp after reading past a buffer that was observed full. After
 *   a full buffer any jump beyond [jitterTolNs] is loss; [lossMinNs] only
 *   separates loss from a re-base when no full buffer explains the jump.
 *
 * Where the gap is. The client buffer drops the newest frames when full, so a
 * buffer observed full at a read that started at frame `R` holds pre-gap
 * frames up to `R + bufferFrames` and the gap sits exactly there. That index
 * is in the future when the loss is detected; [nextReadFrames] shortens the
 * read so a block starts exactly at the gap, and that block carries
 * `inputOverflow`, which opens the new epoch in the [Timeline]. A loss not
 * tied to a full buffer is placed after the last consistent timestamp.
 *
 * Without any successful timestamp the block falls back to the arrival time
 * minus the block duration, and stalls look like loss again; the caller logs
 * that case.
 */
class CaptureClock(
    /** `AudioRecord.bufferSizeInFrames`: the client buffer. */
    val bufferFrames: Int,
    val rate: Int = 16000,
    val jitterTolNs: Long = 5_000_000,
    /**
     * Smaller confirmed forward jumps re-base the intercept instead of opening
     * an epoch. Not applied right after a full buffer, where any jump beyond
     * [jitterTolNs] is loss.
     */
    val lossMinNs: Long = 20_000_000,
    val confirmReads: Int = 3,
    /**
     * Slack on "buffer full": one HAL period. "Backlog exceeds the buffer"
     * uses half of it, so a single dropped period counts as loss.
     */
    val slackFrames: Int = rate / 50,
    private val window: Int = 5,
) {
    data class Result(val block: Timeline.Block, val event: String?)

    private class Seg(val fromF: Long, var c: Long)

    private val segs = ArrayList<Seg>()
    private val accepted = ArrayDeque<Long>()
    private val candidates = ArrayList<Long>()
    private var lastAcceptedPos: Long? = null
    private var fullAt: Long? = null
    private var pendingGap: Long? = null

    /** Frames read from this record so far. */
    var framesRead = 0L
        private set

    /** Frames the position counter includes that never reached us. */
    var countedLost = 0L
        private set
    var losses = 0
        private set
    var outliers = 0
        private set
    val usingTimestamps: Boolean get() = segs.isNotEmpty()

    /** How many frames to read next, at most [max]: ends the read exactly at a known or possible gap. */
    fun nextReadFrames(max: Int): Int {
        val g = pendingGap ?: fullAt ?: return max
        val d = g - framesRead
        return if (d in 1 until max) d.toInt() else max
    }

    /**
     * One completed read of [nFrames]. [ts] is `(framePosition, nanoTime)` from
     * `getTimestamp` taken right after the read, or null if it failed.
     */
    fun onRead(nFrames: Int, arrivalMonoNs: Long, realNs: Long, ts: Pair<Long, Long>?): Result {
        val fFirst = framesRead
        framesRead += nFrames
        val event = if (ts != null) observe(fFirst, nFrames, arrivalMonoNs, ts.first, ts.second) else null
        val g = pendingGap
        val overflow = g != null && g < framesRead
        if (overflow) pendingGap = null
        val capture = if (segs.isEmpty()) {
            arrivalMonoNs - toNs(nFrames.toLong())
        } else {
            // a block that opens an epoch is stamped with the post-gap mapping
            segFor(if (overflow) maxOf(fFirst, g!!) else fFirst).c + toNs(fFirst)
        }
        return Result(Timeline.Block(nFrames, arrivalMonoNs, realNs, arrivalMonoNs - capture, overflow), event)
    }

    private fun observe(fFirst: Long, n: Int, arrival: Long, pos: Long, tsNs: Long): String? {
        val posOur = pos - countedLost
        val ci = tsNs - toNs(posOur)
        if (segs.isEmpty()) {
            segs += Seg(0, ci)
            accept(ci, posOur)
            return "first timestamp: framePosition=$pos after $framesRead frames read, " +
                "latency ${(arrival - (ci + toNs(framesRead))) / 1_000_000} ms"
        }
        // frames the counter has passed (delivered, or dropped if it counts them) that we have not read;
        // frames still inside the HAL are in neither number
        val backlog = posOur - framesRead

        // without loss the backlog is at most what the buffer holds after this read, bufferFrames − n.
        // The tolerance is half a period, not a whole one: an overrun that drops a single HAL period
        // must register, and a whole-period tolerance would hide exactly that loss.
        if (backlog > bufferFrames - n + slackFrames / 2) {
            // counted overrun: the buffer (full at this read's start) holds pre-gap frames up to fFirst + bufferFrames
            val lost = backlog - (bufferFrames - n)
            val g = maxOf(fullAt ?: (fFirst + bufferFrames), fFirst)
            countedLost += lost
            loss(g, segs.last().c + toNs(lost))
            return "overrun: $lost frames lost (counted by framePosition), new epoch at frame $g"
        }
        if (fullAt == null && backlog >= bufferFrames - n - slackFrames) fullAt = fFirst + bufferFrames

        val ref = median(accepted)
        val d = ci - ref
        val fa = fullAt
        if (fa != null && fFirst >= fa) {
            // first timestamp after reading past a full buffer: any jump beyond jitter here is the overrun,
            // however short (lossMinNs is for jumps with no full buffer to explain them)
            fullAt = null
            if (d > jitterTolNs) {
                loss(maxOf(fa, fFirst), ci)
                return "overrun: ${toFrames(d)} frames lost (not counted by framePosition), new epoch at frame $fa"
            }
        }
        if (abs(d) <= jitterTolNs) {
            candidates.clear()
            accept(ci, posOur)
            return null
        }
        candidates += ci
        if (candidates.size > confirmReads) candidates.removeAt(0)
        if (candidates.size == confirmReads) {
            val m = median(candidates)
            if (candidates.all { abs(it - m) <= jitterTolNs }) {
                candidates.clear()
                val jump = m - ref
                val full = fullAt
                if (full != null && jump > jitterTolNs) {
                    // post-gap frames were delivered before we read up to the full buffer's end
                    val g = maxOf(full, fFirst)
                    loss(g, m)
                    return "overrun: ${toFrames(jump)} frames lost (not counted by framePosition), new epoch at frame $g"
                }
                if (jump > lossMinNs) {
                    val g = maxOf(lastAcceptedPos ?: fFirst, fFirst)
                    loss(g, m)
                    return "loss: timestamps jumped ${jump / 1_000_000} ms, new epoch at frame $g"
                }
                segs.last().c = m
                accepted.clear()
                accept(m, posOur)
                return "timestamp re-based by ${jump / 1_000} us"
            }
        }
        outliers++
        return null
    }

    private fun accept(ci: Long, posOur: Long) {
        accepted.addLast(ci)
        if (accepted.size > window) accepted.removeFirst()
        lastAcceptedPos = posOur
        segs.last().c = median(accepted)
    }

    private fun loss(g: Long, newC: Long) {
        segs += Seg(g, newC)
        if (segs.size > 3) segs.removeAt(0)
        accepted.clear()
        accepted.addLast(newC)
        candidates.clear()
        fullAt = null
        pendingGap = g
        losses++
    }

    private fun segFor(f: Long): Seg = segs.lastOrNull { it.fromF <= f } ?: segs.first()

    private fun median(xs: Collection<Long>): Long = xs.sorted()[xs.size / 2]

    private fun toNs(frames: Long): Long {
        val r = rate.toLong()
        return Math.floorDiv(frames, r) * 1_000_000_000L + Math.floorMod(frames, r) * 1_000_000_000L / r
    }

    private fun toFrames(ns: Long): Long = Math.floorDiv(ns * rate, 1_000_000_000L)
}
