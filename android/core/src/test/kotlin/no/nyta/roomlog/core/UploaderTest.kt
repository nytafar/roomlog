package no.nyta.roomlog.core

import no.nyta.roomlog.core.UploadPolicy.Action.ACK
import no.nyta.roomlog.core.UploadPolicy.Action.FAIL
import no.nyta.roomlog.core.UploadPolicy.Action.RETRY
import java.io.File
import java.io.IOException
import java.nio.file.Files
import kotlin.random.Random
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Port of `edge/tests/test_uploader.py` against a scripted fake `put`. */
class UploaderTest {
    private lateinit var tmp: File
    private val run = "b0d0f1a2-3c4d-4e5f-8a9b-0c1d2e3f4a5b"

    @BeforeTest
    fun setUp() {
        tmp = Files.createTempDirectory("uploader").toFile()
    }

    @AfterTest
    fun tearDown() {
        tmp.deleteRecursively()
    }

    data class Request(val opus: ByteArray, val metaWire: String, val sha: String)

    /**
     * Scripted responses: one (status, body-or-null) per request, the last one
     * repeats. Status -1 drops the connection (IOException). A null body is
     * the server's normal body for that status.
     */
    class FakeIngest(script: List<Pair<Int, String?>>) {
        private val script = script.toMutableList()
        val requests = mutableListOf<Request>()

        fun put(opus: ByteArray, metaWire: String, sha: String): Pair<Int, ByteArray> {
            requests += Request(opus, metaWire, sha)
            val (status, body) = if (script.size > 1) script.removeAt(0) else script[0]
            if (status == -1) throw IOException("connection reset")
            val resp = body ?: if (status == 200 || status == 201) {
                Json.dumps(linkedMapOf(
                    "sha256" to Spool.sha256Hex(opus),
                    "status" to if (status == 201) "created" else "exists",
                    "path" to "2026/09/26/x.opus",
                ))
            } else {
                Json.dumps(linkedMapOf("error" to "status $status"))
            }
            return status to resp.toByteArray()
        }
    }

    private fun spoolWith(n: Int = 1): Spool {
        val sp = Spool(File(tmp, "spool"))
        for (i in 0 until n) {
            val meta = Sidecar.build(
                deviceId = "oma", sha256 = "0".repeat(64), utcNs = 1_790_000_000_000_000_000L + i * 1_000_000_000L,
                nStart = i * 16000L, nSamples = 16000, runId = run, epoch = 0, discontinuity = false,
                clockStep = false, clockSynced = true, cutReason = "silence", vad = emptyMap(), edgeVersion = "0.1.0",
            )
            sp.write("OggS-chunk-$i".toByteArray(), meta)
        }
        return sp
    }

    private fun uploader(sp: Spool, srv: FakeIngest) = Uploader(sp, srv::put, rng = Random(0))

    @Test
    fun createdAndExistsAckAndDelete() {
        val sp = spoolWith(2)
        val srv = FakeIngest(listOf(201 to null, 200 to null))
        val up = uploader(sp, srv)
        val outcomes = up.runOnce()
        assertEquals(listOf(ACK, ACK), outcomes.map { it.action })
        assertEquals(emptyList(), sp.entries("pending"))
        val req = srv.requests[0]
        val meta = Json.parseObject(req.metaWire)
        assertEquals(meta["sha256"], req.sha) // the URL path segment
        assertEquals(Spool.sha256Hex(req.opus), meta["sha256"])
        assertTrue('\n' !in req.metaWire && req.metaWire.all { it.code < 128 })
        assertEquals("OggS-chunk-0", req.opus.decodeToString()) // oldest first
        assertEquals(0, up.failures)
        assertNotNull(up.lastSuccessNs)
        assertEquals(2, up.uploadedTotal)
    }

    @Test
    fun permanentFailuresGoToFailed() {
        for (status in listOf(409, 413, 422)) {
            tmp.deleteRecursively()
            val sp = spoolWith(1)
            val up = uploader(sp, FakeIngest(listOf(status to null)))
            val o = up.runOnce().single()
            assertTrue(o.action == FAIL && o.status == status)
            assertTrue(sp.entries("pending").isEmpty() && sp.stats().failedFiles == 1)
            assertEquals(0, up.failures) // not a backoff condition
            assertEquals(1, up.failedTotal)
        }
    }

    @Test
    fun retryableStatusesKeepFileAndBackOff() {
        for (status in listOf(401, 403, 411, 500, 503)) {
            tmp.deleteRecursively()
            val sp = spoolWith(2)
            val up = uploader(sp, FakeIngest(listOf(status to null)))
            val outcomes = up.runOnce()
            assertTrue(outcomes.size == 1 && outcomes[0].action == RETRY, "status $status") // stops at the first retry
            assertEquals(2, sp.entries("pending").size)
            assertEquals(1, up.failures)
            val d = up.nextDelayS(outcomes)
            assertTrue(d in 0.0..2.0)
        }
    }

    @Test
    fun connectionErrorIsRetry() {
        val sp = spoolWith(1)
        val up = Uploader(sp, { _, _, _ -> throw java.net.ConnectException("nothing listens") }, rng = Random(0))
        val o = up.runOnce().single()
        assertTrue(o.action == RETRY && o.status == null)
        assertEquals(1, sp.entries("pending").size)
    }

    @Test
    fun droppedConnectionIsRetryThenAck() {
        val sp = spoolWith(1)
        val up = uploader(sp, FakeIngest(listOf(-1 to null, 201 to null)))
        assertEquals(RETRY, up.runOnce().single().action)
        assertEquals(1, sp.entries("pending").size)
        val o = up.runOnce().single()
        assertTrue(o.action == ACK && up.failures == 0)
    }

    @Test
    fun ackWithWrongShaInBodyKeepsFile() {
        val sp = spoolWith(1)
        val body = Json.dumps(linkedMapOf("sha256" to "f".repeat(64), "status" to "created", "path" to "x"))
        val up = uploader(sp, FakeIngest(listOf(201 to body)))
        assertEquals(RETRY, up.runOnce().single().action)
        assertEquals(1, sp.entries("pending").size)
        // an unparseable ack body is the same: keep the file
        val up2 = uploader(sp, FakeIngest(listOf(200 to "<html>proxy</html>")))
        assertEquals(RETRY, up2.runOnce().single().action)
        assertEquals(1, sp.entries("pending").size)
    }

    @Test
    fun backoffBoundsAndReset() {
        val rng = Random(1)
        for (k in 0 until 12) repeat(20) {
            val b = UploadPolicy.backoffS(k, 300.0, rng)
            assertTrue(b >= 0 && b <= minOf(300.0, Math.pow(2.0, k.toDouble())))
        }
        assertTrue(UploadPolicy.backoffS(40, 300.0, rng) <= 300)
        val sp = spoolWith(1)
        val up = uploader(sp, FakeIngest(listOf(500 to null, 500 to null, 500 to null, 201 to null)))
        for (expect in 1..3) {
            val outs = up.runOnce()
            assertEquals(expect, up.failures)
            assertTrue(up.nextDelayS(outs) <= Math.pow(2.0, expect.toDouble()))
        }
        val outs = up.runOnce()
        assertTrue(outs[0].action == ACK && up.failures == 0 && up.nextDelayS(outs) == 0.0)
        assertEquals(up.idlePollS, up.nextDelayS(emptyList()))
    }

    @Test
    fun unreadableSidecarGoesToFailed() {
        val sp = spoolWith(1)
        sp.entries("pending")[0].json.writeText("{not json")
        val srv = FakeIngest(listOf(201 to null))
        val o = uploader(sp, srv).runOnce().single()
        assertEquals(FAIL, o.action)
        assertNull(o.status)
        assertTrue(srv.requests.isEmpty())
        assertEquals(1, sp.stats().failedFiles)
    }

    @Test
    fun otherDeviceIdsAreSkippedNotUploadedAndNeverBlockTheQueue() {
        // An s22 segment at the head of pending/ (recorded before the id was set to lass22):
        // the lass22 token would get 403 forever; it must be kept and passed over.
        val sp = Spool(File(tmp, "spool"))
        for ((i, dev) in listOf("s22", "lass22", "s22", "lass22").withIndex()) {
            sp.write(
                "OggS-$dev-$i".toByteArray(),
                Sidecar.build(
                    deviceId = dev, sha256 = "0".repeat(64), utcNs = 1_790_000_000_000_000_000L + i * 30_000_000_000L,
                    nStart = i * 480000L, nSamples = 480000, runId = run, epoch = 0, discontinuity = false,
                    clockStep = false, clockSynced = false, cutReason = "cap", vad = null, edgeVersion = "t", kind = "raw",
                ),
            )
        }
        val srv = FakeIngest(listOf(201 to null))
        val up = Uploader(sp, srv::put, rng = Random(0), deviceId = "lass22")
        val outs = up.runOnce()
        assertEquals(listOf("OggS-lass22-1", "OggS-lass22-3"), srv.requests.map { it.opus.decodeToString() })
        assertEquals(listOf(ACK, ACK), outs.map { it.action })
        assertEquals(2, up.skippedForeign)
        assertEquals(listOf("s22", "s22"), sp.entries("pending").map { it.readMeta()["device_id"] }) // kept
        assertEquals(0, up.failures)
        assertEquals(up.idlePollS, up.nextDelayS(up.runOnce())) // nothing left to do: idle, not busy
        assertEquals(2, up.skippedForeign)
    }

    @Test
    fun classifyUnknownStatusIsRetry() {
        assertTrue(UploadPolicy.classify(418) == RETRY && UploadPolicy.classify(302) == RETRY)
        assertEquals(RETRY, UploadPolicy.classify(411)) // server: missing Content-Length; never failed/
        assertTrue(UploadPolicy.classify(200) == ACK && UploadPolicy.classify(201) == ACK)
        assertEquals(setOf(FAIL), listOf(409, 413, 422).map(UploadPolicy::classify).toSet())
    }
}
