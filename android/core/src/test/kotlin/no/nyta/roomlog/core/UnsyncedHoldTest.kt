package no.nyta.roomlog.core

import java.io.File
import java.nio.file.Files
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** The unsynced hold of ADR 0006: capture-side bookkeeping and the spool-side release. */
class UnsyncedHoldTest {
    private lateinit var tmp: File
    private val run = "11111111-2222-4333-8444-555555555555"
    private val base = 7_000_000_000_000L // capture clock at n=0
    private val device = 1_790_000_000_000_000_000L // device wall clock − mono

    @BeforeTest
    fun setUp() {
        tmp = Files.createTempDirectory("hold").toFile()
    }

    @AfterTest
    fun tearDown() {
        tmp.deleteRecursively()
    }

    /** [blocks] 100 ms blocks on the device clock. */
    private fun timeline(blocks: Int): Timeline {
        val tl = Timeline()
        for (k in 0 until blocks) {
            val mono = base + (k + 1) * 100_000_000L
            tl.feed(Timeline.Block(1600, mono, mono + device, 100_000_000L))
        }
        return tl
    }

    private val nowMono get() = base + 10_000_000_000L

    @Test
    fun heldSegmentsAreRestampedThroughTheServerOffset() {
        val tl = timeline(100)
        val hold = UnsyncedHold()
        assertNull(hold.update(false, tl, nowMono, null)) // no transition, starts unsynced
        assertEquals(false, hold.onClose(0, 0, tl))
        assertEquals(false, hold.onClose(8000, 0, tl))
        assertEquals(0L, hold.minHeldN)
        val before = tl.utcNs(8000, 0)

        val server = device + 2_000_000_000L // the device clock is 2 s behind the server
        val r = hold.update(true, tl, nowMono, server)!!
        assertEquals(setOf(0L, 8000L), r.keys)
        assertEquals(before + 2_000_000_000L, r.getValue(8000).utcNs)
        assertEquals(tl.utcNs(0, 0), r.getValue(0).utcNs)
        assertTrue(r.values.all { it.clockStep }) // a step was applied to stamps made before it
        assertEquals(0, hold.heldCount)
        assertNull(hold.minHeldN)
        assertEquals(true, hold.onClose(16000, 0, tl)) // synced now: straight to pending

        // the probe ages out: held again, released without a step on the next success
        assertNull(hold.update(false, tl, nowMono, server))
        assertEquals(false, hold.onClose(24000, 0, tl))
        val again = hold.update(true, tl, nowMono, server)!!
        assertEquals(listOf(24000L), again.keys.toList())
        assertEquals(false, again.getValue(24000).clockStep)
    }

    @Test
    fun offsetWithinTheStepLimitRestampsWithoutAStep() {
        val tl = timeline(50)
        val hold = UnsyncedHold()
        hold.onClose(0, 0, tl)
        val r = hold.update(true, tl, nowMono, device + 30_000_000L)!!
        assertEquals(false, r.getValue(0).clockStep)
        assertEquals(base + device, r.getValue(0).utcNs) // the mapping did not move
    }

    private fun seg(sp: Spool, runId: String, nStart: Long, deviceId: String = "lass22", utcNs: Long = 1_790_000_000_000_000_000L + nStart * 62_500) =
        sp.write(
            "opus-$runId-$nStart-$deviceId".toByteArray(),
            Sidecar.build(
                deviceId = deviceId, sha256 = "0".repeat(64), utcNs = utcNs, nStart = nStart, nSamples = 480000,
                runId = runId, epoch = 0, discontinuity = false, clockStep = false, clockSynced = false,
                cutReason = "cap", vad = null, edgeVersion = "t", kind = "raw",
            ),
            dest = "unsynced",
        )

    @Test
    fun releaseRestampsOwnMovesOthersAndKeepsForeignDevices() {
        val sp = Spool(File(tmp, "spool"))
        seg(sp, run, 0)
        seg(sp, run, 480000)
        seg(sp, "old-run", 0)
        seg(sp, "old-run", 0, deviceId = "s22")
        seg(sp, "bad", 960000).json.writeText("{broken")

        val newUtc = 1_790_000_100_123_000_000L
        val r = UnsyncedHold.release(sp, run, "lass22", mapOf(0L to UnsyncedHold.Restamp(newUtc, true)))
        assertEquals(UnsyncedHold.Released(restamped = 1, moved = 1, foreign = 1, failed = 1, kept = 1), r)

        val pending = sp.entries("pending").map { it.readMeta() }
        val own = pending.single { it["run_id"] == run }
        assertEquals(Sidecar.formatUtc(newUtc), own["start_utc"])
        assertEquals(true, own["clock_synced"])
        assertEquals(true, own["clock_step"])
        assertTrue(Sidecar.validate(own).isEmpty())
        val old = pending.single { it["run_id"] == "old-run" }
        assertEquals(false, old["clock_synced"]) // another run's mapping is gone: as-is
        assertEquals(listOf("s22", "lass22"), sp.entries("unsynced").map { it.readMeta()["device_id"] }.sortedBy { it != "s22" })
        assertEquals(1, sp.stats().failedFiles)
        // stems follow the new start_utc; audio untouched
        assertTrue(sp.entries("pending").all { Spool.stemFor(it.readMeta()) == it.stem })

        // shutdown: this run's remaining held segments go as-is
        val last = UnsyncedHold.release(sp, run, "lass22", releaseOwn = true)
        assertEquals(1, last.moved)
        assertEquals(listOf("s22"), sp.entries("unsynced").map { it.readMeta()["device_id"] })
        assertEquals(3, sp.entries("pending").size)
    }

    @Test
    fun aFailedMoveLeavesTheSegmentHeldAndDoesNotThrow() {
        val sp = Spool(File(tmp, "spool"))
        seg(sp, run, 0)
        seg(sp, run, 480000)
        val pending = sp.dir("pending")
        pending.setWritable(false) // stands in for a full disk
        try {
            val r = UnsyncedHold.release(
                sp, run, "lass22",
                mapOf(0L to UnsyncedHold.Restamp(1_790_000_100_000_000_000L, false), 480000L to UnsyncedHold.Restamp(1_790_000_130_000_000_000L, false)),
            )
            assertEquals(2, r.errors)
            assertEquals(0, r.restamped)
            assertEquals(2, sp.entries("unsynced").size) // audio untouched, still there
        } finally {
            pending.setWritable(true)
        }
        // released as stamped at shutdown once the disk has room again
        assertEquals(2, UnsyncedHold.release(sp, run, "lass22", releaseOwn = true).moved)
        sp.cleanupTmp()
        assertEquals(2, sp.entries("pending").size)
    }
}
