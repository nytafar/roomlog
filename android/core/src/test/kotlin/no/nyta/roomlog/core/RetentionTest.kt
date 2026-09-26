package no.nyta.roomlog.core

import no.nyta.roomlog.core.Timeline.Companion.NS
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

class RetentionTest {
    private val w = TimelineTest.W

    @Test
    fun minimumOverLiveInflightAndHeld() {
        val r = Retention()
        assertEquals(5000L, r.retainFromN(5000))
        r.startInflight(4000)
        assertEquals(4000L, r.retainFromN(5000))
        r.hold("20260926T100000000Z_aaaaaaaa", 1000)
        r.hold("20260926T100030000Z_bbbbbbbb", 3000)
        r.endInflight(4000)
        assertEquals(1000L, r.retainFromN(5000))
        r.release("20260926T100000000Z_aaaaaaaa")
        assertEquals(3000L, r.retainFromN(5000))
        assertEquals(1, r.heldCount)
        r.release("20260926T100030000Z_bbbbbbbb")
        assertEquals(5000L, r.retainFromN(5000))
        assertEquals(0L, r.retainFromN(-7))
    }

    /** Twenty minutes of drifting capture with a loss in between, a segment held in
     *  unsynced/ from the first minute, then a probe corrects the clock: the held
     *  segment must still map through its own epoch and anchor. */
    private fun holdThenRestamp(holdIt: Boolean): Pair<Long, Long>? {
        val tl = Timeline()
        val mic = TimelineTest.Mic(ppm = 50.0)
        mic.offset -= 90 * NS // unsynced device clock, 90 s slow
        val retention = Retention()
        val heldN = 10L * w
        fun feed(k: Int, overflow: Boolean = false) = repeat(k) {
            val (b, _) = mic.block(overflow = overflow && it == 0)
            val r = tl.feed(b)
            // live state: pretend only the last block is still open
            tl.retainFromN = retention.retainFromN(r.nStart)
        }
        feed(40)
        val stale = tl.utcNs(heldN, 0)
        if (holdIt) retention.hold("held", heldN)
        feed(10 * 60 * 16000 / w)
        mic.nMic += 5 * w
        feed(10 * 60 * 16000 / w, overflow = true)
        assertEquals(1, tl.epoch!!.id)
        val delta = tl.observeOffset(mic.trueReal(mic.nMic) + 90 * NS, mic.trueMono(mic.nMic))
        assertEquals(90 * NS, delta)
        return try {
            stale to tl.utcNs(heldN, 0)
        } catch (e: NoSuchElementException) {
            null
        }
    }

    @Test
    fun heldSegmentKeepsItsEpochAndAnchorForRestamping() {
        val (stale, restamped) = holdThenRestamp(holdIt = true)!!
        // exact up to the first drift re-reference, which may add an anchor before the held sample
        assertTrue(kotlin.math.abs(restamped - (stale + 90 * NS)) < 1_000_000, "${restamped - stale - 90 * NS} ns")
    }

    @Test
    fun withoutHoldingTheEpochIsPruned() {
        // the failure the retention prevents: epoch 0 is gone when the probe arrives
        assertEquals(null, holdThenRestamp(holdIt = false))
    }
}
