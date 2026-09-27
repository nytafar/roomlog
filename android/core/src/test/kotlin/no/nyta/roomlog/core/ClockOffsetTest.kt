package no.nyta.roomlog.core

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

class ClockOffsetTest {
    private val s = 1_000_000_000L
    private val min = 60 * s

    @Test
    fun cristianOffsetUsesTheRoundTripMidpoint() {
        val c = ClockOffset()
        // mono 1000 s → 1000.040 s; the server stamped 1_790_000_000.020 s, i.e. at the midpoint
        val p = c.record(tSendMono = 1000 * s, tRecvMono = 1000 * s + 40_000_000, serverUtcNs = 1_790_000_000 * s + 20_000_000)!!
        assertEquals(40_000_000L, p.rttNs)
        assertEquals(1_790_000_000 * s - 1000 * s, p.offsetNs)
        assertEquals(p.offsetNs, c.offsetNs)
        // a sample captured at mono m maps to server time m + offset
        assertEquals(1_790_000_000 * s + 20_000_000, 1000 * s + 20_000_000 + c.offsetNs!!)
    }

    @Test
    fun syncedWithinTenMinutesOfTheLastSuccess() {
        val c = ClockOffset()
        assertFalse(c.synced(0))
        c.record(100 * s, 100 * s + 10_000_000, 5_000 * s)
        val at = 100 * s + 10_000_000
        assertTrue(c.synced(at))
        assertTrue(c.synced(at + 10 * min))
        assertFalse(c.synced(at + 10 * min + 1))
        assertFalse(c.synced(at - 1)) // a clock that went backwards proves nothing
    }

    @Test
    fun failedAndImpreciseProbesKeepThePreviousOne() {
        val c = ClockOffset()
        var mono = 0L
        val tick = { mono += 20_000_000; mono }
        val good = c.probe(tick) { 7_000 * s }!!
        assertEquals(20_000_000L, good.rttNs)
        assertNull(c.probe(tick) { throw java.io.IOException("tailnet down") })
        assertEquals(good, c.last)
        // a 6 s round trip is rejected
        assertNull(c.record(0, 6 * s, 7_000 * s))
        assertNull(c.record(10, 5, 7_000 * s)) // negative rtt
        assertEquals(good, c.last)
        // the ten minutes run from the last success, not from later failures
        assertTrue(c.synced(good.atMonoNs + 10 * min))
        assertFalse(c.synced(good.atMonoNs + 11 * min))
    }

    @Test
    fun aSlowProbeDoesNotMoveAnEstablishedOffset() {
        val c = ClockOffset()
        val o = 1_790_000_000 * s - 1000 * s
        c.record(1000 * s, 1000 * s + 40_000_000, 1000 * s + 20_000_000 + o)
        assertEquals(o, c.offsetNs)
        // 4.9 s round trip (radio waking, DNS + TCP + TLS), the server stamped at its very end:
        // the midpoint would be 2.45 s off. The true offset is only known to lie in the
        // interval, which contains the current one, so nothing moves; `synced` is refreshed.
        val t0 = 1300 * s
        val t1 = t0 + 4_900_000_000L
        val p = c.record(t0, t1, t1 + o)!!
        assertEquals(o, p.offsetNs)
        assertEquals(t1, p.atMonoNs)
        // and a timeline fed with it takes no step
        val tl = Timeline()
        var mono = 2000 * s
        repeat(10) { mono += 100_000_000; tl.feed(Timeline.Block(1600, mono, mono + o, 100_000_000)) }
        assertNull(tl.observeOffset(mono + c.offsetNs!!, mono))
    }

    @Test
    fun aRealStepMovesTheOffsetToTheProbesInterval() {
        val c = ClockOffset()
        val o = 5_000 * s
        c.record(1000 * s, 1000 * s + 40_000_000, 1000 * s + 20_000_000 + o)
        // the server's clock stepped 2 s forward: a fast probe proves the old offset wrong
        c.record(1300 * s, 1300 * s + 40_000_000, 1300 * s + 20_000_000 + o + 2 * s)
        val moved = c.offsetNs!! - o
        assertTrue(moved in (2 * s - 20_000_000)..(2 * s + 20_000_000), "moved $moved")
        // drift just past the interval moves only as far as its edge
        val before = c.offsetNs!!
        c.record(1600 * s, 1600 * s + 10_000_000, 1600 * s + before + 15_000_000) // interval [before+5ms, before+15ms]
        assertEquals(before + 5_000_000, c.offsetNs)
    }

    @Test
    fun offsetDrivesTimelineStepOnResync() {
        // Unsynced start on the device clock, then a probe: observeOffset re-stamps held segments.
        val tl = Timeline()
        val devOffset = 1_790_000_000 * s - 1000 * s - 90 * s // device wall clock 90 s slow
        var mono = 1000 * s
        repeat(100) {
            mono += 100_000_000
            tl.feed(Timeline.Block(1600, mono, mono + devOffset, 100_000_000))
        }
        val held = tl.utcNs(16_000)
        val c = ClockOffset()
        c.record(mono, mono + 30_000_000, mono + 15_000_000 + devOffset + 90 * s)
        val delta = tl.observeOffset(mono + c.offsetNs!!, mono)
        assertEquals(90 * s, delta)
        assertEquals(held + 90 * s, tl.utcNs(16_000))
    }
}
