package no.nyta.roomlog.core

import no.nyta.roomlog.core.RawSegmenter.CutReason
import no.nyta.roomlog.core.RawSegmenter.Event
import no.nyta.roomlog.core.RawSegmenter.Segment
import no.nyta.roomlog.core.Timeline.BlockResult
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class RawSegmenterTest {
    private val s = 480_000L
    private val w = TimelineTest.W

    /** Replays events like the encoder would and checks their grammar: every
     *  sample exactly once, in order, each Close covering exactly its Samples. */
    private class Sink {
        val segments = mutableListOf<Segment>()
        var next = 0L
        private var open: Event.Open? = null
        private var openTo = 0L

        fun take(events: List<Event>) {
            for (e in events) when (e) {
                is Event.Open -> {
                    check(open == null) { "open while open" }
                    check(e.nStart == next) { "open at ${e.nStart}, expected $next" }
                    open = e
                    openTo = e.nStart
                }
                is Event.Samples -> {
                    check(open != null) { "samples without open" }
                    check(e.nStart == openTo && e.nEnd > e.nStart) { "samples $e after $openTo" }
                    openTo = e.nEnd
                    next = e.nEnd
                }
                is Event.Close -> {
                    val o = open ?: error("close without open")
                    val seg = e.segment
                    check(seg.nStart == o.nStart && seg.nEnd == openTo && seg.epoch == o.epoch)
                    check(seg.discontinuity == o.discontinuity)
                    segments += seg
                    open = null
                }
            }
        }

        val isOpen get() = open != null
    }

    private fun steady(nStart: Long, nFrames: Int, epoch: Int = 0, newEpoch: Boolean = false, lateness: Long = 0) =
        BlockResult(nStart, nStart + nFrames, epoch, newEpoch, null, lateness, nStart)

    @Test
    fun capCutsAreExactOnTheSampleCounter() {
        val seg = RawSegmenter()
        val sink = Sink()
        var n = 0L
        sink.take(seg.feed(steady(0, w, newEpoch = true)))
        n += w
        // 100 s of 512-frame blocks: 480000 is not a multiple of 512, so blocks straddle boundaries
        while (n < 100L * 16000) {
            sink.take(seg.feed(steady(n, w)))
            n += w
        }
        sink.take(seg.shutdown())
        val segs = sink.segments
        assertEquals(listOf(0L, s, 2 * s, 3 * s), segs.map { it.nStart })
        assertEquals(listOf(CutReason.CAP, CutReason.CAP, CutReason.CAP, CutReason.SHUTDOWN), segs.map { it.reason })
        assertTrue(segs.dropLast(1).all { it.nSamples == s })
        assertEquals(n, segs.last().nEnd)
        assertTrue(segs.zipWithNext().all { (a, b) -> a.nEnd == b.nStart })
        assertTrue(segs.none { it.discontinuity })
        assertEquals(seg.shutdown(), emptyList()) // idempotent
    }

    @Test
    fun blockStraddlingBoundaryIsSplit() {
        val seg = RawSegmenter(segmentSamples = 1000)
        seg.feed(steady(0, 600, newEpoch = true))
        val ev = seg.feed(steady(600, 600))
        assertEquals(
            listOf(
                Event.Samples(600, 1000),
                Event.Close(Segment(0, 0, 1000, CutReason.CAP, false)),
                Event.Open(0, 1000, false),
                Event.Samples(1000, 1200),
            ),
            ev,
        )
    }

    @Test
    fun xrunClosesAtTheGapAndStartsNewEpochGrid() {
        val seg = RawSegmenter(segmentSamples = 10_000)
        val sink = Sink()
        sink.take(seg.feed(steady(0, 1000, newEpoch = true)))
        for (i in 1 until 25) sink.take(seg.feed(steady(i * 1000L, 1000)))
        // overflow at n = 25000: epoch 1 starts here, its grid is 25000 + k·10000
        sink.take(seg.feed(BlockResult(25_000, 26_000, 1, true, null, 0, 25_000)))
        for (i in 26 until 50) sink.take(seg.feed(steady(i * 1000L, 1000, epoch = 1)))
        sink.take(seg.shutdown())
        val got = sink.segments.map { listOf(it.epoch.toLong(), it.nStart, it.nEnd) to (it.reason to it.discontinuity) }
        assertEquals(
            listOf(
                listOf(0L, 0L, 10_000L) to (CutReason.CAP to false),
                listOf(0L, 10_000L, 20_000L) to (CutReason.CAP to false),
                listOf(0L, 20_000L, 25_000L) to (CutReason.DISCONTINUITY to false),
                listOf(1L, 25_000L, 35_000L) to (CutReason.CAP to true),
                listOf(1L, 35_000L, 45_000L) to (CutReason.CAP to false),
                listOf(1L, 45_000L, 50_000L) to (CutReason.SHUTDOWN to false),
            ),
            got,
        )
    }

    @Test
    fun lossExactlyOnABoundaryHasNoShortSegment() {
        val seg = RawSegmenter(segmentSamples = 10_000)
        val sink = Sink()
        sink.take(seg.feed(steady(0, 1000, newEpoch = true)))
        for (i in 1 until 10) sink.take(seg.feed(steady(i * 1000L, 1000)))
        sink.take(seg.feed(BlockResult(10_000, 11_000, 1, true, null, 0, 10_000)))
        sink.take(seg.shutdown())
        assertEquals(
            listOf(
                Segment(0, 0, 10_000, CutReason.CAP, false),
                Segment(1, 10_000, 11_000, CutReason.SHUTDOWN, true),
            ),
            sink.segments,
        )
    }

    @Test
    fun silentLossMovesHeldBlocksIntoTheNewEpoch() {
        // Real timeline: 100 blocks, then 224 ms lost without a flag.
        val tl = Timeline()
        val mic = TimelineTest.Mic()
        val seg = RawSegmenter(segmentSamples = 20_000, latenessLimitNs = tl.latenessLimitNs)
        val sink = Sink()
        fun feed(k: Int) = repeat(k) { sink.take(seg.feed(tl.feed(mic.block().first))) }
        feed(100) // n = 51200
        mic.nMic += 7 * w
        feed(1)
        assertEquals(w.toLong(), seg.heldSamples) // first late block held
        feed(1)
        assertEquals(2L * w, seg.heldSamples)
        assertEquals(100L * w, sink.next) // nothing past the gap reached the encoder
        feed(1) // third late block: epoch 1 decided, starting at the first late block
        assertEquals(0L, seg.heldSamples)
        feed(80) // epoch 1 now spans 83 blocks = 42496 samples: three segments on its grid
        sink.take(seg.shutdown())
        val gap = 100L * w
        val e0 = sink.segments.filter { it.epoch == 0 }
        val e1 = sink.segments.filter { it.epoch == 1 }
        assertEquals(gap, e0.last().nEnd)
        assertEquals(CutReason.DISCONTINUITY, e0.last().reason)
        assertEquals(gap, e1.first().nStart)
        assertTrue(e1.first().discontinuity)
        assertEquals(listOf(gap, gap + 20_000, gap + 40_000), e1.map { it.nStart }) // epoch-1 grid
        assertEquals(tl.n, sink.segments.last().nEnd)
    }

    @Test
    fun hiccupReleasesHeldBlocksIntoTheSameSegmentAcrossACap() {
        val seg = RawSegmenter(segmentSamples = 10_000)
        val sink = Sink()
        sink.take(seg.feed(steady(0, 1000, newEpoch = true)))
        for (i in 1 until 9) sink.take(seg.feed(steady(i * 1000L, 1000)))
        // two late blocks straddling the 10000 boundary, then on time again
        sink.take(seg.feed(steady(9000, 1000, lateness = 250_000_000)))
        sink.take(seg.feed(steady(10_000, 1000, lateness = 250_000_000)))
        assertEquals(9000L, sink.next)
        sink.take(seg.feed(steady(11_000, 1000)))
        assertEquals(12_000L, sink.next)
        sink.take(seg.shutdown())
        assertEquals(
            listOf(Segment(0, 0, 10_000, CutReason.CAP, false), Segment(0, 10_000, 12_000, CutReason.SHUTDOWN, false)),
            sink.segments,
        )
    }

    @Test
    fun xrunAfterLateBlocksGivesHeldBlocksToTheOldEpoch() {
        val seg = RawSegmenter(segmentSamples = 10_000)
        val sink = Sink()
        sink.take(seg.feed(steady(0, 1000, newEpoch = true)))
        sink.take(seg.feed(steady(1000, 1000, lateness = 300_000_000)))
        // the timeline opens epoch 1 at the overflow block itself
        sink.take(seg.feed(BlockResult(2000, 3000, 1, true, null, 0, 2000)))
        sink.take(seg.shutdown())
        assertEquals(
            listOf(Segment(0, 0, 2000, CutReason.DISCONTINUITY, false), Segment(1, 2000, 3000, CutReason.SHUTDOWN, true)),
            sink.segments,
        )
    }

    @Test
    fun shutdownReleasesHeldAndSkipsEmptySegments() {
        val seg = RawSegmenter(segmentSamples = 1000)
        val sink = Sink()
        sink.take(seg.feed(steady(0, 1000, newEpoch = true))) // exactly one full segment
        assertTrue(!sink.isOpen)
        sink.take(seg.shutdown()) // nothing open: no empty shutdown segment
        assertEquals(listOf(Segment(0, 0, 1000, CutReason.CAP, false)), sink.segments)

        val seg2 = RawSegmenter(segmentSamples = 1000)
        val sink2 = Sink()
        sink2.take(seg2.feed(steady(0, 300, newEpoch = true)))
        sink2.take(seg2.feed(steady(300, 300, lateness = 500_000_000)))
        sink2.take(seg2.shutdown()) // the held block is real audio: it goes into the last segment
        assertEquals(listOf(Segment(0, 0, 600, CutReason.SHUTDOWN, false)), sink2.segments)
        assertFailsWith<IllegalStateException> { seg2.feed(steady(600, 300)) }
    }

    @Test
    fun sampleRingCopiesOutRecentSamples() {
        val ring = SampleRing(10)
        ring.write(0, ShortArray(6) { it.toShort() })
        ring.write(6, ShortArray(8) { (6 + it).toShort() })
        assertEquals(4L, ring.nStart)
        assertEquals((4..13).map { it.toShort() }, ring.read(4, 14).toList())
        assertEquals(listOf<Short>(9, 10), ring.read(9, 11).toList())
        assertFailsWith<IllegalArgumentException> { ring.read(3, 5) }
        assertFailsWith<IllegalArgumentException> { ring.write(20, ShortArray(1)) }
    }
}
