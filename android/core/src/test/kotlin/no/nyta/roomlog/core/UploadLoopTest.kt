package no.nyta.roomlog.core

import java.io.File
import java.io.IOException
import java.nio.file.Files
import java.util.Collections
import kotlin.random.Random
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Probe scheduling, store-and-forward across an outage, wake and stop semantics. */
class UploadLoopTest {
    private lateinit var tmp: File
    private val logs: MutableList<String> = Collections.synchronizedList(mutableListOf())
    private var mono = 1_000_000_000_000L
    private val serverOffset = 1_790_000_000_000_000_000L

    @BeforeTest
    fun setUp() {
        tmp = Files.createTempDirectory("uploadloop").toFile()
    }

    @AfterTest
    fun tearDown() {
        tmp.deleteRecursively()
    }

    private var nextN = 0L
    private fun spoolSegment(sp: Spool) {
        val n = nextN
        nextN += 480000
        sp.write(
            "OggS-seg-$n".toByteArray(),
            Sidecar.build(
                deviceId = "lass22", sha256 = "0".repeat(64), utcNs = serverOffset + n * 62_500, nStart = n,
                nSamples = 480000, runId = "r", epoch = 0, discontinuity = false, clockStep = false,
                clockSynced = true, cutReason = "cap", vad = null, edgeVersion = "t", kind = "raw",
            ),
        )
    }

    /** A network that is up or down; while down, both calls fail like an unreachable host. */
    private class Net {
        @Volatile var up = true
        val ingest = UploaderTest.FakeIngest(listOf(201 to null))
        val puts: MutableList<String> = Collections.synchronizedList(mutableListOf())
        var probes = 0

        fun put(opus: ByteArray, meta: String, sha: String): Pair<Int, ByteArray> {
            if (!up) throw java.net.ConnectException("connect timed out")
            puts += opus.decodeToString()
            return ingest.put(opus, meta, sha)
        }
    }

    private fun loop(sp: Spool, net: Net, clock: ClockOffset = ClockOffset(), rng: Random = Random(0), idle: Double = 5.0) =
        UploadLoop(
            Uploader(sp, net::put, idlePollS = idle, rng = rng),
            clock,
            fetchServerUtcNs = {
                net.probes++
                if (!net.up) throw IOException("GET /v1/time: unreachable")
                mono + 20_000_000L + serverOffset
            },
            monoNow = { mono },
            log = { logs += it },
        )

    @Test
    fun outageAccumulatesThenDrainsInOrderAndDeletesOnlyAcked() {
        val sp = Spool(File(tmp, "spool"))
        val net = Net()
        val clock = ClockOffset()
        val l = loop(sp, net, clock)
        spoolSegment(sp)
        assertFalse(l.step().backoff)
        assertEquals(serverOffset + 20_000_000L, clock.offsetNs) // rtt 0 on the fake clock
        assertEquals(0, sp.stats().pendingFiles)

        // 30 minutes without the server: one segment per 30 s, nothing deleted
        net.up = false
        var k = 0
        repeat(60) {
            mono += 30_000_000_000L
            spoolSegment(sp)
            val next = l.step()
            k++
            assertTrue(next.backoff)
            assertTrue(next.delayS <= minOf(300.0, Math.pow(2.0, k.toDouble())))
        }
        assertEquals(60, sp.stats().pendingFiles)
        assertEquals(1, net.puts.size)
        assertFalse(clock.synced(mono)) // the last good probe is 30 min old
        assertTrue(logs.any { it.startsWith("clock probe failed") })
        assertTrue(logs.any { it.contains("retry 60 in") })

        // back: the next pass probes and drains the whole backlog, oldest first
        net.up = true
        mono += 30_000_000_000L
        val next = l.step()
        assertFalse(next.backoff)
        assertEquals(0.0, next.delayS)
        assertTrue(clock.synced(mono))
        assertEquals(0, sp.stats().pendingFiles)
        assertEquals((0 until 61).map { "OggS-seg-${it * 480000L}" }, net.puts)
        assertEquals(60, logs.count { it.startsWith("uploaded ") } - 1)
    }

    @Test
    fun foreignSegmentsAreLoggedOnceNotEveryPass() {
        val sp = Spool(File(tmp, "spool"))
        sp.write(
            "OggS-s22".toByteArray(),
            Sidecar.build(
                deviceId = "s22", sha256 = "0".repeat(64), utcNs = serverOffset, nStart = 0, nSamples = 480000,
                runId = "old", epoch = 0, discontinuity = false, clockStep = false, clockSynced = false,
                cutReason = "cap", vad = null, edgeVersion = "t", kind = "raw",
            ),
        )
        spoolSegment(sp)
        val net = Net()
        val l = UploadLoop(
            Uploader(sp, net::put, deviceId = "lass22"), ClockOffset(),
            fetchServerUtcNs = { mono + serverOffset }, monoNow = { mono }, log = { logs += it },
        )
        repeat(3) { assertFalse(l.step().backoff) }
        assertEquals(listOf("OggS-seg-0"), net.puts)
        assertEquals(1, logs.count { it.contains("other device ids") })
        assertEquals(1, sp.stats().pendingFiles)
    }

    @Test
    fun probeSchedule() {
        val sp = Spool(File(tmp, "spool"))
        val net = Net()
        val l = loop(sp, net)
        assertTrue(l.probeDue(mono))
        l.step()
        assertEquals(1, net.probes)
        mono += 4 * 60_000_000_000L
        l.step()
        assertEquals(1, net.probes) // ok probes every 5 min
        mono += 60_000_000_000L
        net.up = false
        l.step()
        assertEquals(2, net.probes)
        mono += 29_000_000_000L
        l.step()
        assertEquals(2, net.probes)
        mono += 1_000_000_000L
        l.step()
        assertEquals(3, net.probes) // failed probes retry every 30 s
    }

    @Test
    fun aSlowProbeIsFollowedByASecondOnTheWarmConnection() {
        val sp = Spool(File(tmp, "spool"))
        val rtts = ArrayDeque(listOf(900_000_000L, 20_000_000L))
        var fetches = 0
        val clock = ClockOffset()
        val l = UploadLoop(
            Uploader(sp, Net()::put), clock,
            fetchServerUtcNs = { fetches++; mono += rtts.removeFirst(); serverOffset + mono },
            monoNow = { mono }, log = { logs += it },
        )
        val p = l.probe()!!
        assertEquals(2, fetches)
        assertEquals(20_000_000L, p.rttNs)
        // the server stamped at the end of both round trips (true offset = serverOffset): the first
        // probe's midpoint is 450 ms off, the second clamps it into its 20 ms wide interval
        assertEquals(serverOffset + 20_000_000L, clock.offsetNs)
    }

    @Test
    fun rejectedProbeKeepsThePreviousOffset() {
        val sp = Spool(File(tmp, "spool"))
        val clock = ClockOffset(maxRttNs = 1_000_000L)
        val l = UploadLoop(
            Uploader(sp, Net()::put), clock,
            fetchServerUtcNs = { mono += 5_000_000L; serverOffset + mono },
            monoNow = { mono }, log = { logs += it },
        )
        assertNull(l.probe())
        assertNull(clock.last)
        assertTrue(logs.single().startsWith("clock probe rejected: rtt 5 ms"))
    }

    @Test
    fun wakeEndsAnIdleWaitAndStopMakesALastPass() {
        val sp = Spool(File(tmp, "spool"))
        val net = Net()
        val l = loop(sp, net, idle = 60.0)
        val t = Thread(l::run).apply { start() }
        waitFor { net.probes == 1 }
        spoolSegment(sp)
        l.wake()
        waitFor { net.puts.size == 1 }
        spoolSegment(sp)
        l.stop()
        t.join(5_000)
        assertFalse(t.isAlive)
        assertEquals(2, net.puts.size) // the last pass sent what was spooled before stop
    }

    /** Full jitter always at its cap, so the first backoff is 2 s. */
    private class MaxRandom : Random() {
        override fun nextBits(bitCount: Int): Int = (-1 ushr (32 - bitCount))
    }

    @Test
    fun wakeDoesNotShortenABackoff() {
        val sp = Spool(File(tmp, "spool"))
        val net = Net()
        net.up = false
        spoolSegment(sp)
        var attempts = 0
        val l = UploadLoop(
            Uploader(sp, { o, m, s -> attempts++; net.put(o, m, s) }, rng = MaxRandom()),
            ClockOffset(), fetchServerUtcNs = { throw IOException("down") }, monoNow = { mono }, log = { logs += it },
        )
        val t = Thread(l::run).apply { start() }
        waitFor { attempts == 1 }
        repeat(5) {
            l.wake()
            Thread.sleep(50)
        }
        assertEquals(1, attempts)
        net.up = true
        l.stop()
        t.join(5_000)
        assertEquals(2, attempts)
        assertEquals(0, sp.stats().pendingFiles)
        assertNotNull(logs.firstOrNull { it.startsWith("uploaded ") })
    }

    private fun waitFor(cond: () -> Boolean) {
        val end = System.nanoTime() + 5_000_000_000L
        while (!cond()) {
            check(System.nanoTime() < end) { "timed out; log: $logs" }
            Thread.sleep(10)
        }
    }
}
