package no.nyta.roomlog.core

import org.junit.jupiter.api.Assumptions.assumeTrue
import java.io.File
import java.nio.file.Files
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/**
 * The P3 upload path over real HTTP against a running ingest: outage, probe,
 * re-stamp of held segments, PUT, ack, delete, and an idempotent re-PUT.
 * Skipped unless all three are set (never against the production server):
 *
 * - `ROOMLOG_LIVE_URL`: base URL of a throwaway ingest, e.g. `http://127.0.0.1:18480`
 * - `ROOMLOG_LIVE_TOKEN_FILE`: a file holding that ingest's token for the segments' device
 * - `ROOMLOG_LIVE_SEGMENTS`: a directory of real `.opus` + `.json` raw segments of one
 *   run (e.g. a pulled `spool/unsynced/`); copied, never modified
 */
class LiveIngestTest {
    private lateinit var tmp: File
    private val logs = mutableListOf<String>()

    @BeforeTest
    fun setUp() {
        tmp = Files.createTempDirectory("live").toFile()
    }

    @AfterTest
    fun tearDown() {
        tmp.deleteRecursively()
    }

    @Test
    fun outageProbeRestampUploadAckDelete() {
        val url = System.getenv("ROOMLOG_LIVE_URL")
        val tokenFile = System.getenv("ROOMLOG_LIVE_TOKEN_FILE")
        val segDir = System.getenv("ROOMLOG_LIVE_SEGMENTS")
        assumeTrue(url != null && tokenFile != null && segDir != null, "ROOMLOG_LIVE_* not set")
        val token = File(tokenFile!!).readText().trim()

        val sp = Spool(File(tmp, "spool"))
        val sources = File(segDir!!).listFiles { f -> f.name.endsWith(".opus") }!!.sortedBy { it.name }
        assertTrue(sources.size >= 3, "need at least 3 segments in $segDir")
        for (f in sources) {
            f.copyTo(File(sp.dir("unsynced"), f.name))
            File(f.parentFile, f.name.removeSuffix(".opus") + ".json").copyTo(File(sp.dir("unsynced"), f.name.removeSuffix(".opus") + ".json"))
        }
        val metas = sp.entries("unsynced").map { it.readMeta() }
        val runId = metas[0]["run_id"] as String
        val deviceId = metas[0]["device_id"] as String
        assertTrue(metas.all { it["run_id"] == runId && it["epoch"] == 0L }, "one run, one epoch")

        // This run's timeline on the device clock, long enough for every segment,
        // with each segment closed while no probe had succeeded: all held.
        val lastEnd = metas.maxOf { (it["n_start"] as Long) + (it["n_samples"] as Long) }
        val blocks = (lastEnd / 1600 + 1).toInt()
        val base = System.nanoTime() - blocks * 100_000_000L
        val deviceOffset = System.currentTimeMillis() * 1_000_000 - System.nanoTime() - 1_500_000_000L // 1.5 s slow
        val tl = Timeline()
        for (k in 0 until blocks) {
            val mono = base + (k + 1) * 100_000_000L
            tl.feed(Timeline.Block(1600, mono, mono + deviceOffset, 100_000_000L))
        }
        // The first segment stands in for an earlier run's leftover: moved to pending as-is.
        // The others were closed in this run while no probe had succeeded: held.
        sp.move(sp.entries("unsynced").first(), "pending")
        val hold = UnsyncedHold()
        for (m in metas.drop(1)) assertEquals(false, hold.onClose(m["n_start"] as Long, 0, tl))

        // Outage: nothing listens. Probe fails, the PUT retries, nothing is deleted.
        var http = Http("http://127.0.0.1:1", token, timeoutMs = 2_000, connectTimeoutMs = 2_000)
        val clock = ClockOffset()
        val loop = UploadLoop(
            Uploader(sp, { o, m, s -> http.put(o, m, s) }),
            clock,
            fetchServerUtcNs = { http.serverUtcNs() },
            monoNow = System::nanoTime,
            log = { logs += it; println(it) },
            realNow = { System.currentTimeMillis() * 1_000_000 },
            probeRetryNs = 0,
        )
        repeat(2) { assertTrue(loop.step().backoff) }
        assertEquals(null, clock.last)
        assertEquals(1, sp.stats().pendingFiles)
        assertEquals(sources.size - 1, sp.stats().unsyncedFiles)

        // Back: the probe succeeds and the leftover uploads.
        http = Http(url!!, token)
        val n = loop.step()
        assertTrue(!n.backoff, "log: $logs")
        assertTrue(clock.synced(System.nanoTime()))
        assertEquals(0, sp.stats().pendingFiles)

        // The capture side sees the probe: held segments re-stamped in the server timebase.
        val restamps = hold.update(true, tl, System.nanoTime(), clock.offsetNs)!!
        assertEquals(sources.size - 1, restamps.size)
        assertTrue(restamps.values.all { it.clockStep }) // 1.5 s is a step
        val r = UnsyncedHold.release(sp, runId, deviceId, restamps)
        assertEquals(sources.size - 1, r.restamped)
        val restamped = sp.entries("pending").map { it.readMeta() }
        assertTrue(restamped.all { it["clock_synced"] == true && it["clock_step"] == true })
        assertTrue(restamped.all { Sidecar.validate(it).isEmpty() })

        val outs = loop.step()
        assertTrue(!outs.backoff, "log: $logs")
        assertEquals(0, sp.stats().files)
        assertEquals(sources.size, logs.count { it.startsWith("uploaded ") }, "log: $logs") // 201, or 200 on a re-run

        // Re-PUT of one segment: 200 exists, acked and deleted.
        val again = sources[1]
        again.copyTo(File(sp.dir("pending"), again.name))
        File(again.parentFile, again.name.removeSuffix(".opus") + ".json").copyTo(File(sp.dir("pending"), again.name.removeSuffix(".opus") + ".json"))
        loop.step()
        assertEquals(0, sp.stats().files)
        assertTrue(logs.last().startsWith("uploaded ") && logs.last().endsWith(": 200"), "log: $logs")
    }
}
