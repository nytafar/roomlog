package no.nyta.roomlog.core

import no.nyta.roomlog.core.Timeline.Anchor
import no.nyta.roomlog.core.Timeline.Block
import no.nyta.roomlog.core.Timeline.BlockResult
import no.nyta.roomlog.core.Timeline.Companion.NS
import kotlin.math.abs
import kotlin.random.Random
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Port of `edge/tests/test_timeline.py`: synthetic block records, no clocks. */
class TimelineTest {
    companion object {
        const val RATE = 16000
        const val W = 512
        const val BLOCK_NS = W * NS / RATE // 32 ms
        const val LAT = 40_000_000L // 40 ms input latency
        const val MONO0 = 1_000 * NS
        const val REAL0 = 1_758_880_000 * NS
    }

    /**
     * Produces blocks the way a capture callback would report them.
     * `trueReal(n)` is the wall-clock time the mic actually sampled `n`.
     */
    class Mic(ppm: Double = 0.0, private val jitterNs: Long = 0, seed: Int = 1) {
        var nMic = 0L // samples the mic has produced (including lost ones)
        private val ratio = 1.0 + ppm * 1e-6
        private val rng = Random(seed)
        var offset = REAL0 - MONO0

        fun trueMono(nMic: Long): Long = MONO0 + Math.rint((nMic * NS).toDouble() * ratio / RATE).toLong()
        fun trueReal(nMic: Long): Long = trueMono(nMic) + offset

        /** One 512-frame callback. `delayNs` delays it; when `honest` the
         *  reported latency grows with it, otherwise it shows up as lateness. */
        fun block(overflow: Boolean = false, delayNs: Long = 0, honest: Boolean = false): Pair<Block, Long> {
            val n0 = nMic
            nMic += W
            val adcEnd = trueMono(nMic)
            val jitter = if (jitterNs > 0) rng.nextLong(jitterNs + 1) else 0
            val m = adcEnd + LAT + jitter + delayNs
            val lat = (adcEnd - trueMono(n0)) + LAT + (if (honest) delayNs else 0)
            return Block(W, m, m + offset, lat, overflow) to n0
        }
    }

    private fun run(
        tl: Timeline, mic: Mic, blocks: Int, overflow: Boolean = false, delayNs: Long = 0,
    ): List<Pair<BlockResult, Long>> = List(blocks) {
        val (b, nMic) = mic.block(overflow, delayNs)
        tl.feed(b) to nMic
    }

    private fun near(a: Long, b: Long, tol: Long = 1_000_000) = abs(a - b) < tol

    @Test
    fun steadyStateWithin1ms() {
        val tl = Timeline()
        val mic = Mic()
        val res = run(tl, mic, 2000)
        assertTrue(res[0].first.newEpoch && res[0].first.epoch == 0)
        for ((r, nMic) in res.drop(1)) {
            assertTrue(r.epoch == 0 && !r.newEpoch && r.clockStepNs == null)
            assertTrue(near(tl.utcNs(r.nStart), mic.trueReal(nMic)))
        }
        assertEquals(2000L * W, tl.n)
    }

    @Test
    fun inputOverflowOpensNewEpoch() {
        val tl = Timeline()
        val mic = Mic()
        run(tl, mic, 100)
        mic.nMic += 5 * W // the hardware dropped five blocks
        val (r, nMic) = run(tl, mic, 1, overflow = true).single()
        assertTrue(r.newEpoch && r.epoch == 1)
        assertEquals(100L * W, r.nStart) // n keeps counting received samples
        assertTrue(near(tl.utcNs(r.nStart), mic.trueReal(nMic)))
        for ((r2, n2) in run(tl, mic, 50)) {
            assertTrue(!r2.newEpoch && r2.epoch == 1)
            assertTrue(near(tl.utcNs(r2.nStart), mic.trueReal(n2)))
        }
    }

    @Test
    fun silentLossDetectedWithinThreeBlocks() {
        val tl = Timeline()
        val mic = Mic()
        run(tl, mic, 100)
        mic.nMic += 7 * W // 224 ms lost, no flag
        val res = run(tl, mic, 5)
        assertEquals(listOf(false, false, true, false, false), res.map { it.first.newEpoch })
        assertEquals(listOf(true, true, true), res.take(3).map { it.first.latenessNs > 200_000_000 })
        // The epoch starts at the first late block, i.e. the first post-gap
        // sample, so the two blocks seen before the decision belong to it too.
        val r2 = res[2].first
        assertTrue(r2.epochStartN == 100L * W && r2.nStart == 102L * W)
        assertEquals(100L * W, tl.epochs.getValue(0).endN)
        for ((r, nMic) in res.drop(2)) {
            assertEquals(1, r.epoch)
            assertTrue(near(tl.utcNs(r.nStart), mic.trueReal(nMic)))
        }
        for ((r, nMic) in res.take(2)) { // the first two late blocks map through the new epoch
            assertTrue(near(tl.utcNs(r.nStart, 1), mic.trueReal(nMic)))
        }
    }

    @Test
    fun schedulingHiccupWithCatchupKeepsEpoch() {
        val tl = Timeline()
        val mic = Mic()
        run(tl, mic, 100)
        // 150 ms late callback whose latency figure grows with the delay
        val (b, _) = mic.block(delayNs = 150_000_000, honest = true)
        val r = tl.feed(b)
        assertTrue(!r.newEpoch && abs(r.latenessNs) < 1_000_000)
        // cruder hiccup: two late blocks then catch-up must not trip the three-block rule
        repeat(2) {
            val (b2, _) = mic.block(delayNs = 250_000_000)
            val r2 = tl.feed(b2)
            assertTrue(!r2.newEpoch && r2.latenessNs > 200_000_000)
        }
        for ((r3, _) in run(tl, mic, 20)) assertTrue(!r3.newEpoch && r3.epoch == 0)
    }

    private fun clockStepCase(deltaNs: Long) {
        val tl = Timeline()
        val mic = Mic()
        run(tl, mic, 200)
        val chunkStart = 150L * W
        val before = tl.utcNs(chunkStart)
        mic.offset += deltaNs // the wall clock steps; monotonic does not
        val (r, _) = run(tl, mic, 1).single()
        assertTrue(r.clockStepNs == deltaNs && !r.newEpoch && r.epoch == 0)
        assertTrue(tl.steppedIn(chunkStart, r.nStart + W))
        assertFalse(tl.steppedIn(chunkStart, r.nStart)) // chunk ended before the step
        assertFalse(tl.steppedIn(r.nStart + 1, r.nStart + 10 * W))
        assertEquals(1, tl.stepCount)
        // in-flight chunk re-stamped with the post-step mapping
        assertEquals(before + deltaNs, tl.utcNs(chunkStart))
        // later chunks: no gap, sample-exact relative timing, same epoch
        val res = run(tl, mic, 300)
        for ((r2, n2) in res) {
            assertTrue(r2.epoch == 0 && !r2.newEpoch && r2.clockStepNs == null)
            assertTrue(near(tl.utcNs(r2.nStart), mic.trueReal(n2)))
        }
        val a = res[10].first
        val b = res[11].first
        assertEquals(BLOCK_NS, tl.utcNs(b.nStart) - tl.utcNs(a.nStart))
    }

    @Test
    fun clockStepForwardOneHour() = clockStepCase(3600 * NS)

    @Test
    fun clockStepBackwardTwoSeconds() = clockStepCase(-2 * NS)

    @Test
    fun drift50ppmOneHourUnder10ms() {
        for (ppm in listOf(50.0, -50.0)) {
            val tl = Timeline()
            val mic = Mic(ppm = ppm, jitterNs = 5_000_000)
            val blocks = 3600 * RATE / W
            var worstNow = 0L
            var worstLater = 0L
            val res = ArrayList<Pair<BlockResult, Long>>(blocks)
            repeat(blocks) {
                val (b, nMic) = mic.block()
                val r = tl.feed(b)
                assertEquals(0, r.epoch)
                res.add(r to nMic)
                // stamped when the chunk closes, i.e. right away
                worstNow = maxOf(worstNow, abs(tl.utcNs(r.nStart) - mic.trueReal(nMic)))
            }
            for ((r, nMic) in res) {
                // stamped an hour later (unsynced hold release)
                worstLater = maxOf(worstLater, abs(tl.utcNs(r.nStart) - mic.trueReal(nMic)))
            }
            assertTrue(worstNow < 10_000_000, "ppm=$ppm worstNow=$worstNow")
            assertTrue(worstLater < 10_000_000, "ppm=$ppm worstLater=$worstLater")
        }
    }

    @Test
    fun unsyncedStartThenSyncRestampsHeldChunks() {
        // The clock is 90 s slow at boot; sync steps it forward. Chunks stamped
        // before the step are recomputed through the same epoch mapping.
        val tl = Timeline()
        val mic = Mic()
        val trueOffset = mic.offset
        mic.offset -= 90 * NS
        run(tl, mic, 300)
        val held = listOf(0 to 20L * W, 0 to 120L * W) // (epoch, n_start) of chunks in unsynced/
        val stale = held.map { (e, n) -> tl.utcNs(n, e) }
        mic.offset = trueOffset
        val (r, _) = run(tl, mic, 1).single()
        assertEquals(90 * NS, r.clockStepNs)
        for ((i, h) in held.withIndex()) {
            val (e, n) = h
            assertEquals(stale[i] + 90 * NS, tl.utcNs(n, e))
            assertEquals(mic.trueReal(n), tl.utcNs(n, e))
        }
    }

    @Test
    fun restampAcrossEpochsAfterStep() {
        val tl = Timeline()
        val mic = Mic()
        run(tl, mic, 50)
        mic.nMic += 3 * W
        run(tl, mic, 1, overflow = true)
        run(tl, mic, 50)
        val old0 = tl.utcNs(10L * W, 0)
        val old1 = tl.utcNs(60L * W, 1)
        mic.offset += 7 * NS
        run(tl, mic, 1)
        assertEquals(old0 + 7 * NS, tl.utcNs(10L * W, 0))
        assertEquals(old1 + 7 * NS, tl.utcNs(60L * W, 1))
    }

    @Test
    fun stepSeenOnEpochOpeningBlockShiftsOldEpochs() {
        val tl = Timeline()
        val mic = Mic()
        run(tl, mic, 100)
        val old = tl.utcNs(50L * W, 0)
        mic.offset += 5 * NS
        mic.nMic += 4 * W
        val (r, nMic) = run(tl, mic, 1, overflow = true).single()
        assertTrue(r.newEpoch && r.epoch == 1 && r.clockStepNs == 5 * NS)
        assertTrue(tl.lastStepN == r.nStart && tl.stepCount == 1)
        assertEquals(old + 5 * NS, tl.utcNs(50L * W, 0))
        assertTrue(near(tl.utcNs(r.nStart, 1), mic.trueReal(nMic)))
    }

    @Test
    fun observeOffsetAppliesStepOutsideCallbacks() {
        val tl = Timeline()
        val mic = Mic()
        run(tl, mic, 100)
        val old = tl.utcNs(10L * W)
        assertNull(tl.observeOffset(mic.trueReal(0) + 1_000_000, mic.trueMono(0))) // 1 ms: no step
        val delta = tl.observeOffset(mic.trueReal(0) + 90 * NS, mic.trueMono(0))
        assertTrue(delta == 90 * NS && tl.stepCount == 1 && tl.lastStepN == tl.n)
        assertEquals(old + 90 * NS, tl.utcNs(10L * W))
        // the next block agrees with the new offset: no second step
        mic.offset += 90 * NS
        val (r, _) = run(tl, mic, 1).single()
        assertNull(r.clockStepNs)
    }

    @Test
    fun observedStepIsNotReversedByQueuedPreStepCallbacks() {
        val tl = Timeline()
        val mic = Mic()
        mic.offset -= 90 * NS
        run(tl, mic, 100)
        val queued = List(2) { mic.block().first }
        mic.offset += 90 * NS
        val observedMono = queued.last().monoNs + 1_000_000
        assertEquals(90 * NS, tl.observeOffset(observedMono + mic.offset, observedMono))
        for (block in queued) assertNull(tl.feed(block).clockStepNs)
        assertEquals(1, tl.stepCount)
        val (block, nMic) = mic.block()
        assertNull(tl.feed(block).clockStepNs)
        assertEquals(1, tl.stepCount)
        assertTrue(near(tl.utcNs(tl.n - W), mic.trueReal(nMic)))
    }

    @Test
    fun anchorLookupAndPruning() {
        val tl = Timeline()
        val mic = Mic(ppm = 50.0)
        val blocks = 20 * 60 * RATE / W // twenty minutes → ~20 anchors
        tl.retainFromN = 0
        run(tl, mic, blocks)
        val ep = tl.epochs.getValue(0)
        assertTrue(ep.anchors.size >= 15)
        // bisect lookup matches a linear scan for samples all over the range
        var n = 0L
        while (n < tl.n) {
            val linear = ep.anchors.filter { it.n <= n }.maxByOrNull { it.n } ?: ep.anchors[0]
            assertEquals(linear, ep.anchorFor(n))
            n += 7919 * 5
        }
        assertEquals(ep.anchors[0], ep.anchorFor(-5))
        // pruning keeps the anchor that still covers retainFromN and everything after
        val keepN = ep.anchors[10].n + 100
        tl.retainFromN = keepN
        run(tl, mic, 60 * RATE / W + 5) // one more window → prune runs
        assertTrue(ep.anchors[0].n <= keepN && keepN < ep.anchors[1].n)
        assertTrue(ep.anchors.size < 15)
        // epochs entirely before retainFromN go too, the current one never does
        mic.nMic += W
        run(tl, mic, 1, overflow = true)
        tl.retainFromN = tl.n
        run(tl, mic, 60 * RATE / W + 5)
        assertEquals(listOf(1), tl.epochs.keys.toList())
    }

    // -- Kotlin-specific ---------------------------------------------------

    @Test
    fun samplesToNsIsExactAndHalfEven() {
        val tl = Timeline()
        assertEquals(62_500L, tl.samplesToNs(1))
        assertEquals(-62_500L, tl.samplesToNs(-1))
        assertEquals(30 * NS, tl.samplesToNs(480_000))
        // a two-week run at 16 kHz does not overflow
        val twoWeeks = 14L * 24 * 3600 * RATE
        assertEquals(14L * 24 * 3600 * NS, tl.samplesToNs(twoWeeks))
        // 3 samples at 7 Hz = 428571428.57 ns → rounds up; 1 at 3 Hz → 333333333.33 → down
        assertEquals(428_571_429L, Timeline(rate = 7).samplesToNs(3))
        assertEquals(333_333_333L, Timeline(rate = 3).samplesToNs(1))
        // exact half: 1 sample at 2e9/1 Hz is impossible with Int rate; use rate 400000000 → 2.5 ns → 2 (even)
        assertEquals(2L, Timeline(rate = 400_000_000).samplesToNs(1))
        assertEquals(8L, Timeline(rate = 400_000_000).samplesToNs(3)) // 7.5 → 8 (even)
    }

    @Test
    fun anchorForUsesLastAnchorAtOrBefore() {
        val e = Timeline.Epoch(0, 0, Anchor(100, 0))
        e.anchors.add(Anchor(200, 10))
        e.anchors.add(Anchor(300, 20))
        assertEquals(Anchor(100, 0), e.anchorFor(50))
        assertEquals(Anchor(200, 10), e.anchorFor(200))
        assertEquals(Anchor(200, 10), e.anchorFor(299))
        assertEquals(Anchor(300, 20), e.anchorFor(10_000))
        e.prune(250)
        assertEquals(listOf(Anchor(200, 10), Anchor(300, 20)), e.anchors)
    }
}
