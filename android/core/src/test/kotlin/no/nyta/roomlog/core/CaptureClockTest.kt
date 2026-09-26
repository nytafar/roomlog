package no.nyta.roomlog.core

import no.nyta.roomlog.core.Timeline.Companion.NS
import kotlin.math.abs
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * CaptureClock + Timeline against a simulated AudioRecord: the mic produces
 * frames continuously, the HAL delivers them in 20 ms periods into a client
 * buffer of [SimRecord.capacity] frames that drops the newest frames when
 * full, and a reader does blocking reads that may stall. `getTimestamp`
 * reports the newest delivered frame, counting dropped frames or not.
 */
class CaptureClockTest {
    companion object {
        const val RATE = 16000
        const val MONO0 = 5_000 * NS
        const val OFF = 1_790_000_000 * NS - MONO0 // real − mono
        const val BLOCK = 1600
    }

    class SimRecord(
        val capacity: Int = 32_000,
        val period: Int = 320,
        val countsLost: Boolean,
        ppm: Double = 0.0,
        val halLatencyNs: Long = 3_000_000,
    ) {
        private val ratio = 1.0 + ppm * 1e-6
        private var k = 0L // periods delivered
        var written = 0L
            private set
        var readCount = 0L
            private set
        var lost = 0L
            private set
        private val map = ArrayList<LongArray>() // (our start, hardware start, count)

        /** True capture time of hardware frame h. */
        fun t(h: Long): Long = MONO0 + Math.rint((h * NS).toDouble() * ratio / RATE).toLong()

        private fun deliveryTime(k: Long) = t((k + 1) * period) + halLatencyNs

        private fun deliver() {
            val space = capacity - (written - readCount)
            val w = minOf(period.toLong(), space)
            if (w > 0) {
                val last = map.lastOrNull()
                if (last != null && last[0] + last[2] == written && last[1] + last[2] == k * period) {
                    last[2] += w
                } else {
                    map += longArrayOf(written, k * period, w)
                }
            }
            written += w
            lost += period - w
            k++
        }

        fun hOf(f: Long): Long {
            val s = map.last { it[0] <= f }
            return s[1] + (f - s[0])
        }

        /** Blocking read of [n] frames by a reader ready at [readyNs]; returns the arrival time. */
        fun read(n: Int, readyNs: Long): Long {
            while (deliveryTime(k) <= readyNs) deliver()
            var t = readyNs
            while (written - readCount < n) {
                t = deliveryTime(k)
                deliver()
            }
            readCount += n
            return t + 500_000
        }

        /** (framePosition, nanoTime) of the newest delivered frame boundary. */
        fun timestamp(): Pair<Long, Long> = if (countsLost) {
            k * period to t(k * period)
        } else {
            written to t(hOf(written - 1) + 1)
        }
    }

    data class Rec(val r: Timeline.BlockResult, val trueNs: Long, val fFirst: Long)

    class Run(val sim: SimRecord, val clock: CaptureClock = CaptureClock(sim.capacity)) {
        val tl = Timeline()
        val recs = ArrayList<Rec>()
        val events = ArrayList<String>()
        private var ready = MONO0
        private var i = 0

        fun blocks(count: Int, stallNs: Map<Int, Long> = emptyMap(), noTs: Set<Int> = emptySet(),
                   tsJitterNs: Map<Int, Long> = emptyMap()) {
            repeat(count) {
                val n = clock.nextReadFrames(BLOCK)
                val f = sim.readCount
                val arrival = sim.read(n, ready)
                val ts = if (i in noTs) null else sim.timestamp().let { (p, t) -> p to t + (tsJitterNs[i] ?: 0) }
                val res = clock.onRead(n, arrival, arrival + OFF, ts)
                res.event?.let { e -> events += e }
                recs += Rec(tl.feed(res.block), sim.t(sim.hOf(f)), f)
                ready = arrival + (stallNs[i] ?: 0)
                i++
            }
        }

        val newEpochs: Int get() = recs.count { it.r.newEpoch } - 1

        fun stampErrorNs(rec: Rec) = abs(tl.utcNs(rec.r.nStart, rec.r.epoch) - (rec.trueNs + OFF))
        fun worstNs(from: Int = 0) = recs.drop(from).maxOf { stampErrorNs(it) }
    }

    private val ms = 1_000_000L

    @Test
    fun stalledReaderWithoutLossOpensNoEpochAndStampsStayExact() {
        for (countsLost in listOf(true, false)) {
            // 0.42 s (where arrival-based lateness opened epochs), 1 s and 1.9 s stalls against a 2 s buffer
            val run = Run(SimRecord(countsLost = countsLost))
            run.blocks(3000, stallNs = mapOf(100 to 420 * ms, 700 to 1000 * ms, 1500 to 1900 * ms))
            assertEquals(0L, run.sim.lost, "the simulation lost nothing")
            assertEquals(0, run.newEpochs, "countsLost=$countsLost events=${run.events}")
            assertTrue(run.worstNs() < 1 * ms, "countsLost=$countsLost worst ${run.worstNs()} ns")
            assertEquals(0, run.clock.losses)
        }
    }

    @Test
    fun withoutTimestampsAStallStillLooksLikeLoss() {
        // the arrival-based fallback, i.e. the spike's first scheme: this is what timestamps fix
        val run = Run(SimRecord(countsLost = true))
        run.blocks(400, stallNs = mapOf(100 to 1000 * ms), noTs = (0 until 400).toSet())
        assertEquals(0L, run.sim.lost)
        assertEquals(1, run.newEpochs)
        assertTrue(run.worstNs() > 100 * ms)
    }

    @Test
    fun stallsUnderMicDriftStayWithinTheTimelinesDriftBound() {
        for (ppm in listOf(50.0, -50.0)) {
            val run = Run(SimRecord(countsLost = true, ppm = ppm))
            val stalls = (1..20).associate { it * 1500 to (300L + 80 * it) * ms } // up to 1.9 s, every 2.5 min
            run.blocks(36_000, stallNs = stalls) // one hour
            assertEquals(0L, run.sim.lost)
            assertEquals(0, run.newEpochs, "ppm=$ppm events=${run.events}")
            // TimelineTest.drift50ppmOneHourUnder10ms is the timeline's own bound
            assertTrue(run.worstNs() < 10 * ms, "ppm=$ppm worst ${run.worstNs()} ns")
        }
    }

    @Test
    fun overrunOpensExactlyOneEpochAtTheGap() {
        for (countsLost in listOf(true, false)) {
            val run = Run(SimRecord(countsLost = countsLost))
            run.blocks(101, stallNs = mapOf(100 to 3000 * ms)) // 3 s stall after block 100, 2 s buffer: 1 s lost
            val gap = run.sim.readCount + run.sim.capacity // the buffer fills from here and then drops
            run.blocks(300)
            assertEquals(16_000L, run.sim.lost)
            assertEquals(1, run.newEpochs, "countsLost=$countsLost events=${run.events}")
            val opening = run.recs.single { it.r.newEpoch && it.r.epoch == 1 }
            assertEquals(gap, opening.fFirst, "epoch 1 starts exactly at the gap")
            assertEquals(gap, run.tl.epochs.getValue(0).endN)
            // every block, before and after the gap, is stamped to within a millisecond
            assertTrue(run.worstNs() < 1 * ms, "countsLost=$countsLost worst ${run.worstNs()} ns events=${run.events}")
            assertEquals(1, run.clock.losses)
        }
    }

    @Test
    fun singleJitteryTimestampsAreIgnored() {
        val run = Run(SimRecord(countsLost = false))
        // one late and one early outlier, including one at the 60 s mark where the old scheme refreshed
        run.blocks(1500, tsJitterNs = mapOf(50 to 80 * ms, 600 to -30 * ms, 601 to 45 * ms))
        assertEquals(0, run.newEpochs, run.events.toString())
        assertTrue(run.worstNs() < 1 * ms, "worst ${run.worstNs()}")
        assertEquals(3, run.clock.outliers)
    }

    @Test
    fun failedTimestampsFallBackWithoutFreezingLatencyAtZero() {
        val run = Run(SimRecord(countsLost = true, halLatencyNs = 4 * ms))
        // no timestamp for the first five reads, then gaps later on
        run.blocks(1500, noTs = (0 until 5).toSet() + (300 until 320).toSet() + setOf(900))
        assertEquals(0, run.newEpochs, run.events.toString())
        // the arrival-based first blocks are off by the HAL latency at most; once the drift
        // re-reference (60 s) picks a timestamped block everything is exact
        assertTrue(run.worstNs() < 6 * ms, "worst ${run.worstNs()}")
        assertTrue(run.worstNs(from = 700) < 1 * ms, "after re-reference ${run.worstNs(700)}")
    }

    @Test
    fun persistentSmallShiftRebasesWithoutAnEpoch() {
        val run = Run(SimRecord(countsLost = false))
        run.blocks(50)
        // a platform that re-bases its timestamps by 8 ms: under the loss minimum
        run.blocks(100, tsJitterNs = (50 until 150).associateWith { 8 * ms })
        assertEquals(0, run.newEpochs)
        assertTrue(run.events.any { it.startsWith("timestamp re-based") }, run.events.toString())
    }

    @Test
    fun readHintEndsTheReadAtAPossibleGap() {
        val c = CaptureClock(bufferFrames = 32_000)
        assertEquals(1600, c.nextReadFrames(1600))
        c.onRead(1600, MONO0, 0, 0L to MONO0)
        // a read that finds the buffer full (backlog of 30400 after taking 1600)
        c.onRead(1600, MONO0 + 2 * NS, 0, (3200L + 30_400) to (MONO0 + 2 * NS))
        // the full buffer ends at 1600 + 32000; reads line up with it
        var read = c.framesRead
        while (c.framesRead < 33_600) {
            val n = c.nextReadFrames(1600)
            assertTrue(c.framesRead + n <= 33_600)
            c.onRead(n, MONO0 + 2 * NS, 0, null)
            read += n
        }
        assertEquals(33_600L, c.framesRead)
    }
}
