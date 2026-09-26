package no.nyta.roomlog.core

import java.io.File
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/** Port of `edge/tests/test_sidecar_config.py` (sidecar part) plus wire-format
 *  byte equality against strings produced by the Python module. */
class SidecarTest {
    private val run = "b0d0f1a2-3c4d-4e5f-8a9b-0c1d2e3f4a5b"

    @Test
    fun contractExamplesValidate() {
        val examples = TestSupport.contractDir.resolve("examples").listFiles { f -> f.name.endsWith(".json") }!!
            .sortedBy { it.name }
        assertTrue(examples.size >= 5, "expected the five contract examples, got ${examples.map { it.name }}")
        for (p in examples) {
            val meta = Json.parseObject(p.readText())
            assertEquals(emptyList(), Sidecar.validate(meta), p.name)
        }
        assertTrue(examples.any { Json.parseObject(it.readText())["kind"] == "raw" })
    }

    @Test
    fun contractExamplesRoundTripThroughWriter() {
        // every example re-serialised compactly must parse back to the same map
        for (p in TestSupport.contractDir.resolve("examples").listFiles { f -> f.name.endsWith(".json") }!!) {
            val meta = Json.parseObject(p.readText())
            val wire = Sidecar.dumpsWire(meta)
            assertTrue(wire.all { it.code in 0x21..0x7e }, "wire must be printable ASCII without spaces: $wire")
            assertEquals(meta, Json.parseObject(wire))
        }
    }

    @Test
    fun validateCatchesProblems() {
        val good = linkedMapOf<String, Any?>(
            "schema_version" to 1L, "device_id" to "oma", "sha256" to "a".repeat(64),
            "start_utc" to "2026-09-26T10:15:32.417Z", "duration_s" to 1.5,
            "run_id" to run, "epoch" to 0L,
            "discontinuity" to false, "clock_synced" to true,
        )
        fun with(k: String, v: Any?) = LinkedHashMap(good).apply { put(k, v) }
        assertEquals(emptyList(), Sidecar.validate(good))
        assertEquals(listOf("device_id pattern"), Sidecar.validate(with("device_id", "Bad_ID")))
        assertEquals(listOf("start_utc pattern"), Sidecar.validate(with("start_utc", "2026-09-26T10:15:32Z")))
        assertEquals(listOf("duration_s range"), Sidecar.validate(with("duration_s", 0L)))
        assertEquals(listOf("cut_reason"), Sidecar.validate(with("cut_reason", "oops")))
        assertEquals(listOf("discontinuity must be boolean"), Sidecar.validate(with("discontinuity", 1L)))
        assertEquals(listOf("kind"), Sidecar.validate(with("kind", "video")))
        assertEquals(emptyList(), Sidecar.validate(with("kind", "raw")))
        assertTrue("missing sha256" in Sidecar.validate(LinkedHashMap(good).apply { remove("sha256") }))
        assertEquals(listOf("sidecar is not an object"), Sidecar.validate(listOf(1)))
    }

    @Test
    fun formatAndCompactUtc() {
        val ns = 1_790_000_000_123_456_789L
        val s = Sidecar.formatUtc(ns)
        assertTrue(s.endsWith("Z") && s[19] == '.' && s.length == 24)
        val expect = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss").withZone(ZoneOffset.UTC)
            .format(Instant.ofEpochSecond(1_790_000_000)) + ".123Z"
        assertEquals(expect, s)
        assertEquals(expect.replace("-", "").replace(":", "").replace(".", ""), Sidecar.compactUtc(s))
        assertTrue(Sidecar.formatUtc(1_790_000_000_999_600_000L).endsWith(":21.000Z")) // rounds up
    }

    @Test
    fun durationMatchesPythonRound() {
        // python3 -c 'print(repr(round(n/16000, 3)))'
        val cases = mapOf(
            1L to "0.0", 7L to "0.0", 8L to "0.001", 24L to "0.002", 480000L to "30.0",
            479999L to "30.0", 16001L to "1.0", 123457L to "7.716",
        )
        for ((n, py) in cases) assertEquals(py, Json.dumps(Sidecar.durationS(n, 16000)), "n=$n")
    }

    @Test
    fun floatReprMatchesPython() {
        val cases = mapOf(
            0.5 to "0.5", 1e-05 to "1e-05", 0.0001 to "0.0001", 1e16 to "1e+16", 1.5e16 to "1.5e+16",
            123.456 to "123.456", 0.1 + 0.2 to "0.30000000000000004", 1e22 to "1e+22", 5e-324 to "5e-324",
        )
        for ((x, py) in cases) assertEquals(py, Json.pyRepr(x), "x=$x")
    }

    @Test
    fun buildAndWireAreAsciiOneLine() {
        val meta = Sidecar.build(
            deviceId = "oma", sha256 = "f".repeat(64), utcNs = 1_790_000_000_000_000_000L,
            nStart = 5, nSamples = 16000, runId = run, epoch = 2, discontinuity = true, clockStep = false,
            clockSynced = false, cutReason = "cap",
            vad = linkedMapOf("model" to "silero-vad", "version" to "v6.2.3", "threshold" to 0.5),
            edgeVersion = "0.1.0",
        )
        assertEquals(emptyList(), Sidecar.validate(meta))
        assertEquals(1.0, meta["duration_s"])
        assertTrue(meta.containsKey("session_hint") && meta["session_hint"] == null)
        assertTrue(meta.containsKey("multi_speaker") && meta["multi_speaker"] == null)
        val wire = Sidecar.dumpsWire(meta)
        assertTrue('\n' !in wire && wire.all { it.code < 128 } && ' ' !in wire.substringBefore("\"start_utc\""))
    }

    // Generated during authoring from edge/src/roomlog_edge/sidecar.py:
    //   m = sidecar.build(device_id="oma", sha256="f"*64, utc_ns=1_790_000_000_123_456_789, n_start=5,
    //       n_samples=197632, run_id=RUN, epoch=2, discontinuity=True, clock_step=False, clock_synced=False,
    //       cut_reason="silence", vad={"model": "silero-vad", "version": "v6.2.3", "threshold": 0.5},
    //       edge_version="0.1.0-æøå")
    //   sidecar.dumps_wire(m); sidecar.dumps_file(m)
    // Kotlin decodes \uXXXX even inside raw strings, so the backslash is spliced in.
    private val BS = "\\"
    private val pySpeechWire =
        """{"schema_version":1,"device_id":"oma","sha256":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff","start_utc":"2026-09-21T14:13:20.123Z","duration_s":12.352,"sample_rate":16000,"run_id":"b0d0f1a2-3c4d-4e5f-8a9b-0c1d2e3f4a5b","epoch":2,"n_start":5,"n_samples":197632,"discontinuity":true,"clock_step":false,"clock_synced":false,"cut_reason":"silence","vad":{"model":"silero-vad","version":"v6.2.3","threshold":0.5},"edge_version":"0.1.0-${BS}u00e6${BS}u00f8${BS}u00e5","session_hint":null,"multi_speaker":null}"""
    private val pySpeechFile =
        "{\n  \"schema_version\": 1,\n  \"device_id\": \"oma\",\n  \"sha256\": \"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff\",\n  \"start_utc\": \"2026-09-21T14:13:20.123Z\",\n  \"duration_s\": 12.352,\n  \"sample_rate\": 16000,\n  \"run_id\": \"b0d0f1a2-3c4d-4e5f-8a9b-0c1d2e3f4a5b\",\n  \"epoch\": 2,\n  \"n_start\": 5,\n  \"n_samples\": 197632,\n  \"discontinuity\": true,\n  \"clock_step\": false,\n  \"clock_synced\": false,\n  \"cut_reason\": \"silence\",\n  \"vad\": {\n    \"model\": \"silero-vad\",\n    \"version\": \"v6.2.3\",\n    \"threshold\": 0.5\n  },\n  \"edge_version\": \"0.1.0-\\u00e6\\u00f8\\u00e5\",\n  \"session_hint\": null,\n  \"multi_speaker\": null\n}\n"

    //   r = sidecar.build(device_id="s22", sha256="c"*64, utc_ns=1_790_000_030_000_400_000, n_start=1440000,
    //       n_samples=120000, run_id="5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b", epoch=0, discontinuity=False,
    //       clock_step=True, clock_synced=True, cut_reason="shutdown", vad={}, edge_version="android-0.1.0")
    //   del r["vad"]; r = {"schema_version": 1, "kind": "raw", **r}; sidecar.dumps_wire(r)
    private val pyRawWire =
        """{"schema_version":1,"kind":"raw","device_id":"s22","sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","start_utc":"2026-09-21T14:13:50.000Z","duration_s":7.5,"sample_rate":16000,"run_id":"5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b","epoch":0,"n_start":1440000,"n_samples":120000,"discontinuity":false,"clock_step":true,"clock_synced":true,"cut_reason":"shutdown","edge_version":"android-0.1.0","session_hint":null,"multi_speaker":null}"""

    @Test
    fun wireAndFileBytesEqualPython() {
        val meta = Sidecar.build(
            deviceId = "oma", sha256 = "f".repeat(64), utcNs = 1_790_000_000_123_456_789L, nStart = 5,
            nSamples = 197632, runId = run, epoch = 2, discontinuity = true, clockStep = false,
            clockSynced = false, cutReason = "silence",
            vad = linkedMapOf("model" to "silero-vad", "version" to "v6.2.3", "threshold" to 0.5),
            edgeVersion = "0.1.0-æøå",
        )
        assertEquals(pySpeechWire, Sidecar.dumpsWire(meta))
        assertEquals(pySpeechFile, Sidecar.dumpsFile(meta))
        // and the file form parses back to the same sidecar
        assertEquals(meta, Json.parseObject(Sidecar.dumpsFile(meta)))
    }

    @Test
    fun rawWireBytesEqualPython() {
        val meta = Sidecar.build(
            deviceId = "s22", sha256 = "c".repeat(64), utcNs = 1_790_000_030_000_400_000L, nStart = 1_440_000,
            nSamples = 120_000, runId = "5e6f7a8b-9c0d-4e1f-8a2b-3c4d5e6f7a8b", epoch = 0, discontinuity = false,
            clockStep = true, clockSynced = true, cutReason = "shutdown", vad = null,
            edgeVersion = "android-0.1.0", kind = "raw",
        )
        assertEquals(emptyList(), Sidecar.validate(meta))
        assertEquals(pyRawWire, Sidecar.dumpsWire(meta))
        val keys = meta.keys.toList()
        assertEquals(listOf("schema_version", "kind", "device_id"), keys.take(3))
        assertTrue("vad" !in keys)
    }

    @Test
    fun jsonParserHandlesEscapesAndRejectsGarbage() {
        val m = Json.parseObject("""{"a": "x\"y\\z${BS}u00e6\n", "b": [1, -2.5e3, true, null], "c": {}}""")
        assertEquals("x\"y\\zæ\n", m["a"])
        assertEquals(listOf(1L, -2500.0, true, null), m["b"])
        assertEquals(emptyMap<String, Any?>(), m["c"])
        for (bad in listOf("", "{", "{\"a\":}", "[1,]x", "{\"a\":1} 2", "nul")) {
            kotlin.test.assertFailsWith<Json.ParseError>(bad) { Json.parse(bad) }
        }
    }
}

object TestSupport {
    val repoRoot: File = File(
        System.getProperty("roomlog.repoRoot") ?: error("roomlog.repoRoot system property not set"),
    )
    val contractDir: File get() = repoRoot.resolve("contract")
}
