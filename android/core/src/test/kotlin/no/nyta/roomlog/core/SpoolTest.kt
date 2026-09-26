package no.nyta.roomlog.core

import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/** Port of the spool cases in `edge/tests/test_encode_spool.py`. */
class SpoolTest {
    private lateinit var tmp: File
    private val run = "b0d0f1a2-3c4d-4e5f-8a9b-0c1d2e3f4a5b"

    @BeforeTest
    fun setUp() {
        tmp = Files.createTempDirectory("spool").toFile()
    }

    @AfterTest
    fun tearDown() {
        tmp.deleteRecursively()
    }

    private fun meta(start: String = "2026-09-26T10:15:32.417Z", vararg over: Pair<String, Any?>): LinkedHashMap<String, Any?> {
        val m = Sidecar.build(
            deviceId = "oma", sha256 = "0".repeat(64), utcNs = 0, nStart = 0, nSamples = 16000, runId = run,
            epoch = 0, discontinuity = false, clockStep = false, clockSynced = true, cutReason = "silence",
            vad = emptyMap(), edgeVersion = "0.1.0",
        )
        m["start_utc"] = start
        m.putAll(over)
        return m
    }

    private fun mv(from: File, to: File) {
        Files.move(from.toPath(), to.toPath(), StandardCopyOption.REPLACE_EXISTING)
    }

    private fun spool() = Spool(File(tmp, "spool"))

    @Test
    fun writeRenameAndNaming() {
        val sp = spool()
        val data = "OggS-fake-bytes".toByteArray()
        val e = sp.write(data, meta())
        val sha = Spool.sha256Hex(data)
        assertEquals("20260926T101532417Z_${sha.take(8)}", e.stem)
        assertTrue(e.opus.parentFile.name == "pending" && e.json.exists())
        assertEquals(sha, e.readMeta()["sha256"])
        assertEquals(emptyList(), Sidecar.validate(e.readMeta()))
        assertEquals(emptyList(), sp.dir("tmp").list()!!.toList())
        val st = sp.stats()
        assertTrue(st.pendingFiles == 1 && st.pendingBytes > data.size)
        // the file form is the Python one: indent=2 plus a trailing newline, ASCII only
        val text = e.json.readText()
        assertTrue(text.startsWith("{\n  \"schema_version\": 1,\n") && text.endsWith("}\n"))
        assertEquals(sha, Spool.sha256Hex(e.opus.readBytes()))
    }

    @Test
    fun lexicalOrderIsTimeOrder() {
        val sp = spool()
        sp.write("b".toByteArray(), meta(start = "2026-09-26T10:15:33.000Z"))
        sp.write("a".toByteArray(), meta(start = "2026-09-26T10:15:32.000Z"))
        sp.write("c".toByteArray(), meta(start = "2026-09-27T00:00:00.000Z"))
        val stems = sp.entries("pending").map { it.stem }
        assertEquals(stems.sorted(), stems)
        assertTrue(stems[0].startsWith("20260926T101532000Z"))
    }

    @Test
    fun cleanupTmpAndMove() {
        val sp = spool()
        File(sp.dir("tmp"), "x.opus.tmp").writeBytes("junk".toByteArray())
        File(sp.dir("tmp"), "x.json.tmp").writeBytes("junk".toByteArray())
        assertEquals(2, sp.cleanupTmp())
        val e = sp.write("data".toByteArray(), meta())
        val f = sp.move(e, "failed")
        assertTrue(f.opus.parentFile.name == "failed" && !e.opus.exists())
        assertTrue(sp.stats().failedFiles == 1 && sp.stats().pendingFiles == 0)
        sp.delete(f)
        assertEquals(0, sp.stats().failedFiles)
    }

    @Test
    fun rewriteMetaRestampsAndRenames() {
        val sp = spool()
        val e = sp.write("data".toByteArray(), meta("2026-09-26T10:15:32.417Z", "clock_synced" to false), dest = "unsynced")
        val m = e.readMeta()
        m["start_utc"] = "2026-09-26T11:00:00.000Z"
        m["clock_synced"] = true
        val n = sp.rewriteMeta(e, m, "pending")
        assertTrue(n.stem.startsWith("20260926T110000000Z_"))
        assertEquals(true, n.readMeta()["clock_synced"])
        assertTrue(!e.opus.exists() && !e.json.exists())
        assertEquals(emptyList(), sp.entries("unsynced"))
    }

    @Test
    fun cleanupTmpFinishesHalfRenamedPairs() {
        val sp = spool()
        // crash after the .opus rename, before the .json rename
        val e = sp.write("audio".toByteArray(), meta())
        mv(e.json, File(sp.dir("tmp"), "${e.stem}.json.tmp"))
        assertEquals(emptyList(), sp.entries("pending"))
        assertEquals(0, sp.stats().pendingFiles) // the orphan is not counted
        sp.cleanupTmp()
        assertEquals(listOf(e.stem), sp.entries("pending").map { it.stem })
        assertTrue(e.json.exists() && sp.dir("tmp").list()!!.isEmpty())
        // same for unsynced/
        val u = sp.write("held".toByteArray(), meta("2026-09-26T10:15:32.417Z", "clock_synced" to false), dest = "unsynced")
        mv(u.json, File(sp.dir("tmp"), "${u.stem}.json.tmp"))
        sp.cleanupTmp()
        assertEquals(listOf(u.stem), sp.entries("unsynced").map { it.stem })
        // a sidecar without audio, and a lone .opus.tmp, are removed
        File(sp.dir("pending"), "20260101T000000000Z_00000000.json").writeBytes("{}".toByteArray())
        File(sp.dir("tmp"), "x.opus.tmp").writeBytes("junk".toByteArray())
        File(sp.dir("tmp"), "y.json.tmp").writeBytes("junk".toByteArray())
        assertEquals(3, sp.cleanupTmp())
        assertTrue(sp.dir("tmp").list()!!.isEmpty())
    }

    @Test
    fun diskGuard() {
        val sp = Spool(File(tmp, "spool"), maxBytes = 100, minFreeFraction = 0.05)
        assertTrue(sp.diskOk(Spool.Usage(total = 1000, free = 500)))
        assertFalse(sp.diskOk(Spool.Usage(total = 1000, free = 40)))
        sp.write(ByteArray(200) { 'x'.code.toByte() }, meta())
        assertFalse(sp.diskOk(Spool.Usage(total = 1000, free = 500)))
        assertEquals(1, sp.stats().pendingFiles) // nothing was deleted
        assertTrue(sp.entries("pending")[0].opus.exists())
        assertTrue(Spool(File(tmp, "other")).diskOk()) // real filesystem numbers
    }

    @Test
    fun writeRejectsOtherDestinationsAndSyncsDirectories() {
        val synced = mutableListOf<String>()
        val sp = Spool(File(tmp, "spool"), dirSync = { synced += it.name })
        assertFailsWith<IllegalArgumentException> { sp.write("x".toByteArray(), meta(), dest = "failed") }
        sp.write("x".toByteArray(), meta(), dest = "unsynced")
        assertEquals(listOf("unsynced"), synced)
        Spool.fsyncDir(sp.dir("pending")) // the real one does not throw on this platform
    }
}
